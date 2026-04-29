#!/usr/bin/env python3
"""SessionStart hook step: fetch + apply the workspace policy.

Flow:
  1. Read the SessionStart hook input from stdin (Claude Code passes
     ``cwd`` and ``model`` among other fields).
  2. GET ${API_URL}/api/plugin/strategies (bearer-authed) for the
     workspace's policy_model + policy_text.
  3. If policy_model is set, write ``<cwd>/.claude/settings.local.json``
     so the next session in this directory boots on the right model
     (Claude Code reads that file on session start, per the docs).
  4. If the *current* session's model differs from the policy, drop a
     flag at ``~/.claude/plouto/policy-violation`` containing the
     mismatch detail. The PreToolUse gate (plouto-gate.py) reads this
     flag and denies tool calls until the user runs ``/plouto comply``
     or restarts the session.
  5. Emit the SessionStart additionalContext envelope on stdout so
     the agent sees the policy at the start of the conversation.

Failure modes are silent: the hook never blocks the session itself
(the gate handles in-session enforcement). If the API is down, the
existing settings.local.json is left as-is.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path


def _api_url() -> str:
    return os.environ.get("PLOUTO_API_URL") or os.environ.get("SCALENE_API_URL", "")


def _token() -> str:
    return os.environ.get("PLOUTO_TOKEN") or os.environ.get("SCALENE_TOKEN", "")


def _flag_path() -> Path:
    return Path.home() / ".claude" / "plouto" / "policy-violation"


def _settings_path(cwd: str) -> Path:
    return Path(cwd) / ".claude" / "settings.local.json"


def _log(msg: str) -> None:
    log = Path.home() / ".claude" / "plouto.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    try:
        with log.open("a") as f:
            f.write(msg.rstrip() + "\n")
    except OSError:
        pass


def _fetch_policy() -> dict | None:
    api_url = _api_url()
    token = _token()
    if not api_url or not token:
        _log("plouto-policy: missing PLOUTO_API_URL / PLOUTO_TOKEN, skipping")
        return None
    url = f"{api_url.rstrip('/')}/api/plugin/strategies"
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {token}"}
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.load(resp)
    except (urllib.error.HTTPError, urllib.error.URLError, OSError) as exc:
        _log(f"plouto-policy: fetch failed: {exc}")
        return None
    except json.JSONDecodeError as exc:
        _log(f"plouto-policy: malformed response: {exc}")
        return None


def _merge_settings(path: Path, model: str) -> None:
    """Write ``{"model": model, "availableModels": [model]}`` into ``path``,
    preserving any other keys.

    ``availableModels`` is the platform-supported allow-list — Claude Code
    refuses ``/model``, ``--model``, and ``ANTHROPIC_MODEL`` switches that
    fall outside it. Combined with ``model``, this both pins the boot
    default AND blocks the user from escaping to a non-policy model
    mid-session.
    """
    data: dict = {}
    if path.exists():
        try:
            data = json.loads(path.read_text() or "{}")
            if not isinstance(data, dict):
                data = {}
        except (json.JSONDecodeError, OSError):
            data = {}
    desired_avail = [model]
    if data.get("model") == model and data.get("availableModels") == desired_avail:
        return  # already compliant
    data["model"] = model
    data["availableModels"] = desired_avail
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.write_text(json.dumps(data, indent=2) + "\n")
        _log(f"plouto-policy: wrote model={model} availableModels=[{model}] to {path}")
    except OSError as exc:
        _log(f"plouto-policy: settings write failed ({path}): {exc}")


def _set_flag(required: str, current: str) -> None:
    """Persist a JSON flag the gate hook reads to render its ASCII prompt."""
    path = _flag_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"required": required, "current": current})
    try:
        path.write_text(payload + "\n")
        _log(
            f"plouto-policy: violation flag set: required={required} "
            f"current={current}"
        )
    except OSError as exc:
        _log(f"plouto-policy: flag write failed: {exc}")


def _clear_flag() -> None:
    path = _flag_path()
    try:
        path.unlink()
        _log("plouto-policy: violation flag cleared")
    except FileNotFoundError:
        pass
    except OSError as exc:
        _log(f"plouto-policy: flag clear failed: {exc}")


def _emit(additional_context: str | None) -> None:
    if not additional_context:
        return
    payload = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": additional_context,
        }
    }
    sys.stdout.write(json.dumps(payload))


def main() -> None:
    try:
        raw = sys.stdin.read() or "{}"
        hook_input = json.loads(raw)
    except json.JSONDecodeError:
        hook_input = {}

    cwd = hook_input.get("cwd") or os.getcwd()
    active_model = (
        hook_input.get("model")
        or hook_input.get("session", {}).get("model")
        or ""
    )

    policy = _fetch_policy()
    if policy is None:
        return

    policy_model = (policy.get("policy_model") or "").strip()
    policy_text = (policy.get("policy_text") or "").strip()

    if policy_model:
        _merge_settings(_settings_path(cwd), policy_model)

    notes: list[str] = []

    if policy_model and active_model and active_model != policy_model:
        _set_flag(required=policy_model, current=active_model)
        notes.append(
            f"Plouto workspace policy: required model is `{policy_model}`. "
            f"This session is on `{active_model}`. Tool calls will prompt "
            f"for approval until you run `/model {policy_model}` then "
            f"`/plouto comply` — or `/exit` and `claude --resume` to apply "
            f"the just-written settings.local.json."
        )
    else:
        # Either no constraint, or the user is already compliant.
        _clear_flag()
        if policy_model and active_model == policy_model:
            notes.append(
                f"Plouto workspace policy: model `{policy_model}` "
                f"(matches this session)."
            )

    if policy_text:
        notes.append(
            "Plouto workspace instructions:\n" + policy_text
        )

    _emit("\n\n".join(notes) if notes else None)


if __name__ == "__main__":
    main()
