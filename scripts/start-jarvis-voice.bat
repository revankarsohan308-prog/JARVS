@echo off
rem J.A.R.V.S LINK voice plane — PC-side WebSocket server (port 8761).
rem Start the main backend (8760) first:  scripts\start-alyo.bat
rem Health check:  http://127.0.0.1:8761/health
rem The ESP32 LINK needs inbound TCP 8761 (scripts\open-firewall-port.ps1,
rem run as Administrator) and ALYO_APP_HOST=0.0.0.0 to be reachable on LAN.

cd /d "%~dp0.."

rem Kill any stale voice-server listener on 8761 first
powershell -ExecutionPolicy Bypass -Command "Get-NetTCPConnection -LocalPort 8761 -State Listen -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force }" 2>nul

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -m backend.voice.server
) else (
    python -m backend.voice.server
)