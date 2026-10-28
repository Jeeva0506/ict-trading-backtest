"""
Run: python get_token.py <request_token>

Get the request_token from the redirect URL after logging in at:
https://kite.zerodha.com/connect/login?api_key=<YOUR_API_KEY>&v=3

The redirect URL looks like:
https://127.0.0.1/?request_token=XXXXXXXXXX&action=login&status=success
                                  ^^^^^^^^^^
                                  copy this part only
"""
import sys
import re
from pathlib import Path
from kiteconnect import KiteConnect

ENV_FILE = Path(__file__).parent.parent / "backtest_engine" / ".env.zerodha"

env = {}
for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()

api_key    = env["ZERODHA_API_KEY"]
api_secret = env["ZERODHA_API_SECRET"]

if len(sys.argv) < 2:
    print("Usage: python get_token.py <request_token>")
    print(f"\nLogin URL:\nhttps://kite.zerodha.com/connect/login?api_key={api_key}&v=3")
    sys.exit(1)

arg = sys.argv[1].strip()

# Accept full URL or just the token
if "request_token=" in arg:
    m = re.search(r"request_token=([^&]+)", arg)
    request_token = m.group(1) if m else arg
else:
    request_token = arg

print(f"Exchanging request_token: {request_token[:8]}...")

kite = KiteConnect(api_key=api_key)
try:
    session = kite.generate_session(request_token, api_secret=api_secret)
except Exception as e:
    print(f"FAILED: {e}")
    sys.exit(1)

access_token = session["access_token"]

# Save to .env.zerodha
lines = ENV_FILE.read_text(encoding="utf-8").splitlines()
new_lines, found = [], False
for line in lines:
    if line.strip().startswith("ZERODHA_ACCESS_TOKEN="):
        new_lines.append(f"ZERODHA_ACCESS_TOKEN={access_token}")
        found = True
    else:
        new_lines.append(line)
if not found:
    new_lines.append(f"ZERODHA_ACCESS_TOKEN={access_token}")
ENV_FILE.write_text("\n".join(new_lines) + "\n", encoding="utf-8")

print(f"Access token saved: {access_token[:8]}...{access_token[-4:]}")
print("Run: python live/paper_trader.py")
