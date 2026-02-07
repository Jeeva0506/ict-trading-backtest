"""
Live Trading Scheduler
======================
Runs the trading bot continuously during market hours.
This script manages the trading sessions for all 4 indices.

Trading Hours (IST):
- HK50:   03:30 - 10:00 (Asia session)
- GER30:  10:00 - 18:30 (Europe session)  
- FRA40:  10:00 - 18:30 (Europe session)
- SPX500: 22:00 - 05:30 (US session, next day)

Usage:
    python trading_scheduler.py

To run as a Windows service, use Task Scheduler:
1. Open Task Scheduler
2. Create Basic Task -> "ICT Trading Bot"
3. Trigger: Daily at 3:30 AM
4. Action: Start a program
   - Program: C:\Users\Jeeva\OneDrive\Documents\Trading_Automation\.venv\Scripts\python.exe
   - Arguments: trading_scheduler.py
   - Start in: C:\Users\Jeeva\OneDrive\Documents\Trading_Automation\backtest_engine
5. Check "Run whether user is logged on or not"
"""

import subprocess
import time
import sys
from datetime import datetime, timedelta
from pathlib import Path
import logging

# Setup logging
log_dir = Path(__file__).parent / 'live' / 'logs'
log_dir.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(log_dir / f'scheduler_{datetime.now().strftime("%Y%m%d")}.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Trading sessions (IST hours)
TRADING_SESSIONS = {
    'HK50': {'start': (3, 30), 'end': (10, 0)},
    'GER30': {'start': (10, 0), 'end': (18, 30)},
    'FRA40': {'start': (10, 0), 'end': (18, 30)},
    'SPX500': {'start': (22, 0), 'end': (5, 30)},  # Crosses midnight
}

# Overall trading window
START_HOUR = 3   # 3:30 AM IST
START_MINUTE = 30
END_HOUR = 2     # 2:30 AM IST (next day)
END_MINUTE = 30

# Check interval (minutes)
CHECK_INTERVAL = 5


def is_within_trading_hours():
    """Check if current time is within overall trading hours (3:30 AM - 2:30 AM IST)"""
    now = datetime.now()
    current_minutes = now.hour * 60 + now.minute
    
    start_minutes = START_HOUR * 60 + START_MINUTE  # 3:30 = 210 minutes
    end_minutes = END_HOUR * 60 + END_MINUTE  # 2:30 = 150 minutes
    
    # Trading window crosses midnight (3:30 AM to 2:30 AM next day)
    # Active from 3:30 AM to midnight, and midnight to 2:30 AM
    if current_minutes >= start_minutes or current_minutes < end_minutes:
        return True
    return False


def is_weekday():
    """Check if today is a weekday (Mon-Fri)"""
    return datetime.now().weekday() < 5


def get_active_sessions():
    """Get list of currently active trading sessions"""
    now = datetime.now()
    current_minutes = now.hour * 60 + now.minute
    active = []
    
    for symbol, hours in TRADING_SESSIONS.items():
        start_min = hours['start'][0] * 60 + hours['start'][1]
        end_min = hours['end'][0] * 60 + hours['end'][1]
        
        # Handle sessions that cross midnight (SPX500)
        if end_min < start_min:
            # Session crosses midnight
            if current_minutes >= start_min or current_minutes < end_min:
                active.append(symbol)
        else:
            # Normal session
            if start_min <= current_minutes < end_min:
                active.append(symbol)
    
    return active


def run_trading_bot():
    """Execute the trading bot"""
    try:
        script_path = Path(__file__).parent / 'run_live.py'
        python_path = Path(__file__).parent.parent / '.venv' / 'Scripts' / 'python.exe'
        
        logger.info("Starting trading bot...")
        
        result = subprocess.run(
            [str(python_path), str(script_path), '--live'],
            capture_output=True,
            text=True,
            timeout=300,  # 5 minute timeout
            cwd=str(Path(__file__).parent)
        )
        
        if result.returncode == 0:
            logger.info("Trading bot completed successfully")
            if result.stdout:
                for line in result.stdout.strip().split('\n')[-10:]:  # Last 10 lines
                    logger.info(f"  {line}")
        else:
            logger.error(f"Trading bot failed with code {result.returncode}")
            if result.stderr:
                logger.error(result.stderr)
                
    except subprocess.TimeoutExpired:
        logger.warning("Trading bot timed out (5 min limit)")
    except Exception as e:
        logger.error(f"Error running trading bot: {e}")


def main():
    """Main scheduler loop"""
    logger.info("=" * 60)
    logger.info("ICT Trading Scheduler Started")
    logger.info("=" * 60)
    logger.info(f"Trading Hours: {START_HOUR}:{START_MINUTE:02d} AM - {END_HOUR}:{END_MINUTE:02d} AM IST")
    logger.info(f"Check Interval: {CHECK_INTERVAL} minutes")
    logger.info("")
    
    last_run = None
    
    while True:
        try:
            now = datetime.now()
            
            # Check if we should be trading
            if not is_weekday():
                logger.info(f"Weekend - sleeping until Monday...")
                # Sleep until Monday 3:30 AM
                days_until_monday = (7 - now.weekday()) % 7
                if days_until_monday == 0:
                    days_until_monday = 7
                next_monday = now.replace(hour=START_HOUR, minute=START_MINUTE, second=0)
                next_monday += timedelta(days=days_until_monday)
                sleep_seconds = (next_monday - now).total_seconds()
                time.sleep(min(sleep_seconds, 3600))  # Max 1 hour sleep
                continue
            
            if not is_within_trading_hours():
                next_start = now.replace(hour=START_HOUR, minute=START_MINUTE, second=0)
                if now.hour >= END_HOUR and now.minute >= END_MINUTE:
                    next_start += timedelta(days=1)
                
                wait_minutes = (next_start - now).total_seconds() / 60
                logger.info(f"Outside trading hours. Next session in {wait_minutes:.0f} minutes")
                time.sleep(min(wait_minutes * 60, 1800))  # Max 30 min sleep
                continue
            
            # Get active sessions
            active = get_active_sessions()
            
            if active:
                logger.info(f"Active sessions: {', '.join(active)}")
                
                # Run the trading bot
                run_trading_bot()
                last_run = now
            else:
                logger.debug("No active sessions right now")
            
            # Wait for next check
            logger.info(f"Next check in {CHECK_INTERVAL} minutes...")
            time.sleep(CHECK_INTERVAL * 60)
            
        except KeyboardInterrupt:
            logger.info("Scheduler stopped by user")
            break
        except Exception as e:
            logger.error(f"Scheduler error: {e}")
            time.sleep(60)  # Wait 1 minute on error


if __name__ == '__main__':
    main()
