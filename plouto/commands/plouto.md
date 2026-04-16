---
name: plouto
description: AI Engineering Intelligence
arguments:
  - name: action
    description: "setup = authenticate and sync, sync = import all history, sync session = current session only, status = show dashboard link"
    required: true
---

## Instructions

The sync script is at `${CLAUDE_PLUGIN_ROOT}/bin/scalene-sync.py`. Credentials are in `$SCALENE_API_URL` and `$SCALENE_TOKEN` environment variables.

### /plouto setup

Run this ONE command. It opens the browser for OAuth login, saves the token, and syncs history:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/bin/scalene-auth.py
```

### /plouto setup auth

Force re-authentication (clears existing credentials first):

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/bin/scalene-auth.py --force
```

### /plouto sync

Run: `python3 ${CLAUDE_PLUGIN_ROOT}/bin/scalene-sync.py --bulk --api-url "$SCALENE_API_URL" --token "$SCALENE_TOKEN"`

Collects all data locally, uploads in one request.

### /plouto sync session

Run: `python3 ${CLAUDE_PLUGIN_ROOT}/bin/scalene-sync.py --api-url "$SCALENE_API_URL" --token "$SCALENE_TOKEN" --session-only $CLAUDE_SESSION_ID`

Syncs only the current session.

### /plouto status

Print the user's dashboard URL (the base domain from `$SCALENE_API_URL` + `/me`). No sync.

After any sync, tell the user their dashboard is updated.

## Privacy

Only metadata is exported: token counts, timestamps, model IDs, tool names. Never prompt text, file contents, or tool arguments.
