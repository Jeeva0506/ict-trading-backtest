@echo off
:: Start ORB Live Trading Bot — STELLAR 15K Account
:: Runs on a SEPARATE MT5 terminal instance

echo ================================================
echo    ORB Trading Bot - STELLAR 15K
echo    SPX500 / NDX100 / GER30 / FRA40 / US30
echo    (No US2000)
echo ================================================
echo.
echo This runs CONTINUOUSLY until you close the window.
echo The watchdog will auto-restart if bot crashes.
echo Press Ctrl+C to stop.
echo.

cd /d C:\Users\Jeeva\OneDrive\Documents\Trading_Automation\backtest_engine

:: Activate virtual environment and run with watchdog + account profile
call C:\Users\Jeeva\OneDrive\Documents\Trading_Automation\.venv\Scripts\activate.bat
python watchdog.py --account stellar15k

pause
