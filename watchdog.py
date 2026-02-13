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
        def run(self):
            """Watchdog disabled: does nothing and exits immediately."""
            logger.info("WATCHDOG DISABLED - Exiting immediately, no bot will be started or restarted.")
            return
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
