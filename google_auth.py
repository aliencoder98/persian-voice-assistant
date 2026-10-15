#!/usr/bin/env python3
"""One-time Google OAuth for the owner's calendar. Prints a refresh token.

Run this on your own computer (it opens a browser), not inside Docker:

    pip install httpx
    python google_auth.py

It reads GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET from the environment or from .env.
Then put the printed token into .env as GOOGLE_REFRESH_TOKEN.
"""

import base64
import hashlib
import http.server
import os
import secrets
import sys
import threading
import urllib.parse
import webbrowser
from pathlib import Path

import httpx

SCOPE = "https://www.googleapis.com/auth/calendar.events"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"


def load_env() -> dict[str, str]:
    values = dict(os.environ)
    env_file = Path(__file__).with_name(".env")
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                values.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    return values


def main() -> int:
    env = load_env()
    client_id, client_secret = env.get("GOOGLE_CLIENT_ID"), env.get("GOOGLE_CLIENT_SECRET")
    creds_file = Path(__file__).with_name("secrets") / "credentials.json"
    if (not client_id or not client_secret) and creds_file.is_file():
        import json

        installed = json.loads(creds_file.read_text(encoding="utf-8"))["installed"]
        client_id, client_secret = installed["client_id"], installed["client_secret"]
    if not client_id or not client_secret:
        print("Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET first (see README).", file=sys.stderr)
        return 1

    state = secrets.token_urlsafe(16)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    result: dict[str, str] = {}
    done = threading.Event()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            query = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(self.path).query))
            if "code" in query and query.get("state") == state:
                result["code"] = query["code"]
                body = "Authorization received. You can close this tab and return to the terminal."
            else:
                result["error"] = query.get("error", "invalid response")
                body = "Authorization failed. Return to the terminal."
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(body.encode())
            done.set()

        def log_message(self, *args) -> None:
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    redirect_uri = f"http://127.0.0.1:{server.server_port}"
    threading.Thread(target=server.serve_forever, daemon=True).start()

    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        "prompt": "consent",  # forces a refresh token even if you authorized before
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    url = f"{AUTH_URL}?{urllib.parse.urlencode(params)}"
    print("Opening your browser to authorize calendar access...")
    print(f"If it does not open, visit:\n{url}\n")
    webbrowser.open(url)

    if not done.wait(timeout=300):
        print("Timed out waiting for authorization.", file=sys.stderr)
        return 1
    server.shutdown()
    if "code" not in result:
        print(f"Authorization failed: {result.get('error')}", file=sys.stderr)
        return 1

    resp = httpx.post(
        TOKEN_URL,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "code": result["code"],
            "code_verifier": verifier,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        },
        timeout=30,
    )
    if resp.status_code >= 400:
        print(f"Token exchange failed: {resp.status_code} {resp.text}", file=sys.stderr)
        return 1
    refresh = resp.json().get("refresh_token")
    if not refresh:
        print("No refresh token returned. Revoke the app at myaccount.google.com/permissions and retry.", file=sys.stderr)
        return 1
    print("\nSuccess. Add this line to your .env (keep it secret):\n")
    print(f"GOOGLE_REFRESH_TOKEN={refresh}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
