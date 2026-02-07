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

echo Creating scheduled task...

schtasks /create ^
    /tn "ICT Trading Bot" ^
    /tr "C:\Users\Jeeva\OneDrive\Documents\Trading_Automation\.venv\Scripts\pythonw.exe C:\Users\Jeeva\OneDrive\Documents\Trading_Automation\backtest_engine\trading_scheduler.py" ^
    /sc weekly ^
    /d MON,TUE,WED,THU,FRI ^
    /st 03:30 ^
    /rl highest ^
    /f

if %errorLevel% equ 0 (
    echo.
    echo ================================================
    echo    SUCCESS! Task created.
    echo ================================================
    echo.
    echo The trading bot will start at 3:30 AM (Mon-Fri only).
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
