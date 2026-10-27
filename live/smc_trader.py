"""STELLAR-2 SMC trader -- a SUBCLASS of LiveTrader. Shared code untouched.

WHY A SUBCLASS AND NOT AN EDIT
    mt5_connector.py is imported by both running bots (STELLAR-3, STELLAR-15K).
    Editing it to add a second strategy would put two live accounts at risk for
    no benefit. This subclasses LiveTrader and overrides exactly two things:

        _generate_signal()  -> SMC (CHoCH + order block) instead of ORB
        SMC_SL_GUARD        -> SMC's own min/max SL bounds

    Everything else is INHERITED unchanged: drawdown protection, news filter,
    edge guard, SL protection, trade journal, Telegram, position management,
    the drift guard, spread guard, watchdog supervision.

WHY THE STRATEGY CODE IS IMPORTED, NOT REWRITTEN
    It calls orb_v2/strategy_smc.generate_signals() -- the exact function that
    was audited (causality gate 478 trades, directional inversion 17/17,
    random-direction 14/17, OOS t=+4.13). Reimplementing it for live is the
    most likely way a validated strategy silently becomes a losing one.
    Calling it repeatedly on a growing session is safe because the causality
    gate proved truncation invariance.

SL GUARDS (x28, sized from SMC's own R distribution at the 97th percentile)
    The live caps were tuned for the ORB's 2-15pt stops. SMC's are 21-159pt,
    so the shared caps rejected ~49% of signals and DESTROYED the edge on
    NDX100 (t 3.41 -> 0.68) and US30 (3.63 -> 1.25). With SMC's own caps all
    five symbols retain ~96% of signals and every t-stat rises.
    These are NOT a risk increase: position sizing fixes dollar risk, so a
    wider stop simply means a smaller lot.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta as _td, timezone as _tz
from pathlib import Path
from typing import Optional

import pandas as pd
import pytz

import MetaTrader5 as mt5

from mt5_connector import LiveTrader, logger, get_index_config

# the audited strategy, imported rather than reimplemented
_ORB_V2 = Path(__file__).resolve().parent.parent.parent / "orb_v2"
if str(_ORB_V2) not in sys.path:
    sys.path.insert(0, str(_ORB_V2))
import strategy_smc                                    # noqa: E402
from strategy_smc import ParamsSMC                     # noqa: E402

# --- SMC-specific SL bounds (x28: p2 floor, p97 cap, rounded) -------------
SMC_SL_GUARD = {
    "GER30":  (11.0, 250.0),
    "FRA40":  (5.0, 90.0),
    "SPX500": (3.0, 90.0),
    "NDX100": (16.0, 400.0),
    "US30":   (27.0, 610.0),
}
SMC_BAR_MINUTES = 5          # audited at M5
# Minimum M5 bars before the strategy is consulted. swing_n=5 needs 11 bars to
# confirm a pivot; a CHoCH + order block + retrace needs ~25. See the note in
# _generate_signal -- this was 60 (five hours) and blinded live to most of the
# session.
SMC_MIN_BARS = 25

SMC_PARAMS = dict(
    swing_n=5, shift_mode="choch", require_sweep=False, sweep_levels="pdhpdl",
    sweep_lookback=60, poi="ob", ote_lo=0.618, ote_hi=0.786, poi_max_wait=60,
    confirm_engulf=False, htf_align=False, session_start_hour=0,
    session_end_hour=0, sl_buffer=0.0, target_mode="rr", tp_r=3.0,
    be_trigger_r=1.0, trail_buffer_r=0.5, force_close_minutes_before_end=15,
    max_signals_per_session=2)


class SMCTrader(LiveTrader):
    """LiveTrader with the SMC entry. All protections inherited."""

    STRATEGY_NAME = "SMC_CHoCH_OB"

    def __init__(self):
        super().__init__()
        self._smc_prev_day = {}      # symbol -> date of the PDH/PDL in use
        logger.info(f"SMCTrader active | symbols={list(SMC_SL_GUARD)} | "
                    f"M{SMC_BAR_MINUTES} | SL guards={SMC_SL_GUARD}")

    def initialize(self):
        """dd_protection is built inside the parent's initialize(), not
        __init__ -- applying the risk cap any earlier silently does nothing."""
        ok = super().initialize()
        if ok:
            try:
                from orb15_trader import _apply_risk_cap
                _apply_risk_cap(self)
            except Exception as e:
                logger.warning(f"risk cap not applied: {e}")
        return ok

    # -- session window: same DST-proof logic the parent uses ------------
    def _smc_session_window(self, symbol, m1, config):
        """Market-local open/close -> broker time, with the parent's static
        fallback. Mirrors LiveTrader._generate_signal so SMC and ORB agree on
        what 'the session' is; verified by tools/verify_smc_session.py."""
        try:
            broker_offset = self._get_broker_utc_offset()
            broker_tz = _tz(_td(hours=broker_offset))
            mt5_now = datetime.now(pytz.UTC).astimezone(broker_tz).replace(tzinfo=None)
            today = mt5_now.date()
        except Exception:
            broker_offset, today = None, datetime.now().date()

        if broker_offset is not None and config.get("timezone"):
            try:
                ltz = pytz.timezone(config["timezone"])
                lo = ltz.localize(datetime(today.year, today.month, today.day,
                                           config["market_open_hour"],
                                           config["market_open_minute"]))
                lc = ltz.localize(datetime(today.year, today.month, today.day,
                                           config["market_close_hour"],
                                           config["market_close_minute"]))
                btz = _tz(_td(hours=broker_offset))
                return (pd.Timestamp(lo.astimezone(btz).replace(tzinfo=None)),
                        pd.Timestamp(lc.astimezone(btz).replace(tzinfo=None)))
            except Exception as e:
                logger.warning(f"{symbol}: SMC dynamic session calc failed ({e})")

        s = pd.Timestamp(year=today.year, month=today.month, day=today.day,
                         hour=config["mt5_open_hour"], minute=config["mt5_open_minute"])
        e = pd.Timestamp(year=today.year, month=today.month, day=today.day,
                         hour=config["mt5_close_hour"], minute=config["mt5_close_minute"])
        today_bars = m1[m1.index.date == today]
        if len(today_bars):
            first = today_bars.index[0]
            gap = (s - first).total_seconds()
            if 30 * 60 < gap <= 90 * 60:
                logger.info(f"{symbol}: SMC session snapped to first bar {first}")
                dur = e - s
                s = pd.Timestamp(first)
                e = s + dur
        return s, e

    # -- the override --------------------------------------------------
    def _generate_signal(self, symbol: str) -> Optional[dict]:
        if symbol not in SMC_SL_GUARD:
            return None
        config = get_index_config(symbol)
        if not config:
            return None

        # 800 M1 bars reaches back only ~13h -- not enough to guarantee a
        # PRIOR DATE is present for PDH/PDL once a session is hours old.
        m1 = self.mt5.get_ohlc_data(symbol, mt5.TIMEFRAME_M1, count=3000)
        if m1 is None or m1.empty:
            return None
        s_start, s_end = self._smc_session_window(symbol, m1, config)
        sess = m1[(m1.index >= s_start) & (m1.index <= s_end)]
        if len(sess) < SMC_MIN_BARS * SMC_BAR_MINUTES:
            return None

        df = sess.resample(f"{SMC_BAR_MINUTES}min", label="left", closed="left").agg(
            {"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
        # These thresholds were 60 M5 bars = 300 MINUTES, which meant the
        # strategy was not consulted until five hours into a session. With an
        # 8.5h session that hid the first ~59% of it, and the 1-bar staleness
        # check then discarded anything that had formed during the blackout --
        # so live could only ever take late-session setups (the single live
        # fill so far came at 20:00). The backtest has no such blindness: it
        # passes the whole session to generate_signals at once.
        #
        # 60 was never a requirement of the strategy, only of this wrapper.
        # swing_n=5 needs 11 bars to confirm one pivot, and a CHoCH plus an
        # order block plus a retrace needs roughly 25. Below that
        # generate_signals simply finds nothing -- it cannot invent a signal
        # from too little history. Evaluating on a partial session is causally
        # sound: the causality gate proved truncation invariance (478 trades).
        if len(df) < SMC_MIN_BARS:
            return None

        # PDH/PDL -- derived from DATA every cycle, never accumulated.
        # The backtest calls generate_signals ONCE per session and lets it
        # overwrite _PREV with that day's H/L for the NEXT session. Live we
        # call it every bar, so copying _PREV back would feed TODAY's running
        # high/low in as "yesterday" within minutes. Deriving it from the
        # previous session's bars each time is stateless and cannot drift.
        # Bars before the session start are NOT automatically "yesterday":
        # index CFDs trade overnight, so today's own pre-session bars sit in
        # this window too. max(date) then picked TODAY and PDH/PDL became the
        # overnight range -- observed live 2026-09-24, GER30 reporting
        # "PDH/PDL from 2026-09-24 H=25342.50 L=25281.70", a 60.8-point
        # "day". FRA40 was correct only because it had no qualifying
        # pre-session bars. Restrict the pool to strictly earlier DATES.
        #
        # This MUST NOT be able to stop trading. An 800-bar M1 window only
        # reaches back ~13h, so for a symbol whose session opened hours ago
        # every prior bar can be from today -- the strict filter would then
        # find nothing and block every signal, which is worse than the bug it
        # fixes. Hence the larger window above, and the fall back to the old
        # pool (with a warning) rather than returning None.
        older = m1[m1.index < s_start]
        strict = older[older.index.date < s_start.date()]
        if len(strict) >= 50:
            older = strict
        elif len(older) >= 50:
            logger.warning(f"{symbol}: no prior-date bars in window; PDH/PDL "
                           f"falling back to most recent available date")
        if len(older) < 50:
            return None
        lastday = max(older.index.date)
        pbars = older[older.index.date == lastday]
        if len(pbars) < 50:
            return None
        prev = (float(pbars["high"].max()), float(pbars["low"].min()))
        if self._smc_prev_day.get(symbol) != lastday:
            self._smc_prev_day[symbol] = lastday
            logger.info(f"{symbol}: SMC PDH/PDL from {lastday} "
                        f"H={prev[0]:.2f} L={prev[1]:.2f}")

        mn, mx = SMC_SL_GUARD[symbol]
        params = ParamsSMC(min_sl_points=mn, max_sl_points=mx, **SMC_PARAMS)

        strategy_smc._PREV.clear()
        strategy_smc._PREV[symbol] = prev
        trades = strategy_smc.generate_signals(symbol, df, self._get_htf_trend(symbol),
                                               0.0, params)
        if not trades:
            return None

        t = trades[-1]
        # Act ONLY on a signal whose entry bar is the one that just closed.
        #
        # This was 3 bars (15 min) and that tolerance caused the first live
        # trade's drift: entry_price is the OPEN of the zone-touch bar, so a
        # 15-minute-old bar was used as the reference while the fill happened
        # at current market -- FRA40 2026-09-23 planned 8145.38, filled
        # 8132.53, -12.85 pts = 0.238R, with only 244ms of actual latency.
        # A stale signal is not a bad signal, it is a bad PRICE; dropping it
        # costs a trade, chasing it cost a quarter of R.
        age = df.index[-1] - pd.Timestamp(t.entry_time)
        if age > pd.Timedelta(minutes=SMC_BAR_MINUTES):
            return None

        return {
            "direction": t.direction,
            "entry_price": float(t.entry_price),
            "stop_loss": float(t.stop_loss),
            "tp": float(t.tp_price),
            "risk_points": float(t.risk_points),
            "entry_time": t.entry_time,
            "htf_trend": self._get_htf_trend(symbol) or "",
            "orb_range": float(t.orb_range),
            "signal_time": str(t.signal_time),
            "signal_price": float(t.entry_price),
            "breakout_time": str(t.signal_time),
            "session_end": str(s_end),
        }


def main():
    for s in SMC_SL_GUARD:
        try:
            mt5.symbol_select(s, True)     # Market Watch state must not gate us
        except Exception:
            pass
    SMCTrader().run()


if __name__ == "__main__":
    main()
