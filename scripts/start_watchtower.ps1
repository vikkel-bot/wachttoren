param(
    [switch]$NoBrowser,
    [int]$PreferredPort = 8011
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$VenvPython = Join-Path $Root ".venv\Scripts\python.exe"
$RuntimeDir = Join-Path $Root "runtime"
$LogsDir = Join-Path $Root "logs"
$DataDir = Join-Path $Root "data"
$PidFile = Join-Path $RuntimeDir "watchtower.server.pid"
$UrlFile = Join-Path $RuntimeDir "watchtower.server.url"

New-Item -ItemType Directory -Force -Path $RuntimeDir | Out-Null
New-Item -ItemType Directory -Force -Path $LogsDir | Out-Null
New-Item -ItemType Directory -Force -Path $DataDir | Out-Null

if (!(Test-Path $VenvPython)) {
    Write-Host "Eerste start: dependencies worden lokaal geinstalleerd..."
    & (Join-Path $Root "scripts\install_watchtower.ps1") -InstallDir $Root -NoShortcut
}

function Test-ProcessAlive {
    param([string]$Path)
    if (!(Test-Path $Path)) {
        return $false
    }
    $pidValue = (Get-Content -LiteralPath $Path -Raw).Trim()
    if (!$pidValue) {
        return $false
    }
    return [bool](Get-Process -Id ([int]$pidValue) -ErrorAction SilentlyContinue)
}

function Test-PortFree {
    param([int]$Port)
    $listener = $null
    try {
        $address = [System.Net.IPAddress]::Parse("127.0.0.1")
        $listener = [System.Net.Sockets.TcpListener]::new($address, $Port)
        $listener.Start()
        return $true
    } catch {
        return $false
    } finally {
        if ($listener) {
            $listener.Stop()
        }
    }
}

if (Test-ProcessAlive -Path $PidFile) {
    $url = (Get-Content -LiteralPath $UrlFile -Raw).Trim()
    if (!$NoBrowser -and $url) {
        Start-Process $url
    }
    Write-Host "Watchtower draait al op $url"
    exit 0
}

$port = $PreferredPort
while ($port -le ($PreferredPort + 20)) {
    if (Test-PortFree -Port $port) {
        break
    }
    $port += 1
}
if ($port -gt ($PreferredPort + 20)) {
    throw "Geen vrije poort gevonden tussen $PreferredPort en $($PreferredPort + 20)."
}

$env:WATCHTOWER_DB = Join-Path $DataDir "watchtower.db"
$outLog = Join-Path $LogsDir "watchtower.server.out.log"
$errLog = Join-Path $LogsDir "watchtower.server.err.log"
$arguments = @("-m", "uvicorn", "watchtower.main:app", "--host", "127.0.0.1", "--port", "$port")

$process = Start-Process -FilePath $VenvPython `
    -ArgumentList $arguments `
    -WorkingDirectory $Root `
    -WindowStyle Hidden `
    -RedirectStandardOutput $outLog `
    -RedirectStandardError $errLog `
    -PassThru

$url = "http://127.0.0.1:$port"
Set-Content -LiteralPath $PidFile -Value $process.Id
Set-Content -LiteralPath $UrlFile -Value $url

$ready = $false
for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Milliseconds 500
    try {
        $health = Invoke-RestMethod -Uri "$url/health" -TimeoutSec 2
        if ($health.status -eq "ok") {
            $ready = $true
            break
        }
    } catch {
    }
}

if (!$ready) {
    Write-Host "Watchtower start, maar health-check is nog niet klaar. Bekijk logs in: $LogsDir"
} else {
    Write-Host "Watchtower draait op $url"
}

if (!$NoBrowser) {
    Start-Process "$url/dashboard"
}
