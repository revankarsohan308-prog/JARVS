# ALYO: open TCP 8760 for inbound LAN access (run elevated, once).
# Usage:  right-click -> Run with PowerShell, or:
#         Start-Process powershell -Verb RunAs -ArgumentList '-File', $PSCommandPath
$ErrorActionPreference = 'Stop'
$port = 8760
$name = 'ALYO (TCP 8760)'
try {
    if (Get-NetFirewallRule -DisplayName $name -ErrorAction SilentlyContinue) {
        "EXISTS: $name"
    } else {
        New-NetFirewallRule -DisplayName $name -Direction Inbound -Protocol TCP -LocalPort $port -Action Allow -Profile Any | Out-Null
        "CREATED: $name (TCP $port)"
    }
    Set-Content -Path "$env:TEMP\alyo_fw_result.txt" -Value "OK"
} catch {
    Set-Content -Path "$env:TEMP\alyo_fw_result.txt" -Value "ERROR: $($_.Exception.Message)"
}
Start-Sleep -Seconds 2