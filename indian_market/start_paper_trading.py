"""
Start ORB Sweep Paper Trading System.

Run this script each morning after authenticating with Zerodha.
It will:
  1. Connect to Zerodha Kite
  2. Wait for market open (9:15 IST)
  3. Execute the combined strategy:
     - Expiry days (Tuesday): Short ATM straddle at 9:31, collect theta
     - Non-expiry days: Scan for PUT sweep signals after ORB
  4. Manage positions with SL/TP/trail exits
  5. Force exit at 15:15
  6. Log everything to Excel journal + SQLite + Telegram
  7. Run safety checks every tick (kill switch, reconciliation, etc.)

Usage:
    python start_paper_trading.py

Capital: Rs 3,00,000 | Mode: PAPER
"""
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

from orb_sweep.trader import ORBSweepTrader
from orb_sweep.config import TradingConfig


def main():
    cfg = TradingConfig()

    print("=" * 60)
    print("  ORB SWEEP PAPER TRADING SYSTEM")
    print("=" * 60)
    print(f"  Capital:     Rs {cfg.capital:,}")
    print(f"  Mode:        {'PAPER' if cfg.paper_mode else 'LIVE'}")
    print(f"  Lot size:    {cfg.lot_size}")
    print(f"  Straddle:    {cfg.straddle_risk_pct*100:.0f}% risk (expiry days)")
    print(f"  Sweep:       {cfg.risk_per_trade_pct*100:.0f}% risk (non-expiry)")
    print(f"  Kill switch: Rs {cfg.daily_dd_limit:,.0f} daily")
    print(f"  VIX halt:    > {cfg.vix_halt_threshold}")
    print("=" * 60)

    trader = ORBSweepTrader(cfg)
    trader.run()


if __name__ == "__main__":
    main()
