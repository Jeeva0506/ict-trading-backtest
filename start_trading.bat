@echo off
:: Start ICT Live Trading Bot
:: Run this script to start automated live trading (runs continuously)

echo ================================================
echo    ICT Trading Bot - Starting...
echo ================================================
echo.
echo This runs CONTINUOUSLY until you close the window.
echo Press Ctrl+C to stop.
echo.

cd /d C:\Users\Jeeva\OneDrive\Documents\Trading_Automation\backtest_engine

:: Activate virtual environment and run live trading
call C:\Users\Jeeva\OneDrive\Documents\Trading_Automation\.venv\Scripts\activate.bat
python run_live.py

pause
