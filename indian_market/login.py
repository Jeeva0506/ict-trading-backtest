"""
Daily Zerodha login — run this each morning before 9:15.

Step 1: Opens login URL in your browser
Step 2: You log in with Zerodha credentials + TOTP
Step 3: Starts a tiny local server to catch the redirect
Step 4: Exchanges request_token for access_token automatically
Step 5: Saves token to .env.zerodha — ready to trade

Usage:
    python login.py
"""
import re
import sys
import threading
import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from kiteconnect import KiteConnect

ENV_FILE = Path(__file__).resolve().parent.parent / "backtest_engine" / ".env.zerodha"


def _read_env():
    env = {}
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def _save_token(token):
    lines = ENV_FILE.read_text().splitlines()
    new = []
    for line in lines:
        if line.strip().startswith("ZERODHA_ACCESS_TOKEN="):
            new.append(f"ZERODHA_ACCESS_TOKEN={token}")
        else:
            new.append(line)
    ENV_FILE.write_text("\n".join(new) + "\n")


class _CallbackHandler(BaseHTTPRequestHandler):
    token = None

    def do_GET(self):
        qs = parse_qs(urlparse(self.path).query)
        req_token = qs.get("request_token", [None])[0]
        status = qs.get("status", [None])[0]

        if req_token and status == "success":
            _CallbackHandler.token = req_token
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(
                b"<html><body><h2>Login successful!</h2>"
                b"<p>You can close this tab. Token captured.</p>"
                b"</body></html>"
            )
        else:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"Login failed or cancelled.")

    def log_message(self, *args):
        pass


def main():
    env = _read_env()
    api_key = env.get("ZERODHA_API_KEY", "")
    api_secret = env.get("ZERODHA_API_SECRET", "")

    if not api_key or not api_secret:
        print("ERROR: Set ZERODHA_API_KEY and ZERODHA_API_SECRET in .env.zerodha")
        sys.exit(1)

    kite = KiteConnect(api_key=api_key)
    login_url = kite.login_url()

    print("=" * 60)
    print("  ZERODHA DAILY LOGIN")
    print("=" * 60)
    print()
    print("  Opening browser for Zerodha login...")
    print("  Log in with your credentials + TOTP.")
    print("  Token will be captured automatically.")
    print()

    server = HTTPServer(("127.0.0.1", 8000), _CallbackHandler)
    server_thread = threading.Thread(target=server.handle_request, daemon=True)
    server_thread.start()

    webbrowser.open(login_url)

    print("  Waiting for login callback...")
    server_thread.join(timeout=120)
    server.server_close()

    request_token = _CallbackHandler.token
    if not request_token:
        print()
        print("  No callback received. Paste the redirect URL or request_token:")
        raw = input("  > ").strip()
        if "request_token=" in raw:
            m = re.search(r"request_token=([^&]+)", raw)
            if m:
                request_token = m.group(1)
        else:
            request_token = raw

    if not request_token:
        print("  ERROR: No request_token. Try again.")
        sys.exit(1)

    try:
        session = kite.generate_session(request_token, api_secret=api_secret)
        access_token = session["access_token"]
    except Exception as e:
        print(f"  ERROR: {e}")
        sys.exit(1)

    _save_token(access_token)

    print()
    print(f"  Access token saved: {access_token[:8]}...{access_token[-4:]}")
    print(f"  Saved to: {ENV_FILE}")
    print()
    print("  Ready to trade! Run: python start_paper_trading.py")
    print("=" * 60)


if __name__ == "__main__":
    main()
