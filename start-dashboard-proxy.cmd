@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Python environment not found. See docs\ESPHOME_PROXY.md.
  pause
  exit /b 1
)
if not exist "transport.local.json" (
  echo Missing transport.local.json. Copy examples\transport.example.json and edit its placeholders.
  echo See docs\ESPHOME_PROXY.md. Your sensors.local.json is kept separately.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m linptech_ble dashboard --config sensors.local.json --transport-config transport.local.json --open %*
if errorlevel 1 pause
