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
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import socket
import struct
import random
import threading
import json
import time

_TELEGRAM_FALLBACK_IPS = ['149.154.167.220', '149.154.166.110']

def _dns_query_cloudflare(hostname: str, timeout: float = 3.0) -> list:
    """Query Cloudflare (1.1.1.1) and Google (8.8.8.8) DNS directly via UDP, return list of A record IPs."""
    for dns_server in ('1.1.1.1', '8.8.8.8'):
        try:
            tx_id = random.randint(1, 65535)
            parts = hostname.encode().split(b'.')
            qname = b''.join(bytes([len(p)]) + p for p in parts) + b'\x00'
            packet = struct.pack('>HHHHHH', tx_id, 0x0100, 1, 0, 0, 0) + qname + struct.pack('>HH', 1, 1)
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(timeout)
            sock.sendto(packet, (dns_server, 53))
            resp, _ = sock.recvfrom(512)
            sock.close()
            # Parse answer section
            ancount = struct.unpack('>H', resp[6:8])[0]
            off = 12
            # skip question name
            while off < len(resp):
                if resp[off] == 0:
                    off += 1
                    break
                if resp[off] >= 192:
                    off += 2
                    break
                off += resp[off] + 1
            off += 4  # skip QTYPE+QCLASS
            ips = []
            for _ in range(ancount):
                if off + 2 > len(resp):
                    break
                if resp[off] >= 192:
                    off += 2
                else:
                    while off < len(resp) and resp[off] != 0:
                        off += resp[off] + 1
                    off += 1
                if off + 10 > len(resp):
                    break
                rtype, _rc, _ttl, rdlen = struct.unpack('>HHIH', resp[off:off + 10])
                off += 10
                if rtype == 1 and rdlen == 4:
                    ips.append('.'.join(str(b) for b in resp[off:off + 4]))
                off += rdlen
            if ips:
                return ips
        except Exception:
            continue
    return []


def _override_dns():
    """Patch socket.getaddrinfo to always use Cloudflare DNS for Telegram (ISP DNS returns stale IPs)."""
    _orig = socket.getaddrinfo

    def _patched(host, port, family=0, type=0, proto=0, flags=0):
        # Always bypass ISP DNS for Telegram — ISP may return wrong/stale IPs
        # Fallback IPs first (known-working), then Cloudflare result appended
        if isinstance(host, str) and 'telegram' in host:
            cf_ips = _dns_query_cloudflare(host)
            all_ips = cf_ips + [ip for ip in _TELEGRAM_FALLBACK_IPS if ip not in cf_ips]
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (ip, port or 443)) for ip in all_ips]
        return _orig(host, port, family, type, proto, flags)

    socket.getaddrinfo = _patched

_override_dns()

sys.path.insert(0, str(Path(__file__).parent.parent))

logger = logging.getLogger(__name__)

# IST Timezone (UTC+5:30)
IST = timezone(timedelta(hours=5, minutes=30))


def get_ist_now() -> datetime:
    """Get current time in IST"""
    return datetime.now(IST)


def ist_time(fmt: str = "%H:%M:%S IST") -> str:
    """Get formatted IST time string"""
    return get_ist_now().strftime(fmt)


class TelegramNotifier:
    """Send trading notifications via Telegram"""

    _MAX_QUEUE = 50  # max pending messages stored on disk

    def __init__(self, bot_token: str, chat_id: str, account_label: str = ""):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.base_url = f"https://api.telegram.org/bot{bot_token}"
        self.enabled = bool(bot_token and chat_id)
        self.account_label = account_label  # e.g. "STELLAR-2"
        self._queue_file = Path(__file__).parent / 'logs' / f'pending_msgs_{account_label or "default"}.json'
        self._queue_lock = threading.Lock()
        self._start_flush_thread()

        if not self.enabled:
            logger.warning("Telegram notifications disabled - missing token or chat_id")

    def _start_flush_thread(self):
        """Background thread: retry pending messages every 5 minutes."""
        def _flush_loop():
            while True:
                time.sleep(300)
                try:
                    self._flush_pending()
                except Exception:
                    pass
        t = threading.Thread(target=_flush_loop, daemon=True)
        t.start()

    def _queue_message(self, text: str):
        """Persist a failed message to disk for later retry (non-blocking)."""
        if not self._queue_lock.acquire(blocking=False):
            return  # flush is running — skip to avoid deadlock
        try:
            try:
                msgs = json.loads(self._queue_file.read_text()) if self._queue_file.exists() else []
            except Exception:
                msgs = []
            msgs.append({'text': text, 'ts': get_ist_now().isoformat()})
            if len(msgs) > self._MAX_QUEUE:
                msgs = msgs[-self._MAX_QUEUE:]
            try:
                self._queue_file.parent.mkdir(parents=True, exist_ok=True)
                self._queue_file.write_text(json.dumps(msgs))
            except Exception:
                pass
        finally:
            self._queue_lock.release()

    def _flush_pending(self):
        """Try to send any queued messages. Holds lock only for file I/O, not network sends."""
        if not self._queue_file.exists():
            return
        # Read and clear queue under lock, then send without lock
        with self._queue_lock:
            try:
                msgs = json.loads(self._queue_file.read_text())
            except Exception:
                return
            if not msgs:
                return
            self._queue_file.write_text('[]')  # clear before sending
        # Send outside the lock so _queue_message never deadlocks
        sent, remaining = 0, []
        for m in msgs:
            if self._raw_send(m['text']):
                sent += 1
            else:
                remaining.append(m)
        # Re-queue any that still failed
        if remaining:
            with self._queue_lock:
                try:
                    existing = json.loads(self._queue_file.read_text()) if self._queue_file.exists() else []
                except Exception:
                    existing = []
                combined = (existing + remaining)[-self._MAX_QUEUE:]
                self._queue_file.write_text(json.dumps(combined))
        if sent:
            logger.info(f"Flushed {sent} pending Telegram messages")

    def _tag(self, message: str) -> str:
        """Add account header without HTML/Markdown formatting."""
        if self.account_label:
            header = f"━━ 💼 {self.account_label} ━━"
            return f"{header}\n{message}"
        return message

    def _raw_send(self, text: str) -> bool:
        """Attempt a single HTTP POST to Telegram. Returns True on success."""
        try:
            from requests.adapters import HTTPAdapter
            from urllib3.util.retry import Retry

            session = requests.Session()
            retry = Retry(total=2, backoff_factor=0.5,
                          status_forcelist=[429, 500, 502, 503, 504],
                          allowed_methods=["POST"])
            session.mount("https://", HTTPAdapter(max_retries=retry))
            resp = session.post(
                f"{self.base_url}/sendMessage",
                json={"chat_id": self.chat_id, "text": text, "disable_web_page_preview": True},
                timeout=5,
            )
            if resp.status_code == 200:
                return True
            logger.error(f"Telegram error {resp.status_code}: {resp.text}")
            return False
        except requests.exceptions.Timeout:
            logger.error("Telegram request timed out after 10s")
            return False
        except requests.exceptions.ConnectionError as e:
            logger.error(f"Telegram connection error: {e}")
            return False
        except Exception as e:
            logger.error(f"Telegram send error: {e}")
            return False

    def send_message(self, message: str) -> bool:
        """Send a plain-text message to Telegram. Queues to disk on failure for auto-retry.
        Runs the network call in a thread with a hard 12-second cap so SSL/ISP blocks
        cannot freeze the calling process (e.g. watchdog startup).
        """
        if not self.enabled:
            return False
        text = self._tag(message)
        result = [False]
        def _do_send():
            result[0] = self._raw_send(text)
        t = threading.Thread(target=_do_send, daemon=True)
        t.start()
        t.join(timeout=12)  # hard cap — ISP SSL blocks can outlast requests timeout
        if not result[0]:
            self._queue_message(text)
        return result[0]

    # ==================== Trade Alerts ====================

    def trade_entry(
        self,
        symbol: str,
        direction: str,
        entry_price: float,
        sl: float,
        tp1: float,
        tp2: float,
        tp3: float,
        lot_size: float,
        risk_amount: float,
    ):
        """Notify on trade entry"""
        dir_label = "[BUY]" if direction.upper() == "BUY" else "[SELL]"

        r_pts = abs(entry_price - sl)

        message = f"""NEW TRADE - {symbol} {dir_label}

Direction: {direction.upper()}
Entry: {entry_price:.2f}
Lot Size: {lot_size:.2f}

TP1: {tp1:.2f}
TP2: {tp2:.2f}
TP3: {tp3:.2f}
Stop Loss: {sl:.2f}
R (SL distance): {r_pts:.2f} pts

Risk: $ {risk_amount:.2f}

Time: {ist_time()}
"""
        return self.send_message(message)

    def tp_hit(self, symbol: str, tp_level: int, price: float, profit: float, remaining_lots: float):
        """Notify on take profit hit"""
        message = f"""TP{tp_level} HIT - {symbol}

Price: {price:.2f}
Profit: +$ {profit:.2f}
Remaining: {remaining_lots} lots

Time: {ist_time()}
"""
        return self.send_message(message)

    def sl_hit(self, symbol: str, price: float, loss: float):
        """Notify on stop loss hit"""
        message = f"""STOP LOSS - {symbol}

Exit Price: {price:.2f}
Loss: -$ {abs(loss):.2f}

Time: {ist_time()}
"""
        return self.send_message(message)

    def trailing_stop_hit(self, symbol: str, price: float, profit: float):
        """Notify on trailing stop exit"""
        message = f"""TRAIL STOP - {symbol}

Exit Price: {price:.2f}
Profit: $ {profit:+.2f}

Time: {ist_time()}
"""
        return self.send_message(message)

    def trade_closed(self, symbol: str, reason: str, profit: float):
        """Notify on any trade closure"""
        status = "WIN" if profit >= 0 else "LOSS"

        message = f"""TRADE CLOSED - {symbol} [{status}]

Reason: {reason}
P&L: $ {profit:+.2f}

Time: {ist_time()}
"""
        return self.send_message(message)

    # ==================== Status Updates ====================

    def daily_summary(self, date: str, trades: int, wins: int, gross_pnl: float, balance: float, drawdown: float):
        """Send daily trading summary"""
        win_rate = (wins / trades * 100) if trades > 0 else 0
        status = "UP" if gross_pnl >= 0 else "DOWN"

        message = f"""DAILY SUMMARY - {date} [{status}]

Trades: {trades}
Wins: {wins} ({win_rate:.0f}%)
P&L: $ {gross_pnl:+.2f}

Balance: $ {balance:,.2f}
Drawdown: {drawdown:.2f}%

====================="""
        return self.send_message(message)

    def bot_started(self, indices: list, balance: float):
        """Notify bot startup"""
        message = f"""TRADING BOT STARTED

Indices: {', '.join(indices)}
Balance: $ {balance:.2f}
Mode: Live Trading

Time: {ist_time('%Y-%m-%d %H:%M:%S IST')}
"""
        return self.send_message(message)

    def bot_stopped(self, reason: str, balance: float, daily_pnl: float):
        """Notify bot shutdown"""
        message = f"""TRADING BOT STOPPED

Reason: {reason}
Balance: $ {balance:,.2f}
Today's P&L: $ {daily_pnl:+.2f}

Time: {ist_time('%Y-%m-%d %H:%M:%S IST')}
"""
        return self.send_message(message)

    def error_alert(self, error_type: str, details: str):
        """Send error notification"""
        message = f"""ERROR ALERT

Type: {error_type}
Details: {details}

Time: {ist_time()}
"""
        return self.send_message(message)

    def drawdown_warning(self, current_dd: float, limit: float, balance: float):
        """Warn when approaching drawdown limit"""
        message = f"""DRAWDOWN WARNING - APPROACHING LIMIT

Current DD: {current_dd:.2f}%
Limit: {limit:.2f}%
Balance: $ {balance:,.2f}

WARNING: Approaching maximum drawdown!
"""
        return self.send_message(message)

    def market_open(self, symbol: str):
        """Notify when market opens"""
        message = f"""MARKET OPEN - {symbol}

Looking for trade setups...

Time: {ist_time()}
"""
        return self.send_message(message)

    # ==================== Connection & Environment Alerts ====================

    def connection_lost(self, connection_type: str, details: str = ""):
        """Alert when connection is lost"""
        message = f"""CONNECTION LOST

Type: {connection_type}
Details: {details if details else 'Connection interrupted'}

WARNING: Trading paused until reconnection

Time: {ist_time()}
"""
        return self.send_message(message)

    def connection_restored(self, connection_type: str, downtime_seconds: int = 0):
        """Alert when connection is restored"""
        downtime_str = (
            f"{downtime_seconds}s"
            if downtime_seconds < 60
            else f"{downtime_seconds // 60}m {downtime_seconds % 60}s"
        )

        message = f"""CONNECTION RESTORED

Type: {connection_type}
Downtime: {downtime_str}

Trading resumed

Time: {ist_time()}
"""
        return self.send_message(message)

    def reconnecting(self, connection_type: str, attempt: int, max_attempts: int):
        """Alert on reconnection attempt"""
        message = f"""RECONNECTING...

Type: {connection_type}
Attempt: {attempt}/{max_attempts}

Time: {ist_time()}
"""
        return self.send_message(message)

    def internet_down(self):
        """Alert when internet connection is down"""
        message = f"""INTERNET DISCONNECTED

No internet connectivity detected.
Trading paused until connection restored.

Time: {ist_time()}
"""
        return self.send_message(message)

    def mt5_disconnected(self, reason: str = ""):
        """Alert when MT5 connection is lost"""
        message = f"""MT5 DISCONNECTED

Reason: {reason if reason else 'Connection lost'}

WARNING: Cannot execute trades!

Time: {ist_time()}
"""
        return self.send_message(message)

    def mt5_reconnected(self, account_balance: float):
        """Alert when MT5 connection is restored"""
        message = f"""MT5 RECONNECTED

Balance: $ {account_balance:,.2f}
Trading resumed

Time: {ist_time()}
"""
        return self.send_message(message)

    def market_closed(self, symbol: str, next_open: str = ""):
        """Notify when market closes"""
        next_open_str = f"Next open: {next_open}" if next_open else ""

        message = f"""MARKET CLOSED - {symbol}

{next_open_str}

Time: {ist_time()}
"""
        return self.send_message(message)

    def sl_moved(self, symbol: str, old_sl: float, new_sl: float, reason: str):
        """Notify when stop loss is moved"""
        message = f"""SL MODIFIED - {symbol}

Old SL: {old_sl:.2f}
New SL: {new_sl:.2f}
Reason: {reason}

Time: {ist_time()}
"""
        return self.send_message(message)

    def order_failed(self, symbol: str, order_type: str, reason: str):
        """Alert when order execution fails"""
        message = f"""ORDER FAILED - {symbol}

Type: {order_type}
Reason: {reason}

Time: {ist_time()}
"""
        return self.send_message(message)

    def news_blocked(self, symbol: str, event_name: str, minutes_away: int):
        """Alert when trade blocked due to news"""
        message = f"""TRADE BLOCKED - NEWS

Symbol: {symbol}
Event: {event_name}
Time: In {minutes_away} minutes

Signal skipped due to high-impact news.

Time: {ist_time()}
"""
        return self.send_message(message)

    def daily_limit_reached(self, trades_today: int, daily_pnl: float):
        """Alert when daily trade limit or loss limit reached"""
        message = f"""DAILY LIMIT REACHED

Trades Today: {trades_today}
Daily P&L: $ {daily_pnl:+,.2f}

No more trades until tomorrow.

Time: {ist_time()}
"""
        return self.send_message(message)

    def system_status(
        self,
        balance: float,
        open_positions: int,
        daily_pnl: float,
        daily_dd: float,
        overall_dd: float,
        active_markets: list,
    ):
        """Send periodic system status update"""
        markets_str = ", ".join(active_markets) if active_markets else "None"

        message = f"""SYSTEM STATUS

Balance: $ {balance:,.2f}
Today's P&L: $ {daily_pnl:+,.2f}
Daily DD: {daily_dd:.2f}%
Overall DD: {overall_dd:.2f}%
Open Positions: {open_positions}
Active Markets: {markets_str}

Time: {ist_time()}
"""
        return self.send_message(message)

    def critical_alert(self, title: str, message_text: str):
        """Send critical alert that requires immediate attention"""
        message = f"""*** CRITICAL ALERT ***

{title}

{message_text}

IMMEDIATE ATTENTION REQUIRED!

Time: {ist_time()}
"""
        return self.send_message(message)

    # ==================== Test ====================

    def test_connection(self) -> bool:
        """Test Telegram connection"""
        message = f"""CONNECTION TEST - SUCCESS

ICT Trading Bot connected successfully!

Time: {ist_time('%Y-%m-%d %H:%M:%S IST')}
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
        print("[OK] Telegram connected!")
    else:
        print("[FAILED] Telegram connection failed")
        print("\nSetup instructions:")
        print("1. Message @BotFather on Telegram")
        print("2. Send /newbot and follow instructions")
        print("3. Copy the bot token")
        print("4. Message @userinfobot to get your chat ID")
        print("5. Add to config_fundednext.py:")
        print('   TELEGRAM_BOT_TOKEN = "your_bot_token"')
        print('   TELEGRAM_CHAT_ID = "your_chat_id"')