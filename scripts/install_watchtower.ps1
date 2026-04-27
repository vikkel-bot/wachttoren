param(
    [string]$InstallDir = "$env:LOCALAPPDATA\Watchtower",
    [switch]$NoShortcut,
    [switch]$Start
)

$ErrorActionPreference = "Stop"

function Get-ProjectRoot {
    return (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
}

function Copy-WatchtowerApp {
    param(
        [string]$SourceRoot,
        [string]$TargetRoot
    )

    $sourceFull = [System.IO.Path]::GetFullPath($SourceRoot)
    $targetFull = [System.IO.Path]::GetFullPath($TargetRoot)
    if ($sourceFull.TrimEnd("\") -ieq $targetFull.TrimEnd("\")) {
        return
    }

    New-Item -ItemType Directory -Force -Path $targetFull | Out-Null
    $items = @(
        "watchtower",
        "scripts",
        "tests",
        "requirements.txt",
        "README.md",
        "INSTALL_USB_NL.md",
        "START_WATCHTOWER.bat",
        "STOP_WATCHTOWER.bat",
        "INSTALLEER_WATCHTOWER.bat"
    )

    foreach ($item in $items) {
        $src = Join-Path $sourceFull $item
        if (!(Test-Path $src)) {
            continue
        }
        $dst = Join-Path $targetFull $item
        if (Test-Path $dst) {
            Remove-Item -LiteralPath $dst -Recurse -Force
        }
        Copy-Item -LiteralPath $src -Destination $dst -Recurse -Force
    }

    New-Item -ItemType Directory -Force -Path (Join-Path $targetFull "data") | Out-Null
    $sourceDb = Join-Path $sourceFull "data\watchtower.db"
    $targetDb = Join-Path $targetFull "data\watchtower.db"
    if ((Test-Path $sourceDb) -and !(Test-Path $targetDb)) {
        Copy-Item -LiteralPath $sourceDb -Destination $targetDb -Force
    }

    $sourceWheels = Join-Path $sourceFull "wheels"
    if (Test-Path $sourceWheels) {
        $targetWheels = Join-Path $targetFull "wheels"
        if (Test-Path $targetWheels) {
            Remove-Item -LiteralPath $targetWheels -Recurse -Force
        }
        Copy-Item -LiteralPath $sourceWheels -Destination $targetWheels -Recurse -Force
    }
}

function Get-CompatiblePython {
    $checks = @(
        @{ File = "py"; Args = @("-3.13") },
        @{ File = "py"; Args = @("-3.12") },
        @{ File = "py"; Args = @("-3.11") },
        @{ File = "python"; Args = @() }
    )

    foreach ($check in $checks) {
        if (!(Get-Command $check.File -ErrorAction SilentlyContinue)) {
            continue
        }
        $probe = @($check.Args) + @("-c", "import sys; print(sys.executable); print(f'{sys.version_info.major}.{sys.version_info.minor}')")
        $output = & $check.File @probe 2>$null
        if ($LASTEXITCODE -ne 0 -or !$output -or $output.Count -lt 2) {
            continue
        }
        $version = [version]$output[1]
        if ($version.Major -eq 3 -and $version.Minor -ge 11) {
            return $output[0]
        }
    }

    throw "Geen geschikte Python gevonden. Installeer Python 3.11 of nieuwer vanaf https://www.python.org/downloads/ en start dit script opnieuw."
}

$sourceRoot = Get-ProjectRoot
$targetRoot = [System.IO.Path]::GetFullPath($InstallDir)

Copy-WatchtowerApp -SourceRoot $sourceRoot -TargetRoot $targetRoot

New-Item -ItemType Directory -Force -Path (Join-Path $targetRoot "data") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $targetRoot "logs") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $targetRoot "runtime") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $targetRoot "wheels") | Out-Null

$venvPython = Join-Path $targetRoot ".venv\Scripts\python.exe"
if (!(Test-Path $venvPython)) {
    $python = Get-CompatiblePython
    Write-Host "Python gevonden: $python"
    & $python -m venv (Join-Path $targetRoot ".venv")
}

$requirements = Join-Path $targetRoot "requirements.txt"
$wheels = @(Get-ChildItem -Path (Join-Path $targetRoot "wheels") -Filter "*.whl" -ErrorAction SilentlyContinue)
if ($wheels.Count -gt 0) {
    Write-Host "Dependencies installeren vanuit lokale wheelhouse..."
    & $venvPython -m pip install --no-index --find-links (Join-Path $targetRoot "wheels") -r $requirements
} else {
    Write-Host "Dependencies installeren via internet..."
    & $venvPython -m pip install -r $requirements
}

if (!$NoShortcut) {
    & (Join-Path $targetRoot "scripts\create_desktop_shortcut.ps1") -InstallDir $targetRoot
}

Write-Host ""
Write-Host "Watchtower is geinstalleerd in: $targetRoot"
Write-Host "Starten kan via de bureaubladsnelkoppeling of via START_WATCHTOWER.bat."

if ($Start) {
    & (Join-Path $targetRoot "scripts\start_watchtower.ps1")
}
