@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Python environment not found. See README.md for setup.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m linptech_ble dashboard --open %*
if errorlevel 1 pause
