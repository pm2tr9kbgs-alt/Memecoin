@echo off
cd /d "%~dp0.."
echo === memebot setup ===
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY (
  echo Python was not found. Install it from https://www.python.org/downloads/
  echo and tick "Add python.exe to PATH" in the installer, then run this again.
  pause
  exit /b 1
)
%PY% --version
if not exist .venv (
  echo Creating virtual environment...
  %PY% -m venv .venv || goto :error
)
echo Installing packages...
.venv\Scripts\python -m pip install --upgrade pip >nul
.venv\Scripts\python -m pip install -r requirements.txt || goto :error
if not exist .env (
  copy .env.example .env >nul
  echo Created .env from .env.example
)
echo.
echo Setup complete. Next: double-click 2a-paper-STRICT.bat and 2b-paper-LOOSE.bat
pause
exit /b 0
:error
echo.
echo Setup FAILED - copy the error above and send it to Claude.
pause
exit /b 1
