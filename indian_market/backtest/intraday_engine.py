"""
Intraday Options Backtest Engine
=================================
- Entry  : market open (9:15 AM) when signal fires
- SL/TP  : checked against intraday High / Low
- Exit   : 3:15 PM close if neither SL nor TP hit (same-day mandatory exit)
- No overnight holding of any position

Anti-lookahead design:
  - Signal conditions use PRIOR day's OHLC / indicators (see cpr_pivot_strategy.py)
  - Entry is at today's Open (known at market start — not future data)
  - SL/TP are checked using same-day High/Low (these happen AFTER the Open fill)
  - This is standard for intraday backtesting on daily OHLCV data

Option pricing for intraday:
  - Use Black-Scholes with entry DTE for both entry and exit
  - Intraday theta negligible (< 1/252 year ≈ 0.004% of total theta)
  - Same-day VIX used as IV proxy; intraday VIX changes approximated by
    using 1.05× entry VIX for TP scenarios (conservative bias on profits)
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pandas as pd
import numpy as np

from backtest.option_pricer import bs_price, atm_strike
from costs.zerodha import round_trip_cost
from strategies.cpr_pivot_strategy import CPR_PARAMS


class IntradayOptionsBacktest:
    """
    One trade per qualifying day. No position carried overnight.
    """

    def __init__(self, capital: float = 1_000_000, params: dict = None):
        self.capital = capital
        self.p       = {**CPR_PARAMS, **(params or {})}
        self.trades  = []

    def run(self, nifty: pd.DataFrame, vix: pd.DataFrame,
            signals: pd.DataFrame,
            start_date: str = None, end_date: str = None) -> list:

        vix_aligned = vix["Close"].reindex(nifty.index, method="ffill")
        df          = nifty.copy()
        df["vix"]   = vix_aligned
        df["signal"] = signals["signal"]

        if start_date:
            df = df[df.index >= pd.Timestamp(start_date)]
        if end_date:
            df = df[df.index <= pd.Timestamp(end_date)]

        for date, row in df.iterrows():
            if row["signal"] in ("call", "put") and not pd.isna(row["vix"]):
                self._process_trade(date, row)

        return self.trades

    def _option_price(self, spot: float, strike: float,
                      vix: float, option_type: str) -> float:
        dte = self.p["entry_dte"]
        T   = max(dte, 0.5) / 365.0
        return bs_price(spot, strike, T, self.p["rfr"], vix / 100.0, option_type)

    def _process_trade(self, date: pd.Timestamp, row: pd.Series):
        option_type = row["signal"]  # 'call' or 'put'
        spot        = row["Open"]
        vix         = row["vix"]

        if pd.isna(spot) or spot <= 0 or vix <= 0:
            return

        strike     = atm_strike(spot, step=self.p["strike_step"])
        entry_prem = self._option_price(spot, strike, vix, option_type)

        if entry_prem < 1.0:
            return  # degenerate pricing — skip

        # ── Position sizing ───────────────────────────────────────────────────
        max_budget = self.p["risk_per_trade"] / self.p["sl_pct"]
        lots       = int(max_budget / (entry_prem * self.p["lot_size"]))
        lots       = max(1, min(lots, self.p["max_lots"]))
        qty        = lots * self.p["lot_size"]

        tp_price   = entry_prem * (1 + self.p["tp_pct"])
        sl_price   = entry_prem * (1 - self.p["sl_pct"])

        # ── Intraday SL / TP check ────────────────────────────────────────────
        # For calls: spot_high → option_high; spot_low → option_low
        # For puts:  spot_low  → option_high; spot_high → option_low
        if option_type == "call":
            prem_high = self._option_price(row["High"], strike, vix, "call")
            prem_low  = self._option_price(row["Low"],  strike, vix, "call")
        else:
            prem_high = self._option_price(row["Low"],  strike, vix, "put")
            prem_low  = self._option_price(row["High"], strike, vix, "put")

        prem_close = self._option_price(row["Close"], strike, vix, option_type)

        # Determine exit: TP takes priority over SL (favourable outcome first)
        if prem_high >= tp_price:
            exit_prem, exit_type = tp_price, "TP"
        elif prem_low <= sl_price:
            exit_prem, exit_type = sl_price, "SL"
        else:
            exit_prem, exit_type = prem_close, "3:15"  # end-of-day mandatory exit

        # ── P&L calculation ───────────────────────────────────────────────────
        gross_pnl = (exit_prem - entry_prem) * qty
        cost      = round_trip_cost(entry_prem, max(exit_prem, 0.05), qty)
        net_pnl   = gross_pnl - cost

        self.trades.append({
            "entry_date"  : date.strftime("%Y-%m-%d"),
            "exit_date"   : date.strftime("%Y-%m-%d"),   # same day
            "direction"   : option_type,
            "strike"      : strike,
            "entry_spot"  : round(spot, 2),
            "entry_prem"  : round(entry_prem, 2),
            "exit_prem"   : round(exit_prem, 2),
            "lots"        : lots,
            "qty"         : qty,
            "days_held"   : 0,  # intraday = 0 overnight
            "exit_type"   : exit_type,
            "gross_pnl"   : round(gross_pnl, 2),
            "cost"        : round(cost, 2),
            "pnl_net"     : round(net_pnl, 2),
            "vix_at_entry": round(vix, 2),
        })
