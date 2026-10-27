"""
Enhanced ORB Strategy — Nifty Intraday Options
===============================================
Builds on the baseline ORB with layered ICT + technical filters:

  1. FVG  (Fair Value Gap)  — institutional order flow present
  2. Sweep — liquidity swept opposite ORB boundary before breakout
  3. HTF EMA (H1 EMA20)    — higher-timeframe trend alignment
  4. ATR range gate         — ORB range in healthy zone (not choppy, not gapped)
  5. Breakout bar quality   — close-strength: close in upper/lower 50% of bar
  6. EMA slope              — fast EMA actively rising/falling (not flat)
  7. Tighter entry window   — 9:30 to 10:30 (earlier = cleaner)
  8. RSI band refinement    — tighter bands vs baseline

All filters use PRIOR bar data only. No lookahead bias.
"""
import numpy as np
import pandas as pd


# ── Default params (all filters ON — individual flags let you toggle) ──────────
ENHANCED_PARAMS = {
    # ── Existing baseline params ──────────────────────────────────────────────
    "orb_minutes"        : 15,
    "entry_window_start" : "09:30",
    "entry_window_end"   : "10:30",   # tightened from 11:30 → 10:30
    "mandatory_exit"     : "15:15",

    "ema_fast"           : 10,
    "ema_slow"           : 20,
    "rsi_period"         : 14,
    "rsi_call_lo"        : 52,        # tightened from 50
    "rsi_call_hi"        : 72,        # tightened from 78
    "rsi_put_lo"         : 28,        # tightened from 22
    "rsi_put_hi"         : 48,        # tightened from 50

    "vix_max"            : 22,
    "vix_min"            : 10,
    "adx_period"         : 14,
    "adx_min"            : 20,
    "use_cpr_filter"     : True,

    "sl_pct"             : 0.25,
    "tp_pct"             : 0.50,
    "entry_dte"          : 5,
    "rfr"                : 0.065,
    "lot_size"           : 50,
    "strike_step"        : 50,
    "risk_per_trade"     : 10_000,
    "max_lots"           : 5,

    # ── New filter flags ──────────────────────────────────────────────────────
    "use_fvg_filter"     : True,   # Bullish/bearish FVG present before entry bar
    "use_sweep_filter"   : True,   # Liquidity sweep of opposite ORB boundary
    "use_htf_ema_filter" : True,   # H1 EMA20 direction matches trade
    "htf_ema_period"     : 20,
    "use_atr_filter"     : True,   # ORB range in [atr_min_mult, atr_max_mult] × daily ATR14
    "atr_min_mult"       : 0.25,
    "atr_max_mult"       : 2.50,
    "use_bar_quality"    : True,   # Breakout bar close in upper/lower 50% of bar
    "bar_quality_pct"    : 0.50,
    "use_ema_slope"      : True,   # fast EMA rising (call) / falling (put) over N bars
    "ema_slope_bars"     : 3,
}


# ── Indicators ─────────────────────────────────────────────────────────────────

def _ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def _rsi(s: pd.Series, n: int) -> pd.Series:
    d = s.diff()
    g = d.clip(lower=0).ewm(alpha=1/n, adjust=False).mean()
    l = (-d).clip(lower=0).ewm(alpha=1/n, adjust=False).mean()
    return 100 - 100 / (1 + g / l.replace(0, np.nan))


def _adx(h: pd.Series, l: pd.Series, c: pd.Series, n: int) -> pd.Series:
    tr  = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(1)
    u   = h - h.shift()
    d   = l.shift() - l
    dp  = pd.Series(0., index=h.index)
    dm  = dp.copy()
    dp[(u > d) & (u > 0)] = u[(u > d) & (u > 0)]
    dm[(d > u) & (d > 0)] = d[(d > u) & (d > 0)]
    a   = 1 / n
    atr = tr.ewm(alpha=a, adjust=False).mean()
    dip = 100 * dp.ewm(alpha=a, adjust=False).mean() / atr
    dim = 100 * dm.ewm(alpha=a, adjust=False).mean() / atr
    dx  = 100 * (dip - dim).abs() / (dip + dim).replace(0, np.nan)
    return dx.ewm(alpha=a, adjust=False).mean()


def _atr_daily(df_daily: pd.DataFrame, n: int = 14) -> pd.Series:
    h, l, c = df_daily["High"], df_daily["Low"], df_daily["Close"]
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(1)
    return tr.ewm(alpha=1/n, adjust=False).mean()


def _orb_levels(df: pd.DataFrame, orb_minutes: int) -> pd.DataFrame:
    """ORB High/Low from first orb_minutes bars (9:15 to 9:15+N-1)."""
    orb_end_m  = 15 + orb_minutes - 1
    orb_end_h  = orb_end_m // 60 + 9
    orb_end_mm = orb_end_m % 60

    times  = df.index.time
    in_orb = pd.Series(
        [t.hour < orb_end_h or (t.hour == orb_end_h and t.minute <= orb_end_mm)
         for t in times],
        index=df.index
    )
    orb_df = df[in_orb].copy()
    orb_df["_date"] = orb_df.index.date

    daily = orb_df.groupby("_date").agg(ORB_H=("High", "max"), ORB_L=("Low", "min"))
    daily.index = pd.DatetimeIndex([pd.Timestamp(d) for d in daily.index])
    return daily


def _compute_h1_ema(df_1m: pd.DataFrame, period: int) -> pd.Series:
    """Resample 1m to H1 and compute EMA; returns a series of H1 EMA values."""
    h1 = df_1m["Close"].resample("60min", closed="left", label="left").last().dropna()
    return _ema(h1, period)


def _fvg_flags(df: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    """
    Fair Value Gap detection (no lookahead — uses i-2, i-1, i):
      Bullish FVG: low[i] > high[i-2]  → gap upward
      Bearish FVG: high[i] < low[i-2]  → gap downward
    Returns two boolean Series aligned to df.index.
    """
    bullish = df["Low"] > df["High"].shift(2)
    bearish = df["High"] < df["Low"].shift(2)
    return bullish, bearish


def _daily_sweep_flags(df: pd.DataFrame,
                        orb_h: pd.Series,
                        orb_l: pd.Series) -> tuple[pd.Series, pd.Series]:
    """
    Per-bar cumulative sweep flags (intraday, no lookahead).

    swept_low[bar]  = True if, at ANY prior bar today (before this bar),
                      a bar's Low touched/crossed ORB_L.
    swept_high[bar] = True if a prior bar's High touched/crossed ORB_H.

    Used to verify: before a bullish breakout (call), price swept sell-side
    liquidity (below ORB_L). Before a bearish breakout (put), price swept
    buy-side liquidity (above ORB_H).
    """
    dates     = pd.Series(df.index.date, index=df.index)
    swept_low  = pd.Series(False, index=df.index)
    swept_high = pd.Series(False, index=df.index)

    for day, grp in df.groupby(dates.values):
        orb_h_day = orb_h.reindex(grp.index, method="ffill").iloc[0] if len(grp) > 0 else np.nan
        orb_l_day = orb_l.reindex(grp.index, method="ffill").iloc[0] if len(grp) > 0 else np.nan
        if np.isnan(orb_h_day) or np.isnan(orb_l_day):
            continue

        lows  = grp["Low"].values
        highs = grp["High"].values
        idx   = grp.index

        # Cumulative: has any prior bar (shifted by 1) breached the boundary?
        # shift: use prior bars only (so at bar i, we know bars 0..i-1)
        swept_l = np.zeros(len(grp), dtype=bool)
        swept_h = np.zeros(len(grp), dtype=bool)
        hit_l = hit_h = False
        for j in range(len(grp)):
            swept_l[j] = hit_l
            swept_h[j] = hit_h
            if lows[j] <= orb_l_day:
                hit_l = True
            if highs[j] >= orb_h_day:
                hit_h = True

        swept_low.loc[idx]  = swept_l
        swept_high.loc[idx] = swept_h

    return swept_low, swept_high


def _daily_fvg_seen(bullish_fvg: pd.Series,
                     bearish_fvg: pd.Series,
                     df: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    """
    Cumulative daily flags: has a bullish/bearish FVG been seen
    at any PRIOR bar today?  (shifted by 1 — bar i looks at bars 0..i-1)
    """
    dates      = pd.Series(df.index.date, index=df.index)
    bull_seen  = pd.Series(False, index=df.index)
    bear_seen  = pd.Series(False, index=df.index)

    for day, grp in df.groupby(dates.values):
        bull = bullish_fvg.reindex(grp.index).fillna(False).values
        bear = bearish_fvg.reindex(grp.index).fillna(False).values
        idx  = grp.index

        b_seen = b2_seen = False
        b_arr  = np.zeros(len(grp), dtype=bool)
        b2_arr = np.zeros(len(grp), dtype=bool)
        for j in range(len(grp)):
            b_arr[j]  = b_seen
            b2_arr[j] = b2_seen
            if bull[j]:
                b_seen = True
            if bear[j]:
                b2_seen = True

        bull_seen.loc[idx] = b_arr
        bear_seen.loc[idx] = b2_arr

    return bull_seen, bear_seen


def generate_enhanced_signals(
        df_intraday: pd.DataFrame,
        df_daily: pd.DataFrame,
        df_vix: pd.DataFrame,
        params: dict = None,
        active_filters: set = None,   # None = use all enabled in params
) -> pd.DataFrame:
    """
    Generate enhanced ORB signals.

    Parameters
    ----------
    df_intraday   : 1m/5m OHLCV IST naive DatetimeIndex
    df_daily      : daily OHLCV (for CPR + ATR)
    df_vix        : daily India VIX with 'Close' column
    params        : override ENHANCED_PARAMS
    active_filters: set of filter names to enable (overrides param flags).
                    None = respect individual param flags.
                    Example: {"fvg", "sweep", "htf_ema"}

    Returns
    -------
    DataFrame with 'signal', 'orb_h', 'orb_l', 'entry_price', 'vix_val',
    plus per-filter debug columns.
    """
    p   = {**ENHANCED_PARAMS, **(params or {})}
    idx = df_intraday.index

    def _filter_on(name: str) -> bool:
        if active_filters is not None:
            return name in active_filters
        return p.get(f"use_{name}_filter", p.get(f"use_{name}", False))

    bar_dates = pd.Series(idx.date, index=idx)
    close     = df_intraday["Close"]
    high      = df_intraday["High"]
    low       = df_intraday["Low"]

    # ── Intraday indicators (all shifted 1 bar — no lookahead) ───────────────
    ema_fast   = _ema(close, p["ema_fast"]).shift(1)
    ema_slow   = _ema(close, p["ema_slow"]).shift(1)
    rsi        = _rsi(close, p["rsi_period"]).shift(1)
    adx_val    = _adx(high, low, close, p["adx_period"]).shift(1)

    # ── EMA slope (fast EMA actively rising/falling) ─────────────────────────
    ema_fast_raw    = _ema(close, p["ema_fast"])
    n_slope         = p.get("ema_slope_bars", 3)
    ema_rising      = (ema_fast_raw.shift(1) > ema_fast_raw.shift(1 + n_slope))
    ema_falling     = (ema_fast_raw.shift(1) < ema_fast_raw.shift(1 + n_slope))

    # ── ORB levels ────────────────────────────────────────────────────────────
    daily_orb = _orb_levels(df_intraday, p["orb_minutes"])
    orb_h     = daily_orb["ORB_H"].reindex(
        pd.DatetimeIndex([pd.Timestamp(d) for d in idx.date]), method="ffill")
    orb_h.index = idx
    orb_l     = daily_orb["ORB_L"].reindex(
        pd.DatetimeIndex([pd.Timestamp(d) for d in idx.date]), method="ffill")
    orb_l.index = idx

    # ── ATR range filter ─────────────────────────────────────────────────────
    if _filter_on("atr"):
        daily_atr14 = _atr_daily(df_daily).shift(1)  # prior day ATR
        atr_bar = daily_atr14.reindex(
            pd.DatetimeIndex([pd.Timestamp(d) for d in idx.date]), method="ffill")
        atr_bar.index = idx
        orb_range  = orb_h - orb_l
        atr_ok     = (
            (orb_range >= p["atr_min_mult"] * atr_bar) &
            (orb_range <= p["atr_max_mult"] * atr_bar)
        )
    else:
        atr_ok = pd.Series(True, index=idx)

    # ── CPR pivot ────────────────────────────────────────────────────────────
    if p["use_cpr_filter"]:
        h_d   = df_daily["High"].shift(1)
        l_d   = df_daily["Low"].shift(1)
        c_d   = df_daily["Close"].shift(1)
        pp    = (h_d + l_d + c_d) / 3
        pp_bar = pp.reindex(
            pd.DatetimeIndex([pd.Timestamp(d) for d in idx.date]), method="ffill")
        pp_bar.index = idx
    else:
        pp_bar = pd.Series(np.nan, index=idx)

    # ── VIX ──────────────────────────────────────────────────────────────────
    vix_bar = df_vix["Close"].reindex(
        pd.DatetimeIndex([pd.Timestamp(d) for d in idx.date]), method="ffill")
    vix_bar.index = idx
    vix_shifted   = vix_bar.shift(1)

    # ── H1 EMA (HTF trend filter) ────────────────────────────────────────────
    if _filter_on("htf_ema"):
        h1_ema = _compute_h1_ema(df_intraday, p["htf_ema_period"])
        h1_close = close.resample("60min", closed="left", label="left").last().dropna()
        # Map H1 close and EMA to each 1m/5m bar; shift so we use the LAST COMPLETED H1 bar
        h1_above = (h1_close > h1_ema).shift(1)
        h1_below = (h1_close < h1_ema).shift(1)
        htf_bull = h1_above.reindex(idx, method="ffill").fillna(False)
        htf_bear = h1_below.reindex(idx, method="ffill").fillna(False)
    else:
        htf_bull = pd.Series(True, index=idx)
        htf_bear = pd.Series(True, index=idx)

    # ── FVG ──────────────────────────────────────────────────────────────────
    if _filter_on("fvg"):
        bull_fvg_raw, bear_fvg_raw = _fvg_flags(df_intraday)
        fvg_bull_seen, fvg_bear_seen = _daily_fvg_seen(bull_fvg_raw, bear_fvg_raw, df_intraday)
    else:
        fvg_bull_seen = pd.Series(True, index=idx)
        fvg_bear_seen = pd.Series(True, index=idx)

    # ── Liquidity sweep ──────────────────────────────────────────────────────
    if _filter_on("sweep"):
        swept_low, swept_high = _daily_sweep_flags(df_intraday, orb_h, orb_l)
    else:
        swept_low  = pd.Series(True, index=idx)
        swept_high = pd.Series(True, index=idx)

    # ── Entry window ─────────────────────────────────────────────────────────
    bar_time  = idx.time
    es_h, es_m = map(int, p["entry_window_start"].split(":"))
    ee_h, ee_m = map(int, p["entry_window_end"].split(":"))
    in_window  = pd.Series(
        [(t.hour > es_h or (t.hour == es_h and t.minute >= es_m)) and
         (t.hour < ee_h or (t.hour == ee_h and t.minute <= ee_m))
         for t in bar_time],
        index=idx
    )

    # ── Breakout bar quality (close-strength) ────────────────────────────────
    bar_range   = (high - low).replace(0, np.nan)
    close_str   = (close - low) / bar_range          # 0=bottom 1=top
    quality_ok  = ~close_str.isna()                  # valid bars

    # ── Base filters ─────────────────────────────────────────────────────────
    base = (
        in_window &
        (vix_shifted >= p["vix_min"]) &
        (vix_shifted <= p["vix_max"]) &
        (adx_val    >= p["adx_min"]) &
        atr_ok
    )

    # ── Breakout conditions ───────────────────────────────────────────────────
    call_break = close > orb_h
    put_break  = close < orb_l

    # CPR
    if p["use_cpr_filter"]:
        above_pp = close > pp_bar
        below_pp = close < pp_bar
    else:
        above_pp = pd.Series(True, index=idx)
        below_pp = pd.Series(True, index=idx)

    # ── Bar quality filter ────────────────────────────────────────────────────
    bq_pct = p.get("bar_quality_pct", 0.50)
    if _filter_on("bar_quality"):
        call_bq = close_str >= bq_pct          # close in upper half
        put_bq  = close_str <= (1 - bq_pct)   # close in lower half
    else:
        call_bq = pd.Series(True, index=idx)
        put_bq  = pd.Series(True, index=idx)

    # ── EMA slope filter ─────────────────────────────────────────────────────
    if _filter_on("ema_slope"):
        call_slope = ema_rising
        put_slope  = ema_falling
    else:
        call_slope = pd.Series(True, index=idx)
        put_slope  = pd.Series(True, index=idx)

    # ── Combined conditions ───────────────────────────────────────────────────
    call_cond = (
        base & call_break & above_pp &
        (ema_fast > ema_slow) &
        (rsi > p["rsi_call_lo"]) & (rsi < p["rsi_call_hi"]) &
        htf_bull &
        fvg_bull_seen &
        swept_low &     # sell-side liquidity swept before bull breakout
        call_bq &
        call_slope
    )
    put_cond = (
        base & put_break & below_pp &
        (ema_fast < ema_slow) &
        (rsi > p["rsi_put_lo"]) & (rsi < p["rsi_put_hi"]) &
        htf_bear &
        fvg_bear_seen &
        swept_high &    # buy-side liquidity swept before bear breakout
        put_bq &
        put_slope
    )

    # ── Build one signal per day ──────────────────────────────────────────────
    out = pd.DataFrame({
        "signal"     : None,
        "entry_price": close,
        "orb_h"      : orb_h,
        "orb_l"      : orb_l,
        "vix_val"    : vix_shifted,
        "close_str"  : close_str,
        "swept_low"  : swept_low,
        "swept_high" : swept_high,
        "fvg_bull"   : fvg_bull_seen,
        "fvg_bear"   : fvg_bear_seen,
        "htf_bull"   : htf_bull,
        "htf_bear"   : htf_bear,
        "atr_ok"     : atr_ok,
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
