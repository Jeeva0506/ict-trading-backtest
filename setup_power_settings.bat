@echo off
:: Configure Windows Power Settings for 24/7 Trading Bot
:: RUN THIS AS ADMINISTRATOR

echo ================================================
echo    Power Settings for Trading Bot
echo ================================================
echo.

:: Check for admin rights
net session >nul 2>&1
if %errorLevel% neq 0 (
    echo ERROR: Please run this script as Administrator!
    pause
    exit /b 1
)

echo Configuring power settings...
echo.

:: Set power scheme to High Performance
powercfg /setactive 8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c 2>nul
if %errorLevel% neq 0 (
    echo Creating High Performance profile...
    powercfg /duplicatescheme 8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c
)

:: Lid close action = Do nothing (AC power)
powercfg /setacvalueindex SCHEME_CURRENT SUB_BUTTONS LIDACTION 0
:: Lid close action = Do nothing (Battery)
powercfg /setdcvalueindex SCHEME_CURRENT SUB_BUTTONS LIDACTION 0

:: Sleep = Never (AC)
powercfg /change standby-timeout-ac 0
:: Sleep = Never (Battery)  
powercfg /change standby-timeout-dc 0

:: Hibernate = Never
powercfg /change hibernate-timeout-ac 0
powercfg /change hibernate-timeout-dc 0

:: Display off = 30 min (AC) - saves power but doesn't affect bot
powercfg /change monitor-timeout-ac 30
powercfg /change monitor-timeout-dc 10

:: Disable USB selective suspend (prevents USB device disconnects)
powercfg /setacvalueindex SCHEME_CURRENT 2a737441-1930-4402-8d77-b2bebba308a3 48e6b7a6-50f5-4782-a5d4-53bb8f07e226 0

:: Apply changes
powercfg /setactive SCHEME_CURRENT

echo.
echo ================================================
echo    Power settings configured!
echo ================================================
echo.
echo [x] Lid close = Do nothing
echo [x] Sleep = Never
echo [x] Hibernate = Disabled
echo [x] USB suspend = Disabled
echo.
echo Your laptop will now stay awake for trading!
echo.

pause
