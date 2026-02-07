@echo off
:: Start ICT Trading Scheduler
:: Run this script to start automated live trading

echo ================================================
echo    ICT Trading Bot - Starting...
echo ================================================
echo.

cd /d C:\Users\Jeeva\OneDrive\Documents\Trading_Automation\backtest_engine

:: Activate virtual environment and run scheduler
call C:\Users\Jeeva\OneDrive\Documents\Trading_Automation\.venv\Scripts\activate.bat
python trading_scheduler.py

pause
