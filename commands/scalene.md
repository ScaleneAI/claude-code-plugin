---
name: scalene
description: Scalene AI coding scorecard
arguments:
  - name: action
    description: "setup = configure credentials, sync = import all history, sync session = current session only, status = show dashboard link"
    required: true
---

## Instructions

The sync script is at `${CLAUDE_PLUGIN_ROOT}/bin/scalene-sync.py`. Credentials are in `$SCALENE_API_URL` and `$SCALENE_TOKEN` environment variables.

### /scalene setup

Guide the user through connecting their Scalene account:

1. Check if `$SCALENE_API_URL` and `$SCALENE_TOKEN` are already set. If both exist, tell the user they're already configured and show their dashboard URL.

2. If not configured, ask the user for their Scalene personal URL and token. They can find these at https://getscalene.com/me → Connect Claude Code.

3. Help them add the env vars to their shell profile. Run:
   ```
   echo 'export SCALENE_API_URL=<their-url>' >> ~/.zshrc
   echo 'export SCALENE_TOKEN=<their-token>' >> ~/.zshrc
   ```
   (Use `~/.bashrc` if they use bash.)

4. After setting the vars, ask if they want to sync their history now (`/scalene sync`).

### /scalene sync

Run: `python3 ${CLAUDE_PLUGIN_ROOT}/bin/scalene-sync.py --api-url "$SCALENE_API_URL" --token "$SCALENE_TOKEN"`

Imports all historical sessions from `~/.claude/projects/`. May take a few minutes for large histories.

### /scalene sync session

Run: `python3 ${CLAUDE_PLUGIN_ROOT}/bin/scalene-sync.py --api-url "$SCALENE_API_URL" --token "$SCALENE_TOKEN" --session-only $CLAUDE_SESSION_ID`

Syncs only the current session.

### /scalene status

Print the user's dashboard URL (the base domain from `$SCALENE_API_URL` + `/me`). No sync.

After any sync, tell the user their dashboard is updated.

## Privacy

Only metadata is exported — token counts, timestamps, model IDs, tool names. Never prompt text, file contents, or tool arguments.
