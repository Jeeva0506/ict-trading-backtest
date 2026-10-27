"""
Options backtest engine for VRDOB strategy.

Simulates daily option prices via Black-Scholes.
Tracks open positions and closes them on SL / TP / time-stop.
Deducts Zerodha costs from each round-trip.

Key anti-lookahead invariants:
  - Signals from strategies/vrdob_strategy.py already shift by 1
  - Position entry uses current-bar Open (signal from prior close)
  - SL/TP checked using intra-day proxied option prices:
      call high  → BS(day_high_spot, ...)
      call low   → BS(day_low_spot, ...)
    This only requires same-bar OHLC — not future data
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import numpy as np
import pandas as pd
from typing import Optional

from backtest.option_pricer import bs_price, atm_strike
from costs.zerodha import round_trip_cost
from strategies.vrdob_strategy import DEFAULT_PARAMS


class OptionsBacktest:
    """
    Event-driven daily backtest for a single instrument (Nifty 50).

    Parameters
    ----------
    capital      : starting capital (₹)
    params       : strategy params dict (merged with DEFAULT_PARAMS)
    """

    def __init__(self, capital: float = 1_000_000, params: dict = None):
        self.capital  = capital
        self.p        = {**DEFAULT_PARAMS, **(params or {})}
        self.trades   = []          # list of closed trade dicts
        self._pos     = None        # currently open position

    # ─────────────────────────────────────────────────────────────────────────
    def run(self, nifty: pd.DataFrame, vix: pd.DataFrame,
            signals: pd.DataFrame,
            start_date: str = None, end_date: str = None) -> list:
        """
        Run backtest over date range.

        Parameters
        ----------
        nifty    : OHLCV DataFrame (Nifty spot), DatetimeIndex
        vix      : 'Close' column = India VIX, DatetimeIndex
        signals  : output of vrdob_strategy.generate_signals()
        start/end: ISO date strings to slice the data

        Returns list of trade dicts.
        """
        vix_aligned = vix["Close"].reindex(nifty.index, method="ffill")

        # Align all to common index
        df = nifty.copy()
        df["vix"]    = vix_aligned
        df["signal"] = signals["signal"]

        if start_date:
            df = df[df.index >= pd.Timestamp(start_date)]
        if end_date:
            df = df[df.index <= pd.Timestamp(end_date)]

        # Warm-up: need at least ema_slow + ivr_lookback bars before trading
        # — signals already handle this via NaN propagation, so we can iterate
        for date, row in df.iterrows():
            self._process_bar(date, row)

        # Close any open position at end of data (time exit)
        if self._pos:
            last_row = df.iloc[-1]
            self._close_position(df.index[-1], last_row["Close"],
                                 last_row["vix"], "TIME")

        return self.trades

    # ─────────────────────────────────────────────────────────────────────────
    def _process_bar(self, date: pd.Timestamp, row: pd.Series):
        vix_today = row["vix"]
        if pd.isna(vix_today) or vix_today <= 0:
            return

        # ── 1. Manage open position first ────────────────────────────────────
        if self._pos is not None:
            self._manage_position(date, row)

        # ── 2. Check entry signal (only if no open position) ─────────────────
        if self._pos is None and row["signal"] in ("call", "put"):
            self._open_position(date, row)

    # ─────────────────────────────────────────────────────────────────────────
    def _option_price(self, spot: float, strike: float, dte: int,
                      vix: float, option_type: str) -> float:
        """Black-Scholes price for one day in the holding window."""
        T     = max(dte, 0.5) / 365.0   # floor at 0.5 day to avoid zero
        sigma = vix / 100.0
        return bs_price(spot, strike, T, self.p["rfr"], sigma, option_type)

    # ─────────────────────────────────────────────────────────────────────────
    def _open_position(self, date: pd.Timestamp, row: pd.Series):
        spot        = row["Open"]
        vix         = row["vix"]
        option_type = row["signal"]         # 'call' or 'put'
        dte         = self.p["entry_dte"]   # DTE at entry

        if pd.isna(spot) or spot <= 0 or pd.isna(vix) or vix <= 0:
            return

        strike  = atm_strike(spot, step=self.p["strike_step"])
        premium = self._option_price(spot, strike, dte, vix, option_type)

        if premium < 1.0:  # extremely cheap → skip (likely mis-priced day)
            return

        # ── Position sizing ───────────────────────────────────────────────────
        # Max premium budget so that if SL fires, loss = risk_per_trade
        max_budget = self.p["risk_per_trade"] / self.p["sl_pct"]
        lots       = int(max_budget / (premium * self.p["lot_size"]))
        lots       = max(1, min(lots, self.p["max_lots"]))
        qty        = lots * self.p["lot_size"]

        sl_price   = premium * (1 - self.p["sl_pct"])   # 40% below entry
        tp_price   = premium * (1 + self.p["tp_pct"])   # 80% above entry

        self._pos = {
            "entry_date"  : date,
            "option_type" : option_type,
            "strike"      : strike,
            "entry_spot"  : spot,
            "entry_prem"  : premium,
            "sl_price"    : sl_price,
            "tp_price"    : tp_price,
            "lots"        : lots,
            "qty"         : qty,
            "days_held"   : 0,
            "entry_dte"   : dte,
        }

    # ─────────────────────────────────────────────────────────────────────────
    def _manage_position(self, date: pd.Timestamp, row: pd.Series):
        pos          = self._pos
        pos["days_held"] += 1
        remaining_dte = max(pos["entry_dte"] - pos["days_held"], 0.5)
        vix           = row["vix"]
        option_type   = pos["option_type"]
        strike        = pos["strike"]

        if pd.isna(vix) or vix <= 0:
            return

        # ── Compute intra-day option price range ─────────────────────────────
        # For calls: spot_high → opt_high; spot_low → opt_low
        # For puts:  spot_low  → opt_high; spot_high → opt_low
        if option_type == "call":
            opt_high = self._option_price(row["High"], strike, remaining_dte, vix, "call")
            opt_low  = self._option_price(row["Low"],  strike, remaining_dte, vix, "call")
        else:
            opt_high = self._option_price(row["Low"],  strike, remaining_dte, vix, "put")
            opt_low  = self._option_price(row["High"], strike, remaining_dte, vix, "put")

        opt_close = self._option_price(row["Close"], strike, remaining_dte, vix, option_type)

        # ── Check TP first (more favourable) ─────────────────────────────────
        if opt_high >= pos["tp_price"]:
            self._close_position(date, pos["tp_price"], vix, "TP")
            return

        # ── Check SL ─────────────────────────────────────────────────────────
        if opt_low <= pos["sl_price"]:
            self._close_position(date, pos["sl_price"], vix, "SL")
            return

        # ── Time stop ────────────────────────────────────────────────────────
        if pos["days_held"] >= self.p["max_hold_days"]:
            self._close_position(date, opt_close, vix, "TIME")

    # ─────────────────────────────────────────────────────────────────────────
    def _close_position(self, date: pd.Timestamp, exit_premium: float,
                        vix: float, exit_type: str):
        pos  = self._pos
        qty  = pos["qty"]
        buy  = pos["entry_prem"]
        sell = max(exit_premium, 0.05)

        gross_pnl  = (sell - buy) * qty
        cost       = round_trip_cost(buy, sell, qty)
        net_pnl    = gross_pnl - cost

        self.trades.append({
            "entry_date"  : pos["entry_date"].strftime("%Y-%m-%d"),
            "exit_date"   : date.strftime("%Y-%m-%d"),
            "direction"   : pos["option_type"],
            "strike"      : pos["strike"],
            "entry_spot"  : round(pos["entry_spot"], 2),
            "entry_prem"  : round(buy, 2),
            "exit_prem"   : round(sell, 2),
            "lots"        : pos["lots"],
            "qty"         : qty,
            "days_held"   : pos["days_held"],
            "exit_type"   : exit_type,
            "gross_pnl"   : round(gross_pnl, 2),
            "cost"        : round(cost, 2),
            "pnl_net"     : round(net_pnl, 2),
        })

        self._pos = None
