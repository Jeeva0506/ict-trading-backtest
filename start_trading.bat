@echo off
:: Start ICT Live Trading Bot with Watchdog
:: Run this script to start automated live trading (runs continuously)

echo ================================================
echo    ICT Trading Bot - Starting with Watchdog...
echo ================================================
echo.
echo This runs CONTINUOUSLY until you close the window.
echo The watchdog will auto-restart if bot crashes.
echo Press Ctrl+C to stop.
echo.

cd /d C:\Users\Jeeva\OneDrive\Documents\Trading_Automation\backtest_engine

:: Activate virtual environment and run with watchdog
call C:\Users\Jeeva\OneDrive\Documents\Trading_Automation\.venv\Scripts\activate.bat
python watchdog.py

pause
