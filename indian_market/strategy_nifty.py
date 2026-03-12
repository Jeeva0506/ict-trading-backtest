"""
ORB Breakout Strategy — Optimized for NIFTY 50 Futures

Simplified from StrategyICTClean to achieve 100+ trades/year on a single instrument.

Key differences from StrategyICTClean:
1. NO volatility contraction filter (NIFTY's gap opens make it too restrictive)
2. Bidirectional breakouts: trades first valid breakout in EITHER direction
   (not restricted to ORB bias direction)
3. Direct breakout entry on next bar open (no pullback retest requirement)
4. Simpler displacement filter (configurable, can be disabled)
5. Same proven exit logic: BE at 1R, wide trail, TP at configurable R-multiple
6. Same force close 15 min before session end
"""

from __future__ import annotations
from dataclasses import dataclass
from typing import Any, List, Optional, Dict
import pandas as pd


@dataclass
class TradeSignal:
    direction: str                 # "bullish" or "bearish"
    entry_time: Any
    entry_price: float
    stop_loss: float
    tp1: float
    tp2: float
    tp3: float
    risk_points: float

    exit_time: Any = None
    exit_price: float = None
    exit_reason: str = None        # TP, SL_BE, SL_HIT, SESSION_CLOSE
    pnl_points: float = 0.0
    signal_price: float = None
    breakout_time: Any = None


class StrategyORBNifty:
    """
    ORB Breakout for NIFTY 50 — high-frequency variant.

    Entry: First M1 candle that closes beyond ORB high/low with sufficient body%.
    Exit: BE + trailing stop, single TP target.
    Filter: Optional HTF EMA, optional displacement, body% filter.
    """

    def __init__(
        self,
        sl_buffer: float = 1.0,
        min_sl_points: float = 5.0,
        max_sl_points: float = 200.0,
        # --- ORB settings ---
        orb_minutes: int = 15,
        entry_hours: int = 3,
        breakout_margin: float = 0.05,   # fraction of ORB range
        # --- filters ---
        body_min: float = 0.55,          # minimum body% of breakout candle
        use_disp_filter: bool = False,   # displacement filter on/off
        disp_mult: float = 1.0,          # displacement multiplier (if enabled)
        use_orb_bias: bool = False,      # require breakout to match ORB bias
        # --- exits ---
        tp_r: float = 2.5,              # TP in R-multiples
        be_trigger_r: float = 1.0,      # move SL to BE at this R
        trail_buffer_r: float = 1.0,    # trail buffer after BE
        # --- ORB range filter ---
        min_orb_range: float = 10.0,    # minimum ORB range in points
        max_orb_range: float = 200.0,   # maximum ORB range in points
        **_kwargs,
    ):
        self.sl_buffer = float(sl_buffer)
        self.min_sl = float(min_sl_points)
        self.max_sl = float(max_sl_points)
        self.orb_minutes = int(orb_minutes)
        self.entry_hours = int(entry_hours)
        self.breakout_margin = float(breakout_margin)
        self.body_min = float(body_min)
        self.use_disp_filter = bool(use_disp_filter)
        self.disp_mult = float(disp_mult)
        self.use_orb_bias = bool(use_orb_bias)
        self.tp_r = float(tp_r)
        self.be_trigger_r = float(be_trigger_r)
        self.trail_buffer_r = float(trail_buffer_r)
        self.min_orb_range = float(min_orb_range)
        self.max_orb_range = float(max_orb_range)

    @staticmethod
    def _body_pct(o, h, l, c) -> float:
        rng = h - l
        if rng <= 0:
            return 0.0
        return abs(c - o) / rng

    def _simulate_trade(
        self,
        df: pd.DataFrame,
        start_idx: int,
        direction: str,
        entry: float,
        sl_init: float,
        R: float,
        cutoff_time: pd.Timestamp,
    ) -> TradeSignal:
        """BE + wide trailing stop + single TP."""
        tp_price = float(entry + R * self.tp_r if direction == "bullish"
                         else entry - R * self.tp_r)
        be_price = float(entry + R * self.be_trigger_r if direction == "bullish"
                         else entry - R * self.be_trigger_r)

        t = TradeSignal(
            direction=direction,
            entry_time=df.index[start_idx],
            entry_price=float(entry),
            stop_loss=float(sl_init),
            tp1=be_price,
            tp2=tp_price,
            tp3=tp_price,
            risk_points=float(R),
        )

        hit_be = False
        max_favorable = 0.0

        for i in range(start_idx, len(df)):
            c = df.iloc[i]
            ts = df.index[i]

            # Force close near session end
            if ts >= cutoff_time:
                t.exit_time = ts
                t.exit_price = float(c.close)
                t.pnl_points = (float(c.close) - t.entry_price if direction == "bullish"
                                else t.entry_price - float(c.close))
                t.exit_reason = "SESSION_CLOSE"
                return t

            hi, lo = float(c.high), float(c.low)

            # SL check
            if direction == "bullish" and lo <= t.stop_loss:
                t.exit_time = ts
                t.exit_price = float(t.stop_loss)
                t.pnl_points = t.stop_loss - t.entry_price
                t.exit_reason = "SL_BE" if hit_be else "SL_HIT"
                return t
            if direction == "bearish" and hi >= t.stop_loss:
                t.exit_time = ts
                t.exit_price = float(t.stop_loss)
                t.pnl_points = t.entry_price - t.stop_loss
                t.exit_reason = "SL_BE" if hit_be else "SL_HIT"
                return t

            # TP check
            if direction == "bullish" and hi >= tp_price:
                t.exit_time = ts
                t.exit_price = tp_price
                t.pnl_points = tp_price - t.entry_price
                t.exit_reason = "TP"
                return t
            if direction == "bearish" and lo <= tp_price:
                t.exit_time = ts
                t.exit_price = tp_price
                t.pnl_points = t.entry_price - tp_price
                t.exit_reason = "TP"
                return t

            # BE trigger
            if not hit_be:
                if direction == "bullish" and hi >= be_price:
                    hit_be = True
                    t.stop_loss = t.entry_price
                elif direction == "bearish" and lo <= be_price:
                    hit_be = True
                    t.stop_loss = t.entry_price

            # Wide trailing after BE
            if hit_be:
                current_r = ((hi - entry) / R if direction == "bullish"
                             else (entry - lo) / R)
                if current_r > max_favorable:
                    max_favorable = current_r
                trail_r = max(0.0, max_favorable - self.trail_buffer_r)
                if direction == "bullish":
                    new_sl = entry + trail_r * R
                    if new_sl > t.stop_loss:
                        t.stop_loss = new_sl
                else:
                    new_sl = entry - trail_r * R
                    if new_sl < t.stop_loss:
                        t.stop_loss = new_sl

        # fallback
        last = df.iloc[-1]
        t.exit_time = df.index[-1]
        t.exit_price = float(last.close)
        t.pnl_points = (float(last.close) - t.entry_price if direction == "bullish"
                        else t.entry_price - float(last.close))
        t.exit_reason = "SESSION_CLOSE"
        return t

    def generate_signals(
        self,
        df: pd.DataFrame,
        htf_trend: Optional[str] = None,
    ) -> List[TradeSignal]:
        """
        Generate ORB breakout signals for one session day.
        df: M1 OHLC sliced to session (09:15 - 15:30).
        htf_trend: "bullish"/"bearish" or None (ignored if not used).
        """
        out: List[TradeSignal] = []

        if df is None or len(df) < 30:
            return out

        session_start = df.index[0]
        orb_end = session_start + pd.Timedelta(minutes=self.orb_minutes)
        entry_cutoff = session_start + pd.Timedelta(hours=self.entry_hours)
        cutoff_close = df.index[-1] - pd.Timedelta(minutes=15)

        # Build ORB
        orb = df[(df.index >= session_start) & (df.index < orb_end)]
        if len(orb) < max(3, self.orb_minutes - 2):
            return out

        orbH = float(orb["high"].max())
        orbL = float(orb["low"].min())
        orb_range = orbH - orbL

        if orb_range < self.min_orb_range or orb_range > self.max_orb_range:
            return out

        # ORB bias (optional filter)
        orb_open = float(orb.iloc[0]["open"])
        orb_close = float(orb.iloc[-1]["close"])
        orb_bias = "bullish" if orb_close > orb_open else "bearish"

        # HTF trend filter (optional)
        if htf_trend is not None:
            allowed_dir = htf_trend  # only trade in HTF direction
        elif self.use_orb_bias:
            allowed_dir = orb_bias   # only trade in ORB bias direction
        else:
            allowed_dir = None       # trade either direction

        # Scan for breakout
        scan = df[(df.index >= orb_end) & (df.index < entry_cutoff)]
        if len(scan) < 5:
            return out

        margin = self.breakout_margin * orb_range

        # Precompute rolling avg range for optional displacement filter
        lookback = 10
        scan_copy = scan.copy()
        scan_copy["_range"] = scan_copy["high"] - scan_copy["low"]
        scan_copy["_avg_range"] = scan_copy["_range"].rolling(lookback, min_periods=3).mean()

        for idx in range(max(3, lookback), len(scan_copy)):
            bar = scan_copy.iloc[idx]
            ts = scan_copy.index[idx]

            if ts >= cutoff_close:
                break

            o = float(bar["open"])
            h = float(bar["high"])
            l = float(bar["low"])
            c = float(bar["close"])
            candle_range = h - l

            # Direction: close beyond ORB level + margin
            direction = None
            if c > orbH + margin:
                direction = "bullish"
            elif c < orbL - margin:
                direction = "bearish"
            else:
                continue

            # Filter: allowed direction
            if allowed_dir is not None and direction != allowed_dir:
                continue

            # Filter: body %
            body_pct = self._body_pct(o, h, l, c)
            if body_pct < self.body_min:
                continue

            # Filter: displacement (optional)
            if self.use_disp_filter:
                avg_r = float(scan_copy.iloc[idx]["_avg_range"])
                if avg_r > 0 and candle_range < self.disp_mult * avg_r:
                    continue

            # Entry: next bar open
            df_pos = df.index.get_indexer([ts])[0]
            next_idx = df_pos + 1
            if next_idx >= len(df):
                continue

            entry_price = float(df.iloc[next_idx]["open"])

            # SL: below/above breakout candle + buffer
            if direction == "bullish":
                sl = l - self.sl_buffer
                R = entry_price - sl
            else:
                sl = h + self.sl_buffer
                R = sl - entry_price

            if R <= 0 or R < self.min_sl or R > self.max_sl:
                continue

            trade = self._simulate_trade(
                df=df,
                start_idx=next_idx + 1,
                direction=direction,
                entry=entry_price,
                sl_init=sl,
                R=R,
                cutoff_time=cutoff_close,
            )
            trade.signal_price = c
            trade.breakout_time = ts
            out.append(trade)
            break  # 1 trade per day

        return out
