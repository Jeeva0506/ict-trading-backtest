@echo off
:: Restart STELLAR-3 Watchdog after session
:: Run this ONLY after all positions are closed (off-hours)
:: ========================================================

echo ================================================
echo  STELLAR-3 Watchdog Restart
echo  Safe only when no open positions!
echo ================================================
echo.
echo Checking for running bot process...

:: Read PID from bot.lock
set LOCK=C:\Users\Jeeva\OneDrive\Documents\Trading_Automation\backtest_engine\live\logs\stellar-3\bot.lock
set WLOCK=C:\Users\Jeeva\OneDrive\Documents\Trading_Automation\backtest_engine\live\logs\stellar-3\watchdog.lock

if exist "%LOCK%" (
    set /p BOT_PID=<"%LOCK%"
    echo Found bot.lock with PID: %BOT_PID%
    echo Killing old bot process...
    taskkill /PID %BOT_PID% /F >nul 2>&1
    del /F /Q "%LOCK%" >nul 2>&1
    echo Old bot stopped.
) else (
    echo No bot.lock found - bot already stopped.
)

:: Clear stale watchdog lock
if exist "%WLOCK%" (
    del /F /Q "%WLOCK%" >nul 2>&1
)

echo.
echo Starting fresh watchdog + bot...
echo (Will auto-start bot when trading window opens)
echo.

cd /d C:\Users\Jeeva\OneDrive\Documents\Trading_Automation\backtest_engine
call C:\Users\Jeeva\OneDrive\Documents\Trading_Automation\.venv\Scripts\activate.bat
python watchdog.py --account stellar3

pause
