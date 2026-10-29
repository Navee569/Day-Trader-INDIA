@echo off
cd /d "%~dp0"
python -m india_trader paper-cycle --watchlist paper_watchlist.txt --tick 0.10 --capital 100000 --risk-pct 0.5 --max-alloc-pct 20 --slippage-bps 10 --fee-buffer 50 --out trader\paper_cycle.json
if errorlevel 1 (
  echo Paper research cycle failed.
  exit /b 1
)
echo Saved result to trader\paper_cycle.json
