
from typing import Tuple, Optional, List
import pandas as pd
import numpy as np


def find_swing_highs_lows(df: pd.DataFrame, lookback: int = 5) -> Tuple[List[int], List[int]]:
    """
    Find swing highs and swing lows in the data
    
    A swing high is a high that is higher than `lookback` candles on both sides
    A swing low is a low that is lower than `lookback` candles on both sides
    """
    swing_highs = []
    swing_lows = []
    
    highs = df['high'].values
    lows = df['low'].values
    
    for i in range(lookback, len(df) - lookback):
        # Check swing high
        is_swing_high = True
        for j in range(1, lookback + 1):
            if highs[i] <= highs[i - j] or highs[i] <= highs[i + j]:
                is_swing_high = False
                break
        if is_swing_high:
            swing_highs.append(i)
        
        # Check swing low
        is_swing_low = True
        for j in range(1, lookback + 1):
            if lows[i] >= lows[i - j] or lows[i] >= lows[i + j]:
                is_swing_low = False
                break
        if is_swing_low:
            swing_lows.append(i)
    
    return swing_highs, swing_lows


def is_strong_bullish_candle(candle: pd.Series, body_threshold: float = 0.5) -> bool:
    """
    Check if candle is a strong bullish candle (BOS candidate)
    
    Criteria:
    - Green candle (close > open)
    - Body is at least 50% of range
    - Close is near the high (within top 30% of range)
    """
    body = candle['close'] - candle['open']
    range_ = candle['high'] - candle['low']
    
    if body <= 0 or range_ == 0:
        return False
    
    body_ratio = body / range_
    close_position = (candle['close'] - candle['low']) / range_
    
    return body_ratio >= body_threshold and close_position >= 0.7


def is_strong_bearish_candle(candle: pd.Series, body_threshold: float = 0.5) -> bool:
    """
    Check if candle is a strong bearish candle (BOS candidate)
    
    Criteria:
    - Red candle (close < open)
    - Body is at least 50% of range
    - Close is near the low (within bottom 30% of range)
    """
    body = candle['open'] - candle['close']
    range_ = candle['high'] - candle['low']
    
    if body <= 0 or range_ == 0:
        return False
    
    body_ratio = body / range_
    close_position = (candle['close'] - candle['low']) / range_
    
    return body_ratio >= body_threshold and close_position <= 0.3


def get_fibonacci_levels(high: float, low: float) -> dict:
    """
    Calculate Fibonacci retracement levels
    """
    diff = high - low
    return {
        '0': high,
        '23.6': high - 0.236 * diff,
        '38.2': high - 0.382 * diff,
        '50': high - 0.5 * diff,
        '61.8': high - 0.618 * diff,
        '78.6': high - 0.786 * diff,
        '100': low
    }


def detect_external_bos(
    df: pd.DataFrame,
    swing_highs: List[int],
    swing_lows: List[int],
    fib_retracement_level: float = 0.30
) -> Tuple[List[dict], List[dict]]:
    """
    Detect EXTERNAL BOS structure only.
    
    Bullish BOS Pattern:
    1. Swing Low → Swing High → Retracement to at least 30% fib 
       (but NOT break below the swing low) → Break above Swing High
    
    Bearish BOS Pattern:
    1. Swing High → Swing Low → Retracement to at least 30% fib
       (but NOT break above the swing high) → Break below Swing Low
    
    Returns: (bullish_bos_list, bearish_bos_list)
    """
    bullish_bos_list = []
    bearish_bos_list = []
    
    # Need at least some swings to work with
    if len(swing_highs) < 2 or len(swing_lows) < 2:
        return bullish_bos_list, bearish_bos_list
    
    # Track the last confirmed swing high and low for BOS reference
    last_swing_high_idx = None
    last_swing_high_val = None
    last_swing_low_idx = None
    last_swing_low_val = None
    
    # Track setup state
    bullish_setup_active = False  # Looking for bullish BOS after retracement
    bearish_setup_active = False  # Looking for bearish BOS after retracement
    
    bullish_fib_level = None  # 30% retracement target for bullish
    bearish_fib_level = None  # 30% retracement target for bearish
    
    # Track the swing low/high that started the move (for invalidation check)
    bullish_origin_low = None  # Swing low that started bullish move - must not break
    bearish_origin_high = None  # Swing high that started bearish move - must not break
    
    for i in range(10, len(df)):
        candle = df.iloc[i]
        
        # Update swings if current index is a swing
        if i in swing_highs:
            last_swing_high_idx = i
            last_swing_high_val = candle['high']
            
            # After a new swing high, setup for bullish BOS
            # Calculate fib level for retracement (30% from low to high)
            if last_swing_low_val is not None:
                fib_range = last_swing_high_val - last_swing_low_val
                bullish_fib_level = last_swing_high_val - (fib_range * fib_retracement_level)
                bullish_origin_low = last_swing_low_val  # Track the origin swing low
                bullish_setup_active = False  # Reset, need retracement first
        
        if i in swing_lows:
            last_swing_low_idx = i
            last_swing_low_val = candle['low']
            
            # After a new swing low, setup for bearish BOS
            if last_swing_high_val is not None:
                fib_range = last_swing_high_val - last_swing_low_val
                bearish_fib_level = last_swing_low_val + (fib_range * fib_retracement_level)
                bearish_origin_high = last_swing_high_val  # Track the origin swing high
                bearish_setup_active = False  # Reset, need retracement first
        
        # Check for retracement (activates setup)
        if bullish_fib_level is not None and not bullish_setup_active:
            # Retracement down to at least 30% fib level for bullish setup
            if candle['low'] <= bullish_fib_level:
                bullish_setup_active = True
        
        if bearish_fib_level is not None and not bearish_setup_active:
            # Retracement up to at least 30% fib level for bearish setup
            if candle['high'] >= bearish_fib_level:
                bearish_setup_active = True
        
        # Check for INVALIDATION - retracement went too far
        if bullish_setup_active and bullish_origin_low is not None:
            # Bullish setup invalidated if price breaks below the origin swing low
            if candle['close'] < bullish_origin_low:
                bullish_setup_active = False
        
        if bearish_setup_active and bearish_origin_high is not None:
            # Bearish setup invalidated if price breaks above the origin swing high
            if candle['close'] > bearish_origin_high:
                bearish_setup_active = False
        
        # Check for BOS after valid retracement
        if bullish_setup_active and last_swing_high_val is not None:
            # Bullish BOS: Close above the previous swing high
            if candle['close'] > last_swing_high_val:
                bullish_bos_list.append({
                    'idx': i,
                    'swing_high': last_swing_high_val,
                    'swing_high_idx': last_swing_high_idx,
                    'swing_low': last_swing_low_val,
                    'swing_low_idx': last_swing_low_idx,
                    'fib_retracement': bullish_fib_level,
                    'origin_low': bullish_origin_low
                })
                bullish_setup_active = False  # Reset after BOS
                # Update swing high to current
                last_swing_high_val = candle['high']
                last_swing_high_idx = i
        
        if bearish_setup_active and last_swing_low_val is not None:
            # Bearish BOS: Close below the previous swing low
            if candle['close'] < last_swing_low_val:
                bearish_bos_list.append({
                    'idx': i,
                    'swing_high': last_swing_high_val,
                    'swing_high_idx': last_swing_high_idx,
                    'swing_low': last_swing_low_val,
                    'swing_low_idx': last_swing_low_idx,
                    'fib_retracement': bearish_fib_level,
                    'origin_high': bearish_origin_high
                })
                bearish_setup_active = False  # Reset after BOS
                # Update swing low to current
                last_swing_low_val = candle['low']
                last_swing_low_idx = i
    
    return bullish_bos_list, bearish_bos_list


def find_order_block_v2(
    df: pd.DataFrame, 
    bos_idx: int, 
    pullback_idx: int,
    direction: str
) -> Optional[Tuple[float, float]]:
    """
    STEP 3: Find Order Block during pullback
    
    OB 1 Definition (Updated):
    - Find last 3 consecutive opposite-color candles before the BOS impulse
    
    For LONG (bullish BOS):
    - Find last 3 bearish candles before BOS
    - OB HIGH = highest high of those 3 candles
    - OB LOW = lowest low of the swing that created those candles
    
    For SHORT (bearish BOS):
    - Find last 3 bullish candles before BOS
    - OB LOW = lowest low of those 3 candles
    - OB HIGH = highest high of the swing that created those candles
    
    Returns: (ob_high, ob_low) or None
    """
    max_candles = 3
    
    if direction == 'bullish':
        # Find last opposite-color 3-minute candle (aligned to 3-min boundaries: :00, :03, :06, etc.)
        for i in range(bos_idx - 1, max(2, 0), -1):
            candle = df.iloc[i]
            
            # Check if this candle is at a 3-minute boundary (minute divisible by 3)
            if hasattr(candle.name, 'minute'):
                minute = candle.name.minute
            else:
                continue
            
            # Only process candles at 3-min boundaries (:00, :03, :06, :09, etc.)
            if minute % 3 != 0:
                continue
            
            # Get 3 candles starting from this boundary (e.g., 9:30, 9:31, 9:32)
            if i < 2:
                continue
            three_candles = df.iloc[i-2:i+1]  # Previous 2 + current = 3 candles
            if len(three_candles) < 3:
                continue
            
            # 3-min candle properties (open of first, close of last, high/low of all 3)
            three_min_open = three_candles.iloc[0]['open']
            three_min_close = three_candles.iloc[-1]['close']
            three_min_high = three_candles['high'].max()
            three_min_low = three_candles['low'].min()
            
            # Check if 3-min candle is bearish (opposite of bullish BOS)
            if three_min_close < three_min_open:
                ob_high = three_min_high
                ob_low = three_min_low
                return (ob_high, ob_low)
        
        return None
                
    else:  # Bearish
        # Find last opposite-color 3-minute candle (aligned to 3-min boundaries)
        for i in range(bos_idx - 1, max(2, 0), -1):
            candle = df.iloc[i]
            
            # Check if this candle is at a 3-minute boundary
            if hasattr(candle.name, 'minute'):
                minute = candle.name.minute
            else:
                continue
            
            # Only process candles at 3-min boundaries (:00, :03, :06, :09, etc.)
            if minute % 3 != 0:
                continue
            
            # Get 3 candles forming this 3-min period
            if i < 2:
                continue
            three_candles = df.iloc[i-2:i+1]
            if len(three_candles) < 3:
                continue
            
            # 3-min candle properties
            three_min_open = three_candles.iloc[0]['open']
            three_min_close = three_candles.iloc[-1]['close']
            three_min_high = three_candles['high'].max()
            three_min_low = three_candles['low'].min()
            
            # Check if 3-min candle is bullish (opposite of bearish BOS)
            if three_min_close > three_min_open:
                ob_high = three_min_high
                ob_low = three_min_low
                return (ob_high, ob_low)
    
    return None


def detect_choch_v2(
    df: pd.DataFrame, 
    idx: int, 
    direction: str,
    bos_swing_high: float,
    bos_swing_low: float,
    lookback: int = 10
) -> Optional[dict]:
    """
    STEP 5 (Updated): Detect Change of Character (CHoCH) - OB 2.0 Method
    
    CHoCH is confirmed when:
    - For LONG: Candle CLOSES ABOVE the recent highs that created the last BOS
    - For SHORT: Candle CLOSES BELOW the recent lows that created the last BOS
    
    CHoCH does NOT need to happen inside OB 1 zone.
    
    Returns: dict with choch_idx and swing info, or None
    """
    if idx < lookback:
        return None
    
    candle = df.iloc[idx]
    
    if direction == 'bullish':
        # Find the recent swing highs that created the BOS structure
        # Look for the highest high in the pullback that formed before current candle
        recent_data = df.iloc[max(0, idx - lookback):idx]
        if len(recent_data) == 0:
            return None
        
        # Find the recent lower highs during pullback
        swing_highs_in_pullback = []
        for i in range(1, len(recent_data) - 1):
            if (recent_data.iloc[i]['high'] > recent_data.iloc[i-1]['high'] and 
                recent_data.iloc[i]['high'] > recent_data.iloc[i+1]['high']):
                swing_highs_in_pullback.append((i, recent_data.iloc[i]['high']))
        
        if not swing_highs_in_pullback:
            # Use the max high in recent data as reference
            recent_high = recent_data['high'].max()
        else:
            # Use the most recent swing high
            recent_high = swing_highs_in_pullback[-1][1]
        
        # CHoCH: Candle closes above recent highs
        if candle['close'] > recent_high:
            # Find the lowest wick of the swing that created CHoCH
            choch_swing_low = recent_data['low'].min()
            return {
                'choch_idx': idx,
                'choch_high': recent_high,
                'choch_swing_low': choch_swing_low
            }
            
    else:  # Bearish
        # Find the recent swing lows during pullback
        recent_data = df.iloc[max(0, idx - lookback):idx]
        if len(recent_data) == 0:
            return None
        
        swing_lows_in_pullback = []
        for i in range(1, len(recent_data) - 1):
            if (recent_data.iloc[i]['low'] < recent_data.iloc[i-1]['low'] and 
                recent_data.iloc[i]['low'] < recent_data.iloc[i+1]['low']):
                swing_lows_in_pullback.append((i, recent_data.iloc[i]['low']))
        
        if not swing_lows_in_pullback:
            recent_low = recent_data['low'].min()
        else:
            recent_low = swing_lows_in_pullback[-1][1]
        
        # CHoCH: Candle closes below recent lows
        if candle['close'] < recent_low:
            choch_swing_high = recent_data['high'].max()
            return {
                'choch_idx': idx,
                'choch_low': recent_low,
                'choch_swing_high': choch_swing_high
            }
    
    return None


def find_ob_2_0(
    df: pd.DataFrame,
    choch_idx: int,
    direction: str,
    choch_swing_low: float = None,
    choch_swing_high: float = None,
    max_candles: int = 3
) -> Optional[Tuple[float, float]]:
    """
    STEP: Find OB 2.0 after CHoCH confirmation
    
    OB 2.0 Definition (Updated):
    - After CHoCH occurs, find the last 3 consecutive opposite-color candles before CHoCH
    
    For LONG: Find last 3 bearish candles before CHoCH
      - OB 2.0 HIGH = highest high of those 3 candles
      - OB 2.0 LOW = lowest low of the swing (all 3 candles)
      
    For SHORT: Find last 3 bullish candles before CHoCH
      - OB 2.0 LOW = lowest low of those 3 candles
      - OB 2.0 HIGH = highest high of the swing (all 3 candles)
    
    Entry: When price returns to OB 2.0
    SL: Low wick of OB 2.0 (for longs) or High wick of OB 2.0 (for shorts)
    
    Returns: (ob_high, ob_low) or None
    """
    if choch_idx < max_candles:
        return None
    
    if direction == 'bullish':
        # Find last opposite-color 3-minute candle (aligned to 3-min boundaries) before CHoCH
        for i in range(choch_idx - 1, max(2, 0), -1):
            candle = df.iloc[i]
            
            # Check if this candle is at a 3-minute boundary (minute divisible by 3)
            if hasattr(candle.name, 'minute'):
                minute = candle.name.minute
            else:
                continue
            
            # Only process candles at 3-min boundaries (:00, :03, :06, :09, etc.)
            if minute % 3 != 0:
                continue
            
            # Get 3 candles forming this 3-min period
            if i < 2:
                continue
            three_candles = df.iloc[i-2:i+1]
            if len(three_candles) < 3:
                continue
            
            # 3-min candle properties
            three_min_open = three_candles.iloc[0]['open']
            three_min_close = three_candles.iloc[-1]['close']
            three_min_high = three_candles['high'].max()
            three_min_low = three_candles['low'].min()
            
            # Check if 3-min candle is bearish (opposite of bullish trend)
            if three_min_close < three_min_open:
                ob_high = three_min_high
                ob_low = three_min_low
                return (ob_high, ob_low)
        
        return None
        
    else:  # Bearish
        # Find last opposite-color 3-minute candle (aligned to 3-min boundaries) before CHoCH
        for i in range(choch_idx - 1, max(2, 0), -1):
            candle = df.iloc[i]
            
            # Check if this candle is at a 3-minute boundary
            if hasattr(candle.name, 'minute'):
                minute = candle.name.minute
            else:
                continue
            
            # Only process candles at 3-min boundaries (:00, :03, :06, :09, etc.)
            if minute % 3 != 0:
                continue
            
            # Get 3 candles forming this 3-min period
            if i < 2:
                continue
            three_candles = df.iloc[i-2:i+1]
            if len(three_candles) < 3:
                continue
            
            # 3-min candle properties
            three_min_open = three_candles.iloc[0]['open']
            three_min_close = three_candles.iloc[-1]['close']
            three_min_high = three_candles['high'].max()
            three_min_low = three_candles['low'].min()
            
            # Check if 3-min candle is bullish (opposite of bearish trend)
            if three_min_close > three_min_open:
                ob_high = three_min_high
                ob_low = three_min_low
                return (ob_high, ob_low)
        
        return None


def detect_choch(
    df: pd.DataFrame, 
    idx: int, 
    direction: str,
    ob_high: float,
    ob_low: float,
    lookback: int = 5
) -> bool:
    """
    LEGACY STEP 5: Detect Change of Character (CHoCH)
    
    For LONG:
    - Inside OB zone, candle closes above the most recent lower high
    
    For SHORT:
    - Inside OB zone, candle closes below the most recent higher low
    
    NOTE: Use detect_choch_v2 for OB 2.0 method
    """
    if idx < lookback:
        return False
    
    candle = df.iloc[idx]
    
    if direction == 'bullish':
        # Check if candle is in/near OB zone
        if candle['low'] > ob_high or candle['high'] < ob_low:
            return False
        
        # Find most recent lower high in pullback
        recent_data = df.iloc[max(0, idx - lookback):idx]
        if len(recent_data) == 0:
            return False
        
        lower_highs = []
        prev_high = recent_data.iloc[0]['high']
        for _, row in recent_data.iterrows():
            if row['high'] < prev_high:
                lower_highs.append(row['high'])
            prev_high = row['high']
        
        if lower_highs:
            recent_lower_high = min(lower_highs)
            return candle['close'] > recent_lower_high
            
    else:  # Bearish
        # Check if candle is in/near OB zone
        if candle['high'] < ob_low or candle['low'] > ob_high:
            return False
        
        # Find most recent higher low in pullback
        recent_data = df.iloc[max(0, idx - lookback):idx]
        if len(recent_data) == 0:
            return False
        
        higher_lows = []
        prev_low = recent_data.iloc[0]['low']
        for _, row in recent_data.iterrows():
            if row['low'] > prev_low:
                higher_lows.append(row['low'])
            prev_low = row['low']
        
        if higher_lows:
            recent_higher_low = max(higher_lows)
            return candle['close'] < recent_higher_low
    
    return False


# ============ Legacy functions for backward compatibility ============

def is_bullish_bos(candle: pd.Series, body_threshold: float = 0.7) -> bool:
    """Legacy function - use is_strong_bullish_candle instead"""
    return is_strong_bullish_candle(candle, body_threshold=0.5)


def is_bearish_bos(candle: pd.Series, body_threshold: float = 0.7) -> bool:
    """Legacy function - use is_strong_bearish_candle instead"""
    return is_strong_bearish_candle(candle, body_threshold=0.5)


def find_external_bos(df: pd.DataFrame) -> Optional[List[Tuple[int, str]]]:
    """Legacy function for finding BOS candles"""
    bos_list = []
    for i in range(2, len(df)):
        if is_strong_bullish_candle(df.iloc[i]):
            bos_list.append((i, 'bullish'))
        elif is_strong_bearish_candle(df.iloc[i]):
            bos_list.append((i, 'bearish'))
    return bos_list if bos_list else None


def pullback_reaches_fib(df: pd.DataFrame, bos_idx: int, direction: str) -> Optional[int]:
    """Legacy function for checking pullback to 50% fib"""
    if bos_idx >= len(df) - 1:
        return None
        
    if direction == 'bullish':
        impulse_low = df.iloc[bos_idx]['low']
        impulse_high = df.iloc[bos_idx]['high']
        fib_50 = get_fibonacci_levels(impulse_high, impulse_low)['50']
        for i in range(bos_idx + 1, len(df)):
            if df.iloc[i]['low'] <= fib_50:
                return i
    elif direction == 'bearish':
        impulse_low = df.iloc[bos_idx]['low']
        impulse_high = df.iloc[bos_idx]['high']
        fib_50 = get_fibonacci_levels(impulse_high, impulse_low)['50']
        for i in range(bos_idx + 1, len(df)):
            if df.iloc[i]['high'] >= fib_50:
                return i
    return None


def find_order_block(df: pd.DataFrame, bos_idx: int, direction: str) -> Optional[Tuple[float, float, int, int]]:
    """Legacy function for finding order block"""
    if direction == 'bullish':
        for i in range(bos_idx - 1, 0, -1):
            if df.iloc[i]['close'] < df.iloc[i]['open'] and df.iloc[i - 1]['close'] < df.iloc[i - 1]['open']:
                ob_high = max(df.iloc[i]['high'], df.iloc[i - 1]['high'])
                ob_low = min(df.iloc[i]['low'], df.iloc[i - 1]['low'])
                return ob_high, ob_low, i - 1, i
    elif direction == 'bearish':
        for i in range(bos_idx - 1, 0, -1):
            if df.iloc[i]['close'] > df.iloc[i]['open'] and df.iloc[i - 1]['close'] > df.iloc[i - 1]['open']:
                ob_high = max(df.iloc[i]['high'], df.iloc[i - 1]['high'])
                ob_low = min(df.iloc[i]['low'], df.iloc[i - 1]['low'])
                return ob_high, ob_low, i - 1, i
    return None


def is_choch(candle: pd.Series, direction: str, ob_high: float, ob_low: float) -> bool:
    """Legacy CHoCH detection"""
    if direction == 'bullish':
        return candle['low'] <= ob_low
    elif direction == 'bearish':
        return candle['high'] >= ob_high
    return False
