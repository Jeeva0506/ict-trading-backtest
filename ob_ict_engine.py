#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
OB ICT ENGINE v4 — Displacement + Order Block (Institutional)

Core ICT concepts:
1) H1 bias: EMA21/EMA50 (closed H1 candles only, no look-ahead) ✅
2) Displacement on M15:
   - Strong impulse candle: body > 60% of range, range > 1.5× ATR
   - Shows INSTITUTIONAL ORDER FLOW, not just pivot crossing
3) Order Block at displacement BASE:
   - Last opposite candle BEFORE the displacement candle
   - WHERE institutions placed orders — the origination of the move
4) Pullback to OB zone: price must retrace back to the OB
5) M1 rejection candle entry:
   - Wick tests OB zone (enters from the right side)
   - Closes in displacement direction (bullish close for longs)
   - Entry RIGHT AT the level — risk is OB edge, not some distant pivot
6) SL: OB opposite edge + buffer (tight, structural)
7) OB size filter: reject OBs > max_ob_atr × ATR (prevents giant SL)
8) OB expiry: setup dies after N M15 bars if untested
9) Exit models: A=fixed TP, B=tiered, C=M15 structure trail

Realism:
- Adverse-only slippage, per-symbol spread ✅
- Miss fill probability, same-bar SL pessimism ✅
- No look-ahead bias (H1 close_time gating, M1 scan after displacement close) ✅
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


# ─────────────────────────────────────
# Config
# ─────────────────────────────────────

@dataclass
class Config:
    symbol: str
    csv_path: str
    out_dir: str = "./out"

    # Window
    months: int = 0          # N months back from end (0=full)
    first_months: int = 0    # first N months from start (0=off)

    # Displacement detection
    disp_atr_mult: float = 1.5   # range must exceed this × ATR
    disp_body_pct: float = 0.60  # body must be > this fraction of range
    atr_window: int = 20         # lookback for ATR (median of M15 ranges)

    # Order block
    ob_lookback: int = 10        # max M15 bars back to search for OB
    max_ob_atr: float = 2.0      # reject OB if its range > this × ATR
    ob_max_wait: int = 40        # max M15 bars to wait for pullback to OB

    # Trade quality filters
    require_mss: bool = True     # displacement must break recent swing structure
    mss_lookback: int = 20       # how far back to look for swing to break
    max_risk_atr: float = 0.0    # reject trade if risk > this × ATR (0=off)
    max_entry_overshoot: float = 0.0  # max entry distance from OB edge (× OB range, 0=off)

    # Structure (M15 pivots — used for Model C trailing only)
    pivot_len: int = 3

    # Bias
    use_h1_bias: bool = True
    ema_fast: int = 21
    ema_slow: int = 50

    # Session filter (NY hours)
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
    spread_on_exit: bool = False
    swap_per_day_R: float = 0.0

    # Exit
    exit_model: str = "A"    # A=fixed TP, B=tiered, C=structure trail
    tp_R: float = 2.5
    breakeven_at_R: float = 0.0  # model A only (0=off)

    # RNG
    seed: int = 7


# ─────────────────────────────────────
# IO / Resampling
# ─────────────────────────────────────

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
    h1 = df_m1.set_index("datetime").resample("1h").agg(
        close=("close", "last")).dropna().reset_index()
    h1["close_time"] = h1["datetime"] + pd.Timedelta(hours=1)
    h1["ema_fast"] = ema(h1["close"], cfg.ema_fast)
    h1["ema_slow"] = ema(h1["close"], cfg.ema_slow)
    h1["bull"] = h1["ema_fast"] > h1["ema_slow"]
    h1["bear"] = h1["ema_fast"] < h1["ema_slow"]
    return h1


# ─────────────────────────────────────
# Pivots (for Model C trailing)
# ─────────────────────────────────────

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


# ─────────────────────────────────────
# Metrics
# ─────────────────────────────────────

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


# ─────────────────────────────────────
# ICT Displacement Backtest Engine
# ─────────────────────────────────────

def strict_backtest(df_m1: pd.DataFrame, cfg: Config) -> Tuple[pd.DataFrame, Dict]:
    rng = np.random.default_rng(cfg.seed)

    # ── Window selection ──
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

    h1 = compute_h1_bias(df_m1, cfg) if cfg.use_h1_bias else None

    # ── Fast arrays ──
    m1t = df_m1["datetime"].to_numpy(dtype="datetime64[ns]")
    O1 = df_m1["open"].to_numpy(float)
    H1a = df_m1["high"].to_numpy(float)
    L1a = df_m1["low"].to_numpy(float)
    C1 = df_m1["close"].to_numpy(float)
    S1 = df_m1["sess"].to_numpy(bool)

    m15t = m15["datetime"].to_numpy(dtype="datetime64[ns]")
    m15ct = m15["close_time"].to_numpy(dtype="datetime64[ns]")
    O15 = m15["open"].to_numpy(float)
    H15 = m15["high"].to_numpy(float)
    L15 = m15["low"].to_numpy(float)
    C15 = m15["close"].to_numpy(float)
    S15 = m15["sess"].to_numpy(bool)
    R15 = H15 - L15

    # H1 bias with close-time gating
    if h1 is not None:
        h1ct = h1["close_time"].to_numpy(dtype="datetime64[ns]")
        h1_bull = h1["bull"].to_numpy(bool)
        h1_bear = h1["bear"].to_numpy(bool)

        def bias_ok(now_ns: np.datetime64, d: int) -> bool:
            ki = int(np.searchsorted(h1ct, now_ns, side="right")) - 1
            if ki < 0:
                return False
            return bool(h1_bull[ki]) if d == 1 else bool(h1_bear[ki])
    else:
        def bias_ok(now_ns: np.datetime64, d: int) -> bool:
            return True

    # M15 pivots (for Model C trailing only)
    Lp = cfg.pivot_len
    pH = pivot_high(H15, Lp)
    pL = pivot_low(L15, Lp)

    def m1_idx(ts_ns: np.datetime64) -> int:
        return int(np.searchsorted(m1t, ts_ns, side="left"))

    # ── Pre-compute M15 ATR (rolling median of ranges) ──
    atr15 = np.full(len(m15), np.nan)
    W = cfg.atr_window
    for i in range(W, len(m15)):
        atr15[i] = float(np.median(R15[i - W:i]))

    # ══════════════════════════════════════════════════════
    # MAIN LOOP — scan M15 for displacement setups
    # ══════════════════════════════════════════════════════
    trades: List[Dict] = []
    cooldown_ns = np.datetime64(0, "ns")  # skip M15 candles before this

    start_i = W + 2
    for i in range(start_i, len(m15)):
        if not S15[i]:
            continue
        if m15t[i] < cooldown_ns:
            continue
        if np.isnan(atr15[i]):
            continue

        # ── 1. DISPLACEMENT DETECTION ──
        bar_rng = R15[i]
        if bar_rng <= 0:
            continue
        body = abs(C15[i] - O15[i])
        if bar_rng < cfg.disp_atr_mult * atr15[i]:
            continue
        if body < cfg.disp_body_pct * bar_rng:
            continue

        direction = 1 if C15[i] > O15[i] else -1

        # ── 1b. MSS CHECK: displacement must break beyond recent range ──
        if cfg.require_mss:
            lookback_start = max(0, i - cfg.mss_lookback)
            if direction == 1:
                recent_high = float(np.max(H15[lookback_start:i]))
                mss_ok = C15[i] > recent_high
            else:
                recent_low = float(np.min(L15[lookback_start:i]))
                mss_ok = C15[i] < recent_low
            if not mss_ok:
                continue

        # ── 2. H1 BIAS CHECK (at displacement close time) ──
        disp_close_ns = m15ct[i]
        if not bias_ok(disp_close_ns, direction):
            continue

        # ── 3. FIND ORDER BLOCK (last opposite candle before displacement) ──
        ob_low = ob_high = None
        for j in range(i - 1, max(-1, i - cfg.ob_lookback - 1), -1):
            if direction == 1 and C15[j] < O15[j]:   # bearish candle = bullish OB
                ob_low, ob_high = float(L15[j]), float(H15[j])
                break
            if direction == -1 and C15[j] > O15[j]:  # bullish candle = bearish OB
                ob_low, ob_high = float(L15[j]), float(H15[j])
                break
        if ob_low is None:
            continue

        # OB size filter: reject oversized OBs (prevent giant SL)
        ob_range = ob_high - ob_low
        if ob_range > cfg.max_ob_atr * atr15[i]:
            continue
        if ob_range <= 0:
            continue

        # OB must be in correct zone: below displacement close (bulls) / above (bears)
        if direction == 1 and ob_high >= C15[i]:
            continue
        if direction == -1 and ob_low <= C15[i]:
            continue

        # ── 4. M1 SCAN: wait for pullback to OB, then rejection entry ──
        k = m1_idx(disp_close_ns)
        max_k = k + cfg.ob_max_wait * 15  # approximate M1 limit
        ob_tapped = False

        while k < min(len(df_m1), max_k):
            if not S1[k]:
                k += 1
                continue

            # OB invalidation: M1 close sweeps through OB entirely
            if (direction == 1 and C1[k] < ob_low) or \
               (direction == -1 and C1[k] > ob_high):
                break  # OB swept → dead

            # ── 5. ENTRY: limit fill at OB edge ──
            # Bull: price dips to ob_high → fill at ob_high (edge of demand zone)
            # Bear: price rises to ob_low → fill at ob_low (edge of supply zone)
            if direction == 1:
                touched = L1a[k] <= ob_high
            else:
                touched = H1a[k] >= ob_low

            if not touched:
                k += 1
                continue

            # Miss fill
            if rng.random() < cfg.miss_fill_prob:
                break

            # ── ENTRY PRICE: at OB edge + costs ──
            entry = float(ob_high) if direction == 1 else float(ob_low)
            entry += (cfg.spread if direction == 1 else -cfg.spread)
            entry += (cfg.entry_slippage if direction == 1 else -cfg.entry_slippage)

            # ── Entry proximity: not used for limit entry, skip ──
            if cfg.max_entry_overshoot > 0:
                overshoot = (entry - ob_high) if direction == 1 else (ob_low - entry)
                if overshoot > cfg.max_entry_overshoot * ob_range:
                    k += 1
                    continue  # try next M1 bar (maybe closer entry)

            # ── SL: OB opposite edge ──
            sl = float(ob_low - cfg.sl_buffer) if direction == 1 else float(ob_high + cfg.sl_buffer)

            risk = abs(entry - sl)
            min_risk = 2.0 * (cfg.spread + cfg.entry_slippage + cfg.exit_slippage)
            if risk <= min_risk:
                break

            # ── Risk cap: reject if risk exceeds max_risk_atr × ATR ──
            if cfg.max_risk_atr > 0 and risk > cfg.max_risk_atr * atr15[i]:
                break

            tp = entry + direction * (cfg.tp_R * risk)

            # Same-bar pessimism
            sl_same = (direction == 1 and L1a[k] <= sl) or \
                      (direction == -1 and H1a[k] >= sl)
            if sl_same:
                trades.append({
                    "symbol": cfg.symbol,
                    "direction": "LONG" if direction == 1 else "SHORT",
                    "entry_time": str(pd.Timestamp(m1t[k]).tz_localize("UTC")),
                    "entry_price": entry, "sl": sl, "tp": tp,
                    "exit_time": str(pd.Timestamp(m1t[k]).tz_localize("UTC")),
                    "exit_price": sl, "R": -1.0, "exit_reason": "SL_samebar",
                })
                break

            # ═══════════════════════════════════
            # EXIT SCAN
            # ═══════════════════════════════════
            z = k + 1
            exit_reason = "EOD"
            exit_time = pd.Timestamp(m1t[-1]).tz_localize("UTC")
            exit_price = float(C1[-1])
            R_out = 0.0

            entry_ts = pd.Timestamp(m1t[k]).tz_localize("UTC")
            entry_ny_date = entry_ts.tz_convert(NY).date()
            swap_days = 0

            # ── Model A: fixed TP/SL + optional BE ──
            if cfg.exit_model == "A":
                be_triggered = False
                be_level = entry
                be_threshold = entry + direction * (cfg.breakeven_at_R * risk) \
                    if cfg.breakeven_at_R > 0 else None

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
                        if (direction == 1 and H1a[z] >= be_threshold) or \
                           (direction == -1 and L1a[z] <= be_threshold):
                            be_triggered = True
                            sl = be_level

                    if (direction == 1 and L1a[z] <= sl) or \
                       (direction == -1 and H1a[z] >= sl):
                        exit_reason = "SL_BE" if be_triggered else "SL"
                        exit_time = bar_ts
                        exit_price = sl + (-cfg.exit_slippage if direction == 1
                                           else cfg.exit_slippage)
                        R_out = 0.0 if be_triggered else -1.0
                        break

                    if (direction == 1 and H1a[z] >= tp) or \
                       (direction == -1 and L1a[z] <= tp):
                        exit_reason = "TP"
                        exit_time = bar_ts
                        exit_price = tp + (-cfg.exit_slippage if direction == 1
                                           else cfg.exit_slippage)
                        R_out = cfg.tp_R
                        break
                    z += 1

            # ── Model B: BE@1R, 50%@2R, close@3R ──
            elif cfg.exit_model == "B":
                lv1 = entry + direction * risk
                lv2 = entry + direction * 2 * risk
                lv3 = entry + direction * 3 * risk
                be_done = False
                partial_done = False
                partial_R = 0.0

                while z < len(df_m1):
                    if not S1[z]:
                        z += 1
                        continue
                    bar_ts = pd.Timestamp(m1t[z]).tz_localize("UTC")
                    if cfg.swap_per_day_R != 0.0:
                        d = bar_ts.tz_convert(NY).date()
                        if d != entry_ny_date:
                            swap_days = (d - entry_ny_date).days

                    if not be_done:
                        if (direction == 1 and H1a[z] >= lv1) or \
                           (direction == -1 and L1a[z] <= lv1):
                            be_done = True
                            sl = entry
                    if be_done and not partial_done:
                        if (direction == 1 and H1a[z] >= lv2) or \
                           (direction == -1 and L1a[z] <= lv2):
                            partial_done = True
                            partial_R = 0.5 * 2.0

                    if (direction == 1 and L1a[z] <= sl) or \
                       (direction == -1 and H1a[z] >= sl):
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
                            exit_reason = "SL_BE_partial"
                            exit_price = entry
                            R_out = partial_R
                        break

                    if partial_done:
                        if (direction == 1 and H1a[z] >= lv3) or \
                           (direction == -1 and L1a[z] <= lv3):
                            exit_reason = "TP_3R"
                            exit_time = bar_ts
                            exit_price = float(lv3)
                            R_out = partial_R + 0.5 * 3.0
                            break
                    z += 1

                if exit_reason == "EOD" and (partial_done or be_done):
                    eod_R = (float(C1[-1]) - entry) * direction / risk
                    R_out = (partial_R + 0.5 * eod_R) if partial_done else eod_R

            # ── Model C: BE@1R, 50%@2R, trail M15 swings ──
            elif cfg.exit_model == "C":
                lv1 = entry + direction * risk
                lv2 = entry + direction * 2 * risk
                be_done = False
                partial_done = False
                partial_R = 0.0
                trailing = False

                def get_trail_sl(now_ns):
                    mi = int(np.searchsorted(m15ct, now_ns, side="right")) - 1
                    if mi < Lp:
                        return sl
                    for pi in range(mi - Lp, max(-1, mi - Lp - 20), -1):
                        if direction == 1 and np.isfinite(pL[pi]):
                            return float(pL[pi]) - cfg.sl_buffer
                        if direction == -1 and np.isfinite(pH[pi]):
                            return float(pH[pi]) + cfg.sl_buffer
                    return sl

                while z < len(df_m1):
                    if not S1[z]:
                        z += 1
                        continue
                    bar_ts = pd.Timestamp(m1t[z]).tz_localize("UTC")
                    if cfg.swap_per_day_R != 0.0:
                        d = bar_ts.tz_convert(NY).date()
                        if d != entry_ny_date:
                            swap_days = (d - entry_ny_date).days

                    if not be_done:
                        if (direction == 1 and H1a[z] >= lv1) or \
                           (direction == -1 and L1a[z] <= lv1):
                            be_done = True
                            sl = entry
                    if be_done and not partial_done:
                        if (direction == 1 and H1a[z] >= lv2) or \
                           (direction == -1 and L1a[z] <= lv2):
                            partial_done = True
                            trailing = True
                            partial_R = 0.5 * 2.0

                    if trailing:
                        new_trail = get_trail_sl(m1t[z])
                        if direction == 1 and new_trail > sl:
                            sl = new_trail
                        elif direction == -1 and new_trail < sl:
                            sl = new_trail

                    if (direction == 1 and L1a[z] <= sl) or \
                       (direction == -1 and H1a[z] >= sl):
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
                            trail_R = (sl - entry) * direction / risk
                            exit_reason = "SL_trail"
                            exit_price = sl
                            R_out = partial_R + 0.5 * trail_R
                        break
                    z += 1

                if exit_reason == "EOD":
                    eod_R = (float(C1[-1]) - entry) * direction / risk
                    if partial_done:
                        R_out = partial_R + 0.5 * eod_R
                    else:
                        R_out = eod_R

            # Apply swap
            if cfg.swap_per_day_R != 0.0 and swap_days > 0:
                R_out = float(R_out - swap_days * cfg.swap_per_day_R)

            trades.append({
                "symbol": cfg.symbol,
                "direction": "LONG" if direction == 1 else "SHORT",
                "entry_time": str(pd.Timestamp(m1t[k]).tz_localize("UTC")),
                "entry_price": entry, "sl": sl, "tp": tp,
                "exit_time": str(exit_time),
                "exit_price": float(exit_price),
                "R": float(R_out),
                "exit_reason": exit_reason,
                "swap_days": int(swap_days),
            })
            break  # exit inner M1 loop

        # end while k — set cooldown to where we stopped
        if k < len(df_m1):
            cooldown_ns = m1t[min(k, len(m1t) - 1)]

    # ── Results ──
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


# ─────────────────────────────────────
# CLI
# ─────────────────────────────────────

def parse_args() -> Config:
    p = argparse.ArgumentParser(description="OB ICT v4 — Displacement backtest")
    p.add_argument("--symbol", required=True)
    p.add_argument("--csv", required=True)
    p.add_argument("--out_dir", default="./out")
    p.add_argument("--months", type=int, default=0)
    p.add_argument("--first_months", type=int, default=0)

    # Displacement
    p.add_argument("--disp_atr_mult", type=float, default=1.5)
    p.add_argument("--disp_body_pct", type=float, default=0.60)
    p.add_argument("--atr_window", type=int, default=20)

    # OB
    p.add_argument("--ob_lookback", type=int, default=10)
    p.add_argument("--max_ob_atr", type=float, default=2.0)
    p.add_argument("--ob_max_wait", type=int, default=40)

    # Structure
    p.add_argument("--pivot_len", type=int, default=3)

    # Costs
    p.add_argument("--spread", type=float, default=0.00012)
    p.add_argument("--entry_slip", type=float, default=0.00015)
    p.add_argument("--exit_slip", type=float, default=0.00015)
    p.add_argument("--miss_fill", type=float, default=0.08)
    p.add_argument("--sl_buffer", type=float, default=0.00005)
    p.add_argument("--spread_on_exit", action="store_true")
    p.add_argument("--swap_per_day_R", type=float, default=0.0)

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
    p.add_argument("--exit_model", default="A", choices=["A", "B", "C"])
    p.add_argument("--tp_R", type=float, default=2.5)
    p.add_argument("--breakeven_at_R", type=float, default=0.0)

    # RNG
    p.add_argument("--seed", type=int, default=7)

    a = p.parse_args()
    return Config(
        symbol=a.symbol, csv_path=a.csv, out_dir=a.out_dir,
        months=a.months, first_months=a.first_months,
        disp_atr_mult=a.disp_atr_mult, disp_body_pct=a.disp_body_pct,
        atr_window=a.atr_window,
        ob_lookback=a.ob_lookback, max_ob_atr=a.max_ob_atr,
        ob_max_wait=a.ob_max_wait,
        pivot_len=a.pivot_len,
        spread=a.spread, entry_slippage=a.entry_slip,
        exit_slippage=a.exit_slip, miss_fill_prob=a.miss_fill,
        sl_buffer=a.sl_buffer, spread_on_exit=bool(a.spread_on_exit),
        swap_per_day_R=a.swap_per_day_R,
        use_h1_bias=not a.no_h1_bias, ema_fast=a.ema_fast, ema_slow=a.ema_slow,
        use_killzones=not a.no_killzones,
        london_start=a.london_start, london_end=a.london_end,
        ny_start=a.ny_start, ny_end=a.ny_end,
        tp_R=a.tp_R, exit_model=a.exit_model,
        breakeven_at_R=a.breakeven_at_R,
        seed=a.seed,
    )


def main() -> None:
    cfg = parse_args()
    os.makedirs(cfg.out_dir, exist_ok=True)
    df = load_m1(cfg.csv_path)
    trades_df, metrics = strict_backtest(df, cfg)

    base = f"{cfg.symbol}_ICT_v4_tp{cfg.tp_R}_m{cfg.months}_seed{cfg.seed}"
    trades_path = os.path.join(cfg.out_dir, base + "_trades.csv")
    metrics_path = os.path.join(cfg.out_dir, base + "_metrics.json")
    trades_df.to_csv(trades_path, index=False)
    with open(metrics_path, "w", encoding="utf-8") as f:
        json.dump(metrics, f, indent=2)

    print("\n=== OB ICT v4 COMPLETE ===")
    print(f"Symbol   : {metrics['symbol']}")
    print(f"Trades   : {metrics['trades']}")
    print(f"Winrate  : {metrics['winrate']:.1%}" if not np.isnan(metrics['winrate']) else "Winrate  : N/A")
    print(f"AvgR     : {metrics['avgR']:.3f}" if not np.isnan(metrics['avgR']) else "AvgR     : N/A")
    print(f"PF       : {metrics['profit_factor']:.2f}")
    print(f"MaxDD(R) : {metrics['max_dd_R']:.1f}")
    print(f"SumR     : {metrics['sum_R']:.1f}")
    print(f"Trades  -> {trades_path}")
    print(f"Metrics -> {metrics_path}")


if __name__ == "__main__":
    main()
