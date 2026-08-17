@echo off
REM Windows launcher. Equivalent to ./grab on macOS and Linux.
REM   grab.bat --web              open the browser interface
REM   grab.bat <url> [<url> ...]  download from the command line
setlocal
set "HERE=%~dp0"
set "PY=%HERE%.venv\Scripts\python.exe"

if not exist "%PY%" (
  echo Virtualenv missing. Run setup.bat first.
  exit /b 1
)

"%PY%" -m grabber %*
