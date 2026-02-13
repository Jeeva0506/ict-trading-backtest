# strategy/utils.py
from __future__ import annotations

from typing import List, Tuple
import pandas as pd


def find_fractal_pivots(df: pd.DataFrame, lookback: int = 5) -> Tuple[List[int], List[int]]:
    """
    Fractal pivot detector using wick extremes (cluster-friendly):
      pivot high at i if high[i] is max in [i-lookback, i+lookback]
      pivot low  at i if low[i]  is min in [i-lookback, i+lookback]

    This matches your 'cluster' idea better than strict candle-by-candle swings because it
    finds the extreme of the push/pull structure using wicks.

    NOTE:
      In *live*, a pivot is only confirmed after +lookback candles pass.
      In backtest we can use it directly.
    """
    if df is None or len(df) == 0:
        return [], []

    highs = df["high"].values
    lows = df["low"].values
    n = len(df)

    if n < (lookback * 2 + 1):
        return [], []

    swing_highs: List[int] = []
    swing_lows: List[int] = []

    for i in range(lookback, n - lookback):
        left = i - lookback
        right = i + lookback + 1

        if highs[i] == highs[left:right].max():
            swing_highs.append(i)
        if lows[i] == lows[left:right].min():
            swing_lows.append(i)

    return swing_highs, swing_lows


# alias so strategy can import either name
find_swing_highs_lows = find_fractal_pivots


def aggregate_1m_to_3m(df: pd.DataFrame) -> pd.DataFrame:
    """
    Aggregate 1-min candles into 3-min candles aligned on timestamp.
    Requires df.index to be DatetimeIndex.
    """
    if df is None or len(df) == 0:
        return df

    o = df["open"].resample("3min").first()
    h = df["high"].resample("3min").max()
    l = df["low"].resample("3min").min()
    c = df["close"].resample("3min").last()

    out = pd.DataFrame({"open": o, "high": h, "low": l, "close": c})
    if "volume" in df.columns:
        out["volume"] = df["volume"].resample("3min").sum()

    out = out.dropna(subset=["open", "high", "low", "close"])
    return out
