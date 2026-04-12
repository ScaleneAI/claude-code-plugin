# scalene-mcp

Claude Code plugin for [Scalene](https://getscalene.com) — the AI coding scorecard.

## Install

```bash
claude plugin install https://github.com/scaleneai/mcp
```

Then set your credentials (from your [dashboard](https://getscalene.com/me) → Connect):

```bash
export SCALENE_API_URL=https://getscalene.com/u/<your-token>
export SCALENE_TOKEN=<your-bearer-token>
```

That's it. Every session auto-syncs when it ends.

## How it works

The plugin registers a `SessionEnd` hook that fires once when you close a Claude Code session. It runs a Python script locally that reads the session's JSONL file and POSTs metadata to your Scalene dashboard.

## Import historical data (optional)

Want your past sessions too?

```bash
curl -sL https://raw.githubusercontent.com/scaleneai/mcp/main/sync_script.py | python3 - --api-url 'https://getscalene.com/u/<your-token>' --token '<your-bearer-token>'
```

## What gets exported

The sync script runs **entirely on your machine**. Only metadata crosses the network:

| Exported | Never exported |
|----------|---------------|
| Session ID, timestamps | Prompt text |
| Token counts (input/output/cache) | Response text |
| Model ID (e.g. `claude-opus-4-6`) | File contents |
| Tool name (e.g. `Edit`, `Bash`) | Tool inputs or outputs |
| Stop reason, content block types | Thinking blocks |
| Git branch, project path | Any conversation content |

## Privacy

The privacy whitelist is enforced on **your machine**, not the server. The script is ~300 lines of stdlib Python with zero dependencies. Audit it:

- [sync_script.py](./sync_script.py) (standalone)
- [plugin/bin/scalene-sync.py](./plugin/bin/scalene-sync.py) (bundled in plugin)

## Links

- [Scalene Dashboard](https://getscalene.com)
- [Platform repo](https://github.com/mtrbls/scalene)
- [Scoring methodology](https://github.com/mtrbls/scalene/blob/main/SCORING.md)
