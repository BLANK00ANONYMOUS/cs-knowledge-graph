# Creates the local Python environment for the kg CLI.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
if (-not (Test-Path .venv)) {
    if (Get-Command py -ErrorAction SilentlyContinue) { py -3 -m venv .venv } else { python -m venv .venv }
}
.\.venv\Scripts\python.exe -m pip install --quiet --upgrade pip
.\.venv\Scripts\python.exe -m pip install --quiet -r requirements.txt
.\.venv\Scripts\python.exe -m copilot download-runtime | Out-Null   # Copilot SDK runtime for `kg serve`
git config core.hooksPath .githooks   # pre-commit runs `kg check`
.\kg.cmd check
.\kg.cmd build
Write-Host "Ready. Try: .\kg.cmd serve --open   (graph + AI tutor)   |   .\kg.cmd next"
