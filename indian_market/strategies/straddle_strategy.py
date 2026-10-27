"""
Low-VIX Straddle Expansion Strategy (LVSE)

Research basis:
  - "Option Straddle Strategy on NSE Index Derivatives" — ICICI Direct (2023)
    → VIX < 20th pct = IV at floor; 60-65% WR as IV mean-reverts
  - 5paisa blog (2024): "VIX < 12 = buy straddle" for BankNifty Thursday expiry
    → 60%+ WR documented on low-VIX regime entries
  - BankNiftyOptions.com (2024): Weekly straddle timing study
    → ADX > 20 required to avoid flat market stagnation (theta kills straddle)

Why straddles outperform directional buying on daily data:
  Straddle profit sources:
    1. DELTA: Spot moves UP (call gains) OR DOWN (put gains) — either is fine
    2. VEGA: VIX rises after entry (both legs gain from IV expansion)
  When VIX is at a multi-week low, probability of IV expansion is high.
  Breakeven: Nifty must move ±straddle_cost in index points.
  At VIX=15, 7-DTE: straddle ≈ ₹385; breakeven = ±385 pts = ±1.75% on 22k.
  Historical data: Nifty makes ≥1.75% move in 5 days ~60-65% of the time.

Entry rules (prior-bar signals — no lookahead):
  1. VIX_IVR <= ivr_threshold (VIX in bottom X-th percentile of 252-day range)
  2. VIX has declined for at least `vix_decline_days` consecutive days
     (approaching a floor → expansion likely)
  3. VIX <= vix_max (not panic-driven) AND VIX >= vix_min (not dead market)
  4. ADX >= adx_threshold (some trend activity, not completely flat)
  5. No current open position

Exit rules:
  - TP: combined straddle value rises tp_pct above entry cost
  - SL: combined straddle value falls sl_pct below entry cost (at day close)
  - TIME: max_hold_days elapsed
"""
import numpy as np
import pandas as pd


STRADDLE_PARAMS = {
    "ivr_threshold"   : 25,    # VIX IVR ceiling (bottom 25% = cheap IV)
    "vix_max"         : 18,    # Skip if VIX too high (panic premium)
    "vix_min"         : 11,    # Skip if VIX too low (near-zero moves expected)
    "vix_decline_days": 2,     # Min consecutive VIX decline days before entry
    "adx_period"      : 14,    # ADX look-back
    "adx_threshold"   : 18,    # Min ADX (some trend activity present)
    "sl_pct"          : 0.40,  # Combined straddle SL: 40% loss of total premium
    "tp_pct"          : 0.60,  # Combined straddle TP: 60% gain of total premium
    "max_hold_days"   : 5,     # Time stop
    "entry_dte"       : 7,     # DTE when buying straddle
    "ivr_lookback"    : 252,
    "risk_per_trade"  : 10_000,
    "lot_size"        : 50,
    "strike_step"     : 50,
    "rfr"             : 0.065,
    "max_lots"        : 3,
}


def _adx(high: pd.Series, low: pd.Series, close: pd.Series, period: int) -> pd.Series:
    tr = pd.concat([
        high - low,
        (high - close.shift(1)).abs(),
        (low  - close.shift(1)).abs(),
    ], axis=1).max(axis=1)
    up   = high - high.shift(1)
    down = low.shift(1) - low
    dm_p = pd.Series(0.0, index=high.index)
    dm_m = pd.Series(0.0, index=high.index)
    dm_p[(up > down) & (up > 0)]   = up[(up > down) & (up > 0)]
    dm_m[(down > up) & (down > 0)] = down[(down > up) & (down > 0)]
    alpha = 1.0 / period
    atr   = tr.ewm(alpha=alpha, adjust=False).mean()
    di_p  = 100 * dm_p.ewm(alpha=alpha, adjust=False).mean() / atr
    di_m  = 100 * dm_m.ewm(alpha=alpha, adjust=False).mean() / atr
    dx    = 100 * (di_p - di_m).abs() / (di_p + di_m).replace(0, np.nan)
    return dx.ewm(alpha=alpha, adjust=False).mean()


def _ivr(vix: pd.Series, lookback: int) -> pd.Series:
    return vix.rolling(lookback).apply(
        lambda x: (x[-1] >= x).mean() * 100, raw=True
    )


def generate_straddle_signals(nifty: pd.DataFrame, vix: pd.DataFrame,
                               params: dict = None) -> pd.DataFrame:
    """
    Generate daily straddle entry signals.
    All conditions evaluated on PRIOR bar — no lookahead.
    """
    p = {**STRADDLE_PARAMS, **(params or {})}

    close = nifty["Close"]
    high  = nifty["High"]
    low   = nifty["Low"]
    open_ = nifty["Open"]

    adx_val = _adx(high, low, close, p["adx_period"])
    vix_s   = vix["Close"].reindex(close.index, method="ffill")
    ivr     = _ivr(vix_s, p["ivr_lookback"])

    # Consecutive VIX decline: VIX[t-1] < VIX[t-2] < VIX[t-3] ... for N days
    dec = p["vix_decline_days"]
    vix_declining = pd.Series(True, index=close.index)
    for lag in range(1, dec + 1):
        vix_declining &= (vix_s.shift(lag) < vix_s.shift(lag + 1))

    # All on PRIOR bar
    prev_ivr       = ivr.shift(1)
    prev_vix       = vix_s.shift(1)
    prev_adx       = adx_val.shift(1)
    prev_declining = vix_declining.shift(1)

    entry_cond = (
        (prev_ivr       <= p["ivr_threshold"]) &
        (prev_vix       >= p["vix_min"]) &
        (prev_vix       <= p["vix_max"]) &
        (prev_adx       >= p["adx_threshold"]) &
        (prev_declining == True)
    )

    signals = pd.DataFrame(index=close.index)
    signals["signal"]      = None
    signals.loc[entry_cond, "signal"] = "straddle"
    signals["entry_spot"]  = open_
    signals["vix_val"]     = prev_vix
    signals["ivr_val"]     = prev_ivr
    signals["adx_val"]     = prev_adx

    return signals
