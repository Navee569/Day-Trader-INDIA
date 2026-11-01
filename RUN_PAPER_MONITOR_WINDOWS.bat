@echo off
cd /d "%~dp0"
python -m india_trader paper-monitor watch --watchlist paper_watchlist.txt
echo.
pause
