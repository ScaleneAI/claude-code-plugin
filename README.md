# scalene-mcp

MCP integration for [Scalene](https://getscalene.com) — the AI coding assistant scorecard.

Connects Claude Code to your Scalene dashboard. One command to install, one sentence to sync.

## Install

```bash
claude mcp add --transport http --scope user scalene https://getscalene.com/u/<your-token>/mcp
```

Your personal token is on your [dashboard](https://getscalene.com/me) — click **Connect Claude Code**.

## Sync your history

In any Claude Code session:

```
"sync my Claude Code history to Scalene"
```

The agent saves a Python script to `/tmp/scalene_sync.py` and runs it. Your dashboard updates within seconds.

## What gets exported

The sync script runs **entirely on your machine**. Only metadata crosses the network:

| Exported | Never exported |
|----------|---------------|
| Session ID, timestamps | Prompt text |
| Token counts (input/output/cache) | Response text |
| Model ID (e.g. `claude-opus-4-6`) | File contents |
| Tool name (e.g. `Edit`, `Bash`) | Tool inputs or outputs |
| Git branch, project path | Thinking blocks |
| Git user.email (for attribution) | Any conversation content |

## Ongoing sync

After the initial import, add a Stop hook so every session auto-syncs when it ends:

```json
{
  "hooks": {
    "Stop": [
      {
        "command": "python3 /tmp/scalene_sync.py --api-url https://getscalene.com --token YOUR_TOKEN --session-only $CLAUDE_SESSION_ID"
      }
    ]
  }
}
```

Save this to `~/.claude/hooks.json` (or your project's `.claude/hooks.json`).

## Privacy

The privacy whitelist is enforced on **your machine**, not the server. The script is ~250 lines of stdlib Python with zero dependencies. Audit it yourself:

- [sync_script.py](./sync_script.py)

## Links

- [Scalene Dashboard](https://getscalene.com)
- [Platform repo](https://github.com/mtrbls/scalene)
- [Scoring methodology](https://github.com/mtrbls/scalene/blob/main/SCORING.md)
