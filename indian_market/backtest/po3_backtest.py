"""
PO3 Strategy Backtest Engine
============================
Bar-by-bar simulation on 3-min OHLCV intraday data.

Data requirements:
  - df_3m  : 3-min OHLCV for Nifty spot (DatetimeIndex, IST)
  - df_15m : 15-min OHLCV for Nifty spot (same symbol)
  - vix_df : daily VIX DataFrame with 'Close' column (DatetimeIndex by date)

Option pricing:
  - Black-Scholes at entry using India VIX as IV
  - Same-day exit uses VIX × 1.05 (conservative — premium decays and VIX can spike)
  - All costs via costs.zerodha (STT, brokerage, exchange charges)

SL/Trail/TP logic (matches live po3_paper.py):
  - Hard SL: 30% of entry premium
  - Trail activates at +10% premium gain, trails 20% below peak
  - Hard time exit: 15:05 IST
  - Spot SL reference: stored but options managed by premium %

Output:
  - List of trade dicts (one per signal)
  - Summary metrics via backtest.metrics
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import numpy as np
import pandas as pd
from typing import List, Dict, Optional

from backtest.option_pricer import bs_price, atm_strike, next_expiry, dte_to_expiry
from backtest.metrics       import compute_metrics
from costs.zerodha          import round_trip_cost
from strategies.po3_strategy import (
    PO3SignalGenerator, PO3_PARAMS, generate_signals,
)

import pytz
IST = pytz.timezone("Asia/Kolkata")


class PO3Backtest:
    """
    Runs the PO3 strategy on historical 3-min + 15-min data.

    Usage:
        bt  = PO3Backtest(capital=1_000_000, params={...})
        trades = bt.run(df_3m, df_15m, vix_df)
        metrics = compute_metrics(trades, capital=1_000_000)
        bt.print_summary(metrics)
    """

    def __init__(self, capital: float = 1_000_000,
                 params: Optional[Dict] = None,
                 symbol: str = "NIFTY"):
        self.capital = capital
        self.p       = {**PO3_PARAMS, **(params or {})}
        self.symbol  = symbol
        self.trades: List[Dict] = []

    # ── Main run ───────────────────────────────────────────────────────────────

    def run(self,
            df_3m:  pd.DataFrame,
            df_15m: pd.DataFrame,
            vix_df: pd.DataFrame,
            start_date: Optional[str] = None,
            end_date:   Optional[str] = None,
            day_cache:  Optional[Dict] = None) -> List[Dict]:
        """
        Execute backtest. Returns list of trade dicts.
        """
        self.trades = []
        running_capital = self.capital

        # Align VIX to date index
        vix_by_date = vix_df["Close"].copy()
        vix_by_date.index = pd.to_datetime(vix_by_date.index).date

        # Date filter — strip tz from index for date-only comparison to avoid
        # tz-naive vs tz-aware mismatch when data comes from Zerodha (UTC+05:30)
        def _to_date(idx):
            if hasattr(idx, "date"):
                return idx.date
            return pd.to_datetime(idx).date

        if start_date:
            sd = pd.Timestamp(start_date).date()
            df_3m  = df_3m[_to_date(df_3m.index) >= sd]
            df_15m = df_15m[_to_date(df_15m.index) >= sd - pd.Timedelta(days=120)]
        if end_date:
            ed = pd.Timestamp(end_date).date()
            df_3m  = df_3m[_to_date(df_3m.index) <= ed]
            df_15m = df_15m[_to_date(df_15m.index) <= ed]

        trading_days = sorted(set(df_3m.index.date))
        gen = PO3SignalGenerator(self.p)

        for day in trading_days:
            day_3m = df_3m[df_3m.index.date == day]
            if len(day_3m) < 20:
                continue

            vix = float(vix_by_date.get(day, np.nan))
            if np.isnan(vix):
                continue

            # 15-min history up to this day (no lookahead)
            df_15m_hist = df_15m[df_15m.index.date <= day]
            if len(df_15m_hist) < self.p["htf_sma_period"] + 5:
                continue

            gen.new_day()
            cached = day_cache.get(day) if day_cache else None
            gen.prepare_day(df_15m_hist, cached=cached)
            gen.prepare_bars(day_3m)
            trade = None

            for i in range(20, len(day_3m)):
                df_3m_slice = day_3m.iloc[: i + 1]
                sig = gen.update(df_3m_slice, df_15m_hist, vix)

                if sig and trade is None:
                    trade = self._enter_trade(sig, vix, running_capital, day)
                    if trade is None:
                        continue

                # If in trade, check SL / trail / time exit
                if trade:
                    bar = day_3m.iloc[i]
                    result = self._update_trade(trade, bar, vix)
                    if result:
                        result["entry_date"] = str(day)
                        result["capital_before"] = running_capital
                        running_capital += result["pnl_net"]
                        result["running_capital"] = running_capital
                        self.trades.append(result)
                        trade = None

            # Hard time exit at 15:05 if still in trade
            if trade:
                last_bar = day_3m.iloc[-1]
                result = self._time_exit(trade, last_bar, vix)
                result["entry_date"] = str(day)
                result["capital_before"] = running_capital
                running_capital += result["pnl_net"]
                result["running_capital"] = running_capital
                self.trades.append(result)

        return self.trades

    # ── Trade lifecycle ────────────────────────────────────────────────────────

    def _enter_trade(self, sig: Dict, vix: float,
                     capital: float, day) -> Optional[Dict]:
        """Compute option premium at entry and return trade state dict."""
        import datetime
        entry_date = day if isinstance(day, datetime.date) else pd.Timestamp(day).date()
        expiry_date = next_expiry(entry_date)
        dte = dte_to_expiry(entry_date)
        if dte < 1:
            return None

        direction   = sig["direction"]
        spot_price  = sig["entry_price"]
        opt_type    = "call" if direction == "bull" else "put"
        strike      = atm_strike(spot_price, self.p["strike_step"])
        iv          = vix / 100.0
        T_entry     = dte / 365.0          # calendar days → years for Black-Scholes

        entry_prem = bs_price(spot_price, strike, T_entry, self.p["rfr"], iv, opt_type)
        if entry_prem < 5:
            return None

        # Position sizing: risk_per_trade / (sl_pct × premium × lot_size)
        sl_prem  = entry_prem * self.p["sl_pct"]
        lots     = max(1, min(
            self.p["max_lots"],
            int(self.p["risk_per_trade"] / (sl_prem * self.p["lot_size"]))
        ))
        qty      = lots * self.p["lot_size"]
        cost_rs  = round_trip_cost(entry_prem, entry_prem, qty)

        return {
            "direction"    : direction,
            "opt_type"     : opt_type,
            "strike"       : strike,
            "expiry"       : expiry_date,
            "dte"          : dte,
            "entry_prem"   : entry_prem,
            "entry_prem_net": entry_prem + cost_rs / qty,
            "sl_prem"      : entry_prem * (1 - self.p["sl_pct"]),
            "tp_prem"      : entry_prem * (1 + self.p["tp_pct"]),
            "trail_active"  : False,
            "breakeven_set" : False,
            "peak_prem"     : entry_prem,
            "trail_sl_prem" : None,
            "lots"         : lots,
            "qty"          : qty,
            "cost_rs"      : cost_rs,
            "signal"       : sig,
            "spot_entry"   : sig["entry_price"],
            "sl_spot"      : sig["sl_spot"],
            "ifvg"         : sig["ifvg"],
        }

    def _current_prem(self, trade: Dict, bar: pd.Series, vix: float) -> float:
        """Estimate current option premium from spot price using BS."""
        spot    = float(bar["Close"])
        # Remaining DTE in years; subtract ~half a day per bar processed intraday
        T_now   = max(0.25 / 365.0, (trade["dte"] - 0.5) / 365.0)
        iv_now  = (vix / 100.0) * 1.05   # slightly elevated IV for exit pricing
        prem = bs_price(spot, trade["strike"], T_now, self.p["rfr"], iv_now, trade["opt_type"])
        return max(prem, 1.0)

    def _update_trade(self, trade: Dict, bar: pd.Series, vix: float) -> Optional[Dict]:
        """
        Check SL / breakeven / trail / TP each bar.
        Exit order: hard SL → time exit → trail → TP.
        """
        entry_prem    = trade["entry_prem"]
        sl_pct        = self.p["sl_pct"]
        trail_act     = self.p["trail_activate_pct"]
        trail_pct     = self.p["trail_pct"]
        breakeven_pct = self.p.get("breakeven_pct", 0.0)

        T_now   = max(0.25 / 365.0, (trade["dte"] - 0.5) / 365.0)
        iv_exit = (vix / 100.0) * 1.05

        # Worst-case intrabar premium (call: use Low spot; put: use High spot)
        worst_spot = float(bar["Low"]) if trade["opt_type"] == "call" else float(bar["High"])
        worst_prem = bs_price(worst_spot, trade["strike"], T_now, self.p["rfr"], iv_exit, trade["opt_type"])

        # Dynamic floor SL: starts at hard SL, rises to breakeven once up breakeven_pct
        hard_sl = entry_prem * (1 - sl_pct)
        if breakeven_pct > 0 and not trade.get("breakeven_set"):
            close_prem_check = self._current_prem(trade, bar, vix)
            if (close_prem_check - entry_prem) / entry_prem >= breakeven_pct:
                trade["breakeven_set"] = True
                # Ratchet floor SL up to entry premium
                if entry_prem > trade.get("trail_sl_prem") or trade.get("trail_sl_prem") is None:
                    trade["trail_sl_prem"] = entry_prem
                    trade["trail_active"]  = True   # activate trail once breakeven reached

        floor_sl = trade["trail_sl_prem"] if trade.get("trail_sl_prem") is not None else hard_sl
        effective_sl = max(hard_sl, floor_sl)

        if worst_prem <= effective_sl:
            exit_reason = "TRAIL" if trade.get("trail_active") else "SL"
            return self._close_trade(trade, effective_sl, bar, vix, exit_reason)

        # Hard time exit (15:05)
        t = bar.name.time()
        if t.hour * 60 + t.minute >= self.p["hard_exit_h"] * 60 + self.p["hard_exit_m"]:
            close_prem = self._current_prem(trade, bar, vix)
            return self._close_trade(trade, close_prem, bar, vix, "TIME")

        # Update peak premium using bar close
        close_prem = self._current_prem(trade, bar, vix)
        if close_prem > trade["peak_prem"]:
            trade["peak_prem"] = close_prem

        # Activate trail once gain_pct >= trail_activate_pct
        gain_pct = (trade["peak_prem"] - entry_prem) / entry_prem
        if gain_pct >= trail_act:
            trade["trail_active"] = True

        # Trail: move SL up to (peak × (1 - trail_pct)), never lower it
        if trade["trail_active"]:
            new_trail_sl = trade["peak_prem"] * (1 - trail_pct)
            current_floor = trade["trail_sl_prem"] if trade["trail_sl_prem"] is not None else hard_sl
            if new_trail_sl > current_floor:
                trade["trail_sl_prem"] = new_trail_sl
            if close_prem <= trade["trail_sl_prem"]:
                return self._close_trade(trade, trade["trail_sl_prem"], bar, vix, "TRAIL")

        # TP
        if close_prem >= trade["tp_prem"]:
            return self._close_trade(trade, trade["tp_prem"], bar, vix, "TP")

        return None

    def _time_exit(self, trade: Dict, bar: pd.Series, vix: float) -> Dict:
        prem = self._current_prem(trade, bar, vix)
        return self._close_trade(trade, prem, bar, vix, "TIME")

    def _close_trade(self, trade: Dict, exit_prem: float,
                     bar: pd.Series, vix: float, reason: str) -> Dict:
        qty        = trade["qty"]
        entry_prem = trade["entry_prem"]

        gross_pnl  = (exit_prem - entry_prem) * qty
        cost_rs    = round_trip_cost(entry_prem, exit_prem, qty)
        pnl_net    = gross_pnl - cost_rs

        exit_date = str(pd.Timestamp(bar.name).date())
        return {
            "direction"    : trade["opt_type"],   # 'call'/'put' — matches compute_metrics
            "opt_type"     : trade["opt_type"],
            "strike"       : trade["strike"],
            "lots"         : trade["lots"],
            "qty"          : qty,
            "entry_prem"   : entry_prem,
            "exit_prem"    : exit_prem,
            "peak_prem"    : trade["peak_prem"],
            "gross_pnl"    : gross_pnl,
            "cost_rs"      : cost_rs,
            "pnl_net"      : pnl_net,
            "exit_type"    : reason,
            "exit_date"    : exit_date,           # required by compute_metrics
            "exit_ts"      : str(bar.name),
            "spot_entry"   : trade["spot_entry"],
            "sl_spot"      : trade["sl_spot"],
            "trail_active" : trade["trail_active"],
            "ifvg"         : str(trade["ifvg"]),  # JSON-serialisable
        }

    # ── Reporting ──────────────────────────────────────────────────────────────

    def print_summary(self, metrics: Optional[Dict] = None):
        if metrics is None:
            metrics = compute_metrics(self.trades, self.capital)

        print("\n" + "=" * 60)
        print("  PO3 Strategy — Backtest Results")
        print("=" * 60)
        if "error" in metrics:
            print(f"  {metrics['error']}")
            return

        print(f"  Trades        : {metrics.get('n_total', 0)}")
        print(f"  Win Rate      : {metrics.get('win_rate', 0):.1%}")
        print(f"  Profit Factor : {metrics.get('profit_factor', 0):.2f}")
        print(f"  Expectancy    : Rs {metrics.get('expectancy', 0):,.0f}")
        print(f"  Net P&L       : Rs {metrics.get('total_pnl', 0):,.0f}")
        print(f"  Max DD        : {metrics.get('max_dd', 0):.2f}%")
        print(f"  Calmar        : {metrics.get('calmar', 0):.2f}")
        print(f"  CAGR          : {metrics.get('cagr', 0):.1%}")
        print("=" * 60)

        # Exit type breakdown
        if self.trades:
            exits = pd.Series([t["exit_type"] for t in self.trades]).value_counts()
            print("\n  Exit breakdown:")
            for etype, cnt in exits.items():
                pct = cnt / len(self.trades) * 100
                print(f"    {etype:<8}: {cnt:3d}  ({pct:.0f}%)")
        print()


# ── Runner script ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    """
    Quick run:  python backtest/po3_backtest.py
    Expects CSV files saved by data/fetch_po3_data.py
    """
    from pathlib import Path
    DATA = Path(__file__).parent.parent / "data"

    f3  = DATA / "nifty_3min.csv"
    f15 = DATA / "nifty_15min.csv"
    fv  = DATA / "india_vix_daily.csv"

    missing = [f for f in [f3, f15, fv] if not f.exists()]
    if missing:
        print("Missing data files:")
        for f in missing:
            print(f"  {f}")
        print("\nRun:  python data/fetch_po3_data.py  to download from Zerodha")
        sys.exit(1)

    print("Loading data...")
    df_3m  = pd.read_csv(f3,  index_col=0, parse_dates=True)
    df_15m = pd.read_csv(f15, index_col=0, parse_dates=True)
    vix_df = pd.read_csv(fv,  index_col=0, parse_dates=True)

    print(f"3-min bars  : {len(df_3m):,}  ({df_3m.index[0].date()} → {df_3m.index[-1].date()})")
    print(f"15-min bars : {len(df_15m):,}")
    print(f"VIX days    : {len(vix_df):,}")

    bt = PO3Backtest(capital=1_000_000)
    trades = bt.run(df_3m, df_15m, vix_df)
    metrics = compute_metrics(trades, 1_000_000)
    bt.print_summary(metrics)

    # Save trade log
    if trades:
        out = DATA.parent / "reports" / "po3_trades.csv"
        out.parent.mkdir(exist_ok=True)
        pd.DataFrame(trades).to_csv(out, index=False)
        print(f"Trade log saved: {out}")
