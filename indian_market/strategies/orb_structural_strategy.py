"""
Structural ORB Strategy — Nifty Options
========================================
Builds on the best Enhanced ORB combo (fvg+sweep+bar_quality+ema_slope) with:

Structure filters (candle quality):
  body_strength    — body ≥ 40% of range AND close in top/bottom 60% of bar
  momentum_candle  — breakout bar range > 20-bar rolling avg (conviction move)

Math/regime filters:
  vix_percentile   — VIX in 20th–80th pct of trailing 252-day window
  atr              — tightened: ORB range 0.20–0.60× daily ATR14 (was 0.25–2.5)

Carry-over (proven alpha): fvg, sweep, bar_quality, ema_slope

Exit: Trailing SL handled in runner — not in signal generator.
All filters use prior bar data only (no lookahead).
"""
import numpy as np
import pandas as pd
from strategies.orb_enhanced_strategy import (
    _ema, _rsi, _adx, _atr_daily, _orb_levels, _compute_h1_ema,
    _fvg_flags, _daily_fvg_seen, _daily_sweep_flags, ENHANCED_PARAMS,
)


STRUCTURAL_PARAMS = {
    **ENHANCED_PARAMS,

    # Widen absolute VIX bounds — percentile filter handles regime selection
    "vix_min": 8,
    "vix_max": 35,

    # Tighter ATR ratio (was 0.25–2.5; filters choppy/gap days more precisely)
    "atr_min_mult": 0.20,
    "atr_max_mult": 0.60,

    # Trailing SL (used by runner, not signal generator)
    "sl_pct": 0.25,              # initial hard SL
    "tp_pct": 5.00,              # safety net only — trailing SL handles exits
    "trail_pct": 0.20,           # trail 20% below peak premium
    "trail_activate_pct": 0.10,  # activate trail once up 10% from entry
    "use_trail_sl": True,

    # New filter flags
    "use_body_strength": True,
    "body_strength_min": 0.40,   # body ≥ 40% of range
    "body_close_pct": 0.60,      # close in top 60% (call) / bottom 40% (put)

    "use_vix_percentile": True,
    "vix_pct_lo": 0.20,
    "vix_pct_hi": 0.80,
    "vix_pct_window": 252,

    "use_momentum_candle": True,
    "momentum_candle_mult": 1.0,  # range >= 1.0× rolling avg (above average)
    "momentum_candle_bars": 20,
}


def generate_structural_signals(
        df_intraday: pd.DataFrame,
        df_daily: pd.DataFrame,
        df_vix: pd.DataFrame,
        params: dict = None,
        active_filters: set = None,
) -> pd.DataFrame:
    """
    Generate structural ORB signals.

    active_filters : set of filter names to enable.
                     Recognised names (old): fvg, sweep, htf_ema, atr, bar_quality, ema_slope
                     New names: body_strength, vix_percentile, momentum_candle
                     None = use all flags in params.
    """
    p   = {**STRUCTURAL_PARAMS, **(params or {})}
    idx = df_intraday.index

    def _on(name: str) -> bool:
        if active_filters is not None:
            return name in active_filters
        return p.get(f"use_{name}_filter", p.get(f"use_{name}", False))

    bar_dates = pd.Series(idx.date, index=idx)
    close = df_intraday["Close"]
    high  = df_intraday["High"]
    low   = df_intraday["Low"]
    open_ = df_intraday["Open"]

    # ── Intraday indicators (all shifted 1 bar — no lookahead) ──────────────
    ema_fast_raw = _ema(close, p["ema_fast"])
    ema_fast     = ema_fast_raw.shift(1)
    ema_slow     = _ema(close, p["ema_slow"]).shift(1)
    rsi          = _rsi(close, p["rsi_period"]).shift(1)
    adx_val      = _adx(high, low, close, p["adx_period"]).shift(1)

    n_slope      = p.get("ema_slope_bars", 3)
    ema_rising   = ema_fast_raw.shift(1) > ema_fast_raw.shift(1 + n_slope)
    ema_falling  = ema_fast_raw.shift(1) < ema_fast_raw.shift(1 + n_slope)

    # ── ORB levels ────────────────────────────────────────────────────────────
    daily_orb = _orb_levels(df_intraday, p["orb_minutes"])
    _dates_idx = pd.DatetimeIndex([pd.Timestamp(d) for d in idx.date])

    orb_h = daily_orb["ORB_H"].reindex(_dates_idx, method="ffill")
    orb_h.index = idx
    orb_l = daily_orb["ORB_L"].reindex(_dates_idx, method="ffill")
    orb_l.index = idx

    # ── Candle geometry (current bar — no shift needed for entry-bar filters) ─
    bar_range = (high - low).replace(0, np.nan)
    body      = (close - open_).abs()
    body_pct  = body / bar_range            # 0–1, fraction that is body
    close_pct = (close - low) / bar_range   # 0=bottom, 1=top

    # ── ATR range filter ─────────────────────────────────────────────────────
    if _on("atr"):
        daily_atr14 = _atr_daily(df_daily).shift(1)
        atr_bar = daily_atr14.reindex(_dates_idx, method="ffill")
        atr_bar.index = idx
        orb_range = orb_h - orb_l
        atr_ok = (
            (orb_range >= p["atr_min_mult"] * atr_bar) &
            (orb_range <= p["atr_max_mult"] * atr_bar)
        )
    else:
        atr_ok = pd.Series(True, index=idx)

    # ── CPR pivot ────────────────────────────────────────────────────────────
    if p.get("use_cpr_filter", True):
        pp = (df_daily["High"].shift(1) + df_daily["Low"].shift(1) + df_daily["Close"].shift(1)) / 3
        pp_bar = pp.reindex(_dates_idx, method="ffill")
        pp_bar.index = idx
    else:
        pp_bar = pd.Series(np.nan, index=idx)

    # ── VIX (raw value for absolute bounds check) ────────────────────────────
    vix_bar     = df_vix["Close"].reindex(_dates_idx, method="ffill")
    vix_bar.index = idx
    vix_shifted = vix_bar.shift(1)

    # ── VIX percentile filter ────────────────────────────────────────────────
    if _on("vix_percentile"):
        w = p.get("vix_pct_window", 252)
        # rank current day's VIX among the prior w days (no lookahead)
        vix_pct_rank = df_vix["Close"].rolling(w + 1).apply(
            lambda x: (x[:-1] < x[-1]).mean(), raw=True
        ).shift(1)
        vix_pct_bar = vix_pct_rank.reindex(_dates_idx, method="ffill")
        vix_pct_bar.index = idx
        vix_pct_ok = (
            vix_pct_bar.notna() &
            (vix_pct_bar >= p.get("vix_pct_lo", 0.20)) &
            (vix_pct_bar <= p.get("vix_pct_hi", 0.80))
        )
    else:
        vix_pct_ok = pd.Series(True, index=idx)

    # ── HTF EMA ──────────────────────────────────────────────────────────────
    if _on("htf_ema"):
        h1_ema   = _compute_h1_ema(df_intraday, p["htf_ema_period"])
        h1_close = close.resample("60min", closed="left", label="left").last().dropna()
        htf_bull = (h1_close > h1_ema).shift(1).reindex(idx, method="ffill").fillna(False)
        htf_bear = (h1_close < h1_ema).shift(1).reindex(idx, method="ffill").fillna(False)
    else:
        htf_bull = pd.Series(True, index=idx)
        htf_bear = pd.Series(True, index=idx)

    # ── FVG ──────────────────────────────────────────────────────────────────
    if _on("fvg"):
        bull_fvg_raw, bear_fvg_raw = _fvg_flags(df_intraday)
        fvg_bull_seen, fvg_bear_seen = _daily_fvg_seen(bull_fvg_raw, bear_fvg_raw, df_intraday)
    else:
        fvg_bull_seen = pd.Series(True, index=idx)
        fvg_bear_seen = pd.Series(True, index=idx)

    # ── Liquidity sweep ──────────────────────────────────────────────────────
    if _on("sweep"):
        swept_low, swept_high = _daily_sweep_flags(df_intraday, orb_h, orb_l)
    else:
        swept_low  = pd.Series(True, index=idx)
        swept_high = pd.Series(True, index=idx)

    # ── Entry window ─────────────────────────────────────────────────────────
    es_h, es_m = map(int, p["entry_window_start"].split(":"))
    ee_h, ee_m = map(int, p["entry_window_end"].split(":"))
    in_window = pd.Series(
        [(t.hour > es_h or (t.hour == es_h and t.minute >= es_m)) and
         (t.hour < ee_h or (t.hour == ee_h and t.minute <= ee_m))
         for t in idx.time],
        index=idx,
    )

    # ── Bar quality (close in upper/lower half) ───────────────────────────────
    bq_pct = p.get("bar_quality_pct", 0.50)
    if _on("bar_quality"):
        call_bq = close_pct >= bq_pct
        put_bq  = close_pct <= (1 - bq_pct)
    else:
        call_bq = pd.Series(True, index=idx)
        put_bq  = pd.Series(True, index=idx)

    # ── Body strength + close position (structural candle) ───────────────────
    bsm = p.get("body_strength_min", 0.40)
    bcp = p.get("body_close_pct", 0.60)
    if _on("body_strength"):
        call_body = (body_pct >= bsm) & (close_pct >= bcp)
        put_body  = (body_pct >= bsm) & (close_pct <= (1.0 - bcp))
    else:
        call_body = pd.Series(True, index=idx)
        put_body  = pd.Series(True, index=idx)

    # ── Momentum candle (above-average range) ────────────────────────────────
    if _on("momentum_candle"):
        mc_bars  = p.get("momentum_candle_bars", 20)
        mc_mult  = p.get("momentum_candle_mult", 1.0)
        avg_rng  = bar_range.rolling(mc_bars).mean().shift(1)
        mom_ok   = bar_range >= avg_rng * mc_mult
    else:
        mom_ok = pd.Series(True, index=idx)

    # ── EMA slope ────────────────────────────────────────────────────────────
    if _on("ema_slope"):
        call_slope = ema_rising
        put_slope  = ema_falling
    else:
        call_slope = pd.Series(True, index=idx)
        put_slope  = pd.Series(True, index=idx)

    # ── Base gate ────────────────────────────────────────────────────────────
    base = (
        in_window &
        (vix_shifted >= p["vix_min"]) &
        (vix_shifted <= p["vix_max"]) &
        (adx_val >= p["adx_min"]) &
        atr_ok &
        vix_pct_ok
    )

    # ── Breakout close confirmation ───────────────────────────────────────────
    call_break = close > orb_h
    put_break  = close < orb_l

    # CPR side
    above_pp = (close > pp_bar) if p.get("use_cpr_filter", True) else pd.Series(True, index=idx)
    below_pp = (close < pp_bar) if p.get("use_cpr_filter", True) else pd.Series(True, index=idx)

    # ── Combined signal conditions ────────────────────────────────────────────
    call_cond = (
        base & call_break & above_pp &
        (ema_fast > ema_slow) &
        (rsi > p["rsi_call_lo"]) & (rsi < p["rsi_call_hi"]) &
        htf_bull & fvg_bull_seen & swept_low &
        call_bq & call_body & mom_ok & call_slope
    )
    put_cond = (
        base & put_break & below_pp &
        (ema_fast < ema_slow) &
        (rsi > p["rsi_put_lo"]) & (rsi < p["rsi_put_hi"]) &
        htf_bear & fvg_bear_seen & swept_high &
        put_bq & put_body & mom_ok & put_slope
    )

    # ── One signal per day ────────────────────────────────────────────────────
    out = pd.DataFrame({
        "signal"     : None,
        "entry_price": close,
        "orb_h"      : orb_h,
        "orb_l"      : orb_l,
        "vix_val"    : vix_shifted,
    }, index=idx)

    seen_days: set = set()
    for i, ts in enumerate(idx):
        d = bar_dates.iloc[i]
        if d in seen_days:
            continue
        if call_cond.iloc[i] and not put_cond.iloc[i]:
            out.at[ts, "signal"] = "call"
            seen_days.add(d)
        elif put_cond.iloc[i] and not call_cond.iloc[i]:
            out.at[ts, "signal"] = "put"
            seen_days.add(d)

    return out
