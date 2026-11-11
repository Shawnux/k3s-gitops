"""One-time TIDAL login (authorization code + PKCE). Standard library only, so it
runs on any machine with Python 3 and a browser."""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import secrets
import urllib.error
import urllib.parse
import urllib.request

AUTHORIZE_URL = "https://login.tidal.com/authorize"
TOKEN_URL = "https://auth.tidal.com/v1/oauth2/token"
SCOPES = "collection.read playlists.read playlists.write"
REDIRECT = "http://localhost:8765/tidal"


def _pkce():
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def authorize_url(client_id, redirect, scopes, challenge, state, base=AUTHORIZE_URL) -> str:
    query = urllib.parse.urlencode({
        "response_type": "code", "client_id": client_id, "redirect_uri": redirect,
        "scope": scopes, "code_challenge_method": "S256", "code_challenge": challenge, "state": state,
    })
    return f"{base}?{query}"


def _wait_for_callback(redirect: str) -> str:
    parts = urllib.parse.urlsplit(redirect)
    got = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if urllib.parse.urlsplit(self.path).path != parts.path:
                self.send_response(404)
                self.end_headers()
                return
            got["url"] = self.path
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Logged in. You can close this tab and go back to the terminal.\n")

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer((parts.hostname or "localhost", parts.port or 80), Handler)
    try:
        while "url" not in got:
            server.handle_request()
    finally:
        server.server_close()
    return got["url"]


def exchange(client_id, client_secret, code, verifier, redirect, token_url=TOKEN_URL) -> dict:
    data = urllib.parse.urlencode({
        "grant_type": "authorization_code", "client_id": client_id, "code": code,
        "redirect_uri": redirect, "code_verifier": verifier,
    }).encode()
    req = urllib.request.Request(token_url, data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    if client_secret:
        basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        req.add_header("Authorization", f"Basic {basic}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"TIDAL refused the login: {exc.code} {exc.read().decode(errors='replace')[:300]}")


def run(client_id, client_secret="", redirect=REDIRECT, scopes=SCOPES, paste=False,
        authorize_base=AUTHORIZE_URL, token_url=TOKEN_URL) -> dict:
    if not client_id:
        raise SystemExit("set TIDAL_CLIENT_ID or pass --client-id")
    verifier, challenge = _pkce()
    state = secrets.token_urlsafe(16)
    print("\nOpen this link, log in to TIDAL and approve:\n")
    print("  " + authorize_url(client_id, redirect, scopes, challenge, state, authorize_base) + "\n")
    if paste:
        print("The browser will end on a localhost page that may fail to load. That is fine:")
        callback = input("paste the full address from the address bar here: ").strip()
    else:
        print(f"Waiting for TIDAL to send the browser back to {redirect} ...")
        callback = _wait_for_callback(redirect)
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(callback).query)
    if "error" in query:
        raise SystemExit(f"TIDAL returned an error: {query['error'][0]} {query.get('error_description', [''])[0]}")
    if query.get("state", [""])[0] != state:
        raise SystemExit("state mismatch: start the login again")
    if "code" not in query:
        raise SystemExit("no code in the address: start the login again")
    tokens = exchange(client_id, client_secret, query["code"][0], verifier, redirect, token_url)
    if not tokens.get("refresh_token"):
        raise SystemExit("TIDAL returned no refresh token")
    granted = set((tokens.get("scope") or "").split())
    missing = [s for s in scopes.split() if granted and s not in granted]
    if missing:
        print(f"\nWARNING: TIDAL did not grant {missing}. Enable those scopes on the app at "
              "developer.tidal.com and log in again.")
    return tokens


def main(argv=None) -> int:
    import argparse
    import os

    parser = argparse.ArgumentParser(description="One-time TIDAL login; prints a refresh token.")
    parser.add_argument("--client-id", default=os.environ.get("TIDAL_CLIENT_ID", ""))
    parser.add_argument("--client-secret", default=os.environ.get("TIDAL_CLIENT_SECRET", ""))
    parser.add_argument("--redirect", default=REDIRECT)
    parser.add_argument("--paste", action="store_true",
                        help="paste the callback address instead of listening on localhost")
    args = parser.parse_args(argv)
    tokens = run(args.client_id, args.client_secret, redirect=args.redirect, paste=args.paste)
    print("\nLogged in. Refresh token:\n\n  " + tokens["refresh_token"] + "\n")
    print("Store it as TIDAL_REFRESH_TOKEN in the tidal-discography secret (see README.md).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
