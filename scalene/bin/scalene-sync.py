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

Audit this file: https://github.com/scaleneai/claude-code-plugin/blob/main/sync_script.py
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

    print("Scalene Sync")
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

        for line in _iter_jsonl(jsonl_path):
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

    print("Scalene Bulk Sync (last 3 months)")
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

        for line in _iter_jsonl(jsonl_path):
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
        description="Scalene Sync — export Claude Code metadata"
    )
    parser.add_argument("--api-url", required=True, help="Scalene API base URL")
    parser.add_argument("--token", required=True, help="Bearer token for authentication")
    parser.add_argument("--session-only", default=None, help="Sync a single session ID")
    parser.add_argument("--bulk", action="store_true", help="Bulk mode: collect all, upload once")
    parser.add_argument("--root", default=str(Path.home() / ".claude" / "projects"))
    args = parser.parse_args()

    if args.bulk:
        sync_bulk(args.api_url, args.token, Path(args.root))
    else:
        sync(args.api_url, args.token, Path(args.root), args.session_only)
        if not args.session_only:
            history = Path.home() / ".claude" / "history.jsonl"
            sync_history_stubs(args.api_url, args.token, history, Path(args.root))
