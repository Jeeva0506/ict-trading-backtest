"""Technical indicators for ORB Sweep strategy."""

import numpy as np
import pandas as pd


def vwap(df: pd.DataFrame) -> pd.Series:
    """Session-reset VWAP. Falls back to cumulative typical price when volume=0."""
    tp = (df["High"] + df["Low"] + df["Close"]) / 3
    vol = df["Volume"]
    groups = df.index.date

    has_volume = vol.groupby(groups).transform("sum") > 0
    cum_tp_vol = (tp * vol).groupby(groups).cumsum()
    cum_vol = vol.groupby(groups).cumsum()
    vwap_vol = cum_tp_vol / cum_vol.replace(0, np.nan)

    cum_tp = tp.groupby(groups).cumsum()
    cum_count = tp.groupby(groups).cumcount() + 1
    vwap_twap = cum_tp / cum_count

    return vwap_vol.where(has_volume, vwap_twap)


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average True Range."""
    prev_close = df["Close"].shift(1)
    tr = pd.concat([
        df["High"] - df["Low"],
        (df["High"] - prev_close).abs(),
        (df["Low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(period, min_periods=period).mean()


def sma_volume(df: pd.DataFrame, period: int = 20) -> pd.Series:
    """Simple moving average of volume."""
    return df["Volume"].rolling(period, min_periods=period).mean()


def find_swing_highs(highs: pd.Series, n: int = 2) -> pd.Series:
    """Pivot highs with n bars each side. Returns NaN where no pivot."""
    result = pd.Series(np.nan, index=highs.index)
    vals = highs.values
    for i in range(n, len(vals) - n):
        is_pivot = True
        for j in range(1, n + 1):
            if vals[i] <= vals[i - j] or vals[i] <= vals[i + j]:
                is_pivot = False
                break
        if is_pivot:
            result.iloc[i] = vals[i]
    return result


def find_swing_lows(lows: pd.Series, n: int = 2) -> pd.Series:
    """Pivot lows with n bars each side. Returns NaN where no pivot."""
    result = pd.Series(np.nan, index=lows.index)
    vals = lows.values
    for i in range(n, len(vals) - n):
        is_pivot = True
        for j in range(1, n + 1):
            if vals[i] >= vals[i - j] or vals[i] >= vals[i + j]:
                is_pivot = False
                break
        if is_pivot:
            result.iloc[i] = vals[i]
    return result


def last_confirmed_swing_high(highs: pd.Series, up_to_idx: int, n: int = 2) -> float:
    """Most recent confirmed swing high before position up_to_idx."""
    vals = highs.values
    for i in range(up_to_idx - n, n - 1, -1):
        is_pivot = True
        for j in range(1, n + 1):
            if i - j < 0 or i + j >= len(vals):
                is_pivot = False
                break
            if vals[i] <= vals[i - j] or vals[i] <= vals[i + j]:
                is_pivot = False
                break
        if is_pivot:
            return vals[i]
    return np.nan


def last_confirmed_swing_low(lows: pd.Series, up_to_idx: int, n: int = 2) -> float:
    """Most recent confirmed swing low before position up_to_idx."""
    vals = lows.values
    for i in range(up_to_idx - n, n - 1, -1):
        is_pivot = True
        for j in range(1, n + 1):
            if i - j < 0 or i + j >= len(vals):
                is_pivot = False
                break
            if vals[i] >= vals[i - j] or vals[i] >= vals[i + j]:
                is_pivot = False
                break
        if is_pivot:
            return vals[i]
    return np.nan


def resample_to_5m(df_1m: pd.DataFrame) -> pd.DataFrame:
    """Resample 1-minute OHLCV to 5-minute bars."""
    return df_1m.resample("5min").agg({
        "Open": "first",
        "High": "max",
        "Low": "min",
        "Close": "last",
        "Volume": "sum",
    }).dropna(subset=["Open"])
