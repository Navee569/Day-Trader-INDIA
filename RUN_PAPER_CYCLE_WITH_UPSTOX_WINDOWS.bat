@echo off
cd /d "%~dp0"
python run_with_upstox.py paper-cycle --watchlist paper_watchlist.txt --tick 0.10 --capital 100000 --out trader\paper_cycle_upstox.json
echo.
pause
