"""
ATR-Based Stop Loss Calculator
==============================
Dynamically calculates SL based on ATR(14) * 1.5
instead of fixed point buffers.

Usage:
    from atr_sl import calculate_atr_sl
    
    atr_sl_points = calculate_atr_sl(market_df, atr_multiplier=1.5)
"""

import pandas as pd
import numpy as np
from typing import Optional

def compute_atr(data: pd.DataFrame, period: int = 14) -> float:
    """
    Compute ATR(period) from OHLC data.
    
    Args:
        data: DataFrame with 'high', 'low', 'close' columns
        period: ATR period (default 14)
    
    Returns:
        Latest ATR value
    """
    if len(data) < period:
        return np.nan
    
    try:
        df = data[['high', 'low', 'close']].copy()
        
        # True Range calculation
        df['tr1'] = df['high'] - df['low']
        df['tr2'] = abs(df['high'] - df['close'].shift())
        df['tr3'] = abs(df['low'] - df['close'].shift())
        df['tr'] = df[['tr1', 'tr2', 'tr3']].max(axis=1)
        
        # ATR = SMA of TR
        df['atr'] = df['tr'].rolling(window=period).mean()
        
        # Return latest valid ATR
        atr_value = df['atr'].iloc[-1]
        return atr_value if not pd.isna(atr_value) else np.nan
    except Exception as e:
        print(f"Error computing ATR: {e}")
        return np.nan

def calculate_atr_sl(market_data: pd.DataFrame, 
                     atr_multiplier: float = 1.5,
                     atr_period: int = 14,
                     min_sl: float = 1.0,
                     max_sl: float = 50.0) -> float:
    """
    Calculate SL distance using ATR.
    
    Args:
        market_data: DataFrame with OHLC data
        atr_multiplier: Multiplier for ATR (default 1.5)
        atr_period: ATR lookback period (default 14)
        min_sl: Minimum SL distance in points
        max_sl: Maximum SL distance in points
    
    Returns:
        SL distance in points, clamped to [min_sl, max_sl]
    """
    atr = compute_atr(market_data, atr_period)
    
    if pd.isna(atr) or atr <= 0:
        # Fallback if ATR can't be computed
        return min_sl
    
    sl_distance = atr * atr_multiplier
    
    # Clamp to reasonable bounds
    sl_distance = max(min_sl, min(max_sl, sl_distance))
    
    return sl_distance

def get_atr_for_symbol(symbol: str, 
                       mkt5_instance,
                       lookback_bars: int = 100,
                       atr_multiplier: float = 1.5,
                       atr_period: int = 14) -> Optional[float]:
    """
    Get current ATR-based SL for a symbol from MT5.
    
    Args:
        symbol: MT5 symbol name
        mt5_instance: MetaTrader5 instance
        lookback_bars: How many bars to fetch for ATR
        atr_multiplier: ATR multiplier (1.5 = 1.5x ATR)
        atr_period: ATR period (14 = ATR14)
    
    Returns:
        SL distance in points or None if fetch failed
    """
    try:
        # Fetch recent M1 data
        rates = mt5_instance.copy_rates_from_pos(symbol, mt5_instance.TIMEFRAME_M1, 0, lookback_bars)
        
        if rates is None or len(rates) == 0:
            return None
        
        # Convert to DataFrame
        df = pd.DataFrame(rates)
        df['time'] = pd.to_datetime(df['time'], unit='s')
        df = df.set_index('time')
        
        # Calculate ATR-based SL
        sl_distance = calculate_atr_sl(df, atr_multiplier, atr_period)
        
        return sl_distance
    except Exception as e:
        print(f"Error getting ATR for {symbol}: {e}")
        return None

def fallback_to_fixed(symbol: str, fixed_buffers: dict) -> float:
    """Fallback to fixed buffer if ATR calculation fails."""
    return fixed_buffers.get(symbol, 1.0)
