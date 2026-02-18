"""
Watchdog Process for ORB Trading Bot
=====================================
Monitors the main trading bot and restarts if it crashes or freezes.
Also monitors system resources and MT5 process.

Run this INSTEAD of run_live.py directly.

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
from datetime import datetime, timedelta
from pathlib import Path
import logging

# Setup logging with UTF-8 encoding for Windows Task Scheduler
log_dir = Path(__file__).parent / 'live' / 'logs'
log_dir.mkdir(parents=True, exist_ok=True)

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# File handler
file_handler = logging.FileHandler(log_dir / 'watchdog.log', encoding='utf-8')
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

# Add path for imports
sys.path.insert(0, str(Path(__file__).parent / 'live'))

from config_fundednext import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
from telegram_notifier import TelegramNotifier

# Lock file to prevent multiple watchdog instances
LOCK_FILE = Path(__file__).parent / 'live' / 'logs' / 'watchdog.lock'
HEARTBEAT_FILE = Path(__file__).parent / 'live' / 'logs' / 'heartbeat.txt'
HEARTBEAT_STALE_SECONDS = 300  # 5 minutes without heartbeat = frozen

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
        if LOCK_FILE.exists():
            # Check if the PID in the lock file is still running
            try:
                with open(LOCK_FILE, 'r') as f:
                    old_pid = int(f.read().strip())
                if psutil.pid_exists(old_pid):
                    try:
                        proc = psutil.Process(old_pid)
                        if 'python' in proc.name().lower():
                            logger.error(f"Another watchdog is already running (PID: {old_pid})")
                            return False
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass  # Stale lock, safe to overwrite
            except (ValueError, FileNotFoundError):
                pass  # Corrupted lock file, overwrite
        
        # Write our PID to the lock file
        with open(LOCK_FILE, 'w') as f:
            f.write(str(os.getpid()))
        
        logger.info(f"Lock acquired, PID: {os.getpid()}")
        return True
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
        # Determine Python path: prefer venv, fallback to current interpreter
        venv_python = Path(__file__).parent.parent / '.venv' / 'Scripts' / 'python.exe'
        self.python_path = venv_python if venv_python.exists() else Path(sys.executable)
        self.script_path = Path(__file__).parent / 'run_live.py'
        self.process = None
        self.start_time = None
        self.restart_count = 0
        self.max_restarts_per_hour = 5
        self.restart_times = []
        self.telegram = TelegramNotifier(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
        self._mt5_path_cache = None  # Cache found MT5 path
        self._stdout_file = None
        self._stderr_file = None
    
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
            
            # Redirect stdout/stderr to files to prevent PIPE buffer deadlock
            stdout_log = log_dir / 'bot_stdout.log'
            stderr_log = log_dir / 'bot_stderr.log'
            stdout_f = open(stdout_log, 'a', encoding='utf-8')
            stderr_f = open(stderr_log, 'a', encoding='utf-8')
            
            self.process = subprocess.Popen(
                [str(self.python_path), str(self.script_path)],
                cwd=str(self.script_path.parent),
                stdout=stdout_f,
                stderr=stderr_f,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == 'win32' else 0,
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
    
    def is_bot_running(self) -> bool:
        """Check if the bot subprocess is still alive"""
        if self.process is None:
            return False
        return self.process.poll() is None
    
    def kill_bot(self):
        """Force kill the bot subprocess and all children"""
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
        """Check if MetaTrader 5 terminal is running"""
        try:
            for proc in psutil.process_iter(['name']):
                name = proc.info['name']
                if name and 'terminal64' in name.lower():
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
    # Main loop
    # ------------------------------------------------------------------
    def run(self):
        """Main watchdog loop"""
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
        
        # Start the bot initially
        if not self.start_bot():
            logger.error("Initial bot start failed. Retrying in 30 seconds...")
            time.sleep(30)
            if not self.start_bot():
                self.telegram.critical_alert("WATCHDOG ABORT", "Cannot start bot after 2 attempts.")
                return
        
        while True:
            try:
                time.sleep(30)  # Check every 30 seconds
                
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
