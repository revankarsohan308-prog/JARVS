@echo off
rem Stop a stale ALYO listener (port conflict is a known cause of "old code")
powershell -ExecutionPolicy Bypass -File "%~dp0stop-alyo.ps1" %*

rem Start ALYO with the venv interpreter
cd /d "%~dp0.."
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" main.py
) else (
    python main.py
)