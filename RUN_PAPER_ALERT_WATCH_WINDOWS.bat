@echo off
cd /d "%~dp0"
echo Checking paper alerts every 30 seconds during the normal NSE session.
echo Keep this window open. Press Ctrl+C to stop. No orders will be placed.
python -m india_trader alerts watch --every 30
echo.
pause
