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
    PROFITABLE_INDICES, 
    get_index_config,
    calculate_position_size,
    MT5_SERVER,
    MT5_LOGIN,
    MT5_PASSWORD,
    ACCOUNT_SIZE,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID
)
from strategy.strategy_ict_clean import StrategyICTClean
from telegram_notifier import TelegramNotifier
from trade_journal import TradeJournal, ReportScheduler

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('live/logs/trading.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


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
        
    def update(self, current_balance: float) -> dict:
        """Update balances and check limits"""
        # Update peak
        if current_balance > self.peak_balance:
            self.peak_balance = current_balance
        
        # Calculate drawdowns
        overall_dd = ((self.peak_balance - current_balance) / self.peak_balance) * 100
        daily_dd = ((self.daily_start_balance - current_balance) / self.daily_start_balance) * 100
        
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
    
    def reset_daily(self, current_balance: float):
        """Reset daily tracking (call at market open)"""
        self.daily_start_balance = current_balance


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
            "deviation": 20,
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
    
    def modify_position(self, ticket: int, sl: float = None, tp: float = None) -> bool:
        """Modify SL/TP of an open position"""
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
        
        if result.retcode != mt5.TRADE_RETCODE_DONE:
            logger.error(f"Modify failed: {result.comment}")
            return False
        
        logger.info(f"Position {ticket} modified - SL: {sl}, TP: {tp}")
        return True
    
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
            "deviation": 20,
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
    
    now = datetime.now()  # Assuming MT5 server time
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
        
        # Setup graceful shutdown handlers
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)
        atexit.register(self._cleanup)
    
    def _signal_handler(self, signum, frame):
        """Handle shutdown signals gracefully"""
        logger.info(f"Received signal {signum}, initiating graceful shutdown...")
        self._shutdown_requested = True
    
    def _cleanup(self):
        """Cleanup on exit"""
        logger.info("Cleaning up...")
        try:
            account = self.mt5.get_account_info()
            if account:
                self.telegram.bot_stopped("Graceful shutdown", account.get('balance', 0), self.state.daily_pnl)
        except:
            pass
        self.mt5.disconnect()
    
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
        
        # Send Telegram notification
        self.telegram.bot_started(list(PROFITABLE_INDICES.keys()), initial_balance)
        
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
                risk_percent=FUNDEDNEXT_RULES['risk_per_trade_percent'],
                use_session_filter=False,  # We handle this externally
                use_sl_distance_filter=True,
                min_sl_distance=config['min_sl_distance'],
                max_sl_distance=config['max_sl_distance']
            )
        
        strategy = self.strategies[symbol]
        
        # Update capital
        strategy.capital = self.state.current_balance
        strategy.risk_amount = self.state.current_balance * (FUNDEDNEXT_RULES['risk_per_trade_percent'] / 100)
        
        # Get today's high/low
        today_data = df[df.index.date == datetime.now().date()]
        if len(today_data) < 30:
            return None
        
        day_high = today_data['high'].max()
        day_low = today_data['low'].min()
        
        # Generate signals
        signals = strategy.generate_signals(
            df=today_data,
            day_high=day_high,
            day_low=day_low,
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
        
        # Calculate position size
        account = self.mt5.get_account_info()
        risk_amount = account['balance'] * (FUNDEDNEXT_RULES['risk_per_trade_percent'] / 100)
        sl_points = signal['risk_points']
        point_value = config.get('point_value', 1.0)
        
        volume = calculate_position_size(
            account['balance'],
            FUNDEDNEXT_RULES['risk_per_trade_percent'],
            sl_points,
            point_value
        )
        
        # Place order
        order_type = 'buy' if signal['direction'] == 'bullish' else 'sell'
        
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
            
            return True
        
        return False
    
    def manage_positions(self):
        """Manage open positions (move SL to BE at TP1, partial closes)"""
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
                last_deal = history[-1]
                exit_price = last_deal.price
                pnl = last_deal.profit
                
                # Determine exit reason
                if pnl > 0:
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
                        'pnl': pnl,
                        'pnl_percent': (pnl / signal.get('risk_amount', 1)) * 100 if signal.get('risk_amount') else 0,
                        'balance_after': account['balance'],
                        'drawdown': dd_status['overall_dd']
                    })
                    # Send telegram
                    self.telegram.trade_closed(signal['symbol'], exit_reason, pnl)
                
                self.state.daily_pnl += pnl
                logger.info(f"Position {ticket} closed - {exit_reason} - P&L: ${pnl:.2f}")
        
        for pos in positions:
            ticket = pos['ticket']
            
            # Skip if not our trade
            if ticket not in self.state.open_positions:
                continue
            
            signal = self.state.open_positions[ticket]
            current_price = pos['price']  # This is entry price, need current
            
            bid, ask = self.mt5.get_current_price(pos['symbol'])
            current = bid if pos['type'] == 'sell' else ask
            
            # Check if TP1 hit (for CTC - move SL to entry)
            if pos['type'] == 'buy':
                if current >= signal['tp1'] and pos['sl'] < signal['entry_price']:
                    # Move SL to breakeven
                    old_sl = pos['sl']
                    self.mt5.modify_position(ticket, sl=signal['entry_price'])
                    logger.info(f"TP1 hit for {ticket} - SL moved to BE")
                    self.telegram.tp_hit(pos['symbol'], 1, current, 0, pos['volume'])
                    self.telegram.sl_moved(pos['symbol'], old_sl, signal['entry_price'], "TP1 hit - moved to breakeven")
                    
            else:  # sell
                if current <= signal['tp1'] and pos['sl'] > signal['entry_price']:
                    old_sl = pos['sl']
                    self.mt5.modify_position(ticket, sl=signal['entry_price'])
                    logger.info(f"TP1 hit for {ticket} - SL moved to BE")
                    self.telegram.tp_hit(pos['symbol'], 1, current, 0, pos['volume'])
                    self.telegram.sl_moved(pos['symbol'], old_sl, signal['entry_price'], "TP1 hit - moved to breakeven")
    
    def run(self):
        """Main trading loop - runs 24/7"""
        logger.info("Starting 24/7 trading loop...")
        
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
                
                # Check drawdown status
                dd_status = self.dd_protection.update(account['balance'])
                
                # Warn at 80% of daily drawdown limit (only once per hour)
                if dd_status['daily_dd'] >= FUNDEDNEXT_RULES['daily_drawdown_stop'] * 0.8:
                    if not hasattr(self, '_last_dd_warning') or (datetime.now() - self._last_dd_warning).seconds > 3600:
                        self.telegram.drawdown_warning(
                            dd_status['daily_dd'],
                            FUNDEDNEXT_RULES['daily_drawdown_stop'],
                            account['balance']
                        )
                        self._last_dd_warning = datetime.now()
                
                if dd_status['is_max_dd_breach']:
                    logger.critical("MAX DRAWDOWN BREACHED - STOPPING ALL TRADING")
                    self.telegram.critical_alert("MAX DRAWDOWN BREACHED", 
                        f"Drawdown: {dd_status['overall_dd']:.2f}%\nBalance: ${account['balance']:,.2f}\n\nAll trading stopped!")
                    break
                
                # Check daily limit
                if dd_status['is_daily_dd_breach'] and not self.state.is_daily_limit_hit:
                    self.state.is_daily_limit_hit = True
                    self.telegram.daily_limit_reached(self.state.daily_trades, self.state.daily_pnl)
                
                # Get active indices for current time
                active_indices = get_active_indices()
                
                # Track market open/close (notify once)
                self._check_market_notifications(active_indices)
                
                if active_indices:
                    logger.debug(f"Active indices: {active_indices}")
                
                # Check each active index for signals
                for symbol in active_indices:
                    can_trade, reason = self.can_trade(symbol)
                    
                    if not can_trade:
                        logger.debug(f"{symbol}: Cannot trade - {reason}")
                        # Notify if blocked by news
                        if "news" in reason.lower() and hasattr(self, '_news_blocked'):
                            if symbol not in self._news_blocked:
                                # Extract event name from reason
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
                        if not success:
                            self.telegram.order_failed(symbol, signal['direction'], "Execution failed")
                
                # Manage existing positions
                self.manage_positions()
                
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
                
                # Sleep for 10 seconds between checks
                time.sleep(10)
                
            except KeyboardInterrupt:
                logger.info("Shutdown requested...")
                account = self.mt5.get_account_info()
                self.telegram.bot_stopped("User requested shutdown", account['balance'], self.state.daily_pnl)
                break
            except Exception as e:
                logger.error(f"Error in main loop: {e}")
                self.telegram.error_alert("Main Loop Error", str(e))
                time.sleep(30)  # Wait before retrying
        
        # Cleanup
        self.mt5.disconnect()
        logger.info("Trading stopped")


if __name__ == '__main__':
    trader = LiveTrader()
    
    if trader.initialize():
        trader.run()
    else:
        logger.error("Failed to initialize trader")
