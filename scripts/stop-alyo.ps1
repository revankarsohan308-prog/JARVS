# Stop every ALYO listener on the configured port (default 8760).
# Fixes the classic "two servers, one port" confusion that makes the browser
# hit stale code. Only stops processes that are actually listening on the port.
#
# Usage:  powershell -File scripts\stop-alyo.ps1
param(
    [int]$Port = 8760,
    [switch]$Force
)

$ErrorActionPreference = "Continue"

$listeners = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if (-not $listeners) {
    Write-Host "No listener on port $Port — nothing to stop." -ForegroundColor Green
    exit 0
}

$ids = $listeners | Select-Object -ExpandProperty OwningProcess -Unique
foreach ($id in $ids) {
    $p = Get-Process -Id $id -ErrorAction SilentlyContinue
    if (-not $p) { continue }
    $cmdline = (Get-CimInstance Win32_Process -Filter "ProcessId=$id" -ErrorAction SilentlyContinue).CommandLine
    Write-Host "Stopping PID $id ($($p.ProcessName)) listening on :$Port" -ForegroundColor Yellow
    if ($cmdline) { Write-Host "    command: $cmdline" -ForegroundColor DarkGray }
    try {
        Stop-Process -Id $id -Force
        Write-Host "    stopped." -ForegroundColor Green
    } catch {
        Write-Host "    FAILED: $($_.Exception.Message)" -ForegroundColor Red
    }
}

Start-Sleep -Milliseconds 500
$left = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($left) {
    Write-Host "Port $Port is still in use! Remaining PIDs: $($left.OwningProcess -join ', ')" -ForegroundColor Red
    exit 1
}
Write-Host "Port $Port is free. You can start ALYO with:  python main.py" -ForegroundColor Green
exit 0