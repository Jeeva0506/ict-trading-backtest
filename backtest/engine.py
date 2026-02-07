from pathlib import Path
from typing import Any, Optional, List, Dict
import pandas as pd
from dataclasses import dataclass, field
from datetime import datetime, timedelta

@dataclass
class TradeState:
    """Tracks the state of an open trade with multiple targets"""
    entry_time: Any
    entry_price: float
    stop_loss: float
    original_sl: float
    tp1: float  # 1:1 RR
    tp2: float  # 1:2 RR
    final_target: float  # Day High/Low
    direction: str
    ob_zone: tuple       # OB 1 (original)
    ob2_zone: tuple      # OB 2.0 (entry zone)
    open_idx: int
    bos_idx: int = None
    choch_idx: int = None
    
    # Position tracking
    position_size: int = 1  # In lots
    remaining_position: float = 1.0  # 100% initially
    
    # Exit tracking
    tp1_hit: bool = False
    tp2_hit: bool = False
    final_hit: bool = False
    sl_hit: bool = False
    
    # PnL tracking
    realized_pnl: float = 0.0
    exit_time: Any = None
    exit_price: float = None
    exit_reason: str = ""
    closed: bool = False


class BacktestEngine:
    def __init__(self, config: dict, data: pd.DataFrame, strategy: Any, mode: str = 'nifty'):
        self.config = config
        self.data = data
        self.strategy = strategy
        self.mode = mode
        self.trades: List[Dict] = []
        self.equity_curve: List[float] = []
        self.daily_pnl: Dict[str, float] = {}
        
        # Capital management
        self.initial_capital = config['capital']['initial_capital']
        self.risk_per_trade = config['capital'].get('risk_per_trade', 0.01)
        self.max_drawdown_pct = config['capital'].get('max_drawdown_percent', 20)
        self.max_loss_per_trade_pct = config['capital'].get('max_loss_per_trade_pct', None)  # Stop if trade loses more than this %
        
        # Execution settings - handle both lot_size (NIFTY/FOREX) and contract_size (SPX500)
        self.lot_size = config['market'].get('lot_size', config['market'].get('contract_size', 1))
        self.sl_buffer = config['market']['sl_buffer_points']
        self.brokerage = config['execution']['brokerage_per_trade']
        self.slippage = config['execution']['slippage_points']
        self.max_trades_per_day = config['execution'].get('max_trades_per_day', 1)
        
        # SPX500 specific: tick value for PnL calculation
        self.tick_value = config['market'].get('tick_value', None)
        self.tick_size = config['market'].get('tick_size', 0.01)
        
        # Trading hours (mode-specific)
        self.chart_start_time = config.get('backtest', {}).get('chart_start_time', '00:00')
        self.force_close_time = config.get('backtest', {}).get('force_close_time', '15:15')
        
        # Sessions configuration (FOREX)
        self.sessions = config.get('sessions', {})
        if not self.sessions:
            # Default session for all markets based on mode
            mt4_markets = ['spx500', 'fra40', 'ger30', 'hk50', 'jp225', 'ndx100', 'uk100', 'us30']
            
            session_names = {
                'nifty': 'Indian',
                'spx500': 'New York',
                'ndx100': 'New York',
                'us30': 'New York',
                'fra40': 'Paris',
                'ger30': 'Frankfurt',
                'hk50': 'Hong Kong',
                'jp225': 'Tokyo',
                'uk100': 'London'
            }
            
            session_name = session_names.get(mode, mode.upper())
            self.sessions = {
                mode: {
                    'name': session_name,
                    'start_time': config.get('backtest', {}).get('trading_start_time', '09:15'),
                    'end_time': config.get('backtest', {}).get('trading_end_time', '15:15'),
                    'enabled': True
                }
            }
        
        # For backward compatibility
        self.trading_start = config.get('backtest', {}).get('trading_start_time', '09:15')
        self.trading_end = config.get('backtest', {}).get('trading_end_time', '15:15')
        
        # Currency symbol for display - USD for all CFD markets
        usd_markets = ['forex', 'spx500', 'fra40', 'ger30', 'hk50', 'jp225', 'ndx100', 'uk100', 'us30']
        self.currency_symbol = '$' if mode in usd_markets else '₹'
        
        # FOREX specific: pip value for lot size calculation
        self.pip_value = config['market'].get('pip_value', 10) if mode == 'forex' else None
        
        # Session-based trade tracking
        self.session_trades: Dict[str, List[Dict]] = {}

    def run(self) -> None:
        """Execute backtest over the full data period (1 year)"""
        self.trades = []
        self.equity_curve = []
        self.daily_pnl = {}
        
        # Initialize session trade tracking
        self.session_trades = {}
        for session_key in self.sessions:
            if self.sessions[session_key].get('enabled', True):
                self.session_trades[session_key] = []
        
        equity = self.initial_capital
        peak_equity = equity
        max_drawdown = 0
        
        # No artificial limit - use full data range from config
        data = self.data.copy()
        
        if data.empty:
            print("No data available for backtesting")
            return
        
        # Suppress verbose output unless debug mode
        if hasattr(self, 'verbose') and self.verbose:
            print(f"Backtesting from {data['datetime'].min()} to {data['datetime'].max()}")
            print(f"Total candles: {len(data)}")
        
        # Print active sessions for FOREX
        if self.mode == 'forex' and self.sessions and hasattr(self, 'verbose') and self.verbose:
            print("Active Trading Sessions:")
            for session_key, session in self.sessions.items():
                if session.get('enabled', True):
                    print(f"   - {session['name']}: {session['start_time']} - {session['end_time']} UTC")
        
        # Group data by date for daily processing
        data['date'] = data['datetime'].dt.date
        unique_dates = sorted(data['date'].unique())
        
        total_days = len(unique_dates)
        processed_days = 0
        
        # For FOREX 1H: Use rolling window of days for structure building
        structure_lookback_days = 5 if self.mode == 'forex' else 1
        
        import time
        start_time = time.time()
        for idx, date in enumerate(unique_dates):
            day_data = data[data['date'] == date].copy()
            if day_data.empty:
                continue
            if self.mode == 'forex' and idx >= structure_lookback_days:
                lookback_dates = unique_dates[max(0, idx - structure_lookback_days):idx + 1]
                structure_context_data = data[data['date'].isin(lookback_dates)].copy()
            else:
                structure_context_data = day_data
            day_high = day_data['high'].max()
            day_low = day_data['low'].min()
            trades_today = 0
            for session_key, session in self.sessions.items():
                if not session.get('enabled', True):
                    continue
                if trades_today >= self.max_trades_per_day:
                    break
                trade_result = self._process_trading_day(
                    structure_context_data if self.mode == 'forex' else day_data,
                    date,
                    day_high,
                    day_low,
                    equity,
                    session_key=session_key,
                    session_start=session['start_time'],
                    session_end=session['end_time'],
                    current_date=date
                )
                if trade_result:
                    trades_today += 1
                    net_pnl = trade_result['pnl'] - (2 * self.brokerage)
                    net_pnl -= (self.slippage * self.lot_size * 2)
                    trade_result['net_pnl'] = net_pnl
                    trade_result['gross_pnl'] = trade_result['pnl']
                    trade_result['session'] = session['name']
                    self.trades.append(trade_result)
                    if session_key in self.session_trades:
                        self.session_trades[session_key].append(trade_result)
                    if self.max_loss_per_trade_pct is not None and net_pnl < 0:
                        trade_loss_pct = abs(net_pnl) / equity * 100
                        if trade_loss_pct >= self.max_loss_per_trade_pct:
                            print(f"⛔ Trade loss of {trade_loss_pct:.2f}% exceeds {self.max_loss_per_trade_pct}% limit. Stopping backtest.")
                            equity += net_pnl
                            self.equity_curve.append(equity)
                            self._print_final_summary(equity, max_drawdown)
                            return
                    equity += net_pnl
                    self.equity_curve.append(equity)
                    date_str = str(date)
                    self.daily_pnl[date_str] = self.daily_pnl.get(date_str, 0) + net_pnl
                    peak_equity = max(peak_equity, equity)
                    current_drawdown = (peak_equity - equity) / peak_equity * 100
                    max_drawdown = max(max_drawdown, current_drawdown)
                    if current_drawdown >= self.max_drawdown_pct:
                        print(f"⛔ Max drawdown of {self.max_drawdown_pct}% exceeded. Stopping backtest.")
                        self._print_final_summary(equity, max_drawdown)
                        return
            processed_days += 1
            # Progress logging every 10 days
            if processed_days % 10 == 0 or processed_days == total_days:
                elapsed = time.time() - start_time
                percent = processed_days / total_days * 100
                est_total = elapsed / processed_days * total_days if processed_days > 0 else 0
                est_remaining = est_total - elapsed
                print(f"Progress: {processed_days}/{total_days} days ({percent:.1f}%) | Elapsed: {elapsed:.1f}s | Est. Remaining: {est_remaining:.1f}s | Equity: {self.currency_symbol}{equity:,.2f}")
        
        if hasattr(self, 'verbose') and self.verbose:
            print(f"\nBacktest complete. Final equity: {self.currency_symbol}{equity:,.2f}")
            print(f"Total trades: {len(self.trades)}")
            print(f"Max drawdown: {max_drawdown:.2f}%")
        
        self._print_session_breakdown()

    def _print_final_summary(self, equity: float, max_drawdown: float) -> None:
        """Print final summary when backtest ends early"""
        print(f"\nBacktest ended. Final equity: {self.currency_symbol}{equity:,.2f}")
        print(f"Total trades: {len(self.trades)}")
        print(f"Max drawdown: {max_drawdown:.2f}%")
        self._print_session_breakdown()
    
    def _print_session_breakdown(self) -> None:
        """Print session breakdown for FOREX trades"""
        # Print session breakdown for FOREX
        if self.mode == 'forex' and self.session_trades:
            print("\n📊 SESSION BREAKDOWN:")
            for session_key, trades in self.session_trades.items():
                session_name = self.sessions[session_key]['name']
                session_pnl = sum(t.get('net_pnl', 0) for t in trades)
                session_wins = sum(1 for t in trades if t.get('pnl', 0) > 0)
                session_total = len(trades)
                session_wr = (session_wins / session_total * 100) if session_total > 0 else 0
                print(f"   {session_name}: {session_total} trades, {session_wr:.1f}% WR, {self.currency_symbol}{session_pnl:,.2f} PnL")

    def _process_trading_day(
        self, 
        day_data: pd.DataFrame, 
        date: Any,
        day_high: float,
        day_low: float,
        current_equity: float,
        session_key: str = None,
        session_start: str = None,
        session_end: str = None,
        current_date: Any = None
    ) -> Optional[Dict]:
        """Process a single trading day/session following the 10-step strategy with Trend Confirmation"""
        
        open_trade: Optional[TradeState] = None
        trade_taken_today = False
        
        # Use session times if provided, otherwise use defaults
        trading_start = session_start or self.trading_start
        trading_end = session_end or self.trading_end
        
        # Use current_date if provided (for multi-day structure in FOREX)
        target_date = current_date or date
        
        # DON'T reset strategy state for FOREX (need to maintain structure across days)
        # Only reset for NIFTY (intraday)
        if self.mode != 'forex':
            if hasattr(self.strategy, 'reset_state'):
                self.strategy.reset_state(keep_trend=True)
        
        # For FOREX: Use all provided data for structure marking, filter to current date for trading
        # For NIFTY: Filter to trading hours only
        if self.mode == 'forex':
            # Use all data for structure context
            structure_data = day_data.copy()
            # Only take trades during the specified session ON THE CURRENT DATE
            trading_window_data = day_data[
                (day_data['datetime'].dt.date == target_date) &
                (day_data['datetime'].dt.strftime('%H:%M') >= trading_start) &
                (day_data['datetime'].dt.strftime('%H:%M') <= trading_end)
            ]
        else:
            # NIFTY: Filter to trading hours only
            structure_data = day_data[
                (day_data['datetime'].dt.strftime('%H:%M') >= trading_start) &
                (day_data['datetime'].dt.strftime('%H:%M') <= trading_end)
            ]
            trading_window_data = structure_data
        
        if len(structure_data) < 10:  # Need minimum candles for analysis
            return None
        
        # Calculate previous day high/low for TP3 context
        prev_day_high = None
        prev_day_low = None
        prev_day = None
        if hasattr(structure_data, 'iloc') and len(structure_data) > 0:
            first_dt = structure_data.iloc[0]['datetime']
            if hasattr(first_dt, 'date'):
                prev_day = (first_dt.date() - pd.Timedelta(days=1))
        if prev_day is not None:
            prev_day_data = self.data[self.data['datetime'].dt.date == prev_day]
            if not prev_day_data.empty:
                prev_day_high = prev_day_data['high'].max()
                prev_day_low = prev_day_data['low'].min()

        # Iterate using structure_data for analysis context
        for idx, (df_idx, row) in enumerate(structure_data.iterrows()):
            dt = row['datetime']
            time_str = dt.strftime('%H:%M')
            row_date = dt.date() if hasattr(dt, 'date') else dt
            # For FOREX: Only process candles from the target date
            if self.mode == 'forex' and row_date != target_date:
                continue
            # Check if we're in the trading window for entries (session-specific)
            in_trading_window = (time_str >= trading_start and time_str <= trading_end)
            # If no open trade, look for entry signal
            if open_trade is None and not trade_taken_today:
                # Get data up to current candle for signal generation (includes multi-day context)
                lookback_data = structure_data.iloc[:idx+1]
                if len(lookback_data) < 5:
                    continue
                    continue
                
                signals = self.strategy.generate_signals(
                    lookback_data,
                    day_high=day_high,
                    day_low=day_low,
                    debug=False
                )
                
                # Only enter trades during trading window (session-specific)
                if signals and in_trading_window:
                    signal = signals[0]
                    risk_amount = current_equity * self.risk_per_trade
                    sl_points = abs(signal.entry_price - signal.stop_loss)
                    if sl_points > 0:
                        lots = max(1, int(risk_amount / (sl_points * self.lot_size)))
                    else:
                        lots = 1
                    # Use targets from signal (strategy already calculates with PDH/PDL for FOREX)
                    # signal.target_price contains the proper target based on mode
                    if signal.direction == 'bullish':
                        # Use signal's TP1/TP2 if available, otherwise calculate
                        tp1 = signal.tp1 if hasattr(signal, 'tp1') and signal.tp1 else signal.entry_price + sl_points
                        tp2 = signal.tp2 if hasattr(signal, 'tp2') and signal.tp2 else signal.entry_price + (2 * sl_points)
                        # For FOREX: Use PDH from signal, for NIFTY: use day_high
                        if self.mode == 'forex' and 'pdh' in row.index and row['pdh']:
                            final_target = signal.target_price  # Strategy already set PDH as target
                        else:
                            final_target = signal.target_price if signal.target_price else day_high
                    else:
                        tp1 = signal.tp1 if hasattr(signal, 'tp1') and signal.tp1 else signal.entry_price - sl_points
                        tp2 = signal.tp2 if hasattr(signal, 'tp2') and signal.tp2 else signal.entry_price - (2 * sl_points)
                        # For FOREX: Use PDL from signal, for NIFTY: use day_low
                        if self.mode == 'forex' and 'pdl' in row.index and row['pdl']:
                            final_target = signal.target_price  # Strategy already set PDL as target
                        else:
                            final_target = signal.target_price if signal.target_price else day_low
                # Pass pdh/pdl for 30-min OR strategy, fallback to day_high/low if not available
                # Only pass pdh/pdl if using Strategy30MinOR
                from strategy.strategy_30min_or import Strategy30MinOR
                if isinstance(self.strategy, Strategy30MinOR):
                    signals = self.strategy.generate_signals(
                        lookback_data,
                        pdh=prev_day_high if prev_day_high is not None else day_high,
                        pdl=prev_day_low if prev_day_low is not None else day_low,
                        debug=False
                    )
                else:
                    signals = self.strategy.generate_signals(
                        lookback_data,
                        day_high=day_high,
                        day_low=day_low,
                        debug=False
                    )
                if signals and in_trading_window:
                    signal = signals[0]
                    risk_amount = current_equity * self.risk_per_trade
                    sl_points = abs(signal.entry_price - signal.stop_loss)
                    if sl_points > 0:
                        lots = max(1, int(risk_amount / (sl_points * self.lot_size)))
                    else:
                        lots = 1
                    # Use targets from signal (strategy already calculates with PDH/PDL for FOREX)
                    # signal.target_price contains the proper target based on mode
                    if signal.direction == 'bullish':
                        # Use signal's TP1/TP2 if available, otherwise calculate
                        tp1 = signal.tp1 if hasattr(signal, 'tp1') and signal.tp1 else signal.entry_price + sl_points
                        tp2 = signal.tp2 if hasattr(signal, 'tp2') and signal.tp2 else signal.entry_price + (2 * sl_points)
                        # For FOREX: Use PDH from signal, for NIFTY: use day_high
                        if self.mode == 'forex' and 'pdh' in row.index and row['pdh']:
                            final_target = signal.target_price  # Strategy already set PDH as target
                        else:
                            final_target = signal.target_price if signal.target_price else day_high
                    else:
                        tp1 = signal.tp1 if hasattr(signal, 'tp1') and signal.tp1 else signal.entry_price - sl_points
                        tp2 = signal.tp2 if hasattr(signal, 'tp2') and signal.tp2 else signal.entry_price - (2 * sl_points)
                        # For FOREX: Use PDL from signal, for NIFTY: use day_low
                        if self.mode == 'forex' and 'pdl' in row.index and row['pdl']:
                            final_target = signal.target_price  # Strategy already set PDL as target
                        else:
                            final_target = signal.target_price if signal.target_price else day_low
                    # Get OB 2.0 zone if available
                    ob2_zone = getattr(signal, 'ob2_zone', signal.ob_zone)
                    open_trade = TradeState(
                        entry_time=dt,
                        entry_price=signal.entry_price,
                        stop_loss=signal.stop_loss,
                        original_sl=signal.stop_loss,
                        tp1=tp1,
                        tp2=tp2,
                        final_target=final_target,
                        direction=signal.direction,
                        ob_zone=signal.ob_zone,
                        ob2_zone=ob2_zone,
                        open_idx=df_idx,
                        position_size=lots,
                        bos_idx=getattr(signal, 'bos_idx', None),
                        choch_idx=getattr(signal, 'choch_idx', None)
                    )
                    trade_taken_today = True
                    
            elif open_trade is not None:
                # Manage open trade
                open_trade = self._manage_trade(open_trade, row, time_str)
                
                if open_trade.closed:
                    # Convert to trade dict for logging
                    trade_dict = self._trade_state_to_dict(open_trade, date)
                    return trade_dict
        
        # Force close at end of day if still open
        if open_trade is not None and not open_trade.closed:
            last_row = structure_data.iloc[-1]
            if open_trade.direction == 'bullish':
                exit_pnl = (last_row['close'] - open_trade.entry_price) * self.lot_size * open_trade.position_size
            else:
                exit_pnl = (open_trade.entry_price - last_row['close']) * self.lot_size * open_trade.position_size
            
            open_trade.realized_pnl += exit_pnl * open_trade.remaining_position
            open_trade.exit_time = last_row['datetime']
            open_trade.exit_price = last_row['close']
            open_trade.exit_reason = "EOD_CLOSE"
            open_trade.closed = True
            
            return self._trade_state_to_dict(open_trade, date)
        
        return None

    def _manage_trade(self, trade: TradeState, candle: pd.Series, time_str: str) -> TradeState:
        """Manage open trade - check SL, TP1, TP2, Final Target, and Force Close at 15:15"""
        
        if trade.closed:
            return trade
        
        high = candle['high']
        low = candle['low']
        close = candle['close']
        
        pnl_per_point = self.lot_size * trade.position_size
        
        # FORCE CLOSE at 15:15 (15 min before market close)
        if time_str >= self.force_close_time:
            if trade.direction == 'bullish':
                exit_pnl = (close - trade.entry_price) * pnl_per_point
            else:
                exit_pnl = (trade.entry_price - close) * pnl_per_point
            
            trade.realized_pnl += exit_pnl * trade.remaining_position
            trade.exit_price = close
            trade.exit_time = candle['datetime']
            trade.exit_reason = "FORCE_CLOSE_1515"
            trade.closed = True
            return trade
        
        if trade.direction == 'bullish':
            # Check Stop Loss first
            if low <= trade.stop_loss:
                exit_pnl = (trade.stop_loss - trade.entry_price) * pnl_per_point
                trade.realized_pnl += exit_pnl * trade.remaining_position
                trade.exit_price = trade.stop_loss
                trade.exit_time = candle['datetime']
                trade.sl_hit = True
                trade.exit_reason = "SL_HIT" if not trade.tp1_hit else "SL_CTC"  # CTC = Cost to Cost
                trade.closed = True
                return trade
            
            # Check TP1 (1:1) - Just move SL to CTC, NO partial booking
            if not trade.tp1_hit and high >= trade.tp1:
                trade.stop_loss = trade.entry_price  # Move SL to Cost-to-Cost
                trade.tp1_hit = True
                # No profit booking, just trail protection
            
            # Check TP2 (1:2) - Book 50% here
            if trade.tp1_hit and not trade.tp2_hit and high >= trade.tp2:
                # Book 50% at TP2
                partial_pnl = (trade.tp2 - trade.entry_price) * pnl_per_point * 0.5
                trade.realized_pnl += partial_pnl
                trade.remaining_position = 0.5
                trade.tp2_hit = True
            
            # Check Final Target (Day High) - Book remaining 50%
            if trade.tp2_hit and high >= trade.final_target:
                partial_pnl = (trade.final_target - trade.entry_price) * pnl_per_point * trade.remaining_position
                trade.realized_pnl += partial_pnl
                trade.exit_price = trade.final_target
                trade.exit_time = candle['datetime']
                trade.final_hit = True
                trade.exit_reason = "FINAL_TARGET"
                trade.closed = True
                
        else:  # Bearish
            # Check Stop Loss first
            if high >= trade.stop_loss:
                exit_pnl = (trade.entry_price - trade.stop_loss) * pnl_per_point
                trade.realized_pnl += exit_pnl * trade.remaining_position
                trade.exit_price = trade.stop_loss
                trade.exit_time = candle['datetime']
                trade.sl_hit = True
                trade.exit_reason = "SL_HIT" if not trade.tp1_hit else "SL_CTC"
                trade.closed = True
                return trade
            
            # Check TP1 (1:1) - Just move SL to CTC, NO partial booking
            if not trade.tp1_hit and low <= trade.tp1:
                trade.stop_loss = trade.entry_price  # Move SL to Cost-to-Cost
                trade.tp1_hit = True
                # No profit booking, just trail protection
            
            # Check TP2 (1:2) - Book 50% here
            if trade.tp1_hit and not trade.tp2_hit and low <= trade.tp2:
                partial_pnl = (trade.entry_price - trade.tp2) * pnl_per_point * 0.5
                trade.realized_pnl += partial_pnl
                trade.remaining_position = 0.5
                trade.tp2_hit = True
            
            # Check Final Target (Day Low) - Book remaining 50%
            if trade.tp2_hit and low <= trade.final_target:
                partial_pnl = (trade.entry_price - trade.final_target) * pnl_per_point * trade.remaining_position
                trade.realized_pnl += partial_pnl
                trade.exit_price = trade.final_target
                trade.exit_time = candle['datetime']
                trade.final_hit = True
                trade.exit_reason = "FINAL_TARGET"
                trade.closed = True
        
        return trade

    def _trade_state_to_dict(self, trade: TradeState, date: Any) -> Dict:
        """Convert TradeState to dict for logging"""
        
        # Calculate R:R achieved
        if trade.direction == 'bullish':
            risk = trade.entry_price - trade.original_sl
            if trade.exit_price:
                reward = trade.exit_price - trade.entry_price
            else:
                reward = 0
        else:
            risk = trade.original_sl - trade.entry_price
            if trade.exit_price:
                reward = trade.entry_price - trade.exit_price
            else:
                reward = 0
        
        rr_achieved = reward / risk if risk > 0 else 0
        
        # Get trend confirmation info from strategy
        confirmed_trend = getattr(self.strategy, 'confirmed_trend', None)
        bos_count_bullish = getattr(self.strategy, 'bos_count_bullish', 0)
        bos_count_bearish = getattr(self.strategy, 'bos_count_bearish', 0)
        
        return {
            'date': str(date),
            'direction': trade.direction,
            'entry_time': trade.entry_time,
            'entry_price': trade.entry_price,
            'stop_loss': trade.original_sl,
            'tp1': trade.tp1,
            'tp2': trade.tp2,
            'final_target': trade.final_target,
            'exit_time': trade.exit_time,
            'exit_price': trade.exit_price,
            'exit_reason': trade.exit_reason,
            'ob_zone': trade.ob_zone,
            'ob2_zone': trade.ob2_zone,
            'bos_idx': trade.bos_idx,
            'choch_idx': trade.choch_idx,
            'position_size': trade.position_size,
            'tp1_hit': trade.tp1_hit,
            'tp2_hit': trade.tp2_hit,
            'final_hit': trade.final_hit,
            'sl_hit': trade.sl_hit,
            'pnl': trade.realized_pnl,
            'rr_achieved': rr_achieved,
            'win': trade.realized_pnl > 0,
            # Trend confirmation info
            'confirmed_trend': confirmed_trend,
            'bos_count_bullish': bos_count_bullish,
            'bos_count_bearish': bos_count_bearish
        }

    def get_results(self) -> dict:
        return {
            'trades': self.trades,
            'equity_curve': self.equity_curve,
            'daily_pnl': self.daily_pnl,
            'initial_capital': self.initial_capital,
            'final_capital': self.equity_curve[-1] if self.equity_curve else self.initial_capital,
            'session_trades': self.session_trades,
            'sessions': self.sessions
        }

