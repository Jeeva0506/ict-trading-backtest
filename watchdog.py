"""
Watchdog Process for ORB Trading Bot
=====================================
Monitors the main trading bot and restarts if it crashes or freezes.
Also monitors system resources and MT5 process.

Run this INSTEAD of run_live.py directly.

Usage:
    python watchdog.py                          # Default account
    python watchdog.py --account stellar15k     # Stellar15k profile

Edge cases handled:
- Bot crash / unexpected exit → auto-restart with rate limiting
- Bot freeze (no heartbeat) → force kill + restart
- MT5 terminal not running → auto-start with retry
- High CPU / RAM → Telegram alert
- Multiple watchdog prevention → PID-based lock file
- Power loss recovery → heartbeat file checks on startup
- Network loss → bot handles internally; watchdog monitors process only
"""
import subprocess
import time
import psutil
import os
import sys
import io
import signal
import argparse
from datetime import datetime, timedelta
from pathlib import Path
import logging

# ── Venv guard removed ────────────────────────────────────────────────
# The .venv launcher stubs on Windows spawn child processes, causing
# duplicate watchdogs and bots. Launch via .venv/Scripts/python.exe
# (the stub handles site-packages setup, sys.executable is the real
# interpreter). No re-exec needed.

# ── CRITICAL: parse --account BEFORE any config imports ──────────────
_pre_parser = argparse.ArgumentParser(add_help=False)
_pre_parser.add_argument('--account', type=str, default='',
                         help='Account profile name (loads .env.<profile>)')
_pre_args, _ = _pre_parser.parse_known_args()
if _pre_args.account:
    os.environ['ACCOUNT_PROFILE'] = _pre_args.account

# Add path for imports
sys.path.insert(0, str(Path(__file__).parent / 'live'))

from config_fundednext import (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
                               HEARTBEAT_URL, HEARTBEAT_INTERVAL_SECONDS,
                               PROFITABLE_INDICES, ACCOUNT_LABEL, MT5_PATH)
from telegram_notifier import TelegramNotifier
import requests as _requests
import pytz

# Setup logging with UTF-8 encoding for Windows Task Scheduler
# Namespace per account profile
_label_slug = ACCOUNT_LABEL.lower().replace(' ', '_')
log_dir = Path(__file__).parent / 'live' / 'logs' / _label_slug
log_dir.mkdir(parents=True, exist_ok=True)

logger = logging.getLogger(f"watchdog.{ACCOUNT_LABEL}")
logger.setLevel(logging.INFO)

# File handler
file_handler = logging.FileHandler(log_dir / 'watchdog.log', encoding='utf-8')
file_handler.setLevel(logging.INFO)
file_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))

logger.addHandler(file_handler)

# Console handler with UTF-8 for Windows
# When running under pythonw.exe (Task Scheduler), stdout may not exist —
# skip the console handler to avoid AttributeError / OSError.
_has_console = hasattr(sys.stdout, 'buffer') and sys.stdout.buffer is not None
if _has_console:
    if sys.platform == 'win32':
        console_stream = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    else:
        console_stream = sys.stdout
    console_handler = logging.StreamHandler(console_stream)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
    logger.addHandler(console_handler)

# Lock file to prevent multiple watchdog instances (namespaced per account)
LOCK_FILE = log_dir / 'watchdog.lock'
HEARTBEAT_FILE = log_dir / 'heartbeat.txt'
HEARTBEAT_STALE_SECONDS = 300  # 5 minutes without heartbeat = frozen

# Buffer: start bot this many minutes before earliest market open,
# keep running this many minutes after latest market close
PRE_OPEN_BUFFER_MIN = 30
POST_CLOSE_BUFFER_MIN = 15


def is_trading_window() -> bool:
    """
    Returns True if the current time falls within the trading window
    of ANY configured market (with pre-open/post-close buffers).
    Returns False on weekends (Saturday/Sunday).
    """
    utc_now = datetime.now(pytz.UTC)

    for symbol, config in PROFITABLE_INDICES.items():
        try:
            local_tz = pytz.timezone(config.get('timezone', 'America/New_York'))
            local_now = utc_now.astimezone(local_tz)

            # Skip weekends in market's local timezone
            if local_now.weekday() >= 5:  # Saturday=5, Sunday=6
                continue

            cur_min = local_now.hour * 60 + local_now.minute
            open_min = config['market_open_hour'] * 60 + config['market_open_minute']
            close_min = config['market_close_hour'] * 60 + config['market_close_minute']

            # Apply buffers
            window_start = open_min - PRE_OPEN_BUFFER_MIN
            window_end = close_min + POST_CLOSE_BUFFER_MIN

            if window_start <= cur_min <= window_end:
                return True
        except Exception as e:
            logger.warning(f"Trading window check failed for {symbol}: {e}")
            continue

    return False

# Common MT5 installation paths
MT5_PATHS = [
    Path(os.environ.get('PROGRAMFILES', 'C:\\Program Files')) / 'MetaTrader 5' / 'terminal64.exe',
    Path(os.environ.get('PROGRAMFILES(X86)', 'C:\\Program Files (x86)')) / 'MetaTrader 5' / 'terminal64.exe',
    Path(os.environ.get('APPDATA', '')) / 'MetaQuotes' / 'Terminal' / 'terminal64.exe',
    Path('C:\\Program Files\\FundedNext MT5 Terminal') / 'terminal64.exe',
    Path('C:\\Program Files\\FundedNext MetaTrader 5') / 'terminal64.exe',
]


def acquire_lock():
    """Try to acquire exclusive lock. Returns True if successful, False if another instance is running."""
    try:
        # Atomic exclusive creation — avoids TOCTOU race between two simultaneous starts
        try:
            fd = os.open(str(LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, 'w') as f:
                f.write(str(os.getpid()))
            logger.info(f"Lock acquired, PID: {os.getpid()}")
            return True
        except FileExistsError:
            pass  # Lock file already exists — check if owner is alive

        # Lock exists — determine whether it belongs to a live watchdog
        try:
            with open(LOCK_FILE, 'r') as f:
                old_pid = int(f.read().strip())
            if psutil.pid_exists(old_pid):
                try:
                    proc = psutil.Process(old_pid)
                    if 'python' in proc.name().lower():
                        logger.error(f"Another watchdog is already running (PID: {old_pid})")
                        return False
                except psutil.AccessDenied:
                    # Cannot inspect the process (e.g. different user / Task Scheduler SYSTEM).
                    # Treat as alive to avoid overwriting a legitimate lock.
                    logger.error(f"Another watchdog is running (PID: {old_pid}, access denied)")
                    return False
                except psutil.NoSuchProcess:
                    pass  # Process vanished between pid_exists and Process() — stale
        except (ValueError, FileNotFoundError):
            pass  # Corrupted or missing lock — fall through to overwrite

        # Stale lock — remove and re-acquire atomically
        LOCK_FILE.unlink(missing_ok=True)
        try:
            fd = os.open(str(LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, 'w') as f:
                f.write(str(os.getpid()))
            logger.info(f"Lock acquired (replaced stale lock), PID: {os.getpid()}")
            return True
        except FileExistsError:
            logger.error("Lock was grabbed by another process during stale cleanup")
            return False
    except Exception as e:
        logger.error(f"Error acquiring lock: {e}")
        return False


def release_lock():
    """Release the lock file"""
    try:
        if LOCK_FILE.exists():
            LOCK_FILE.unlink()
            logger.info("Lock released")
    except Exception as e:
        logger.error(f"Error releasing lock: {e}")


class TradingWatchdog:
    """Monitors and restarts the trading bot if needed"""
    
    def __init__(self):
        # Determine Python path: prefer pythonw (no console window) > python
        # Use _base_executable — the REAL interpreter, not the .venv launcher
        # stub. On Windows, .venv/python.exe is a stub that spawns the system
        # Python as a child process, causing duplicates. _base_executable
        # gives the actual interpreter path directly.
        self.python_path = Path(getattr(sys, '_base_executable', sys.executable))
        self.script_path = Path(__file__).parent / 'run_live.py'
        # Build subprocess command — pass --account to run_live.py if set
        self._account_profile = _pre_args.account
        self.process = None
        self.start_time = None
        self.restart_count = 0
        self.max_restarts_per_hour = 5
        self.restart_times = []
        self.telegram = TelegramNotifier(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
                                         account_label=ACCOUNT_LABEL)
        self._mt5_path_cache = MT5_PATH if MT5_PATH else None  # Use configured MT5 path
        self._stdout_file = None
        self._stderr_file = None
        self._last_heartbeat = 0  # epoch timestamp of last heartbeat ping
        self._adopted_pid = None  # PID of a pre-existing bot we adopted (not started by us)
    
    # ------------------------------------------------------------------
    # Bot process management
    # ------------------------------------------------------------------
    def start_bot(self):
        """Start the trading bot as a subprocess"""
        try:
            logger.info(f"Starting bot: {self.python_path} {self.script_path}")
            
            # Clear stale heartbeat before starting
            if HEARTBEAT_FILE.exists():
                HEARTBEAT_FILE.unlink()
            
            # Close any previous stdout/stderr file handles to prevent leaks
            for attr in ('_stdout_file', '_stderr_file'):
                old_fh = getattr(self, attr, None)
                if old_fh and not old_fh.closed:
                    try:
                        old_fh.close()
                    except Exception:
                        pass
            
            # Redirect stdout/stderr to files to prevent PIPE buffer deadlock
            stdout_log = log_dir / 'bot_stdout.log'
            stderr_log = log_dir / 'bot_stderr.log'
            # Cap log growth: these are raw subprocess streams (not rotatable
            # via logging). On each (re)start, if a file exceeds the cap, roll
            # it to a single .old generation so total stays bounded (~2x cap).
            # Prevents the multi-GB bot_stderr.log buildup from network-error
            # traceback spam during connectivity drops.
            _LOG_CAP_BYTES = 50 * 1024 * 1024  # 50 MB
            for _lf in (stdout_log, stderr_log):
                try:
                    if _lf.exists() and _lf.stat().st_size > _LOG_CAP_BYTES:
                        _old = _lf.with_suffix(_lf.suffix + '.old')
                        if _old.exists():
                            _old.unlink()
                        _lf.rename(_old)
                        logger.info(f"Rotated oversized log: {_lf.name} -> {_old.name}")
                except Exception as _re:
                    logger.warning(f"Log rotation check failed for {_lf.name}: {_re}")
            stdout_f = open(stdout_log, 'a', encoding='utf-8')
            stderr_f = open(stderr_log, 'a', encoding='utf-8')
            
            # CREATE_NO_WINDOW prevents a visible console flash on each restart
            _flags = 0
            if sys.platform == 'win32':
                _flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
            
            cmd = [str(self.python_path), str(self.script_path)]
            if self._account_profile:
                cmd.extend(['--account', self._account_profile])
            
            self.process = subprocess.Popen(
                cmd,
                cwd=str(self.script_path.parent),
                stdout=stdout_f,
                stderr=stderr_f,
                creationflags=_flags,
            )
            self._stdout_file = stdout_f
            self._stderr_file = stderr_f
            self.start_time = datetime.now()
            self.restart_times.append(datetime.now())
            logger.info(f"Bot started with PID: {self.process.pid}")
            return True
        except FileNotFoundError:
            logger.error(f"Python not found at: {self.python_path}")
            self.telegram.send_message(
                f"❌ <b>BOT START FAILED</b>\n\n"
                f"Python not found at:\n<code>{self.python_path}</code>"
            )
            return False
        except Exception as e:
            logger.error(f"Failed to start bot: {e}")
            self.telegram.send_message(f"❌ <b>BOT START FAILED</b>\n\n{e}")
            return False
    
    def _try_adopt_existing_bot(self) -> bool:
        """On fresh watchdog start, adopt a bot that is already running (has a live bot.lock).
        This prevents the watchdog from double-starting a bot that survived a watchdog crash."""
        bot_lock = log_dir / 'bot.lock'
        if not bot_lock.exists():
            return False
        try:
            existing_pid = int(bot_lock.read_text().strip())
            if psutil.pid_exists(existing_pid):
                proc = psutil.Process(existing_pid)
                if 'python' in proc.name().lower():
                    logger.info(f"Found existing bot PID {existing_pid} — adopting (no restart)")
                    self._adopted_pid = existing_pid
                    self.start_time = datetime.now()
                    return True
        except Exception as e:
            logger.warning(f"Bot.lock adoption check failed: {e}")
        return False

    def is_bot_running(self) -> bool:
        """Check if the bot subprocess (or adopted bot process) is still alive."""
        if self._adopted_pid is not None:
            if psutil.pid_exists(self._adopted_pid):
                try:
                    proc = psutil.Process(self._adopted_pid)
                    if 'python' in proc.name().lower():
                        return True
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
            # Adopted process has died — fall through to normal subprocess tracking
            logger.info(f"Adopted bot PID {self._adopted_pid} is no longer alive")
            self._adopted_pid = None
            return False
        if self.process is None:
            return False
        return self.process.poll() is None
    
    def kill_bot(self):
        """Force kill the bot subprocess (or adopted bot process) and all children."""
        if self._adopted_pid is not None:
            try:
                proc = psutil.Process(self._adopted_pid)
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except psutil.TimeoutExpired:
                    proc.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
            logger.info(f"Adopted bot PID {self._adopted_pid} terminated")
            self._adopted_pid = None
            return
        if self.process is None:
            return
        
        try:
            pid = self.process.pid
            logger.info(f"Killing bot process {pid} and children...")
            
            # Kill entire process tree
            try:
                parent = psutil.Process(pid)
                children = parent.children(recursive=True)
                
                # Terminate children first
                for child in children:
                    try:
                        child.terminate()
                    except psutil.NoSuchProcess:
                        pass
                
                # Terminate parent
                parent.terminate()
                
                # Wait up to 5 seconds for graceful shutdown
                gone, alive = psutil.wait_procs([parent] + children, timeout=5)
                
                # Force kill survivors
                for p in alive:
                    try:
                        logger.warning(f"Force killing PID {p.pid}")
                        p.kill()
                    except psutil.NoSuchProcess:
                        pass
                        
            except psutil.NoSuchProcess:
                pass  # Already dead
            
            self.process = None
            logger.info("Bot process killed")
            
        except Exception as e:
            logger.error(f"Error killing bot: {e}")
            self.process = None
    
    def is_bot_frozen(self) -> bool:
        """Check if the bot is alive but not responding (no heartbeat updates)"""
        if not self.is_bot_running():
            return False  # Not frozen if not running
        
        if not HEARTBEAT_FILE.exists():
            # No heartbeat file yet — bot might still be initializing
            # Only flag as frozen if bot has been running > 3 minutes with no heartbeat
            if self.start_time and (datetime.now() - self.start_time).total_seconds() > 180:
                logger.warning("No heartbeat file found after 3 minutes of runtime")
                return True
            return False
        
        try:
            # Check heartbeat file modification time
            mtime = datetime.fromtimestamp(HEARTBEAT_FILE.stat().st_mtime)
            age = (datetime.now() - mtime).total_seconds()
            
            if age > HEARTBEAT_STALE_SECONDS:
                logger.warning(f"Heartbeat stale: {age:.0f}s (threshold: {HEARTBEAT_STALE_SECONDS}s)")
                return True
            return False
        except Exception as e:
            logger.error(f"Error checking heartbeat: {e}")
            return False
    
    def can_restart(self) -> bool:
        """Rate-limit restarts: max N per hour"""
        one_hour_ago = datetime.now() - timedelta(hours=1)
        recent_restarts = [t for t in self.restart_times if t > one_hour_ago]
        self.restart_times = recent_restarts  # Clean up old entries
        return len(recent_restarts) < self.max_restarts_per_hour
    
    # ------------------------------------------------------------------
    # System monitoring
    # ------------------------------------------------------------------
    def check_system_resources(self) -> dict:
        """Check CPU and memory usage"""
        try:
            cpu = psutil.cpu_percent(interval=1)
            mem = psutil.virtual_memory()
            disk = psutil.disk_usage('/')
            return {
                'cpu_percent': cpu,
                'memory_percent': mem.percent,
                'memory_available_gb': round(mem.available / (1024 ** 3), 1),
                'disk_percent': disk.percent,
            }
        except Exception as e:
            logger.error(f"Error checking resources: {e}")
            return {'cpu_percent': 0, 'memory_percent': 0, 'memory_available_gb': 0, 'disk_percent': 0}
    
    def check_mt5_process(self) -> bool:
        """Check if THIS account's MetaTrader 5 terminal is running.
        FIX B5: Filter by executable path when MT5_PATH is configured,
        so each watchdog monitors only its own terminal instance."""
        try:
            target_path = self._mt5_path_cache
            for proc in psutil.process_iter(['name', 'exe']):
                name = proc.info['name']
                if name and 'terminal64' in name.lower():
                    if target_path:
                        # Multi-instance: match exact executable path
                        exe = proc.info.get('exe', '')
                        if exe and Path(exe).resolve() == Path(str(target_path)).resolve():
                            return True
                    else:
                        # Single instance: any terminal64 counts
                        return True
            return False
        except Exception as e:
            logger.error(f"Error checking MT5 process: {e}")
            return False
    
    def start_mt5(self) -> bool:
        """Attempt to start MetaTrader 5 terminal"""
        # Use cached path if found before
        if self._mt5_path_cache and self._mt5_path_cache.exists():
            return self._launch_mt5(self._mt5_path_cache)
        
        # Search common install locations
        for mt5_path in MT5_PATHS:
            if mt5_path.exists():
                self._mt5_path_cache = mt5_path
                return self._launch_mt5(mt5_path)
        
        # Search running processes for MT5 path (in case it was started manually before)
        try:
            for proc in psutil.process_iter(['name', 'exe']):
                if proc.info['name'] and 'terminal64' in proc.info['name'].lower():
                    exe = proc.info.get('exe')
                    if exe:
                        self._mt5_path_cache = Path(exe)
                        return True  # Already running
        except Exception:
            pass
        
        logger.error("MT5 terminal not found in any known location")
        return False
    
    def _launch_mt5(self, path: Path) -> bool:
        """Launch MT5 from given path and wait for it to start"""
        try:
            logger.info(f"Launching MT5 from: {path}")
            subprocess.Popen([str(path)], creationflags=subprocess.DETACHED_PROCESS)
            
            # Wait up to 30 seconds for MT5 to appear
            for _ in range(15):
                time.sleep(2)
                if self.check_mt5_process():
                    logger.info("MT5 started successfully")
                    # Give MT5 extra time to fully initialize and connect
                    time.sleep(10)
                    return True
            
            logger.error("MT5 process did not appear within 30 seconds")
            return False
        except Exception as e:
            logger.error(f"Failed to launch MT5: {e}")
            return False
    
    # ------------------------------------------------------------------
    # External heartbeat (healthchecks.io)
    # ------------------------------------------------------------------
    def _send_heartbeat(self):
        """Ping external heartbeat URL if configured and interval has elapsed."""
        if not HEARTBEAT_URL:
            return
        now = time.time()
        if now - self._last_heartbeat < HEARTBEAT_INTERVAL_SECONDS:
            return  # Not time yet
        try:
            _requests.get(HEARTBEAT_URL, timeout=10)
            self._last_heartbeat = now
            logger.debug("Heartbeat ping sent")
        except Exception as e:
            logger.warning(f"Heartbeat ping failed: {e}")
    
    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------
    def run(self):
        """Main watchdog loop with schedule awareness"""
        logger.info("=" * 60)
        logger.info("ORB Trading Bot Watchdog Started")
        logger.info(f"Python: {self.python_path}")
        logger.info(f"Script: {self.script_path}")
        logger.info("=" * 60)
        
        self.telegram.send_message(
            "🐕 <b>WATCHDOG STARTED</b>\n\n"
            f"Python: <code>{self.python_path}</code>\n"
            f"Monitoring: <code>{self.script_path.name}</code>"
        )
        
        bot_was_running = False  # Track if we stopped the bot for off-hours
        
        while True:
            try:
                # ── Schedule check ─────────────────────────────────────
                if not is_trading_window():
                    # Outside trading hours
                    if self.is_bot_running():
                        logger.info("Outside trading window — stopping bot")
                        self.telegram.send_message(
                            "🌙 <b>SCHEDULE: Off-hours</b>\n\n"
                            "All markets closed. Stopping bot to save resources.\n"
                            "Will auto-start before next market opens.")
                        self.kill_bot()
                        bot_was_running = True
                    # Sleep longer outside trading hours (check every 5 min)
                    time.sleep(300)
                    self._send_heartbeat()
                    continue
                
                # ── Inside trading window — ensure bot is running ──────
                if not self.is_bot_running():
                    if bot_was_running or self.process is None:
                        # On first iteration (process is None), try to adopt an
                        # existing bot before starting a new one. This handles the
                        # case where the watchdog was restarted while the bot is
                        # still running (e.g., watchdog crash during live session).
                        if self.process is None and self._try_adopt_existing_bot():
                            logger.info("Monitoring adopted bot — no new start needed")
                            bot_was_running = False
                            self.telegram.send_message(
                                "🔗 <b>WATCHDOG: Adopted existing bot</b>\n\n"
                                f"PID {self._adopted_pid} is already running.\n"
                                "Watchdog is now monitoring it.")
                            time.sleep(30)
                            continue
                        # Bot was stopped for off-hours (or no existing bot) — start fresh
                        logger.info("Trading window active — starting bot")
                        if not self.start_bot():
                            logger.error("Bot start failed. Retrying in 30 seconds...")
                            time.sleep(30)
                            if not self.start_bot():
                                self.telegram.critical_alert(
                                    "BOT START FAILED",
                                    "Cannot start bot after 2 attempts.")
                                time.sleep(300)
                                continue
                        bot_was_running = False
                        self.telegram.send_message(
                            "☀️ <b>SCHEDULE: Markets opening</b>\n\n"
                            "Bot started for trading session.")
                        time.sleep(30)
                        continue  # Skip to next iteration — let bot initialize
                
                time.sleep(30)  # Check every 30 seconds during trading hours
                
                # ── External heartbeat ping ──────────────────────────
                self._send_heartbeat()
                
                # Check system resources
                resources = self.check_system_resources()
                if resources['cpu_percent'] > 90:
                    logger.warning(f"High CPU usage: {resources['cpu_percent']}%")
                if resources['memory_percent'] > 90:
                    logger.warning(f"High memory usage: {resources['memory_percent']}%")
                    self.telegram.send_message(
                        f"⚠️ <b>HIGH MEMORY</b>\n\nMemory: {resources['memory_percent']}%\n\nConsider closing other apps!"
                    )
                if resources['disk_percent'] > 95:
                    self.telegram.send_message(
                        f"⚠️ <b>LOW DISK SPACE</b>\n\nDisk usage: {resources['disk_percent']}%"
                    )
                
                # Check if MT5 is running
                if not self.check_mt5_process():
                    logger.warning("MT5 not running! Attempting to start...")
                    self.telegram.send_message("⚠️ <b>MT5 NOT RUNNING</b>\n\nAttempting to start...")
                    if self.start_mt5():
                        self.telegram.send_message("✅ MT5 started successfully")
                    else:
                        self.telegram.critical_alert("MT5 FAILED TO START", 
                            "Could not find or start MT5 terminal. Manual intervention required!")
                
                # Check if bot is running
                if not self.is_bot_running():
                    exit_code = self.process.returncode if self.process else "Unknown"
                    logger.warning(f"Bot process died! Exit code: {exit_code}")
                    
                    # Capture stderr for crash diagnosis
                    crash_info = ""
                    try:
                        # Close file handles before reading
                        if hasattr(self, '_stdout_file') and self._stdout_file:
                            self._stdout_file.close()
                        if hasattr(self, '_stderr_file') and self._stderr_file:
                            self._stderr_file.close()
                        stderr_log = log_dir / 'bot_stderr.log'
                        if stderr_log.exists():
                            with open(stderr_log, 'r', encoding='utf-8', errors='replace') as f:
                                content = f.read()
                                crash_info = content[-500:] if len(content) > 500 else content
                    except Exception:
                        pass
                    
                    if self.can_restart():
                        self.restart_count += 1
                        msg = (
                            f"🔄 <b>BOT CRASHED</b>\n\n"
                            f"Exit code: {exit_code}\n"
                            f"Restart #{self.restart_count}\n"
                        )
                        if crash_info:
                            msg += f"\n<pre>{crash_info[:200]}</pre>\n"
                        msg += "\nRestarting in 10 seconds..."
                        self.telegram.send_message(msg)
                        time.sleep(10)
                        self.start_bot()
                        self.telegram.send_message("✅ Bot restarted successfully")
                    else:
                        self.telegram.critical_alert(
                            "TOO MANY RESTARTS",
                            f"Bot crashed {len(self.restart_times)} times in last hour.\n"
                            f"Manual intervention required!"
                        )
                        logger.critical("Too many restarts! Stopping watchdog.")
                        break
                
                # Check if bot is frozen
                elif self.is_bot_frozen():
                    logger.warning("Bot appears frozen (no heartbeat)")
                    
                    if self.can_restart():
                        self.telegram.send_message(
                            "🥶 <b>BOT FROZEN</b>\n\n"
                            "No heartbeat for 5 minutes.\n"
                            "Force restarting..."
                        )
                        self.kill_bot()
                        time.sleep(5)
                        self.start_bot()
                        self.telegram.send_message("✅ Bot restarted after freeze")
                    else:
                        self.telegram.critical_alert(
                            "BOT FROZEN - TOO MANY RESTARTS",
                            "Manual intervention required!"
                        )
                
                # Log status every 10 minutes
                if datetime.now().minute % 10 == 0 and datetime.now().second < 30:
                    uptime = datetime.now() - self.start_time if self.start_time else timedelta(0)
                    logger.info(
                        f"Watchdog status - Bot PID: {self.process.pid if self.process else 'None'} | "
                        f"Uptime: {uptime} | Restarts: {self.restart_count} | "
                        f"CPU: {resources['cpu_percent']}% | RAM: {resources['memory_percent']}%"
                    )
                
            except KeyboardInterrupt:
                logger.info("Watchdog stopped by user")
                self.telegram.send_message("🛑 <b>WATCHDOG STOPPED</b>\n\nUser requested shutdown")
                self.kill_bot()
                break
            except Exception as e:
                logger.error(f"Watchdog error: {e}")
                time.sleep(60)


if __name__ == '__main__':
    # Prevent multiple watchdog instances
    if not acquire_lock():
        logger.error("Failed to acquire lock - another watchdog may be running. Exiting.")
        sys.exit(1)
    
    try:
        watchdog = TradingWatchdog()
        watchdog.run()
    finally:
        release_lock()
