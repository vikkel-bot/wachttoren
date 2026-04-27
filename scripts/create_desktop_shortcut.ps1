param(
    [string]$InstallDir = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
)

$ErrorActionPreference = "Stop"

$installRoot = [System.IO.Path]::GetFullPath($InstallDir)
$desktop = [Environment]::GetFolderPath("Desktop")
$shortcutPath = Join-Path $desktop "Watchtower Dashboard.lnk"
$startScript = Join-Path $installRoot "scripts\start_watchtower.ps1"
$powershell = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"

if (!(Test-Path $startScript)) {
    throw "Startscript niet gevonden: $startScript"
}

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $powershell
$shortcut.Arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$startScript`""
$shortcut.WorkingDirectory = $installRoot
$shortcut.IconLocation = "$powershell,0"
$shortcut.Description = "Start Watchtower en open het dashboard"
$shortcut.Save()

Write-Host "Snelkoppeling gemaakt: $shortcutPath"
