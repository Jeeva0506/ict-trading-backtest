"""
Watchdog Process for ICT Trading Bot
=====================================
Monitors the main trading bot and restarts if it crashes or freezes.
Also monitors system resources and MT5 process.

Run this INSTEAD of run_live.py directly.
"""
import subprocess
import time
import psutil
import os
import sys
import io
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

def acquire_lock():
    """Try to acquire exclusive lock. Returns True if successful, False if another instance is running."""
    import msvcrt
    try:
        # Create lock file with exclusive access
        if LOCK_FILE.exists():
            # Check if the PID in the lock file is still running
            try:
                with open(LOCK_FILE, 'r') as f:
                    old_pid = int(f.read().strip())
                if psutil.pid_exists(old_pid):
                    # Check if it's actually a python process
                    try:
                        proc = psutil.Process(old_pid)
                        if 'python' in proc.name().lower():
                            logger.error(f"Another watchdog is already running (PID: {old_pid})")
                            return False
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass
            except (ValueError, FileNotFoundError):
                pass
        
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
        self.python_path = Path(__file__).parent.parent / '.venv' / 'Scripts' / 'python.exe'
        self.script_path = Path(__file__).parent / 'run_live.py'
        self.process = None
        self.start_time = None
        self.restart_count = 0
        self.max_restarts_per_hour = 5
        self.restart_times = []
        self.telegram = TelegramNotifier(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
        self.last_heartbeat = datetime.now()
        self.heartbeat_file = Path(__file__).parent / 'live' / 'logs' / 'heartbeat.txt'
    
    def start_bot(self):
        """Start the trading bot process"""
        logger.info("Starting trading bot...")
        
        # Capture stderr to a file to debug crashes
        stderr_log = Path(__file__).parent / 'live' / 'logs' / 'bot_stderr.log'
        self.stderr_file = open(stderr_log, 'a', encoding='utf-8')
        self.stderr_file.write(f"\n\n=== Bot started at {datetime.now()} ===\n")
        self.stderr_file.flush()
        
        self.process = subprocess.Popen(
            [str(self.python_path), str(self.script_path)],
            cwd=str(Path(__file__).parent),
            stdout=subprocess.DEVNULL,
            stderr=self.stderr_file,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP
        )
        
        self.start_time = datetime.now()
        self.restart_times.append(self.start_time)
        
        # Clean old restart times (keep last hour only)
        one_hour_ago = datetime.now() - timedelta(hours=1)
        self.restart_times = [t for t in self.restart_times if t > one_hour_ago]
        
        logger.info(f"Bot started with PID: {self.process.pid}")
        return True
    
    def is_bot_running(self) -> bool:
        """Check if bot process is still running"""
        if self.process is None:
            return False
        return self.process.poll() is None
    
    def is_bot_frozen(self) -> bool:
        """Check if bot is frozen (no heartbeat update)"""
        if not self.heartbeat_file.exists():
            return False  # First run, no heartbeat yet
        
        try:
            mtime = datetime.fromtimestamp(self.heartbeat_file.stat().st_mtime)
            # If heartbeat not updated in 5 minutes, consider frozen
            if datetime.now() - mtime > timedelta(minutes=5):
                return True
        except:
            pass
        return False
    
    def check_mt5_process(self) -> bool:
        """Check if MT5 terminal is running"""
        for proc in psutil.process_iter(['name']):
            if 'terminal64.exe' in proc.info['name'].lower():
                return True
        return False
    
    def start_mt5(self):
        """Attempt to start MT5 terminal"""
        mt5_paths = [
            r"C:\Program Files\MetaTrader 5\terminal64.exe",
            r"C:\Program Files (x86)\MetaTrader 5\terminal64.exe",
            os.path.expandvars(r"%APPDATA%\MetaQuotes\Terminal\*\terminal64.exe")
        ]
        
        for path in mt5_paths:
            if os.path.exists(path):
                logger.info(f"Starting MT5 from: {path}")
                subprocess.Popen([path], shell=True)
                time.sleep(10)  # Wait for MT5 to start
                return True
        
        logger.error("Could not find MT5 terminal executable")
        return False
    
    def check_system_resources(self) -> dict:
        """Check system resource usage"""
        return {
            'cpu_percent': psutil.cpu_percent(interval=1),
            'memory_percent': psutil.virtual_memory().percent,
            'disk_percent': psutil.disk_usage('/').percent if os.name != 'nt' else psutil.disk_usage('C:').percent
        }
    
    def kill_bot(self):
        """Force kill the bot process"""
        if self.process:
            try:
                self.process.terminate()
                self.process.wait(timeout=10)
            except:
                self.process.kill()
            self.process = None
    
    def can_restart(self) -> bool:
        """Check if we can restart (not too many restarts)"""
        one_hour_ago = datetime.now() - timedelta(hours=1)
        recent_restarts = [t for t in self.restart_times if t > one_hour_ago]
        return len(recent_restarts) < self.max_restarts_per_hour
    
    def run(self):
        """Main watchdog loop"""
        logger.info("=" * 60)
        logger.info("ICT Trading Bot Watchdog Started")
        logger.info("=" * 60)
        
        self.telegram.send_message("🐕 <b>WATCHDOG STARTED</b>\n\nMonitoring trading bot...")
        
        # Start the bot initially
        self.start_bot()
        
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
                    
                    if self.can_restart():
                        self.restart_count += 1
                        self.telegram.send_message(
                            f"🔄 <b>BOT CRASHED</b>\n\n"
                            f"Exit code: {exit_code}\n"
                            f"Restart #{self.restart_count}\n\n"
                            f"Restarting in 10 seconds..."
                        )
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
