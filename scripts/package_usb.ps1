param(
    [string]$OutputDir = "",
    [switch]$NoZip,
    [switch]$WithoutDatabase
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$DistRoot = Join-Path $Root "dist"
if (!$OutputDir) {
    $OutputDir = Join-Path $DistRoot "WatchtowerUSB"
}

$distFull = [System.IO.Path]::GetFullPath($DistRoot)
$outputFull = [System.IO.Path]::GetFullPath($OutputDir)
$distPrefix = $distFull.TrimEnd("\") + "\"

New-Item -ItemType Directory -Force -Path $DistRoot | Out-Null

if (!$outputFull.StartsWith($distPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Veiligheidsstop: OutputDir moet binnen de dist-map staan: $distFull"
}

if (Test-Path $outputFull) {
    $resolvedOutput = (Resolve-Path $outputFull).Path
    if (!$resolvedOutput.StartsWith($distPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Veiligheidsstop: bestaande output valt buiten dist."
    }
    Remove-Item -LiteralPath $resolvedOutput -Recurse -Force
}

New-Item -ItemType Directory -Force -Path $outputFull | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $outputFull "data") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $outputFull "logs") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $outputFull "runtime") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $outputFull "wheels") | Out-Null

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
    $src = Join-Path $Root $item
    if (Test-Path $src) {
        Copy-Item -LiteralPath $src -Destination (Join-Path $outputFull $item) -Recurse -Force
    }
}

$cacheDirs = Get-ChildItem -Path $outputFull -Directory -Recurse -Force -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -eq "__pycache__" }
foreach ($dir in $cacheDirs) {
    Remove-Item -LiteralPath $dir.FullName -Recurse -Force
}

if (!$WithoutDatabase) {
    $sourceDb = Join-Path $Root "watchtower.db"
    if (Test-Path $sourceDb) {
        Copy-Item -LiteralPath $sourceDb -Destination (Join-Path $outputFull "data\watchtower.db") -Force
    }
}

$readme = @"
WATCHTOWER USB PAKKET

1. Kopieer deze hele map naar een USB-stick.
2. Op een andere Windows PC: open de map en dubbelklik op INSTALLEER_WATCHTOWER.bat.
3. De installer kopieert Watchtower naar de lokale PC, installeert dependencies en maakt een bureaubladsnelkoppeling.
4. Start daarna via de snelkoppeling 'Watchtower Dashboard'.

Zonder internet op de doel-PC:
- Run eerst scripts\download_wheels.ps1 op een PC met internet.
- Maak daarna opnieuw het USB-pakket of kopieer de gevulde wheels-map mee.
"@
Set-Content -LiteralPath (Join-Path $outputFull "WATCHTOWER_USB_LEESMIJ.txt") -Value $readme

if (!$NoZip) {
    $zipPath = Join-Path $DistRoot "WatchtowerUSB.zip"
    if (Test-Path $zipPath) {
        Remove-Item -LiteralPath $zipPath -Force
    }
    Compress-Archive -Path (Join-Path $outputFull "*") -DestinationPath $zipPath -Force
}

Write-Host "USB-pakket gemaakt: $outputFull"
if (!$NoZip) {
    Write-Host "Zip gemaakt: $(Join-Path $DistRoot 'WatchtowerUSB.zip')"
}
