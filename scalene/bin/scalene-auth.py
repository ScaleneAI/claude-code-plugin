#!/usr/bin/env python3
"""Scalene setup — authenticate, save credentials, sync history. One script does everything."""

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

API = "https://api.plouto.ai"


def _get_credentials():
    """Return (api_url, token) — from env, from zshrc, or via device auth."""
    # Already in env?
    api_url = os.environ.get("SCALENE_API_URL", "")
    token = os.environ.get("SCALENE_TOKEN", "")
    if api_url and token:
        return api_url, token

    # Check zshrc directly (env might not be sourced in this shell)
    zshrc = Path.home() / ".zshrc"
    if zshrc.exists():
        for line in zshrc.read_text().splitlines():
            if line.startswith("export SCALENE_API_URL="):
                api_url = line.split("=", 1)[1].strip()
            if line.startswith("export SCALENE_TOKEN="):
                token = line.split("=", 1)[1].strip()
    if api_url and token:
        return api_url, token

    # Device auth flow
    req = urllib.request.Request(
        f"{API}/api/cli/auth",
        method="POST",
        data=b"",
        headers={"Content-Type": "application/json"},
    )
    resp = json.loads(urllib.request.urlopen(req, timeout=10).read())
    code, url = resp["code"], resp["url"]

    print(f"Opening browser... confirm code: {code}")
    subprocess.run(["open", url], check=False)

    for i in range(30):
        time.sleep(2)
        r = json.loads(
            urllib.request.urlopen(
                f"{API}/api/cli/poll?code={code}", timeout=10
            ).read()
        )
        if r["status"] == "confirmed":
            api_url, token = r["api_url"], r["token"]
            with open(os.path.expanduser("~/.zshrc"), "a") as f:
                f.write(f"\nexport SCALENE_API_URL={api_url}\n")
                f.write(f"export SCALENE_TOKEN={token}\n")
            print(f"Connected! Credentials saved to ~/.zshrc")
            return api_url, token
        if i % 5 == 0 and i > 0:
            print("Waiting for browser confirmation...")

    print("Timed out. Try again.")
    sys.exit(1)


def main():
    api_url, token = _get_credentials()
    print(f"Scalene: {api_url}")

    # Run sync
    sync_script = Path(__file__).resolve().parent / "scalene-sync.py"
    if sync_script.exists():
        print("Syncing history...")
        subprocess.run(
            [sys.executable, str(sync_script), "--api-url", api_url, "--token", token],
            check=False,
        )
    else:
        print(f"Sync script not found at {sync_script}")


if __name__ == "__main__":
    main()
