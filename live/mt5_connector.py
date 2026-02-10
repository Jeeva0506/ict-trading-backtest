"""
MT5 Live Trading Connector for FundedNext
==========================================
Runs 24/7 and trades profitable indices during their market hours
With FundedNext Stellar-2 rules built in
"""
import MetaTrader5 as mt5
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from pathlib import Path
import time
import random
import logging
import json
import requests
from dataclasses import dataclass, asdict
from typing import Optional, Dict, List
import sys
import signal
import atexit

# Add paths for imports
sys.path.insert(0, str(Path(__file__).parent))  # live folder
sys.path.insert(0, str(Path(__file__).parent.parent))  # backtest_engine folder

from config_fundednext import (
    FUNDEDNEXT_RULES, 
    ADAPTIVE_RISK,
    PROFITABLE_INDICES, 
    get_index_config,
    calculate_position_size,
    calculate_position_size_with_broker_data,
    get_broker_symbol_specs,
    MT5_SERVER,
    MT5_LOGIN,
    MT5_PASSWORD,
    ACCOUNT_SIZE,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID
)
from strategy.strategy_ict_clean import StrategyICTClean
from telegram_notifier import TelegramNotifier
from telegram_commands import TelegramCommandHandler
from trade_journal import TradeJournal, ReportScheduler
from sl_protection import (
    set_telegram_config as sl_set_telegram,
    start_monitoring as sl_start_monitoring,
    stop_monitoring as sl_stop_monitoring,
    verify_sl_after_entry,
    is_trading_paused as sl_is_trading_paused,
    close_all_positions as sl_close_all
)

# Setup logging with UTF-8 encoding for console (fixes Windows Task Scheduler)
import io
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# File handler with UTF-8
file_handler = logging.FileHandler('live/logs/trading.log', encoding='utf-8')
file_handler.setLevel(logging.INFO)
file_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))

# Console handler with UTF-8 for Windows
if sys.platform == 'win32':
    console_stream = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
else:
    console_stream = sys.stdout
console_handler = logging.StreamHandler(console_stream)
console_handler.setLevel(logging.INFO)
console_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))

logger.addHandler(file_handler)
logger.addHandler(console_handler)


@dataclass
class TradingState:
    """Tracks current trading state"""
    daily_start_balance: float = 0.0
    current_balance: float = 0.0
    daily_pnl: float = 0.0
    daily_trades: int = 0
    is_daily_limit_hit: bool = False
    last_reset_date: str = ""
    open_positions: Dict = None
    
    def __post_init__(self):
        if self.open_positions is None:
            self.open_positions = {}


class DrawdownProtection:
    """Monitors and enforces drawdown limits"""
    
    def __init__(self, initial_balance: float):
        self.initial_balance = initial_balance
        self.peak_balance = initial_balance
        self.daily_start_balance = initial_balance
        self.daily_peak_balance = initial_balance  # Highest balance TODAY
        
    def update(self, current_balance: float) -> dict:
        """Update balances and check limits"""
        # Update overall peak
        if current_balance > self.peak_balance:
            self.peak_balance = current_balance
        
        # Update daily peak (highest balance reached today)
        if current_balance > self.daily_peak_balance:
            self.daily_peak_balance = current_balance
        
        # Calculate drawdowns
        # Overall DD from overall peak (for max DD rule)
        overall_dd = ((self.initial_balance - current_balance) / self.initial_balance) * 100
        # Daily DD from TODAY's peak (for daily DD rule - FundedNext rule)
        daily_dd = ((self.daily_peak_balance - current_balance) / self.daily_peak_balance) * 100
        
        # Check limits
        max_dd_limit = FUNDEDNEXT_RULES['max_drawdown_percent']
        daily_dd_stop = FUNDEDNEXT_RULES['daily_drawdown_stop']
        
        return {
            'overall_dd': overall_dd,
            'daily_dd': daily_dd,
            'is_max_dd_breach': overall_dd >= max_dd_limit,
            'is_daily_dd_breach': daily_dd >= daily_dd_stop,
            'can_trade': overall_dd < max_dd_limit and daily_dd < daily_dd_stop
        }
    
    def get_adaptive_risk(self, current_balance: float) -> float:
        """
        Get current risk percent based on drawdown (Adaptive Risk Strategy)
        - Start with 2% risk for fast evaluation pass
        - Switch to 1% when drawdown exceeds threshold
        - Switch back to 2% when recovered
        """
        if not ADAPTIVE_RISK.get('enabled', False):
            return FUNDEDNEXT_RULES['risk_per_trade_percent']
        
        # Calculate current drawdown from peak
        if current_balance > self.peak_balance:
            self.peak_balance = current_balance
        
        dd_pct = ((self.peak_balance - current_balance) / self.initial_balance) * 100
        
        # Get thresholds
        dd_threshold = ADAPTIVE_RISK.get('drawdown_threshold', 3.0)
        recovery_threshold = ADAPTIVE_RISK.get('recovery_threshold', 1.0)
        default_risk = ADAPTIVE_RISK.get('default_risk_percent', 2.0)
        reduced_risk = ADAPTIVE_RISK.get('reduced_risk_percent', 1.0)
        
        if dd_pct >= dd_threshold:
            logger.info(f"Adaptive Risk: DD={dd_pct:.2f}% >= {dd_threshold}% - Using {reduced_risk}% risk")
            return reduced_risk
        elif dd_pct <= recovery_threshold:
            return default_risk
        else:
            # In between recovery and threshold, keep default
            return default_risk
    
    def reset_daily(self, current_balance: float):
        """Reset daily tracking (call at market open)"""
        self.daily_start_balance = current_balance
        self.daily_peak_balance = current_balance  # Reset daily peak


class NewsFilter:
    """Filters trades during high-impact news events"""
    
    FOREX_FACTORY_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
    
    def __init__(self, buffer_minutes: int = 30):
        self.buffer_minutes = buffer_minutes
        self.news_cache = []
        self.last_fetch = None
        
    def fetch_news(self) -> List[dict]:
        """Fetch news from Forex Factory API"""
        try:
            # Only fetch every 30 minutes
            if self.last_fetch and (datetime.now() - self.last_fetch).seconds < 1800:
                return self.news_cache
            
            response = requests.get(self.FOREX_FACTORY_URL, timeout=10)
            if response.status_code == 200:
                self.news_cache = response.json()
                self.last_fetch = datetime.now()
                logger.info(f"Fetched {len(self.news_cache)} news events")
            return self.news_cache
        except Exception as e:
            logger.error(f"Failed to fetch news: {e}")
            return self.news_cache
    
    def is_high_impact_news(self, symbol: str) -> tuple:
        """
        Check if high-impact news is coming up for this symbol
        
        Returns:
            (is_news_time, next_news_event)
        """
        news = self.fetch_news()
        now = datetime.utcnow()
        
        # Get currencies affected by this symbol
        config = get_index_config(symbol)
        currency = config.get('currency', 'USD')
        
        # Also check USD for all symbols (reserve currency)
        currencies_to_check = {currency, 'USD'}
        
        for event in news:
            try:
                # Parse event time
                event_time = datetime.strptime(event.get('date', ''), '%Y-%m-%dT%H:%M:%S%z')
                event_time = event_time.replace(tzinfo=None)  # Remove timezone for comparison
                
                # Check if high impact
                impact = event.get('impact', '').lower()
                event_currency = event.get('country', '')
                
                if impact == 'high' and event_currency in currencies_to_check:
                    # Check if within buffer window
                    time_diff = abs((event_time - now).total_seconds() / 60)
                    
                    if time_diff <= self.buffer_minutes:
                        return True, {
                            'title': event.get('title', 'Unknown'),
                            'time': str(event_time),
                            'currency': event_currency,
                            'minutes_away': time_diff
                        }
            except:
                continue
        
        return False, None


class MT5Connector:
    """Handles all MT5 operations"""
    
    def __init__(self):
        self.connected = False
        
    def connect(self) -> bool:
        """Initialize MT5 connection using credentials from .env"""
        if not mt5.initialize():
            logger.error(f"MT5 initialization failed: {mt5.last_error()}")
            return False
        
        # Check if already logged in to the correct account
        account_info = mt5.account_info()
        if account_info and account_info.login == MT5_LOGIN:
            logger.info(f"Already logged in to account {MT5_LOGIN}")
            self.connected = True
            logger.info(f"Connected to MT5 - Account: {account_info.login}, Balance: ${account_info.balance:,.2f}")
            return True
        
        # Login with credentials from .env if not already logged in
        if MT5_LOGIN and MT5_PASSWORD:
            authorized = mt5.login(
                login=MT5_LOGIN,
                password=MT5_PASSWORD,
                server=MT5_SERVER
            )
            if not authorized:
                logger.error(f"MT5 login failed: {mt5.last_error()}")
                mt5.shutdown()
                return False
            logger.info(f"Logged in to {MT5_SERVER} with account {MT5_LOGIN}")
        
        self.connected = True
        account_info = mt5.account_info()
        logger.info(f"Connected to MT5 - Account: {account_info.login}, Balance: ${account_info.balance:,.2f}")
        return True
    
    def disconnect(self):
        """Shutdown MT5 connection"""
        mt5.shutdown()
        self.connected = False
        logger.info("Disconnected from MT5")
    
    def get_account_info(self) -> dict:
        """Get current account information"""
        if not self.connected:
            return {}
        
        info = mt5.account_info()
        return {
            'balance': info.balance,
            'equity': info.equity,
            'margin': info.margin,
            'free_margin': info.margin_free,
            'profit': info.profit,
            'leverage': info.leverage
        }
    
    def get_current_price(self, symbol: str) -> tuple:
        """Get current bid/ask prices"""
        tick = mt5.symbol_info_tick(symbol)
        if tick:
            return tick.bid, tick.ask
        return None, None
    
    def get_ohlc_data(self, symbol: str, timeframe=mt5.TIMEFRAME_M1, 
                      count: int = 500) -> pd.DataFrame:
        """Get historical OHLC data"""
        rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, count)
        if rates is None or len(rates) == 0:
            logger.error(f"Failed to get rates for {symbol}")
            return pd.DataFrame()
        
        df = pd.DataFrame(rates)
        df['time'] = pd.to_datetime(df['time'], unit='s')
        df.set_index('time', inplace=True)
        df.columns = ['open', 'high', 'low', 'close', 'tick_volume', 'spread', 'real_volume']
        return df
    
    def place_order(self, symbol: str, order_type: str, volume: float,
                    sl: float, tp: float, comment: str = "") -> dict:
        """
        Place a market order
        
        Args:
            symbol: Trading symbol
            order_type: 'buy' or 'sell'
            volume: Position size in lots
            sl: Stop loss price
            tp: Take profit price (TP1)
            comment: Order comment
        """
        symbol_info = mt5.symbol_info(symbol)
        if symbol_info is None:
            logger.error(f"Symbol {symbol} not found")
            return {'success': False, 'error': 'Symbol not found'}
        
        if not symbol_info.visible:
            mt5.symbol_select(symbol, True)
        
        # Get current price
        bid, ask = self.get_current_price(symbol)
        
        if order_type.lower() == 'buy':
            price = ask
            trade_type = mt5.ORDER_TYPE_BUY
        else:
            price = bid
            trade_type = mt5.ORDER_TYPE_SELL
        
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": volume,
            "type": trade_type,
            "price": price,
            "sl": sl,
            "tp": tp,
            "deviation": 10,  # Reduced from 20 for tighter fills
            "magic": 20260208,  # Unique ID for our bot
            "comment": comment,
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        
        result = mt5.order_send(request)
        
        if result.retcode != mt5.TRADE_RETCODE_DONE:
            logger.error(f"Order failed: {result.comment}")
            return {'success': False, 'error': result.comment, 'retcode': result.retcode}
        
        logger.info(f"Order placed: {order_type.upper()} {volume} {symbol} @ {price}, SL: {sl}, TP: {tp}")
        return {
            'success': True,
            'ticket': result.order,
            'volume': volume,
            'price': price,
            'sl': sl,
            'tp': tp
        }
    
    def modify_position(self, ticket: int, sl: float = None, tp: float = None, max_retries: int = 3) -> bool:
        """
        Modify SL/TP of an open position with retry logic.
        
        Args:
            ticket: Position ticket
            sl: New stop loss (None to keep current)
            tp: New take profit (None to keep current)
            max_retries: Number of retry attempts
        """
        for attempt in range(max_retries):
            position = mt5.positions_get(ticket=ticket)
            if not position:
                logger.error(f"Position {ticket} not found")
                return False
            
            pos = position[0]
            
            request = {
                "action": mt5.TRADE_ACTION_SLTP,
                "symbol": pos.symbol,
                "position": ticket,
                "sl": sl if sl else pos.sl,
                "tp": tp if tp else pos.tp,
            }
            
            result = mt5.order_send(request)
            
            if result.retcode == mt5.TRADE_RETCODE_DONE:
                logger.info(f"Position {ticket} modified - SL: {sl}, TP: {tp}")
                return True
            
            logger.warning(f"Modify attempt {attempt + 1}/{max_retries} failed: {result.comment}")
            if attempt < max_retries - 1:
                time.sleep(0.5)  # Wait before retry
        
        logger.error(f"Failed to modify position {ticket} after {max_retries} attempts")
        return False
    
    def close_position(self, ticket: int, volume: float = None) -> bool:
        """Close a position (fully or partially)"""
        position = mt5.positions_get(ticket=ticket)
        if not position:
            logger.error(f"Position {ticket} not found")
            return False
        
        pos = position[0]
        close_volume = volume if volume else pos.volume
        
        # Determine close type
        if pos.type == mt5.POSITION_TYPE_BUY:
            trade_type = mt5.ORDER_TYPE_SELL
            price = mt5.symbol_info_tick(pos.symbol).bid
        else:
            trade_type = mt5.ORDER_TYPE_BUY
            price = mt5.symbol_info_tick(pos.symbol).ask
        
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": pos.symbol,
            "volume": close_volume,
            "type": trade_type,
            "position": ticket,
            "price": price,
            "deviation": 10,  # Reduced from 20 for tighter fills
            "magic": 20260208,
            "comment": "Partial/Full close",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        
        result = mt5.order_send(request)
        
        if result.retcode != mt5.TRADE_RETCODE_DONE:
            logger.error(f"Close failed: {result.comment}")
            return False
        
        logger.info(f"Closed {close_volume} lots of position {ticket}")
        return True
    
    def get_open_positions(self, symbol: str = None) -> List[dict]:
        """Get all open positions"""
        if symbol:
            positions = mt5.positions_get(symbol=symbol)
        else:
            positions = mt5.positions_get()
        
        if positions is None:
            return []
        
        return [
            {
                'ticket': p.ticket,
                'symbol': p.symbol,
                'type': 'buy' if p.type == mt5.POSITION_TYPE_BUY else 'sell',
                'volume': p.volume,
                'price': p.price_open,
                'sl': p.sl,
                'tp': p.tp,
                'profit': p.profit,
                'time': datetime.fromtimestamp(p.time)
            }
            for p in positions
        ]


def is_market_open(symbol: str) -> bool:
    """Check if market is open for this symbol (in MT5 server time)"""
    config = get_index_config(symbol)
    if not config:
        return False
    
    # Get MT5 server time from tick data (most accurate)
    try:
        tick = mt5.symbol_info_tick(symbol)
        if tick and tick.time:
            now = datetime.fromtimestamp(tick.time)
        else:
            # Fallback: Convert local time to MT5 server time
            # MT5 server is GMT+2 (winter) or GMT+3 (summer DST)
            import pytz
            local_tz = pytz.timezone('Asia/Kolkata')  # IST
            mt5_tz = pytz.timezone('Europe/Helsinki')  # GMT+2/+3 same as MT5
            local_now = datetime.now(local_tz)
            now = local_now.astimezone(mt5_tz).replace(tzinfo=None)
    except Exception:
        # If MT5 not connected, use timezone conversion
        import pytz
        local_tz = pytz.timezone('Asia/Kolkata')
        mt5_tz = pytz.timezone('Europe/Helsinki')
        local_now = datetime.now(local_tz)
        now = local_now.astimezone(mt5_tz).replace(tzinfo=None)
    
    # Check if it's a weekday (Monday=0, Sunday=6)
    if now.weekday() >= 5:  # Saturday or Sunday
        return False
    
    current_time = now.hour * 60 + now.minute
    
    # Market hours in MT5 time
    open_time = config['mt5_open_hour'] * 60 + config['mt5_open_minute']
    close_time = config['mt5_close_hour'] * 60 + config['mt5_close_minute']
    
    # Handle overnight sessions
    if close_time < open_time:
        # Market spans midnight
        return current_time >= open_time or current_time < close_time
    else:
        return open_time <= current_time < close_time


def get_active_indices() -> List[str]:
    """Get list of indices that are currently in their trading hours"""
    active = []
    for symbol in PROFITABLE_INDICES.keys():
        if is_market_open(symbol):
            active.append(symbol)
    return active


class LiveTrader:
    """Main live trading orchestrator"""
    
    def __init__(self):
        self.mt5 = MT5Connector()
        self.news_filter = NewsFilter(buffer_minutes=FUNDEDNEXT_RULES['news_buffer_minutes'])
        self.dd_protection = None
        self.state = TradingState()
        self.strategies = {}  # Per-symbol strategy instances
        self.telegram = TelegramNotifier(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
        self.journal = TradeJournal()
        self.report_scheduler = ReportScheduler(self.journal, self.telegram)
        self.trade_id_map = {}  # Maps MT5 ticket to journal trade_id
        self.heartbeat_file = Path(__file__).parent / 'logs' / 'heartbeat.txt'
        self._shutdown_requested = False
        
        # Trading control state (for Telegram commands)
        self._trading_enabled = True  # Can be paused/resumed via Telegram
        self._trading_paused = False  # Paused but still running
        
        # Telegram command handler for remote control
        self.cmd_handler = TelegramCommandHandler(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
        self._setup_command_callbacks()
        
        # Setup graceful shutdown handlers
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)
        atexit.register(self._cleanup)
    
    def _signal_handler(self, signum, frame):
        """Handle shutdown signals gracefully"""
        logger.info(f"Received signal {signum}, initiating graceful shutdown...")
        self._shutdown_requested = True
        
        # Send Telegram notification immediately on signal (don't wait for atexit)
        try:
            # Stop SL Protection monitoring first
            sl_stop_monitoring()
            
            self._save_position_state()
            
            # Stop command handler
            if hasattr(self, 'cmd_handler'):
                self.cmd_handler.stop_polling()
            
            account = self.mt5.get_account_info()
            if account:
                self.telegram.bot_stopped("Manual shutdown (Ctrl+C)", account.get('balance', 0), self.state.daily_pnl)
                time.sleep(1)  # Wait for message to be sent
            
            self.mt5.disconnect()
            logger.info("Shutdown complete. Exiting...")
        except Exception as e:
            logger.error(f"Error during shutdown: {e}")
        
        # Exit cleanly
        import sys
        sys.exit(0)
    
    def _cleanup(self):
        """Cleanup on exit (fallback if signal handler didn't run)"""
        if self._shutdown_requested:
            return  # Already handled by signal handler
            
        logger.info("Cleaning up...")
        try:
            # Stop SL Protection monitoring
            sl_stop_monitoring()
            
            # Save state before exit
            self._save_position_state()
            
            # Stop command handler
            if hasattr(self, 'cmd_handler'):
                self.cmd_handler.stop_polling()
            
            account = self.mt5.get_account_info()
            if account:
                self.telegram.bot_stopped("Graceful shutdown", account.get('balance', 0), self.state.daily_pnl)
        except:
            pass
        self.mt5.disconnect()
    
    def _save_position_state(self):
        """
        Save open positions and their TP/trailing state to JSON for recovery.
        Called after any position state change (TP hit, SL moved, partial close).
        """
        state_file = Path(__file__).parent / 'logs' / 'position_state.json'
        try:
            state_data = {
                'timestamp': datetime.now().isoformat(),
                'daily_pnl': self.state.daily_pnl,
                'daily_trades': self.state.daily_trades,
                'positions': {}
            }
            
            # Save each open position's state
            for ticket, signal in self.state.open_positions.items():
                state_data['positions'][str(ticket)] = {
                    'symbol': signal.get('symbol'),
                    'direction': signal.get('direction'),
                    'entry_price': signal.get('entry_price'),
                    'stop_loss': signal.get('stop_loss'),
                    'tp1': signal.get('tp1'),
                    'tp2': signal.get('tp2'),
                    'tp3': signal.get('tp3'),
                    'tp_final': signal.get('tp_final'),
                    'risk_amount': signal.get('risk_amount'),
                    'tp1_executed': signal.get('tp1_executed', False),
                    'tp2_executed': signal.get('tp2_executed', False),
                    'tp3_executed': signal.get('tp3_executed', False),
                    'trailing_active': signal.get('trailing_active', False),
                    'trailing_sl': signal.get('trailing_sl'),
                }
            
            # Also save strategy states for each symbol
            state_data['strategies'] = {}
            for symbol, strategy in self.strategies.items():
                state_data['strategies'][symbol] = {
                    'confirmed_trend': strategy.confirmed_trend,
                    'bos_count_bullish': strategy.bos_count_bullish,
                    'bos_count_bearish': strategy.bos_count_bearish,
                    'sweep_occurred': strategy.sweep_occurred,
                    'sweep_direction': strategy.sweep_direction,
                    'sweep_level': strategy.sweep_level,
                    'ob1_zone': strategy.ob1_zone,
                    'ob1_touched': strategy.ob1_touched,
                    'choch_detected': strategy.choch_detected,
                    'choch_level': strategy.choch_level,
                    'ob2_zone': strategy.ob2_zone,
                    'ob2_extended_zone': strategy.ob2_extended_zone,
                    'waiting_for_entry': strategy.waiting_for_entry,
                    'trade_taken': strategy.trade_taken,
                }
            
            state_file.parent.mkdir(parents=True, exist_ok=True)
            with open(state_file, 'w') as f:
                json.dump(state_data, f, indent=2, default=str)
            
            logger.debug(f"Position state saved: {len(state_data['positions'])} positions")
        except Exception as e:
            logger.error(f"Failed to save position state: {e}")
    
    def _load_position_state(self):
        """
        Load position state from JSON after restart.
        Syncs with actual MT5 positions to recover TP/trailing state.
        """
        state_file = Path(__file__).parent / 'logs' / 'position_state.json'
        
        if not state_file.exists():
            logger.info("No position state file found - fresh start")
            return
        
        try:
            with open(state_file, 'r') as f:
                state_data = json.load(f)
            
            # Check if state is from today
            saved_date = state_data.get('timestamp', '')[:10]
            today = datetime.now().strftime('%Y-%m-%d')
            
            if saved_date != today:
                logger.info(f"Position state from {saved_date}, today is {today} - skipping old state")
                return
            
            # Restore daily counters
            self.state.daily_pnl = state_data.get('daily_pnl', 0)
            self.state.daily_trades = state_data.get('daily_trades', 0)
            
            # Get actual MT5 positions
            mt5_positions = self.mt5.get_open_positions()
            mt5_tickets = {pos['ticket'] for pos in mt5_positions}
            
            # Restore position states that still exist in MT5
            restored_count = 0
            for ticket_str, pos_state in state_data.get('positions', {}).items():
                ticket = int(ticket_str)
                if ticket in mt5_tickets:
                    self.state.open_positions[ticket] = pos_state
                    restored_count += 1
                    logger.info(f"Restored position {ticket} state: TP1={pos_state.get('tp1_executed')}, TP2={pos_state.get('tp2_executed')}, TP3={pos_state.get('tp3_executed')}")
            
            # Restore strategy states
            for symbol, strat_state in state_data.get('strategies', {}).items():
                if symbol in self.strategies:
                    strategy = self.strategies[symbol]
                    strategy.confirmed_trend = strat_state.get('confirmed_trend')
                    strategy.bos_count_bullish = strat_state.get('bos_count_bullish', 0)
                    strategy.bos_count_bearish = strat_state.get('bos_count_bearish', 0)
                    strategy.sweep_occurred = strat_state.get('sweep_occurred', False)
                    strategy.sweep_direction = strat_state.get('sweep_direction')
                    strategy.sweep_level = strat_state.get('sweep_level')
                    strategy.ob1_zone = tuple(strat_state['ob1_zone']) if strat_state.get('ob1_zone') else None
                    strategy.ob1_touched = strat_state.get('ob1_touched', False)
                    strategy.choch_detected = strat_state.get('choch_detected', False)
                    strategy.choch_level = strat_state.get('choch_level')
                    strategy.ob2_zone = tuple(strat_state['ob2_zone']) if strat_state.get('ob2_zone') else None
                    strategy.ob2_extended_zone = tuple(strat_state['ob2_extended_zone']) if strat_state.get('ob2_extended_zone') else None
                    strategy.waiting_for_entry = strat_state.get('waiting_for_entry', False)
                    strategy.trade_taken = strat_state.get('trade_taken', False)
                    logger.info(f"Restored {symbol} strategy state: trend={strategy.confirmed_trend}, OB1={strategy.ob1_zone}")
            
            if restored_count > 0:
                logger.info(f"Restored {restored_count} position states from file")
                self.telegram.send_message(
                    f"🔄 <b>BOT RECOVERED</b>\n\n"
                    f"Restored {restored_count} position(s)\n"
                    f"Daily P&L: ${self.state.daily_pnl:,.2f}\n"
                    f"Daily trades: {self.state.daily_trades}"
                )
                
        except Exception as e:
            logger.error(f"Failed to load position state: {e}")
            # Don't crash - continue with fresh state
    
    def _setup_command_callbacks(self):
        """Setup callbacks for Telegram command handler"""
        # Start command - enable trading (resume from stop)
        def on_start():
            self._trading_enabled = True
            self._trading_paused = False
            logger.info("Trading STARTED via Telegram command")
        
        # Stop command - now just pauses trading (bot keeps running)
        # Use on_pause for /stop so bot stays alive
        def on_stop():
            self._trading_enabled = False
            self._shutdown_requested = True
            logger.info("Bot SHUTDOWN requested via Telegram command")
        
        # Pause command - pause new trades but keep bot running
        def on_pause():
            self._trading_paused = True
            logger.info("Trading PAUSED via Telegram command")
        
        # Resume command - resume trading
        def on_resume():
            self._trading_paused = False
            logger.info("Trading RESUMED via Telegram command")
        
        # Status callback - return current bot status
        def on_status():
            account = self.mt5.get_account_info()
            dd_status = self.dd_protection.update(account['balance']) if self.dd_protection else {}
            active = get_active_indices()
            positions = self.mt5.get_open_positions()
            
            return {
                'balance': account.get('balance', 0),
                'daily_pnl': self.state.daily_pnl,
                'open_positions': len(positions),
                'daily_dd': dd_status.get('daily_dd', 0),
                'overall_dd': dd_status.get('overall_dd', 0),
                'active_markets': active
            }
        
        # Positions callback - return open positions
        def on_positions():
            positions = self.mt5.get_open_positions()
            return [{
                'symbol': p.symbol,
                'type': 'BUY' if p.type == 0 else 'SELL',
                'volume': p.volume,
                'profit': p.profit,
                'price_open': p.price_open,
                'price_current': p.price_current
            } for p in positions]
        
        # Balance callback - return account info
        def on_balance():
            return self.mt5.get_account_info()
        
        # Register callbacks
        self.cmd_handler.on_start = on_start
        self.cmd_handler.on_stop = on_stop
        self.cmd_handler.on_pause = on_pause
        self.cmd_handler.on_resume = on_resume
        self.cmd_handler.on_status = on_status
        self.cmd_handler.on_positions = on_positions
        self.cmd_handler.on_balance = on_balance
    
    def _write_heartbeat(self):
        """Write heartbeat file for watchdog monitoring"""
        try:
            self.heartbeat_file.parent.mkdir(parents=True, exist_ok=True)
            self.heartbeat_file.write_text(datetime.now().isoformat())
        except Exception as e:
            logger.warning(f"Failed to write heartbeat: {e}")
        
    def initialize(self) -> bool:
        """Initialize the trading system"""
        # Connect to MT5
        if not self.mt5.connect():
            return False
        
        # Get initial account info - use .env ACCOUNT_SIZE or actual balance
        account = self.mt5.get_account_info()
        initial_balance = ACCOUNT_SIZE if ACCOUNT_SIZE > 0 else account['balance']
        
        # Initialize drawdown protection
        self.dd_protection = DrawdownProtection(initial_balance)
        
        # Initialize state
        self.state.daily_start_balance = initial_balance
        self.state.current_balance = initial_balance
        self.state.last_reset_date = datetime.now().strftime('%Y-%m-%d')
        
        # Create logs directory
        Path('live/logs').mkdir(parents=True, exist_ok=True)
        
        logger.info(f"LiveTrader initialized - Account Size: ${initial_balance:,.2f}")
        logger.info(f"Risk per trade: {FUNDEDNEXT_RULES['risk_per_trade_percent']}% = ${initial_balance * FUNDEDNEXT_RULES['risk_per_trade_percent'] / 100:,.2f}")
        
        # Initialize strategies for all symbols (needed for state restoration)
        for symbol in PROFITABLE_INDICES:
            config = get_index_config(symbol)
            if config:
                self.strategies[symbol] = StrategyICTClean(
                    sl_buffer=config['sl_buffer'],
                    bos_required=2,
                    capital=initial_balance,
                    risk_percent=FUNDEDNEXT_RULES['risk_per_trade_percent'],
                    use_session_filter=False,
                    use_sl_distance_filter=True,
                    min_sl_distance=config['min_sl_distance'],
                    max_sl_distance=config['max_sl_distance']
                )
        
        # Load any saved position state (recovery after restart)
        self._load_position_state()
        
        # Start Telegram command handler for remote control
        self.cmd_handler.bot_state['is_running'] = True
        self.cmd_handler.start_polling()
        logger.info("Telegram command handler started - use /help for commands")
        
        # Send Telegram notification
        self.telegram.bot_started(list(PROFITABLE_INDICES.keys()), initial_balance)
        
        # Start SL Protection monitoring
        sl_set_telegram(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
        sl_start_monitoring()
        logger.info("SL Protection monitoring started")
        
        return True
    
    def check_daily_reset(self):
        """Check if we need to reset daily tracking"""
        today = datetime.now().strftime('%Y-%m-%d')
        if today != self.state.last_reset_date:
            account = self.mt5.get_account_info()
            
            # Send previous day's report before reset
            if self.state.last_reset_date:
                yesterday = (datetime.now() - timedelta(days=1)).date()
                self.journal.send_daily_report(yesterday, account['balance'])
                self.journal.generate_dashboard(account['balance'])
            
            self.dd_protection.reset_daily(account['balance'])
            self.state.daily_start_balance = account['balance']
            self.state.daily_pnl = 0
            self.state.daily_trades = 0
            self.state.is_daily_limit_hit = False
            self.state.last_reset_date = today
            logger.info(f"Daily reset - New day starting balance: ${account['balance']:,.2f}")
    
    def check_scheduled_reports(self):
        """Check and send scheduled reports (daily at 3 AM, weekly on Saturday 8 AM)"""
        account = self.mt5.get_account_info()
        if not account:
            return
        dd_status = self.dd_protection.update(account['balance'])
        self.report_scheduler.check_and_send_reports(
            balance=account['balance'],
            daily_dd=dd_status['daily_dd'],
            overall_dd=dd_status['overall_dd']
        )
    
    def check_connection(self) -> bool:
        """Check MT5 connection and reconnect if needed"""
        import socket
        
        # Check internet connectivity
        try:
            socket.create_connection(("8.8.8.8", 53), timeout=3)
        except OSError:
            if not hasattr(self, '_internet_down_notified') or not self._internet_down_notified:
                self.telegram.internet_down()
                self._internet_down_notified = True
                self.connection_lost_time = datetime.now()
            logger.warning("No internet connection")
            time.sleep(30)
            return False
        
        # Internet is back
        if hasattr(self, '_internet_down_notified') and self._internet_down_notified:
            self._internet_down_notified = False
            downtime = (datetime.now() - self.connection_lost_time).seconds if hasattr(self, 'connection_lost_time') else 0
            self.telegram.connection_restored("Internet", downtime)
        
        # Check MT5 connection
        if not mt5.terminal_info():
            if not hasattr(self, '_mt5_down_notified') or not self._mt5_down_notified:
                self.telegram.mt5_disconnected("Terminal not responding")
                self._mt5_down_notified = True
                self.connection_lost_time = datetime.now()
            
            # Try to reconnect
            logger.warning("MT5 connection lost, attempting reconnect...")
            for attempt in range(3):
                self.telegram.reconnecting("MT5", attempt + 1, 3)
                if self.mt5.connect():
                    account = self.mt5.get_account_info()
                    self.telegram.mt5_reconnected(account.get('balance', 0))
                    self._mt5_down_notified = False
                    
                    # Verify positions after reconnect
                    self._verify_positions_after_reconnect()
                    return True
                time.sleep(10)
            
            logger.error("Failed to reconnect to MT5 after 3 attempts")
            time.sleep(60)
            return False
        
        # MT5 is back
        if hasattr(self, '_mt5_down_notified') and self._mt5_down_notified:
            self._mt5_down_notified = False
            account = self.mt5.get_account_info()
            downtime = (datetime.now() - self.connection_lost_time).seconds if hasattr(self, 'connection_lost_time') else 0
            self.telegram.connection_restored("MT5", downtime)
            self.telegram.mt5_reconnected(account.get('balance', 0))
        
        return True
    
    def _verify_positions_after_reconnect(self):
        """
        Verify positions after MT5 reconnection.
        Reconcile with saved state and check for closed/modified positions.
        """
        logger.info("Verifying positions after reconnect...")
        
        # Get current MT5 positions
        mt5_positions = self.mt5.get_open_positions()
        mt5_tickets = {pos['ticket'] for pos in mt5_positions}
        
        # Check for positions in our tracking that were closed while disconnected
        orphaned_tickets = []
        for ticket in list(self.state.open_positions.keys()):
            if ticket not in mt5_tickets:
                orphaned_tickets.append(ticket)
        
        if orphaned_tickets:
            logger.warning(f"Positions closed while disconnected: {orphaned_tickets}")
            for ticket in orphaned_tickets:
                signal = self.state.open_positions.pop(ticket, None)
                if signal:
                    # Get trade history to find exit details
                    history = mt5.history_deals_get(position=ticket)
                    if history and len(history) > 0:
                        total_pnl = sum(deal.profit for deal in history if deal.entry == 1)
                        self.state.daily_pnl += total_pnl
                        logger.info(f"Position {ticket} was closed while disconnected - P&L: ${total_pnl:.2f}")
                        self.telegram.send_message(
                            f"⚠️ <b>Position Closed While Disconnected</b>\n\n"
                            f"Ticket: {ticket}\n"
                            f"Symbol: {signal.get('symbol', 'Unknown')}\n"
                            f"P&L: ${total_pnl:+.2f}"
                        )
        
        # Check for positions in MT5 that we're not tracking
        tracked_tickets = set(self.state.open_positions.keys())
        untracked = mt5_tickets - tracked_tickets
        if untracked:
            logger.warning(f"Found untracked MT5 positions: {untracked}")
            self.telegram.send_message(
                f"⚠️ <b>Untracked Positions Found</b>\n\n"
                f"Tickets: {list(untracked)}\n"
                f"These positions are not managed by the bot."
            )
        
        # Verify SL is set on remaining positions
        for pos in mt5_positions:
            if pos['ticket'] in self.state.open_positions:
                if pos['sl'] == 0 or pos['sl'] is None:
                    logger.critical(f"Position {pos['ticket']} has NO SL after reconnect!")
                    self.telegram.critical_alert(
                        "POSITION WITHOUT SL",
                        f"Ticket: {pos['ticket']}\nSymbol: {pos['symbol']}\n\nSet SL immediately!"
                    )
        
        self._save_position_state()
        logger.info(f"Position verification complete - tracking {len(self.state.open_positions)} positions")
    
    def _check_market_notifications(self, active_indices: list):
        """Track market open/close and send notifications"""
        if not hasattr(self, '_previous_active'):
            self._previous_active = set()
            self._news_blocked = set()
        
        current_active = set(active_indices)
        
        # Markets that just opened
        newly_opened = current_active - self._previous_active
        for symbol in newly_opened:
            self.telegram.market_open(symbol)
            logger.info(f"Market opened: {symbol}")
        
        # Markets that just closed
        newly_closed = self._previous_active - current_active
        for symbol in newly_closed:
            self.telegram.market_closed(symbol)
            logger.info(f"Market closed: {symbol}")
        
        self._previous_active = current_active
    
    def can_trade(self, symbol: str) -> tuple:
        """
        Check if we can take a new trade
        
        Returns:
            (can_trade: bool, reason: str)
        """
        # Check if trading is paused via Telegram command
        if self._trading_paused:
            return False, "Trading paused via Telegram"
        
        # Check if trading is disabled
        if not self._trading_enabled:
            return False, "Trading disabled"
        
        # Check drawdown protection
        account = self.mt5.get_account_info()
        dd_status = self.dd_protection.update(account['balance'])
        
        if dd_status['is_max_dd_breach']:
            return False, f"Max drawdown limit reached ({dd_status['overall_dd']:.1f}%)"
        
        if dd_status['is_daily_dd_breach']:
            self.state.is_daily_limit_hit = True
            return False, f"Daily drawdown limit reached ({dd_status['daily_dd']:.1f}%)"
        
        # Check news filter
        is_news, news_event = self.news_filter.is_high_impact_news(symbol)
        if is_news:
            return False, f"High impact news: {news_event['title']} in {news_event['minutes_away']:.0f} min"
        
        # Check market hours
        if not is_market_open(symbol):
            return False, "Market closed"
        
        # Check existing positions
        positions = self.mt5.get_open_positions(symbol)
        if len(positions) >= 1:  # Only 1 position per symbol
            return False, "Already have position in this symbol"
        
        return True, "OK"
    
    def generate_signal(self, symbol: str) -> Optional[dict]:
        """Generate trading signal for a symbol"""
        config = get_index_config(symbol)
        if not config:
            return None
        
        # Get OHLC data
        df = self.mt5.get_ohlc_data(symbol, count=500)
        if df.empty:
            return None
        
        # Get or create strategy for this symbol
        if symbol not in self.strategies:
            self.strategies[symbol] = StrategyICTClean(
                sl_buffer=config['sl_buffer'],
                bos_required=2,
                capital=self.state.current_balance,
                risk_percent=self.dd_protection.get_adaptive_risk(self.state.current_balance),
                use_session_filter=False,  # We handle this externally
                use_sl_distance_filter=True,
                min_sl_distance=config['min_sl_distance'],
                max_sl_distance=config['max_sl_distance']
            )
        
        strategy = self.strategies[symbol]
        
        # Update capital and adaptive risk
        current_risk = self.dd_protection.get_adaptive_risk(self.state.current_balance)
        strategy.capital = self.state.current_balance
        strategy.risk_percent = current_risk
        strategy.risk_amount = self.state.current_balance * (current_risk / 100)
        
        # Get MT5 server time (from tick data or timezone conversion)
        try:
            tick = mt5.symbol_info_tick(symbol)
            if tick and tick.time:
                mt5_now = datetime.fromtimestamp(tick.time)
            else:
                import pytz
                local_tz = pytz.timezone('Asia/Kolkata')
                mt5_tz = pytz.timezone('Europe/Helsinki')
                mt5_now = datetime.now(local_tz).astimezone(mt5_tz).replace(tzinfo=None)
        except Exception:
            import pytz
            local_tz = pytz.timezone('Asia/Kolkata')
            mt5_tz = pytz.timezone('Europe/Helsinki')
            mt5_now = datetime.now(local_tz).astimezone(mt5_tz).replace(tzinfo=None)
        
        mt5_today = mt5_now.date()
        
        # Get today's data - using MT5 server date
        today_data = df[df.index.date == mt5_today]
        if len(today_data) < 30:
            return None
        
        # Get previous day data for PDH/PDL (matches backtest)
        yesterday = mt5_today - timedelta(days=1)
        prev_day_data = df[df.index.date == yesterday]
        if len(prev_day_data) > 0:
            pdh = prev_day_data['high'].max()
            pdl = prev_day_data['low'].min()
        else:
            pdh = None
            pdl = None
        
        # Filter to session start time (exclude pre-market structure)
        # Use MT5 server time for session start
        session_start_hour = config['mt5_open_hour']
        session_start_minute = config['mt5_open_minute']
        session_start = pd.Timestamp(mt5_today).replace(
            hour=session_start_hour, 
            minute=session_start_minute
        )
        market_hours_data = today_data[today_data.index >= session_start]
        
        if len(market_hours_data) < 10:
            return None
        
        # Use session data for day high/low (excludes pre-market extremes)
        day_high = market_hours_data['high'].max()
        day_low = market_hours_data['low'].min()
        
        # Generate signals - pass full today_data for structure analysis (matches backtest)
        # but use session-filtered day_high/day_low
        signals = strategy.generate_signals(
            df=today_data,  # Full day data for structure/order block detection
            day_high=day_high,
            day_low=day_low,
            pdh=pdh,  # Previous day high
            pdl=pdl,  # Previous day low
            debug=False
        )
        
        # Return latest unexecuted signal
        for signal in signals:
            if signal.entry_price and not signal.sl_hit and not signal.tp1_hit:
                return {
                    'symbol': symbol,
                    'direction': signal.direction,
                    'entry_price': signal.entry_price,
                    'stop_loss': signal.stop_loss,
                    'tp1': signal.tp1,
                    'tp2': signal.tp2,
                    'tp3': signal.tp3,
                    'tp_final': signal.tp_final,
                    'risk_points': signal.risk_points,
                    'position_size': signal.position_size
                }
        
        return None
    
    def execute_signal(self, signal: dict) -> bool:
        """Execute a trading signal"""
        symbol = signal['symbol']
        config = get_index_config(symbol)
        
        # Get account info
        account = self.mt5.get_account_info()
        sl_points = signal['risk_points']
        
        # Get adaptive risk based on current drawdown
        current_risk = self.dd_protection.get_adaptive_risk(account['balance'])
        
        # Calculate position size using LIVE BROKER DATA
        # Pass free_margin to enable 90% margin limit check
        sizing = calculate_position_size_with_broker_data(
            symbol=symbol,
            account_balance=account['balance'],
            risk_percent=current_risk,
            sl_points=sl_points,
            free_margin=account['free_margin'],
            max_margin_percent=90.0  # Max 90% of available margin
        )
        
        volume = sizing['lots']
        risk_amount = sizing['risk_amount']
        margin_required = sizing.get('margin_required', 0)
        
        # Log position sizing details
        logger.info(f"Position Sizing for {symbol}:")
        logger.info(f"  Risk Mode: {'ADAPTIVE' if current_risk != FUNDEDNEXT_RULES['risk_per_trade_percent'] else 'FIXED'} @ {current_risk}%")
        logger.info(f"  SL Distance: {sl_points} points")
        logger.info(f"  Point Value: ${sizing.get('point_value', 'N/A')}/point/lot")
        logger.info(f"  Risk Amount: ${risk_amount:.2f}")
        logger.info(f"  Lot Size: {volume}")
        logger.info(f"  Margin Required: ${margin_required:.2f}")
        logger.info(f"  Free Margin: ${account['free_margin']:.2f}")
        logger.info(f"  Data Source: {sizing.get('source', 'unknown')}")
        
        # Log if lots were reduced due to margin limit
        if sizing.get('margin_reduced', False):
            original_lots = sizing.get('original_lots', volume)
            logger.warning(f"  ⚠️ Lots reduced from {original_lots} to {volume} (90% margin limit)")
            self.telegram.send_message(
                f"⚠️ <b>POSITION REDUCED - {symbol}</b>\n\n"
                f"Original lots: {original_lots}\n"
                f"Reduced to: {volume}\n"
                f"Reason: 90% margin limit\n"
                f"Margin used: ${margin_required:.2f} / ${account['free_margin']:.2f}"
            )
        
        # Final margin check (should always pass now with 90% limit)
        if margin_required > account['free_margin']:
            logger.warning(f"Insufficient margin: Need ${margin_required:.2f}, Have ${account['free_margin']:.2f}")
            self.telegram.error_alert("Insufficient Margin", 
                f"Need ${margin_required:.2f}, Have ${account['free_margin']:.2f}")
            return False
        
        # Place order
        order_type = 'buy' if signal['direction'] == 'bullish' else 'sell'
        
        # Random delay 1-2 seconds to avoid prop firm detection
        delay = random.uniform(1.0, 2.0)
        logger.info(f"Adding {delay:.2f}s random delay before order execution")
        time.sleep(delay)
        
        result = self.mt5.place_order(
            symbol=symbol,
            order_type=order_type,
            volume=volume,
            sl=signal['stop_loss'],
            tp=signal['tp1'],  # Initial TP at TP1
            comment=f"ICT_{signal['direction']}"
        )
        
        if result['success']:
            # Store signal data for position management
            self.state.open_positions[result['ticket']] = signal
            self.state.daily_trades += 1
            logger.info(f"Signal executed: {order_type.upper()} {volume} {symbol}")
            
            # Add to trade journal
            journal_trade_id = self.journal.add_trade({
                'symbol': symbol,
                'direction': order_type.upper(),
                'entry_price': signal['entry_price'],
                'stop_loss': signal['stop_loss'],
                'tp1': signal['tp1'],
                'tp2': signal['tp2'],
                'tp3': signal['tp3'],
                'lot_size': volume,
                'risk_amount': risk_amount,
                'notes': f"MT5 Ticket: {result['ticket']}"
            })
            self.trade_id_map[result['ticket']] = journal_trade_id
            
            # Send Telegram notification
            self.telegram.trade_entry(
                symbol=symbol,
                direction=order_type,
                entry_price=signal['entry_price'],
                sl=signal['stop_loss'],
                tp1=signal['tp1'],
                tp2=signal['tp2'],
                tp3=signal['tp3'],
                lot_size=volume,
                risk_amount=risk_amount
            )
            
            # CRITICAL: Verify SL is properly set (failsafe)
            sl_verified = verify_sl_after_entry(
                ticket=result['ticket'],
                expected_sl=signal['stop_loss'],
                expected_tp=signal['tp1'],
                symbol=symbol
            )
            
            if not sl_verified:
                logger.critical(f"SL verification failed for {symbol} - position emergency closed")
                # Position was emergency closed by sl_protection, remove from tracking
                self.state.open_positions.pop(result['ticket'], None)
                return False
            
            return True
        
        return False
    
    def close_positions_before_market_close(self):
        """
        Close all open positions 15 minutes before their market close time.
        This prevents overnight exposure and gap risk.
        """
        positions = self.mt5.get_open_positions()
        if not positions:
            return
        
        for pos in positions:
            symbol = pos['symbol']
            ticket = pos['ticket']
            
            # Get symbol config
            if symbol not in PROFITABLE_INDICES:
                continue
            
            config = PROFITABLE_INDICES[symbol]
            
            # Get current time in symbol's timezone
            try:
                import pytz
                tz = pytz.timezone(config['timezone'])
                now = datetime.now(tz)
                current_time = now.time()
                
                # Force close time (15 mins before market close)
                from datetime import time as dt_time
                force_close_time = dt_time(config['force_close_hour'], config['force_close_minute'])
                market_close_time = dt_time(config['market_close_hour'], config['market_close_minute'])
                
                # Check if we're in the force close window (between force_close and market_close)
                if force_close_time <= current_time < market_close_time:
                    # Close this position
                    pnl = pos.get('profit', 0)
                    logger.info(f"FORCE CLOSE: Closing {symbol} position {ticket} before market close (P&L: ${pnl:.2f})")
                    
                    success = self.mt5.close_position(ticket)
                    
                    if success:
                        # Update journal if tracked
                        if ticket in self.state.open_positions:
                            signal = self.state.open_positions.pop(ticket)
                            
                            if ticket in self.trade_id_map:
                                account = self.mt5.get_account_info()
                                dd_status = self.dd_protection.update(account['balance'])
                                self.journal.close_trade(self.trade_id_map[ticket], {
                                    'exit_price': pos.get('price_current', 0),
                                    'exit_reason': 'MARKET_CLOSE',
                                    'pnl': pnl,
                                    'pnl_percent': (pnl / signal.get('risk_amount', 1)) * 100 if signal.get('risk_amount') else 0,
                                    'balance_after': account['balance'],
                                    'drawdown': dd_status['overall_dd']
                                })
                        
                        self.state.daily_pnl += pnl
                        
                        # Send Telegram notification
                        self.telegram.send_message(
                            f"🌙 <b>MARKET CLOSE - Position Closed</b>\n\n"
                            f"Symbol: {symbol}\n"
                            f"P&L: ${pnl:+.2f}\n"
                            f"Reason: Closing before {config['market_close_hour']}:{config['market_close_minute']:02d} market close\n\n"
                            f"No overnight positions held."
                        )
                        logger.info(f"Position {ticket} closed before market close")
                    else:
                        logger.error(f"Failed to force close position {ticket}")
                        
            except Exception as e:
                logger.error(f"Error checking market close for {symbol}: {e}")
    
    def manage_positions(self):
        """
        Manage open positions with full TP ladder:
        - TP1 (1:1): Move SL to breakeven + 2 points (cover spread)
        - TP2 (2:1): Close 50% of position
        - TP3 (3:1): Close 25% (half of remaining), start trailing
        - Trailing: Move SL to recent swing + 1 point as price extends
        """
        positions = self.mt5.get_open_positions()
        current_tickets = {pos['ticket'] for pos in positions}
        
        # Check for closed positions (in our tracking but not in MT5 anymore)
        closed_tickets = []
        for ticket in list(self.state.open_positions.keys()):
            if ticket not in current_tickets:
                closed_tickets.append(ticket)
        
        # Process closed positions
        for ticket in closed_tickets:
            signal = self.state.open_positions.pop(ticket)
            # Get trade history to find exit details
            history = mt5.history_deals_get(position=ticket)
            if history and len(history) > 0:
                # Sum all deals for this position (partial closes + final)
                total_pnl = sum(deal.profit for deal in history if deal.entry == 1)  # entry=1 means exit deal
                last_deal = history[-1]
                exit_price = last_deal.price
                
                # Determine exit reason based on signal state
                if signal.get('tp3_executed'):
                    exit_reason = "TRAILING_SL" if total_pnl > 0 else "SL_AFTER_TP3"
                elif signal.get('tp2_executed'):
                    exit_reason = "SL_AFTER_TP2"
                elif signal.get('tp1_executed'):
                    exit_reason = "SL_CTC"  # Cost-to-cost (breakeven)
                elif total_pnl > 0:
                    exit_reason = "TP_HIT"
                else:
                    exit_reason = "SL_HIT"
                
                # Update journal
                if ticket in self.trade_id_map:
                    account = self.mt5.get_account_info()
                    dd_status = self.dd_protection.update(account['balance'])
                    self.journal.close_trade(self.trade_id_map[ticket], {
                        'exit_price': exit_price,
                        'exit_reason': exit_reason,
                        'pnl': total_pnl,
                        'pnl_percent': (total_pnl / signal.get('risk_amount', 1)) * 100 if signal.get('risk_amount') else 0,
                        'balance_after': account['balance'],
                        'drawdown': dd_status['overall_dd']
                    })
                    # Send telegram
                    self.telegram.trade_closed(signal['symbol'], exit_reason, total_pnl)
                
                self.state.daily_pnl += total_pnl
                logger.info(f"Position {ticket} closed - {exit_reason} - P&L: ${total_pnl:.2f}")
        
        for pos in positions:
            ticket = pos['ticket']
            
            # Skip if not our trade
            if ticket not in self.state.open_positions:
                continue
            
            signal = self.state.open_positions[ticket]
            
            bid, ask = self.mt5.get_current_price(pos['symbol'])
            current = bid if pos['type'] == 'sell' else ask
            
            if current is None:
                continue
            
            is_buy = pos['type'] == 'buy'
            entry_price = signal['entry_price']
            
            # ====== TP1: Move SL to Breakeven + 2 points ======
            if not signal.get('tp1_executed'):
                tp1_hit = (current >= signal['tp1']) if is_buy else (current <= signal['tp1'])
                
                if tp1_hit:
                    signal['tp1_executed'] = True
                    old_sl = pos['sl']
                    # BE + 2 points to cover spread/slippage
                    new_sl = entry_price + 2 if is_buy else entry_price - 2
                    self.mt5.modify_position(ticket, sl=new_sl)
                    logger.info(f"TP1 hit for {ticket} - SL moved to BE+2 ({new_sl})")
                    self.telegram.tp_hit(pos['symbol'], 1, current, 0, pos['volume'])
                    self.telegram.sl_moved(pos['symbol'], old_sl, new_sl, "TP1 hit - SL to BE+2pts")
                    self._save_position_state()
            
            # ====== TP2: Close 50% of position ======
            elif not signal.get('tp2_executed'):
                tp2_hit = (current >= signal['tp2']) if is_buy else (current <= signal['tp2'])
                
                if tp2_hit:
                    close_volume = round(pos['volume'] * 0.5, 2)
                    
                    if close_volume >= 0.01:  # Min lot size
                        success = self.mt5.close_position(ticket, volume=close_volume)
                        if success:
                            signal['tp2_executed'] = True  # Only mark after successful close
                            pnl_points = abs(current - entry_price)
                            estimated_pnl = pnl_points * close_volume * 10  # Approximate
                            logger.info(f"TP2 hit for {ticket} - Closed 50% ({close_volume} lots)")
                            self.telegram.tp_hit(pos['symbol'], 2, current, estimated_pnl, close_volume)
                            self._save_position_state()
                        else:
                            logger.error(f"Failed to close 50% at TP2 for {ticket} - will retry next loop")
                    else:
                        signal['tp2_executed'] = True  # Skip if volume too small
                        logger.warning(f"TP2: Volume too small to close ({close_volume})")
            
            # ====== TP3: Close 25% (half of remaining 50%), start trailing ======
            elif not signal.get('tp3_executed'):
                tp3_hit = (current >= signal['tp3']) if is_buy else (current <= signal['tp3'])
                
                if tp3_hit:
                    # Close half of remaining (25% of original)
                    current_volume = pos['volume']
                    close_volume = round(current_volume * 0.5, 2)
                    
                    if close_volume >= 0.01:
                        success = self.mt5.close_position(ticket, volume=close_volume)
                        if success:
                            signal['tp3_executed'] = True  # Only mark after successful close
                            signal['trailing_active'] = True
                            pnl_points = abs(current - entry_price)
                            estimated_pnl = pnl_points * close_volume * 10
                            logger.info(f"TP3 hit for {ticket} - Closed 25% ({close_volume} lots), trailing active")
                            self.telegram.tp_hit(pos['symbol'], 3, current, estimated_pnl, close_volume)
                            
                            # Set initial trailing SL at TP2 level
                            trail_sl = signal['tp2'] if is_buy else signal['tp2']
                            self.mt5.modify_position(ticket, sl=trail_sl)
                            signal['trailing_sl'] = trail_sl
                            self._save_position_state()
                        else:
                            logger.error(f"Failed to close 25% at TP3 for {ticket} - will retry next loop")
                    else:
                        signal['tp3_executed'] = True  # Skip if volume too small
                        signal['trailing_active'] = True
                        logger.warning(f"TP3: Volume too small to close ({close_volume}), starting trailing anyway")
            
            # ====== Trailing Stop after TP3 ======
            elif signal.get('trailing_active'):
                # Trail SL to lock in more profits as price extends
                # For BUY: Move SL up as price goes up
                # For SELL: Move SL down as price goes down
                current_sl = pos['sl']
                
                if is_buy:
                    # Trail SL to (current_price - risk) but never below entry
                    risk = signal['tp1'] - entry_price  # Original 1R distance
                    new_trail_sl = current - risk
                    new_trail_sl = max(new_trail_sl, entry_price + 2)  # Never below BE+2
                    
                    if new_trail_sl > current_sl:
                        self.mt5.modify_position(ticket, sl=new_trail_sl)
                        logger.info(f"Trailing SL updated for {ticket}: {current_sl} -> {new_trail_sl}")
                        signal['trailing_sl'] = new_trail_sl
                        self._save_position_state()
                else:
                    # SELL: Trail down
                    risk = entry_price - signal['tp1']
                    new_trail_sl = current + risk
                    new_trail_sl = min(new_trail_sl, entry_price - 2)  # Never above BE-2
                    
                    if new_trail_sl < current_sl:
                        self.mt5.modify_position(ticket, sl=new_trail_sl)
                        logger.info(f"Trailing SL updated for {ticket}: {current_sl} -> {new_trail_sl}")
                        signal['trailing_sl'] = new_trail_sl
                        self._save_position_state()
    
    def run(self):
        """Main trading loop - runs 24/7 with enhanced error handling"""
        logger.info("Starting 24/7 trading loop...")
        
        # Error tracking for smart recovery
        consecutive_errors = 0
        max_consecutive_errors = 5
        last_error_time = None
        
        while not self._shutdown_requested:
            try:
                # Write heartbeat for watchdog
                self._write_heartbeat()
                
                # Check MT5 connection health
                if not self.check_connection():
                    continue  # Skip this iteration if reconnecting
                
                # Daily reset check
                self.check_daily_reset()
                
                # Check for scheduled reports
                self.check_scheduled_reports()
                
                # Update account info
                account = self.mt5.get_account_info()
                if not account or 'balance' not in account:
                    logger.warning("Failed to get account info, checking connection...")
                    self.connection_lost_time = datetime.now()
                    continue
                    
                self.state.current_balance = account['balance']
                
                # ====== CRITICAL: Drawdown Protection (Handles SL Slippage) ======
                # This runs EVERY loop iteration to catch slippage immediately
                dd_status = self.dd_protection.update(account['balance'])
                
                # Calculate actual loss including any slippage
                actual_daily_loss = self.state.daily_start_balance - account['balance']
                actual_daily_dd_pct = (actual_daily_loss / self.state.daily_start_balance) * 100 if self.state.daily_start_balance > 0 else 0
                
                # Warn at 80% of daily drawdown limit (only once per hour)
                if dd_status['daily_dd'] >= FUNDEDNEXT_RULES['daily_drawdown_stop'] * 0.8:
                    if not hasattr(self, '_last_dd_warning') or (datetime.now() - self._last_dd_warning).seconds > 3600:
                        self.telegram.drawdown_warning(
                            dd_status['daily_dd'],
                            FUNDEDNEXT_RULES['daily_drawdown_stop'],
                            account['balance']
                        )
                        self._last_dd_warning = datetime.now()
                
                # CRITICAL: Check if slippage caused unexpected drawdown
                if actual_daily_dd_pct > dd_status['daily_dd'] + 0.1:  # More than 0.1% slippage detected
                    slippage_amount = actual_daily_loss - (self.state.daily_start_balance * dd_status['daily_dd'] / 100)
                    logger.warning(f"SL SLIPPAGE DETECTED: Expected DD {dd_status['daily_dd']:.2f}%, Actual {actual_daily_dd_pct:.2f}%, Slippage ${slippage_amount:.2f}")
                    self.telegram.send_message(
                        f"⚠️ <b>SL SLIPPAGE DETECTED</b>\n\n"
                        f"Expected DD: {dd_status['daily_dd']:.2f}%\n"
                        f"Actual DD: {actual_daily_dd_pct:.2f}%\n"
                        f"Slippage: ${slippage_amount:.2f}\n\n"
                        f"Daily DD limit still protecting account."
                    )
                
                # MAX DRAWDOWN BREACH - Immediate stop (handles slippage)
                if dd_status['is_max_dd_breach']:
                    logger.critical("MAX DRAWDOWN BREACHED - STOPPING ALL TRADING")
                    # Close all open positions immediately
                    self._emergency_close_all_positions("MAX_DD_BREACH")
                    self.telegram.critical_alert("MAX DRAWDOWN BREACHED", 
                        f"Drawdown: {dd_status['overall_dd']:.2f}%\nBalance: ${account['balance']:,.2f}\n\n⚠️ All positions closed! Trading stopped!")
                    break
                
                # DAILY DRAWDOWN BREACH - Stop new trades, keep managing existing (handles slippage)
                if dd_status['is_daily_dd_breach']:
                    if not self.state.is_daily_limit_hit:
                        self.state.is_daily_limit_hit = True
                        self.telegram.daily_limit_reached(self.state.daily_trades, self.state.daily_pnl)
                        logger.warning(f"DAILY DRAWDOWN LIMIT HIT: {actual_daily_dd_pct:.2f}% (includes any slippage)")
                    
                    # Still manage existing positions (but no new trades)
                    self.manage_positions()
                    self._save_position_state()  # Save state periodically
                    time.sleep(10)
                    continue  # Skip signal generation
                
                # ====== End Drawdown Protection ======
                
                # Get active indices for current time
                active_indices = get_active_indices()
                
                # Track market open/close (notify once)
                self._check_market_notifications(active_indices)
                
                if active_indices:
                    logger.debug(f"Active indices: {active_indices}")
                
                # Check each active index for signals
                for symbol in active_indices:
                    try:
                        # Check if SL protection has paused trading (emergency recovery)
                        if sl_is_trading_paused():
                            logger.warning(f"Trading paused by SL protection - skipping signal generation")
                            break
                        
                        can_trade, reason = self.can_trade(symbol)
                        
                        if not can_trade:
                            logger.debug(f"{symbol}: Cannot trade - {reason}")
                            # Notify if blocked by news
                            if "news" in reason.lower() and hasattr(self, '_news_blocked'):
                                if symbol not in self._news_blocked:
                                    self.telegram.news_blocked(symbol, reason.split(":")[-1].strip(), 30)
                                    self._news_blocked.add(symbol)
                            continue
                        
                        # Clear news block tracking when can trade again
                        if hasattr(self, '_news_blocked') and symbol in self._news_blocked:
                            self._news_blocked.remove(symbol)
                        
                        # Generate signal
                        signal = self.generate_signal(symbol)
                        
                        if signal:
                            logger.info(f"Signal generated for {symbol}: {signal['direction']}")
                            success = self.execute_signal(signal)
                            if success:
                                self._save_position_state()  # Save after new trade
                            else:
                                self.telegram.order_failed(symbol, signal['direction'], "Execution failed")
                    
                    except Exception as symbol_error:
                        logger.error(f"Error processing {symbol}: {symbol_error}")
                        # Continue with other symbols
                        continue
                
                # ====== Close positions before market close (no overnight exposure) ======
                try:
                    self.close_positions_before_market_close()
                except Exception as close_error:
                    logger.error(f"Error in market close check: {close_error}")
                
                # Manage existing positions
                try:
                    self.manage_positions()
                except Exception as manage_error:
                    logger.error(f"Error managing positions: {manage_error}")
                    self.telegram.error_alert("Position Management Error", str(manage_error))
                
                # Send system status every hour
                if datetime.now().minute == 0 and datetime.now().second < 10:
                    positions = self.mt5.get_open_positions()
                    self.telegram.system_status(
                        balance=account['balance'],
                        open_positions=len(positions),
                        daily_pnl=self.state.daily_pnl,
                        daily_dd=dd_status['daily_dd'],
                        overall_dd=dd_status['overall_dd'],
                        active_markets=active_indices
                    )
                
                # Log status every 5 minutes
                if datetime.now().minute % 5 == 0 and datetime.now().second < 10:
                    logger.info(
                        f"Status - Balance: ${account['balance']:,.2f} | "
                        f"Daily DD: {dd_status['daily_dd']:.2f}% | "
                        f"Overall DD: {dd_status['overall_dd']:.2f}% | "
                        f"Active: {active_indices}"
                    )
                
                # Save state periodically (every 5 min)
                if datetime.now().minute % 5 == 0 and datetime.now().second < 10:
                    self._save_position_state()
                
                # Reset error counter on successful iteration
                consecutive_errors = 0
                
                # Sleep for 10 seconds between checks
                time.sleep(10)
                
            except KeyboardInterrupt:
                logger.info("Shutdown requested...")
                self._save_position_state()
                account = self.mt5.get_account_info()
                if account:
                    self.telegram.bot_stopped("User requested shutdown", account.get('balance', 0), self.state.daily_pnl)
                break
                
            except Exception as e:
                consecutive_errors += 1
                logger.error(f"Error in main loop ({consecutive_errors}/{max_consecutive_errors}): {e}")
                
                import traceback
                logger.error(f"Traceback: {traceback.format_exc()}")
                
                # Save state on error
                try:
                    self._save_position_state()
                except:
                    pass
                
                if consecutive_errors >= max_consecutive_errors:
                    logger.critical(f"Too many consecutive errors ({consecutive_errors}), stopping bot")
                    self.telegram.critical_alert(
                        "BOT STOPPED - Too Many Errors",
                        f"Error: {str(e)}\n\n{consecutive_errors} consecutive errors.\nManual intervention required!"
                    )
                    break
                else:
                    self.telegram.error_alert(f"Main Loop Error ({consecutive_errors}/{max_consecutive_errors})", str(e))
                    # Exponential backoff: 30s, 60s, 120s, 240s, 480s
                    wait_time = min(30 * (2 ** (consecutive_errors - 1)), 480)
                    logger.info(f"Waiting {wait_time}s before retry...")
                    time.sleep(wait_time)
        
        # Cleanup
        self._save_position_state()
        self.mt5.disconnect()
        logger.info("Trading stopped")
    
    def _emergency_close_all_positions(self, reason: str):
        """Emergency close all positions (used when DD limit breached)"""
        logger.warning(f"EMERGENCY CLOSE ALL POSITIONS - Reason: {reason}")
        
        positions = self.mt5.get_open_positions()
        failed_closes = []
        
        for pos in positions:
            closed = False
            for attempt in range(5):  # 5 retry attempts
                try:
                    result = self.mt5.close_position(pos['ticket'])
                    if result:
                        logger.info(f"Emergency closed position {pos['ticket']} on attempt {attempt + 1}")
                        closed = True
                        break
                except Exception as e:
                    logger.error(f"Emergency close attempt {attempt + 1} failed for {pos['ticket']}: {e}")
                time.sleep(1)  # Wait between retries
            
            if not closed:
                failed_closes.append(pos['ticket'])
                logger.critical(f"FAILED to emergency close position {pos['ticket']} after 5 attempts!")
        
        # Alert if any positions failed to close
        if failed_closes:
            self.telegram.critical_alert(
                "EMERGENCY CLOSE FAILED",
                f"Tickets: {failed_closes}\n\n⚠️ MANUAL INTERVENTION REQUIRED!\nThese positions could not be closed automatically."
            )


if __name__ == '__main__':
    trader = LiveTrader()
    
    if trader.initialize():
        trader.run()
    else:
        logger.error("Failed to initialize trader")
