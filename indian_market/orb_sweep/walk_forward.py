"""Walk-forward testing for ORB Sweep strategy."""

import logging
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, timedelta
from itertools import product
from typing import Dict, List, Tuple

import numpy as np
import pandas as pd

from orb_sweep.config import TradingConfig
from orb_sweep.backtest import ORBSweepBacktester

logger = logging.getLogger(__name__)


@dataclass
class WFWindow:
    is_start: date
    is_end: date
    oos_start: date
    oos_end: date
    best_params: dict
    is_metrics: dict
    oos_metrics: dict


PARAM_GRID = {
    "min_range_pct": [0.0025, 0.0035, 0.0050],
    "min_atr_5m": [18.0, 22.0, 28.0],
    "volume_multiplier": [1.2, 1.5, 2.0],
    "sweep_closeback_bars": [2, 3, 5],
}


class WalkForwardTester:
    """
    Walk-forward optimisation with rolling windows.

    Default: 12-month in-sample, 3-month out-of-sample, sliding by 3 months.

    Optimises on in-sample, validates on out-of-sample, then combines
    all OOS results for aggregate metrics.
    """

    def __init__(self, nifty_1m_path: str, vix_path: str,
                 is_months: int = 12, oos_months: int = 3,
                 param_grid: dict = None):
        self.nifty_1m_path = nifty_1m_path
        self.vix_path = vix_path
        self.is_months = is_months
        self.oos_months = oos_months
        self.param_grid = param_grid or PARAM_GRID
        self.windows: List[WFWindow] = []

    def _generate_windows(self, start_date: date, end_date: date
                          ) -> List[Tuple[date, date, date, date]]:
        windows = []
        cursor = start_date
        while True:
            is_start = cursor
            is_end = is_start + timedelta(days=self.is_months * 30)
            oos_start = is_end + timedelta(days=1)
            oos_end = oos_start + timedelta(days=self.oos_months * 30)
            if oos_end > end_date:
                break
            windows.append((is_start, is_end, oos_start, oos_end))
            cursor = oos_start
        return windows

    def _optimise_is(self, is_start: date, is_end: date
                     ) -> Tuple[dict, dict]:
        """Find best parameter combination on in-sample period."""
        param_names = list(self.param_grid.keys())
        param_values = list(self.param_grid.values())
        combos = list(product(*param_values))

        best_pf = -1
        best_params = {}
        best_metrics = {}

        for combo in combos:
            params = dict(zip(param_names, combo))
            cfg = TradingConfig(**params)
            bt = ORBSweepBacktester(cfg)
            bt.load_data(self.nifty_1m_path, self.vix_path)
            metrics = bt.run(start_date=is_start, end_date=is_end)

            if "error" in metrics:
                continue

            pf = metrics.get("profit_factor", 0)
            wr = metrics.get("win_rate_pct", 0)
            score = pf * (wr / 100) if pf > 0 else 0

            if score > best_pf:
                best_pf = score
                best_params = params
                best_metrics = metrics

        logger.info(
            f"IS optimised: PF={best_metrics.get('profit_factor', 0):.2f} "
            f"WR={best_metrics.get('win_rate_pct', 0):.1f}% "
            f"Params={best_params}"
        )
        return best_params, best_metrics

    def run(self, start_date: date = None, end_date: date = None) -> dict:
        """Run full walk-forward test."""
        bt_dummy = ORBSweepBacktester()
        bt_dummy.load_data(self.nifty_1m_path, self.vix_path)
        all_dates = bt_dummy.trade_dates

        if start_date is None:
            start_date = all_dates[0]
        if end_date is None:
            end_date = all_dates[-1]

        windows = self._generate_windows(start_date, end_date)
        logger.info(f"Walk-forward: {len(windows)} windows")

        self.windows = []
        all_oos_trades = []

        for i, (is_start, is_end, oos_start, oos_end) in enumerate(windows):
            logger.info(
                f"Window {i+1}/{len(windows)}: "
                f"IS={is_start}→{is_end} | OOS={oos_start}→{oos_end}"
            )

            best_params, is_metrics = self._optimise_is(is_start, is_end)

            cfg_oos = TradingConfig(**best_params)
            bt_oos = ORBSweepBacktester(cfg_oos)
            bt_oos.load_data(self.nifty_1m_path, self.vix_path)
            oos_metrics = bt_oos.run(start_date=oos_start, end_date=oos_end)
            all_oos_trades.extend(bt_oos.trades)

            wf = WFWindow(
                is_start=is_start,
                is_end=is_end,
                oos_start=oos_start,
                oos_end=oos_end,
                best_params=best_params,
                is_metrics=is_metrics,
                oos_metrics=oos_metrics,
            )
            self.windows.append(wf)

            logger.info(
                f"  IS: PF={is_metrics.get('profit_factor', 0):.2f} "
                f"WR={is_metrics.get('win_rate_pct', 0):.1f}%"
            )
            logger.info(
                f"  OOS: PF={oos_metrics.get('profit_factor', 0):.2f} "
                f"WR={oos_metrics.get('win_rate_pct', 0):.1f}%"
            )

        agg_bt = ORBSweepBacktester()
        agg_bt.load_data(self.nifty_1m_path, self.vix_path)
        agg_bt.trades = all_oos_trades
        aggregate = agg_bt._compute_metrics()

        return {
            "aggregate_oos": aggregate,
            "n_windows": len(windows),
            "windows": [
                {
                    "is_period": f"{w.is_start} → {w.is_end}",
                    "oos_period": f"{w.oos_start} → {w.oos_end}",
                    "params": w.best_params,
                    "is_pf": w.is_metrics.get("profit_factor", 0),
                    "is_wr": w.is_metrics.get("win_rate_pct", 0),
                    "oos_pf": w.oos_metrics.get("profit_factor", 0),
                    "oos_wr": w.oos_metrics.get("win_rate_pct", 0),
                    "oos_net_pnl": w.oos_metrics.get("net_pnl", 0),
                }
                for w in self.windows
            ],
        }

    def print_report(self, results: dict):
        agg = results["aggregate_oos"]
        sep = "-" * 60
        print(f"\n{'=' * 60}")
        print("  WALK-FORWARD TEST — Aggregate OOS Results")
        print(f"{'=' * 60}")
        print(f"  Windows         : {results['n_windows']}")
        if "error" not in agg:
            print(f"  OOS Trades      : {agg['n_trades']}")
            print(f"  OOS Win Rate    : {agg['win_rate_pct']:.1f}%")
            print(f"  OOS Profit Factor: {agg['profit_factor']:.3f}")
            print(f"  OOS Net P&L     : Rs{agg['net_pnl']:>12,.2f}")
            print(f"  OOS CAGR        : {agg['cagr_pct']:.1f}%")
            print(f"  OOS Sharpe      : {agg['sharpe']:.3f}")
            print(f"  OOS Max DD      : {agg['max_drawdown_pct']:.1f}%")
        print(sep)
        print("  Per-Window Breakdown:")
        for w in results["windows"]:
            pf_ratio = w["oos_pf"] / w["is_pf"] if w["is_pf"] > 0 else 0
            print(
                f"    {w['oos_period']} | "
                f"PF: {w['is_pf']:.2f}→{w['oos_pf']:.2f} "
                f"(ratio={pf_ratio:.2f}) | "
                f"WR: {w['is_wr']:.0f}%→{w['oos_wr']:.0f}% | "
                f"P&L: Rs{w['oos_net_pnl']:+,.0f}"
            )
        print(f"{'=' * 60}\n")
