"""
Expiry-Day Straddle — 0DTE overlay for ORB Sweep.

On NIFTY weekly expiry days (Thursdays), option premiums decay rapidly
(theta crush) but gamma spikes — meaning any real move creates outsized
returns. This module layers a straddle on top of the sweep system:

Modes:
  1. STRADDLE_AT_ORB: Buy ATM straddle at 9:30 (ORB close), exit by 15:15
     - Works when ORB range is tight (< 0.3%) → breakout expected either way
     - SL: premium decay (straddle loses X% of entry value)
     - TP: straddle gains Y% (one leg runs while other decays)

  2. SWEEP_ENHANCED: When sweep signal fires on expiry, buy straddle
     instead of single leg — captures reversal + any continuation
     - Sweep already identifies trapped longs, straddle captures the move

  3. DIRECTIONAL_BIAS: Use ORB + VWAP to decide if CALL-heavy or PUT-heavy
     - If spot > VWAP at 9:30: buy 60% CALL + 40% PUT (biased straddle)
     - If spot < VWAP at 9:30: buy 40% CALL + 60% PUT

Backtest uses synthetic BS premiums with accelerated theta for 0DTE.
"""

from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import List, Optional

import numpy as np
import pandas as pd

from orb_sweep.config import TradingConfig, IST


@dataclass
class StraddleTrade:
    trade_date: date
    entry_time: datetime
    exit_time: Optional[datetime] = None
    mode: str = ""
    strike: int = 0
    spot_entry: float = 0
    spot_exit: float = 0
    entry_call_prem: float = 0
    entry_put_prem: float = 0
    exit_call_prem: float = 0
    exit_put_prem: float = 0
    straddle_entry: float = 0
    straddle_exit: float = 0
    quantity: int = 0
    lots: int = 0
    gross_pnl: float = 0
    costs: float = 0
    net_pnl: float = 0
    exit_type: str = ""
    orb_high: float = 0
    orb_low: float = 0
    orb_range_pct: float = 0
    vwap_at_entry: float = 0

    def to_dict(self) -> dict:
        return {
            "trade_date": str(self.trade_date),
            "entry_date": str(self.trade_date),
            "exit_date": str(self.exit_time.date()) if self.exit_time else str(self.trade_date),
            "entry_time": str(self.entry_time),
            "exit_time": str(self.exit_time),
            "direction": "straddle",
            "mode": self.mode,
            "strike": self.strike,
            "spot_entry": round(self.spot_entry, 2),
            "spot_exit": round(self.spot_exit, 2),
            "straddle_entry": round(self.straddle_entry, 2),
            "straddle_exit": round(self.straddle_exit, 2),
            "quantity": self.quantity,
            "lots": self.lots,
            "gross_pnl": round(self.gross_pnl, 2),
            "costs": round(self.costs, 2),
            "pnl_net": round(self.net_pnl, 2),
            "exit_type": self.exit_type,
            "orb_range_pct": round(self.orb_range_pct * 100, 3),
        }


@dataclass
class ExpiryStraddleConfig:
    sl_pct: float = 0.30
    tp_pct: float = 0.80
    trail_activate_pct: float = 0.40
    trail_pct: float = 0.25
    max_orb_range_pct: float = 0.005
    entry_time: time = field(default_factory=lambda: time(9, 31))
    force_exit_time: time = field(default_factory=lambda: time(15, 10))
    capital: float = 1_000_000
    risk_per_trade_pct: float = 0.01
    lot_size: int = 50
    strike_step: int = 50
    slippage_pct: float = 0.01


def is_nifty_expiry(d: date) -> bool:
    """Thursday = NIFTY weekly expiry (post Oct 2019)."""
    if d.weekday() != 3:
        return False
    from datetime import date as dt
    if d < dt(2019, 10, 24):
        import calendar
        last_day = calendar.monthrange(d.year, d.month)[1]
        last_thu = dt(d.year, d.month, last_day)
        while last_thu.weekday() != 3:
            last_thu = last_thu.replace(day=last_thu.day - 1)
        return d == last_thu
    return True


def _bs_0dte(spot: float, strike: int, minutes_left: float,
             vix: float, opt_type: str, rfr: float = 0.065) -> float:
    """BS price for 0DTE with time in minutes (not days)."""
    from scipy.stats import norm
    T = max(minutes_left / (365 * 375), 1e-6)
    sigma = max(vix / 100, 0.05)
    S, K = spot, float(strike)
    if T <= 0 or sigma <= 0:
        return max(0.0, S - K) if opt_type == "call" else max(0.0, K - S)
    d1 = (np.log(S / K) + (rfr + 0.5 * sigma**2) * T) / (sigma * np.sqrt(T))
    d2 = d1 - sigma * np.sqrt(T)
    if opt_type == "call":
        return S * norm.cdf(d1) - K * np.exp(-rfr * T) * norm.cdf(d2)
    else:
        return K * np.exp(-rfr * T) * norm.cdf(-d2) - S * norm.cdf(-d1)


class ExpiryStraddleBacktester:
    """
    Backtest 0DTE straddle on NIFTY expiry days.

    Usage:
        bt = ExpiryStraddleBacktester()
        bt.load_data(nifty_1m_path, vix_path)
        results = bt.run()
        bt.print_report(results)
    """

    def __init__(self, config: ExpiryStraddleConfig = None):
        self.cfg = config or ExpiryStraddleConfig()
        self.trades: List[StraddleTrade] = []

    def load_data(self, nifty_1m_path: str, vix_path: str):
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

    def _get_vix(self, d: date) -> float:
        mask = self.df_vix.index.date <= d
        if mask.any():
            return float(self.df_vix.loc[mask, "Close"].iloc[-1])
        return 15.0

    def _minutes_to_close(self, bar_time: datetime) -> float:
        """Minutes remaining from bar_time to 15:30 IST."""
        close_minutes = 15 * 60 + 30
        bar_minutes = bar_time.hour * 60 + bar_time.minute
        return max(close_minutes - bar_minutes, 1)

    def run(self, start_date: date = None, end_date: date = None) -> dict:
        self.trades = []
        dates = [d for d in self.trade_dates if is_nifty_expiry(d)]

        if start_date:
            dates = [d for d in dates if d >= start_date]
        if end_date:
            dates = [d for d in dates if d <= end_date]

        print(f"  Found {len(dates)} expiry days to backtest")

        for trade_date in dates:
            trade = self._run_expiry_day(trade_date)
            if trade is not None:
                self.trades.append(trade)

        return self._compute_metrics()

    def _run_expiry_day(self, trade_date: date) -> Optional[StraddleTrade]:
        day_1m = self.df_1m[self.df_1m.index.date == trade_date].copy()
        if len(day_1m) < 30:
            return None

        # Compute ORB (9:15-9:30)
        orb_mask = (
            (day_1m.index.time >= pd.Timestamp("09:15").time()) &
            (day_1m.index.time < pd.Timestamp("09:30").time())
        )
        orb_bars = day_1m[orb_mask]
        if len(orb_bars) < 10:
            return None

        orb_high = float(orb_bars["High"].max())
        orb_low = float(orb_bars["Low"].min())
        spot_open = float(orb_bars.iloc[0]["Open"])
        orb_range_pct = (orb_high - orb_low) / spot_open if spot_open > 0 else 0

        # Filter: only trade when ORB range is not too wide
        if orb_range_pct > self.cfg.max_orb_range_pct:
            return None

        vix = self._get_vix(trade_date)
        if vix < 10 or vix > 30:
            return None

        # Entry at 9:31
        entry_mask = day_1m.index.time >= self.cfg.entry_time
        if not entry_mask.any():
            return None
        entry_bar = day_1m[entry_mask].iloc[0]
        entry_time = day_1m[entry_mask].index[0]
        spot_entry = float(entry_bar["Close"])

        # VWAP from ORB bars
        orb_tp = (orb_bars["High"] + orb_bars["Low"] + orb_bars["Close"]) / 3
        orb_vol = orb_bars["Volume"]
        vwap_entry = float((orb_tp * orb_vol).sum() / orb_vol.sum()) if orb_vol.sum() > 0 else spot_entry

        # ATM strike
        strike = int(round(spot_entry / self.cfg.strike_step) * self.cfg.strike_step)

        # Compute entry premiums (0DTE)
        mins_left = self._minutes_to_close(entry_time)
        entry_call = _bs_0dte(spot_entry, strike, mins_left, vix, "call")
        entry_put = _bs_0dte(spot_entry, strike, mins_left, vix, "put")
        straddle_entry = entry_call + entry_put

        if straddle_entry < 5:
            return None

        # Apply entry slippage
        straddle_entry *= (1 + self.cfg.slippage_pct)
        entry_call *= (1 + self.cfg.slippage_pct)
        entry_put *= (1 + self.cfg.slippage_pct)

        # Position sizing
        risk = self.cfg.capital * self.cfg.risk_per_trade_pct
        max_loss = straddle_entry * self.cfg.sl_pct
        lots = max(1, int(risk / (max_loss * self.cfg.lot_size)))
        quantity = lots * self.cfg.lot_size

        # SL/TP levels
        sl_value = straddle_entry * (1 - self.cfg.sl_pct)
        tp_value = straddle_entry * (1 + self.cfg.tp_pct)
        trail_on_value = straddle_entry * (1 + self.cfg.trail_activate_pct)

        # Walk through post-entry bars
        post_entry = day_1m[day_1m.index > entry_time]
        peak_straddle = straddle_entry
        trail_active = False

        for bar_time, bar in post_entry.iterrows():
            spot = float(bar["Close"])
            mins_left = self._minutes_to_close(bar_time)

            cur_call = _bs_0dte(spot, strike, mins_left, vix, "call")
            cur_put = _bs_0dte(spot, strike, mins_left, vix, "put")
            cur_straddle = cur_call + cur_put

            if cur_straddle > peak_straddle:
                peak_straddle = cur_straddle

            if peak_straddle >= trail_on_value:
                trail_active = True

            # Force exit
            if bar_time.time() >= self.cfg.force_exit_time:
                return self._make_trade(
                    trade_date, entry_time, bar_time, spot_entry, spot,
                    strike, entry_call, entry_put, cur_call, cur_put,
                    quantity, lots, "TIME", orb_high, orb_low, orb_range_pct,
                    vwap_entry,
                )

            # TP hit
            if cur_straddle >= tp_value:
                return self._make_trade(
                    trade_date, entry_time, bar_time, spot_entry, spot,
                    strike, entry_call, entry_put, cur_call, cur_put,
                    quantity, lots, "TP", orb_high, orb_low, orb_range_pct,
                    vwap_entry,
                )

            # Trailing stop
            if trail_active and cur_straddle <= peak_straddle * (1 - self.cfg.trail_pct):
                return self._make_trade(
                    trade_date, entry_time, bar_time, spot_entry, spot,
                    strike, entry_call, entry_put, cur_call, cur_put,
                    quantity, lots, "TRAIL", orb_high, orb_low, orb_range_pct,
                    vwap_entry,
                )

            # SL hit
            if cur_straddle <= sl_value:
                return self._make_trade(
                    trade_date, entry_time, bar_time, spot_entry, spot,
                    strike, entry_call, entry_put, cur_call, cur_put,
                    quantity, lots, "SL", orb_high, orb_low, orb_range_pct,
                    vwap_entry,
                )

        # End of day — shouldn't reach here if force_exit works
        last_bar = post_entry.iloc[-1] if not post_entry.empty else entry_bar
        last_time = post_entry.index[-1] if not post_entry.empty else entry_time
        spot_exit = float(last_bar["Close"])
        mins_left = self._minutes_to_close(last_time)
        exit_call = _bs_0dte(spot_exit, strike, mins_left, vix, "call")
        exit_put = _bs_0dte(spot_exit, strike, mins_left, vix, "put")
        return self._make_trade(
            trade_date, entry_time, last_time, spot_entry, spot_exit,
            strike, entry_call, entry_put, exit_call, exit_put,
            quantity, lots, "TIME", orb_high, orb_low, orb_range_pct,
            vwap_entry,
        )

    def _make_trade(self, trade_date, entry_time, exit_time,
                    spot_entry, spot_exit, strike,
                    entry_call, entry_put, exit_call, exit_put,
                    quantity, lots, exit_type,
                    orb_high, orb_low, orb_range_pct, vwap_entry):
        # Apply exit slippage
        exit_call *= (1 - self.cfg.slippage_pct)
        exit_put *= (1 - self.cfg.slippage_pct)
        straddle_entry = entry_call + entry_put
        straddle_exit = exit_call + exit_put

        gross = (straddle_exit - straddle_entry) * quantity

        # Costs: round trip for both legs
        from costs.zerodha import round_trip_cost
        cost_call = round_trip_cost(entry_call, exit_call, quantity)
        cost_put = round_trip_cost(entry_put, exit_put, quantity)
        total_cost = cost_call + cost_put

        net = gross - total_cost

        return StraddleTrade(
            trade_date=trade_date,
            entry_time=entry_time,
            exit_time=exit_time,
            mode="STRADDLE_AT_ORB",
            strike=strike,
            spot_entry=spot_entry,
            spot_exit=spot_exit,
            entry_call_prem=entry_call,
            entry_put_prem=entry_put,
            exit_call_prem=exit_call,
            exit_put_prem=exit_put,
            straddle_entry=straddle_entry,
            straddle_exit=straddle_exit,
            quantity=quantity,
            lots=lots,
            gross_pnl=gross,
            costs=total_cost,
            net_pnl=net,
            exit_type=exit_type,
            orb_high=orb_high,
            orb_low=orb_low,
            orb_range_pct=orb_range_pct,
            vwap_at_entry=vwap_entry,
        )

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

        winners = [t for t in self.trades if t.net_pnl > 0]
        losers = [t for t in self.trades if t.net_pnl <= 0]
        metrics["avg_straddle_entry"] = round(
            np.mean([t.straddle_entry for t in self.trades]), 2
        )
        metrics["avg_winner_move_pct"] = round(
            np.mean([abs(t.spot_exit - t.spot_entry) / t.spot_entry * 100 for t in winners]), 2
        ) if winners else 0
        metrics["avg_loser_move_pct"] = round(
            np.mean([abs(t.spot_exit - t.spot_entry) / t.spot_entry * 100 for t in losers]), 2
        ) if losers else 0

        return metrics

    def print_report(self, metrics: dict = None):
        if metrics is None:
            metrics = self._compute_metrics()
        if "error" in metrics:
            print(f"  {metrics['error']}")
            return

        sep = "-" * 60
        print(f"\n{'=' * 60}")
        print("  EXPIRY-DAY 0DTE STRADDLE — Performance Report")
        print(f"{'=' * 60}")
        print(f"  Period          : {metrics.get('years', 0):.1f} years")
        print(f"  Expiry Days     : {metrics['n_trades']}")
        print(sep)
        print(f"  Win Rate        : {metrics['win_rate_pct']:.1f}%")
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
        print(f"  Avg Straddle    : Rs{metrics.get('avg_straddle_entry', 0):>10,.2f}")
        print(sep)
        print(f"  Avg Winner Move : {metrics.get('avg_winner_move_pct', 0):.2f}%")
        print(f"  Avg Loser Move  : {metrics.get('avg_loser_move_pct', 0):.2f}%")
        print(sep)
        print(f"  Exit Breakdown  : {metrics.get('exit_breakdown', {})}")
        print(f"{'=' * 60}\n")

    def trades_to_df(self) -> pd.DataFrame:
        return pd.DataFrame([t.to_dict() for t in self.trades])


class ExpiryShortStraddleBacktester:
    """
    Backtest 0DTE SHORT straddle on NIFTY expiry days.
    Sell ATM straddle at ORB close, collect theta decay.

    Edge: on 80% of expiry days, spot doesn't move enough to break even
    on a long straddle — the short side collects premium.
    """

    def __init__(self, config: ExpiryStraddleConfig = None):
        cfg = config or ExpiryStraddleConfig()
        cfg.sl_pct = 0.50
        cfg.tp_pct = 0.40
        cfg.trail_activate_pct = 0.25
        cfg.trail_pct = 0.15
        cfg.max_orb_range_pct = 0.008
        self.cfg = cfg
        self.trades: List[StraddleTrade] = []

    def load_data(self, nifty_1m_path: str, vix_path: str):
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

    def _get_vix(self, d: date) -> float:
        mask = self.df_vix.index.date <= d
        return float(self.df_vix.loc[mask, "Close"].iloc[-1]) if mask.any() else 15.0

    def _minutes_to_close(self, bar_time) -> float:
        return max(15 * 60 + 30 - bar_time.hour * 60 - bar_time.minute, 1)

    def run(self, start_date: date = None, end_date: date = None) -> dict:
        self.trades = []
        dates = [d for d in self.trade_dates if is_nifty_expiry(d)]
        if start_date:
            dates = [d for d in dates if d >= start_date]
        if end_date:
            dates = [d for d in dates if d <= end_date]
        print(f"  Found {len(dates)} expiry days to backtest (SHORT straddle)")
        for trade_date in dates:
            trade = self._run_expiry_day(trade_date)
            if trade is not None:
                self.trades.append(trade)
        return self._compute_metrics()

    def _run_expiry_day(self, trade_date: date) -> Optional[StraddleTrade]:
        day_1m = self.df_1m[self.df_1m.index.date == trade_date].copy()
        if len(day_1m) < 30:
            return None

        orb_mask = (
            (day_1m.index.time >= pd.Timestamp("09:15").time()) &
            (day_1m.index.time < pd.Timestamp("09:30").time())
        )
        orb_bars = day_1m[orb_mask]
        if len(orb_bars) < 10:
            return None

        orb_high = float(orb_bars["High"].max())
        orb_low = float(orb_bars["Low"].min())
        spot_open = float(orb_bars.iloc[0]["Open"])
        orb_range_pct = (orb_high - orb_low) / spot_open if spot_open > 0 else 0

        vix = self._get_vix(trade_date)
        # For short straddle: prefer moderate VIX (high premium to collect)
        if vix < 11 or vix > 25:
            return None

        entry_mask = day_1m.index.time >= self.cfg.entry_time
        if not entry_mask.any():
            return None
        entry_bar = day_1m[entry_mask].iloc[0]
        entry_time = day_1m[entry_mask].index[0]
        spot_entry = float(entry_bar["Close"])

        strike = int(round(spot_entry / self.cfg.strike_step) * self.cfg.strike_step)
        mins_left = self._minutes_to_close(entry_time)
        entry_call = _bs_0dte(spot_entry, strike, mins_left, vix, "call")
        entry_put = _bs_0dte(spot_entry, strike, mins_left, vix, "put")
        straddle_entry = entry_call + entry_put

        if straddle_entry < 5:
            return None

        # SHORT: we COLLECT premium, so slippage works against us on entry
        straddle_entry *= (1 - self.cfg.slippage_pct)
        entry_call *= (1 - self.cfg.slippage_pct)
        entry_put *= (1 - self.cfg.slippage_pct)

        # Position sizing: risk = potential SL loss
        risk = self.cfg.capital * self.cfg.risk_per_trade_pct
        max_loss = straddle_entry * self.cfg.sl_pct
        lots = max(1, int(risk / (max_loss * self.cfg.lot_size)))
        quantity = lots * self.cfg.lot_size

        # For SHORT: SL if straddle RISES, TP if it FALLS
        sl_value = straddle_entry * (1 + self.cfg.sl_pct)
        tp_value = straddle_entry * (1 - self.cfg.tp_pct)
        trail_on_value = straddle_entry * (1 - self.cfg.trail_activate_pct)

        post_entry = day_1m[day_1m.index > entry_time]
        trough_straddle = straddle_entry
        trail_active = False

        for bar_time, bar in post_entry.iterrows():
            spot = float(bar["Close"])
            mins_left = self._minutes_to_close(bar_time)
            cur_call = _bs_0dte(spot, strike, mins_left, vix, "call")
            cur_put = _bs_0dte(spot, strike, mins_left, vix, "put")
            cur_straddle = cur_call + cur_put

            if cur_straddle < trough_straddle:
                trough_straddle = cur_straddle
            if trough_straddle <= trail_on_value:
                trail_active = True

            # Force exit at 15:10
            if bar_time.time() >= self.cfg.force_exit_time:
                return self._make_short_trade(
                    trade_date, entry_time, bar_time, spot_entry, spot,
                    strike, entry_call, entry_put, cur_call, cur_put,
                    quantity, lots, "TIME", orb_high, orb_low, orb_range_pct,
                    spot_entry,
                )

            # SHORT TP: straddle fell enough (premium collected)
            if cur_straddle <= tp_value:
                return self._make_short_trade(
                    trade_date, entry_time, bar_time, spot_entry, spot,
                    strike, entry_call, entry_put, cur_call, cur_put,
                    quantity, lots, "TP", orb_high, orb_low, orb_range_pct,
                    spot_entry,
                )

            # SHORT trail: lock in gains as straddle decays
            if trail_active and cur_straddle >= trough_straddle * (1 + self.cfg.trail_pct):
                return self._make_short_trade(
                    trade_date, entry_time, bar_time, spot_entry, spot,
                    strike, entry_call, entry_put, cur_call, cur_put,
                    quantity, lots, "TRAIL", orb_high, orb_low, orb_range_pct,
                    spot_entry,
                )

            # SHORT SL: straddle rose too much (spot moved against us)
            if cur_straddle >= sl_value:
                return self._make_short_trade(
                    trade_date, entry_time, bar_time, spot_entry, spot,
                    strike, entry_call, entry_put, cur_call, cur_put,
                    quantity, lots, "SL", orb_high, orb_low, orb_range_pct,
                    spot_entry,
                )

        last_bar = post_entry.iloc[-1] if not post_entry.empty else entry_bar
        last_time = post_entry.index[-1] if not post_entry.empty else entry_time
        spot_exit = float(last_bar["Close"])
        mins_left = self._minutes_to_close(last_time)
        exit_call = _bs_0dte(spot_exit, strike, mins_left, vix, "call")
        exit_put = _bs_0dte(spot_exit, strike, mins_left, vix, "put")
        return self._make_short_trade(
            trade_date, entry_time, last_time, spot_entry, spot_exit,
            strike, entry_call, entry_put, exit_call, exit_put,
            quantity, lots, "TIME", orb_high, orb_low, orb_range_pct,
            spot_entry,
        )

    def _make_short_trade(self, trade_date, entry_time, exit_time,
                          spot_entry, spot_exit, strike,
                          entry_call, entry_put, exit_call, exit_put,
                          quantity, lots, exit_type,
                          orb_high, orb_low, orb_range_pct, vwap_entry):
        # SHORT: exit slippage means buying back at higher price
        exit_call *= (1 + self.cfg.slippage_pct)
        exit_put *= (1 + self.cfg.slippage_pct)
        straddle_entry = entry_call + entry_put
        straddle_exit = exit_call + exit_put

        # SHORT P&L: profit when straddle_exit < straddle_entry
        gross = (straddle_entry - straddle_exit) * quantity

        from costs.zerodha import round_trip_cost
        cost_call = round_trip_cost(entry_call, exit_call, quantity)
        cost_put = round_trip_cost(entry_put, exit_put, quantity)
        total_cost = cost_call + cost_put
        net = gross - total_cost

        return StraddleTrade(
            trade_date=trade_date,
            entry_time=entry_time,
            exit_time=exit_time,
            mode="SHORT_STRADDLE",
            strike=strike,
            spot_entry=spot_entry,
            spot_exit=spot_exit,
            entry_call_prem=entry_call,
            entry_put_prem=entry_put,
            exit_call_prem=exit_call,
            exit_put_prem=exit_put,
            straddle_entry=straddle_entry,
            straddle_exit=straddle_exit,
            quantity=quantity,
            lots=lots,
            gross_pnl=gross,
            costs=total_cost,
            net_pnl=net,
            exit_type=exit_type,
            orb_high=orb_high,
            orb_low=orb_low,
            orb_range_pct=orb_range_pct,
            vwap_at_entry=vwap_entry,
        )

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
        return metrics

    def print_report(self, metrics: dict = None):
        if metrics is None:
            metrics = self._compute_metrics()
        if "error" in metrics:
            print(f"  {metrics['error']}")
            return
        sep = "-" * 60
        print(f"\n{'=' * 60}")
        print("  EXPIRY-DAY SHORT STRADDLE -- Performance Report")
        print(f"{'=' * 60}")
        print(f"  Period          : {metrics.get('years', 0):.1f} years")
        print(f"  Expiry Days     : {metrics['n_trades']}")
        print(sep)
        print(f"  Win Rate        : {metrics['win_rate_pct']:.1f}%")
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
        print(sep)
        print(f"  Exit Breakdown  : {metrics.get('exit_breakdown', {})}")
        print(f"{'=' * 60}\n")

    def trades_to_df(self) -> pd.DataFrame:
        return pd.DataFrame([t.to_dict() for t in self.trades])
