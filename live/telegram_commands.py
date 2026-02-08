"""
Telegram Command Handler for ICT Trading Bot
=============================================
Allows remote control of the trading bot via Telegram commands.

Commands:
  /start - Start the trading bot
  /stop - Stop the trading bot  
  /status - Get current bot status
  /positions - Show open positions
  /balance - Show account balance
  /pause - Pause trading (keeps bot running)
  /resume - Resume trading
  /help - Show available commands
"""
import requests
import logging
import threading
import time
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional, Dict, Any

logger = logging.getLogger(__name__)

# IST Timezone
IST = timezone(timedelta(hours=5, minutes=30))

def get_ist_now() -> datetime:
    return datetime.now(IST)

def ist_time(fmt: str = '%Y-%m-%d %H:%M:%S IST') -> str:
    return get_ist_now().strftime(fmt)


class TelegramCommandHandler:
    """
    Handles incoming Telegram commands to control the trading bot.
    Uses long-polling to receive updates from Telegram.
    """
    
    def __init__(self, bot_token: str, chat_id: str, authorized_users: list = None):
        """
        Initialize the command handler.
        
        Args:
            bot_token: Telegram bot token
            chat_id: Primary chat ID for notifications
            authorized_users: List of user IDs allowed to send commands (optional)
        """
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.authorized_users = authorized_users or []
        self.base_url = f"https://api.telegram.org/bot{bot_token}"
        self.enabled = bool(bot_token and chat_id)
        
        # State
        self.running = False
        self.polling_thread = None
        self.last_update_id = 0
        
        # Callbacks for bot control
        self.on_start: Optional[Callable] = None
        self.on_stop: Optional[Callable] = None
        self.on_pause: Optional[Callable] = None
        self.on_resume: Optional[Callable] = None
        self.on_status: Optional[Callable[[], Dict]] = None
        self.on_positions: Optional[Callable[[], list]] = None
        self.on_balance: Optional[Callable[[], Dict]] = None
        
        # Bot state tracking
        self.bot_state = {
            'is_running': False,
            'is_paused': False,
            'started_at': None,
            'last_command': None,
            'last_command_by': None
        }
        
        # State file for persistence
        self.state_file = Path(__file__).parent / 'logs' / 'telegram_state.json'
        self._load_state()
        
        if not self.enabled:
            logger.warning("Telegram command handler disabled - missing credentials")
    
    def _load_state(self):
        """Load last update ID from file to avoid processing old messages"""
        try:
            if self.state_file.exists():
                with open(self.state_file, 'r') as f:
                    state = json.load(f)
                    self.last_update_id = state.get('last_update_id', 0)
                    logger.info(f"Loaded last update ID: {self.last_update_id}")
        except Exception as e:
            logger.error(f"Failed to load state: {e}")
    
    def _save_state(self):
        """Save state to file"""
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.state_file, 'w') as f:
                json.dump({
                    'last_update_id': self.last_update_id,
                    'bot_state': self.bot_state
                }, f)
        except Exception as e:
            logger.error(f"Failed to save state: {e}")
    
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
            return response.status_code == 200
        except Exception as e:
            logger.error(f"Failed to send message: {e}")
            return False
    
    def get_updates(self, timeout: int = 30) -> list:
        """Get updates from Telegram using long-polling"""
        try:
            url = f"{self.base_url}/getUpdates"
            params = {
                "offset": self.last_update_id + 1,
                "timeout": timeout,
                "allowed_updates": ["message"]
            }
            response = requests.get(url, params=params, timeout=timeout + 10)
            
            if response.status_code == 200:
                data = response.json()
                if data.get('ok'):
                    return data.get('result', [])
            return []
        except requests.exceptions.Timeout:
            return []
        except Exception as e:
            logger.error(f"Failed to get updates: {e}")
            return []
    
    def is_authorized(self, user_id: int, username: str = None) -> bool:
        """Check if user is authorized to send commands"""
        # If no authorized users specified, allow chat_id owner
        if not self.authorized_users:
            return True
        return user_id in self.authorized_users or str(user_id) in [str(u) for u in self.authorized_users]
    
    def process_command(self, message: dict) -> str:
        """Process a command message and return response"""
        text = message.get('text', '').strip()
        user = message.get('from', {})
        user_id = user.get('id')
        username = user.get('username', 'Unknown')
        first_name = user.get('first_name', 'User')
        
        # Check authorization
        if not self.is_authorized(user_id, username):
            logger.warning(f"Unauthorized command from {username} ({user_id}): {text}")
            return f"⛔ Unauthorized. Your user ID ({user_id}) is not in the allowed list."
        
        # Parse command
        command = text.split()[0].lower() if text else ''
        args = text.split()[1:] if len(text.split()) > 1 else []
        
        # Track command
        self.bot_state['last_command'] = command
        self.bot_state['last_command_by'] = f"{first_name} (@{username})"
        
        logger.info(f"Command from {first_name} (@{username}): {command}")
        
        # Handle commands
        if command in ['/pause', '/pause@bot']:
            return self._handle_pause(first_name)
        
        elif command in ['/resume', '/resume@bot']:
            return self._handle_resume(first_name)
        
        elif command in ['/status', '/status@bot']:
            return self._handle_status()
        
        elif command in ['/positions', '/positions@bot']:
            return self._handle_positions()
        
        elif command in ['/balance', '/balance@bot']:
            return self._handle_balance()
        
        elif command in ['/help', '/help@bot', '/start', '/start@bot']:
            return self._handle_help()
        
        elif command.startswith('/'):
            return f"❓ Unknown command: {command}\n\nType /help for available commands."
        
        return None  # Not a command
    
    def _handle_pause(self, user_name: str) -> str:
        """Handle /pause command"""
        if not self.bot_state['is_running']:
            return "⚠️ Bot is not running!"
        
        if self.bot_state['is_paused']:
            return "⚠️ Trading is already paused!"
        
        if self.on_pause:
            try:
                self.on_pause()
                self.bot_state['is_paused'] = True
                self._save_state()
                
                return f"""
⏸️ <b>TRADING PAUSED</b>

<b>Paused by:</b> {user_name}
<b>Time:</b> {ist_time()}

Bot is running but will NOT open new trades.
Existing positions will still be managed.

Use /resume to continue trading.
"""
            except Exception as e:
                logger.error(f"Failed to pause: {e}")
                return f"❌ Failed to pause: {e}"
        else:
            self.bot_state['is_paused'] = True
            self._save_state()
            return "⏸️ Trading paused (no callback configured)"
    
    def _handle_resume(self, user_name: str) -> str:
        """Handle /resume command"""
        if not self.bot_state['is_running']:
            return "⚠️ Bot is not running! Use /start first."
        
        if not self.bot_state['is_paused']:
            return "⚠️ Trading is not paused!"
        
        if self.on_resume:
            try:
                self.on_resume()
                self.bot_state['is_paused'] = False
                self._save_state()
                
                return f"""
▶️ <b>TRADING RESUMED</b>

<b>Resumed by:</b> {user_name}
<b>Time:</b> {ist_time()}

✅ Bot will now look for new trade setups.
"""
            except Exception as e:
                logger.error(f"Failed to resume: {e}")
                return f"❌ Failed to resume: {e}"
        else:
            self.bot_state['is_paused'] = False
            self._save_state()
            return "▶️ Trading resumed (no callback configured)"
    
    def _handle_status(self) -> str:
        """Handle /status command"""
        status_emoji = "🟢" if self.bot_state['is_running'] else "🔴"
        status_text = "Running" if self.bot_state['is_running'] else "Stopped"
        
        if self.bot_state['is_paused']:
            status_emoji = "⏸️"
            status_text = "Paused"
        
        # Get additional status from callback
        extra_status = ""
        if self.on_status:
            try:
                status_data = self.on_status()
                if status_data:
                    balance = status_data.get('balance', 0)
                    daily_pnl = status_data.get('daily_pnl', 0)
                    open_pos = status_data.get('open_positions', 0)
                    daily_dd = status_data.get('daily_dd', 0)
                    overall_dd = status_data.get('overall_dd', 0)
                    active = status_data.get('active_markets', [])
                    
                    extra_status = f"""
<b>💰 Balance:</b> ${balance:,.2f}
<b>📈 Today's P&L:</b> ${daily_pnl:+,.2f}
<b>📉 Daily DD:</b> {daily_dd:.2f}%
<b>📉 Overall DD:</b> {overall_dd:.2f}%
<b>📍 Open Positions:</b> {open_pos}
<b>🌍 Active Markets:</b> {', '.join(active) if active else 'None'}
"""
            except Exception as e:
                logger.error(f"Failed to get status: {e}")
        
        started_at = self.bot_state.get('started_at', 'N/A')
        last_cmd = self.bot_state.get('last_command', 'None')
        last_by = self.bot_state.get('last_command_by', 'N/A')
        
        return f"""
{status_emoji} <b>BOT STATUS: {status_text.upper()}</b>

<b>Started:</b> {started_at}
<b>Last Command:</b> {last_cmd}
<b>By:</b> {last_by}
{extra_status}
⏰ {ist_time()}
"""
    
    def _handle_positions(self) -> str:
        """Handle /positions command"""
        if self.on_positions:
            try:
                positions = self.on_positions()
                
                if not positions:
                    return "📭 No open positions."
                
                msg = "<b>📊 OPEN POSITIONS</b>\n\n"
                
                for pos in positions:
                    symbol = pos.get('symbol', 'Unknown')
                    direction = pos.get('type', 'Unknown')
                    lots = pos.get('volume', 0)
                    profit = pos.get('profit', 0)
                    entry = pos.get('price_open', 0)
                    current = pos.get('price_current', 0)
                    
                    emoji = "🟢" if profit >= 0 else "🔴"
                    
                    msg += f"""
{emoji} <b>{symbol}</b>
   {direction} {lots} lots @ {entry:.2f}
   Current: {current:.2f}
   P&L: ${profit:+.2f}
"""
                
                msg += f"\n⏰ {ist_time()}"
                return msg
                
            except Exception as e:
                logger.error(f"Failed to get positions: {e}")
                return f"❌ Failed to get positions: {e}"
        else:
            return "⚠️ Positions callback not configured."
    
    def _handle_balance(self) -> str:
        """Handle /balance command"""
        if self.on_balance:
            try:
                data = self.on_balance()
                
                balance = data.get('balance', 0)
                equity = data.get('equity', 0)
                margin = data.get('margin', 0)
                free_margin = data.get('free_margin', 0)
                profit = data.get('profit', 0)
                
                return f"""
💰 <b>ACCOUNT BALANCE</b>

<b>Balance:</b> ${balance:,.2f}
<b>Equity:</b> ${equity:,.2f}
<b>Margin Used:</b> ${margin:,.2f}
<b>Free Margin:</b> ${free_margin:,.2f}
<b>Floating P&L:</b> ${profit:+,.2f}

⏰ {ist_time()}
"""
            except Exception as e:
                logger.error(f"Failed to get balance: {e}")
                return f"❌ Failed to get balance: {e}"
        else:
            return "⚠️ Balance callback not configured."
    
    def _handle_help(self) -> str:
        """Handle /help command"""
        return """
📖 <b>AVAILABLE COMMANDS</b>

<b>Trading Control:</b>
/pause - Pause trading (bot keeps running)
/resume - Resume trading

<b>Information:</b>
/status - Get bot status
/positions - Show open positions
/balance - Show account balance
/help - Show this help message

<i>Bot start/stop is handled by Windows Task Scheduler</i>

━━━━━━━━━━━━━━━━━━
<i>ICT Trading Bot v1.0</i>
"""
    
    def start_polling(self):
        """Start the polling thread to listen for commands"""
        if not self.enabled:
            logger.warning("Cannot start polling - Telegram not configured")
            return
        
        if self.running:
            logger.warning("Polling already running")
            return
        
        self.running = True
        self.polling_thread = threading.Thread(target=self._poll_loop, daemon=True)
        self.polling_thread.start()
        logger.info("Telegram command polling started")
    
    def stop_polling(self):
        """Stop the polling thread"""
        self.running = False
        if self.polling_thread:
            self.polling_thread.join(timeout=5)
        logger.info("Telegram command polling stopped")
    
    def _poll_loop(self):
        """Main polling loop"""
        # Mark old messages as read on startup
        self._skip_old_messages()
        
        while self.running:
            try:
                updates = self.get_updates(timeout=30)
                
                for update in updates:
                    update_id = update.get('update_id', 0)
                    
                    # Update last processed ID
                    if update_id > self.last_update_id:
                        self.last_update_id = update_id
                        self._save_state()
                    
                    # Process message
                    message = update.get('message', {})
                    if message and 'text' in message:
                        response = self.process_command(message)
                        if response:
                            self.send_message(response)
                
            except Exception as e:
                logger.error(f"Polling error: {e}")
                time.sleep(5)
    
    def _skip_old_messages(self):
        """Skip messages that were sent while bot was offline"""
        try:
            # Get all pending updates
            url = f"{self.base_url}/getUpdates"
            response = requests.get(url, timeout=10)
            
            if response.status_code == 200:
                data = response.json()
                if data.get('ok'):
                    updates = data.get('result', [])
                    if updates:
                        # Set last_update_id to latest, effectively skipping all old messages
                        self.last_update_id = updates[-1].get('update_id', 0)
                        self._save_state()
                        logger.info(f"Skipped {len(updates)} old messages")
        except Exception as e:
            logger.error(f"Failed to skip old messages: {e}")


# Test the command handler
if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).parent))
    
    from config_fundednext import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
    
    handler = TelegramCommandHandler(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
    
    # Set up test callbacks
    def test_start():
        print("Bot started!")
    
    def test_stop():
        print("Bot stopped!")
    
    def test_status():
        return {
            'balance': 100000,
            'daily_pnl': 500,
            'open_positions': 1,
            'daily_dd': 0.5,
            'overall_dd': 1.2,
            'active_markets': ['SPX500', 'GER30']
        }
    
    handler.on_start = test_start
    handler.on_stop = test_stop
    handler.on_status = test_status
    
    print("Starting Telegram command handler...")
    print("Send commands to your bot in Telegram!")
    print("Press Ctrl+C to stop")
    
    handler.start_polling()
    
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping...")
        handler.stop_polling()
