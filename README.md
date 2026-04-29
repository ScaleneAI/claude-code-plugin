# Plouto plugin

AI engineering intelligence for [Plouto](https://plouto.ai). Workspace observability and policy push-down for AI coding agents — see what your team actually spent on Claude Code, push down a recommended model strategy, track compliance and effect.

## Install

One line, cross-agent (Claude Code, Cursor, Codex):

```bash
npx plugins add PloutoAI/plouto-plugin
```

The runner auto-detects which agent tools you have on PATH and installs Plouto into each. Restart your agent to load.

### Credentials

After install, drop your workspace credentials into the agent's environment (find them in your [dashboard](https://plouto.ai/me) under Connect):

```bash
export PLOUTO_API_URL=https://api.plouto.ai
export PLOUTO_TOKEN=<your-bearer-token>
```

Legacy `SCALENE_API_URL` / `SCALENE_TOKEN` are honored as fallbacks.

That's it. The plugin registers SessionStart, PreToolUse, SessionEnd and StopFailure hooks. Each Claude Code session auto-syncs when it ends.

### Alternative: Claude Code marketplace

If you'd rather install through Claude Code's native marketplace flow:

```
/plugin marketplace add PloutoAI/plouto-plugin
/plugin install plouto
```

Same plugin, slightly more keystrokes.

## What the plugin does

| Hook | What it does |
| --- | --- |
| `SessionStart` | Fetches the workspace's recommended-model strategy, writes it to `.claude/settings.local.json` for the next session, and flags an in-session mismatch. |
| `PreToolUse` | When the active model is off-policy, surfaces a non-blocking dialog explaining the recommendation. The user can Allow (logged as off-policy) or Deny (and `/model`-switch). |
| `SessionEnd` | Reads the session's JSONL and POSTs metadata (no prompt text, no tool args, no file contents) to the Plouto dashboard. |
| `StopFailure` | Records rate-limit hits so the dashboard can show Claude Code outage exposure. |

There's also a `/plouto` slash command for setup, manual sync, audit, and dashboard links.

## Privacy

The sync script runs entirely on your machine. Only metadata crosses the network:

| Exported | Never exported |
| --- | --- |
| Session ID, timestamps | Prompt text |
| Token counts (input / output / cache) | Response text |
| Model id (e.g. `claude-opus-4-7`) | File contents |
| Tool name (e.g. `Edit`, `Bash`) | Tool inputs or outputs |
| Stop reason, content block types | Thinking blocks |
| Git branch, project path | Any conversation content |

The privacy whitelist is enforced on your machine, not the server. The sync script is stdlib Python with zero dependencies. Audit it under [`plouto/bin/`](./plouto/bin/).

## Telemetry

The plugin pings `${PLOUTO_API_URL}/api/ingest/event` on session start, on session end (full sync), and on rate-limit stop. The pings always include the session id and timestamp; the SessionEnd path additionally posts the privacy-bounded metadata above. Disable by unsetting `PLOUTO_TOKEN`.

## Links

- [Plouto dashboard](https://plouto.ai)
- [Platform repo](https://github.com/PloutoAI/plouto)
- [Plugin repo](https://github.com/PloutoAI/plouto-plugin)
