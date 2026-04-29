#!/usr/bin/env python3
"""Plouto Sync — privacy-first Claude Code activity exporter.

Zero dependencies. Runs entirely on the developer's machine.

Walks ~/.claude/projects/ and ~/.claude/telemetry/, extracts session and
API-error metadata, and POSTs it to the Plouto API. The privacy
whitelist below defines exactly which fields leave the machine.
Everything else is dropped.

What is exported:
    - Session: id, project path, git branch, CLI version, timestamps
    - Turn: id, type, timestamp, model, token counts, tool name
    - API error: event id, error_type enum, attempt, duration, model
    - Identity: git user.email + user.name (for attribution)

What is NEVER exported:
    - Prompt text, response text, thinking blocks
    - File contents, tool inputs, tool results
    - Any content from the conversation
    - Email, device ID, org/account UUIDs from telemetry

Usage:
    python3 plouto-sync.py --api-url https://api.plouto.ai --token <bearer>
    python3 plouto-sync.py --api-url ... --token ... --session-only <id>

Audit this file: https://github.com/PloutoAI/plouto-plugin/blob/main/plouto/bin/plouto-sync.py
"""

from __future__ import annotations

import base64
import binascii
import json
import subprocess
import sys
import urllib.error
import urllib.request
from functools import partial
from pathlib import Path

# Unbuffered output so Claude Code sees progress immediately.
print = partial(print, flush=True)

# ─── Privacy whitelist ───────────────────────────────────────────────
#
# ONLY these fields are extracted. Everything else is dropped before
# any data leaves the machine. This is the trust boundary.


def _extract_session(line: dict) -> dict | None:
    if line.get("type") not in ("user", "assistant"):
        return None
    sid = line.get("sessionId")
    if not sid:
        return None
    return {
        "id": sid,
        "workspace_id": "default",
        "cwd": line.get("cwd", ""),
        "git_branch": line.get("gitBranch"),
        "cli_version": line.get("version"),
        "user_type": line.get("userType"),
        "entrypoint": line.get("entrypoint"),
        "started_at": line.get("timestamp"),
        "permission_mode": line.get("permissionMode"),
    }


def _extract_turn(line: dict) -> dict | None:
    uid = line.get("uuid")
    if not uid:
        return None
    turn_type = line.get("type")
    if turn_type not in ("user", "assistant", "tool_result"):
        return None

    msg = line.get("message", {}) or {}
    usage = msg.get("usage", {}) or {}
    cache = usage.get("cache_creation", {}) or {}
    server_tools = usage.get("server_tool_use", {}) or {}
    content = msg.get("content", []) or []

    # Tool names only — never input or content.
    tool_names = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            name = block.get("name")
            if name:
                tool_names.append(name)

    # Count content block types (text, thinking, tool_use, tool_result, image).
    block_counts: dict[str, int] = {}
    for block in content:
        if isinstance(block, dict):
            bt = block.get("type", "unknown")
            block_counts[bt] = block_counts.get(bt, 0) + 1

    return {
        "uuid": uid,
        "session_id": line.get("sessionId"),
        "workspace_id": "default",
        "parent_uuid": line.get("parentUuid"),
        "is_sidechain": bool(line.get("isSidechain")),
        "turn_type": turn_type,
        "timestamp": line.get("timestamp"),
        "model_id": msg.get("model"),
        "stop_reason": msg.get("stop_reason"),
        # Token counts
        "input_tokens": usage.get("input_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
        "cache_read_tokens": usage.get("cache_read_input_tokens", 0),
        "cache_creation_5m_tokens": cache.get("ephemeral_5m_input_tokens", 0),
        "cache_creation_1h_tokens": cache.get("ephemeral_1h_input_tokens", 0),
        # Server tool usage
        "web_search_count": server_tools.get("web_search_requests", 0),
        "web_fetch_count": server_tools.get("web_fetch_requests", 0),
        # Tool metadata — names only, never input/output
        "tool_name": tool_names[0] if tool_names else None,
        "tool_names": tool_names,
        "tool_count": len(tool_names),
        # Content shape (no actual content)
        "block_counts": block_counts,
        "has_thinking": block_counts.get("thinking", 0) > 0,
        "has_image": block_counts.get("image", 0) > 0,
        # Performance metadata
        "speed": usage.get("speed"),
        "service_tier": usage.get("service_tier"),
    }


# ─── Activity classification + retry counting ───────────────────────
#
# Per-session, group consecutive entries by the user message that
# spawned them, then classify the logical turn into one of the
# allow-listed categories below and count Edit→Bash→Edit retry cycles.
# All derived fields (logical_turn_id, activity, retries) are computed
# on this side of the wire — no prompt text ever ships, only the
# resulting enum value and integer.

import re as _re

_ACTIVITY_ALLOWLIST = frozenset({
    "planning", "delegation", "coding", "debugging", "refactoring",
    "feature", "exploration", "testing", "git", "build",
    "brainstorming", "conversation",
})

_EDIT_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})
_READ_TOOLS = frozenset({"Read", "Grep", "Glob"})
_BASH_TOOLS = frozenset({"Bash", "BashOutput"})
_TASK_TOOLS = frozenset({
    "TaskCreate", "TaskUpdate", "TaskGet", "TaskList",
    "TaskOutput", "TaskStop", "TodoWrite",
})
_SEARCH_TOOLS = frozenset({"WebSearch", "WebFetch", "ToolSearch"})
# Both 'Agent' (older) and 'Task' (newer) signal a subagent spawn.
_AGENT_TOOLS = frozenset({"Agent", "Task"})

_TEST_PATTERNS = _re.compile(
    r"\b(test|pytest|vitest|jest|mocha|spec|coverage|npm\s+test|npx\s+vitest|npx\s+jest)\b",
    _re.IGNORECASE,
)
_GIT_PATTERNS = _re.compile(
    r"\bgit\s+(push|pull|commit|merge|rebase|checkout|branch|stash|log|diff|status|add|reset|cherry-pick|tag)\b",
    _re.IGNORECASE,
)
_BUILD_PATTERNS = _re.compile(
    r"\b(npm\s+run\s+build|npm\s+publish|pip\s+install|docker|deploy|make\s+build|npm\s+run\s+dev|npm\s+start|pm2|systemctl|brew|cargo\s+build)\b",
    _re.IGNORECASE,
)
_INSTALL_PATTERNS = _re.compile(
    r"\b(npm\s+install|pip\s+install|brew\s+install|apt\s+install|cargo\s+add)\b",
    _re.IGNORECASE,
)

_DEBUG_KEYWORDS = _re.compile(
    r"\b(fix|bug|error|broken|failing|crash|issue|debug|traceback|exception|stack\s*trace|not\s+working|wrong|unexpected|status\s+code|404|500|401|403)\b",
    _re.IGNORECASE,
)
_FEATURE_KEYWORDS = _re.compile(
    r"\b(add|create|implement|new|build|feature|introduce|set\s*up|scaffold|generate|make\s+(?:a|me|the)|write\s+(?:a|me|the))\b",
    _re.IGNORECASE,
)
_REFACTOR_KEYWORDS = _re.compile(
    r"\b(refactor|clean\s*up|rename|reorganize|simplify|extract|restructure|move|migrate|split)\b",
    _re.IGNORECASE,
)
_BRAINSTORM_KEYWORDS = _re.compile(
    r"\b(brainstorm|idea|what\s+if|explore|think\s+about|approach|strategy|design|consider|how\s+should|what\s+would|opinion|suggest|recommend)\b",
    _re.IGNORECASE,
)
_RESEARCH_KEYWORDS = _re.compile(
    r"\b(research|investigate|look\s+into|find\s+out|check|search|analyze|review|understand|explain|how\s+does|what\s+is|show\s+me|list|compare)\b",
    _re.IGNORECASE,
)
_FILE_PATTERNS = _re.compile(
    r"\.(py|js|ts|tsx|jsx|json|yaml|yml|toml|sql|sh|go|rs|java|rb|php|css|html|md|csv|xml)\b",
    _re.IGNORECASE,
)
_SCRIPT_PATTERNS = _re.compile(
    r"\b(run\s+\S+\.\w+|execute|scrip?t|curl|api\s+\S+|endpoint|request\s+url|fetch\s+\S+|query|database|db\s+\S+)\b",
    _re.IGNORECASE,
)
_URL_PATTERN = _re.compile(r"https?://\S+", _re.IGNORECASE)


def _user_msg_text(line: dict) -> str:
    """Extract user prompt text. Used for classification only — the text
    is consumed in-process and never serialised onto the wire."""
    msg = line.get("message") or {}
    if msg.get("role") != "user":
        return ""
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                t = block.get("text")
                if isinstance(t, str):
                    parts.append(t)
        return " ".join(parts)
    return ""


def _classify_no_tools(user_msg: str) -> str:
    if _BRAINSTORM_KEYWORDS.search(user_msg):
        return "brainstorming"
    if _RESEARCH_KEYWORDS.search(user_msg):
        return "exploration"
    if _DEBUG_KEYWORDS.search(user_msg):
        return "debugging"
    if _FEATURE_KEYWORDS.search(user_msg):
        return "feature"
    if _FILE_PATTERNS.search(user_msg):
        return "coding"
    if _SCRIPT_PATTERNS.search(user_msg):
        return "coding"
    if _URL_PATTERN.search(user_msg):
        return "exploration"
    return "conversation"


def _classify_logical_turn(user_msg: str, tool_seq: list) -> str:
    """tool_seq is list[list[str]] — one inner list per assistant call,
    each containing the tool names invoked in that call. We flatten for
    presence checks but preserve order for retry detection."""
    flat = [t for call_tools in tool_seq for t in call_tools]
    if not flat:
        return _classify_no_tools(user_msg)

    if any(t == "EnterPlanMode" for t in flat):
        return "planning"
    if any(t in _AGENT_TOOLS for t in flat):
        return "delegation"

    has_edits = any(t in _EDIT_TOOLS for t in flat)
    has_reads = any(t in _READ_TOOLS for t in flat)
    has_bash = any(t in _BASH_TOOLS for t in flat)
    has_tasks = any(t in _TASK_TOOLS for t in flat)
    has_search = any(t in _SEARCH_TOOLS for t in flat)
    has_mcp = any(t.startswith("mcp__") for t in flat)

    if has_bash and not has_edits:
        if _TEST_PATTERNS.search(user_msg):
            return "testing"
        if _GIT_PATTERNS.search(user_msg):
            return "git"
        if _BUILD_PATTERNS.search(user_msg) or _INSTALL_PATTERNS.search(user_msg):
            return "build"

    if has_edits:
        if _DEBUG_KEYWORDS.search(user_msg):
            return "debugging"
        if _REFACTOR_KEYWORDS.search(user_msg):
            return "refactoring"
        if _FEATURE_KEYWORDS.search(user_msg):
            return "feature"
        return "coding"

    if has_bash and has_reads:
        return "exploration"
    if has_bash:
        return "coding"
    if has_search or has_mcp:
        return "exploration"
    if has_reads:
        return "exploration"
    if has_tasks:
        return "planning"

    return _classify_no_tools(user_msg)


def _count_retries(tool_seq: list) -> int:
    """Count Edit→Bash→Edit cycles inside a logical turn.

    A retry is when the model edits, runs a bash check, and then has to
    edit again. The count is per logical turn, denormalised onto each
    assistant call so the server can aggregate via MAX().
    """
    saw_edit = False
    saw_bash_after_edit = False
    retries = 0
    for call_tools in tool_seq:
        has_edit = any(t in _EDIT_TOOLS for t in call_tools)
        has_bash = any(t in _BASH_TOOLS for t in call_tools)
        if has_edit:
            if saw_bash_after_edit:
                retries += 1
            saw_edit = True
            saw_bash_after_edit = False
        if has_bash and saw_edit:
            saw_bash_after_edit = True
    return retries


def _classify_session_lines(lines: list) -> dict:
    """Walk a session's JSONL lines in order, group into logical turns
    by user message, and return ``{assistant_uuid: {logical_turn_id,
    activity, retries}}`` for every assistant call.

    Sidechain starts with no prior user message use the first assistant
    call's uuid as a stable group id.
    """
    out: dict = {}
    current_user_text = ""
    current_lt_id: str | None = None
    current_assistant_uuids: list = []
    current_tool_seq: list = []

    def _flush():
        if not current_assistant_uuids:
            return
        lt_id = current_lt_id or current_assistant_uuids[0]
        activity = _classify_logical_turn(current_user_text, current_tool_seq)
        if activity not in _ACTIVITY_ALLOWLIST:
            activity = None
        retries = _count_retries(current_tool_seq)
        for uid in current_assistant_uuids:
            out[uid] = {
                "logical_turn_id": lt_id,
                "activity": activity,
                "retries": retries,
            }

    for line in lines:
        if not isinstance(line, dict):
            continue
        line_type = line.get("type")
        if line_type == "user":
            _flush()
            current_user_text = _user_msg_text(line)
            current_lt_id = line.get("uuid")
            current_assistant_uuids = []
            current_tool_seq = []
        elif line_type == "assistant":
            uid = line.get("uuid")
            if not uid:
                continue
            msg = line.get("message") or {}
            content = msg.get("content") or []
            tools = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    name = block.get("name")
                    if name:
                        tools.append(name)
            current_assistant_uuids.append(uid)
            current_tool_seq.append(tools)
    _flush()
    return out


# ─── Telemetry error whitelist ───────────────────────────────────────
#
# Mirror of scalene/agent/extractor.py::extract_error. Source is
# ~/.claude/telemetry/1p_failed_events.*.json — Anthropic's buffered
# telemetry file. We pull ONLY:
#   - event_name (tengu_api_error / tengu_api_retry)
#   - event_id (for server-side dedup)
#   - session_id, model, client_timestamp
#   - error_type / status / attempt / durations / provider
#     (from base64-decoded additional_metadata, strict allow-list)
# We drop: email, device_id, auth.{organization,account}_uuid, env,
# process, betas, `error` free-text, clientRequestId, queryChainId.

_ERROR_EVENT_NAMES = {"tengu_api_error", "tengu_api_retry"}
_ERROR_META_WHITELIST = {
    "errorType", "status", "attempt",
    "durationMs", "durationMsIncludingRetries", "provider",
}


def _decode_error_metadata(blob) -> dict:
    """Decode `additional_metadata` base64 → JSON and filter to whitelist.
    Returns {} on any decode/parse failure — never raises.
    """
    if not isinstance(blob, str) or not blob:
        return {}
    try:
        raw = base64.b64decode(blob, validate=False)
        decoded = json.loads(raw)
    except (binascii.Error, ValueError, json.JSONDecodeError):
        return {}
    if not isinstance(decoded, dict):
        return {}
    return {k: v for k, v in decoded.items() if k in _ERROR_META_WHITELIST}


def _extract_error(line: dict) -> dict | None:
    """Extract metadata-only error row from one telemetry JSONL line."""
    if not isinstance(line, dict):
        return None
    if line.get("event_type") != "ClaudeCodeInternalEvent":
        return None
    data = line.get("event_data")
    if not isinstance(data, dict):
        return None

    event_name = data.get("event_name")
    if event_name not in _ERROR_EVENT_NAMES:
        return None

    event_id = data.get("event_id")
    ts = data.get("client_timestamp")
    if not event_id or not ts:
        return None

    meta = _decode_error_metadata(data.get("additional_metadata"))

    status_raw = meta.get("status")
    if isinstance(status_raw, (int, float)):
        status = str(int(status_raw))
    elif isinstance(status_raw, str) and status_raw:
        status = status_raw
    else:
        status = None

    def _int_or_none(v):
        return int(v) if isinstance(v, (int, float)) else None

    attempt = meta.get("attempt")
    return {
        "event_id": event_id,
        "session_id": data.get("session_id") or None,
        "event_name": event_name,
        "timestamp": ts,
        "model_id": data.get("model") or None,
        "error_type": meta.get("errorType") or None,
        "status": status,
        "attempt": int(attempt) if isinstance(attempt, (int, float)) else 0,
        "duration_ms": _int_or_none(meta.get("durationMs")),
        "duration_ms_total": _int_or_none(meta.get("durationMsIncludingRetries")),
        "provider": meta.get("provider") or None,
    }


# ─── File I/O ────────────────────────────────────────────────────────


def _iter_jsonl(path: Path):
    with path.open("r", encoding="utf-8") as f:
        for raw in f:
            raw = raw.strip()
            if raw:
                try:
                    yield json.loads(raw)
                except json.JSONDecodeError:
                    pass


def _find_session_files(root: Path):
    if not root.exists():
        return
    for project_dir in sorted(root.iterdir()):
        if project_dir.is_dir():
            yield from sorted(project_dir.rglob("*.jsonl"))


def _find_telemetry_files(root: Path):
    """Yield `~/.claude/telemetry/1p_failed_events.*.json` paths.

    The files have a `.json` extension but each line is its own JSON
    object (append-only JSONL), matching Anthropic's buffered-telemetry
    format.
    """
    if not root.exists():
        return
    for path in sorted(root.glob("1p_failed_events.*.json")):
        if path.is_file():
            yield path


# ─── Git identity ────────────────────────────────────────────────────


def _git_config(key: str) -> str | None:
    try:
        r = subprocess.run(
            ["git", "config", "--global", key],
            capture_output=True, text=True, timeout=2, check=False,
        )
        return r.stdout.strip() or None
    except Exception:
        return None


def _get_identity() -> dict | None:
    email = _git_config("user.email")
    if not email:
        return None
    identity: dict = {"email": email}
    name = _git_config("user.name")
    if name:
        identity["display_name"] = name
    return identity


# ─── API ─────────────────────────────────────────────────────────────


def _post(url: str, token: str, payload: dict, retries: int = 3) -> dict:
    import time
    data = json.dumps(payload).encode("utf-8")
    for attempt in range(retries):
        req = urllib.request.Request(
            url,
            data=data,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            try:
                body = e.read().decode()[:200]
            except Exception:
                body = "(unreadable)"
            if e.code >= 500 and attempt < retries - 1:
                wait = 2 * (attempt + 1)
                print(f"  HTTP {e.code}, retrying in {wait}s...", file=sys.stderr)
                time.sleep(wait)
                continue
            print(f"  HTTP {e.code}: {body}", file=sys.stderr)
            return {}
        except Exception as e:
            if attempt < retries - 1:
                wait = 2 * (attempt + 1)
                print(f"  error: {e}, retrying in {wait}s...", file=sys.stderr)
                time.sleep(wait)
                continue
            print(f"  error: {e}", file=sys.stderr)
            return {}
    return {}


# ─── Sync ────────────────────────────────────────────────────────────


BATCH_TURNS = 1000  # flush when turn buffer hits this size


def sync(api_url: str, token: str, root: Path, session_filter: str | None = None):
    """Walk JSONL files, buffer turns, flush in batches of ~200."""
    import time
    identity = _get_identity()
    ingest_url = f"{api_url.rstrip('/')}/api/ingest/sessions"

    print("Plouto Sync")
    print(f"  api:  {api_url}")
    print(f"  root: {root}")
    if identity:
        print(f"  user: {identity.get('email', '?')}")
    print()

    all_files = list(_find_session_files(root))
    print(f"Found {len(all_files)} JSONL files")

    sessions_total = 0
    turns_total = 0
    batches_sent = 0
    errors = 0
    seen_sessions: set[str] = set()
    seen_turns: set[str] = set()

    # Buffers — flushed when turn_buf hits BATCH_TURNS.
    session_buf: list[dict] = []
    turn_buf: list[dict] = []

    def _flush():
        nonlocal sessions_total, turns_total, batches_sent, errors
        if not session_buf and not turn_buf:
            return
        payload: dict = {"sessions": session_buf[:], "turns": turn_buf[:]}
        if identity:
            payload["agent_identity"] = identity
        result = _post(ingest_url, token, payload)
        s = result.get("sessions_upserted", 0)
        t = result.get("turns_upserted", 0)
        sessions_total += s
        turns_total += t
        batches_sent += 1
        if not result:
            errors += 1
        print(f"  batch {batches_sent}: {len(session_buf)}s {len(turn_buf)}t → {s}s {t}t")
        session_buf.clear()
        turn_buf.clear()
        time.sleep(0.1)

    for i, jsonl_path in enumerate(all_files):
        if session_filter and session_filter not in str(jsonl_path):
            continue

        # Materialise the file's lines so we can classify logical turns
        # before emitting their assistant calls. JSONL files are small
        # enough (a few MB at most) that this is cheap.
        file_lines = list(_iter_jsonl(jsonl_path))
        classifications = _classify_session_lines(file_lines)

        for line in file_lines:
            sm = _extract_session(line)
            if sm and sm["id"] not in seen_sessions:
                seen_sessions.add(sm["id"])
                sm["project_path_encoded"] = sm["cwd"].replace("/", "-")
                sm["jsonl_path"] = str(jsonl_path)
                sm["jsonl_offset"] = 0
                sm["total_turns"] = 0
                sm["is_subagent"] = 0
                session_buf.append(sm)

            tm = _extract_turn(line)
            if tm and tm["uuid"] not in seen_turns:
                seen_turns.add(tm["uuid"])
                tag = classifications.get(tm["uuid"])
                if tag:
                    tm["logical_turn_id"] = tag["logical_turn_id"]
                    tm["activity"] = tag["activity"]
                    tm["retries"] = tag["retries"]
                turn_buf.append(tm)

            if len(turn_buf) >= BATCH_TURNS:
                _flush()

        if (i + 1) % 200 == 0:
            print(f"  scanned {i + 1}/{len(all_files)} files...")

    # Final flush.
    _flush()

    print()
    print(f"Done. {sessions_total} sessions, {turns_total} turns in {batches_sent} batches.")
    if errors:
        print(f"  {errors} batches had errors.")
    return sessions_total, turns_total


def sync_errors(api_url: str, token: str, telemetry_root: Path):
    """Walk ~/.claude/telemetry/*.json and ship API-error events.

    Idempotent: the server dedups by `event_id` so re-running is free.
    Telemetry volume is tiny (a few events per outage), so we POST
    everything in one payload instead of batching.
    """
    identity = _get_identity()
    ingest_url = f"{api_url.rstrip('/')}/api/ingest/sessions"

    files = list(_find_telemetry_files(telemetry_root))
    if not files:
        return 0

    errors: list[dict] = []
    seen: set[str] = set()
    for path in files:
        for line in _iter_jsonl(path):
            ep = _extract_error(line)
            if ep is None:
                continue
            if ep["event_id"] in seen:
                continue
            seen.add(ep["event_id"])
            errors.append(ep)

    if not errors:
        return 0

    payload: dict = {"sessions": [], "turns": [], "errors": errors}
    if identity:
        payload["agent_identity"] = identity
    result = _post(ingest_url, token, payload)
    upserted = result.get("errors_upserted", 0)
    print(f"Telemetry: {len(errors)} errors scanned, {upserted} new")
    return upserted


def sync_history_stubs(api_url: str, token: str, history_path: Path, root: Path):
    """Import activity dates from history.jsonl for purged sessions.

    Claude Code purges old JSONL files but keeps a summary in
    ~/.claude/history.jsonl. This creates lightweight stub sessions
    (enough for the heatmap) for dates that have no real JSONL data.
    """
    if not history_path.exists():
        return

    entries = []
    with history_path.open("r", encoding="utf-8") as f:
        for raw in f:
            raw = raw.strip()
            if raw:
                try:
                    entries.append(json.loads(raw))
                except json.JSONDecodeError:
                    pass

    # Dates already covered by real JSONL data.
    covered_dates: set[str] = set()
    for jsonl_path in _find_session_files(root):
        for line in _iter_jsonl(jsonl_path):
            ts = line.get("timestamp", "")
            if ts and len(ts) >= 10:
                covered_dates.add(ts[:10])

    # Group history entries by (date, project).
    from collections import defaultdict
    from datetime import datetime

    stubs_by_key: dict[tuple, list] = defaultdict(list)
    for entry in entries:
        ts = entry.get("timestamp", 0)
        project = entry.get("project", "")
        if not (ts > 1_000_000_000_000 and project):
            continue
        dt = datetime.fromtimestamp(ts / 1000)
        date_str = dt.strftime("%Y-%m-%d")
        if date_str not in covered_dates:
            stubs_by_key[(date_str, project)].append(dt.isoformat())

    if not stubs_by_key:
        return

    import uuid

    identity = _get_identity()
    ingest_url = f"{api_url.rstrip('/')}/api/ingest/sessions"

    stub_sessions = []
    for (_date, project), timestamps in sorted(stubs_by_key.items()):
        stub_sessions.append({
            "id": str(uuid.uuid4()),
            "workspace_id": "default",
            "cwd": project,
            "project_path_encoded": project.replace("/", "-"),
            "started_at": min(timestamps),
            "total_turns": len(timestamps),
            "is_subagent": 0,
            "jsonl_path": "history.jsonl",
            "jsonl_offset": 0,
        })

    total = 0
    for i in range(0, len(stub_sessions), 50):
        batch = stub_sessions[i : i + 50]
        payload: dict = {"sessions": batch, "turns": []}
        if identity:
            payload["agent_identity"] = identity
        result = _post(ingest_url, token, payload)
        total += result.get("sessions_upserted", 0)

    print(f"Recovered {total} activity stubs from purged history")


def sync_bulk(api_url: str, token: str, root: Path):
    """Collect last 3 months locally, then upload in chunked requests."""
    import math
    import time
    from datetime import datetime as _dt, timedelta as _td

    identity = _get_identity()
    # Chunks skip score recompute — we trigger one at the end instead of
    # replaying ELO for every user on every chunk.
    ingest_url = f"{api_url.rstrip('/')}/api/ingest/sessions?skip_score=1"
    recompute_url = f"{api_url.rstrip('/')}/api/ingest/recompute-score"
    cutoff = (_dt.utcnow() - _td(days=90)).isoformat() + "Z"

    print("Plouto Bulk Sync (last 3 months)")
    print(f"  api:  {api_url}")
    print(f"  root: {root}")
    print(f"  cutoff: {cutoff[:10]}")
    if identity:
        print(f"  user: {identity.get('email', '?')}")
    print()

    # Phase 1: collect everything locally.
    all_files = list(_find_session_files(root))
    print(f"Found {len(all_files)} JSONL files")

    all_sessions: list[dict] = []
    all_turns: list[dict] = []
    seen_sessions: set[str] = set()
    seen_turns: set[str] = set()

    for i, jsonl_path in enumerate(all_files):
        # Skip files older than cutoff by checking file mtime first.
        try:
            if jsonl_path.stat().st_mtime < (_dt.utcnow() - _td(days=90)).timestamp():
                continue
        except OSError:
            continue

        file_lines = list(_iter_jsonl(jsonl_path))
        classifications = _classify_session_lines(file_lines)

        for line in file_lines:
            sm = _extract_session(line)
            if sm and sm["id"] not in seen_sessions:
                # Skip sessions before cutoff.
                if sm.get("started_at", "") < cutoff:
                    continue
                seen_sessions.add(sm["id"])
                sm["project_path_encoded"] = sm["cwd"].replace("/", "-")
                sm["jsonl_path"] = str(jsonl_path)
                sm["jsonl_offset"] = 0
                sm["total_turns"] = 0
                sm["is_subagent"] = 0
                all_sessions.append(sm)

            tm = _extract_turn(line)
            if tm and tm["uuid"] not in seen_turns:
                # Skip turns before cutoff.
                if tm.get("timestamp", "") < cutoff:
                    continue
                seen_turns.add(tm["uuid"])
                tag = classifications.get(tm["uuid"])
                if tag:
                    tm["logical_turn_id"] = tag["logical_turn_id"]
                    tm["activity"] = tag["activity"]
                    tm["retries"] = tag["retries"]
                all_turns.append(tm)

        if (i + 1) % 200 == 0:
            print(f"  scanned {i + 1}/{len(all_files)} files...")

    print(f"Collected {len(all_sessions)} sessions, {len(all_turns)} turns")
    print()

    # Phase 2: upload in chunks of 5000 turns each.
    CHUNK = 5000
    total_chunks = max(1, math.ceil(len(all_turns) / CHUNK))
    sessions_total = 0
    turns_total = 0
    errors = 0
    sent_session_ids: set[str] = set()

    for i in range(0, len(all_turns), CHUNK):
        chunk_idx = i // CHUNK + 1
        chunk_turns = all_turns[i : i + CHUNK]
        print(f"Uploading chunk {chunk_idx}/{total_chunks} ({len(chunk_turns)} turns)...")

        # Include sessions referenced by these turns.
        chunk_session_ids = {t["session_id"] for t in chunk_turns}
        chunk_sessions = [s for s in all_sessions if s["id"] in chunk_session_ids]
        sent_session_ids.update(s["id"] for s in chunk_sessions)

        payload: dict = {"sessions": chunk_sessions, "turns": chunk_turns}
        if identity:
            payload["agent_identity"] = identity

        result = _post(ingest_url, token, payload)
        s = result.get("sessions_upserted", 0)
        t = result.get("turns_upserted", 0)
        sessions_total += s
        turns_total += t
        if not result:
            errors += 1
        else:
            print(f"  → {s} sessions, {t} turns upserted")
        time.sleep(0.1)

    # Phase 3: send remaining sessions that had no turns.
    orphan_sessions = [s for s in all_sessions if s["id"] not in sent_session_ids]
    if orphan_sessions:
        print(f"Uploading {len(orphan_sessions)} sessions with no turns...")
        payload = {"sessions": orphan_sessions, "turns": []}
        if identity:
            payload["agent_identity"] = identity
        result = _post(ingest_url, token, payload)
        sessions_total += result.get("sessions_upserted", 0)

    # Phase 4: single score recompute now that every chunk has landed.
    print()
    print("Recomputing score...")
    try:
        result = _post(recompute_url, token, {})
        if result.get("ok"):
            print(f"  score = {result.get('score')}")
    except Exception as exc:
        print(f"  score recompute failed: {exc}")

    print()
    print(f"Done. {sessions_total} sessions, {turns_total} turns in {total_chunks} chunks.")
    if errors:
        print(f"  {errors} chunks had errors.")
    return sessions_total, turns_total


# ─── CLI ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Plouto Sync — export Claude Code metadata"
    )
    parser.add_argument("--api-url", required=True, help="Plouto API base URL")
    parser.add_argument("--token", required=True, help="Bearer token for authentication")
    parser.add_argument("--session-only", default=None, help="Sync a single session ID")
    parser.add_argument("--bulk", action="store_true", help="Bulk mode: collect all, upload once")
    parser.add_argument("--root", default=str(Path.home() / ".claude" / "projects"))
    parser.add_argument(
        "--telemetry-root",
        default=str(Path.home() / ".claude" / "telemetry"),
        help="Directory with Claude Code API-error telemetry files",
    )
    parser.add_argument(
        "--skip-errors",
        action="store_true",
        help="Skip the telemetry-errors pass",
    )
    args = parser.parse_args()

    if args.bulk:
        sync_bulk(args.api_url, args.token, Path(args.root))
    else:
        sync(args.api_url, args.token, Path(args.root), args.session_only)
        if not args.session_only:
            history = Path.home() / ".claude" / "history.jsonl"
            sync_history_stubs(args.api_url, args.token, history, Path(args.root))

    if not args.skip_errors and not args.session_only:
        sync_errors(args.api_url, args.token, Path(args.telemetry_root))
