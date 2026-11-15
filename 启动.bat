@echo off
cd /d "%~dp0"
echo Starting ass-font-fetch (source mode, requires Python 3.9+) ...
python gui_server.py %*
if errorlevel 1 (
  echo.
  echo Failed. Check that Python 3.9+ is installed and "python" is on PATH.
  echo You can also run:  python "%~dp0gui_server.py"
  pause
)
