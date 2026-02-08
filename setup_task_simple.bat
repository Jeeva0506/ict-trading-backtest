@echo off
:: Setup ICT Trading Bot Scheduled Task (Simple Version)
:: RUN THIS AS ADMINISTRATOR

echo ================================================
echo    ICT Trading Bot - Task Scheduler Setup
echo ================================================

:: Check for admin rights
net session >nul 2>&1
if %errorLevel% neq 0 (
    echo ERROR: Please run this script as Administrator!
    pause
    exit /b 1
)

:: Delete existing task if exists
schtasks /delete /tn "ICT_Trading_Bot" /f >nul 2>&1

echo Creating scheduled task...

:: Create task that runs Mon-Fri at 12:00 PM
schtasks /create /tn "ICT_Trading_Bot" /tr "\"C:\Users\Jeeva\OneDrive\Documents\Trading_Automation\.venv\Scripts\python.exe\" \"C:\Users\Jeeva\OneDrive\Documents\Trading_Automation\backtest_engine\run_live.py\"" /sc weekly /d MON,TUE,WED,THU,FRI /st 12:00 /ru "%USERNAME%" /rl HIGHEST /f

if %errorLevel% equ 0 (
    echo.
    echo SUCCESS! Task "ICT_Trading_Bot" created.
    echo Runs Mon-Fri at 12:00 PM
    echo.
    echo To start now: schtasks /run /tn "ICT_Trading_Bot"
    echo To check:     schtasks /query /tn "ICT_Trading_Bot" /v /fo list
    echo To delete:    schtasks /delete /tn "ICT_Trading_Bot" /f
) else (
    echo.
    echo ERROR: Failed. Trying alternative method...
    
    :: Alternative: Use PowerShell
    powershell -Command "& {$action = New-ScheduledTaskAction -Execute 'C:\Users\Jeeva\OneDrive\Documents\Trading_Automation\.venv\Scripts\python.exe' -Argument 'run_live.py' -WorkingDirectory 'C:\Users\Jeeva\OneDrive\Documents\Trading_Automation\backtest_engine'; $trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At 12:00PM; $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -WakeToRun -StartWhenAvailable; Register-ScheduledTask -TaskName 'ICT_Trading_Bot' -Action $action -Trigger $trigger -Settings $settings -Force}"
    
    if %errorLevel% equ 0 (
        echo SUCCESS via PowerShell!
    ) else (
        echo.
        echo MANUAL SETUP REQUIRED:
        echo 1. Press Win+R, type: taskschd.msc
        echo 2. Click "Create Basic Task"
        echo 3. Name: ICT_Trading_Bot
        echo 4. Trigger: Weekly, Mon-Fri at 12:00 PM
        echo 5. Action: Start program
        echo    Program: C:\Users\Jeeva\OneDrive\Documents\Trading_Automation\.venv\Scripts\python.exe
        echo    Arguments: run_live.py
        echo    Start in: C:\Users\Jeeva\OneDrive\Documents\Trading_Automation\backtest_engine
    )
)

echo.
pause
