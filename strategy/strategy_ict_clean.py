"""
Clean ICT Strategy - Simplified for SPX500
===========================================

Core Logic:
1. 2 BOS in same direction confirms trend
2. Wait for opposite BOS (liquidity sweep) in confirmed trend
3. OB1 identified during pullback to 30-50% Fib
4. Price must touch OB1 before CHoCH is valid
5. CHoCH confirmation (close through opposite swing level)
6. OB2.0 entry using 3-min aggregated candles (last opposite-color 3-min candle before CHoCH)
7. SL below OB2.0 zone (extended to include any wicks) + 1pt buffer

Trade Management:
- TP1 at 1:1 R:R: NO partial booking, just move SL to Cost-to-Cost (breakeven)
- TP2 at 2:1 R:R: Book 50% profit
- TP3 at 3:1 R:R: Book 25% profit
- Final at day high/low: Book remaining 25% (let it run)

Filters: BOS Liquidity Sweep only (opposite BOS in confirmed trend)
"""

from dataclasses import dataclass, field
from typing import Optional, List, Tuple, Any
import pandas as pd
from .utils import find_swing_highs_lows, get_fibonacci_levels


@dataclass
class TradeSignalICT:
    direction: str
    entry_price: float
    stop_loss: float
    tp1: float          # 1:1 R:R
    tp2: float          # 2:1 R:R
    tp3: float          # 3:1 R:R
    tp_final: float     # Day high/low (final target)
    entry_time: Any
    ob1_zone: Tuple[float, float]
    ob2_zone: Tuple[float, float]
    ob2_extended_zone: Tuple[float, float]
    bos_count: int
    choch_level: float
    sweep_level: float = None
    risk_points: float = 0
    risk_amount: float = 0
    dollar_per_point: float = 0
    # Original SL tracking (for R:R calculation)
    original_sl: float = None
    # Exit tracking - New partial scheme
    tp1_hit: bool = False           # 1:1 - No booking, just move SL to CTC
    tp2_hit: bool = False           # 2:1 - Book 50%
    tp3_hit: bool = False           # 3:1 - Book 25%
    tp_final_hit: bool = False      # Day high/low - Book remaining 25%
    sl_hit: bool = False
    sl_moved_to_be: bool = False
    position_size: float = 100.0    # Current position % (100 -> 50 -> 25 -> 0)
    remaining_position: float = 1.0  # 1.0 -> 0.5 -> 0.25 -> 0
    exit_time: Any = None
    exit_price: float = None
    exit_reason: str = None
    pnl_points: float = 0
    realized_pnl: float = 0


class StrategyICTClean:
    """
    Clean ICT Strategy with BOS Sweep Filter
    
    Entry Rules:
    1. 2 BOS confirms trend direction
    2. Wait for opposite BOS (liquidity sweep)
    3. After sweep, find OB1 and wait for CHoCH
    4. Enter at OB2.0 retest
    5. SL below OB2.0 (extended for wicks) - 1pt buffer
    
    Filters (for 60%+ TP1 rate):
    - Session Filter: Only SPX market hours (9:30 AM - 4:00 PM ET)
    - Min SL Distance: 5 points (avoid tiny SL)
    - Max SL Distance: 15 points (avoid overextended entries)
    """
    
    # SPX Market Hours (US Eastern)
    MARKET_OPEN_HOUR = 9
    MARKET_OPEN_MINUTE = 30
    MARKET_CLOSE_HOUR = 16
    MARKET_CLOSE_MINUTE = 0
    MIN_SL_DISTANCE = 2.0   # Minimum SL distance in points (relaxed)
    MAX_SL_DISTANCE = 25.0  # Maximum SL distance in points (relaxed)
    
    def __init__(
        self,
        lot_size: int = 1,
        sl_buffer: float = 1.0,
        min_rr: float = 2.0,
        bos_required: int = 2,
        fib_min: float = 0.3,
        fib_max: float = 0.5,
        swing_lookback: int = 5,
        capital: float = 100000.0,
        risk_percent: float = 1.0,
        session_start_hour: int = 16,
        session_start_minute: int = 30,
        use_session_filter: bool = True,
        use_sl_distance_filter: bool = True,
        min_sl_distance: float = None,
        max_sl_distance: float = None,
        market_open_hour: int = None,
        market_open_minute: int = None,
        market_close_hour: int = None,
        market_close_minute: int = None
    ):
        self.lot_size = lot_size
        self.sl_buffer = sl_buffer
        self.min_rr = min_rr
        self.bos_required = bos_required
        self.fib_min = fib_min
        self.fib_max = fib_max
        self.swing_lookback = swing_lookback
        self.capital = capital
        self.risk_percent = risk_percent
        self.risk_amount = capital * risk_percent / 100
        self.session_start_hour = session_start_hour
        self.session_start_minute = session_start_minute
        self.use_session_filter = use_session_filter
        self.use_sl_distance_filter = use_sl_distance_filter
        # Allow overriding SL distance limits
        if min_sl_distance is not None:
            self.MIN_SL_DISTANCE = min_sl_distance
        if max_sl_distance is not None:
            self.MAX_SL_DISTANCE = max_sl_distance
        # Allow overriding market hours
        if market_open_hour is not None:
            self.MARKET_OPEN_HOUR = market_open_hour
        if market_open_minute is not None:
            self.MARKET_OPEN_MINUTE = market_open_minute
        if market_close_hour is not None:
            self.MARKET_CLOSE_HOUR = market_close_hour
        if market_close_minute is not None:
            self.MARKET_CLOSE_MINUTE = market_close_minute
        
        self._reset_state()
    
    def _reset_state(self):
        """Reset all state for new day"""
        # Trend confirmation
        self.confirmed_trend = None
        self.bos_count_bullish = 0
        self.bos_count_bearish = 0
        
        # BOS tracking
        self.bos_history = []
        self.last_bullish_bos_swing_low = None
        self.last_bearish_bos_swing_high = None
        
        # Sweep tracking
        self.sweep_occurred = False
        self.sweep_direction = None
        self.sweep_level = None
        
        # Current setup
        self.current_bos = None
        self.pullback_valid = False
        self.ob1_zone = None
        self.ob1_touched = False  # Price must touch OB1 before CHoCH is valid
        self.ob1_touch_idx = None
        self.choch_detected = False
        self.choch_idx = None
        self.choch_level = None
        self.ob2_zone = None
        self.ob2_extended_zone = None
        self.waiting_for_entry = False
        self.trade_taken = False
        
        # Active trade
        self.active_signal = None
    
    def _find_recent_swing_low(self, df: pd.DataFrame, current_idx: int, lookback: int = 10) -> float:
        """Find the most recent swing low for trailing stop (bullish trades)"""
        if current_idx < 3:
            return None
        
        start_idx = max(0, current_idx - lookback)
        swing_low = None
        
        for i in range(current_idx - 1, start_idx, -1):
            if i < 1 or i >= len(df) - 1:
                continue
            # A swing low: current low is lower than both neighbors
            if df.iloc[i]['low'] < df.iloc[i-1]['low'] and df.iloc[i]['low'] < df.iloc[i+1]['low']:
                swing_low = df.iloc[i]['low']
                break
        
        return swing_low
    
    def _find_recent_swing_high(self, df: pd.DataFrame, current_idx: int, lookback: int = 10) -> float:
        """Find the most recent swing high for trailing stop (bearish trades)"""
        if current_idx < 3:
            return None
        
        start_idx = max(0, current_idx - lookback)
        swing_high = None
        
        for i in range(current_idx - 1, start_idx, -1):
            if i < 1 or i >= len(df) - 1:
                continue
            # A swing high: current high is higher than both neighbors
            if df.iloc[i]['high'] > df.iloc[i-1]['high'] and df.iloc[i]['high'] > df.iloc[i+1]['high']:
                swing_high = df.iloc[i]['high']
                break
        
        return swing_high
    
    def _detect_bos(
        self, 
        df: pd.DataFrame, 
        idx: int, 
        swing_highs: List[int], 
        swing_lows: List[int]
    ) -> Optional[dict]:
        """Detect Break of Structure
        
        Args:
            df: DataFrame with price data
            idx: Current candle index
            swing_highs: List of swing high indices
            swing_lows: List of swing low indices
        """
        if idx < 2:
            return None
        
        candle = df.iloc[idx]
        prev_candle = df.iloc[idx - 1]
        
        # Get recent swing levels (swing_highs and swing_lows are lists of indices)
        recent_swing_high = None
        recent_swing_low = None
        recent_swing_high_idx = None
        recent_swing_low_idx = None
        
        # Find most recent swing high before current index
        for sh_idx in sorted(swing_highs, reverse=True):
            if sh_idx < idx - 1:
                recent_swing_high_idx = sh_idx
                recent_swing_high = df.iloc[sh_idx]['high']
                break
        
        # Find most recent swing low before current index
        for sl_idx in sorted(swing_lows, reverse=True):
            if sl_idx < idx - 1:
                recent_swing_low_idx = sl_idx
                recent_swing_low = df.iloc[sl_idx]['low']
                break
        
        if recent_swing_high is None or recent_swing_low is None:
            return None
        
        # Bullish BOS: Close above swing high
        if candle['close'] > recent_swing_high and prev_candle['close'] <= recent_swing_high:
            return {
                'direction': 'bullish',
                'idx': idx,
                'level': recent_swing_high,
                'swing_high': recent_swing_high,
                'swing_low': recent_swing_low,
                'high': candle['high'],
                'low': candle['low']
            }
        
        # Bearish BOS: Close below swing low
        if candle['close'] < recent_swing_low and prev_candle['close'] >= recent_swing_low:
            return {
                'direction': 'bearish',
                'idx': idx,
                'level': recent_swing_low,
                'swing_high': recent_swing_high,
                'swing_low': recent_swing_low,
                'high': candle['high'],
                'low': candle['low']
            }
        
        return None
    
    def _check_pullback(self, df: pd.DataFrame, idx: int, bos: dict) -> str:
        """Check if price has pulled back to 30-70% Fib level (more relaxed range)"""
        candle = df.iloc[idx]
        
        if bos['direction'] == 'bullish':
            # Pullback = price moves down toward swing low
            range_high = bos['high']
            range_low = bos['swing_low']
            range_size = range_high - range_low
            
            if range_size <= 0:
                return 'waiting'
            
            # Fib levels (30% to 70% retracement from high)
            fib_30 = range_high - range_size * 0.30  # Higher level
            fib_70 = range_high - range_size * 0.70  # Lower level
            
            # Check if low touched the fib zone (30-70% retracement)
            if candle['low'] <= fib_30 and candle['low'] >= fib_70:
                return 'valid'
            # Also valid if already pulled back deeper (but not breaking structure)
            if candle['low'] < fib_70 and candle['low'] > range_low:
                return 'valid'
            if candle['low'] <= range_low:
                return 'invalidated'
        else:
            # Bearish: Pullback = price moves up toward swing high
            range_high = bos['swing_high']
            range_low = bos['low']
            range_size = range_high - range_low
            
            if range_size <= 0:
                return 'waiting'
            
            # Fib levels (30% to 70% retracement from low)
            fib_30 = range_low + range_size * 0.30  # Lower level
            fib_70 = range_low + range_size * 0.70  # Higher level
            
            # Check if high touched the fib zone
            if candle['high'] >= fib_30 and candle['high'] <= fib_70:
                return 'valid'
            # Also valid if already pulled back deeper
            if candle['high'] > fib_70 and candle['high'] < range_high:
                return 'valid'
            if candle['high'] >= range_high:
                return 'invalidated'
        
        return 'waiting'
    
    def _find_ob1(
        self, 
        df: pd.DataFrame, 
        bos_idx: int, 
        pullback_idx: int, 
        direction: str,
        debug: bool = False
    ) -> Optional[Tuple[float, float]]:
        """
        Find Order Block 1 - The LOWEST opposite-color candle + extend for any wicks below
        
        For BULLISH:
        - Find the lowest bearish (red) candle between BOS and pullback
        - Extend zone to include any lower wicks from nearby candles
        
        For BEARISH:
        - Find the highest bullish (green) candle between BOS and pullback
        - Extend zone to include any higher wicks from nearby candles
        """
        candidates = []
        
        # Search range: from pullback back toward BOS
        search_start = max(bos_idx - 5, 0)
        search_end = min(pullback_idx + 5, len(df))
        
        if direction == 'bullish':
            # Find ALL bearish candles in the range
            for i in range(search_start, search_end):
                candle = df.iloc[i]
                if candle['close'] < candle['open']:  # Bearish candle
                    candidates.append({
                        'idx': i,
                        'high': candle['high'],
                        'low': candle['low'],
                        'body_low': min(candle['open'], candle['close'])
                    })
            
            if not candidates:
                return None
            
            # Find the one with the LOWEST low (the deepest bearish candle)
            lowest_candle = min(candidates, key=lambda x: x['low'])
            ob1_high = lowest_candle['high']
            ob1_low = lowest_candle['low']
            ob1_idx = lowest_candle['idx']
            
            # Extend: Check for any candles with wicks below this OB1
            for i in range(ob1_idx, min(ob1_idx + 10, len(df))):
                candle = df.iloc[i]
                if candle['low'] < ob1_low:
                    if debug:
                        print(f"   📏 Extending OB1 low: {ob1_low:.2f} -> {candle['low']:.2f}")
                    ob1_low = candle['low']
            
            if debug:
                print(f"   📦 OB1 (BULLISH): High {ob1_high:.2f}, Low {ob1_low:.2f}")
            
            return (ob1_high, ob1_low)
        
        else:  # Bearish
            # Find ALL bullish candles in the range
            for i in range(search_start, search_end):
                candle = df.iloc[i]
                if candle['close'] > candle['open']:  # Bullish candle
                    candidates.append({
                        'idx': i,
                        'high': candle['high'],
                        'low': candle['low'],
                        'body_high': max(candle['open'], candle['close'])
                    })
            
            if not candidates:
                return None
            
            # Find the one with the HIGHEST high (the highest bullish candle)
            highest_candle = max(candidates, key=lambda x: x['high'])
            ob1_high = highest_candle['high']
            ob1_low = highest_candle['low']
            ob1_idx = highest_candle['idx']
            
            # Extend: Check for any candles with wicks above this OB1
            for i in range(ob1_idx, min(ob1_idx + 10, len(df))):
                candle = df.iloc[i]
                if candle['high'] > ob1_high:
                    if debug:
                        print(f"   📏 Extending OB1 high: {ob1_high:.2f} -> {candle['high']:.2f}")
                    ob1_high = candle['high']
            
            if debug:
                print(f"   📦 OB1 (BEARISH): High {ob1_high:.2f}, Low {ob1_low:.2f}")
            
            return (ob1_high, ob1_low)
    
    def _detect_choch(
        self, 
        df: pd.DataFrame, 
        idx: int, 
        direction: str,
        choch_level: float
    ) -> bool:
        """Detect Change of Character"""
        if choch_level is None:
            return False
        
        candle = df.iloc[idx]
        
        if direction == 'bullish':
            # For bullish entry: Need close above the last bearish BOS swing high
            if candle['close'] > choch_level:
                return True
        else:
            # For bearish entry: Need close below the last bullish BOS swing low
            if candle['close'] < choch_level:
                return True
        
        return False
    
    def _aggregate_to_3min(self, df: pd.DataFrame, end_idx: int, num_3min_candles: int = 10) -> pd.DataFrame:
        """
        Aggregate 1-min candles to 3-min candles for OB2.0 detection
        Returns aggregated DataFrame going back from end_idx
        """
        aggregated = []
        
        # Go back in 3-candle chunks
        for i in range(num_3min_candles):
            start_idx = end_idx - (i + 1) * 3 + 1
            end_i = end_idx - i * 3
            
            if start_idx < 0:
                break
            
            chunk = df.iloc[start_idx:end_i + 1]
            if len(chunk) == 0:
                break
            
            agg_candle = {
                'open': chunk.iloc[0]['open'],
                'high': chunk['high'].max(),
                'low': chunk['low'].min(),
                'close': chunk.iloc[-1]['close'],
                'start_idx': start_idx,
                'end_idx': end_i
            }
            aggregated.append(agg_candle)
        
        return aggregated
    
    def _find_ob2(
        self, 
        df: pd.DataFrame, 
        choch_idx: int, 
        direction: str,
        debug: bool = False
    ) -> Optional[Tuple[float, float, int]]:
        """
        Find OB 2.0 using 3-minute candles - Last opposite-color 3-min candle before CHoCH
        Returns: (high, low, end_idx)
        """
        # Aggregate to 3-min candles
        candles_3min = self._aggregate_to_3min(df, choch_idx - 1, num_3min_candles=10)
        
        if not candles_3min:
            return None
        
        ob2_high = None
        ob2_low = None
        ob2_end_idx = None
        
        for candle in candles_3min:
            if direction == 'bullish':
                # Look for bearish 3-min candle (close < open)
                if candle['close'] < candle['open']:
                    ob2_high = candle['high']
                    ob2_low = candle['low']
                    ob2_end_idx = candle['end_idx']
                    if debug:
                        print(f"   📍 OB2.0 (3min): High {ob2_high:.2f}, Low {ob2_low:.2f}")
                        print(f"      Candle range: idx {candle['start_idx']} to {candle['end_idx']}")
                    return (ob2_high, ob2_low, ob2_end_idx)
            else:
                # Look for bullish 3-min candle (close > open)
                if candle['close'] > candle['open']:
                    ob2_high = candle['high']
                    ob2_low = candle['low']
                    ob2_end_idx = candle['end_idx']
                    if debug:
                        print(f"   📍 OB2.0 (3min): High {ob2_high:.2f}, Low {ob2_low:.2f}")
                        print(f"      Candle range: idx {candle['start_idx']} to {candle['end_idx']}")
                    return (ob2_high, ob2_low, ob2_end_idx)
        
        return None
    
    def _extend_ob2_zone(
        self,
        df: pd.DataFrame,
        ob2_result: Tuple[float, float, int],
        choch_idx: int,
        direction: str,
        debug: bool = False
    ) -> Tuple[float, float]:
        """
        Extend OB2.0 zone to include any wicks beyond the zone.
        Same logic as OB1 extension:
        - For bullish: Find any candle with wick below OB2.0 low and extend
        - For bearish: Find any candle with wick above OB2.0 high and extend
        """
        ob2_high, ob2_low, ob2_end_idx = ob2_result
        extended_high = ob2_high
        extended_low = ob2_low
        
        # Scan from OB2.0 end to CHoCH (and a bit beyond) for wicks
        scan_end = min(choch_idx + 10, len(df))
        
        for i in range(ob2_end_idx, scan_end):
            if i >= len(df):
                break
            candle = df.iloc[i]
            
            if direction == 'bullish':
                # Look for any wick that goes BELOW the OB2.0 low
                if candle['low'] < extended_low:
                    if debug:
                        print(f"   📏 Extending OB2.0 low: {extended_low:.2f} -> {candle['low']:.2f} (at {candle.name})")
                    extended_low = candle['low']
            else:
                # Look for any wick that goes ABOVE the OB2.0 high
                if candle['high'] > extended_high:
                    if debug:
                        print(f"   📏 Extending OB2.0 high: {extended_high:.2f} -> {candle['high']:.2f} (at {candle.name})")
                    extended_high = candle['high']
        
        if debug and (extended_low != ob2_low or extended_high != ob2_high):
            print(f"   📦 OB2.0 Extended Zone: High {extended_high:.2f}, Low {extended_low:.2f}")
        
        return (extended_high, extended_low)
    
    def _manage_trade(self, df: pd.DataFrame, idx: int, debug: bool = False) -> bool:
        """
        Manage active trade - Original risk management logic:
        - TP1 (1:1): NO partial booking, just move SL to Cost-to-Cost (breakeven)
        - TP2 (1:2): Book 50% profit
        - TP3 (Final Target): Book remaining 50%
        """
        if self.active_signal is None:
            return False
        
        candle = df.iloc[idx]
        signal = self.active_signal
        pnl_per_point = signal.dollar_per_point if signal.dollar_per_point > 0 else 1.0
        
        if signal.direction == 'bullish':
            # Check SL first
            if candle['low'] <= signal.stop_loss:
                if getattr(signal, 'trailing_active', False) and signal.tp3_hit:
                    # Trailing stop hit after TP3 - book remaining at trailed SL price
                    exit_pnl = (signal.stop_loss - signal.entry_price) * pnl_per_point * signal.remaining_position
                    signal.realized_pnl += exit_pnl
                    signal.pnl_points += (signal.stop_loss - signal.entry_price) * signal.remaining_position
                    signal.remaining_position = 0
                    signal.position_size = 0
                    signal.exit_time = candle.name
                    signal.exit_price = signal.stop_loss
                    signal.exit_reason = "TRAIL_SL"
                    if debug:
                        print(f"   📈 Trailing SL Hit at {candle.name}: {signal.stop_loss:.2f} | Remaining closed with profit")
                elif signal.sl_moved_to_be:
                    # BE/CTC hit - close remaining position at entry price
                    exit_pnl = 0  # No gain/loss on remaining position at CTC
                    signal.realized_pnl += exit_pnl
                    signal.remaining_position = 0
                    signal.position_size = 0
                    signal.exit_time = candle.name
                    signal.exit_price = signal.entry_price
                    signal.exit_reason = "SL_CTC"
                    if debug:
                        print(f"   ❌ SL CTC Hit at {candle.name} (BE)")
                else:
                    # Full SL hit - 100% position closed at loss
                    exit_pnl = (signal.stop_loss - signal.entry_price) * pnl_per_point * signal.remaining_position
                    signal.realized_pnl += exit_pnl
                    signal.sl_hit = True
                    signal.remaining_position = 0
                    signal.position_size = 0
                    signal.pnl_points = signal.stop_loss - signal.entry_price
                    signal.exit_time = candle.name
                    signal.exit_price = signal.stop_loss
                    signal.exit_reason = "SL_HIT"
                    if debug:
                        print(f"   ❌ SL Hit at {candle.name}: {signal.stop_loss:.2f}")
                return True
            
            # Check TP1 (1:1) - NO partial booking, just move SL to CTC (Cost-to-Cost)
            if not signal.tp1_hit and candle['high'] >= signal.tp1:
                signal.tp1_hit = True
                signal.sl_moved_to_be = True
                signal.stop_loss = signal.entry_price  # Move SL to breakeven
                # NO profit booking at TP1 - just trail protection
                if debug:
                    print(f"   ✅ TP1 Hit at {candle.name}: {signal.tp1:.2f} | SL -> CTC (BE)")
            
            # Check TP2 (2:1) - Book 50% profit here
            if signal.tp1_hit and not signal.tp2_hit and candle['high'] >= signal.tp2:
                signal.tp2_hit = True
                partial_pnl = (signal.tp2 - signal.entry_price) * pnl_per_point * 0.5
                signal.realized_pnl += partial_pnl
                signal.pnl_points += (signal.tp2 - signal.entry_price) * 0.5
                signal.remaining_position = 0.5
                signal.position_size = 50.0
                if debug:
                    print(f"   ✅ TP2 Hit at {candle.name}: {signal.tp2:.2f} | 50% booked, 50% remaining")
            
            # Check TP3 (3:1) - Book 25% profit (half of remaining), start trailing
            if signal.tp2_hit and not signal.tp3_hit and candle['high'] >= signal.tp3:
                signal.tp3_hit = True
                partial_pnl = (signal.tp3 - signal.entry_price) * pnl_per_point * 0.25
                signal.realized_pnl += partial_pnl
                signal.pnl_points += (signal.tp3 - signal.entry_price) * 0.25
                signal.remaining_position = 0.25
                signal.position_size = 25.0
                signal.trailing_active = True
                if debug:
                    print(f"   ✅ TP3 Hit at {candle.name}: {signal.tp3:.2f} | 25% booked, 25% remaining, trailing started")
            
            # After TP3: Trail SL using recent swing lows (1 point below)
            if signal.tp3_hit and signal.remaining_position > 0:
                # Find recent swing low for trailing
                recent_swing_low = self._find_recent_swing_low(df, idx, lookback=10)
                if recent_swing_low is not None:
                    new_trail_sl = recent_swing_low - 1.0  # 1 point below swing low
                    # Only move SL up, never down
                    if new_trail_sl > signal.stop_loss:
                        old_sl = signal.stop_loss
                        signal.stop_loss = new_trail_sl
                        if debug:
                            print(f"   📈 Trail SL: {old_sl:.2f} -> {new_trail_sl:.2f} (swing low: {recent_swing_low:.2f})")
        
        else:  # Bearish
            # Check SL first
            if candle['high'] >= signal.stop_loss:
                if getattr(signal, 'trailing_active', False) and signal.tp3_hit:
                    # Trailing stop hit after TP3 - book remaining at trailed SL price
                    exit_pnl = (signal.entry_price - signal.stop_loss) * pnl_per_point * signal.remaining_position
                    signal.realized_pnl += exit_pnl
                    signal.pnl_points += (signal.entry_price - signal.stop_loss) * signal.remaining_position
                    signal.remaining_position = 0
                    signal.position_size = 0
                    signal.exit_time = candle.name
                    signal.exit_price = signal.stop_loss
                    signal.exit_reason = "TRAIL_SL"
                    if debug:
                        print(f"   📉 Trailing SL Hit at {candle.name}: {signal.stop_loss:.2f} | Remaining closed with profit")
                elif signal.sl_moved_to_be:
                    # BE/CTC hit
                    exit_pnl = 0
                    signal.realized_pnl += exit_pnl
                    signal.remaining_position = 0
                    signal.position_size = 0
                    signal.exit_time = candle.name
                    signal.exit_price = signal.entry_price
                    signal.exit_reason = "SL_CTC"
                    if debug:
                        print(f"   ❌ SL CTC Hit at {candle.name} (BE)")
                else:
                    # Full SL hit
                    exit_pnl = (signal.entry_price - signal.stop_loss) * pnl_per_point * signal.remaining_position
                    signal.realized_pnl += exit_pnl
                    signal.sl_hit = True
                    signal.remaining_position = 0
                    signal.position_size = 0
                    signal.pnl_points = signal.entry_price - signal.stop_loss
                    signal.exit_time = candle.name
                    signal.exit_price = signal.stop_loss
                    signal.exit_reason = "SL_HIT"
                    if debug:
                        print(f"   ❌ SL Hit at {candle.name}: {signal.stop_loss:.2f}")
                return True
            
            # Check TP1 (1:1) - NO partial booking, just move SL to CTC
            if not signal.tp1_hit and candle['low'] <= signal.tp1:
                signal.tp1_hit = True
                signal.sl_moved_to_be = True
                signal.stop_loss = signal.entry_price  # Move SL to breakeven
                # NO profit booking at TP1
                if debug:
                    print(f"   ✅ TP1 Hit at {candle.name}: {signal.tp1:.2f} | SL -> CTC (BE)")
            
            # Check TP2 (2:1) - Book 50% profit
            if signal.tp1_hit and not signal.tp2_hit and candle['low'] <= signal.tp2:
                signal.tp2_hit = True
                partial_pnl = (signal.entry_price - signal.tp2) * pnl_per_point * 0.5
                signal.realized_pnl += partial_pnl
                signal.pnl_points += (signal.entry_price - signal.tp2) * 0.5
                signal.remaining_position = 0.5
                signal.position_size = 50.0
                if debug:
                    print(f"   ✅ TP2 Hit at {candle.name}: {signal.tp2:.2f} | 50% booked, 50% remaining")
            
            # Check TP3 (3:1) - Book 25% profit (half of remaining), start trailing
            if signal.tp2_hit and not signal.tp3_hit and candle['low'] <= signal.tp3:
                signal.tp3_hit = True
                partial_pnl = (signal.entry_price - signal.tp3) * pnl_per_point * 0.25
                signal.realized_pnl += partial_pnl
                signal.pnl_points += (signal.entry_price - signal.tp3) * 0.25
                signal.remaining_position = 0.25
                signal.position_size = 25.0
                signal.trailing_active = True
                if debug:
                    print(f"   ✅ TP3 Hit at {candle.name}: {signal.tp3:.2f} | 25% booked, 25% remaining, trailing started")
            
            # After TP3: Trail SL using recent swing highs (1 point above)
            if signal.tp3_hit and signal.remaining_position > 0:
                # Find recent swing high for trailing
                recent_swing_high = self._find_recent_swing_high(df, idx, lookback=10)
                if recent_swing_high is not None:
                    new_trail_sl = recent_swing_high + 1.0  # 1 point above swing high
                    # Only move SL down, never up (for bearish)
                    if new_trail_sl < signal.stop_loss:
                        old_sl = signal.stop_loss
                        signal.stop_loss = new_trail_sl
                        if debug:
                            print(f"   📉 Trail SL: {old_sl:.2f} -> {new_trail_sl:.2f} (swing high: {recent_swing_high:.2f})")
        
        return signal.remaining_position == 0
    
    def generate_signals(
        self,
        df: pd.DataFrame,
        day_high: float = None,
        day_low: float = None,
        pdh: float = None,
        pdl: float = None,
        debug: bool = False
    ) -> List[TradeSignalICT]:
        """Generate trading signals"""
        signals = []
        self._reset_state()
        
        if len(df) < 20:
            return signals
        
        # Find swing highs and lows
        swing_highs, swing_lows = find_swing_highs_lows(df, lookback=self.swing_lookback)
        
        if not swing_highs or not swing_lows:
            return signals
        
        # Use day high/low or PDH/PDL for final target
        # Only use day_high/day_low if they're reasonable (from market open, not premarket)
        # For safety, if day_high/day_low look wrong, use PDH/PDL as fallback
        target_high = day_high if day_high else (pdh if pdh else df['high'].max())
        target_low = day_low if day_low else (pdl if pdl else df['low'].min())
        
        # Validate targets - for bullish trades, target_high should be above current price
        # For bearish trades, target_low should be below current price
        # Store session start for dynamic target calculation
        self.session_start_hour = getattr(self, 'session_start_hour', 16)
        self.session_start_minute = getattr(self, 'session_start_minute', 30)
        
        for i in range(self.swing_lookback, len(df)):
            candle = df.iloc[i]
            
            # Manage active trade
            if self.active_signal and not self.trade_taken:
                self.trade_taken = True
            
            if self.active_signal:
                trade_complete = self._manage_trade(df, i, debug)
                if trade_complete:
                    signals.append(self.active_signal)
                    self.active_signal = None
                    self.trade_taken = True
                continue
            
            # Skip if trade already taken today
            if self.trade_taken:
                continue
            
            # If we have a setup in progress (sweep occurred, waiting for pullback/OB/entry),
            # skip BOS detection to avoid resetting the setup
            setup_in_progress = (
                self.sweep_occurred and 
                self.current_bos is not None and
                not self.waiting_for_entry  # Once we have OB2, we wait for entry
            )
            
            # STEP 1: Detect BOS (only when no setup in progress)
            if not setup_in_progress:
                bos = self._detect_bos(df, i, swing_highs, swing_lows)
                
                if bos:
                    direction = bos['direction']
                    
                    # Track BOS for trend confirmation
                    if direction == 'bullish':
                        self.bos_count_bullish += 1
                        self.bos_count_bearish = 0
                        self.last_bullish_bos_swing_low = bos['swing_low']
                        
                        if self.bos_count_bullish >= self.bos_required:
                            if self.confirmed_trend != 'bullish':
                                self.confirmed_trend = 'bullish'
                                self.sweep_occurred = False
                                if debug:
                                    print(f"🔺 BULLISH TREND CONFIRMED at {candle.name} (BOS #{self.bos_count_bullish})")
                    else:
                        self.bos_count_bearish += 1
                        self.bos_count_bullish = 0
                        self.last_bearish_bos_swing_high = bos['swing_high']
                        
                        if self.bos_count_bearish >= self.bos_required:
                            if self.confirmed_trend != 'bearish':
                                self.confirmed_trend = 'bearish'
                                self.sweep_occurred = False
                                if debug:
                                    print(f"🔻 BEARISH TREND CONFIRMED at {candle.name} (BOS #{self.bos_count_bearish})")
                    
                    # Check for liquidity sweep (opposite BOS in confirmed trend)
                    if self.confirmed_trend and direction != self.confirmed_trend:
                        self.sweep_occurred = True
                        self.sweep_direction = direction
                        self.sweep_level = bos['level']
                        self.current_bos = bos
                        self.pullback_valid = False
                        self.ob1_zone = None
                        self.ob1_touched = False
                        self.choch_detected = False
                        self.ob2_zone = None
                        self.waiting_for_entry = False
                        if debug:
                            print(f"🔄 LIQUIDITY SWEEP ({direction}) at {candle.name}: {bos['level']:.2f}")
                    
                    self.bos_history.append(bos)
                    continue
            
            # Only proceed if we have confirmed trend AND sweep
            if not self.confirmed_trend or not self.sweep_occurred:
                continue
            
            # STEP 2: Check pullback after sweep
            if self.current_bos and not self.pullback_valid:
                pb_result = self._check_pullback(df, i, self.current_bos)
                if pb_result == 'valid':
                    self.pullback_valid = True
                    self.pullback_idx = i
                    if debug:
                        print(f"   📐 Valid pullback at {candle.name}")
                elif pb_result == 'invalidated':
                    self.current_bos = None
                    self.sweep_occurred = False
                    if debug:
                        print(f"   ❌ Pullback invalidated at {candle.name}")
                continue
            
            # STEP 3: Find OB1
            if self.pullback_valid and self.ob1_zone is None:
                # Direction for OB is opposite to sweep (same as trend)
                ob_direction = self.confirmed_trend
                self.ob1_zone = self._find_ob1(
                    df, self.current_bos['idx'], self.pullback_idx, ob_direction, debug
                )
                if self.ob1_zone and debug:
                    print(f"   📦 OB1 found: {self.ob1_zone}")
                continue
            
            # STEP 3b: Check if price touches OB1 zone (required before CHoCH)
            if self.ob1_zone and not self.ob1_touched and not self.choch_detected:
                ob1_high, ob1_low = self.ob1_zone
                
                if self.confirmed_trend == 'bullish':
                    # For bullish: Price must dip into OB1 zone (demand zone)
                    if candle['low'] <= ob1_high:
                        self.ob1_touched = True
                        self.ob1_touch_idx = i
                        if debug:
                            print(f"   ✅ OB1 TOUCHED at {candle.name}: Low {candle['low']:.2f} <= OB1 High {ob1_high:.2f}")
                else:
                    # For bearish: Price must rise into OB1 zone (supply zone)
                    if candle['high'] >= ob1_low:
                        self.ob1_touched = True
                        self.ob1_touch_idx = i
                        if debug:
                            print(f"   ✅ OB1 TOUCHED at {candle.name}: High {candle['high']:.2f} >= OB1 Low {ob1_low:.2f}")
                continue
            
            # STEP 4: Detect CHoCH (only after OB1 is touched)
            if self.ob1_zone and self.ob1_touched and not self.choch_detected:
                # CHoCH level depends on trade direction
                if self.confirmed_trend == 'bullish':
                    choch_level = self.last_bearish_bos_swing_high
                else:
                    choch_level = self.last_bullish_bos_swing_low
                
                if self._detect_choch(df, i, self.confirmed_trend, choch_level):
                    self.choch_detected = True
                    self.choch_idx = i
                    self.choch_level = choch_level
                    if debug:
                        print(f"   🔄 CHoCH confirmed at {candle.name}: {choch_level:.2f}")
                    
                    # Find OB2.0 using 3-min candles
                    ob2_result = self._find_ob2(df, i, self.confirmed_trend, debug)
                    if ob2_result:
                        self.ob2_zone = (ob2_result[0], ob2_result[1])
                        # Extend zone for wicks
                        extended = self._extend_ob2_zone(df, ob2_result, i, self.confirmed_trend, debug)
                        self.ob2_extended_zone = extended
                        self.waiting_for_entry = True
                        if debug:
                            print(f"   📦 OB2.0 Zone: {self.ob2_zone} | Extended: {self.ob2_extended_zone}")
                continue
            
            # STEP 5: Wait for OB2.0 retest and enter
            if self.waiting_for_entry and self.ob2_extended_zone and not self.trade_taken:
                ext_high, ext_low = self.ob2_extended_zone
                
                # Check if price taps the zone
                in_zone = candle['low'] <= ext_high and candle['high'] >= ext_low
                
                if in_zone:
                    if self.confirmed_trend == 'bullish':
                        entry_price = ext_high
                        stop_loss = ext_low - self.sl_buffer
                        risk = entry_price - stop_loss
                        tp1 = entry_price + risk  # 1:1
                        tp2 = entry_price + risk * 2  # 2:1
                        tp3 = entry_price + risk * 3  # 3:1
                        tp_final = target_high  # Day high (let it run)
                    else:
                        entry_price = ext_low
                        stop_loss = ext_high + self.sl_buffer
                        risk = stop_loss - entry_price
                        tp1 = entry_price - risk  # 1:1
                        tp2 = entry_price - risk * 2  # 2:1
                        tp3 = entry_price - risk * 3  # 3:1
                        tp_final = target_low  # Day low (let it run)
                    
                    risk_points = abs(entry_price - stop_loss)
                    dollar_per_point = self.risk_amount / risk_points if risk_points > 0 else 0
                    
                    # FILTER 1: Market Hours Filter - Only trade during SPX market hours (9:30 AM - 4:00 PM ET)
                    if self.use_session_filter:
                        entry_hour = candle.name.hour
                        entry_minute = candle.name.minute
                        
                        # Check if within market hours
                        entry_time_mins = entry_hour * 60 + entry_minute
                        market_open_mins = self.MARKET_OPEN_HOUR * 60 + self.MARKET_OPEN_MINUTE  # 9:30 = 570
                        market_close_mins = self.MARKET_CLOSE_HOUR * 60 + self.MARKET_CLOSE_MINUTE  # 16:00 = 960
                        
                        if entry_time_mins < market_open_mins or entry_time_mins >= market_close_mins:
                            if debug:
                                print(f"   ⏰ FILTERED: {entry_hour}:{entry_minute:02d} outside market hours (9:30-16:00)")
                            self.waiting_for_entry = False
                            continue
                    
                    # FILTER 2: SL Distance Filter - Avoid tiny or oversized SL
                    if self.use_sl_distance_filter:
                        if risk_points < self.MIN_SL_DISTANCE:
                            if debug:
                                print(f"   📏 FILTERED: SL distance {risk_points:.1f} < min {self.MIN_SL_DISTANCE}")
                            self.waiting_for_entry = False
                            continue
                        if risk_points > self.MAX_SL_DISTANCE:
                            if debug:
                                print(f"   📏 FILTERED: SL distance {risk_points:.1f} > max {self.MAX_SL_DISTANCE}")
                            self.waiting_for_entry = False
                            continue
                    
                    # Create signal
                    signal = TradeSignalICT(
                        direction=self.confirmed_trend,
                        entry_price=entry_price,
                        stop_loss=stop_loss,
                        tp1=tp1,
                        tp2=tp2,
                        tp3=tp3,
                        tp_final=tp_final,
                        entry_time=candle.name,
                        ob1_zone=self.ob1_zone,
                        ob2_zone=self.ob2_zone,
                        ob2_extended_zone=self.ob2_extended_zone,
                        bos_count=self.bos_count_bullish if self.confirmed_trend == 'bullish' else self.bos_count_bearish,
                        choch_level=self.choch_level,
                        sweep_level=self.sweep_level,
                        risk_points=risk_points,
                        risk_amount=self.risk_amount,
                        dollar_per_point=dollar_per_point,
                        original_sl=stop_loss  # Store original SL for R:R calculation
                    )
                    
                    self.active_signal = signal
                    self.waiting_for_entry = False
                    
                    if debug:
                        print(f"\n✅ TRADE SIGNAL: {self.confirmed_trend.upper()}")
                        print(f"   Entry: {entry_price:.2f} at {candle.name}")
                        print(f"   SL: {stop_loss:.2f} | Risk: {risk_points:.2f} pts")
                        print(f"   TP1: {tp1:.2f} | TP2: {tp2:.2f} | TP3: {tp3:.2f}")
        
        # Add active signal if still running at end of data
        if self.active_signal:
            signals.append(self.active_signal)
        
        return signals
