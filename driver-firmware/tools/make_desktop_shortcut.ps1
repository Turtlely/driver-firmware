<#
.SYNOPSIS
    Creates a Windows desktop shortcut for the spectrometer application.

.DESCRIPTION
    Points the shortcut at the project's .venv Python (falling back to
    whatever "python" resolves to on PATH if .venv is missing) running
    tools\spectrometer_app.py, using pythonw.exe so no console window
    opens alongside the GUI. Icon is tools\spectrometer\icon.ico.

    Safe to re-run: it overwrites its own shortcut in place rather than
    creating duplicates.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File tools\make_desktop_shortcut.ps1
#>

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$venvPythonw = Join-Path $repoRoot ".venv\Scripts\pythonw.exe"
$launcher = Join-Path $repoRoot "tools\spectrometer_app.py"
$icon = Join-Path $repoRoot "tools\spectrometer\icon.ico"

if (-not (Test-Path $launcher)) {
    throw "Launcher not found: $launcher"
}
if (-not (Test-Path $icon)) {
    throw "Icon not found: $icon (expected a built icon.ico next to icon.jpg)"
}

$pythonExe = if (Test-Path $venvPythonw) {
    $venvPythonw
} else {
    Write-Warning ".venv\Scripts\pythonw.exe not found; falling back to 'pythonw' on PATH."
    "pythonw"
}

$desktop = [Environment]::GetFolderPath("Desktop")
$shortcutPath = Join-Path $desktop "Spectrometer.lnk"

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $pythonExe
$shortcut.Arguments = '"' + $launcher + '"'
$shortcut.WorkingDirectory = $repoRoot
$shortcut.IconLocation = $icon
$shortcut.Description = "S3903-1024Q spectrometer application"
$shortcut.Save()

Write-Output "Created: $shortcutPath"
Write-Output "  target: $pythonExe $launcher"
Write-Output "  icon:   $icon"
