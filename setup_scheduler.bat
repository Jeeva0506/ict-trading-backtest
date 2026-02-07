@echo off
:: Setup ICT Trading Bot Scheduled Task
:: RUN THIS AS ADMINISTRATOR

echo ================================================
echo    ICT Trading Bot - Task Scheduler Setup
echo ================================================
echo.
echo This will create a Windows Task Scheduler task to:
echo - Start trading bot at 3:30 AM IST (Mon-Fri only)
echo - Run until 2:30 AM IST (next day)
echo - Trade SPX500, GER30, FRA40, HK50
echo.

:: Check for admin rights
net session >nul 2>&1
if %errorLevel% neq 0 (
    echo ERROR: Please run this script as Administrator!
    echo Right-click on this file and select "Run as administrator"
    pause
    exit /b 1
)

:: Delete existing task if exists
schtasks /delete /tn "ICT Trading Bot" /f >nul 2>&1

echo Creating scheduled task with optimized settings...
echo.

:: Use XML for advanced settings
schtasks /create /tn "ICT Trading Bot" /xml "C:\Users\Jeeva\OneDrive\Documents\Trading_Automation\backtest_engine\ICT_Trading_Task.xml" /f

if %errorLevel% equ 0 (
    echo.
    echo ================================================
    echo    SUCCESS! Task created with these settings:
    echo ================================================
    echo.
    echo    [x] Runs Mon-Fri at 3:30 AM
    echo    [x] Works on battery power
    echo    [x] Wakes PC from sleep
    echo    [x] No time limit (runs all day)
    echo    [x] Auto-restart on failure (3 attempts)
    echo    [x] Starts if missed (PC was off)
    echo    [x] Uses visible console (python.exe)
    echo.
    echo To start NOW, run:
    echo    schtasks /run /tn "ICT Trading Bot"
    echo.
    echo To check status:
    echo    schtasks /query /tn "ICT Trading Bot"
    echo.
    echo To delete:
    echo    schtasks /delete /tn "ICT Trading Bot" /f
) else (
    echo.
    echo ERROR: Failed to create task. Check permissions.
)

echo.
pause
