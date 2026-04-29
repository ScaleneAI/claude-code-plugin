#!/usr/bin/env python3
"""PreToolUse hook step: prompt the user for approval when policy
is violated, instead of silently denying.

A flag file at ``~/.claude/plouto/policy-violation`` (set by
plouto-policy.py at SessionStart when the active model doesn't match
the workspace's required model) means we should pause every
code-touching tool call (Edit / Write / MultiEdit / NotebookEdit /
Bash / BashOutput / Task) and surface a real Allow/Deny dialog with
the policy gap.

Output schema follows the documented PreToolUse hook contract:

```json
{
  "hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "ask",
    "permissionDecisionReason": "<ASCII box with the gap + remedy>"
  }
}
```

Switching from ``deny`` to ``ask`` makes this an interactive
approval flow: the user gets a dialog every time, can choose to
Allow once (acknowledged the policy, proceed for this call) or
deny (fix it first via `/model` + `/plouto comply`). The reason
field carries the visual UI — Claude Code renders it as the
dialog's body.

When the flag is absent, this script exits silently and Claude Code
proceeds without prompting. Failures here MUST not block the user
(no flag → no prompt); a network or filesystem error never converts
into a tool gate.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


_FLAG = Path.home() / ".claude" / "plouto" / "policy-violation"
_GATED_TOOLS = {
    "Edit",
    "Write",
    "MultiEdit",
    "NotebookEdit",
    "Bash",
    "BashOutput",
    "Task",
}


def _short(model_id: str) -> str:
    """``claude-sonnet-4-6`` → ``Sonnet 4.6`` for the ASCII display."""
    if not model_id:
        return "—"
    s = model_id.replace("claude-", "")
    parts = s.split("-")
    if len(parts) >= 3:
        return f"{parts[0].title()} {parts[1]}.{parts[2]}"
    if len(parts) == 2:
        return f"{parts[0].title()} {parts[1]}"
    return model_id


def _box(required: str, current: str) -> str:
    """Render the policy-mismatch dialog body as a unicode box.

    Width is held at 56 chars so the box fits the typical Claude Code
    permission dialog without wrapping. The labels left-pad to align;
    the box-drawing chars (╭ ╮ ╯ ╰ │ ─ ├ ┤) render in any modern
    terminal.
    """
    req_full = required or "(unspecified)"
    cur_full = current or "(unknown)"
    req_short = _short(required)
    cur_short = _short(current)

    lines = [
        "╭───────────────────────────────────────────╮",
        "│  ⚠  PLOUTO WORKSPACE POLICY               │",
        "├───────────────────────────────────────────┤",
        f"│   Required:  {req_short:<28} │",
        f"│   Current:   {cur_short:<28} │",
        "├───────────────────────────────────────────┤",
        "│   To comply, in your next prompt:         │",
        "│                                           │",
        f"│     /model {req_full:<30} │",
        "│     /plouto comply                        │",
        "│                                           │",
        "│   Or click Allow to override once.        │",
        "╰───────────────────────────────────────────╯",
    ]
    return "\n".join(lines)


def main() -> None:
    if not _FLAG.exists():
        return  # no violation, allow

    try:
        raw = sys.stdin.read() or "{}"
        hook_input = json.loads(raw)
    except json.JSONDecodeError:
        return

    tool_name = hook_input.get("tool_name") or ""
    if tool_name not in _GATED_TOOLS:
        return  # only gate tools that touch code or shell

    required = ""
    current = ""
    try:
        body = _FLAG.read_text().strip()
        try:
            data = json.loads(body)
            if isinstance(data, dict):
                required = str(data.get("required") or "")
                current = str(data.get("current") or "")
        except json.JSONDecodeError:
            # Old-format flag (free-text). Fall through with the
            # raw body as the reason; the box still renders, just
            # without the structured fields.
            pass
    except OSError:
        pass

    payload = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "ask",
            "permissionDecisionReason": _box(required, current),
        }
    }
    sys.stdout.write(json.dumps(payload))


if __name__ == "__main__":
    main()
