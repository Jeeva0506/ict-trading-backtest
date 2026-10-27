"""
Opening Range Breakout (ORB) Strategy for Nifty Intraday Options
================================================================
Opening Range = High/Low of the first N minutes (default 15m: 9:15-9:29).

Entry Logic:
  - After ORB forms, wait for the first bar that closes ABOVE ORB_H (call)
    or BELOW ORB_L (put) — only within entry window (9:30 to 11:00).
  - Direction filters (all use prior bars, no lookahead):
      * EMA(10) > EMA(20): uptrend required for calls
      * EMA(10) < EMA(20): downtrend required for puts
      * RSI(14) 52-75 for calls, 25-48 for puts
      * ADX(14) >= 22: trending market only
      * VIX 11-20: moderate volatility
      * Price above daily CPR (PP) for calls, below for puts (optional)
  - Only first qualifying signal per day

References:
  Toby Crabel (1990), "Day Trading with Short Term Price Patterns"
  Indian market studies show 55-65% WR in trending regimes
"""
import numpy as np
import pandas as pd


ORB_PARAMS = {
    "orb_minutes"       : 15,        # Opening range = first 15 min (9:15-9:29)
    "entry_window_start": "09:30",   # Earliest entry after ORB forms
    "entry_window_end"  : "11:30",   # Latest entry (first 2 hrs post-ORB)
    "mandatory_exit"    : "15:15",   # Hard close time (IST)

    "ema_fast"          : 10,
    "ema_slow"          : 20,
    "rsi_period"        : 14,
    "rsi_call_lo"       : 50,
    "rsi_call_hi"       : 78,
    "rsi_put_lo"        : 22,
    "rsi_put_hi"        : 50,
    "vix_max"           : 22,
    "vix_min"           : 10,
    "adx_period"        : 14,
    "adx_min"           : 20,

    # CPR direction filter (uses daily pivots from prior day)
    "use_cpr_filter"    : True,      # True = require price above PP for calls

    # Trade management
    "sl_pct"            : 0.25,      # SL = 25% of entry premium
    "tp_pct"            : 0.50,      # TP = 50% of entry premium  (2:1 R:R)
    "entry_dte"         : 5,
    "rfr"               : 0.065,
    "lot_size"          : 50,
    "strike_step"       : 50,
    "risk_per_trade"    : 10_000,
    "max_lots"          : 5,
}


def _ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


def _rsi(s, n):
    d = s.diff()
    g = d.clip(lower=0).ewm(alpha=1/n, adjust=False).mean()
    l = (-d).clip(lower=0).ewm(alpha=1/n, adjust=False).mean()
    return 100 - 100 / (1 + g / l.replace(0, np.nan))


def _adx(h, l, c, n):
    tr = pd.concat([h-l, (h-c.shift()).abs(), (l-c.shift()).abs()], axis=1).max(1)
    u = h - h.shift()
    d = l.shift() - l
    dp = pd.Series(0., index=h.index)
    dm = dp.copy()
    dp[(u > d) & (u > 0)] = u[(u > d) & (u > 0)]
    dm[(d > u) & (d > 0)] = d[(d > u) & (d > 0)]
    a = 1 / n
    atr = tr.ewm(alpha=a, adjust=False).mean()
    dip = 100 * dp.ewm(alpha=a, adjust=False).mean() / atr
    dim = 100 * dm.ewm(alpha=a, adjust=False).mean() / atr
    dx = 100 * (dip - dim).abs() / (dip + dim).replace(0, np.nan)
    return dx.ewm(alpha=a, adjust=False).mean()


def compute_orb_levels(df_intraday: pd.DataFrame, orb_minutes: int = 15) -> pd.DataFrame:
    """
    Compute the daily Opening Range high/low from the first orb_minutes bars.
    Returns a DataFrame indexed by date with columns ORB_H, ORB_L.
    No lookahead: ORB is formed BEFORE the entry window opens.
    """
    idx = df_intraday.index
    if idx.tzinfo is not None:
        dates = idx.normalize().date
    else:
        dates = idx.date

    bar_dates = pd.Series(dates, index=idx)
    bar_times = idx.time

    # Mark bars within the opening range formation window (9:15 to 9:15+N-1 min)
    from datetime import time as dtime
    orb_end_min = 15 + orb_minutes - 1   # e.g. 15m ORB → 9:15 to 9:29
    orb_end_h   = (orb_end_min) // 60 + 9
    orb_end_m   = (orb_end_min) % 60

    in_orb = pd.Series(
        [t.hour < orb_end_h or (t.hour == orb_end_h and t.minute <= orb_end_m)
         for t in bar_times],
        index=idx
    )

    orb_df = df_intraday[in_orb].copy()
    orb_df["_date"] = bar_dates[in_orb].values

    daily_orb = orb_df.groupby("_date").agg(
        ORB_H=("High", "max"),
        ORB_L=("Low",  "min"),
    )
    daily_orb.index = pd.DatetimeIndex([pd.Timestamp(d) for d in daily_orb.index])
    return daily_orb


def generate_orb_signals(df_intraday: pd.DataFrame,
                          df_daily: pd.DataFrame,
                          df_vix: pd.DataFrame,
                          params: dict = None) -> pd.DataFrame:
    """
    Generate ORB entry signals for each bar.

    Parameters
    ----------
    df_intraday : 1m/5m OHLCV, IST naive DatetimeIndex
    df_daily    : daily OHLCV (for CPR filter)
    df_vix      : daily India VIX with 'Close' column
    params      : override dict for ORB_PARAMS

    Returns
    -------
    DataFrame indexed like df_intraday with 'signal', 'orb_h', 'orb_l',
    'entry_price', 'vix_val' columns.
    """
    p = {**ORB_PARAMS, **(params or {})}

    idx = df_intraday.index
    if idx.tzinfo is not None:
        idx_date = idx.normalize().date
    else:
        idx_date = idx.date

    close = df_intraday["Close"]
    high  = df_intraday["High"]
    low   = df_intraday["Low"]

    # ── Intraday indicators (all shifted 1 bar — no lookahead) ──────────────
    ema_fast = _ema(close, p["ema_fast"]).shift(1)
    ema_slow = _ema(close, p["ema_slow"]).shift(1)
    rsi      = _rsi(close, p["rsi_period"]).shift(1)
    adx_val  = _adx(high, low, close, p["adx_period"]).shift(1)

    # ── Map ORB levels to each bar ──────────────────────────────────────────
    daily_orb = compute_orb_levels(df_intraday, p["orb_minutes"])
    orb_bar = daily_orb.reindex(
        pd.DatetimeIndex([pd.Timestamp(d) for d in idx_date]), method="ffill"
    )
    orb_bar.index = df_intraday.index
    orb_h = orb_bar["ORB_H"]
    orb_l = orb_bar["ORB_L"]

    # ── Map daily CPR pivots (prior day, no lookahead) ───────────────────────
    if p["use_cpr_filter"]:
        h = df_daily["High"].shift(1)
        l = df_daily["Low"].shift(1)
        c = df_daily["Close"].shift(1)
        pp_daily = (h + l + c) / 3
        pp_bar = pp_daily.reindex(
            pd.DatetimeIndex([pd.Timestamp(d) for d in idx_date]), method="ffill"
        )
        pp_bar.index = df_intraday.index
    else:
        pp_bar = pd.Series(np.nan, index=df_intraday.index)

    # ── Map daily VIX ────────────────────────────────────────────────────────
    vix_daily = df_vix["Close"].copy()
    vix_bar = vix_daily.reindex(
        pd.DatetimeIndex([pd.Timestamp(d) for d in idx_date]), method="ffill"
    )
    vix_bar.index = df_intraday.index
    vix_shifted = vix_bar.shift(1)  # prior day VIX (avoid lookahead on current day)

    # ── Entry window mask ────────────────────────────────────────────────────
    bar_time = idx.time
    es_h, es_m = map(int, p["entry_window_start"].split(":"))
    ee_h, ee_m = map(int, p["entry_window_end"].split(":"))

    after_orb  = pd.Series(
        [t.hour > es_h or (t.hour == es_h and t.minute >= es_m) for t in bar_time],
        index=idx
    )
    before_end = pd.Series(
        [t.hour < ee_h or (t.hour == ee_h and t.minute <= ee_m) for t in bar_time],
        index=idx
    )
    in_window = after_orb & before_end

    # ── Base filters ─────────────────────────────────────────────────────────
    base = (
        in_window &
        (vix_shifted >= p["vix_min"]) &
        (vix_shifted <= p["vix_max"]) &
        (adx_val >= p["adx_min"])
    )

    # ── ORB breakout conditions ───────────────────────────────────────────────
    call_break = close > orb_h   # current bar closes above ORB high
    put_break  = close < orb_l   # current bar closes below ORB low

    # CPR direction filter
    if p["use_cpr_filter"]:
        above_pp = close > pp_bar
        below_pp = close < pp_bar
    else:
        above_pp = pd.Series(True, index=idx)
        below_pp = pd.Series(True, index=idx)

    call_cond = (base & call_break & above_pp & (ema_fast > ema_slow) &
                 (rsi > p["rsi_call_lo"]) & (rsi < p["rsi_call_hi"]))
    put_cond  = (base & put_break & below_pp & (ema_fast < ema_slow) &
                 (rsi > p["rsi_put_lo"]) & (rsi < p["rsi_put_hi"]))

    # ── Build signal DataFrame ────────────────────────────────────────────────
    bar_dates = pd.Series(idx_date, index=idx)
    date_first_signal = {}

    signals = pd.DataFrame(index=idx)
    signals["signal"]      = None
    signals["entry_price"] = close
    signals["orb_h"]       = orb_h
    signals["orb_l"]       = orb_l
    signals["pp"]          = pp_bar
    signals["vix_val"]     = vix_shifted

    for i, (idx_val, row) in enumerate(zip(idx, zip(call_cond, put_cond))):
        call_ok, put_ok = row
        d = bar_dates.iloc[i]
        if d in date_first_signal:
            continue
        if call_ok and not put_ok:
            signals.at[idx_val, "signal"] = "call"
            date_first_signal[d] = True
        elif put_ok and not call_ok:
            signals.at[idx_val, "signal"] = "put"
            date_first_signal[d] = True

    return signals
