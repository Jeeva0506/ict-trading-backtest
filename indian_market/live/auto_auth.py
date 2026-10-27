"""
Automated Zerodha access token refresh.
Runs at 8:50 AM IST on each trading day (called from paper_trader.py).

Requires in backtest_engine/.env.zerodha:
  ZERODHA_USER_ID       — Zerodha login ID (e.g. AB1234)
  ZERODHA_PASSWORD      — Zerodha password
  ZERODHA_TOTP_SECRET   — base32 secret from authenticator setup (NOT the 6-digit code)
  ZERODHA_API_KEY       — Kite Connect API key
  ZERODHA_API_SECRET    — Kite Connect API secret

Can also be run as a standalone script:
  python live/auto_auth.py
"""
import logging
import re
import sys
from pathlib import Path

import requests
import pyotp

logger = logging.getLogger(__name__)

ENV_FILE = Path(__file__).resolve().parents[2] / "backtest_engine" / ".env.zerodha"

_LOGIN_URL   = "https://kite.zerodha.com/api/login"
_TOTP_URL    = "https://kite.zerodha.com/api/twofa"
_CONNECT_URL = "https://kite.zerodha.com/connect/login"
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer":    "https://kite.zerodha.com/",
}


# ── Env helpers ───────────────────────────────────────────────────────────────
def _read_env() -> dict:
    env = {}
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def _write_token(token: str):
    text = ENV_FILE.read_text(encoding="utf-8")
    lines = text.splitlines()
    new_lines, found = [], False
    for line in lines:
        if line.strip().startswith("ZERODHA_ACCESS_TOKEN="):
            new_lines.append(f"ZERODHA_ACCESS_TOKEN={token}")
            found = True
        else:
            new_lines.append(line)
    if not found:
        new_lines.append(f"ZERODHA_ACCESS_TOKEN={token}")
    ENV_FILE.write_text("\n".join(new_lines) + "\n", encoding="utf-8")


# ── Core auth flow ────────────────────────────────────────────────────────────
def refresh_token() -> str:
    """
    Automate Zerodha Kite Connect login and return a fresh access_token.
    Also saves the token to .env.zerodha.
    """
    from kiteconnect import KiteConnect

    env = _read_env()

    required = ["ZERODHA_API_KEY", "ZERODHA_API_SECRET",
                "ZERODHA_USER_ID", "ZERODHA_PASSWORD", "ZERODHA_TOTP_SECRET"]
    missing = [k for k in required if not env.get(k)]
    if missing:
        raise ValueError(f"Missing in .env.zerodha: {', '.join(missing)}")

    api_key     = env["ZERODHA_API_KEY"]
    api_secret  = env["ZERODHA_API_SECRET"]
    user_id     = env["ZERODHA_USER_ID"]
    password    = env["ZERODHA_PASSWORD"]
    totp_secret = env["ZERODHA_TOTP_SECRET"].replace(" ", "").upper()

    sess = requests.Session()
    sess.headers.update(_HEADERS)

    # ── Step 1: Password login ────────────────────────────────────────────────
    logger.info("Zerodha auto-auth: step 1 — password login")
    r = sess.post(_LOGIN_URL, data={"user_id": user_id, "password": password}, timeout=15)
    r.raise_for_status()
    body = r.json()
    if body.get("status") != "success":
        raise RuntimeError(f"Login failed: {body.get('message', body)}")
    request_id = body["data"]["request_id"]

    # ── Step 2: TOTP ──────────────────────────────────────────────────────────
    logger.info("Zerodha auto-auth: step 2 — TOTP")
    totp_code = pyotp.TOTP(totp_secret).now()
    r = sess.post(_TOTP_URL, data={
        "user_id":     user_id,
        "request_id":  request_id,
        "twofa_value": totp_code,
        "twofa_type":  "totp",
    }, timeout=15)
    r.raise_for_status()
    body = r.json()
    if body.get("status") != "success":
        raise RuntimeError(f"TOTP failed: {body.get('message', body)}")

    # ── Step 3: Get request_token from Kite Connect redirect ──────────────────
    logger.info("Zerodha auto-auth: step 3 — get request_token")
    r = sess.get(
        _CONNECT_URL,
        params={"v": "3", "api_key": api_key},
        allow_redirects=False,
        timeout=15,
    )
    location = r.headers.get("Location", "")
    m = re.search(r"request_token=([^&]+)", location)
    if not m:
        raise RuntimeError(
            f"No request_token in redirect URL: {location or '(empty)'}"
        )
    request_token = m.group(1)

    # ── Step 4: Exchange for access token ─────────────────────────────────────
    logger.info("Zerodha auto-auth: step 4 — generate access token")
    kite = KiteConnect(api_key=api_key)
    session_data = kite.generate_session(request_token, api_secret=api_secret)
    access_token = session_data["access_token"]

    _write_token(access_token)
    logger.info(f"Zerodha token saved: {access_token[:8]}...{access_token[-4:]}")
    return access_token


# ── Standalone runner ─────────────────────────────────────────────────────────
if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    try:
        token = refresh_token()
        print(f"\nSuccess. Token: {token[:8]}...{token[-4:]}")
        print("paper_trader.py can now connect to Zerodha.")
    except Exception as e:
        print(f"\nFailed: {e}")
        sys.exit(1)
