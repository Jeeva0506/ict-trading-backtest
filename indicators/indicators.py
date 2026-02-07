"""
ICT Strategy Indicators
========================

Indicators designed to complement the ICT trading strategy:
1. Fair Value Gaps (FVG) - Imbalance zones for confluence
2. Liquidity Sweeps - Stop hunts before reversals
3. Session Levels - Asian, London, NY highs/lows
4. RSI Divergence - Confirm reversals at OB zones
5. Volume Confirmation - Validate OB strength
6. ATR for dynamic SL - Volatility-adjusted stops
7. EMA Trend Filter - Higher timeframe bias
8. VWAP - Institutional price reference
"""

import pandas as pd
import numpy as np
from typing import List, Tuple, Optional, Dict
from dataclasses import dataclass


# =============================================================================
# FAIR VALUE GAPS (FVG) - ICT CONCEPT
# =============================================================================

@dataclass
class FVG:
    """Fair Value Gap - price imbalance zone"""
    idx: int
    high: float
    low: float
    direction: str  # 'bullish' or 'bearish'
    filled: bool = False
    fill_idx: int = None


def detect_fair_value_gaps(df: pd.DataFrame, min_gap_points: float = 5.0) -> List[FVG]:
    """
    Detect Fair Value Gaps (imbalance zones)
    
    Bullish FVG: Gap between candle[i-1].high and candle[i+1].low (gap up)
    Bearish FVG: Gap between candle[i-1].low and candle[i+1].high (gap down)
    """
    fvgs = []
    
    for i in range(1, len(df) - 1):
        prev_candle = df.iloc[i - 1]
        next_candle = df.iloc[i + 1]
        
        # Bullish FVG: Strong up move leaving gap
        if next_candle['low'] > prev_candle['high']:
            gap_size = next_candle['low'] - prev_candle['high']
            if gap_size >= min_gap_points:
                fvgs.append(FVG(idx=i, high=next_candle['low'], low=prev_candle['high'], direction='bullish'))
        
        # Bearish FVG: Strong down move leaving gap
        if next_candle['high'] < prev_candle['low']:
            gap_size = prev_candle['low'] - next_candle['high']
            if gap_size >= min_gap_points:
                fvgs.append(FVG(idx=i, high=prev_candle['low'], low=next_candle['high'], direction='bearish'))
    
    return fvgs


def check_fvg_confluence(price: float, fvgs: List[FVG], direction: str) -> Optional[FVG]:
    """Check if price is at an unfilled FVG zone (confluence with OB)"""
    for fvg in fvgs:
        if not fvg.filled and fvg.direction == direction and fvg.low <= price <= fvg.high:
            return fvg
    return None


# =============================================================================
# LIQUIDITY SWEEPS - Stop Hunt Detection
# =============================================================================

@dataclass
class LiquiditySweep:
    """Liquidity sweep / stop hunt event"""
    idx: int
    level: float
    direction: str  # 'bullish' (swept lows) or 'bearish' (swept highs)
    swing_idx: int


def detect_liquidity_sweeps(
    df: pd.DataFrame, 
    swing_highs: List[int], 
    swing_lows: List[int],
    buffer_points: float = 2.0
) -> List[LiquiditySweep]:
    """
    Detect liquidity sweeps (stop hunts)
    
    Bullish sweep: Price dips below swing low then closes above (swept sell stops)
    Bearish sweep: Price spikes above swing high then closes below (swept buy stops)
    """
    sweeps = []
    
    for i in range(5, len(df)):
        candle = df.iloc[i]
        
        # Bullish sweep
        for sl_idx in swing_lows:
            if sl_idx >= i - 1 or sl_idx < i - 50:
                continue
            swing_low_price = df.iloc[sl_idx]['low']
            if candle['low'] < swing_low_price - buffer_points and candle['close'] > swing_low_price:
                sweeps.append(LiquiditySweep(idx=i, level=swing_low_price, direction='bullish', swing_idx=sl_idx))
                break
        
        # Bearish sweep
        for sh_idx in swing_highs:
            if sh_idx >= i - 1 or sh_idx < i - 50:
                continue
            swing_high_price = df.iloc[sh_idx]['high']
            if candle['high'] > swing_high_price + buffer_points and candle['close'] < swing_high_price:
                sweeps.append(LiquiditySweep(idx=i, level=swing_high_price, direction='bearish', swing_idx=sh_idx))
                break
    
    return sweeps


# =============================================================================
# SESSION LEVELS - Trading Session Highs/Lows
# =============================================================================

@dataclass
class SessionLevel:
    """Session high/low levels"""
    date: str
    session: str
    high: float
    low: float
    high_time: any = None
    low_time: any = None


def calculate_session_levels(df: pd.DataFrame) -> Dict[str, List[SessionLevel]]:
    """Calculate session highs and lows (IST times for NIFTY)"""
    if 'datetime' not in df.columns:
        return {}
    
    df = df.copy()
    df['date'] = df['datetime'].dt.date
    df['hour'] = df['datetime'].dt.hour
    
    sessions = {'morning': [], 'afternoon': [], 'full_day': []}
    
    for date, day_df in df.groupby('date'):
        morning = day_df[(day_df['hour'] >= 9) & (day_df['hour'] < 12)]
        if len(morning) > 0:
            sessions['morning'].append(SessionLevel(
                date=str(date), session='morning',
                high=morning['high'].max(), low=morning['low'].min()
            ))
        
        afternoon = day_df[(day_df['hour'] >= 12) & (day_df['hour'] < 16)]
        if len(afternoon) > 0:
            sessions['afternoon'].append(SessionLevel(
                date=str(date), session='afternoon',
                high=afternoon['high'].max(), low=afternoon['low'].min()
            ))
        
        if len(day_df) > 0:
            sessions['full_day'].append(SessionLevel(
                date=str(date), session='full_day',
                high=day_df['high'].max(), low=day_df['low'].min()
            ))
    
    return sessions


def get_previous_session_levels(df: pd.DataFrame, current_idx: int) -> Optional[SessionLevel]:
    """Get previous day's session levels for key support/resistance"""
    if 'datetime' not in df.columns:
        return None
    
    current_date = df.iloc[current_idx]['datetime'].date()
    prev_day_df = df[df['datetime'].dt.date < current_date]
    
    if len(prev_day_df) == 0:
        return None
    
    last_date = prev_day_df['datetime'].dt.date.max()
    last_day_df = prev_day_df[prev_day_df['datetime'].dt.date == last_date]
    
    return SessionLevel(
        date=str(last_date), session='full_day',
        high=last_day_df['high'].max(), low=last_day_df['low'].min()
    )


# =============================================================================
# RSI - Relative Strength Index
# =============================================================================

def calculate_rsi(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Calculate RSI indicator"""
    delta = df['close'].diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)
    
    avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()
    
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def detect_rsi_divergence(
    df: pd.DataFrame, 
    swing_highs: List[int], 
    swing_lows: List[int],
    rsi_period: int = 14,
    lookback: int = 20
) -> Dict[str, List[int]]:
    """
    Detect RSI divergence at swing points
    
    Bullish divergence: Price makes lower low, RSI makes higher low
    Bearish divergence: Price makes higher high, RSI makes lower high
    """
    rsi = calculate_rsi(df, rsi_period)
    divergences = {'bullish': [], 'bearish': []}
    
    # Bullish divergence at swing lows
    for i, sl_idx in enumerate(swing_lows):
        if sl_idx < lookback:
            continue
        prev_lows = [idx for idx in swing_lows[:i] if sl_idx - idx <= lookback]
        if not prev_lows:
            continue
        prev_sl_idx = prev_lows[-1]
        if (df.iloc[sl_idx]['low'] < df.iloc[prev_sl_idx]['low'] and
            rsi.iloc[sl_idx] > rsi.iloc[prev_sl_idx]):
            divergences['bullish'].append(sl_idx)
    
    # Bearish divergence at swing highs
    for i, sh_idx in enumerate(swing_highs):
        if sh_idx < lookback:
            continue
        prev_highs = [idx for idx in swing_highs[:i] if sh_idx - idx <= lookback]
        if not prev_highs:
            continue
        prev_sh_idx = prev_highs[-1]
        if (df.iloc[sh_idx]['high'] > df.iloc[prev_sh_idx]['high'] and
            rsi.iloc[sh_idx] < rsi.iloc[prev_sh_idx]):
            divergences['bearish'].append(sh_idx)
    
    return divergences


def check_rsi_zone(rsi_value: float) -> str:
    """Check RSI zone: 'oversold' (< 30), 'overbought' (> 70), or 'neutral'"""
    if rsi_value < 30:
        return 'oversold'
    elif rsi_value > 70:
        return 'overbought'
    return 'neutral'


# =============================================================================
# VOLUME CONFIRMATION
# =============================================================================

def calculate_volume_ma(df: pd.DataFrame, period: int = 20) -> pd.Series:
    """Calculate volume moving average"""
    if 'volume' not in df.columns:
        return pd.Series([0] * len(df))
    return df['volume'].rolling(window=period).mean()


def check_volume_confirmation(df: pd.DataFrame, idx: int, lookback: int = 5, multiplier: float = 1.5) -> bool:
    """Check if there's volume confirmation (above average volume)"""
    if 'volume' not in df.columns or idx < lookback:
        return True
    
    recent_vol = df.iloc[idx]['volume']
    avg_vol = df.iloc[idx - lookback:idx]['volume'].mean()
    return recent_vol >= avg_vol * multiplier


# =============================================================================
# ATR - Average True Range
# =============================================================================

def calculate_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Calculate Average True Range"""
    high = df['high']
    low = df['low']
    close = df['close'].shift(1)
    
    tr1 = high - low
    tr2 = abs(high - close)
    tr3 = abs(low - close)
    
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return tr.rolling(window=period).mean()


def calculate_dynamic_sl(entry_price: float, direction: str, atr_value: float, multiplier: float = 1.5) -> float:
    """Calculate dynamic stop loss based on ATR"""
    sl_distance = atr_value * multiplier
    return entry_price - sl_distance if direction == 'bullish' else entry_price + sl_distance


# =============================================================================
# EMA - Exponential Moving Average
# =============================================================================

def calculate_ema(df: pd.DataFrame, period: int) -> pd.Series:
    """Calculate Exponential Moving Average"""
    return df['close'].ewm(span=period, adjust=False).mean()


def get_ema_trend(df: pd.DataFrame, idx: int, fast_period: int = 9, slow_period: int = 21) -> str:
    """Get trend direction based on EMA crossover: 'bullish', 'bearish', or 'neutral'"""
    if idx < slow_period:
        return 'neutral'
    
    ema_fast = calculate_ema(df.iloc[:idx + 1], fast_period)
    ema_slow = calculate_ema(df.iloc[:idx + 1], slow_period)
    
    if ema_fast.iloc[-1] > ema_slow.iloc[-1]:
        return 'bullish'
    elif ema_fast.iloc[-1] < ema_slow.iloc[-1]:
        return 'bearish'
    return 'neutral'


def check_ema_filter(df: pd.DataFrame, idx: int, trade_direction: str, period: int = 50) -> bool:
    """Check if trade direction aligns with EMA trend"""
    if idx < period:
        return True
    
    ema = calculate_ema(df.iloc[:idx + 1], period)
    current_price = df.iloc[idx]['close']
    
    return current_price > ema.iloc[-1] if trade_direction == 'bullish' else current_price < ema.iloc[-1]


# =============================================================================
# VWAP - Volume Weighted Average Price
# =============================================================================

def calculate_vwap(df: pd.DataFrame) -> pd.Series:
    """Calculate VWAP (Volume Weighted Average Price)"""
    typical_price = (df['high'] + df['low'] + df['close']) / 3
    
    if 'volume' in df.columns and df['volume'].sum() > 0:
        cumulative_tp_vol = (typical_price * df['volume']).cumsum()
        cumulative_vol = df['volume'].cumsum()
        return cumulative_tp_vol / cumulative_vol
    return typical_price.expanding().mean()


def calculate_vwap_bands(df: pd.DataFrame, std_period: int = 14, std_multiplier: float = 2.0) -> dict:
    """
    Calculate VWAP with upper and lower bands (rolling standard deviation based)
    
    Args:
        df: OHLC DataFrame
        std_period: Period for rolling standard deviation (default 14)
        std_multiplier: Standard deviation multiplier for bands (default 2.0)
    
    Returns:
        Dict with 'vwap', 'upper_band', 'lower_band' as pd.Series
    """
    typical_price = (df['high'] + df['low'] + df['close']) / 3
    
    if 'volume' in df.columns and df['volume'].sum() > 0:
        cumulative_tp_vol = (typical_price * df['volume']).cumsum()
        cumulative_vol = df['volume'].cumsum()
        vwap = cumulative_tp_vol / cumulative_vol
    else:
        vwap = typical_price.expanding().mean()
    
    # Use rolling 14-period standard deviation for bands
    std_dev = typical_price.rolling(window=std_period, min_periods=1).std()
    
    upper_band = vwap + (std_multiplier * std_dev)
    lower_band = vwap - (std_multiplier * std_dev)
    
    return {
        'vwap': vwap,
        'upper_band': upper_band,
        'lower_band': lower_band
    }


def check_vwap_band_filter(df: pd.DataFrame, idx: int, direction: str, std_period: int = 14, std_multiplier: float = 2.0) -> dict:
    """
    Check VWAP band filter for trade entry
    
    LONG: OK below VWAP center, but candle must NOT close below lower band
    SHORT: OK above VWAP center, but candle must NOT close above upper band
    
    Args:
        df: OHLC DataFrame
        idx: Current candle index
        direction: 'bullish' or 'bearish'
        std_period: Period for rolling standard deviation (default 14)
        std_multiplier: Standard deviation multiplier for bands (default 2.0)
    
    Returns:
        Dict with 'allowed', 'vwap', 'upper_band', 'lower_band', 'close'
    """
    bands = calculate_vwap_bands(df.iloc[:idx + 1], std_period, std_multiplier)
    
    vwap_value = bands['vwap'].iloc[-1]
    upper_band = bands['upper_band'].iloc[-1]
    lower_band = bands['lower_band'].iloc[-1]
    close_price = df.iloc[idx]['close']
    
    if direction == 'bullish':
        # LONG: Candle must NOT close below lower band
        allowed = close_price >= lower_band
    else:
        # SHORT: Candle must NOT close above upper band
        allowed = close_price <= upper_band
    
    return {
        'allowed': allowed,
        'vwap': vwap_value,
        'upper_band': upper_band,
        'lower_band': lower_band,
        'close': close_price
    }


def get_vwap_bias(df: pd.DataFrame, idx: int) -> str:
    """Get trading bias based on VWAP: 'bullish', 'bearish', or 'neutral'"""
    vwap = calculate_vwap(df.iloc[:idx + 1])
    current_price = df.iloc[idx]['close']
    vwap_value = vwap.iloc[-1]
    threshold = (df.iloc[idx]['high'] - df.iloc[idx]['low']) * 0.5
    
    if current_price > vwap_value + threshold:
        return 'bullish'
    elif current_price < vwap_value - threshold:
        return 'bearish'
    return 'neutral'


# =============================================================================
# SMA - Simple Moving Average (for FOREX bias)
# =============================================================================

def calculate_sma(df: pd.DataFrame, period: int = 50) -> pd.Series:
    """Calculate Simple Moving Average"""
    return df['close'].rolling(window=period, min_periods=1).mean()


def calculate_sma_with_bands(df: pd.DataFrame, period: int = 50, band_pct: float = 0.5) -> dict:
    """
    Calculate SMA with upper and lower bands (percentage-based)
    
    Args:
        df: OHLC DataFrame
        period: SMA period (default 50)
        band_pct: Band distance as percentage of price (default 0.5%)
    
    Returns:
        Dict with 'sma', 'upper_band', 'lower_band' as pd.Series
    """
    sma = calculate_sma(df, period)
    
    # For FOREX, use percentage-based bands
    upper_band = sma * (1 + band_pct / 100)
    lower_band = sma * (1 - band_pct / 100)
    
    return {
        'sma': sma,
        'upper_band': upper_band,
        'lower_band': lower_band
    }


def check_sma_bias_filter(df: pd.DataFrame, idx: int, direction: str, period: int = 50) -> dict:
    """
    Check SMA bias filter for trade entry (FOREX)
    
    LONG: Price should be above SMA (bullish bias)
    SHORT: Price should be below SMA (bearish bias)
    
    Args:
        df: OHLC DataFrame
        idx: Current candle index
        direction: 'bullish' or 'bearish'
        period: SMA period (default 50)
    
    Returns:
        Dict with 'allowed', 'sma', 'close', 'bias'
    """
    if len(df) < period:
        # Not enough data, allow trade
        return {
            'allowed': True,
            'sma': df['close'].mean(),
            'close': df.iloc[idx]['close'],
            'bias': 'neutral'
        }
    
    sma = calculate_sma(df.iloc[:idx + 1], period)
    sma_value = sma.iloc[-1]
    close_price = df.iloc[idx]['close']
    
    # Determine bias
    if close_price > sma_value:
        bias = 'bullish'
    elif close_price < sma_value:
        bias = 'bearish'
    else:
        bias = 'neutral'
    
    # Check alignment
    if direction == 'bullish':
        # LONG: Price should be above SMA
        allowed = close_price > sma_value
    else:
        # SHORT: Price should be below SMA
        allowed = close_price < sma_value
    
    return {
        'allowed': allowed,
        'sma': sma_value,
        'close': close_price,
        'bias': bias
    }


def get_sma_bias(df: pd.DataFrame, idx: int, period: int = 50) -> str:
    """Get trading bias based on SMA: 'bullish', 'bearish', or 'neutral'"""
    if len(df) < period:
        return 'neutral'
    
    sma = calculate_sma(df.iloc[:idx + 1], period)
    current_price = df.iloc[idx]['close']
    sma_value = sma.iloc[-1]
    
    if current_price > sma_value:
        return 'bullish'
    elif current_price < sma_value:
        return 'bearish'
    return 'neutral'


# =============================================================================
# CONFLUENCE SCORING - Trade Quality Assessment
# =============================================================================

def calculate_confluence_score(
    df: pd.DataFrame,
    idx: int,
    ob_zone: Tuple[float, float],
    trade_direction: str,
    swing_highs: List[int] = None,
    swing_lows: List[int] = None,
    fvgs: List[FVG] = None,
    min_score: int = 3
) -> Dict[str, any]:
    """
    Calculate overall confluence score for a potential trade
    
    Factors:
    1. EMA alignment (+1)
    2. VWAP alignment (+1)
    3. RSI zone (+1 if oversold for longs, overbought for shorts)
    4. FVG confluence (+1)
    5. Liquidity sweep (+1)
    6. Volume confirmation (+1)
    7. Previous day high/low (+1)
    
    Returns:
        Dict with score, grade (A/B/C), and individual factor results
    """
    result = {'score': 0, 'max_score': 7, 'factors': {}}
    
    # 1. EMA alignment
    ema_trend = get_ema_trend(df, idx)
    result['factors']['ema'] = ema_trend == trade_direction or ema_trend == 'neutral'
    if result['factors']['ema']:
        result['score'] += 1
    
    # 2. VWAP alignment
    vwap_bias = get_vwap_bias(df, idx)
    result['factors']['vwap'] = vwap_bias == trade_direction or vwap_bias == 'neutral'
    if result['factors']['vwap']:
        result['score'] += 1
    
    # 3. RSI zone
    rsi = calculate_rsi(df.iloc[:idx + 1])
    rsi_value = rsi.iloc[-1] if not pd.isna(rsi.iloc[-1]) else 50
    rsi_zone = check_rsi_zone(rsi_value)
    result['factors']['rsi'] = {
        'value': round(rsi_value, 2),
        'zone': rsi_zone,
        'favorable': (trade_direction == 'bullish' and rsi_zone == 'oversold') or
                    (trade_direction == 'bearish' and rsi_zone == 'overbought')
    }
    if result['factors']['rsi']['favorable']:
        result['score'] += 1
    
    # 4. FVG confluence
    if fvgs:
        ob_mid = (ob_zone[0] + ob_zone[1]) / 2
        result['factors']['fvg'] = check_fvg_confluence(ob_mid, fvgs, trade_direction) is not None
        if result['factors']['fvg']:
            result['score'] += 1
    else:
        result['factors']['fvg'] = False
    
    # 5. Liquidity sweep
    if swing_highs and swing_lows:
        sweeps = detect_liquidity_sweeps(df.iloc[:idx + 1], swing_highs, swing_lows)
        recent_sweeps = [s for s in sweeps if s.idx >= idx - 10]
        result['factors']['liquidity_sweep'] = any(s.direction == trade_direction for s in recent_sweeps)
        if result['factors']['liquidity_sweep']:
            result['score'] += 1
    else:
        result['factors']['liquidity_sweep'] = False
    
    # 6. Volume confirmation
    result['factors']['volume'] = check_volume_confirmation(df, idx)
    if result['factors']['volume']:
        result['score'] += 1
    
    # 7. Previous day levels
    prev_levels = get_previous_session_levels(df, idx)
    if prev_levels:
        current_price = df.iloc[idx]['close']
        near_prev_low = abs(current_price - prev_levels.low) < (prev_levels.low * 0.002)
        near_prev_high = abs(current_price - prev_levels.high) < (prev_levels.high * 0.002)
        result['factors']['prev_day_level'] = (
            (trade_direction == 'bullish' and near_prev_low) or
            (trade_direction == 'bearish' and near_prev_high)
        )
        if result['factors']['prev_day_level']:
            result['score'] += 1
    else:
        result['factors']['prev_day_level'] = False
    
    # Calculate grade
    result['percentage'] = (result['score'] / result['max_score']) * 100
    result['grade'] = 'A' if result['percentage'] >= 70 else 'B' if result['percentage'] >= 50 else 'C'
    result['take_trade'] = result['score'] >= min_score
    
    return result


# =============================================================================
# MARKET STRUCTURE HELPERS
# =============================================================================

def detect_market_structure(df: pd.DataFrame, swing_highs: List[int], swing_lows: List[int]) -> str:
    """Detect overall market structure: 'uptrend', 'downtrend', or 'ranging'"""
    if len(swing_highs) < 2 or len(swing_lows) < 2:
        return 'ranging'
    
    last_sh = [df.iloc[i]['high'] for i in swing_highs[-2:]]
    last_sl = [df.iloc[i]['low'] for i in swing_lows[-2:]]
    
    higher_high = last_sh[-1] > last_sh[-2]
    higher_low = last_sl[-1] > last_sl[-2]
    lower_high = last_sh[-1] < last_sh[-2]
    lower_low = last_sl[-1] < last_sl[-2]
    
    if higher_high and higher_low:
        return 'uptrend'
    elif lower_high and lower_low:
        return 'downtrend'
    return 'ranging'


# =============================================================================
# CUMULATIVE VOLUME DELTA (CVD) - Order Flow Indicator
# =============================================================================

def calculate_cvd(df: pd.DataFrame, reset_daily: bool = True) -> pd.Series:
    """
    Calculate Cumulative Volume Delta (CVD).
    
    CVD measures the cumulative difference between buying and selling volume:
    - If close > open (bullish candle): add volume (buyers dominated)
    - If close < open (bearish candle): subtract volume (sellers dominated)
    - If close == open: neutral (no delta)
    
    Args:
        df: DataFrame with 'open', 'close', 'volume', and 'datetime' columns
        reset_daily: If True, reset CVD at start of each trading day
        
    Returns:
        pd.Series: CVD values for each candle
    """
    if 'volume' not in df.columns:
        # No volume data - return zeros
        return pd.Series(0, index=df.index)
    
    # Calculate delta for each candle
    delta = pd.Series(0.0, index=df.index)
    
    bullish_mask = df['close'] > df['open']
    bearish_mask = df['close'] < df['open']
    
    delta[bullish_mask] = df.loc[bullish_mask, 'volume']
    delta[bearish_mask] = -df.loc[bearish_mask, 'volume']
    
    if reset_daily and 'datetime' in df.columns:
        # Reset CVD at start of each day
        df_temp = df.copy()
        df_temp['date_only'] = df_temp['datetime'].dt.date
        df_temp['delta'] = delta
        
        # Cumulative sum within each day
        cvd = df_temp.groupby('date_only')['delta'].cumsum()
        return cvd
    else:
        # Single cumulative sum
        return delta.cumsum()


def check_cvd_confirmation(cvd_series: pd.Series, idx: int, direction: str, 
                           lookback: int = 10) -> Dict:
    """
    Check if CVD confirms the trade direction.
    
    Simple confirmation rules:
    - Long trades: CVD > 0 (buyers in control)
    - Short trades: CVD < 0 (sellers in control)
    
    Args:
        cvd_series: CVD values
        idx: Current candle index
        direction: 'bullish' or 'bearish'
        lookback: Number of candles to check for trend
        
    Returns:
        Dict with confirmation status and details
    """
    if idx >= len(cvd_series):
        return {'confirmed': False, 'cvd_value': 0, 'reason': 'Index out of range'}
    
    current_cvd = cvd_series.iloc[idx]
    
    # Get recent CVD values for context
    start_idx = max(0, idx - lookback)
    recent_cvd = cvd_series.iloc[start_idx:idx+1]
    
    result = {
        'confirmed': False,
        'cvd_value': current_cvd,
        'cvd_min': recent_cvd.min() if len(recent_cvd) > 0 else 0,
        'cvd_max': recent_cvd.max() if len(recent_cvd) > 0 else 0,
        'reason': ''
    }
    
    if direction == 'bullish':
        # For longs: CVD should be positive (buyers in control)
        if current_cvd > 0:
            result['confirmed'] = True
            result['reason'] = f'CVD positive ({current_cvd:.0f}) - buyers in control'
        else:
            result['reason'] = f'CVD negative ({current_cvd:.0f}) - sellers in control, skip long'
    else:
        # For shorts: CVD should be negative (sellers in control)
        if current_cvd < 0:
            result['confirmed'] = True
            result['reason'] = f'CVD negative ({current_cvd:.0f}) - sellers in control'
        else:
            result['reason'] = f'CVD positive ({current_cvd:.0f}) - buyers in control, skip short'
    
    return result


# =============================================================================
# LIQUIDITY SWEEP FILTER - Pre-CHoCH Sweep Detection
# =============================================================================

def check_pre_choch_liquidity_sweep(
    df: pd.DataFrame,
    direction: str,
    start_idx: int,
    end_idx: int,
    lookback: int = 10
) -> Dict:
    """
    Check if liquidity was swept before CHoCH (Option A - Pre-CHoCH Sweep).
    
    For LONGS: Between pullback and CHoCH, price should have swept below a swing low
              (wick went below, but close stayed above) - grabbed sell stops
    
    For SHORTS: Between pullback and CHoCH, price should have swept above a swing high
               (wick went above, but close stayed below) - grabbed buy stops
    
    Args:
        df: DataFrame with OHLC data
        direction: 'bullish' or 'bearish'
        start_idx: Index to start looking (pullback start or BOS idx)
        end_idx: Index to end looking (CHoCH idx or current candle)
        lookback: How far back to find swing points for sweep reference
        
    Returns:
        Dict with sweep confirmation and details
    """
    result = {
        'sweep_found': False,
        'sweep_idx': None,
        'sweep_level': None,
        'sweep_type': None,
        'reason': ''
    }
    
    if start_idx >= end_idx or end_idx >= len(df):
        result['reason'] = 'Invalid index range'
        return result
    
    # Find swing points before the range to use as liquidity levels
    pre_range_start = max(0, start_idx - lookback)
    pre_range_df = df.iloc[pre_range_start:start_idx]
    
    if len(pre_range_df) < 3:
        result['reason'] = 'Not enough data for swing detection'
        return result
    
    # Simple swing detection for liquidity levels
    swing_lows = []
    swing_highs = []
    
    for i in range(1, len(pre_range_df) - 1):
        idx = pre_range_start + i
        candle = df.iloc[idx]
        prev_candle = df.iloc[idx - 1]
        next_candle = df.iloc[idx + 1]
        
        # Swing low: lower than neighbors
        if candle['low'] < prev_candle['low'] and candle['low'] < next_candle['low']:
            swing_lows.append({'idx': idx, 'level': candle['low']})
        
        # Swing high: higher than neighbors
        if candle['high'] > prev_candle['high'] and candle['high'] > next_candle['high']:
            swing_highs.append({'idx': idx, 'level': candle['high']})
    
    if direction == 'bullish':
        # For LONGS: Look for sweep below a swing low in the range
        if not swing_lows:
            result['reason'] = 'No swing lows found for liquidity sweep check'
            return result
        
        # Check each candle in the range for a sweep
        for i in range(start_idx, end_idx + 1):
            candle = df.iloc[i]
            for sl in swing_lows:
                # Sweep: wick went below swing low, but close stayed above
                if candle['low'] < sl['level'] and candle['close'] > sl['level']:
                    result['sweep_found'] = True
                    result['sweep_idx'] = i
                    result['sweep_level'] = sl['level']
                    result['sweep_type'] = 'sell_stops_grabbed'
                    result['reason'] = f"Sell stops swept at {sl['level']:.2f} (idx {i})"
                    return result
    
    else:  # Bearish
        # For SHORTS: Look for sweep above a swing high in the range
        if not swing_highs:
            result['reason'] = 'No swing highs found for liquidity sweep check'
            return result
        
        # Check each candle in the range for a sweep
        for i in range(start_idx, end_idx + 1):
            candle = df.iloc[i]
            for sh in swing_highs:
                # Sweep: wick went above swing high, but close stayed below
                if candle['high'] > sh['level'] and candle['close'] < sh['level']:
                    result['sweep_found'] = True
                    result['sweep_idx'] = i
                    result['sweep_level'] = sh['level']
                    result['sweep_type'] = 'buy_stops_grabbed'
                    result['reason'] = f"Buy stops swept at {sh['level']:.2f} (idx {i})"
                    return result
    
    result['reason'] = f"No liquidity sweep found between idx {start_idx} and {end_idx}"
    return result


# =============================================================================
# VWAP BAND OB 2.0 FILTER - OB 2.0 must be inside VWAP bands
# =============================================================================

def check_ob2_inside_vwap_bands(
    df: pd.DataFrame,
    ob2_zone: tuple,
    idx: int,
    tolerance: float = 3.0
) -> Dict:
    """
    Check if OB 2.0 zone is inside VWAP upper and lower bands.
    
    For valid entry, OB 2.0 should be within VWAP bands (with tolerance).
    This ensures entry is at a fair value, not at extremes.
    
    Args:
        df: DataFrame with OHLC data
        ob2_zone: (high, low) of OB 2.0
        idx: Current candle index
        tolerance: Points tolerance beyond VWAP bands (default 3 points)
        
    Returns:
        Dict with VWAP band check result
    """
    result = {
        'inside_bands': False,
        'ob2_high': ob2_zone[0],
        'ob2_low': ob2_zone[1],
        'vwap': None,
        'vwap_upper': None,
        'vwap_lower': None,
        'reason': ''
    }
    
    # Calculate VWAP and bands up to current index
    df_slice = df.iloc[:idx+1].copy()
    
    if 'datetime' not in df_slice.columns or len(df_slice) < 5:
        result['reason'] = 'Insufficient data for VWAP calculation'
        result['inside_bands'] = True  # Don't filter if can't calculate
        return result
    
    # Calculate VWAP
    vwap = calculate_vwap(df_slice)
    if vwap is None or len(vwap) == 0 or pd.isna(vwap.iloc[-1]):
        result['reason'] = 'VWAP calculation failed'
        result['inside_bands'] = True  # Don't filter if can't calculate
        return result
    
    current_vwap = vwap.iloc[-1]
    
    # Calculate bands (1 std dev)
    df_slice['vwap'] = vwap
    df_slice['vwap_diff'] = (df_slice['close'] - df_slice['vwap']) ** 2
    
    # Group by date for daily VWAP std
    df_slice['date_only'] = df_slice['datetime'].dt.date
    today = df_slice['date_only'].iloc[-1]
    today_data = df_slice[df_slice['date_only'] == today]
    
    if len(today_data) < 5:
        result['reason'] = 'Not enough intraday data for VWAP bands'
        result['inside_bands'] = True
        return result
    
    std_dev = today_data['close'].std()
    vwap_upper = current_vwap + std_dev
    vwap_lower = current_vwap - std_dev
    
    result['vwap'] = current_vwap
    result['vwap_upper'] = vwap_upper
    result['vwap_lower'] = vwap_lower
    
    ob2_high, ob2_low = ob2_zone
    
    # Check if OB 2.0 zone is inside VWAP bands (with tolerance)
    # OB 2.0 high should be below VWAP upper + tolerance
    # OB 2.0 low should be above VWAP lower - tolerance
    upper_ok = ob2_high <= vwap_upper + tolerance
    lower_ok = ob2_low >= vwap_lower - tolerance
    
    if upper_ok and lower_ok:
        result['inside_bands'] = True
        result['reason'] = f"OB 2.0 ({ob2_low:.2f}-{ob2_high:.2f}) inside VWAP bands ({vwap_lower:.2f}-{vwap_upper:.2f})"
    else:
        if not upper_ok:
            result['reason'] = f"OB 2.0 high {ob2_high:.2f} above VWAP upper {vwap_upper:.2f} + {tolerance} tolerance"
        elif not lower_ok:
            result['reason'] = f"OB 2.0 low {ob2_low:.2f} below VWAP lower {vwap_lower:.2f} - {tolerance} tolerance"
    
    return result
