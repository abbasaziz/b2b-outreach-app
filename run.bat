@echo off
setlocal

REM ── Always run from the folder this .bat lives in ──
cd /d "%~dp0"

REM ── Sanity check: is Python installed? ──
where python >nul 2>nul
if errorlevel 1 (
  echo.
  echo  ERROR: Python is not installed or not in your PATH.
  echo  Install Python 3.11+ from python.org and tick "Add python.exe to PATH".
  echo.
  pause
  exit /b 1
)

REM ── Sanity check: does the venv exist? ──
if not exist ".venv\Scripts\activate.bat" (
  echo.
  echo  ERROR: Virtual environment not found.
  echo  Run this once from this folder:
  echo      python -m venv .venv
  echo      .venv\Scripts\activate.bat
  echo      pip install flask openpyxl pyyaml
  echo.
  pause
  exit /b 1
)

REM ── Activate the venv ──
call ".venv\Scripts\activate.bat"

REM ── Make sure the DB is initialised (safe to run every time) ──
python -c "from db import init; init()" 2>nul

REM ── Open the browser after 2 seconds, in the background ──
start "" /b cmd /c "timeout /t 2 /nobreak >nul & start http://127.0.0.1:5000"

REM ── Start the server (this line keeps the window open) ──
echo.
echo  MoverSync Outreach is starting...
echo  Open http://127.0.0.1:5000 in your browser.
echo  To stop the server, close this window or press Ctrl+C.
echo.
python app.py

REM ── If Flask exits for any reason, keep the window open so you can see the error ──
echo.
echo  Server stopped. Press any key to close.
pause >nul