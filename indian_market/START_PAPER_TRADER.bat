@echo off
title ORB Sweep Paper Trader
echo ============================================
echo   ORB SWEEP PAPER TRADER - Starting...
echo   Capital: Rs 3,00,000 - PAPER MODE
echo ============================================
echo.
cd /d "%~dp0"
python start_paper_trading.py
pause
