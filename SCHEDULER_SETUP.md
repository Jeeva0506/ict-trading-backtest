# Running Both Accounts with Windows Task Scheduler

## Quick Start Options

### Option 1: Manual Start (Recommended for Testing)
Run this to start both accounts immediately in visible windows:
```
Double-click: start_both_accounts.bat
```
Both accounts will run in separate command windows until you close them.

### Option 2: Scheduler Start (Silent, Background)
For immediate silent start without console windows:
```
start_both_accounts_scheduler.bat
```

---

## Setting Up Windows Task Scheduler (For Auto-Startup)

### Prerequisites
- Both MT5 terminals installed and logged in
- Both `.env` files configured (`.env` and `.env.stellar15k`)
- Python virtual environment activated

### Steps to Schedule Auto-Start on Boot

#### 1. Open Task Scheduler
- Press **Win + R**, type `taskschd.msc`, and press Enter
- Or: Control Panel → Admin Tools → Task Scheduler

#### 2. Create Basic Task
- Right-click on "Task Scheduler Library" → **New Basic Task...**
- Name: `ORB Trading - Both Accounts`
- Description: `Starts both STELLAR-2 and STELLAR-15K trading accounts`
- Click **Next**

#### 3. Set Trigger
- Select: **At log on**
- Click **Next**
- (Or choose **On a schedule** if you want a specific time)

#### 4. Set Action
- Select: **Start a program**
- Program/script: `C:\Users\Jeeva\OneDrive\Documents\Trading_Automation\backtest_engine\start_both_accounts_scheduler.bat`
- Start in: `C:\Users\Jeeva\OneDrive\Documents\Trading_Automation\backtest_engine`
- Click **Next**

#### 5. Finish
- Check **Run with highest privileges**
- Click **Finish**

#### 6. Configure (Optional)
- Right-click the new task → **Properties**
- **General tab:**
  - Check "Run with highest privileges"
  - Select: "Run whether user is logged in or not"
- **Triggers tab:**
  - Modify trigger: Check "Repeat task every 30 minutes" (for recovery if one process dies)
  - Duration: "For a duration of 1 day"
- **Conditions tab:**
  - Uncheck "Start the task only if the computer is on AC power"
  - Uncheck "Stop if the computer switches to battery power"
- **Settings tab:**
  - Check "Allow task to be run on demand"
  - Check "If the task fails, restart every: 5 minutes"
  - Number of times to retry: 3
- Click **OK**

### Test the Task
1. Right-click the task → **Run** 
2. Check `live/logs/scheduler_startup.log` for confirmation
3. Verify both MT5 terminals are trading

---

## Monitoring Both Accounts

### Log Files Location
```
live/logs/stellar-2/
  ├── trading.log          # Main bot trades & decisions
  ├── watchdog.log         # Process monitoring
  ├── heartbeat.txt        # Last ping time
  ├── position_state.json  # Current open positions
  └── edge_guard.json      # Risk parameters

live/logs/stellar-15k/
  ├── trading.log
  ├── watchdog.log
  ├── heartbeat.txt
  ├── position_state.json
  └── edge_guard.json

scheduler_startup.log      # Task Scheduler startup history
```

### Check Running Processes
```powershell
# PowerShell command to check both watchdogs
Get-Process | Where-Object {$_.Name -eq "python"} | Select-Object Name, Id, Memory
```

### Check MT5 Terminals
Both terminals must be running:
- **STELLAR-2**: C:\Program Files\FundedNext MT5 Terminal\terminal64.exe
- **STELLAR-15K**: C:\MT5_Stellar15K\terminal64.exe

---

## Stopping the Accounts

### Method 1: Task Scheduler (Most Reliable)
1. Open Task Scheduler
2. Find `ORB Trading - Both Accounts`
3. Right-click → **End**

### Method 2: From Command Line
```powershell
# Kill both watchdog processes
taskkill /F /IM python.exe
```

### Method 3: Manual
- Close the command windows for each account
- Or close both MT5 terminals (which will cause watchdog to exit)

---

## Troubleshooting

| Problem | Solution |
|---------|----------|
| One account not trading | Check its MT5 terminal is open and logged in |
| Process exited immediately | Check Task Scheduler history for errors |
| Can't find task in logs | Verify path in Task Scheduler matches exactly |
| Need to switch to different account | Stop the task, swap .env files, restart |
| Trades going to one account only | Verify both .env files have unique MT5_LOGIN values |

---

## Advanced: Custom Recovery Script

If you want more advanced recovery (e.g., restart if one account stops), create `recover_both_accounts.ps1`:

```powershell
# Run in PowerShell as Admin every 5 minutes via Task Scheduler
$path = "C:\Users\Jeeva\OneDrive\Documents\Trading_Automation\backtest_engine"
$stellar2_heartbeat = "$path\live\logs\stellar-2\heartbeat.txt"
$stellar15k_heartbeat = "$path\live\logs\stellar-15k\heartbeat.txt"

#Check if heartbeats are stale (> 6 minutes old)
# If stale, restart that account...
```

Then schedule this PowerShell script to run every 5 minutes.

---

## Quick Reference

**To start now:** Double-click → `start_both_accounts.bat`

**To schedule:** Use Task Scheduler wizard (steps above) with these settings:
- Trigger: At log on (or specific time)
- Program: `start_both_accounts_scheduler.bat`
- Working dir: `<Trading_Automation>\backtest_engine`
- Run with highest privileges
- Run whether logged in or not
