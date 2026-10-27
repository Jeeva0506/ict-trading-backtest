"""
Quick test harness for IndianCommandHandler.
Runs the Telegram command polling with realistic mock data.
No Zerodha credentials needed.
Press Ctrl+C to stop.
"""
import sys
import time
import logging
from pathlib import Path
from datetime import date

sys.path.insert(0, str(Path(__file__).parent))
from live.indian_commands import IndianCommandHandler
from live.paper_trader import _load_env

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)

env = _load_env()
TOKEN  = env.get("TELEGRAM_BOT_TOKEN", "")
CHATID = env.get("TELEGRAM_CHAT_ID", "")

# ── Mock data ─────────────────────────────────────────────────────────────────
MOCK_CAPITAL   = 1_050_000.0
MOCK_INIT      = 1_000_000.0

def mock_status():
    return {
        "running_capital": MOCK_CAPITAL,
        "return_pct":      (MOCK_CAPITAL - MOCK_INIT) / MOCK_INIT * 100,
        "open_positions":  1,
        "monthly_pnl":     25_000.0,
        "month_paused":    False,
    }

def mock_positions():
    return [{
        "instrument":   "NIFTY",
        "direction":    "call",
        "quality":      "A+",
        "strike":       24800,
        "dte":          3,
        "entry_prem":   85.50,
        "current_prem": 112.30,
        "peak":         118.00,
        "lots":         4,
        "qty":          200,
        "live_pnl":     5360.0,
        "bep_active":   True,
        "trail_active": False,
    }]

def mock_capital():
    return {
        "initial":     MOCK_INIT,
        "running":     MOCK_CAPITAL,
        "net_pnl":     MOCK_CAPITAL - MOCK_INIT,
        "return_pct":  (MOCK_CAPITAL - MOCK_INIT) / MOCK_INIT * 100,
        "monthly_pnl": 25_000.0,
    }

def mock_trades():
    return {
        "trades":   3,
        "wins":     2,
        "win_rate": 66.7,
        "net_rs":   18_500.0,
        "best_rs":  14_200.0,
        "worst_rs": -4_800.0,
        "details": [
            {"instrument": "NIFTY",    "direction": "call", "net_rs":  14200, "exit_reason": "Trail SL"},
            {"instrument": "FINNIFTY", "direction": "put",  "net_rs":   9100, "exit_reason": "BEP"},
            {"instrument": "NIFTY",    "direction": "put",  "net_rs":  -4800, "exit_reason": "SL"},
        ],
    }

def mock_risk():
    return {
        "monthly_pnl":         25_000.0,
        "monthly_dd_limit_rs": -50_000.0,
        "monthly_dd_pct":      -5.0,
        "month_paused":        False,
        "vix_buy_min":         12.0,
        "capital":             MOCK_CAPITAL,
    }

def mock_orb():
    return {
        "NIFTY": {"h": 24850.0, "l": 24680.0},
        "FINNIFTY": {"h": 23620.0, "l": 23490.0},
    }

def mock_pause():
    print("[TEST] Pause callback triggered")

def mock_resume():
    print("[TEST] Resume callback triggered")

def mock_report():
    print("[TEST] Report callback triggered")

# ── Wire handler ──────────────────────────────────────────────────────────────
cmd = IndianCommandHandler(TOKEN, CHATID, label="NIFTY-PAPER")
cmd.get_status    = mock_status
cmd.get_positions = mock_positions
cmd.get_capital   = mock_capital
cmd.get_trades    = mock_trades
cmd.get_risk      = mock_risk
cmd.get_orb       = mock_orb
cmd.do_pause      = mock_pause
cmd.do_resume     = mock_resume
cmd.do_report     = mock_report

cmd.start_polling()
print(f"\nCommand handler running. Chat ID: {CHATID}")
print("Send any command from Telegram to test. Press Ctrl+C to stop.\n")

try:
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    cmd.stop_polling()
    print("\nStopped.")
