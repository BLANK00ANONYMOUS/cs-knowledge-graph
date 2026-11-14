# Installs Start Menu + Desktop shortcuts so the app opens like any other application.
#   .\tools\install.ps1                 shortcuts only
#   .\tools\install.ps1 -AutoStart      also start the server (hidden) when you sign in to Windows
#   .\tools\install.ps1 -Uninstall      remove everything this script created
param([switch]$AutoStart, [switch]$Uninstall)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$py = Join-Path $root ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $py)) { throw "Create the virtual environment first (see README: Setup)." }
$launcher = Join-Path $root "tools\launcher.py"
$start = Join-Path ([Environment]::GetFolderPath("Programs")) "Knowledge Graph.lnk"
$stop = Join-Path ([Environment]::GetFolderPath("Programs")) "Knowledge Graph (stop server).lnk"
$desk = Join-Path ([Environment]::GetFolderPath("Desktop")) "Knowledge Graph.lnk"
$auto = Join-Path ([Environment]::GetFolderPath("Startup")) "Knowledge Graph server.lnk"

if ($Uninstall) {
    foreach ($f in $start, $stop, $desk, $auto) { Remove-Item $f -ErrorAction SilentlyContinue }
    Write-Host "Removed shortcuts."; return
}

function New-Shortcut($path, $arguments, $description) {
    $sh = (New-Object -ComObject WScript.Shell).CreateShortcut($path)
    $sh.TargetPath = $py
    $sh.Arguments = "`"$launcher`" $arguments"
    $sh.WorkingDirectory = $root
    $sh.Description = $description
    $sh.IconLocation = "$env:SystemRoot\System32\shell32.dll,13"
    $sh.Save()
}
New-Shortcut $start "" "Knowledge Graph — learn, quiz, build, jobs"
New-Shortcut $desk "" "Knowledge Graph — learn, quiz, build, jobs"
New-Shortcut $stop "--stop" "Stop the Knowledge Graph background server"
if ($AutoStart) {
    $sh = (New-Object -ComObject WScript.Shell).CreateShortcut($auto)
    $sh.TargetPath = (Join-Path $root ".venv\Scripts\pythonw.exe")
    $sh.Arguments = "`"$launcher`" --server-only"
    $sh.WorkingDirectory = $root
    $sh.WindowStyle = 7
    $sh.Save()
}
Write-Host "Installed. Press the Windows key and type 'Knowledge Graph'."
