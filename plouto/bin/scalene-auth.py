#!/usr/bin/env python3
"""Plouto setup — authenticate via OAuth, save credentials, sync history.

Flow:
  1. Start a tiny HTTP server on a random localhost port
  2. Open browser to https://api.plouto.ai/cli/login?port={port}&state={state}
  3. User logs in (or is already logged in)
  4. Server mints an API token and redirects to http://localhost:{port}/callback?token=...
  5. Local server catches the redirect, verifies state, saves token
  6. Runs initial sync

Same pattern as `gh auth login`, `gcloud auth login`, `fly auth login`.
Zero dependencies. Runs on the developer's machine.
"""

import json
import os
import socket
import subprocess
import sys
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

API = "https://api.plouto.ai"
TIMEOUT = 120  # seconds to wait for browser callback


def _get_existing_credentials():
    """Return (api_url, token) from env or ~/.zshrc, or (None, None)."""
    api_url = os.environ.get("SCALENE_API_URL", "")
    token = os.environ.get("SCALENE_TOKEN", "")
    if api_url and token:
        return api_url, token

    zshrc = Path.home() / ".zshrc"
    if zshrc.exists():
        for line in zshrc.read_text().splitlines():
            if line.startswith("export SCALENE_API_URL="):
                api_url = line.split("=", 1)[1].strip()
            if line.startswith("export SCALENE_TOKEN="):
                token = line.split("=", 1)[1].strip()
    if api_url and token:
        return api_url, token

    return None, None


def _find_free_port() -> int:
    """Find a random available port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _save_credentials(api_url: str, token: str) -> None:
    """Append credentials to ~/.zshrc."""
    zshrc = Path.home() / ".zshrc"
    # Remove old entries first.
    if zshrc.exists():
        lines = zshrc.read_text().splitlines()
        lines = [l for l in lines if not l.startswith("export SCALENE_API_URL=") and not l.startswith("export SCALENE_TOKEN=")]
        zshrc.write_text("\n".join(lines) + "\n")
    with open(zshrc, "a") as f:
        f.write(f"export SCALENE_API_URL={api_url}\n")
        f.write(f"export SCALENE_TOKEN={token}\n")


def _oauth_login() -> tuple[str, str]:
    """Run the OAuth localhost-redirect flow. Returns (api_url, token)."""
    port = _find_free_port()
    state = os.urandom(16).hex()
    result = {"api_url": None, "token": None, "error": None}
    got_callback = threading.Event()

    class CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path != "/callback":
                self.send_response(404)
                self.end_headers()
                return

            params = parse_qs(parsed.query)
            cb_state = params.get("state", [""])[0]
            cb_token = params.get("token", [""])[0]
            cb_api_url = params.get("api_url", [""])[0]

            if cb_state != state:
                result["error"] = "State mismatch (possible CSRF). Try again."
                self.send_response(400)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(b"<html><body><h2>Authentication failed.</h2><p>State mismatch. Please try again.</p></body></html>")
                got_callback.set()
                return

            if not cb_token:
                result["error"] = "No token received."
                self.send_response(400)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(b"<html><body><h2>Authentication failed.</h2><p>No token received.</p></body></html>")
                got_callback.set()
                return

            result["api_url"] = cb_api_url or API
            result["token"] = cb_token

            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(
                b"<html><body style='font-family:system-ui;display:flex;align-items:center;"
                b"justify-content:center;height:100vh;margin:0;background:#fcfaf7'>"
                b"<div style='text-align:center'>"
                b"<h2 style='color:#18181b'>Connected to Plouto</h2>"
                b"<p style='color:#71717a'>You can close this tab.</p>"
                b"</div></body></html>"
            )
            got_callback.set()

        def log_message(self, format, *args):
            pass  # suppress HTTP logs

    server = HTTPServer(("127.0.0.1", port), CallbackHandler)
    server.timeout = TIMEOUT

    # Open browser.
    login_url = f"{API}/cli/login?port={port}&state={state}"
    print(f"Opening browser to authenticate...", flush=True)
    subprocess.run(["open", login_url], check=False)

    # Wait for the callback.
    thread = threading.Thread(target=lambda: server.handle_request(), daemon=True)
    thread.start()
    got_callback.wait(timeout=TIMEOUT)
    server.server_close()

    if result["error"]:
        print(f"Error: {result['error']}", file=sys.stderr, flush=True)
        sys.exit(1)

    if not result["token"]:
        print("Timed out waiting for browser authentication.", file=sys.stderr, flush=True)
        sys.exit(1)

    return result["api_url"], result["token"]


def main():
    force = "--force" in sys.argv

    # Check for existing credentials (skip if --force).
    api_url, token = (None, None) if force else _get_existing_credentials()
    if api_url and token:
        print(f"Already connected: {api_url}", flush=True)
    else:
        # OAuth flow.
        api_url, token = _oauth_login()
        _save_credentials(api_url, token)
        print(f"Connected! Credentials saved to ~/.zshrc", flush=True)

    print(f"API: {api_url}", flush=True)

    # Run sync.
    sync_script = Path(__file__).resolve().parent / "scalene-sync.py"
    if sync_script.exists():
        print("Syncing history...", flush=True)
        subprocess.run(
            [sys.executable, str(sync_script), "--api-url", api_url, "--token", token],
            check=False,
        )
    else:
        print(f"Sync script not found at {sync_script}", flush=True)


if __name__ == "__main__":
    main()
