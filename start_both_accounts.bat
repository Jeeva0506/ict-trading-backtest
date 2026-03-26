@echo off
:: Start ORB Live Trading Bot - BOTH ACCOUNTS (Scheduler Mode)
:: Runs both STELLAR-2 and STELLAR-15K accounts simultaneously
:: Each runs in its own process with separate MT5 terminal instances

echo ================================================
echo    ORB Trading Bot - STARTING BOTH ACCOUNTS
echo    Account 1: STELLAR-2 (6K)
echo    Account 2: STELLAR-15K (15K)
echo ================================================
echo.
echo IMPORTANT:
echo  1. Both MT5 terminals must be running:
echo     - C:\Program Files\FundedNext MT5 Terminal\terminal64.exe (STELLAR-2)
echo     - C:\MT5_Stellar15K\terminal64.exe (STELLAR-15K)
echo  2. Auto Trading must be ENABLED in both terminals
echo  3. Both accounts will run CONTINUOUSLY until you stop them
echo  4. Each account has separate log files and trade journals
echo.
echo Press any key to continue or Ctrl+C to abort...
pause

cd /d C:\Users\Jeeva\OneDrive\Documents\Trading_Automation\backtest_engine

:: Activate virtual environment
call C:\Users\Jeeva\OneDrive\Documents\Trading_Automation\.venv\Scripts\activate.bat

:: Start Account 1 (STELLAR-2) in a separate window
echo.
echo [%date% %time%] Starting STELLAR-2 account...
start "ORB Trading - STELLAR-2" cmd /k python watchdog.py

:: Wait 2 seconds to stagger startup
timeout /t 2 /nobreak

:: Start Account 2 (STELLAR-15K) in a separate window
echo [%date% %time%] Starting STELLAR-15K account...
start "ORB Trading - STELLAR-15K" cmd /k python watchdog.py --account stellar15k

echo.
echo ================================================
echo Both accounts are now running in separate windows
echo Log files are at:
echo   STELLAR-2: live\logs\stellar-2\
echo   STELLAR-15K: live\logs\stellar-15k\
echo ================================================
echo.
echo Remember:
echo   - Keep both MT5 terminals open
echo   - Close individual windows to stop that account
echo   - Close both windows to stop all trading
echo.

pause
