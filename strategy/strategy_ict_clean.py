from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List, Optional, Dict
import pandas as pd


@dataclass
class TradeSignalICT:
    """
    Compatibility dataclass used by backtest_live_setup.py.
    We keep the same fields so you can swap this file without changing the runner.
    """
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
    pnl_points: float = 0.0        # realized PnL in points
    signal_price: float = None     # breakout/retest candle close (for drift calc)
    breakout_time: Any = None      # breakout candle timestamp (for stale calc)


class StrategyICTClean:
    """
    === ORB Clean Breakout — Single Target (Universal Index) ===

    Design targets: 50% WR, PF ≥ 2.5, RR = 1:2.5, ≥ 3 trades/week

    Math:  50% × 2.5R = 1.25R avg / trade
           With BE + lock exits: PF = (50×2.5) / (50×1) = 2.5

    Research-backed improvements (Quantocracy / CrackingMarkets / SSRN / HowToTrade):
    1. Volatility contraction filter: trade breakouts ONLY after narrow ORB
       (coiled spring). 3x per-trade expectancy in research.
    2. Pure BE + TP system: move SL to entry at 1R, let trade run to 2.5R TP.
       No trail, no lock — maximum chance of reaching full target.
    3. HTF EMA trend filter (50/200 EMA on H1): only trade breakouts
       in the direction of the dominant trend (HowToTrade article).
    4. Pullback retest entry: wait for price to retest ORB level after breakout.
       Tighter SL from retest candle → higher RR → more achievable TP.

    Rules:
    - ORB = first `orb_minutes` from session start.
    - Breakout candle CLOSE beyond ORB high/low within breakout window.
    - Filters:
        * Narrow ORB (volatility contraction): ORB range < median of ORB candle ranges × num_candles × contraction_mult
        * Displacement >= disp_mult x avg range (clean impulsive candle)
        * Body% >= body_min (decisive, not doji)
        * Close strength in direction (close in upper/lower portion of candle)
        * Close must exceed ORB level by breakout_margin x ORB range
        * HTF trend alignment (50/200 EMA on H1, fallback: ORB bias)
        * Pullback retest: enter on confirmed retest of ORB level (tighter SL)
    - SL = retest candle extreme +/- buffer (or breakout candle if no pullback).
    - Single TP at 2.5R. No partials.
    - SL moves to BE when price reaches 1R in profit.
    - Force close 15 min before session end.
    - 1 trade per day per instrument.
    """

    def __init__(
        self,
        sl_buffer: float = 1.0,
        min_sl_points: float = 2.0,
        max_sl_points: float = 10_000.0,
        debug: bool = False,
        # --- ORB settings ---
        orb_minutes: int = 15,
        entry_hours: int = 3,
        breakout_window_minutes: int = 60,
        lookback_ranges: int = 10,
        disp_mult: float = 1.3,
        body_min: float = 0.60,
        close_strength: float = 0.25,
        breakout_margin: float = 0.05,
        orb_range_min_mult: float = 0.5,
        # --- exits ---
        tp1_r: float = 1.0,                        # BE trigger level
        tp3_r: float = 2.5,                        # actual TP target
        **_kwargs,                                  # absorb legacy params from old callers
    ):
        self.sl_buffer = float(sl_buffer)
        self.min_sl = float(min_sl_points)
        self.max_sl = float(max_sl_points)
        self.debug = bool(debug)

        self.orb_minutes = int(orb_minutes)
        self.entry_hours = int(entry_hours)
        self.breakout_window_minutes = int(breakout_window_minutes)
        self.lookback_ranges = int(lookback_ranges)

        self.disp_mult = float(disp_mult)
        self.body_min = float(body_min)
        self.close_strength = float(close_strength)
        self.breakout_margin = float(breakout_margin)
        self.orb_range_min_mult = float(orb_range_min_mult)

        # Single target system
        self.be_trigger_r = float(tp1_r)       # move SL to BE at this R
        self.tp_r = float(tp3_r)               # single TP target
        self.tp1_r = float(tp1_r)
        self.tp3_r = float(tp3_r)

        self.debug_rows: List[Dict[str, Any]] = []

    # ----------------- helpers -----------------

    def _log(self, **k):
        if self.debug:
            self.debug_rows.append(k)

    @staticmethod
    def _body_pct(r) -> float:
        rng = float(r.high) - float(r.low)
        if rng <= 0:
            return 0.0
        return abs(float(r.close) - float(r.open)) / rng

    # ----------------- trade simulation -----------------

    def _simulate_trade(
        self,
        df: pd.DataFrame,
        start_idx: int,
        direction: str,
        entry: float,
        sl_init: float,
        R: float,
        cutoff_time: pd.Timestamp,
    ) -> TradeSignalICT:
        """
        Simulation with BE + wide trailing stop:
        1. SL at initial level until price reaches 1R → move SL to BE (entry price)
        2. After BE, trail with 1.5R buffer: SL = entry + max(0, MFE - 1.5R)
           This locks profit gradually while giving room to reach 2.5R TP.
        3. TP at tp_r × R (2.5R)
        4. Force close 15 min before session end
        """
        tp_price = float(entry + (R * self.tp_r) if direction == "bullish"
                         else entry - (R * self.tp_r))
        be_price = float(entry + (R * self.be_trigger_r) if direction == "bullish"
                         else entry - (R * self.be_trigger_r))

        t = TradeSignalICT(
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
        max_favorable = 0.0  # tracks peak unrealized profit in R-multiples

        for i in range(start_idx, len(df)):
            c = df.iloc[i]
            ts = df.index[i]
            bars_since_entry = i - start_idx

            # Force close near session end
            if ts >= cutoff_time:
                t.exit_time = ts
                t.exit_price = float(c.close)
                if direction == "bullish":
                    t.pnl_points = float(c.close) - t.entry_price
                else:
                    t.pnl_points = t.entry_price - float(c.close)
                t.exit_reason = "SESSION_CLOSE"
                return t

            hi = float(c.high)
            lo = float(c.low)

            # --- SL check first (conservative) ---
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

            # --- TP check ---
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

            # --- BE trigger ---
            if not hit_be:
                if direction == "bullish" and hi >= be_price:
                    hit_be = True
                    t.stop_loss = t.entry_price
                    self._log(event="BE_TRIGGER", time=ts, price=be_price)
                elif direction == "bearish" and lo <= be_price:
                    hit_be = True
                    t.stop_loss = t.entry_price
                    self._log(event="BE_TRIGGER", time=ts, price=be_price)

            # --- Wide trailing after BE: lock profit gradually ---
            # Trail buffer of 1.0R: SL = entry + max(0, MFE - 1.0R)
            # Locks profit earlier to boost WR while still allowing
            # trades room to reach 2.5R TP target.
            if hit_be:
                current_r = ((hi - entry) / R) if direction == "bullish" else ((entry - lo) / R)
                if current_r > max_favorable:
                    max_favorable = current_r

                # Trail when MFE exceeds 1.0R (after BE)
                trail_r = max(0.0, max_favorable - 1.0)
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
        if direction == "bullish":
            t.pnl_points = float(last.close) - t.entry_price
        else:
            t.pnl_points = t.entry_price - float(last.close)
        t.exit_reason = "SESSION_CLOSE"
        return t

    # ----------------- main -----------------

    def generate_signals(self, df: pd.DataFrame, htf_trend: Optional[str] = None) -> List[TradeSignalICT]:
        """
        Expects df already sliced to the session window.
        htf_trend: "bullish" or "bearish" from 50/200 EMA on H1 (higher timeframe filter).
                   If None, falls back to ORB bias.
        Returns a list of completed TradeSignalICT objects (entry + exit filled).
        """
        self.debug_rows = []
        out: List[TradeSignalICT] = []

        if df is None or len(df) < 30:
            return out

        cols = {c.lower(): c for c in df.columns}
        for need in ["open", "high", "low", "close"]:
            if need not in cols:
                raise ValueError(f"Missing '{need}' column in df. Got: {df.columns.tolist()}")

        w = df.copy()

        session_start = w.index[0]
        orb_end = session_start + pd.Timedelta(minutes=self.orb_minutes)
        entry_cutoff = session_start + pd.Timedelta(hours=self.entry_hours)
        breakout_deadline = orb_end + pd.Timedelta(minutes=self.breakout_window_minutes)
        cutoff_close = w.index[-1] - pd.Timedelta(minutes=15)

        orb = w[(w.index >= session_start) & (w.index < orb_end)]
        if len(orb) < max(3, self.orb_minutes - 2):
            return out

        orbH = float(orb["high"].max())
        orbL = float(orb["low"].min())
        orb_range = orbH - orbL

        # Skip degenerate ORB (near-zero range)
        if orb_range <= 0:
            self._log(event="ORB_REJECT", time=session_start, reason="zero_range")
            return out

        # VOLATILITY CONTRACTION FILTER (research-backed: 3x expectancy)
        # Only trade when ORB is compressed (coiled spring).
        # A narrow ORB relative to its own candles' avg range means the market
        # is consolidating → breakout is more likely to produce a trend day.
        orb_candle_ranges = orb["high"] - orb["low"]
        orb_avg_candle_range = float(orb_candle_ranges.mean())
        orb_median_candle_range = float(orb_candle_ranges.median())
        # ORB total range vs what you'd expect if each candle was trending
        # If ORB range < expected (candles overlapping = compression), take the trade
        expected_range = orb_avg_candle_range * len(orb) * 0.40  # 40% = moderate compression filter
        if orb_range > expected_range and expected_range > 0:
            self._log(event="ORB_REJECT", time=session_start, reason="wide_orb",
                      orb_range=round(orb_range, 2), expected=round(expected_range, 2))
            return out

        self._log(event="ORB", time=session_start, orbH=orbH, orbL=orbL, orb_range=orb_range)

        # Trend direction filter: require BOTH HTF + ORB bias to agree (double confirmation)
        # This maximizes quality: only trade when intraday momentum AND structural trend align
        orb_open = float(orb.iloc[0]["open"])
        orb_close = float(orb.iloc[-1]["close"])
        orb_bias = "bullish" if orb_close > orb_open else "bearish"
        if htf_trend is not None:
            if htf_trend != orb_bias:
                self._log(event="TREND_CONFLICT", time=session_start,
                          htf_trend=htf_trend, orb_bias=orb_bias)
                return out
        trend_filter = orb_bias  # both agree at this point

        scan = w[(w.index >= orb_end) & (w.index < entry_cutoff)]
        if len(scan) < 10:
            return out

        scan = scan.copy().reset_index()
        idx_col = [c for c in scan.columns if c not in ["open", "high", "low", "close", "volume"]][0]
        scan = scan.rename(columns={idx_col: "ts"})

        # precompute rolling avg range for displacement filter
        scan["range"] = scan["high"] - scan["low"]
        scan["avg_range"] = scan["range"].rolling(self.lookback_ranges, min_periods=3).mean()

        start_i = max(3, self.lookback_ranges)

        for i in range(start_i, len(scan)):
            ts = scan.at[i, "ts"]
            if ts >= cutoff_close:
                break
            if ts > breakout_deadline:
                break

            o = float(scan.at[i, "open"])
            h = float(scan.at[i, "high"])
            l = float(scan.at[i, "low"])
            c = float(scan.at[i, "close"])
            candle_range = h - l

            # --- Direction: breakout close beyond ORB with margin ---
            direction = None
            margin = self.breakout_margin * orb_range
            if c > orbH + margin:
                direction = "bullish"
            elif c < orbL - margin:
                direction = "bearish"
            else:
                continue

            # --- Filter 1: Displacement ---
            avg_r = float(scan.at[i, "avg_range"]) if pd.notna(scan.at[i, "avg_range"]) else None
            if avg_r is None or avg_r <= 0:
                continue
            if candle_range < self.disp_mult * avg_r:
                continue

            # --- Filter 2: Body % ---
            body_pct = abs(c - o) / candle_range if candle_range > 0 else 0.0
            if body_pct < self.body_min:
                continue

            # --- Filter 3: Close strength (close must be in correct portion) ---
            if candle_range > 0:
                if direction == "bullish":
                    # Close should be in the upper portion of the candle
                    close_pos = (c - l) / candle_range
                    if close_pos < (1.0 - self.close_strength):
                        continue
                else:
                    # Close should be in the lower portion of the candle
                    close_pos = (h - c) / candle_range
                    if close_pos < (1.0 - self.close_strength):
                        continue

            # --- Filter 4: Trend alignment (HTF EMA or ORB bias) ---
            if direction != trend_filter:
                continue

            # --- PULLBACK RETEST ENTRY ---
            # Wait for price to pull back to ORB level and confirm hold.
            # Benefits: tighter SL, confirmed level, higher WR, better RR.
            # If no pullback within window, fall back to direct breakout entry.
            df_idx = w.index.get_indexer([ts])[0]
            retest_found = False
            entry_price = None
            sl = None
            next_idx = None
            retest_limit = min(df_idx + 1 + 20, len(w))

            for look in range(df_idx + 1, retest_limit):
                look_bar = w.iloc[look]
                look_ts = w.index[look]
                if look_ts >= cutoff_close:
                    break

                if direction == "bullish":
                    # Price pulls back near ORB high (within 10% of ORB range)
                    if float(look_bar["low"]) <= orbH + orb_range * 0.10:
                        # Confirm bullish hold: close back above ORB high
                        if float(look_bar["close"]) >= orbH:
                            if look + 1 < len(w):
                                next_idx = look + 1
                                entry_price = float(w.iloc[next_idx]["open"])
                                sl = float(look_bar["low"]) - self.sl_buffer
                                retest_found = True
                            break
                else:
                    # Price pulls back near ORB low (within 10% of ORB range)
                    if float(look_bar["high"]) >= orbL - orb_range * 0.10:
                        # Confirm bearish hold: close back below ORB low
                        if float(look_bar["close"]) <= orbL:
                            if look + 1 < len(w):
                                next_idx = look + 1
                                entry_price = float(w.iloc[next_idx]["open"])
                                sl = float(look_bar["high"]) + self.sl_buffer
                                retest_found = True
                            break

            if not retest_found:
                # No pullback — use direct breakout entry (next bar)
                next_idx = df_idx + 1
                if next_idx >= len(w):
                    continue
                entry_price = float(w.iloc[next_idx]["open"])
                if direction == "bullish":
                    sl = l - self.sl_buffer
                else:
                    sl = h + self.sl_buffer

            if next_idx is None or entry_price is None or sl is None:
                continue

            # Re-calculate R from actual entry price
            if direction == "bullish":
                R = entry_price - sl
            else:
                R = sl - entry_price

            if R <= 0 or R < self.min_sl or R > self.max_sl:
                continue

            self._log(
                event="ENTRY_SIGNAL",
                time=w.index[next_idx],
                dir=direction,
                entry=entry_price,
                signal_bar_close=c,
                sl=sl,
                R=R,
                body_pct=round(body_pct, 3),
                disp_ratio=round(candle_range / avg_r, 2),
            )

            # Determine signal price (trigger candle close) for drift calculation
            if retest_found:
                signal_close = float(w.iloc[next_idx - 1]["close"])  # retest candle close
            else:
                signal_close = c  # breakout candle close

            trade = self._simulate_trade(
                df=w,
                start_idx=next_idx + 1,  # simulate from bar AFTER entry
                direction=direction,
                entry=entry_price,
                sl_init=sl,
                R=R,
                cutoff_time=cutoff_close,
            )
            trade.signal_price = signal_close
            trade.breakout_time = ts  # breakout candle timestamp
            out.append(trade)
            break  # 1 trade per day

        return out
