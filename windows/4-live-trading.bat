@echo off
cd /d "%~dp0.."
echo LIVE TRADING with real SOL from the wallet in .env
echo Close this window or press Ctrl+C to stop.
echo.
.venv\Scripts\python -m memebot wallet
.venv\Scripts\python -m memebot run
pause
