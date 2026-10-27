"""
Straddle backtest engine.
Tracks combined (call + put) straddle P&L using daily OHLCV + VIX.

SL/TP logic:
  - TP: if intraday best straddle value >= TP_value → exit at TP_value
  - SL: if end-of-day straddle value <= SL_value → exit at SL (avoid fake intraday SLs)
  - TIME: if days_held >= max_hold_days → exit at close straddle value

Straddle intraday range:
  Straddle is U-shaped in spot price. Maximum during day happens when spot is
  FURTHEST from strike. We approximate by evaluating at daily H and daily L:
    best_today  = max(straddle(H), straddle(L))   → for TP check
    close_today = straddle(Close)                  → for SL check (conservative)
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pandas as pd
import numpy as np

from backtest.option_pricer import bs_price, atm_strike
from costs.zerodha import round_trip_cost
from strategies.straddle_strategy import STRADDLE_PARAMS


class StraddleBacktest:

    def __init__(self, capital: float = 1_000_000, params: dict = None):
        self.capital = capital
        self.p       = {**STRADDLE_PARAMS, **(params or {})}
        self.trades  = []
        self._pos    = None

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
            self._process_bar(date, row)

        if self._pos:
            last = df.iloc[-1]
            sv   = self._straddle_value(last["Close"], self._pos["strike"],
                                        0.5, last["vix"])
            self._close(df.index[-1], sv, "TIME")

        return self.trades

    def _straddle_value(self, spot: float, strike: float, dte: float,
                        vix: float) -> float:
        T     = max(dte, 0.5) / 365.0
        sigma = vix / 100.0
        call  = bs_price(spot, strike, T, self.p["rfr"], sigma, "call")
        put   = bs_price(spot, strike, T, self.p["rfr"], sigma, "put")
        return call + put

    def _process_bar(self, date: pd.Timestamp, row: pd.Series):
        vix = row["vix"]
        if pd.isna(vix) or vix <= 0:
            return

        if self._pos is not None:
            self._manage(date, row)

        if self._pos is None and row["signal"] == "straddle":
            self._open(date, row)

    def _open(self, date: pd.Timestamp, row: pd.Series):
        spot    = row["Open"]
        vix     = row["vix"]
        dte     = self.p["entry_dte"]
        strike  = atm_strike(spot, step=self.p["strike_step"])
        sv_entry = self._straddle_value(spot, strike, dte, vix)

        if sv_entry < 2.0:
            return

        max_budget = self.p["risk_per_trade"] / self.p["sl_pct"]
        lots       = int(max_budget / (sv_entry * self.p["lot_size"]))
        lots       = max(1, min(lots, self.p["max_lots"]))
        qty        = lots * self.p["lot_size"]

        self._pos = {
            "entry_date"   : date,
            "strike"       : strike,
            "entry_spot"   : spot,
            "entry_sv"     : sv_entry,    # combined straddle value at entry
            "entry_call_p" : sv_entry / 2, # approximate (stored for cost calc)
            "tp_value"     : sv_entry * (1 + self.p["tp_pct"]),
            "sl_value"     : sv_entry * (1 - self.p["sl_pct"]),
            "lots"         : lots,
            "qty"          : qty,
            "days_held"    : 0,
            "entry_dte"    : dte,
        }

    def _manage(self, date: pd.Timestamp, row: pd.Series):
        pos = self._pos
        pos["days_held"] += 1
        dte_rem = max(pos["entry_dte"] - pos["days_held"], 0.5)
        vix     = row["vix"]

        # Best straddle value today (TP check): evaluate at day High and Low
        sv_high = self._straddle_value(row["High"], pos["strike"], dte_rem, vix)
        sv_low  = self._straddle_value(row["Low"],  pos["strike"], dte_rem, vix)
        sv_best = max(sv_high, sv_low)

        # Close straddle value (SL check): evaluated at Close (conservative)
        sv_close = self._straddle_value(row["Close"], pos["strike"], dte_rem, vix)

        if sv_best >= pos["tp_value"]:
            self._close(date, pos["tp_value"], "TP")
            return

        if sv_close <= pos["sl_value"]:
            self._close(date, pos["sl_value"], "SL")
            return

        if pos["days_held"] >= self.p["max_hold_days"]:
            self._close(date, sv_close, "TIME")

    def _close(self, date: pd.Timestamp, exit_sv: float, exit_type: str):
        pos     = self._pos
        qty     = pos["qty"]
        entry_sv = pos["entry_sv"]
        exit_sv  = max(exit_sv, 0.05)

        # Cost: round-trip for CALL leg + round-trip for PUT leg
        # approximate: call≈put≈sv/2 at entry; at exit proportionally
        entry_leg = entry_sv / 2
        exit_leg  = exit_sv  / 2
        cost_call = round_trip_cost(entry_leg, exit_leg, qty)
        cost_put  = round_trip_cost(entry_leg, exit_leg, qty)
        total_cost = cost_call + cost_put

        gross_pnl = (exit_sv - entry_sv) * qty
        net_pnl   = gross_pnl - total_cost

        self.trades.append({
            "entry_date" : pos["entry_date"].strftime("%Y-%m-%d"),
            "exit_date"  : date.strftime("%Y-%m-%d"),
            "direction"  : "straddle",
            "strike"     : pos["strike"],
            "entry_spot" : round(pos["entry_spot"], 2),
            "entry_prem" : round(entry_sv, 2),
            "exit_prem"  : round(exit_sv, 2),
            "lots"       : pos["lots"],
            "qty"        : qty,
            "days_held"  : pos["days_held"],
            "exit_type"  : exit_type,
            "gross_pnl"  : round(gross_pnl, 2),
            "cost"       : round(total_cost, 2),
            "pnl_net"    : round(net_pnl, 2),
        })
        self._pos = None
