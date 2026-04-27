$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$PidFile = Join-Path $Root "runtime\watchtower.server.pid"

if (!(Test-Path $PidFile)) {
    Write-Host "Geen Watchtower PID gevonden."
    exit 0
}

$pidValue = (Get-Content -LiteralPath $PidFile -Raw).Trim()
if (!$pidValue) {
    Remove-Item -LiteralPath $PidFile -Force
    Write-Host "Leeg PID-bestand verwijderd."
    exit 0
}

$process = Get-Process -Id ([int]$pidValue) -ErrorAction SilentlyContinue
if (!$process) {
    Remove-Item -LiteralPath $PidFile -Force
    Write-Host "Watchtower draaide niet meer."
    exit 0
}

Stop-Process -Id $process.Id -Force
Remove-Item -LiteralPath $PidFile -Force
Write-Host "Watchtower is gestopt."
