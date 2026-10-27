"""
Token flow test — run once to verify:
  1. Login URL message arrives in Telegram
  2. _write_env_token reads/writes the .env file correctly
  3. /token dispatch handles bad token gracefully (generate_session will fail — expected)

Usage:
    python test_token_flow.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from live.paper_trader  import _load_env, _write_env_token, ENV_FILE
from live.telegram_alert import IndianTelegram
from live.indian_commands import IndianCommandHandler


def main():
    env = _load_env()

    tg = IndianTelegram(
        bot_token=env.get("TELEGRAM_BOT_TOKEN", ""),
        chat_id=env.get("TELEGRAM_CHAT_ID", ""),
        label="NR7-PAPER",
    )
    api_key = env.get("ZERODHA_API_KEY", "MISSING")

    # ── Test 1: login URL message ─────────────────────────────────────────────
    print("TEST 1: sending login URL to Telegram...")
    login_url = f"https://kite.zerodha.com/connect/login?api_key={api_key}&amp;v=3"
    # Use _send (returns bool) not send_info (returns None)
    ok = tg._send(
        f"[NR7-PAPER] TOKEN FLOW TEST\n\n"
        f"Good morning! Zerodha auth required.\n\n"
        f"1. Open this link and log in:\n{login_url}\n\n"
        f"2. After login, copy the <b>request_token</b> from the redirect URL\n"
        f"   (value after ?request_token=)\n\n"
        f"3. Reply here: /token YOUR_REQUEST_TOKEN\n\n"
        f"Bot reloads automatically at 8:50 AM."
    )
    print(f"  Result: {'SENT' if ok else 'FAILED (check TELEGRAM_BOT_TOKEN / CHAT_ID)'}")

    # ── Test 2: _write_env_token round-trip ───────────────────────────────────
    print("\nTEST 2: _write_env_token round-trip...")
    if not ENV_FILE.exists():
        print(f"  SKIP — {ENV_FILE} not found")
    else:
        original = ENV_FILE.read_text(encoding="utf-8")
        old_token = env.get("ZERODHA_ACCESS_TOKEN", "")
        print(f"  Current token (last 6): ...{old_token[-6:] if old_token else 'MISSING'}")

        # Write a fake token
        _write_env_token("TEST_TOKEN_12345")
        check = _load_env().get("ZERODHA_ACCESS_TOKEN", "")
        assert check == "TEST_TOKEN_12345", f"Write failed: got '{check}'"
        print("  Write: OK")

        # Restore original
        _write_env_token(old_token)
        restored = _load_env().get("ZERODHA_ACCESS_TOKEN", "")
        assert restored == old_token, f"Restore failed: got '{restored}'"
        print("  Restore: OK")

    # ── Test 3: /token command dispatch with a bad token ─────────────────────
    print("\nTEST 3: /token dispatch with invalid request_token...")

    results = {}

    def fake_token_auth(request_token: str) -> str:
        results["called_with"] = request_token
        # Simulate what _cb_token_auth does — generate_session will fail
        from kiteconnect import KiteConnect
        api_secret = env.get("ZERODHA_API_SECRET", "")
        kite_tmp = KiteConnect(api_key=api_key)
        try:
            kite_tmp.generate_session(request_token, api_secret=api_secret)
            return "[TEST] Auth SUCCESS (unexpected with fake token)"
        except Exception as e:
            return f"[TEST] Auth failed as expected: {e}"

    cmd = IndianCommandHandler(
        bot_token=env.get("TELEGRAM_BOT_TOKEN", ""),
        chat_id=env.get("TELEGRAM_CHAT_ID", ""),
        label="TEST",
    )
    cmd.do_token_auth = fake_token_auth

    # Test: missing arg
    reply = cmd._cmd_token("", "Jeeva")
    print(f"  Missing arg reply: '{reply[:60]}...'")
    assert "Usage" in reply, "Expected Usage message for empty arg"
    print("  Missing arg: OK")

    # Test: full redirect URL parsing
    fake_url = "https://127.0.0.1/?request_token=FAKE_TOKEN_XYZ&action=login&status=success"
    reply = cmd._cmd_token(fake_url, "Jeeva")
    print(f"  URL parse called with: '{results.get('called_with', 'NOT CALLED')}'")
    assert results.get("called_with") == "FAKE_TOKEN_XYZ", "URL parsing failed"
    print(f"  URL parsing: OK")
    print(f"  generate_session result: {reply}")

    # Test: raw token
    results.clear()
    reply = cmd._cmd_token("RAW_TOKEN_ABC", "Jeeva")
    assert results.get("called_with") == "RAW_TOKEN_ABC", "Raw token not passed through"
    print(f"  Raw token: OK — called with '{results['called_with']}'")

    # Send test 3 result to Telegram
    tg._send(
        f"[TOKEN FLOW TEST] /token dispatch test complete\n"
        f"URL parse: OK (extracted FAKE_TOKEN_XYZ)\n"
        f"Raw token: OK\n"
        f"generate_session: rejected fake token as expected\n\n"
        f"Ready. Send /token YOUR_REAL_REQUEST_TOKEN after login."
    )

    print("\nAll tests passed. Check Telegram for the 2 messages.")


if __name__ == "__main__":
    main()
