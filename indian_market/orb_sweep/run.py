"""
ORB Sweep Trading System — Entry Point
=======================================
Usage:
  python -m orb_sweep.run live          Run live/paper trader
  python -m orb_sweep.run backtest      Run backtest on historical data
  python -m orb_sweep.run walkforward   Run walk-forward test
  python -m orb_sweep.run metrics       Print performance metrics from trade DB
"""

import argparse
import logging
import sys
from datetime import date
from pathlib import Path

from orb_sweep.config import TradingConfig, PROJECT_ROOT

logger = logging.getLogger("orb_sweep")


def cmd_live(args):
    from orb_sweep.trader import ORBSweepTrader
    cfg = TradingConfig(paper_mode=not args.live)
    trader = ORBSweepTrader(cfg)
    trader.run()


def cmd_backtest(args):
    from orb_sweep.backtest import ORBSweepBacktester

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")

    data_dir = PROJECT_ROOT / "data"
    nifty_path = str(data_dir / (args.data or "zerodha_nifty_1m.csv"))
    vix_path = str(data_dir / (args.vix or "zerodha_vix_day.csv"))

    cfg = TradingConfig()
    if args.atr:
        cfg.min_atr_5m = args.atr
    if args.range_pct:
        cfg.min_range_pct = args.range_pct
    if args.vol_mult:
        cfg.volume_multiplier = args.vol_mult

    bt = ORBSweepBacktester(cfg)
    bt.load_data(nifty_path, vix_path)

    start = date.fromisoformat(args.start) if args.start else None
    end = date.fromisoformat(args.end) if args.end else None
    results = bt.run(start_date=start, end_date=end)
    bt.print_report(results)

    if args.csv:
        df = bt.trades_to_df()
        out_path = PROJECT_ROOT / "reports" / "orb_sweep_backtest.csv"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(out_path, index=False)
        print(f"Trades exported to {out_path}")


def cmd_walkforward(args):
    from orb_sweep.walk_forward import WalkForwardTester

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")

    data_dir = PROJECT_ROOT / "data"
    nifty_path = str(data_dir / (args.data or "zerodha_nifty_1m.csv"))
    vix_path = str(data_dir / (args.vix or "zerodha_vix_day.csv"))

    wf = WalkForwardTester(
        nifty_path, vix_path,
        is_months=args.is_months,
        oos_months=args.oos_months,
    )
    results = wf.run()
    wf.print_report(results)


def cmd_metrics(args):
    from orb_sweep.trade_logger import TradeLogger

    cfg = TradingConfig()
    tl = TradeLogger(cfg)
    tl.print_report()
    tl.close()


def main():
    parser = argparse.ArgumentParser(
        description="ORB Sweep — NIFTY Weekly Options Trading System"
    )
    sub = parser.add_subparsers(dest="command", help="Command to run")

    # ── live ───────────────────────────────────────────────────────────
    p_live = sub.add_parser("live", help="Run live/paper trader")
    p_live.add_argument("--live", action="store_true",
                        help="Run in LIVE mode (default: paper)")
    p_live.set_defaults(func=cmd_live)

    # ── backtest ───────────────────────────────────────────────────────
    p_bt = sub.add_parser("backtest", help="Run backtest")
    p_bt.add_argument("--data", type=str, help="1m CSV filename")
    p_bt.add_argument("--vix", type=str, help="VIX daily CSV filename")
    p_bt.add_argument("--start", type=str, help="Start date (YYYY-MM-DD)")
    p_bt.add_argument("--end", type=str, help="End date (YYYY-MM-DD)")
    p_bt.add_argument("--atr", type=float, help="Override min ATR threshold")
    p_bt.add_argument("--range-pct", type=float, help="Override min range pct")
    p_bt.add_argument("--vol-mult", type=float, help="Override volume multiplier")
    p_bt.add_argument("--csv", action="store_true", help="Export trades to CSV")
    p_bt.set_defaults(func=cmd_backtest)

    # ── walkforward ────────────────────────────────────────────────────
    p_wf = sub.add_parser("walkforward", help="Run walk-forward test")
    p_wf.add_argument("--data", type=str, help="1m CSV filename")
    p_wf.add_argument("--vix", type=str, help="VIX daily CSV filename")
    p_wf.add_argument("--is-months", type=int, default=12,
                      help="In-sample window months (default: 12)")
    p_wf.add_argument("--oos-months", type=int, default=3,
                      help="Out-of-sample window months (default: 3)")
    p_wf.set_defaults(func=cmd_walkforward)

    # ── metrics ────────────────────────────────────────────────────────
    p_m = sub.add_parser("metrics", help="Print performance metrics")
    p_m.set_defaults(func=cmd_metrics)

    args = parser.parse_args()
    if args.command is None:
        parser.print_help()
        sys.exit(1)
    args.func(args)


if __name__ == "__main__":
    main()
