"""Backtesting engine for ORB Sweep strategy."""

import logging
import sys
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import List, Optional

import numpy as np
import pandas as pd

from orb_sweep.config import TradingConfig, PROJECT_ROOT
from orb_sweep.strategy import ORBSweepStrategy, TradeSignal
from orb_sweep.indicators import atr, resample_to_5m, vwap

logger = logging.getLogger(__name__)

sys.path.insert(0, str(PROJECT_ROOT))
from backtest.option_pricer import bs_price, atm_strike, next_expiry
from costs.zerodha import round_trip_cost


@dataclass
class BacktestTrade:
    trade_date: date
    entry_time: datetime
    exit_time: Optional[datetime] = None
    direction: str = ""
    strike: int = 0
    spot_entry: float = 0
    spot_exit: float = 0
    entry_premium: float = 0
    exit_premium: float = 0
    quantity: int = 0
    lots: int = 0
    gross_pnl: float = 0
    costs: float = 0
    net_pnl: float = 0
    stop_loss: float = 0
    tp1: float = 0
    tp2: float = 0
    tp1_hit: bool = False
    exit_type: str = ""
    r_value: float = 0
    r_multiple: float = 0
    orb_high: float = 0
    orb_low: float = 0
    vwap_at_entry: float = 0

    def to_dict(self) -> dict:
        return {
            "trade_date": str(self.trade_date),
            "entry_date": str(self.entry_time.date()) if self.entry_time else "",
            "exit_date": str(self.exit_time.date()) if self.exit_time else "",
            "entry_time": str(self.entry_time),
            "exit_time": str(self.exit_time),
            "direction": self.direction,
            "strike": self.strike,
            "spot_entry": self.spot_entry,
            "spot_exit": self.spot_exit,
            "entry_premium": round(self.entry_premium, 2),
            "exit_premium": round(self.exit_premium, 2),
            "quantity": self.quantity,
            "lots": self.lots,
            "pnl_net": round(self.net_pnl, 2),
            "gross_pnl": round(self.gross_pnl, 2),
            "costs": round(self.costs, 2),
            "exit_type": self.exit_type,
            "r_value": round(self.r_value, 2),
            "r_multiple": round(self.r_multiple, 2),
        }


def _synthetic_premium(spot: float, strike: int, direction: str,
                       vix: float, dte_days: float, rfr: float = 0.065) -> float:
    """Black-Scholes synthetic option premium."""
    iv = max(vix / 100, 0.05)
    t = max(dte_days / 365, 0.001)
    opt_type = "call" if direction == "CALL" else "put"
    return bs_price(spot, strike, t, rfr, iv, opt_type)


class ORBSweepBacktester:
    """
    Run the ORB Sweep strategy on historical 1-minute data.

    Usage:
        bt = ORBSweepBacktester()
        bt.load_data("data/zerodha_nifty_1m.csv", "data/zerodha_vix_day.csv")
        results = bt.run()
        bt.print_report(results)
    """

    def __init__(self, config: TradingConfig = None):
        self.cfg = config or TradingConfig()
        self.strategy = ORBSweepStrategy(self.cfg)
        self.trades: List[BacktestTrade] = []

    def load_data(self, nifty_1m_path: str, vix_path: str):
        """Load 1-minute NIFTY data and daily VIX data."""
        df = pd.read_csv(nifty_1m_path, parse_dates=["datetime"])
        if "datetime" in df.columns:
            df = df.set_index("datetime")
        for col in ["Open", "High", "Low", "Close", "Volume"]:
            c_lower = col.lower()
            if c_lower in df.columns and col not in df.columns:
                df = df.rename(columns={c_lower: col})
        df.index = pd.to_datetime(df.index)
        if df.index.tz is not None:
            df.index = df.index.tz_localize(None)
        self.df_1m = df.sort_index()

        vix = pd.read_csv(vix_path, parse_dates=["datetime"])
        if "datetime" in vix.columns:
            vix = vix.set_index("datetime")
        if "close" in vix.columns:
            vix = vix.rename(columns={"close": "Close"})
        vix.index = pd.to_datetime(vix.index)
        if vix.index.tz is not None:
            vix.index = vix.index.tz_localize(None)
        self.df_vix = vix.sort_index()

        self.trade_dates = sorted(set(self.df_1m.index.date))
        logger.info(
            f"Loaded {len(self.df_1m)} 1m bars, "
            f"{len(self.trade_dates)} trade dates, "
            f"{len(self.df_vix)} VIX records"
        )

    def _get_vix(self, d: date) -> float:
        mask = self.df_vix.index.date <= d
        if mask.any():
            return float(self.df_vix.loc[mask, "Close"].iloc[-1])
        return 15.0

    def run(self, start_date: date = None, end_date: date = None) -> dict:
        """Run backtest over all trade dates. Returns metrics dict."""
        self.trades = []
        dates = self.trade_dates

        if start_date:
            dates = [d for d in dates if d >= start_date]
        if end_date:
            dates = [d for d in dates if d <= end_date]

        logger.info(f"Backtesting {len(dates)} days: {dates[0]} to {dates[-1]}")
        weekly_pnl = 0.0
        current_week = None

        for trade_date in dates:
            week = trade_date - pd.Timedelta(days=trade_date.weekday())
            if current_week != week:
                current_week = week
                weekly_pnl = 0.0

            day_trades = self._run_day(trade_date, weekly_pnl)
            for t in day_trades:
                weekly_pnl += t.net_pnl
            self.trades.extend(day_trades)

        return self._compute_metrics()

    def _run_day(self, trade_date: date, weekly_pnl: float) -> List[BacktestTrade]:
        """Simulate one trading day."""
        self.strategy.reset_day()
        day_1m = self.df_1m[self.df_1m.index.date == trade_date].copy()
        if len(day_1m) < 30:
            return []

        lookback_start = trade_date - pd.Timedelta(days=10)
        lookback_1m = self.df_1m[
            (self.df_1m.index.date >= lookback_start) &
            (self.df_1m.index.date <= trade_date)
        ].copy()

        orb = self.strategy.compute_orb(lookback_1m, trade_date)
        if orb is None:
            return []

        regime_ok = self.strategy.check_regime(lookback_1m, trade_date)
        if not regime_ok:
            return []

        vix_today = self._get_vix(trade_date)
        day_trades = []
        daily_pnl = 0.0
        n_trades = 0
        active: Optional[BacktestTrade] = None
        tp1_done = False

        self.strategy.precompute_day(day_1m)

        post_orb = day_1m[day_1m.index.time >= pd.Timestamp("09:30").time()]

        for idx_pos in range(len(post_orb)):
            bar = post_orb.iloc[idx_pos]
            bar_time = post_orb.index[idx_pos]
            spot = float(bar["Close"])
            high = float(bar["High"])
            low = float(bar["Low"])

            # ── Force exit at 15:15 ──────────────────────────────────
            if bar_time.time() >= pd.Timestamp("15:15").time():
                if active:
                    active = self._exit_trade(
                        active, bar_time, spot, vix_today, "TIME"
                    )
                    daily_pnl += active.net_pnl
                    day_trades.append(active)
                    active = None
                break

            # ── Check daily/weekly DD limits ─────────────────────────
            if daily_pnl <= -self.cfg.daily_dd_limit:
                if active:
                    active = self._exit_trade(
                        active, bar_time, spot, vix_today, "DD_LIMIT"
                    )
                    daily_pnl += active.net_pnl
                    day_trades.append(active)
                    active = None
                break
            if weekly_pnl + daily_pnl <= -self.cfg.weekly_dd_limit:
                if active:
                    active = self._exit_trade(
                        active, bar_time, spot, vix_today, "DD_LIMIT"
                    )
                    daily_pnl += active.net_pnl
                    day_trades.append(active)
                    active = None
                break

            # ── Manage active position ───────────────────────────────
            if active:
                if self.cfg.use_premium_exits:
                    n_bars_total = len(post_orb)
                    elapsed_frac = idx_pos / max(n_bars_total, 1) * 0.8
                    t_now = max((dte_active - elapsed_frac) / 365, 0.001)
                    cur_prem = _synthetic_premium(
                        spot, active.strike, active.direction, vix_today,
                        dte_active - elapsed_frac,
                    )
                    if cur_prem > peak_prem:
                        peak_prem = cur_prem

                    is_call = active.direction == "CALL"
                    sl_p = self.cfg.call_sl_pct if is_call else self.cfg.put_sl_pct
                    tp_p = self.cfg.call_tp_pct if is_call else self.cfg.put_tp_pct
                    ta_p = self.cfg.call_trail_activate if is_call else self.cfg.put_trail_activate
                    tr_p = self.cfg.call_trail_pct if is_call else self.cfg.put_trail_pct

                    sl_prem = active.entry_premium * (1 - sl_p)
                    tp_prem = active.entry_premium * (1 + tp_p)
                    trail_on = peak_prem >= active.entry_premium * (1 + ta_p)

                    if cur_prem >= tp_prem:
                        active = self._exit_trade(active, bar_time, spot, vix_today, "TP", override_prem=cur_prem)
                        daily_pnl += active.net_pnl
                        day_trades.append(active)
                        active = None
                        continue
                    if trail_on and cur_prem <= peak_prem * (1 - tr_p):
                        active = self._exit_trade(active, bar_time, spot, vix_today, "TRAIL", override_prem=cur_prem)
                        daily_pnl += active.net_pnl
                        day_trades.append(active)
                        active = None
                        continue
                    if cur_prem <= sl_prem:
                        active = self._exit_trade(active, bar_time, spot, vix_today, "SL", override_prem=cur_prem)
                        daily_pnl += active.net_pnl
                        day_trades.append(active)
                        active = None
                        continue
                    continue
                else:
                    sl = active.stop_loss if not tp1_done else active.spot_entry
                    hit_sl = False
                    hit_tp1 = False
                    hit_tp2 = False

                    if active.direction == "CALL":
                        hit_sl = low <= sl
                        hit_tp1 = high >= active.tp1
                        hit_tp2 = high >= active.tp2
                    else:
                        hit_sl = high >= sl
                        hit_tp1 = low <= active.tp1
                        hit_tp2 = low <= active.tp2

                    if hit_tp1 and not tp1_done:
                        tp1_done = True

                    if hit_tp2 and tp1_done:
                        active = self._exit_trade(
                            active, bar_time, spot, vix_today, "TP2"
                        )
                        daily_pnl += active.net_pnl
                        day_trades.append(active)
                        active = None
                        tp1_done = False
                        continue

                    if hit_sl:
                        exit_type = "SL_BE" if tp1_done else "SL"
                        active = self._exit_trade(
                            active, bar_time, spot, vix_today, exit_type
                        )
                        daily_pnl += active.net_pnl
                        day_trades.append(active)
                        active = None
                        tp1_done = False
                        continue

                    continue

            # ── Scan for entry signal ────────────────────────────────
            if n_trades >= self.cfg.max_trades_per_day:
                continue
            if bar_time.time() >= pd.Timestamp("14:00").time():
                continue

            bar_global_idx = lookback_1m.index.get_loc(bar_time)
            if isinstance(bar_global_idx, slice):
                bar_global_idx = bar_global_idx.start

            day_local_pos = day_1m.index.get_loc(bar_time)
            if isinstance(day_local_pos, slice):
                day_local_pos = day_local_pos.start

            signal = self.strategy.scan_bar(
                lookback_1m, bar_global_idx, local_pos=day_local_pos
            )
            if signal is None:
                continue

            lots = max(1, int(
                self.cfg.risk_per_trade / (self.cfg.lot_size * signal.r_value)
            ))
            quantity = lots * self.cfg.lot_size
            strike = signal.strike
            expiry = next_expiry(trade_date)
            dte = max((expiry - trade_date).days, 1)

            entry_prem = _synthetic_premium(
                signal.entry_spot, strike, signal.direction, vix_today, dte
            )
            entry_prem *= (1 + self.cfg.slippage_pct)

            active = BacktestTrade(
                trade_date=trade_date,
                entry_time=bar_time,
                direction=signal.direction,
                strike=strike,
                spot_entry=signal.entry_spot,
                entry_premium=entry_prem,
                quantity=quantity,
                lots=lots,
                stop_loss=signal.stop_loss,
                tp1=signal.tp1,
                tp2=signal.tp2,
                r_value=signal.r_value,
                orb_high=orb.orb_high,
                orb_low=orb.orb_low,
                vwap_at_entry=signal.vwap_at_entry,
            )
            tp1_done = False
            peak_prem = entry_prem
            dte_active = dte
            n_trades += 1

        if active:
            last_bar = post_orb.iloc[-1]
            last_time = post_orb.index[-1]
            active = self._exit_trade(
                active, last_time, float(last_bar["Close"]), vix_today, "TIME"
            )
            daily_pnl += active.net_pnl
            day_trades.append(active)

        return day_trades

    def _exit_trade(self, trade: BacktestTrade, exit_time, spot: float,
                    vix: float, exit_type: str, override_prem: float = 0) -> BacktestTrade:
        if override_prem > 0:
            exit_prem = override_prem * (1 - self.cfg.slippage_pct)
        else:
            expiry = next_expiry(trade.trade_date)
            dte = max((expiry - exit_time.date()).days if hasattr(exit_time, 'date') else 1, 0.5)
            exit_prem = _synthetic_premium(spot, trade.strike, trade.direction, vix, dte)
            exit_prem *= (1 - self.cfg.slippage_pct)

        gross = (exit_prem - trade.entry_premium) * trade.quantity
        costs = round_trip_cost(trade.entry_premium, exit_prem, trade.quantity)
        net = gross - costs

        trade.exit_time = exit_time
        trade.spot_exit = spot
        trade.exit_premium = exit_prem
        trade.gross_pnl = gross
        trade.costs = costs
        trade.net_pnl = net
        trade.exit_type = exit_type
        trade.r_multiple = net / (trade.r_value * trade.quantity) if trade.r_value > 0 else 0
        trade.tp1_hit = exit_type in ("TP2", "SL_BE")
        return trade

    def _compute_metrics(self) -> dict:
        if not self.trades:
            return {"error": "No trades"}

        trade_dicts = [t.to_dict() for t in self.trades]
        from backtest.metrics import compute_metrics
        metrics = compute_metrics(trade_dicts, self.cfg.capital)

        exit_types = {}
        for t in self.trades:
            exit_types[t.exit_type] = exit_types.get(t.exit_type, 0) + 1
        metrics["exit_breakdown"] = exit_types

        r_multiples = [t.r_multiple for t in self.trades]
        metrics["avg_r_multiple"] = round(float(np.mean(r_multiples)), 2) if r_multiples else 0
        metrics["median_r_multiple"] = round(float(np.median(r_multiples)), 2) if r_multiples else 0

        calls = [t for t in self.trades if t.direction == "CALL"]
        puts = [t for t in self.trades if t.direction == "PUT"]
        metrics["call_trades"] = len(calls)
        metrics["put_trades"] = len(puts)
        metrics["call_win_rate"] = (
            round(sum(1 for t in calls if t.net_pnl > 0) / len(calls) * 100, 1)
            if calls else 0
        )
        metrics["put_win_rate"] = (
            round(sum(1 for t in puts if t.net_pnl > 0) / len(puts) * 100, 1)
            if puts else 0
        )

        return metrics

    def print_report(self, metrics: dict = None):
        if metrics is None:
            metrics = self._compute_metrics()
        if "error" in metrics:
            print(f"  {metrics['error']}")
            return

        sep = "-" * 55
        print(f"\n{'=' * 55}")
        print("  ORB SWEEP BACKTEST — Performance Report")
        print(f"{'=' * 55}")
        print(f"  Period          : {metrics.get('years', 0):.1f} years")
        print(f"  Total Trades    : {metrics['n_trades']}")
        print(f"  Calls / Puts    : {metrics.get('call_trades', 0)} / {metrics.get('put_trades', 0)}")
        print(sep)
        print(f"  Win Rate        : {metrics['win_rate_pct']:.1f}%")
        print(f"    Call WR       : {metrics.get('call_win_rate', 0):.1f}%")
        print(f"    Put WR        : {metrics.get('put_win_rate', 0):.1f}%")
        print(f"  Profit Factor   : {metrics['profit_factor']:.3f}")
        print(f"  Expectancy      : Rs{metrics['expectancy']:>10,.2f}")
        print(sep)
        print(f"  Net P&L         : Rs{metrics['net_pnl']:>12,.2f}")
        print(f"  CAGR            : {metrics['cagr_pct']:.1f}%")
        print(f"  Sharpe          : {metrics['sharpe']:.3f}")
        print(f"  Max Drawdown    : {metrics['max_drawdown_pct']:.1f}%")
        print(sep)
        print(f"  Avg Win         : Rs{metrics['avg_win']:>10,.2f}")
        print(f"  Avg Loss        : Rs{metrics['avg_loss']:>10,.2f}")
        print(f"  Avg R-Multiple  : {metrics.get('avg_r_multiple', 0):.2f}R")
        print(f"  Median R        : {metrics.get('median_r_multiple', 0):.2f}R")
        print(sep)
        print(f"  Exit Breakdown  : {metrics.get('exit_breakdown', {})}")
        print(f"{'=' * 55}\n")

    def trades_to_df(self) -> pd.DataFrame:
        return pd.DataFrame([t.to_dict() for t in self.trades])
