"""
VIX-Rank Directional Option Buyer (VRDOB)

Strategy design based on research synthesis:
  1. "Low VIX = cheap options" → IVR-based timing (Sahi, ICFM, 5paisa research 2024)
  2. Directional momentum filter → EMA trend + RSI confirmation
  3. Weekly option buying with defined SL/TP

References from Indian options research:
  - ICFM India (2024): "5 Proven Intraday Option Strategies for Nifty/BankNifty"
    → EMA cross + RSI confirmation; 60-65% WR with 1:2 R:R
  - 5paisa Blog (2024): "India VIX Strategies"
    → VIX < 15 (low regime): buy options (cheaper premium, higher expansion potential)
  - ICICI Direct Research: Weekly straddle on low-VIX days → Sharpe 0.41→1.12
  - NSE Paper (2008): India VIX methodology → VIX = 1yr forward implied vol

Entry logic (all signals evaluated at PRIOR day close — no lookahead):
  1. IVR (252-day rolling) ≤ 35  → options are in bottom third of expensive-ness
  2. India VIX ≤ 20              → not panic-driven premium spike
  3. Call signal: Close > EMA20 > EMA50 AND 50 < RSI14 < 75
  4. Put signal:  Close < EMA20 < EMA50 AND 25 < RSI14 < 50
  5. No position currently open

Exit logic:
  - SL  : option price falls 40% below entry premium  → SL_HIT
  - TP  : option price rises 80% above entry premium  → TP_HIT (2:1 R:R)
  - Time: position open > MAX_HOLD_DAYS trading days  → TIME_EXIT

Sizing:
  - Risk per trade = 1% × capital = ₹10,000
  - Max premium budget = risk / SL_pct = ₹10,000 / 0.40 = ₹25,000
  - Lots = floor(budget / (premium × lot_size)), min 1
"""
import numpy as np
import pandas as pd


# ── Default parameters (tunable for optimization) ────────────────────────────
DEFAULT_PARAMS = {
    "ivr_threshold"  : 35,    # IVR percentile ceiling for entry (0-100)
    "vix_max"        : 20,    # Hard VIX ceiling — skip if VIX above this
    "vix_min"        : 12,    # Hard VIX floor — skip if too cheap (no range)
    "ema_fast"       : 20,    # Fast EMA period (days)
    "ema_slow"       : 50,    # Slow EMA period (days)
    "adx_period"     : 14,    # ADX look-back period
    "adx_threshold"  : 20,    # Minimum ADX — only trade trending markets
    "rsi_period"     : 14,    # RSI look-back
    "rsi_call_lo"    : 55,    # RSI floor for call entry
    "rsi_call_hi"    : 72,    # RSI ceiling for call entry (not overbought)
    "rsi_put_lo"     : 28,    # RSI floor for put entry (not oversold)
    "rsi_put_hi"     : 45,    # RSI ceiling for put entry
    "sl_pct"         : 0.40,  # Stop loss: % of entry premium
    "tp_pct"         : 0.80,  # Take profit: % of entry premium (×0.80 above entry)
    "max_hold_days"  : 5,     # Max calendar days to hold position
    "entry_dte"      : 7,     # Days-to-expiry when buying option (BS pricing)
    "ivr_lookback"   : 252,   # Rolling window for IVR computation
    "risk_per_trade" : 10_000, # ₹ per trade
    "lot_size"       : 50,    # Nifty lot size
    "strike_step"    : 50,    # ATM rounding step
    "rfr"            : 0.065,  # Risk-free rate (India 10-yr G-sec)
    "max_lots"       : 5,     # Hard cap on lots per trade
}


def _ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def _rsi(series: pd.Series, period: int) -> pd.Series:
    delta = series.diff()
    gain  = delta.clip(lower=0)
    loss  = (-delta).clip(lower=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs  = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def _adx(high: pd.Series, low: pd.Series, close: pd.Series, period: int) -> pd.Series:
    """Average Directional Index — measures trend strength (0-100)."""
    tr = pd.concat([
        high - low,
        (high - close.shift(1)).abs(),
        (low  - close.shift(1)).abs(),
    ], axis=1).max(axis=1)

    up_move   = high - high.shift(1)
    down_move = low.shift(1) - low

    dm_plus  = pd.Series(0.0, index=high.index)
    dm_minus = pd.Series(0.0, index=high.index)
    mask_up   = (up_move > down_move) & (up_move > 0)
    mask_down = (down_move > up_move) & (down_move > 0)
    dm_plus[mask_up]    = up_move[mask_up]
    dm_minus[mask_down] = down_move[mask_down]

    alpha = 1.0 / period
    atr      = tr.ewm(alpha=alpha, adjust=False).mean()
    di_plus  = 100 * dm_plus.ewm(alpha=alpha, adjust=False).mean()  / atr
    di_minus = 100 * dm_minus.ewm(alpha=alpha, adjust=False).mean() / atr

    dx_denom = (di_plus + di_minus).replace(0, np.nan)
    dx  = 100 * (di_plus - di_minus).abs() / dx_denom
    return dx.ewm(alpha=alpha, adjust=False).mean()


def _ivr(vix: pd.Series, lookback: int) -> pd.Series:
    """
    IVR = percentile rank of today's VIX in rolling lookback window.
    Range 0-100; 0 = historically cheapest, 100 = historically most expensive.
    """
    ivr = vix.rolling(lookback).apply(
        lambda x: (x[-1] >= x).mean() * 100, raw=True
    )
    return ivr


def generate_signals(nifty: pd.DataFrame, vix: pd.DataFrame,
                     params: dict = None) -> pd.DataFrame:
    """
    Generate daily entry signals. All indicators use prior-day values.

    Parameters
    ----------
    nifty : DataFrame with OHLCV columns, DatetimeIndex
    vix   : DataFrame with 'Close' column = India VIX daily close
    params: strategy parameters (defaults to DEFAULT_PARAMS)

    Returns
    -------
    DataFrame with columns:
        signal   : 'call', 'put', or None
        entry_spot, vix_val, ivr_val, ema_fast_val, ema_slow_val, rsi_val, adx_val
    """
    p = {**DEFAULT_PARAMS, **(params or {})}

    close = nifty["Close"]
    high  = nifty["High"]
    low   = nifty["Low"]
    open_ = nifty["Open"]

    ema_fast = _ema(close, p["ema_fast"])
    ema_slow = _ema(close, p["ema_slow"])
    rsi      = _rsi(close, p["rsi_period"])
    adx_val  = _adx(high, low, close, p["adx_period"])
    ivr      = _ivr(vix["Close"].reindex(close.index, method="ffill"), p["ivr_lookback"])

    vix_aligned = vix["Close"].reindex(close.index, method="ffill")

    # All signals evaluated on PRIOR bar (shift by 1 to avoid lookahead)
    prev_close    = close.shift(1)
    prev_ema_fast = ema_fast.shift(1)
    prev_ema_slow = ema_slow.shift(1)
    prev_rsi      = rsi.shift(1)
    prev_adx      = adx_val.shift(1)
    prev_ivr      = ivr.shift(1)
    prev_vix      = vix_aligned.shift(1)

    # Shared pre-conditions (VIX range + IVR + trend strength)
    common = (
        (prev_ivr >= 0) &              # IVR computed (needs ivr_lookback bars)
        (prev_ivr  <= p["ivr_threshold"]) &
        (prev_vix  >= p["vix_min"]) &  # minimum VIX (options have value)
        (prev_vix  <= p["vix_max"]) &  # maximum VIX (not panic)
        (prev_adx  >= p["adx_threshold"])  # market is trending
    )

    # ── Call condition ────────────────────────────────────────────────────────
    call_cond = common & (
        (prev_close > prev_ema_fast) &
        (prev_ema_fast > prev_ema_slow) &
        (prev_rsi  >  p["rsi_call_lo"]) &
        (prev_rsi  <  p["rsi_call_hi"])
    )

    # ── Put condition ─────────────────────────────────────────────────────────
    put_cond = common & (
        (prev_close < prev_ema_fast) &
        (prev_ema_fast < prev_ema_slow) &
        (prev_rsi  >  p["rsi_put_lo"]) &
        (prev_rsi  <  p["rsi_put_hi"])
    )

    signals = pd.DataFrame(index=close.index)
    signals["signal"]       = None
    signals.loc[call_cond,  "signal"] = "call"
    signals.loc[put_cond,   "signal"] = "put"

    # When both fire same day (rare) → skip (conflicting bias)
    both = call_cond & put_cond
    signals.loc[both, "signal"] = None

    signals["entry_spot"]   = open_
    signals["vix_val"]      = prev_vix
    signals["ivr_val"]      = prev_ivr
    signals["ema_fast_val"] = prev_ema_fast
    signals["ema_slow_val"] = prev_ema_slow
    signals["rsi_val"]      = prev_rsi
    signals["adx_val"]      = prev_adx

    return signals
