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

CRITICAL: You MUST follow these exact steps. Do NOT ask the user to paste credentials. Do NOT show menus. Do NOT improvise. Execute these bash commands in order:

Step 1 — check if already configured:
```bash
echo "URL=${SCALENE_API_URL:-}" && echo "TOKEN=${SCALENE_TOKEN:-}"
```
If both are set (not empty), say "Already configured." and stop.

Step 2 — run this SINGLE command that does everything (auth, open browser, poll, save credentials):
```bash
python3 -c "
import json, os, subprocess, sys, time, urllib.request, urllib.error
# Start auth
req = urllib.request.Request('https://getscalene.com/api/cli/auth', method='POST', data=b'', headers={'Content-Type':'application/json'})
resp = json.loads(urllib.request.urlopen(req, timeout=10).read())
code, url = resp['code'], resp['url']
print(f'Opening browser... confirm code: {code}')
subprocess.run(['open', url])
# Poll
for i in range(60):
    time.sleep(2)
    r = json.loads(urllib.request.urlopen(f'https://getscalene.com/api/cli/poll?code={code}', timeout=10).read())
    if r['status'] == 'confirmed':
        api_url, token = r['api_url'], r['token']
        # Save to zshrc
        with open(os.path.expanduser('~/.zshrc'), 'a') as f:
            f.write(f\"\nexport SCALENE_API_URL={api_url}\nexport SCALENE_TOKEN={token}\n\")
        os.environ['SCALENE_API_URL'] = api_url
        os.environ['SCALENE_TOKEN'] = token
        print(f'Connected! Credentials saved to ~/.zshrc')
        print(f'API URL: {api_url}')
        sys.exit(0)
    if i % 5 == 0 and i > 0: print('Waiting for browser confirmation...')
print('Timed out. Try again.'); sys.exit(1)
"
```

After the command succeeds, export the vars in the current shell:
```bash
source ~/.zshrc
```

Step 3 — say "Connected!" then sync:
```bash
python3 ${CLAUDE_PLUGIN_ROOT}/bin/scalene-sync.py --api-url "$SCALENE_API_URL" --token "$SCALENE_TOKEN"
```

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
