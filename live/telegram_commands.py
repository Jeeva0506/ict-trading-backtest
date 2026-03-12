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
import os
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
    
    def __init__(self, bot_token: str, chat_id: str, authorized_users: list = None,
                 state_dir: Path = None, account_label: str = ""):
        """
        Initialize the command handler.
        
        Args:
            bot_token: Telegram bot token
            chat_id: Primary chat ID for notifications
            authorized_users: List of user IDs allowed to send commands (optional)
            state_dir: Directory for state file (default: live/logs/)
            account_label: Account label for multi-account identification
        """
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.authorized_users = authorized_users or []
        self.base_url = f"https://api.telegram.org/bot{bot_token}"
        self.enabled = bool(bot_token and chat_id)
        self.account_label = account_label
        
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
        self.on_trades: Optional[Callable[[], Dict]] = None
        self.on_risk: Optional[Callable[[], Dict]] = None
        self.on_performance: Optional[Callable[[], Dict]] = None
        self.on_emergency: Optional[Callable[[], Dict]] = None
        
        # Bot state tracking
        self.bot_state = {
            'is_running': False,
            'is_paused': False,
            'started_at': None,
            'last_command': None,
            'last_command_by': None
        }
        
        # State file for persistence (namespaced per account)
        if state_dir:
            self.state_file = state_dir / 'telegram_state.json'
        else:
            self.state_file = Path(__file__).parent / 'logs' / 'telegram_state.json'
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
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
        
        elif command in ['/trades', '/trades@bot']:
            return self._handle_trades()
        
        elif command in ['/risk', '/risk@bot']:
            return self._handle_risk()
        
        elif command in ['/performance', '/performance@bot', '/perf', '/perf@bot']:
            return self._handle_performance()
        
        elif command in ['/emergency', '/emergency@bot']:
            return self._handle_emergency(first_name)
        
        elif command in ['/uptime', '/uptime@bot']:
            return self._handle_uptime()
        
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
                if isinstance(status_data, dict) and 'error' not in status_data:
                    balance = status_data.get('balance', 0)
                    equity = status_data.get('equity', 0)
                    daily_pnl = status_data.get('daily_pnl', 0)
                    open_pos = status_data.get('open_positions', 0)
                    daily_dd = status_data.get('daily_dd', 0)
                    overall_dd = status_data.get('overall_dd', 0)
                    active = status_data.get('active_markets', [])
                    trades_today = status_data.get('trades_today', 0)
                    tracked = status_data.get('tracked', 0)
                    paused = status_data.get('paused', False)
                    
                    # DD warning indicators
                    dd_warn = ""
                    if overall_dd >= 6.0:
                        dd_warn = " ⚠️ DANGER"
                    elif overall_dd >= 4.0:
                        dd_warn = " ⚠️"
                    
                    daily_dd_warn = ""
                    if daily_dd >= 2.0:
                        daily_dd_warn = " ⚠️ DANGER"
                    elif daily_dd >= 1.5:
                        daily_dd_warn = " ⚠️"
                    
                    extra_status = f"""
<b>💰 Balance:</b> ${balance:,.2f}
<b>💎 Equity:</b> ${equity:,.2f}
<b>📈 Today's P&L:</b> ${daily_pnl:+,.2f}
<b>📉 Daily DD:</b> {daily_dd:.2f}%{daily_dd_warn}
<b>📉 Overall DD:</b> {overall_dd:.2f}%{dd_warn}
<b>📍 Open Positions:</b> {open_pos} (tracked: {tracked})
<b>📊 Trades Today:</b> {trades_today}
<b>🌍 Active Markets:</b> {', '.join(active) if active else 'None (markets closed)'}
"""
                    if paused:
                        extra_status += "\n⏸️ <b>Trading is PAUSED</b>\n"
                elif isinstance(status_data, dict) and 'error' in status_data:
                    extra_status = f"\n⚠️ {status_data['error']}\n"
            except Exception as e:
                logger.error(f"Failed to get status: {e}")
                extra_status = f"\n⚠️ Error getting live data: {e}\n"
        
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
                
                msg = f"<b>📊 OPEN POSITIONS ({len(positions)})</b>\n"
                total_pnl = 0
                
                for pos in positions:
                    symbol = pos.get('symbol', 'Unknown')
                    direction = pos.get('type', 'Unknown')
                    lots = pos.get('volume', 0)
                    profit = pos.get('profit', 0)
                    entry = pos.get('price_open', 0)
                    sl = pos.get('sl', 0)
                    tp = pos.get('tp', 0)
                    ticket = pos.get('ticket', 0)
                    hit_be = pos.get('hit_be', False)
                    total_pnl += profit
                    
                    emoji = "🟢" if profit >= 0 else "🔴"
                    be_tag = " [BE]" if hit_be else ""
                    
                    msg += f"""
{emoji} <b>{symbol}</b> — {direction} {lots} lots{be_tag}
   Entry: {entry:.2f} | SL: {sl:.2f} | TP: {tp:.2f}
   P&L: <b>${profit:+.2f}</b> | #{ticket}
"""
                
                pnl_emoji = "📈" if total_pnl >= 0 else "📉"
                msg += f"\n{pnl_emoji} <b>Total Floating:</b> ${total_pnl:+,.2f}"
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
                
                if 'error' in data:
                    return f"❌ {data['error']}"
                
                balance = data.get('balance', 0)
                equity = data.get('equity', 0)
                margin = data.get('margin', 0)
                free_margin = data.get('free_margin', 0)
                profit = data.get('profit', 0)
                
                # Margin usage percentage
                margin_pct = (margin / equity * 100) if equity > 0 else 0
                
                return f"""
💰 <b>ACCOUNT BALANCE</b>

<b>Balance:</b> ${balance:,.2f}
<b>Equity:</b> ${equity:,.2f}
<b>Margin Used:</b> ${margin:,.2f} ({margin_pct:.1f}%)
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

<b>━━ Monitoring ━━</b>
/status — Bot status, balance, DD, markets
/positions — Open positions with P&L
/balance — Account balance & equity
/trades — Today's completed trades
/risk — Drawdown & risk levels
/performance — Weekly performance summary
/uptime — Bot uptime info

<b>━━ Control ━━</b>
/pause — Pause trading (keeps bot running)
/resume — Resume trading
/emergency — ⚠️ CLOSE ALL positions immediately

<b>━━ Info ━━</b>
/help — Show this help message

<i>Bot start/stop handled by Windows Task Scheduler</i>

━━━━━━━━━━━━━━━━━━
<i>ORB Trading Bot v2.1</i>
"""
    
    def _handle_trades(self) -> str:
        """Handle /trades command - show today's completed trades"""
        if self.on_trades:
            try:
                data = self.on_trades()
                if not data or data.get('trades', 0) == 0:
                    return f"📭 <b>No trades today</b>\n\n⏰ {ist_time()}"
                
                trades = data.get('trades', 0)
                wins = data.get('wins', 0)
                losses = data.get('losses', 0)
                win_rate = data.get('win_rate', 0)
                pnl = data.get('gross_pnl', 0)
                best = data.get('best_trade', 0)
                worst = data.get('worst_trade', 0)
                details = data.get('trade_details', [])
                
                pnl_emoji = "📈" if pnl >= 0 else "📉"
                
                msg = f"""
{pnl_emoji} <b>TODAY'S TRADES</b>

<b>Trades:</b> {trades} ({wins}W / {losses}L)
<b>Win Rate:</b> {win_rate:.0f}%
<b>Total P&L:</b> ${pnl:+,.2f}
<b>Best:</b> ${best:+,.2f}
<b>Worst:</b> ${worst:+,.2f}
"""
                # Show individual trades
                if details:
                    msg += "\n<b>Details:</b>\n"
                    for t in details:
                        icon = "✅" if t.get('pnl', 0) > 0 else "❌"
                        msg += f"  {icon} {t.get('symbol', '?')} {t.get('direction', '?')} → ${t.get('pnl', 0):+,.2f} ({t.get('exit_reason', '?')})\n"
                
                msg += f"\n⏰ {ist_time()}"
                return msg
            except Exception as e:
                logger.error(f"Failed to get trades: {e}")
                return f"❌ Failed to get trades: {e}"
        return "⚠️ Trades callback not configured."
    
    def _handle_risk(self) -> str:
        """Handle /risk command - show drawdown and risk info"""
        if self.on_risk:
            try:
                data = self.on_risk()
                if 'error' in data:
                    return f"❌ {data['error']}"
                
                balance = data.get('balance', 0)
                initial = data.get('initial_balance', 0)
                daily_dd = data.get('daily_dd', 0)
                overall_dd = data.get('overall_dd', 0)
                daily_limit = data.get('daily_dd_limit', 3.0)
                overall_limit = data.get('overall_dd_limit', 8.0)
                can_trade = data.get('can_trade', True)
                risk_pct = data.get('risk_percent', 0)
                risk_dollars = data.get('risk_dollars', 0)
                daily_pnl = data.get('daily_pnl', 0)
                daily_trades = data.get('daily_trades', 0)
                paused = data.get('paused', False)
                
                # Progress bars
                daily_bar = self._progress_bar(daily_dd, daily_limit)
                overall_bar = self._progress_bar(overall_dd, overall_limit)
                
                status = "🟢 CAN TRADE" if can_trade and not paused else "🔴 BLOCKED"
                if paused:
                    status = "⏸️ PAUSED"
                
                return f"""
🛡️ <b>RISK & DRAWDOWN</b>

<b>Status:</b> {status}

<b>📉 Daily DD:</b> {daily_dd:.2f}% / {daily_limit:.1f}%
{daily_bar}

<b>📉 Overall DD:</b> {overall_dd:.2f}% / {overall_limit:.1f}%
{overall_bar}

<b>💰 Balance:</b> ${balance:,.2f}
<b>🏦 Initial:</b> ${initial:,.2f}
<b>📈 Today P&L:</b> ${daily_pnl:+,.2f}
<b>📊 Trades Today:</b> {daily_trades}

<b>⚡ Risk Per Trade:</b> {risk_pct:.1f}% = ${risk_dollars:,.2f}

⏰ {ist_time()}
"""
            except Exception as e:
                logger.error(f"Failed to get risk: {e}")
                return f"❌ Failed to get risk info: {e}"
        return "⚠️ Risk callback not configured."
    
    def _handle_performance(self) -> str:
        """Handle /performance command - weekly summary"""
        if self.on_performance:
            try:
                data = self.on_performance()
                if 'error' in data:
                    return f"❌ {data['error']}"
                
                daily = data.get('daily', {})
                weekly = data.get('weekly', {})
                balance = data.get('balance', 0)
                initial = data.get('initial_balance', 0)
                total_return = data.get('total_return_pct', 0)
                
                d_trades = daily.get('trades', 0)
                d_pnl = daily.get('gross_pnl', 0)
                d_wr = daily.get('win_rate', 0)
                
                w_trades = weekly.get('trades', 0)
                w_pnl = weekly.get('gross_pnl', 0)
                w_wr = weekly.get('win_rate', 0)
                w_avg = weekly.get('avg_trade', 0)
                w_start = weekly.get('week_start', '?')
                w_end = weekly.get('week_end', '?')
                
                return_emoji = "📈" if total_return >= 0 else "📉"
                
                return f"""
📊 <b>PERFORMANCE SUMMARY</b>

<b>━━ Today ━━</b>
Trades: {d_trades} | WR: {d_wr:.0f}% | P&L: ${d_pnl:+,.2f}

<b>━━ This Week ({w_start} to {w_end}) ━━</b>
Trades: {w_trades} | WR: {w_wr:.0f}% | P&L: ${w_pnl:+,.2f}
Avg Trade: ${w_avg:+,.2f}

<b>━━ Overall ━━</b>
{return_emoji} Balance: ${balance:,.2f}
Initial: ${initial:,.2f}
Return: {total_return:+.2f}%

⏰ {ist_time()}
"""
            except Exception as e:
                logger.error(f"Failed to get performance: {e}")
                return f"❌ Failed to get performance: {e}"
        return "⚠️ Performance callback not configured."
    
    def _handle_emergency(self, user_name: str) -> str:
        """Handle /emergency command - close all positions"""
        if self.on_emergency:
            try:
                result = self.on_emergency()
                if 'error' in result:
                    return f"❌ Emergency close failed: {result['error']}"
                
                closed = result.get('closed', 0)
                failed = result.get('failed', 0)
                total = result.get('total', 0)
                
                if total == 0:
                    return "📭 No open positions to close."
                
                return f"""
🚨 <b>EMERGENCY CLOSE EXECUTED</b>

<b>By:</b> {user_name}
<b>Closed:</b> {closed}/{total}
<b>Failed:</b> {failed}

⏸️ Trading has been PAUSED.
Use /resume to continue trading.

⏰ {ist_time()}
"""
            except Exception as e:
                logger.error(f"Emergency close failed: {e}")
                return f"❌ Emergency close failed: {e}"
        return "⚠️ Emergency callback not configured."
    
    def _handle_uptime(self) -> str:
        """Handle /uptime command"""
        started_at = self.bot_state.get('started_at', None)
        if started_at:
            try:
                # Parse the stored start time
                start_dt = datetime.strptime(started_at, '%Y-%m-%d %H:%M:%S IST')
                now = get_ist_now().replace(tzinfo=None)
                uptime = now - start_dt
                
                hours, remainder = divmod(int(uptime.total_seconds()), 3600)
                minutes, seconds = divmod(remainder, 60)
                
                return f"""
⏱️ <b>UPTIME</b>

<b>Started:</b> {started_at}
<b>Uptime:</b> {hours}h {minutes}m {seconds}s
<b>Status:</b> {'🟢 Running' if self.bot_state['is_running'] else '🔴 Stopped'}

⏰ {ist_time()}
"""
            except Exception:
                pass
        
        return f"""
⏱️ <b>UPTIME</b>

<b>Started:</b> {started_at or 'Unknown'}
<b>Status:</b> {'🟢 Running' if self.bot_state['is_running'] else '🔴 Stopped'}

⏰ {ist_time()}
"""
    
    @staticmethod
    def _progress_bar(current: float, limit: float, length: int = 10) -> str:
        """Create a text progress bar for DD visualization"""
        if limit <= 0:
            return ""
        ratio = min(current / limit, 1.0)
        filled = int(ratio * length)
        empty = length - filled
        
        if ratio >= 0.8:
            bar_char = "🟥"
        elif ratio >= 0.5:
            bar_char = "🟨"
        else:
            bar_char = "🟩"
        
        return bar_char * filled + "⬜" * empty + f" ({ratio*100:.0f}%)"
    
    def start_polling(self):
        """Start the polling thread to listen for commands.
        
        Uses a file-based lock to ensure only ONE process polls the bot token
        at a time. When two accounts share the same bot token, only the first
        process to acquire the lock will poll. The other process becomes
        notification-only (can still SEND messages, just won't process commands).
        """
        if not self.enabled:
            logger.warning("Cannot start polling - Telegram not configured")
            return
        
        if self.running:
            logger.warning("Polling already running")
            return
        
        # ── Multi-instance polling lock ──────────────────────────────
        # Prevent two processes from consuming the same getUpdates feed.
        # Lock file lives in a SHARED location (not per-account).
        self._poll_lock_file = Path(__file__).parent / 'logs' / 'telegram_poll.lock'
        self._poll_lock_file.parent.mkdir(parents=True, exist_ok=True)
        self._owns_poll_lock = False
        
        try:
            if self._poll_lock_file.exists():
                import psutil
                try:
                    old_pid = int(self._poll_lock_file.read_text().strip())
                    if psutil.pid_exists(old_pid):
                        proc = psutil.Process(old_pid)
                        if 'python' in proc.name().lower():
                            logger.info(f"Telegram polling lock held by PID {old_pid} — "
                                        f"this instance is NOTIFICATION-ONLY (no commands)")
                            return  # Another process is polling — skip
                except (ValueError, psutil.NoSuchProcess, psutil.AccessDenied):
                    pass  # Stale lock — overwrite
            
            # Acquire lock
            self._poll_lock_file.write_text(str(os.getpid()))
            self._owns_poll_lock = True
            logger.info(f"Telegram polling lock acquired (PID {os.getpid()})")
        except Exception as e:
            logger.warning(f"Could not check/acquire polling lock: {e}")
            # If we can't check, proceed with polling (single-instance fallback)
            self._owns_poll_lock = True
        
        self.running = True
        self.polling_thread = threading.Thread(target=self._poll_loop, daemon=True)
        self.polling_thread.start()
        logger.info("Telegram command polling started")
    
    def stop_polling(self):
        """Stop the polling thread and release lock"""
        self.running = False
        if self.polling_thread:
            self.polling_thread.join(timeout=5)
        # Release polling lock
        if getattr(self, '_owns_poll_lock', False):
            try:
                lock = getattr(self, '_poll_lock_file', None)
                if lock and lock.exists():
                    lock.unlink(missing_ok=True)
                    logger.info("Telegram polling lock released")
            except Exception:
                pass
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
