@echo off
rem ============================================================
rem  J.A.R.V.S - Just A Rather Very Intelligent System
rem  One-click desktop launch: starts the backend if needed,
rem  then opens the desktop command center window.
rem ============================================================
setlocal
set "ROOT=%~dp0.."
set "DESKTOP=%ROOT%\apps\desktop"

rem 1) Ensure the backend is listening on port 8760.
powershell -NoProfile -Command "if (-not (Get-NetTCPConnection -LocalPort 8760 -State Listen -ErrorAction SilentlyContinue)) { exit 1 } else { exit 0 }"
if errorlevel 1 (
    echo Starting J.A.R.V.S backend...
    start "J.A.R.V.S Backend" /min cmd /c "cd /d ""%ROOT%"" && "".venv\Scripts\python.exe"" main.py"
    powershell -NoProfile -Command "for ($i=0; $i -lt 60 -and -not (Get-NetTCPConnection -LocalPort 8760 -State Listen -ErrorAction SilentlyContinue); $i++) { Start-Sleep 1 }"
    if errorlevel 1 (
        echo Backend did not come up on port 8760. Check docs\TROUBLESHOOTING.md.
        pause
        exit /b 1
    )
)

echo Backend is up. Launching J.A.R.V.S desktop...
start "" "%DESKTOP%\node_modules\electron\dist\electron.exe" "%DESKTOP%"
endlocal