"""
Intraday CPR + Pivot Breakout Strategy for Multi-Timeframe Data
================================================================
Supports 3m, 5m, 15m, 1h bar data.

Strategy design:
  1. Calculate daily CPR levels from prior day's OHLC (no lookahead)
  2. Wait for the first breakout confirmation bar on the chosen timeframe
     - Call : a bar closes ABOVE Top CPR (TC) in first 2 hours of session
     - Put  : a bar closes BELOW Bottom CPR (BC) in first 2 hours of session
  3. Additional filters (all using indicators from prior bars of same TF):
     - RSI(14) > 50 for calls, < 50 for puts
     - EMA(10) > EMA(20) for calls, < for puts (trend on that timeframe)
     - VIX <= 22 (daily VIX, prior close) — not too expensive
     - Only one entry per day (first qualifying signal)
  4. Entry: at CLOSE of the breakout bar (next bar open for fills in live)
  5. SL  : option loses 35% of entry premium
  6. TP  : option gains 70% of entry premium (2:1 R:R)
  7. Mandatory exit: 15:15 (3:15 PM) close — never hold overnight

Target frequency: 80-130 qualifying trade days per year.
Target WR: 55-62% (CPR breakout continuation rate in Indian markets)
"""
import numpy as np
import pandas as pd
from typing import Optional


INTRADAY_CPR_PARAMS = {
    "entry_window_end"  : "11:00",   # Only enter in first 1.75 hrs (tightest CPR window)
    "mandatory_exit"    : "15:15",   # Hard close time (IST)
    "ema_fast"          : 10,
    "ema_slow"          : 20,
    "rsi_period"        : 14,
    "rsi_call_lo"       : 52,
    "rsi_call_hi"       : 75,
    "rsi_put_lo"        : 25,
    "rsi_put_hi"        : 48,
    "vix_max"           : 20,        # Daily VIX cap (tighter: no chaotic markets)
    "vix_min"           : 11,
    "adx_period"        : 14,
    "adx_min"           : 22,        # Stronger trend requirement

    # CPR width filter: only trade narrow CPR days (strong S/R)
    # width_pct = (TC - BC) / PP × 100; skip if wider than threshold
    "cpr_width_max_pct" : 0.40,      # Max CPR width as % of PP (0.40 = 40 bps)

    # 2-bar confirmation: require N consecutive closes on breakout side before entry
    "confirm_bars"      : 2,         # 2 = current + prior bar both above TC (or below BC)

    # Trade management
    "sl_pct"            : 0.20,
    "tp_pct"            : 0.50,      # Wider TP → fewer hits but larger wins
    "entry_dte"         : 5,         # 5-DTE: higher gamma, bigger % swings per pt
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
    tr = pd.concat([h-l,(h-c.shift()).abs(),(l-c.shift()).abs()],axis=1).max(1)
    u=h-h.shift(); d=l.shift()-l
    dp=pd.Series(0.,index=h.index); dm=dp.copy()
    dp[(u>d)&(u>0)]=u[(u>d)&(u>0)]; dm[(d>u)&(d>0)]=d[(d>u)&(d>0)]
    a=1/n
    atr=tr.ewm(alpha=a,adjust=False).mean()
    dip=100*dp.ewm(alpha=a,adjust=False).mean()/atr
    dim=100*dm.ewm(alpha=a,adjust=False).mean()/atr
    dx=100*(dip-dim).abs()/(dip+dim).replace(0,np.nan)
    return dx.ewm(alpha=a,adjust=False).mean()


def compute_daily_pivots(df_daily: pd.DataFrame) -> pd.DataFrame:
    """
    Compute daily CPR/pivot levels from prior day's OHLC.
    Returns a DataFrame indexed by date with columns PP, TC, BC, R1, S1, R2, S2.
    All values are from PRIOR day (shift(1)) — no lookahead.
    """
    h = df_daily["High"].shift(1)
    l = df_daily["Low"].shift(1)
    c = df_daily["Close"].shift(1)
    pp = (h + l + c) / 3
    bc = (h + l) / 2
    tc = 2 * pp - bc
    return pd.DataFrame({
        "PP": pp, "TC": tc, "BC": bc,
        "R1": 2*pp - l, "R2": pp + (h-l),
        "S1": 2*pp - h, "S2": pp - (h-l),
    }, index=df_daily.index)


def generate_intraday_signals(df_intraday: pd.DataFrame,
                               df_daily: pd.DataFrame,
                               df_vix: pd.DataFrame,
                               params: dict = None) -> pd.DataFrame:
    """
    Generate intraday entry signals for each bar.

    Parameters
    ----------
    df_intraday : OHLCV with timezone-aware DatetimeIndex (intraday bars)
    df_daily    : OHLCV daily data (for CPR / VIX alignment)
    df_vix      : Daily VIX data with 'Close' column
    params      : strategy parameters

    Returns
    -------
    DataFrame indexed like df_intraday with 'signal' column ('call'/'put'/None),
    'pp', 'tc', 'bc', 'entry_price', 'vix_val'
    """
    p = {**INTRADAY_CPR_PARAMS, **(params or {})}

    # ── Normalise timezone ─────────────────────────────────────────────────
    idx = df_intraday.index
    if idx.tzinfo is not None:
        idx_date = idx.normalize().date
    else:
        idx_date = idx.date

    # ── Compute indicators on intraday bars ───────────────────────────────
    close = df_intraday["Close"]
    high  = df_intraday["High"]
    low   = df_intraday["Low"]

    ema_fast = _ema(close, p["ema_fast"]).shift(1)
    ema_slow = _ema(close, p["ema_slow"]).shift(1)
    rsi      = _rsi(close, p["rsi_period"]).shift(1)
    adx_val  = _adx(high, low, close, p["adx_period"]).shift(1)

    # ── Map daily CPR pivots to intraday bars ─────────────────────────────
    pivots = compute_daily_pivots(df_daily)
    pivot_daily = pivots.reindex(
        pd.DatetimeIndex([pd.Timestamp(d) for d in idx_date]),
        method="ffill"
    )
    pivot_daily.index = df_intraday.index

    # ── Map daily VIX to intraday bars ────────────────────────────────────
    vix_daily = df_vix["Close"].copy()
    vix_intra = vix_daily.reindex(
        pd.DatetimeIndex([pd.Timestamp(d) for d in idx_date]),
        method="ffill"
    )
    vix_intra.index = df_intraday.index
    vix_shifted = vix_intra.shift(1)  # use prior day VIX

    # ── Entry window filter: only 9:15 to entry_window_end ───────────────
    bar_time = df_intraday.index.time
    entry_end_h, entry_end_m = map(int, p["entry_window_end"].split(":"))
    in_window = pd.Series(
        [t.hour < entry_end_h or (t.hour == entry_end_h and t.minute <= entry_end_m)
         for t in bar_time], index=df_intraday.index
    )

    # ── Track first signal per day ────────────────────────────────────────
    bar_dates = pd.Series(idx_date, index=df_intraday.index)
    date_first_signal = {}

    signals = pd.DataFrame(index=df_intraday.index)
    signals["signal"]      = None
    signals["entry_price"] = df_intraday["Close"]  # entry at bar close
    signals["pp"]          = pivot_daily["PP"]
    signals["tc"]          = pivot_daily["TC"]
    signals["bc"]          = pivot_daily["BC"]
    signals["r1"]          = pivot_daily["R1"]
    signals["s1"]          = pivot_daily["S1"]
    signals["vix_val"]     = vix_shifted

    # ── CPR width filter (narrow CPR = strong S/R levels) ────────────────
    cpr_width_pct = (pivot_daily["TC"] - pivot_daily["BC"]) / pivot_daily["PP"] * 100
    narrow_cpr    = cpr_width_pct <= p["cpr_width_max_pct"]

    # ── N-bar confirmation: require consecutive closes on breakout side ───
    n_confirm = int(p.get("confirm_bars", 1))
    above_tc   = close > pivot_daily["TC"]
    below_bc   = close < pivot_daily["BC"]
    # All n prior bars (including current) must agree — shift(k) = k bars ago
    for k in range(1, n_confirm):
        above_tc = above_tc & (close.shift(k) > pivot_daily["TC"])
        below_bc = below_bc & (close.shift(k) < pivot_daily["BC"])

    # ── Build call / put conditions ───────────────────────────────────────
    base = (
        in_window &
        narrow_cpr &
        (vix_shifted >= p["vix_min"]) &
        (vix_shifted <= p["vix_max"]) &
        (adx_val >= p["adx_min"])
    )

    call_break = above_tc
    put_break  = below_bc

    call_cond = base & call_break & (ema_fast > ema_slow) & \
                (rsi > p["rsi_call_lo"]) & (rsi < p["rsi_call_hi"])
    put_cond  = base & put_break  & (ema_fast < ema_slow) & \
                (rsi > p["rsi_put_lo"])  & (rsi < p["rsi_put_hi"])

    # Only first signal per day
    for i, (idx_val, row) in enumerate(
            zip(df_intraday.index, zip(call_cond, put_cond))):
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
