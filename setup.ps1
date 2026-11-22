# Creates the local Python environment for the kg CLI and the app.
#   .\setup.ps1                 core setup (works with the claude-code, anthropic and openai providers)
#   .\setup.ps1 -WithCopilot    also install the GitHub Copilot SDK and its runtime (tutor.provider: copilot)
#   .\setup.ps1 -NoHooks        don't turn on the repository's git hooks
param([switch]$WithCopilot, [switch]$NoHooks)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

# Native commands don't throw on failure in PowerShell: check every exit code and name the step that failed.
function Step([string]$what, [scriptblock]$cmd) {
    Write-Host "-> $what"
    & $cmd
    if ($LASTEXITCODE) { throw "setup failed at: $what (exit code $LASTEXITCODE)" }
}

if (-not (Test-Path .venv)) {
    if (Get-Command py -ErrorAction SilentlyContinue) { Step "create .venv" { py -3 -m venv .venv } }
    else { Step "create .venv" { python -m venv .venv } }
}
$py = ".\.venv\Scripts\python.exe"
Step "check Python version (3.12+)" { & $py -c "import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)" }
Step "upgrade pip" { & $py -m pip install --quiet --upgrade pip }
Step "install requirements.txt" { & $py -m pip install --quiet -r requirements.txt }
if ($WithCopilot) {
    Step "install the Copilot SDK" { & $py -m pip install --quiet -r requirements-copilot.txt }
    Step "download the Copilot runtime" { & $py -m copilot download-runtime }
}
if (-not $NoHooks) {
    # This makes git run .githooks/pre-commit on every commit in this clone. Read it first if you didn't write it.
    Step "enable git hooks (.githooks/pre-commit: kg check + private-file guard)" { git config core.hooksPath .githooks }
}
Step "kg check" { .\kg.cmd check }
Step "kg build" { .\kg.cmd build }
Write-Host "Ready. Try: .\kg.cmd serve --open   (graph + AI tutor)   |   .\kg.cmd next"
