#!/usr/bin/env python3
"""Devin OAuth login for the Hermes devin provider — no Node/Bun required.

Port of devin-gateway's src/login.ts + src/cli/login.ts. Runs the PKCE flow:

  1. Open https://app.devin.ai/auth/cli/continue?... (browser sign-in)
  2. Devin redirects to a local callback server with `code` + `state`
  3. Exchange code + verifier for a session token at api.devin.ai/auth/cli/token
  4. Write DEVIN_API_KEY=<token> to ~/.hermes/.env

Usage:
  python3 login.py            # interactive — opens browser, local callback
  python3 login.py --paste    # paste the redirect URL manually (SSH/headless)
  python3 login.py --print    # print token only, don't save
  python3 login.py --status   # show current credential status
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.server
import json
import os
import secrets
import sys
import threading
import urllib.parse
import urllib.request
import uuid
import webbrowser

DEVIN_WEBAPP_URL = "https://app.devin.ai"
DEVIN_API_URL = "https://api.devin.ai"
TOKEN_PATH = "/auth/cli/token"
CALLBACK_PORT = 59653
CALLBACK_PATH = "/callback"
TIMEOUT_S = 300

HERMES_ENV = os.path.expanduser("~/.hermes/.env")
GATEWAY_TOKEN_FILE = os.path.expanduser("~/.devin-gateway/token")
DEVIN_CLI_CREDENTIALS = os.path.expanduser("~/.local/share/devin/credentials.toml")


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def start_login_flow(redirect_uri: str) -> dict:
    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    state = str(uuid.uuid4())
    params = urllib.parse.urlencode({
        "redirect_uri": redirect_uri,
        "state": state,
        "prompt": "select_account",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })
    return {
        "state": state,
        "verifier": verifier,
        "auth_url": f"{DEVIN_WEBAPP_URL}/auth/cli/continue?{params}",
    }


def exchange_token(code: str, verifier: str) -> str:
    req = urllib.request.Request(
        DEVIN_API_URL + TOKEN_PATH,
        data=json.dumps({"code": code, "code_verifier": verifier}).encode(),
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode())
    token = data.get("token")
    if not token:
        raise RuntimeError("Token exchange returned empty token")
    return token


def token_from_redirect_url(session: dict, redirect_url: str) -> str:
    query = urllib.parse.urlparse(redirect_url.strip()).query
    params = urllib.parse.parse_qs(query)
    code = (params.get("code") or [""])[0]
    state = (params.get("state") or [""])[0]
    if state != session["state"]:
        raise RuntimeError("Invalid callback: state mismatch")
    if not code:
        raise RuntimeError("No code in redirect URL")
    return exchange_token(code, session["verifier"])


# ─── Persistence ─────────────────────────────────────────────────────────────


def upsert_env_var(path: str, key: str, value: str) -> None:
    """Insert or replace `KEY=value` in a dotenv file, preserving other lines."""
    lines = []
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except FileNotFoundError:
        pass
    out, replaced = [], False
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and stripped.split("=", 1)[0].strip() == key:
            if not replaced:
                out.append(f"{key}={value}")
                replaced = True
            continue
        out.append(line)
    if not replaced:
        out.append(f"{key}={value}")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(out) + "\n")
    os.chmod(path, 0o600)


def save_token(token: str) -> str:
    """Write DEVIN_API_KEY to ~/.hermes/.env (canonical Hermes secrets file) and
    ~/.devin-gateway/token for interop with the TS gateway. Returns a description."""
    upsert_env_var(HERMES_ENV, "DEVIN_API_KEY", token)
    try:
        os.makedirs(os.path.dirname(GATEWAY_TOKEN_FILE), exist_ok=True)
        with open(GATEWAY_TOKEN_FILE, "w", encoding="utf-8") as fh:
            fh.write(token + "\n")
        os.chmod(GATEWAY_TOKEN_FILE, 0o600)
    except OSError:
        pass
    return HERMES_ENV


def current_status() -> str:
    if os.environ.get("DEVIN_API_KEY"):
        return "DEVIN_API_KEY is set in the environment."
    if os.path.exists(DEVIN_CLI_CREDENTIALS):
        return f"Devin CLI credentials found: {DEVIN_CLI_CREDENTIALS}"
    if os.path.exists(GATEWAY_TOKEN_FILE):
        return f"Token file found: {GATEWAY_TOKEN_FILE}"
    try:
        with open(HERMES_ENV, encoding="utf-8") as fh:
            for line in fh:
                if line.strip().startswith("DEVIN_API_KEY="):
                    return f"DEVIN_API_KEY found in {HERMES_ENV}"
    except FileNotFoundError:
        pass
    return "No Devin credentials found. Run this script to log in."


# ─── Local callback server ───────────────────────────────────────────────────

_HTML = (
    "<!doctype html><html><body style=\"font-family:system-ui;display:flex;"
    "align-items:center;justify-content:center;height:100vh;margin:0;"
    "background:#f9fafb\"><div style=\"text-align:center;padding:2em;"
    "background:#fff;border-radius:12px;box-shadow:0 1px 3px rgba(0,0,0,0.1)\">"
    "<h1 style=\"font-size:1.5em;margin-bottom:0.5em\">{}</h1>"
    "<p style=\"color:#666\">You can close this tab.</p></div></body></html>"
)


def run_callback_server(session: dict) -> str:
    """Serve the OAuth callback on 127.0.0.1:CALLBACK_PORT; return the token."""
    result: dict = {}
    done = threading.Event()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            url = urllib.parse.urlparse(self.path)
            if url.path != CALLBACK_PATH:
                self.send_response(404)
                self.end_headers()
                return
            params = urllib.parse.parse_qs(url.query)
            code = (params.get("code") or [""])[0]
            state = (params.get("state") or [""])[0]
            error = (params.get("error") or [""])[0]

            def send(message: str) -> None:
                body = _HTML.format(message).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            try:
                if error:
                    desc = (params.get("error_description") or [error])[0]
                    raise RuntimeError(f"Authorization failed: {desc}")
                if not code or state != session["state"]:
                    raise RuntimeError("Invalid callback: missing code or state mismatch")
                send("Login successful! You can close this tab.")
                result["token"] = exchange_token(code, session["verifier"])
            except Exception as exc:
                send(f"Login failed: {exc}")
                result["error"] = exc
            finally:
                done.set()

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", CALLBACK_PORT), Handler)
    server.timeout = 1.0
    deadline = threading.Event()
    timer = threading.Timer(TIMEOUT_S, deadline.set)
    timer.start()
    try:
        while not done.is_set() and not deadline.is_set():
            server.handle_request()
    finally:
        timer.cancel()
        server.server_close()
    if "token" in result:
        return result["token"]
    if "error" in result:
        raise result["error"]
    raise RuntimeError("Login timed out")


def main() -> int:
    ap = argparse.ArgumentParser(description="Devin OAuth login for Hermes")
    ap.add_argument("--paste", action="store_true", help="paste redirect URL manually")
    ap.add_argument("--print", dest="print_only", action="store_true", help="print token, don't save")
    ap.add_argument("--status", action="store_true", help="show credential status")
    args = ap.parse_args()

    if args.status:
        print(current_status())
        return 0

    redirect_uri = f"http://127.0.0.1:{CALLBACK_PORT}{CALLBACK_PATH}"
    session = start_login_flow(redirect_uri)

    print("\n  Devin — Login")
    print("  " + "-" * 45 + "\n")
    print("  Open this URL in your browser to sign in:\n")
    print(f"  {session['auth_url']}\n")

    if args.paste:
        print("  After signing in you'll be redirected to a URL like:")
        print(f"  {redirect_uri}?code=...&state=...")
        try:
            pasted = input("  Paste the full URL here:\n\n  > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nCancelled.")
            return 1
        if not pasted:
            print("No URL provided.")
            return 1
        token = token_from_redirect_url(session, pasted)
    else:
        try:
            webbrowser.open(session["auth_url"])
            print("  (Attempting to open your browser...)")
        except Exception:
            print("  (Could not open a browser — use --paste or open the URL manually)")
        print(f"\n  Waiting for callback on http://127.0.0.1:{CALLBACK_PORT} "
              f"(timeout {TIMEOUT_S}s, Ctrl+C to cancel)\n")
        try:
            token = run_callback_server(session)
        except KeyboardInterrupt:
            print("\nCancelled.")
            return 1

    if args.print_only:
        print(token)
        return 0
    where = save_token(token)
    print("  Login successful!\n")
    print(f"  Token saved to: {where}")
    print("  Hermes will pick it up as DEVIN_API_KEY — try:")
    print("    hermes --provider devin -m claude-opus-4-8-high\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
