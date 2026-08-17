@echo off
REM Windows setup. Creates the virtualenv and installs dependencies.
REM Re-run this to upgrade yt-dlp when downloads start failing.
setlocal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo Python not found. Install it from https://python.org and tick
  echo "Add python.exe to PATH" during setup.
  exit /b 1
)

if not exist ".venv" (
  echo ==^> Creating .venv
  python -m venv .venv || exit /b 1
)

echo ==^> Installing/upgrading dependencies
".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
".venv\Scripts\python.exe" -m pip install --quiet --upgrade -r requirements.txt || exit /b 1

where ffmpeg >nul 2>nul
if errorlevel 1 (
  echo.
  echo ==^> ffmpeg is missing. It is needed to merge video and audio.
  echo     Install it with:  winget install Gyan.FFmpeg
  echo     then open a new terminal so PATH picks it up.
)

echo.
echo ==^> Done. Try:
echo     grab.bat --doctor
echo     grab.bat --web
