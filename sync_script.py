#!/usr/bin/env python3
"""Scalene Sync — privacy-first Claude Code activity exporter.

Zero dependencies. Runs entirely on the developer's machine.

Walks ~/.claude/projects/, extracts session metadata from JSONL files,
and POSTs it to the Scalene API. The privacy whitelist below defines
exactly which fields leave the machine. Everything else is dropped.

What is exported:
    - Session: id, project path, git branch, CLI version, timestamps
    - Turn: id, type, timestamp, model, token counts, tool name
    - Identity: git user.email + user.name (for attribution)

What is NEVER exported:
    - Prompt text, response text, thinking blocks
    - File contents, tool inputs, tool results
    - Any content from the conversation

Usage:
    python3 sync_script.py --api-url https://scalene.example.com --token <bearer>
    python3 sync_script.py --api-url ... --token ... --session-only <id>

Audit this file: https://github.com/mtrbls/scalene-mcp/blob/main/sync_script.py
"""

from __future__ import annotations

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
    }


def _extract_turn(line: dict) -> dict | None:
    uid = line.get("uuid")
    if not uid:
        return None
    turn_type = line.get("type")
    if turn_type not in ("user", "assistant", "tool_result"):
        return None

    msg = line.get("message", {})
    usage = msg.get("usage", {})
    cache = usage.get("cache_creation", {})
    server_tools = msg.get("server_tool_use", {})

    # Tool name only — never input or content.
    tool_name = None
    for block in msg.get("content", []):
        if isinstance(block, dict) and block.get("type") == "tool_use":
            tool_name = block.get("name")
            break

    return {
        "uuid": uid,
        "session_id": line.get("sessionId"),
        "workspace_id": "default",
        "parent_uuid": line.get("parentUuid"),
        "turn_type": turn_type,
        "timestamp": line.get("timestamp"),
        "model_id": msg.get("model"),
        "input_tokens": usage.get("input_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
        "cache_read_tokens": usage.get("cache_read_input_tokens", 0),
        "cache_creation_5m_tokens": cache.get("five_minute_tokens", 0),
        "cache_creation_1h_tokens": cache.get("one_hour_tokens", 0),
        "web_search_count": server_tools.get("web_search", 0),
        "web_fetch_count": server_tools.get("web_fetch", 0),
        "tool_name": tool_name,
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


def _post(url: str, token: str, payload: dict) -> dict:
    data = json.dumps(payload).encode("utf-8")
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
        body = e.read().decode()[:200]
        print(f"  HTTP {e.code}: {body}", file=sys.stderr)
        return {}
    except Exception as e:
        print(f"  error: {e}", file=sys.stderr)
        return {}


# ─── Sync ────────────────────────────────────────────────────────────


def sync(api_url: str, token: str, root: Path, session_filter: str | None = None):
    """Walk JSONL session files and POST metadata to Scalene."""
    identity = _get_identity()
    ingest_url = f"{api_url.rstrip('/')}/api/ingest/sessions"

    print(f"Scalene Sync")
    print(f"  api:  {api_url}")
    print(f"  root: {root}")
    if identity:
        print(f"  user: {identity.get('email', '?')}")
    print()

    # Discover files first.
    all_files = list(_find_session_files(root))
    print(f"Found {len(all_files)} JSONL files")

    sessions_total = 0
    turns_total = 0
    batches_sent = 0
    errors = 0

    # Dedup across subagent files (they share UUIDs with parent).
    seen_sessions: set[str] = set()
    seen_turns: set[str] = set()

    for i, jsonl_path in enumerate(all_files):
        if session_filter and session_filter not in str(jsonl_path):
            continue

        sessions: list[dict] = []
        turns: list[dict] = []

        for line in _iter_jsonl(jsonl_path):
            sm = _extract_session(line)
            if sm and sm["id"] not in seen_sessions:
                seen_sessions.add(sm["id"])
                sm["project_path_encoded"] = sm["cwd"].replace("/", "-")
                sm["jsonl_path"] = str(jsonl_path)
                sm["jsonl_offset"] = 0
                sm["total_turns"] = 0
                sm["is_subagent"] = 0
                sessions.append(sm)

            tm = _extract_turn(line)
            if tm and tm["uuid"] not in seen_turns:
                seen_turns.add(tm["uuid"])
                turns.append(tm)

        if not sessions and not turns:
            continue

        payload: dict = {"sessions": sessions, "turns": turns}
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

        # Progress every 10 batches.
        if batches_sent % 10 == 0:
            print(f"  [{batches_sent}] {sessions_total} sessions, {turns_total} turns so far...")

    print()
    print(f"Done. {sessions_total} sessions, {turns_total} turns synced in {batches_sent} batches.")
    if errors:
        print(f"  {errors} batches had errors.")
    return sessions_total, turns_total


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


# ─── CLI ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Scalene Sync — export Claude Code metadata"
    )
    parser.add_argument("--api-url", required=True, help="Scalene API base URL")
    parser.add_argument("--token", required=True, help="Bearer token for authentication")
    parser.add_argument("--session-only", default=None, help="Sync a single session ID")
    parser.add_argument("--root", default=str(Path.home() / ".claude" / "projects"))
    args = parser.parse_args()

    sync(args.api_url, args.token, Path(args.root), args.session_only)

    if not args.session_only:
        history = Path.home() / ".claude" / "history.jsonl"
        sync_history_stubs(args.api_url, args.token, history, Path(args.root))
