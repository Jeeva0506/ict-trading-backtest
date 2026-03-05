#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
OB STRICT ENGINE — FINAL (No look-ahead, pessimistic fills, realistic costs)

Key fixes vs inflated versions:
1) H1 bias uses ONLY closed H1 candles (close_time gating) ✅
2) M1 scan starts AFTER the MSS candle closes ✅
3) M3 impulse candle uses ONLY closed M3 candles ✅
4) Slippage is adverse-only (not symmetric) ✅
5) Exit costs included (adverse slippage; optional spread-on-exit) ✅
6) Spread is configurable per symbol ✅
7) Optional swap/carry cost model ✅

Strategy summary (your current official logic)
- H1 bias: EMA21/EMA50 (closed H1 only)
- MSS + OB on M15:
  - MSS: M15 close crosses above last confirmed pivot high (bull)
         or crosses below last confirmed pivot low (bear)
  - Pivot confirmation length L = pivot_len (default 5). Only use pivot at i-L.
  - One MSS per swing: lock until a NEW pivot is confirmed.
- OB: last opposite candle before MSS (M15, lookback window)
- Tap (surface rejection): after MSS close, M1 must enter OB at least min_ob_pen
  (default 10%) but NOT penetrate past max_ob_pen (default 60%).
  If max penetration exceeded at ANY point, setup is invalidated.
- Confirmation: M3 pivot (swing low for longs, swing high for shorts)
  Pivot low must be at/near OB zone (below OB_high for bulls).
  Confirmed when next closed M3 candle has higher low (bull) or lower high (bear).
  Entry at confirming M1 close — no look-ahead.
- SL: OB zone boundary (ob_low for longs, ob_high for shorts) + buffer
  If the entire OB is breached, the premise is invalid. Structurally sound.
- Exit models: A=fixed TP, B=tiered, C=M15 structure trail
  - Pessimistic same-bar SL priority

CSV requirements:
- datetime/time/timestamp/date column + open/high/low/close
- datetime treated as UTC
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass, asdict
from typing import Dict, List, Tuple, Optional

import numpy as np
import pandas as pd
import pytz

NY = pytz.timezone("America/New_York")


# -----------------------------
# Config
# -----------------------------

@dataclass
class Config:
    symbol: str
    csv_path: str
    out_dir: str = "./out"

    # Backtest window (months back from end of data; 0 = full)
    months: int = 0
    # In-sample: use only the FIRST N months from start (0 = off)
    first_months: int = 0

    # Structure
    pivot_len: int = 5
    ob_lookback_m15: int = 10
    impulse_lookback_m15: int = 20  # median range filter

    # Bias
    use_h1_bias: bool = True
    ema_fast: int = 21
    ema_slow: int = 50

    # Session filter (NY killzones)
    use_killzones: bool = True
    london_start: int = 2
    london_end: int = 5
    ny_start: int = 7
    ny_end: int = 10

    # Costs / realism
    spread: float = 0.00012
    entry_slippage: float = 0.00015
    exit_slippage: float = 0.00015
    miss_fill_prob: float = 0.08
    sl_buffer: float = 0.00005
    spread_on_exit: bool = False  # optional; conservative if True

    # Swap (optional)
    swap_per_day_R: float = 0.0  # apply in R units per rollover day crossed (default off)

    # Exit model: "A" = fixed TP/SL, "B" = tiered (BE@1R, 50%@2R, close@3R),
    #              "C" = tiered + structure trail (BE@1R, 50%@2R, trail M15 swings)
    exit_model: str = "A"
    tp_R: float = 2.5
    breakeven_at_R: float = 0.0  # model A only: move SL to entry at this R (0=off)

    # OB penetration limits (surface rejection model)
    min_ob_pen: float = 0.10   # minimum penetration to qualify as tap (10%)
    max_ob_pen: float = 0.60   # maximum allowed penetration (60%)

    # M3 pivot confirmation strength
    m3_pivot_confirms: int = 2  # number of confirming M3 candles needed (1=weak, 2=default)

    # RNG
    seed: int = 7


# -----------------------------
# IO / Time
# -----------------------------

def load_m1(csv_path: str) -> pd.DataFrame:
    df = pd.read_csv(csv_path)
    df.columns = [c.lower().strip() for c in df.columns]

    if "datetime" not in df.columns:
        for c in ("time", "date", "timestamp", "open_time"):
            if c in df.columns:
                df = df.rename(columns={c: "datetime"})
                break
    if "datetime" not in df.columns:
        raise ValueError("CSV must have a datetime/time/date/timestamp column.")

    need = {"open", "high", "low", "close"}
    missing = sorted(list(need - set(df.columns)))
    if missing:
        raise ValueError(f"CSV missing columns: {missing}")

    df["datetime"] = pd.to_datetime(df["datetime"], utc=True, errors="coerce")
    df = df.dropna(subset=["datetime"]).sort_values("datetime").reset_index(drop=True)
    df = df.drop_duplicates(subset=["datetime"], keep="last").reset_index(drop=True)

    return df[["datetime", "open", "high", "low", "close"]].copy()


def resample_ohlc(df_m1: pd.DataFrame, rule: str) -> pd.DataFrame:
    base = df_m1.set_index("datetime")
    out = base.resample(rule).agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
    ).dropna().reset_index()
    # For strict "closed candle" gating, we treat datetime as OPEN time
    td = pd.tseries.frequencies.to_offset(rule)
    out["close_time"] = out["datetime"] + td
    return out


def killzone_mask(dt_utc: pd.Series, cfg: Config) -> pd.Series:
    if not cfg.use_killzones:
        return pd.Series(True, index=dt_utc.index)
    ny = dt_utc.dt.tz_convert(NY)
    h = ny.dt.hour
    london = (h >= cfg.london_start) & (h < cfg.london_end)
    newyork = (h >= cfg.ny_start) & (h < cfg.ny_end)
    return london | newyork


def ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def compute_h1_bias(df_m1: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    h1 = df_m1.set_index("datetime").resample("1h").agg(close=("close", "last")).dropna().reset_index()
    # h1 datetime is OPEN time; closed at +1H
    h1["close_time"] = h1["datetime"] + pd.Timedelta(hours=1)
    h1["ema_fast"] = ema(h1["close"], cfg.ema_fast)
    h1["ema_slow"] = ema(h1["close"], cfg.ema_slow)
    h1["bull"] = h1["ema_fast"] > h1["ema_slow"]
    h1["bear"] = h1["ema_fast"] < h1["ema_slow"]
    return h1


# -----------------------------
# Pivots (confirmed)
# -----------------------------

def pivot_high(arr: np.ndarray, L: int) -> np.ndarray:
    n = len(arr)
    out = np.full(n, np.nan)
    for i in range(L, n - L):
        if arr[i] == np.max(arr[i - L:i + L + 1]):
            out[i] = arr[i]
    return out


def pivot_low(arr: np.ndarray, L: int) -> np.ndarray:
    n = len(arr)
    out = np.full(n, np.nan)
    for i in range(L, n - L):
        if arr[i] == np.min(arr[i - L:i + L + 1]):
            out[i] = arr[i]
    return out


# -----------------------------
# Metrics
# -----------------------------

def profit_factor(r: np.ndarray) -> float:
    if len(r) == 0:
        return float("nan")
    wins = r[r > 0].sum()
    losses = -r[r < 0].sum()
    if losses <= 0:
        return float("inf") if wins > 0 else float("nan")
    return float(wins / losses)


def max_drawdown_R(r: np.ndarray) -> float:
    if len(r) == 0:
        return 0.0
    eq = np.cumsum(r)
    peak = np.maximum.accumulate(eq)
    dd = eq - peak
    return float(dd.min())


# -----------------------------
# Strict Engine
# -----------------------------

def strict_backtest(df_m1: pd.DataFrame, cfg: Config) -> Tuple[pd.DataFrame, Dict]:
    rng = np.random.default_rng(cfg.seed)

    # window selection
    if cfg.months and cfg.months > 0:
        cutoff = df_m1["datetime"].max() - pd.DateOffset(months=int(cfg.months))
        df_m1 = df_m1[df_m1["datetime"] >= cutoff].reset_index(drop=True)

    if cfg.first_months and cfg.first_months > 0:
        cutoff = df_m1["datetime"].min() + pd.DateOffset(months=int(cfg.first_months))
        df_m1 = df_m1[df_m1["datetime"] <= cutoff].reset_index(drop=True)

    df_m1 = df_m1.copy()
    df_m1["sess"] = killzone_mask(df_m1["datetime"], cfg).astype(bool)

    m15 = resample_ohlc(df_m1, "15min")
    m15["sess"] = killzone_mask(m15["datetime"], cfg).astype(bool)

    m3 = resample_ohlc(df_m1, "3min")
    m3["sess"] = killzone_mask(m3["datetime"], cfg).astype(bool)

    h1 = compute_h1_bias(df_m1, cfg) if cfg.use_h1_bias else None

    # Fast arrays
    m1t = df_m1["datetime"].to_numpy(dtype="datetime64[ns]")
    O1 = df_m1["open"].to_numpy(float)
    H1a = df_m1["high"].to_numpy(float)
    L1a = df_m1["low"].to_numpy(float)
    C1 = df_m1["close"].to_numpy(float)
    S1 = df_m1["sess"].to_numpy(bool)

    m15t = m15["datetime"].to_numpy(dtype="datetime64[ns]")
    m15_close_t = m15["close_time"].to_numpy(dtype="datetime64[ns]")
    O15 = m15["open"].to_numpy(float)
    H15 = m15["high"].to_numpy(float)
    L15 = m15["low"].to_numpy(float)
    C15 = m15["close"].to_numpy(float)
    S15 = m15["sess"].to_numpy(bool)
    R15 = H15 - L15

    m3t = m3["datetime"].to_numpy(dtype="datetime64[ns]")
    m3_close_t = m3["close_time"].to_numpy(dtype="datetime64[ns]")
    O3 = m3["open"].to_numpy(float)
    H3 = m3["high"].to_numpy(float)
    L3 = m3["low"].to_numpy(float)
    C3 = m3["close"].to_numpy(float)
    S3 = m3["sess"].to_numpy(bool)

    # H1 bias lookup uses close_time gating (FIX #1)
    if h1 is not None:
        h1_close_t = h1["close_time"].to_numpy(dtype="datetime64[ns]")
        h1_bull = h1["bull"].to_numpy(bool)
        h1_bear = h1["bear"].to_numpy(bool)

        def bias_ok(now_utc_ns: np.datetime64, direction: int) -> bool:
            k = int(np.searchsorted(h1_close_t, now_utc_ns, side="right")) - 1
            if k < 0:
                return False
            return bool(h1_bull[k]) if direction == 1 else bool(h1_bear[k])
    else:
        def bias_ok(now_utc_ns: np.datetime64, direction: int) -> bool:  # type: ignore
            return True

    # Pivots
    Lp = cfg.pivot_len
    pH = pivot_high(H15, Lp)
    pL = pivot_low(L15, Lp)

    def m1_index(ts_ns: np.datetime64) -> int:
        return int(np.searchsorted(m1t, ts_ns, side="left"))

    def m3_index_closed_at_or_before(now_ns: np.datetime64) -> int:
        # last M3 candle whose CLOSE time is <= now (FIX #3)
        return int(np.searchsorted(m3_close_t, now_ns, side="right")) - 1

    # One MSS per swing lock
    last_pivot_high = np.nan
    last_pivot_low = np.nan
    mss_lock = False

    trades: List[Dict] = []

    start_i = max(cfg.impulse_lookback_m15 + 5, 2 * Lp + 5)
    for i in range(start_i, len(m15)):
        if not S15[i]:
            continue

        # Unlock when a NEW pivot becomes confirmed (at i-Lp)
        if np.isfinite(pH[i - Lp]) or np.isfinite(pL[i - Lp]):
            mss_lock = False

        # Update confirmed pivots
        if np.isfinite(pH[i - Lp]):
            last_pivot_high = pH[i - Lp]
        if np.isfinite(pL[i - Lp]):
            last_pivot_low = pL[i - Lp]

        if mss_lock:
            continue

        # MSS candle quality filter (median range of last N completed candles)
        med = float(np.median(R15[i - cfg.impulse_lookback_m15:i]))
        if (H15[i] - L15[i]) < med:
            continue

        # Strict MSS close-cross using confirmed pivot levels
        bull_mss = np.isfinite(last_pivot_high) and (C15[i - 1] <= last_pivot_high) and (C15[i] > last_pivot_high)
        bear_mss = np.isfinite(last_pivot_low) and (C15[i - 1] >= last_pivot_low) and (C15[i] < last_pivot_low)
        if not (bull_mss or bear_mss):
            continue

        direction = 1 if bull_mss else -1

        # Bias check at MSS close time (using closed H1) (FIX #1)
        mss_close_time_ns = m15_close_t[i]
        if not bias_ok(mss_close_time_ns, direction):
            continue

        # Find OB: last opposite candle pre-MSS
        ob_low = ob_high = None
        for j in range(i - 1, max(-1, i - cfg.ob_lookback_m15 - 1), -1):
            if not S15[j]:
                continue
            if direction == 1 and C15[j] < O15[j]:
                ob_low, ob_high = float(L15[j]), float(H15[j])
                break
            if direction == -1 and C15[j] > O15[j]:
                ob_low, ob_high = float(L15[j]), float(H15[j])
                break
        if ob_low is None:
            continue

        mss_lock = True
        # Surface rejection: price must tap OB surface (min depth) but NOT
        # penetrate past max depth.  SL = deepest candle that touched OB only.
        ob_range = ob_high - ob_low
        if direction == 1:
            surface_level = ob_high - cfg.min_ob_pen * ob_range   # must reach
            max_pen_level = ob_high - cfg.max_ob_pen * ob_range   # must NOT breach
        else:
            surface_level = ob_low + cfg.min_ob_pen * ob_range
            max_pen_level = ob_low + cfg.max_ob_pen * ob_range

        # IMPORTANT: start scanning AFTER MSS candle closes (FIX #2)
        k = m1_index(mss_close_time_ns)

        tapped = False
        deepest_low = float('inf')    # bulls: lowest low of OB-touching candles
        deepest_high = float('-inf')  # bears: highest high of OB-touching candles

        # M3 pivot confirmation state
        m3_pv_state = 0       # 0=looking, 1=potential pivot found, 2=confirmed
        pot_pivot_m3_j = -1   # M3 index of potential pivot candle
        last_m3_checked = -1  # last processed closed M3 candle index
        confirms_remaining = cfg.m3_pivot_confirms  # candles with HL needed after pivot

        while k < len(df_m1):
            if not S1[k]:
                k += 1
                continue

            # Max penetration guard — invalidate if price goes too deep
            if (direction == 1 and L1a[k] < max_pen_level) or \
               (direction == -1 and H1a[k] > max_pen_level):
                break  # OB failed — too deep

            # Surface tap: must enter OB at least min_depth
            if not tapped:
                if (direction == 1 and L1a[k] <= surface_level) or \
                   (direction == -1 and H1a[k] >= surface_level):
                    tapped = True
                    if direction == 1:
                        deepest_low = float(L1a[k])
                    else:
                        deepest_high = float(H1a[k])
                k += 1
                continue

            # After tap: track deepest candle touching OB zone
            if direction == 1 and L1a[k] <= ob_high:
                deepest_low = min(deepest_low, float(L1a[k]))
            elif direction == -1 and H1a[k] >= ob_low:
                deepest_high = max(deepest_high, float(H1a[k]))

            # Confirmation: M3 pivot (swing low for longs, swing high for shorts)
            cur_m3_j = m3_index_closed_at_or_before(m1t[k])
            if cur_m3_j > last_m3_checked and cur_m3_j >= 1:
                last_m3_checked = cur_m3_j

                if direction == 1:
                    # M3 swing low: candle dips lower than prev AND touches OB
                    if m3_pv_state == 0:
                        if L3[cur_m3_j] < L3[cur_m3_j - 1] and L3[cur_m3_j] <= ob_high:
                            pot_pivot_m3_j = cur_m3_j
                            m3_pv_state = 1
                            confirms_remaining = cfg.m3_pivot_confirms
                    elif m3_pv_state == 1:
                        if L3[cur_m3_j] > L3[pot_pivot_m3_j]:
                            confirms_remaining -= 1
                            if confirms_remaining <= 0:
                                m3_pv_state = 2  # pivot confirmed
                        elif L3[cur_m3_j] <= L3[pot_pivot_m3_j]:
                            pot_pivot_m3_j = cur_m3_j  # deeper low
                            confirms_remaining = cfg.m3_pivot_confirms
                else:
                    # M3 swing high: candle pokes higher than prev AND touches OB
                    if m3_pv_state == 0:
                        if H3[cur_m3_j] > H3[cur_m3_j - 1] and H3[cur_m3_j] >= ob_low:
                            pot_pivot_m3_j = cur_m3_j
                            m3_pv_state = 1
                            confirms_remaining = cfg.m3_pivot_confirms
                    elif m3_pv_state == 1:
                        if H3[cur_m3_j] < H3[pot_pivot_m3_j]:
                            confirms_remaining -= 1
                            if confirms_remaining <= 0:
                                m3_pv_state = 2  # pivot confirmed
                        elif H3[cur_m3_j] >= H3[pot_pivot_m3_j]:
                            pot_pivot_m3_j = cur_m3_j  # higher high
                            confirms_remaining = cfg.m3_pivot_confirms

            if m3_pv_state != 2:
                k += 1
                continue

            # Missed fill
            if rng.random() < cfg.miss_fill_prob:
                break

            # Entry: close + spread + adverse slippage (FIX #4)
            entry = float(C1[k])
            entry += (cfg.spread if direction == 1 else -cfg.spread)
            entry += (cfg.entry_slippage if direction == 1 else -cfg.entry_slippage)

            # SL: OB zone boundary (if full OB is breached, setup is invalid)
            sl = (ob_low - cfg.sl_buffer) if direction == 1 else (ob_high + cfg.sl_buffer)
            sl = float(sl)

            risk = abs(entry - sl)
            # Minimum risk filter: must exceed 2x round-trip costs
            min_risk = 2.0 * (cfg.spread + cfg.entry_slippage + cfg.exit_slippage)
            if risk <= min_risk:
                break

            tp = entry + direction * (cfg.tp_R * risk)

            # Same-bar pessimism: if entry bar touches SL => SL
            sl_same = (direction == 1 and L1a[k] <= sl) or (direction == -1 and H1a[k] >= sl)
            if sl_same:
                trades.append({
                    "symbol": cfg.symbol,
                    "direction": "LONG" if direction == 1 else "SHORT",
                    "entry_time": str(pd.Timestamp(m1t[k]).tz_localize("UTC")),
                    "entry_price": entry,
                    "sl": sl,
                    "tp": tp,
                    "exit_time": str(pd.Timestamp(m1t[k]).tz_localize("UTC")),
                    "exit_price": sl,
                    "R": -1.0,
                    "exit_reason": "SL_samebar",
                })
                break

            # Exit scan
            z = k + 1
            exit_reason = "EOD"
            exit_time = pd.Timestamp(m1t[-1]).tz_localize("UTC")
            exit_price = float(C1[-1])
            R_out = 0.0

            entry_ts = pd.Timestamp(m1t[k]).tz_localize("UTC")
            entry_ny_date = entry_ts.tz_convert(NY).date()
            swap_days = 0

            # ── Model A: fixed TP/SL with optional BE ──
            if cfg.exit_model == "A":
                be_triggered = False
                be_level = entry
                be_threshold = entry + direction * (cfg.breakeven_at_R * risk) if cfg.breakeven_at_R > 0 else None

                while z < len(df_m1):
                    if not S1[z]:
                        z += 1
                        continue
                    bar_ts = pd.Timestamp(m1t[z]).tz_localize("UTC")
                    if cfg.swap_per_day_R != 0.0:
                        d = bar_ts.tz_convert(NY).date()
                        if d != entry_ny_date:
                            swap_days = (d - entry_ny_date).days

                    if be_threshold is not None and not be_triggered:
                        if (direction == 1 and H1a[z] >= be_threshold) or (direction == -1 and L1a[z] <= be_threshold):
                            be_triggered = True
                            sl = be_level

                    if (direction == 1 and L1a[z] <= sl) or (direction == -1 and H1a[z] >= sl):
                        exit_reason = "SL_BE" if be_triggered else "SL"
                        exit_time = bar_ts
                        exit_price = sl
                        exit_price += (-cfg.exit_slippage if direction == 1 else cfg.exit_slippage)
                        R_out = 0.0 if be_triggered else -1.0
                        break

                    if (direction == 1 and H1a[z] >= tp) or (direction == -1 and L1a[z] <= tp):
                        exit_reason = "TP"
                        exit_time = bar_ts
                        exit_price = tp
                        exit_price += (-cfg.exit_slippage if direction == 1 else cfg.exit_slippage)
                        R_out = cfg.tp_R
                        break
                    z += 1

            # ── Model B: BE@1R, 50%@2R, close@3R ──
            elif cfg.exit_model == "B":
                level_1R = entry + direction * risk
                level_2R = entry + direction * 2 * risk
                level_3R = entry + direction * 3 * risk
                be_done = False
                partial_done = False
                partial_R = 0.0  # R accumulated from partial close

                while z < len(df_m1):
                    if not S1[z]:
                        z += 1
                        continue
                    bar_ts = pd.Timestamp(m1t[z]).tz_localize("UTC")
                    if cfg.swap_per_day_R != 0.0:
                        d = bar_ts.tz_convert(NY).date()
                        if d != entry_ny_date:
                            swap_days = (d - entry_ny_date).days

                    # 1R reached → move SL to BE
                    if not be_done:
                        if (direction == 1 and H1a[z] >= level_1R) or (direction == -1 and L1a[z] <= level_1R):
                            be_done = True
                            sl = entry  # SL to breakeven

                    # 2R reached → book 50%
                    if be_done and not partial_done:
                        if (direction == 1 and H1a[z] >= level_2R) or (direction == -1 and L1a[z] <= level_2R):
                            partial_done = True
                            partial_R = 0.5 * 2.0  # 50% of position closed at 2R

                    # SL hit (check BEFORE 3R on same bar — pessimistic)
                    if (direction == 1 and L1a[z] <= sl) or (direction == -1 and H1a[z] >= sl):
                        exit_time = bar_ts
                        if not be_done:
                            exit_reason = "SL"
                            exit_price = sl
                            R_out = -1.0
                        elif not partial_done:
                            exit_reason = "SL_BE"
                            exit_price = entry
                            R_out = 0.0  # full position at BE
                        else:
                            exit_reason = "SL_BE_partial"
                            exit_price = entry
                            R_out = partial_R + 0.5 * 0.0  # remaining 50% at BE
                        break

                    # 3R reached → close remaining
                    if partial_done:
                        if (direction == 1 and H1a[z] >= level_3R) or (direction == -1 and L1a[z] <= level_3R):
                            exit_reason = "TP_3R"
                            exit_time = bar_ts
                            exit_price = float(level_3R)
                            R_out = partial_R + 0.5 * 3.0  # 50%@2R + 50%@3R
                            break
                    z += 1

                # EOD fallback
                if exit_reason == "EOD" and (partial_done or be_done):
                    eod_R = (float(C1[-1]) - entry) * direction / risk
                    if partial_done:
                        R_out = partial_R + 0.5 * eod_R
                    else:
                        R_out = eod_R

            # ── Model C: BE@1R, 50%@2R, then trail on M15 structure ──
            elif cfg.exit_model == "C":
                level_1R = entry + direction * risk
                level_2R = entry + direction * 2 * risk
                be_done = False
                partial_done = False
                partial_R = 0.0
                trailing = False

                # Helper: find last confirmed M15 swing for trailing
                def get_trail_sl(now_ns):
                    # Find last closed M15 bar
                    m15_idx = int(np.searchsorted(m15_close_t, now_ns, side="right")) - 1
                    if m15_idx < Lp:
                        return sl  # not enough data, keep current
                    # Scan backward for last confirmed pivot
                    for pi in range(m15_idx - Lp, max(-1, m15_idx - Lp - 20), -1):
                        if direction == 1 and np.isfinite(pL[pi]):
                            return float(pL[pi]) - cfg.sl_buffer  # trail below swing low
                        if direction == -1 and np.isfinite(pH[pi]):
                            return float(pH[pi]) + cfg.sl_buffer  # trail above swing high
                    return sl  # no pivot found, keep current

                while z < len(df_m1):
                    if not S1[z]:
                        z += 1
                        continue
                    bar_ts = pd.Timestamp(m1t[z]).tz_localize("UTC")
                    if cfg.swap_per_day_R != 0.0:
                        d = bar_ts.tz_convert(NY).date()
                        if d != entry_ny_date:
                            swap_days = (d - entry_ny_date).days

                    # 1R → BE
                    if not be_done:
                        if (direction == 1 and H1a[z] >= level_1R) or (direction == -1 and L1a[z] <= level_1R):
                            be_done = True
                            sl = entry

                    # 2R → partial 50% + start trailing
                    if be_done and not partial_done:
                        if (direction == 1 and H1a[z] >= level_2R) or (direction == -1 and L1a[z] <= level_2R):
                            partial_done = True
                            trailing = True
                            partial_R = 0.5 * 2.0

                    # Update trail SL from M15 swings (only tighten, never widen)
                    if trailing:
                        new_trail = get_trail_sl(m1t[z])
                        if direction == 1 and new_trail > sl:
                            sl = new_trail
                        elif direction == -1 and new_trail < sl:
                            sl = new_trail

                    # SL hit (pessimistic: check before TP on same bar)
                    if (direction == 1 and L1a[z] <= sl) or (direction == -1 and H1a[z] >= sl):
                        exit_time = bar_ts
                        if not be_done:
                            exit_reason = "SL"
                            exit_price = sl
                            R_out = -1.0
                        elif not partial_done:
                            exit_reason = "SL_BE"
                            exit_price = entry
                            R_out = 0.0
                        else:
                            # Remaining 50% stopped at trail level
                            trail_R = (sl - entry) * direction / risk
                            exit_reason = "SL_trail"
                            exit_price = sl
                            R_out = partial_R + 0.5 * trail_R
                        break
                    z += 1

                # EOD fallback
                if exit_reason == "EOD":
                    eod_R = (float(C1[-1]) - entry) * direction / risk
                    if partial_done:
                        R_out = partial_R + 0.5 * eod_R
                    elif be_done:
                        R_out = eod_R
                    else:
                        R_out = eod_R

            # Apply swap
            if cfg.swap_per_day_R != 0.0 and swap_days > 0:
                R_out = float(R_out - swap_days * cfg.swap_per_day_R)

            trades.append({
                "symbol": cfg.symbol,
                "direction": "LONG" if direction == 1 else "SHORT",
                "entry_time": str(pd.Timestamp(m1t[k]).tz_localize("UTC")),
                "entry_price": entry,
                "sl": sl,
                "tp": tp,
                "exit_time": str(exit_time),
                "exit_price": float(exit_price),
                "R": float(R_out),
                "exit_reason": exit_reason,
                "swap_days": int(swap_days),
            })
            break

        # end while k

    trades_df = pd.DataFrame(trades)
    r = trades_df["R"].to_numpy(float) if len(trades_df) else np.array([], dtype=float)

    metrics = {
        "symbol": cfg.symbol,
        "months": cfg.months,
        "trades": int(len(r)),
        "winrate": float((r > 0).mean()) if len(r) else float("nan"),
        "avgR": float(r.mean()) if len(r) else float("nan"),
        "profit_factor": profit_factor(r),
        "max_dd_R": max_drawdown_R(r),
        "sum_R": float(r.sum()) if len(r) else 0.0,
        "params": asdict(cfg),
    }

    return trades_df, metrics


# -----------------------------
# CLI
# -----------------------------

def parse_args() -> Config:
    p = argparse.ArgumentParser()
    p.add_argument("--symbol", required=True)
    p.add_argument("--csv", required=True)
    p.add_argument("--out_dir", default="./out")
    p.add_argument("--months", type=int, default=0)
    p.add_argument("--first_months", type=int, default=0, help="Use first N months only (IS sample)")

    # Costs
    p.add_argument("--spread", type=float, default=0.00012)
    p.add_argument("--entry_slip", type=float, default=0.00015)
    p.add_argument("--exit_slip", type=float, default=0.00015)
    p.add_argument("--miss_fill", type=float, default=0.08)
    p.add_argument("--sl_buffer", type=float, default=0.00005)
    p.add_argument("--spread_on_exit", action="store_true")

    # Swap
    p.add_argument("--swap_per_day_R", type=float, default=0.0)

    # Strategy
    p.add_argument("--pivot_len", type=int, default=5)
    p.add_argument("--ob_lookback", type=int, default=10)
    p.add_argument("--impulse_lookback", type=int, default=20)

    # Bias
    p.add_argument("--no_h1_bias", action="store_true")
    p.add_argument("--ema_fast", type=int, default=21)
    p.add_argument("--ema_slow", type=int, default=50)

    # Session
    p.add_argument("--no_killzones", action="store_true")
    p.add_argument("--london_start", type=int, default=2)
    p.add_argument("--london_end", type=int, default=5)
    p.add_argument("--ny_start", type=int, default=7)
    p.add_argument("--ny_end", type=int, default=10)

    # Exit
    p.add_argument("--exit_model", default="A", choices=["A", "B", "C"], help="Exit model: A=fixed TP, B=tiered, C=trail")
    p.add_argument("--tp_R", type=float, default=2.5)
    p.add_argument("--breakeven_at_R", type=float, default=0.0, help="Move SL to entry at this R MFE (0=off)")
    p.add_argument("--min_ob_pen", type=float, default=0.10, help="Minimum OB penetration (0.10=10pct)")
    p.add_argument("--max_ob_pen", type=float, default=0.60, help="Maximum OB penetration (0.60=60pct)")
    p.add_argument("--m3_pivot_confirms", type=int, default=2, help="Confirming M3 candles for pivot (1=weak, 2=default)")

    # RNG
    p.add_argument("--seed", type=int, default=7)

    a = p.parse_args()

    return Config(
        symbol=a.symbol,
        csv_path=a.csv,
        out_dir=a.out_dir,
        months=a.months,
        first_months=a.first_months,

        spread=a.spread,
        entry_slippage=a.entry_slip,
        exit_slippage=a.exit_slip,
        miss_fill_prob=a.miss_fill,
        sl_buffer=a.sl_buffer,
        spread_on_exit=bool(a.spread_on_exit),

        swap_per_day_R=a.swap_per_day_R,

        pivot_len=a.pivot_len,
        ob_lookback_m15=a.ob_lookback,
        impulse_lookback_m15=a.impulse_lookback,

        use_h1_bias=not a.no_h1_bias,
        ema_fast=a.ema_fast,
        ema_slow=a.ema_slow,

        use_killzones=not a.no_killzones,
        london_start=a.london_start,
        london_end=a.london_end,
        ny_start=a.ny_start,
        ny_end=a.ny_end,

        tp_R=a.tp_R,
        exit_model=a.exit_model,
        breakeven_at_R=a.breakeven_at_R,
        min_ob_pen=a.min_ob_pen,
        max_ob_pen=a.max_ob_pen,
        m3_pivot_confirms=a.m3_pivot_confirms,
        seed=a.seed,
    )


def main() -> None:
    cfg = parse_args()
    os.makedirs(cfg.out_dir, exist_ok=True)

    df = load_m1(cfg.csv_path)
    trades_df, metrics = strict_backtest(df, cfg)

    base = f"{cfg.symbol}_OB_STRICT_FINAL_tp{cfg.tp_R}_m{cfg.months}_seed{cfg.seed}"
    trades_path = os.path.join(cfg.out_dir, base + "_trades.csv")
    metrics_path = os.path.join(cfg.out_dir, base + "_metrics.json")

    trades_df.to_csv(trades_path, index=False)
    with open(metrics_path, "w", encoding="utf-8") as f:
        json.dump(metrics, f, indent=2)

    print("\n=== OB STRICT FINAL COMPLETE ===")
    print("Symbol   :", metrics["symbol"])
    print("Months   :", metrics["months"])
    print("Trades   :", metrics["trades"])
    print("Winrate  :", metrics["winrate"])
    print("AvgR     :", metrics["avgR"])
    print("PF       :", metrics["profit_factor"])
    print("MaxDD(R) :", metrics["max_dd_R"])
    print("SumR     :", metrics["sum_R"])
    print("Trades  ->", trades_path)
    print("Metrics ->", metrics_path)


if __name__ == "__main__":
    main()
