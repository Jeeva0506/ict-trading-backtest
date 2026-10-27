"""
Zerodha Kite Connect — daily authentication helper.

Run this ONCE each morning before fetching data:
    python data/zerodha_auth.py

Flow:
  1. Reads ZERODHA_API_KEY + ZERODHA_API_SECRET from envzerodha
  2. Prints login URL → you open it in browser, log in
  3. Paste the request_token from the redirect URL
  4. Exchanges for access_token and saves it back to envzerodha
"""
import os, re, sys
from pathlib import Path

ENV_FILE = Path(__file__).resolve().parents[2] / "backtest_engine" / ".env.zerodha"


def _read_env() -> dict:
    env = {}
    if not ENV_FILE.exists():
        print(f"ERROR: envzerodha not found at {ENV_FILE}")
        sys.exit(1)
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def _write_env(env: dict):
    lines = ENV_FILE.read_text().splitlines()
    new_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("ZERODHA_ACCESS_TOKEN="):
            new_lines.append(f"ZERODHA_ACCESS_TOKEN={env.get('ZERODHA_ACCESS_TOKEN', '')}")
        else:
            new_lines.append(line)
    ENV_FILE.write_text("\n".join(new_lines) + "\n")


def authenticate():
    from kiteconnect import KiteConnect

    env = _read_env()
    api_key = env.get("ZERODHA_API_KEY", "")
    api_secret = env.get("ZERODHA_API_SECRET", "")

    if not api_key or api_key == "your_api_key_here":
        print("ERROR: Fill in ZERODHA_API_KEY in envzerodha first.")
        sys.exit(1)
    if not api_secret or api_secret == "your_api_secret_here":
        print("ERROR: Fill in ZERODHA_API_SECRET in envzerodha first.")
        sys.exit(1)

    kite = KiteConnect(api_key=api_key)
    login_url = kite.login_url()

    print("\n" + "="*70)
    print("  ZERODHA KITE CONNECT — Daily Authentication")
    print("="*70)
    print(f"\n  1. Open this URL in your browser:")
    print(f"\n     {login_url}\n")
    print("  2. Log in with your Zerodha credentials + 2FA")
    print("  3. After login you'll be redirected to a URL like:")
    print("       https://127.0.0.1/?request_token=XXXXX&action=login&status=success")
    print("  4. Copy the request_token value from that URL\n")

    request_token = input("  Paste request_token here: ").strip()
    if not request_token:
        print("ERROR: No request_token provided.")
        sys.exit(1)

    # Remove URL-encoded garbage if user pasted the full URL
    if "request_token=" in request_token:
        m = re.search(r"request_token=([^&]+)", request_token)
        if m:
            request_token = m.group(1)

    try:
        session = kite.generate_session(request_token, api_secret=api_secret)
        access_token = session["access_token"]
    except Exception as e:
        print(f"ERROR: Failed to generate session: {e}")
        sys.exit(1)

    kite.set_access_token(access_token)

    # Save to envzerodha
    env["ZERODHA_ACCESS_TOKEN"] = access_token
    _write_env(env)

    print(f"\n  Access token saved to envzerodha.")
    print(f"  Token: {access_token[:8]}...{access_token[-4:]}")
    print("\n  You can now run zerodha_data.py to download historical data.")
    return kite


if __name__ == "__main__":
    authenticate()
