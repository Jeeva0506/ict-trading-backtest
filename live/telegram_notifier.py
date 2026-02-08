"""
Telegram Notifier for ICT Trading Bot
======================================
Sends real-time trade alerts to Telegram

Setup:
1. Create a bot: Message @BotFather on Telegram, send /newbot
2. Copy the bot token
3. Get your chat ID: Message @userinfobot or @get_id_bot
4. Update config_fundednext.py with your credentials
"""
import requests
import logging
from datetime import datetime
from typing import Optional, Dict
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent))

logger = logging.getLogger(__name__)


class TelegramNotifier:
    """Send trading notifications via Telegram"""
    
    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.base_url = f"https://api.telegram.org/bot{bot_token}"
        self.enabled = bool(bot_token and chat_id)
        
        if not self.enabled:
            logger.warning("Telegram notifications disabled - missing token or chat_id")
    
    def send_message(self, message: str, parse_mode: str = "HTML") -> bool:
        """Send a message to Telegram"""
        if not self.enabled:
            return False
            
        try:
            url = f"{self.base_url}/sendMessage"
            payload = {
                "chat_id": self.chat_id,
                "text": message,
                "parse_mode": parse_mode,
                "disable_web_page_preview": True
            }
            
            response = requests.post(url, json=payload, timeout=10)
            
            if response.status_code == 200:
                return True
            else:
                logger.error(f"Telegram error: {response.text}")
                return False
                
        except Exception as e:
            logger.error(f"Failed to send Telegram message: {e}")
            return False
    
    # ==================== Trade Alerts ====================
    
    def trade_entry(self, symbol: str, direction: str, entry_price: float, 
                    sl: float, tp1: float, tp2: float, tp3: float,
                    lot_size: float, risk_amount: float):
        """Notify on trade entry"""
        emoji = "🟢" if direction.upper() == "BUY" else "🔴"
        
        message = f"""
{emoji} <b>NEW TRADE - {symbol}</b>

<b>Direction:</b> {direction.upper()}
<b>Entry:</b> {entry_price:.2f}
<b>Lot Size:</b> {lot_size}

<b>Targets:</b>
├─ TP1: {tp1:.2f}
├─ TP2: {tp2:.2f}
└─ TP3: {tp3:.2f}

<b>Stop Loss:</b> {sl:.2f}
<b>Risk:</b> ${risk_amount:.2f}

⏰ {datetime.now().strftime('%H:%M:%S IST')}
"""
        return self.send_message(message)
    
    def tp_hit(self, symbol: str, tp_level: int, price: float, 
               profit: float, remaining_lots: float):
        """Notify on take profit hit"""
        emoji = "🎯" if tp_level < 3 else "🏆"
        
        message = f"""
{emoji} <b>TP{tp_level} HIT - {symbol}</b>

<b>Price:</b> {price:.2f}
<b>Profit:</b> +${profit:.2f}
<b>Remaining:</b> {remaining_lots} lots

⏰ {datetime.now().strftime('%H:%M:%S IST')}
"""
        return self.send_message(message)
    
    def sl_hit(self, symbol: str, price: float, loss: float):
        """Notify on stop loss hit"""
        message = f"""
🛑 <b>STOP LOSS - {symbol}</b>

<b>Exit Price:</b> {price:.2f}
<b>Loss:</b> -${abs(loss):.2f}

⏰ {datetime.now().strftime('%H:%M:%S IST')}
"""
        return self.send_message(message)
    
    def trailing_stop_hit(self, symbol: str, price: float, profit: float):
        """Notify on trailing stop exit"""
        message = f"""
🔄 <b>TRAIL STOP - {symbol}</b>

<b>Exit Price:</b> {price:.2f}
<b>Profit:</b> ${profit:+.2f}

⏰ {datetime.now().strftime('%H:%M:%S IST')}
"""
        return self.send_message(message)
    
    def trade_closed(self, symbol: str, reason: str, profit: float):
        """Notify on any trade closure"""
        emoji = "✅" if profit >= 0 else "❌"
        
        message = f"""
{emoji} <b>TRADE CLOSED - {symbol}</b>

<b>Reason:</b> {reason}
<b>P&L:</b> ${profit:+.2f}

⏰ {datetime.now().strftime('%H:%M:%S IST')}
"""
        return self.send_message(message)
    
    # ==================== Status Updates ====================
    
    def daily_summary(self, date: str, trades: int, wins: int, 
                      gross_pnl: float, balance: float, drawdown: float):
        """Send daily trading summary"""
        win_rate = (wins / trades * 100) if trades > 0 else 0
        emoji = "📈" if gross_pnl >= 0 else "📉"
        
        message = f"""
{emoji} <b>DAILY SUMMARY - {date}</b>

<b>Trades:</b> {trades}
<b>Wins:</b> {wins} ({win_rate:.0f}%)
<b>P&L:</b> ${gross_pnl:+.2f}

<b>Balance:</b> ${balance:,.2f}
<b>Drawdown:</b> {drawdown:.2f}%

━━━━━━━━━━━━━━━━━━
"""
        return self.send_message(message)
    
    def bot_started(self, indices: list, balance: float):
        """Notify bot startup"""
        message = f"""
🚀 <b>TRADING BOT STARTED</b>

<b>Indices:</b> {', '.join(indices)}
<b>Balance:</b> ${balance:,.2f}
<b>Mode:</b> Live Trading

⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S IST')}
"""
        return self.send_message(message)
    
    def bot_stopped(self, reason: str, balance: float, daily_pnl: float):
        """Notify bot shutdown"""
        message = f"""
⏹️ <b>TRADING BOT STOPPED</b>

<b>Reason:</b> {reason}
<b>Balance:</b> ${balance:,.2f}
<b>Today's P&L:</b> ${daily_pnl:+.2f}

⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S IST')}
"""
        return self.send_message(message)
    
    def error_alert(self, error_type: str, details: str):
        """Send error notification"""
        message = f"""
⚠️ <b>ERROR ALERT</b>

<b>Type:</b> {error_type}
<b>Details:</b> {details}

⏰ {datetime.now().strftime('%H:%M:%S IST')}
"""
        return self.send_message(message)
    
    def drawdown_warning(self, current_dd: float, limit: float, balance: float):
        """Warn when approaching drawdown limit"""
        message = f"""
🚨 <b>DRAWDOWN WARNING</b>

<b>Current DD:</b> {current_dd:.2f}%
<b>Limit:</b> {limit:.2f}%
<b>Balance:</b> ${balance:,.2f}

⚠️ Approaching maximum drawdown!
"""
        return self.send_message(message)
    
    def market_open(self, symbol: str):
        """Notify when market opens"""
        message = f"""
🔔 <b>MARKET OPEN - {symbol}</b>

Looking for trade setups...

⏰ {datetime.now().strftime('%H:%M:%S IST')}
"""
        return self.send_message(message)
    
    # ==================== Connection & Environment Alerts ====================
    
    def connection_lost(self, connection_type: str, details: str = ""):
        """Alert when connection is lost"""
        message = f"""
🔴 <b>CONNECTION LOST</b>

<b>Type:</b> {connection_type}
<b>Details:</b> {details if details else 'Connection interrupted'}

⚠️ Trading paused until reconnection

⏰ {datetime.now().strftime('%H:%M:%S IST')}
"""
        return self.send_message(message)
    
    def connection_restored(self, connection_type: str, downtime_seconds: int = 0):
        """Alert when connection is restored"""
        downtime_str = f"{downtime_seconds}s" if downtime_seconds < 60 else f"{downtime_seconds//60}m {downtime_seconds%60}s"
        message = f"""
🟢 <b>CONNECTION RESTORED</b>

<b>Type:</b> {connection_type}
<b>Downtime:</b> {downtime_str}

✅ Trading resumed

⏰ {datetime.now().strftime('%H:%M:%S IST')}
"""
        return self.send_message(message)
    
    def reconnecting(self, connection_type: str, attempt: int, max_attempts: int):
        """Alert on reconnection attempt"""
        message = f"""
🔄 <b>RECONNECTING...</b>

<b>Type:</b> {connection_type}
<b>Attempt:</b> {attempt}/{max_attempts}

⏰ {datetime.now().strftime('%H:%M:%S IST')}
"""
        return self.send_message(message)
    
    def internet_down(self):
        """Alert when internet connection is down"""
        message = f"""
🌐 <b>INTERNET DISCONNECTED</b>

No internet connectivity detected.
Trading paused until connection restored.

⏰ {datetime.now().strftime('%H:%M:%S IST')}
"""
        return self.send_message(message)
    
    def mt5_disconnected(self, reason: str = ""):
        """Alert when MT5 connection is lost"""
        message = f"""
📉 <b>MT5 DISCONNECTED</b>

<b>Reason:</b> {reason if reason else 'Connection lost'}

⚠️ Cannot execute trades!

⏰ {datetime.now().strftime('%H:%M:%S IST')}
"""
        return self.send_message(message)
    
    def mt5_reconnected(self, account_balance: float):
        """Alert when MT5 connection is restored"""
        message = f"""
📈 <b>MT5 RECONNECTED</b>

<b>Balance:</b> ${account_balance:,.2f}
✅ Trading resumed

⏰ {datetime.now().strftime('%H:%M:%S IST')}
"""
        return self.send_message(message)
    
    def market_closed(self, symbol: str, next_open: str = ""):
        """Notify when market closes"""
        message = f"""
🔕 <b>MARKET CLOSED - {symbol}</b>

{f"Next open: {next_open}" if next_open else ""}

⏰ {datetime.now().strftime('%H:%M:%S IST')}
"""
        return self.send_message(message)
    
    def sl_moved(self, symbol: str, old_sl: float, new_sl: float, reason: str):
        """Notify when stop loss is moved"""
        message = f"""
🔄 <b>SL MODIFIED - {symbol}</b>

<b>Old SL:</b> {old_sl:.2f}
<b>New SL:</b> {new_sl:.2f}
<b>Reason:</b> {reason}

⏰ {datetime.now().strftime('%H:%M:%S IST')}
"""
        return self.send_message(message)
    
    def order_failed(self, symbol: str, order_type: str, reason: str):
        """Alert when order execution fails"""
        message = f"""
❌ <b>ORDER FAILED - {symbol}</b>

<b>Type:</b> {order_type}
<b>Reason:</b> {reason}

⏰ {datetime.now().strftime('%H:%M:%S IST')}
"""
        return self.send_message(message)
    
    def news_blocked(self, symbol: str, event_name: str, minutes_away: int):
        """Alert when trade blocked due to news"""
        message = f"""
📰 <b>TRADE BLOCKED - NEWS</b>

<b>Symbol:</b> {symbol}
<b>Event:</b> {event_name}
<b>Time:</b> In {minutes_away} minutes

Signal skipped due to high-impact news.

⏰ {datetime.now().strftime('%H:%M:%S IST')}
"""
        return self.send_message(message)
    
    def daily_limit_reached(self, trades_today: int, daily_pnl: float):
        """Alert when daily trade limit or loss limit reached"""
        message = f"""
🛑 <b>DAILY LIMIT REACHED</b>

<b>Trades Today:</b> {trades_today}
<b>Daily P&L:</b> ${daily_pnl:+,.2f}

No more trades until tomorrow.

⏰ {datetime.now().strftime('%H:%M:%S IST')}
"""
        return self.send_message(message)
    
    def system_status(self, balance: float, open_positions: int, 
                      daily_pnl: float, daily_dd: float, overall_dd: float,
                      active_markets: list):
        """Send periodic system status update"""
        markets_str = ", ".join(active_markets) if active_markets else "None"
        message = f"""
📊 <b>SYSTEM STATUS</b>

<b>💰 Balance:</b> ${balance:,.2f}
<b>📈 Today's P&L:</b> ${daily_pnl:+,.2f}
<b>📉 Daily DD:</b> {daily_dd:.2f}%
<b>📉 Overall DD:</b> {overall_dd:.2f}%
<b>📍 Open Positions:</b> {open_positions}
<b>🌍 Active Markets:</b> {markets_str}

⏰ {datetime.now().strftime('%H:%M:%S IST')}
"""
        return self.send_message(message)
    
    def critical_alert(self, title: str, message_text: str):
        """Send critical alert that requires immediate attention"""
        message = f"""
🚨🚨🚨 <b>CRITICAL ALERT</b> 🚨🚨🚨

<b>{title}</b>

{message_text}

⚠️ IMMEDIATE ATTENTION REQUIRED!

⏰ {datetime.now().strftime('%H:%M:%S IST')}
"""
        return self.send_message(message)
    
    # ==================== Test ====================
    
    def test_connection(self) -> bool:
        """Test Telegram connection"""
        message = f"""
✅ <b>CONNECTION TEST</b>

ICT Trading Bot connected successfully!

⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S IST')}
"""
        result = self.send_message(message)
        if result:
            logger.info("Telegram test message sent successfully")
        return result


# Test the notifier
if __name__ == "__main__":
    from config_fundednext import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
    
    notifier = TelegramNotifier(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
    
    if notifier.test_connection():
        print("✅ Telegram connected!")
    else:
        print("❌ Telegram connection failed")
        print("\nSetup instructions:")
        print("1. Message @BotFather on Telegram")
        print("2. Send /newbot and follow instructions")
        print("3. Copy the bot token")
        print("4. Message @userinfobot to get your chat ID")
        print("5. Add to config_fundednext.py:")
        print('   TELEGRAM_BOT_TOKEN = "your_bot_token"')
        print('   TELEGRAM_CHAT_ID = "your_chat_id"')
