@echo off
if not exist "%~dp0.venv\Scripts\python.exe" (
  echo kg: run setup.ps1 first ^(no .venv found^) 1>&2
  exit /b 1
)
"%~dp0.venv\Scripts\python.exe" "%~dp0tools\kg.py" %*
exit /b %errorlevel%
