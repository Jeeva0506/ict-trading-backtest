"""
SL Protection Module - Stop Loss Failsafe System
=================================================
Critical safety layer for prop firm trading that:
- Verifies SL is properly set after entry
- Monitors positions for SL disappearance
- Emergency closes if loss exceeds limit
- Monitors MT5 connection health
- Pauses trading after emergencies

Usage:
    from sl_protection import *
    
    set_telegram_config(BOT_TOKEN, CHAT_ID)
    start_monitoring()
    
    # After each trade entry:
    verify_sl_after_entry(ticket, expected_sl, expected_tp, symbol)
"""

import MetaTrader5 as mt5
import time
import logging
import threading
import requests
from datetime import datetime, timedelta
from typing import Optional, Tuple, Dict, Any
from pathlib import Path

# ============================================================================
# CONFIGURATION CONSTANTS
# ============================================================================

SL_VERIFICATION_DELAY = 2  # seconds to wait after order
SL_VERIFICATION_ATTEMPTS = 3  # max retry attempts
SL_TOLERANCE_POINTS = 5  # acceptable SL deviation
EMERGENCY_LOSS_MULTIPLIER = 1.3  # close at 130% of intended risk
EMERGENCY_PAUSE_MINUTES = 15  # pause duration after emergency
POSITION_MONITOR_INTERVAL = 10  # check positions every 10 seconds
CONNECTION_MONITOR_INTERVAL = 30  # check connection every 30 seconds
ORDER_DEVIATION_POINTS = 20  # max slippage allowed
RISK_PER_TRADE = 0.015  # 1.5% risk per trade
FAILED_CLOSE_REMINDER_INTERVAL = 120  # remind every 2 minutes for failed closes

# ============================================================================
# GLOBAL STATE VARIABLES
# ============================================================================

# Telegram Configuration
TELEGRAM_BOT_TOKEN: str = ""
TELEGRAM_CHAT_ID: str = ""

# Trading State
trading_paused_until: Optional[datetime] = None
original_sl_tracker: Dict[int, float] = {}  # {ticket: expected_sl_price}
failed_close_tracker: Dict[int, Dict] = {}  # {ticket: {'symbol': str, 'reason': str, 'time': datetime, 'last_reminder': datetime}}
position_lock: Optional[threading.Lock] = None
monitoring_active: bool = False

# Thread references
_monitor_thread: Optional[threading.Thread] = None
_connection_thread: Optional[threading.Thread] = None

# Logger
logger: Optional[logging.Logger] = None


# ============================================================================
# FUNCTION 1: set_telegram_config
# ============================================================================

def set_telegram_config(bot_token: str, chat_id: str) -> None:
    """
    Initialize Telegram credentials for alerts.
    
    Args:
        bot_token: Telegram bot token from BotFather
        chat_id: Target chat/channel ID for notifications
    """
    global TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
    
    TELEGRAM_BOT_TOKEN = bot_token
    TELEGRAM_CHAT_ID = chat_id
    
    if logger:
        logger.info("Telegram configuration set")


# ============================================================================
# FUNCTION 2: send_telegram_alert
# ============================================================================

def send_telegram_alert(message: str) -> bool:
    """
    Send alert message via Telegram API.
    
    Args:
        message: Alert message to send (supports HTML formatting)
    
    Returns:
        True if sent successfully, False otherwise
    """
    global logger
    
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        if logger:
            logger.warning("Telegram not configured - alert not sent")
        return False
    
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML"
        }
        
        response = requests.post(url, json=payload, timeout=10)
        
        if response.status_code == 200:
            if logger:
                logger.debug(f"Telegram alert sent: {message[:50]}...")
            return True
        else:
            if logger:
                logger.warning(f"Telegram API error: {response.status_code}")
            return False
            
    except Exception as e:
        if logger:
            logger.error(f"Telegram send failed: {e}")
        return False


# ============================================================================
# FUNCTION 3: setup_logging
# ============================================================================

def setup_logging() -> logging.Logger:
    """
    Configure logging to file and console.
    
    Returns:
        Configured logger object
    """
    global logger
    
    # Create logs directory if needed
    log_dir = Path(__file__).parent / 'logs'
    log_dir.mkdir(exist_ok=True)
    log_file = log_dir / 'sl_protection.log'
    
    # Create logger
    logger = logging.getLogger('sl_protection')
    logger.setLevel(logging.DEBUG)
    
    # Clear existing handlers
    logger.handlers.clear()
    
    # File handler
    file_handler = logging.FileHandler(log_file, encoding='utf-8')
    file_handler.setLevel(logging.DEBUG)
    file_format = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    file_handler.setFormatter(file_format)
    
    # Console handler with UTF-8 encoding for Windows
    import io
    if sys.platform == 'win32':
        console_stream = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    else:
        console_stream = sys.stdout
    console_handler = logging.StreamHandler(console_stream)
    console_handler.setLevel(logging.INFO)
    console_format = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    console_handler.setFormatter(console_format)
    
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    
    logger.info("SL Protection logging initialized")
    
    return logger


# ============================================================================
# FUNCTION 4: pause_trading
# ============================================================================

def pause_trading(minutes: int) -> None:
    """
    Pause trading for specified duration.
    
    Args:
        minutes: Number of minutes to pause trading
    """
    global trading_paused_until, position_lock
    
    try:
        if position_lock:
            with position_lock:
                trading_paused_until = datetime.now() + timedelta(minutes=minutes)
        else:
            trading_paused_until = datetime.now() + timedelta(minutes=minutes)
        
        if logger:
            logger.warning(f"Trading paused for {minutes} minutes until {trading_paused_until}")
        
        send_telegram_alert(f"⏸️ <b>Trading Paused</b>\n\nDuration: {minutes} minutes\nResumes: {trading_paused_until.strftime('%H:%M:%S')}")
        
    except Exception as e:
        if logger:
            logger.error(f"Error pausing trading: {e}")


# ============================================================================
# FUNCTION 5: is_trading_paused
# ============================================================================

def is_trading_paused() -> bool:
    """
    Check if trading is currently paused.
    
    Returns:
        True if trading is paused, False otherwise
    """
    global trading_paused_until, position_lock
    
    try:
        if position_lock:
            with position_lock:
                if trading_paused_until is None:
                    return False
                return datetime.now() < trading_paused_until
        else:
            if trading_paused_until is None:
                return False
            return datetime.now() < trading_paused_until
            
    except Exception as e:
        if logger:
            logger.error(f"Error checking pause status: {e}")
        return False


# ============================================================================
# FUNCTION 6: track_original_sl
# ============================================================================

def track_original_sl(ticket: int, expected_sl: float) -> None:
    """
    Store the intended SL price for a position.
    
    Args:
        ticket: MT5 position ticket number
        expected_sl: Expected stop loss price
    """
    global original_sl_tracker, position_lock
    
    try:
        if position_lock:
            with position_lock:
                original_sl_tracker[ticket] = expected_sl
        else:
            original_sl_tracker[ticket] = expected_sl
        
        if logger:
            logger.debug(f"Tracking SL for ticket {ticket}: {expected_sl}")
            
    except Exception as e:
        if logger:
            logger.error(f"Error tracking SL: {e}")


# ============================================================================
# FUNCTION 7: get_original_sl
# ============================================================================

def get_original_sl(ticket: int) -> Optional[float]:
    """
    Retrieve the expected SL for a position.
    
    Args:
        ticket: MT5 position ticket number
    
    Returns:
        Expected SL price or None if not found
    """
    global original_sl_tracker, position_lock
    
    try:
        if position_lock:
            with position_lock:
                return original_sl_tracker.get(ticket)
        else:
            return original_sl_tracker.get(ticket)
            
    except Exception as e:
        if logger:
            logger.error(f"Error getting original SL: {e}")
        return None


# ============================================================================
# FUNCTION 8: verify_sl_after_entry
# ============================================================================

def verify_sl_after_entry(ticket: int, expected_sl: float, expected_tp: float, symbol: str) -> bool:
    """
    Verify SL is properly set after order entry. Retry or emergency close if failed.
    
    Args:
        ticket: MT5 position ticket number
        expected_sl: Expected stop loss price
        expected_tp: Expected take profit price
        symbol: Trading symbol
    
    Returns:
        True if SL verified, False if emergency closed
    """
    global logger
    
    try:
        # Wait for order to settle
        time.sleep(SL_VERIFICATION_DELAY)
        
        for attempt in range(1, SL_VERIFICATION_ATTEMPTS + 1):
            if logger:
                logger.info(f"SL verification attempt {attempt}/{SL_VERIFICATION_ATTEMPTS} for {symbol} ticket {ticket}")
            
            # Get position
            positions = mt5.positions_get(ticket=ticket)
            
            # Check if position exists
            if not positions or len(positions) == 0:
                if logger:
                    logger.warning(f"Position {ticket} not found - may have already closed")
                return False
            
            position = positions[0]
            
            # Check if SL is set
            if position.sl is None or position.sl == 0:
                if logger:
                    logger.warning(f"SL not set for {symbol} ticket {ticket}, attempting to set...")
                
                # Try to modify position to add SL
                request = {
                    "action": mt5.TRADE_ACTION_SLTP,
                    "symbol": symbol,
                    "position": ticket,
                    "sl": expected_sl,
                    "tp": expected_tp,
                }
                
                result = mt5.order_send(request)
                
                if result and result.retcode == mt5.TRADE_RETCODE_DONE:
                    if logger:
                        logger.info(f"SL set successfully for {symbol} ticket {ticket}")
                else:
                    error_code = result.retcode if result else "Unknown"
                    if logger:
                        logger.error(f"Failed to set SL: error code {error_code}")
                    
                    if attempt < SL_VERIFICATION_ATTEMPTS:
                        time.sleep(3)
                        continue
                    else:
                        # All retries failed - emergency close
                        if logger:
                            logger.critical(f"SL verification FAILED after {SL_VERIFICATION_ATTEMPTS} attempts - EMERGENCY CLOSE")
                        emergency_close_position(ticket, "SL verification failed after max retries")
                        return False
            
            # Check SL deviation
            elif abs(position.sl - expected_sl) > SL_TOLERANCE_POINTS:
                if logger:
                    logger.warning(f"SL deviation detected: expected {expected_sl}, actual {position.sl}")
                
                # Try to correct SL
                request = {
                    "action": mt5.TRADE_ACTION_SLTP,
                    "symbol": symbol,
                    "position": ticket,
                    "sl": expected_sl,
                    "tp": expected_tp,
                }
                
                result = mt5.order_send(request)
                
                if result and result.retcode == mt5.TRADE_RETCODE_DONE:
                    if logger:
                        logger.info(f"SL corrected for {symbol} ticket {ticket}")
                else:
                    if attempt < SL_VERIFICATION_ATTEMPTS:
                        time.sleep(3)
                        continue
                    else:
                        if logger:
                            logger.critical(f"Cannot correct SL deviation - EMERGENCY CLOSE")
                        emergency_close_position(ticket, "SL deviation cannot be corrected")
                        return False
            else:
                # SL is verified correctly
                track_original_sl(ticket, expected_sl)
                
                if logger:
                    logger.info(f"✓ SL Verified: {symbol} at {expected_sl} (ticket {ticket})")
                
                send_telegram_alert(f"✓ <b>SL Verified</b>\n\nSymbol: {symbol}\nTicket: {ticket}\nSL: {expected_sl}\nTP: {expected_tp}")
                
                return True
        
        # If we get here, verification failed
        if logger:
            logger.critical(f"SL verification exhausted all attempts for {symbol} ticket {ticket}")
        emergency_close_position(ticket, "SL verification exhausted")
        return False
        
    except Exception as e:
        if logger:
            logger.error(f"Exception in SL verification: {e}")
        import traceback
        logger.error(traceback.format_exc())
        
        # On exception, try emergency close
        try:
            emergency_close_position(ticket, f"Verification exception: {str(e)}")
        except:
            pass
        return False


# ============================================================================
# FUNCTION 9: emergency_close_position
# ============================================================================

def emergency_close_position(ticket: int, reason: str) -> bool:
    """
    Emergency close a position immediately.
    
    Args:
        ticket: MT5 position ticket number
        reason: Reason for emergency close
    
    Returns:
        True if closed successfully, False otherwise
    """
    global original_sl_tracker, position_lock, logger
    
    try:
        # Get position details
        positions = mt5.positions_get(ticket=ticket)
        
        if not positions or len(positions) == 0:
            if logger:
                logger.info(f"Position {ticket} not found - already closed")
            
            # Remove from tracker
            if position_lock:
                with position_lock:
                    original_sl_tracker.pop(ticket, None)
            else:
                original_sl_tracker.pop(ticket, None)
            
            return True
        
        position = positions[0]
        symbol = position.symbol
        volume = position.volume
        profit = position.profit
        
        # Determine close order type and price
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            if logger:
                logger.error(f"Cannot get tick for {symbol}")
            send_telegram_alert(f"🚨🚨 <b>CRITICAL</b>: Cannot get price for {symbol} ticket {ticket}\n\n<b>MANUAL INTERVENTION NEEDED</b>")
            return False
        
        if position.type == mt5.POSITION_TYPE_BUY:
            order_type = mt5.ORDER_TYPE_SELL
            price = tick.bid
        else:
            order_type = mt5.ORDER_TYPE_BUY
            price = tick.ask
        
        # Create close request
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": volume,
            "type": order_type,
            "position": ticket,
            "price": price,
            "deviation": ORDER_DEVIATION_POINTS,
            "magic": 234000,
            "comment": f"Emergency: {reason[:30]}",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        
        if logger:
            logger.warning(f"EMERGENCY CLOSE: {symbol} ticket {ticket} - {reason}")
        
        # Send close order
        result = mt5.order_send(request)
        
        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
            if logger:
                logger.info(f"Emergency close SUCCESS: {symbol} ticket {ticket} P&L: ${profit:.2f}")
            
            send_telegram_alert(
                f"🚨 <b>EMERGENCY CLOSE</b>\n\n"
                f"Symbol: {symbol}\n"
                f"Ticket: {ticket}\n"
                f"Reason: {reason}\n"
                f"P&L: ${profit:+.2f}"
            )
            
            # Remove from tracker
            if position_lock:
                with position_lock:
                    original_sl_tracker.pop(ticket, None)
            else:
                original_sl_tracker.pop(ticket, None)
            
            # Pause trading after emergency
            pause_trading(EMERGENCY_PAUSE_MINUTES)
            
            return True
        else:
            error_code = result.retcode if result else "Unknown"
            if logger:
                logger.critical(f"EMERGENCY CLOSE FAILED: {symbol} ticket {ticket} - Error: {error_code}")
            
            send_telegram_alert(
                f"🚨🚨 <b>CRITICAL: EMERGENCY CLOSE FAILED</b>\n\n"
                f"Symbol: {symbol}\n"
                f"Ticket: {ticket}\n"
                f"Error: {error_code}\n\n"
                f"<b>⚠️ MANUAL INTERVENTION NEEDED ⚠️</b>\n\n"
                f"Open MT5 and close this position manually!\n"
                f"You will receive reminders every 2 minutes."
            )
            
            # Track failed close for persistent reminders
            if position_lock:
                with position_lock:
                    failed_close_tracker[ticket] = {
                        'symbol': symbol,
                        'reason': reason,
                        'time': datetime.now(),
                        'last_reminder': datetime.now(),
                        'error': error_code
                    }
            else:
                failed_close_tracker[ticket] = {
                    'symbol': symbol,
                    'reason': reason,
                    'time': datetime.now(),
                    'last_reminder': datetime.now(),
                    'error': error_code
                }
            
            return False
            
    except Exception as e:
        if logger:
            logger.critical(f"Exception in emergency close: {e}")
            import traceback
            logger.critical(traceback.format_exc())
        
        send_telegram_alert(
            f"🚨🚨 <b>CRITICAL EXCEPTION</b>\n\n"
            f"Cannot close ticket {ticket}\n"
            f"Error: {str(e)}\n\n"
            f"<b>⚠️ MANUAL INTERVENTION NEEDED ⚠️</b>\n\n"
            f"Open MT5 and close this position manually!\n"
            f"You will receive reminders every 2 minutes."
        )
        
        # Track failed close for persistent reminders
        if position_lock:
            with position_lock:
                failed_close_tracker[ticket] = {
                    'symbol': 'Unknown',
                    'reason': str(e),
                    'time': datetime.now(),
                    'last_reminder': datetime.now(),
                    'error': str(e)
                }
        else:
            failed_close_tracker[ticket] = {
                'symbol': 'Unknown',
                'reason': str(e),
                'time': datetime.now(),
                'last_reminder': datetime.now(),
                'error': str(e)
            }
        
        return False


# ============================================================================
# FUNCTION 10: close_all_positions
# ============================================================================

def close_all_positions(reason: str = "Unknown") -> int:
    """
    Close all open positions.
    
    Args:
        reason: Reason for closing all positions
    
    Returns:
        Number of positions closed
    """
    global logger
    
    try:
        positions = mt5.positions_get()
        
        if not positions:
            if logger:
                logger.info("No positions to close")
            return 0
        
        closed_count = 0
        failed_count = 0
        total_pnl = 0
        
        if logger:
            logger.warning(f"CLOSE ALL POSITIONS: {len(positions)} positions - Reason: {reason}")
        
        for position in positions:
            total_pnl += position.profit
            success = emergency_close_position(position.ticket, reason)
            
            if success:
                closed_count += 1
            else:
                failed_count += 1
        
        if logger:
            logger.info(f"Close all complete: {closed_count} closed, {failed_count} failed, Total P&L: ${total_pnl:.2f}")
        
        send_telegram_alert(
            f"🚨 <b>ALL POSITIONS CLOSED</b>\n\n"
            f"Reason: {reason}\n"
            f"Closed: {closed_count}\n"
            f"Failed: {failed_count}\n"
            f"Total P&L: ${total_pnl:+.2f}"
        )
        
        return closed_count
        
    except Exception as e:
        if logger:
            logger.critical(f"Exception closing all positions: {e}")
        return 0


# ============================================================================
# FUNCTION 11: monitor_open_positions
# ============================================================================

def monitor_open_positions() -> None:
    """
    Background thread to continuously monitor open positions.
    Checks for:
    - Missing SL
    - Excessive loss
    - Unexpected SL changes
    """
    global monitoring_active, logger
    
    if logger:
        logger.info("Position monitoring thread started")
    
    while monitoring_active:
        try:
            positions = mt5.positions_get()
            
            if not positions:
                time.sleep(POSITION_MONITOR_INTERVAL)
                continue
            
            # Get account balance for risk calculation
            account_info = mt5.account_info()
            if account_info:
                account_balance = account_info.balance
            else:
                account_balance = 6000  # Fallback
            
            for position in positions:
                ticket = position.ticket
                symbol = position.symbol
                profit = position.profit
                current_sl = position.sl
                
                # CHECK 1: SL exists
                if current_sl is None or current_sl == 0:
                    if logger:
                        logger.critical(f"SL DISAPPEARED: {symbol} ticket {ticket}")
                    emergency_close_position(ticket, "SL disappeared")
                    continue
                
                # CHECK 2: Loss not exceeding limit
                max_loss = -(account_balance * RISK_PER_TRADE * EMERGENCY_LOSS_MULTIPLIER)
                if profit < max_loss:
                    if logger:
                        logger.critical(f"LOSS EXCEEDED 130%: {symbol} ticket {ticket} - Loss: ${profit:.2f}")
                    emergency_close_position(ticket, f"Loss exceeded limit (${profit:.2f})")
                    continue
                
                # CHECK 3: SL hasn't moved unexpectedly
                expected_sl = get_original_sl(ticket)
                if expected_sl is not None:
                    sl_deviation = abs(current_sl - expected_sl)
                    if sl_deviation > 10:  # More than 10 points deviation
                        if logger:
                            logger.warning(f"SL MOVED UNEXPECTEDLY: {symbol} from {expected_sl} to {current_sl}")
                        
                        send_telegram_alert(
                            f"⚠️ <b>SL MOVED</b>\n\n"
                            f"Symbol: {symbol}\n"
                            f"Ticket: {ticket}\n"
                            f"Expected: {expected_sl}\n"
                            f"Current: {current_sl}\n"
                            f"Deviation: {sl_deviation:.1f} points"
                        )
                        
                        # Update tracker to new value (it may be a valid BE move)
                        track_original_sl(ticket, current_sl)
            
            # CHECK 4: Send reminders for failed closes that still need manual intervention
            check_failed_close_reminders()
            
            time.sleep(POSITION_MONITOR_INTERVAL)
            
        except Exception as e:
            if logger:
                logger.error(f"Error in position monitor: {e}")
            time.sleep(POSITION_MONITOR_INTERVAL)
    
    if logger:
        logger.info("Position monitoring thread stopped")


# ============================================================================
# FUNCTION 11b: check_failed_close_reminders
# ============================================================================

def check_failed_close_reminders() -> None:
    """
    Check for failed closes that need reminders.
    Sends alerts every FAILED_CLOSE_REMINDER_INTERVAL seconds.
    Auto-clears if position no longer exists (manually closed).
    """
    global failed_close_tracker, position_lock, logger
    
    try:
        if not failed_close_tracker:
            return
        
        # Get current open positions
        positions = mt5.positions_get()
        current_tickets = set()
        if positions:
            current_tickets = {pos.ticket for pos in positions}
        
        tickets_to_remove = []
        
        if position_lock:
            with position_lock:
                for ticket, info in list(failed_close_tracker.items()):
                    # Check if position still exists
                    if ticket not in current_tickets:
                        # Position was manually closed!
                        if logger:
                            logger.info(f"Failed close {ticket} cleared - position manually closed")
                        
                        send_telegram_alert(
                            f"\u2705 <b>MANUAL CLOSE CONFIRMED</b>\n\n"
                            f"Symbol: {info['symbol']}\n"
                            f"Ticket: {ticket}\n"
                            f"\nThank you for manual intervention."
                        )
                        
                        tickets_to_remove.append(ticket)
                        continue
                    
                    # Check if reminder is due
                    time_since_reminder = (datetime.now() - info['last_reminder']).total_seconds()
                    
                    if time_since_reminder >= FAILED_CLOSE_REMINDER_INTERVAL:
                        # Send reminder
                        time_since_fail = (datetime.now() - info['time']).total_seconds() / 60
                        
                        send_telegram_alert(
                            f"\ud83d\udd14 <b>REMINDER: MANUAL CLOSE NEEDED</b>\n\n"
                            f"Symbol: {info['symbol']}\n"
                            f"Ticket: {ticket}\n"
                            f"Failed: {time_since_fail:.0f} minutes ago\n"
                            f"Reason: {info['reason']}\n\n"
                            f"<b>Open MT5 and close this position NOW!</b>\n\n"
                            f"(Reminder every 2 min until closed)"
                        )
                        
                        if logger:
                            logger.warning(f"Sent reminder for failed close: {ticket}")
                        
                        # Update last reminder time
                        failed_close_tracker[ticket]['last_reminder'] = datetime.now()
                
                # Remove cleared tickets
                for ticket in tickets_to_remove:
                    failed_close_tracker.pop(ticket, None)
        else:
            # No lock version
            for ticket, info in list(failed_close_tracker.items()):
                if ticket not in current_tickets:
                    send_telegram_alert(
                        f"\u2705 <b>MANUAL CLOSE CONFIRMED</b>\n\n"
                        f"Symbol: {info['symbol']}\n"
                        f"Ticket: {ticket}"
                    )
                    tickets_to_remove.append(ticket)
                    continue
                
                time_since_reminder = (datetime.now() - info['last_reminder']).total_seconds()
                if time_since_reminder >= FAILED_CLOSE_REMINDER_INTERVAL:
                    time_since_fail = (datetime.now() - info['time']).total_seconds() / 60
                    send_telegram_alert(
                        f"\ud83d\udd14 <b>REMINDER: MANUAL CLOSE NEEDED</b>\n\n"
                        f"Symbol: {info['symbol']}\n"
                        f"Ticket: {ticket}\n"
                        f"Failed: {time_since_fail:.0f} min ago\n"
                        f"<b>Close in MT5 NOW!</b>"
                    )
                    failed_close_tracker[ticket]['last_reminder'] = datetime.now()
            
            for ticket in tickets_to_remove:
                failed_close_tracker.pop(ticket, None)
                
    except Exception as e:
        if logger:
            logger.error(f"Error in failed close reminder check: {e}")


# ============================================================================
# FUNCTION 11c: get_failed_closes
# ============================================================================

def get_failed_closes() -> Dict[int, Dict]:
    """
    Get list of failed closes that need manual intervention.
    
    Returns:
        Dictionary of {ticket: info} for failed closes
    """
    global failed_close_tracker, position_lock
    
    if position_lock:
        with position_lock:
            return dict(failed_close_tracker)
    return dict(failed_close_tracker)


# ============================================================================
# FUNCTION 12: reconnect_mt5
# ============================================================================

def reconnect_mt5() -> bool:
    """
    Attempt to reconnect to MT5.
    
    Returns:
        True if reconnected, False if all attempts failed
    """
    global logger
    
    max_attempts = 3
    
    for attempt in range(1, max_attempts + 1):
        try:
            if logger:
                logger.info(f"MT5 reconnect attempt {attempt}/{max_attempts}")
            
            # Shutdown and reinitialize
            mt5.shutdown()
            time.sleep(2)
            
            if mt5.initialize():
                if logger:
                    logger.info("MT5 reconnected successfully")
                send_telegram_alert("✓ MT5 reconnected successfully")
                return True
            else:
                if logger:
                    logger.warning(f"MT5 initialize failed: {mt5.last_error()}")
                
        except Exception as e:
            if logger:
                logger.error(f"Reconnect attempt {attempt} failed: {e}")
        
        if attempt < max_attempts:
            time.sleep(10)
    
    if logger:
        logger.critical("All MT5 reconnect attempts failed")
    return False


# ============================================================================
# FUNCTION 13: monitor_mt5_connection
# ============================================================================

def monitor_mt5_connection() -> None:
    """
    Background thread to monitor MT5 connection health.
    """
    global monitoring_active, logger
    
    if logger:
        logger.info("MT5 connection monitoring thread started")
    
    while monitoring_active:
        try:
            # Check MT5 terminal connection
            terminal_info = mt5.terminal_info()
            
            if terminal_info is None:
                if logger:
                    logger.critical("MT5 connection lost!")
                
                send_telegram_alert("🚨 <b>MT5 DISCONNECTED</b>\n\nAttempting reconnect...")
                
                if reconnect_mt5():
                    if logger:
                        logger.info("Connection restored")
                else:
                    if logger:
                        logger.critical("Cannot reconnect - closing all positions")
                    close_all_positions("MT5 connection failure")
                    pause_trading(30)
            
            # Check tick freshness for symbols with positions
            positions = mt5.positions_get()
            if positions:
                for position in positions:
                    try:
                        tick = mt5.symbol_info_tick(position.symbol)
                        if tick:
                            time_since_tick = time.time() - tick.time
                            if time_since_tick > 60:  # No tick for 60 seconds
                                if logger:
                                    logger.warning(f"Stale tick for {position.symbol}: {time_since_tick:.0f}s old")
                                
                                send_telegram_alert(
                                    f"⚠️ <b>STALE DATA</b>\n\n"
                                    f"Symbol: {position.symbol}\n"
                                    f"Last tick: {time_since_tick:.0f} seconds ago"
                                )
                    except:
                        pass
            
            time.sleep(CONNECTION_MONITOR_INTERVAL)
            
        except Exception as e:
            if logger:
                logger.error(f"Error in connection monitor: {e}")
            time.sleep(CONNECTION_MONITOR_INTERVAL)
    
    if logger:
        logger.info("MT5 connection monitoring thread stopped")


# ============================================================================
# FUNCTION 14: start_monitoring
# ============================================================================

def start_monitoring() -> Tuple[threading.Thread, threading.Thread]:
    """
    Start all monitoring threads.
    
    Returns:
        Tuple of (position_monitor_thread, connection_monitor_thread)
    """
    global monitoring_active, position_lock, _monitor_thread, _connection_thread, logger
    
    # Initialize if needed
    if logger is None:
        setup_logging()
    
    monitoring_active = True
    position_lock = threading.Lock()
    
    # Create and start position monitor thread
    _monitor_thread = threading.Thread(target=monitor_open_positions, daemon=True, name="SL_PositionMonitor")
    _monitor_thread.start()
    
    # Create and start connection monitor thread
    _connection_thread = threading.Thread(target=monitor_mt5_connection, daemon=True, name="SL_ConnectionMonitor")
    _connection_thread.start()
    
    if logger:
        logger.info("✓ SL Protection monitoring threads started")
    
    send_telegram_alert("✓ <b>SL Protection Active</b>\n\nPosition and connection monitoring started")
    
    return (_monitor_thread, _connection_thread)


# ============================================================================
# FUNCTION 15: stop_monitoring
# ============================================================================

def stop_monitoring() -> None:
    """
    Stop all monitoring threads gracefully.
    """
    global monitoring_active, logger
    
    if logger:
        logger.info("Stopping SL Protection monitoring...")
    
    monitoring_active = False
    
    # Wait for threads to finish
    time.sleep(2)
    
    if logger:
        logger.info("SL Protection monitoring stopped")
    
    send_telegram_alert("⏸️ <b>SL Protection Stopped</b>\n\nMonitoring threads terminated")


# ============================================================================
# MODULE INITIALIZATION
# ============================================================================

# Auto-setup logging when module is imported
if logger is None:
    setup_logging()


# ============================================================================
# EXPORT ALL FUNCTIONS
# ============================================================================

__all__ = [
    # Configuration
    'set_telegram_config',
    'setup_logging',
    
    # Telegram
    'send_telegram_alert',
    
    # Trading Control
    'pause_trading',
    'is_trading_paused',
    
    # SL Tracking
    'track_original_sl',
    'get_original_sl',
    'verify_sl_after_entry',
    
    # Emergency Actions
    'emergency_close_position',
    'close_all_positions',
    'check_failed_close_reminders',
    'get_failed_closes',
    
    # Connection
    'reconnect_mt5',
    
    # Monitoring
    'start_monitoring',
    'stop_monitoring',
    
    # Constants
    'SL_VERIFICATION_DELAY',
    'SL_VERIFICATION_ATTEMPTS',
    'SL_TOLERANCE_POINTS',
    'EMERGENCY_LOSS_MULTIPLIER',
    'EMERGENCY_PAUSE_MINUTES',
    'FAILED_CLOSE_REMINDER_INTERVAL',
]
