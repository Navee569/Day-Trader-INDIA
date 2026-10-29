@echo off
cd /d "%~dp0"
python -c "import sys; assert sys.version_info >= (3, 11), 'Python 3.11 or newer required'"
if errorlevel 1 (
  echo Python 3.11 or newer is unavailable.
  exit /b 1
)
python -m unittest discover -s tests -v
if errorlevel 1 (
  echo TESTS FAILED.
  exit /b 1
)
python -m india_trader doctor
