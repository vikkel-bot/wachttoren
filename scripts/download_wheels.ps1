param(
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$WheelsDir = Join-Path $Root "wheels"
$Requirements = Join-Path $Root "requirements.txt"

New-Item -ItemType Directory -Force -Path $WheelsDir | Out-Null

Write-Host "Python wheels downloaden naar: $WheelsDir"
& $Python -m pip download -r $Requirements -d $WheelsDir

Write-Host "Klaar. Deze wheels worden gebruikt voor offline installatie als ze op de USB-stick staan."
