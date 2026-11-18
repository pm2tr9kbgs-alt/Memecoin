@echo off
cd /d "%~dp0.."
title memebot COPY (paper)
color 2F
set BOT_NAME=COPY
set MODE=paper
set STATE_FILE=state-copy.json
set TRADES_CSV=trades-copy.csv
set LOG_FILE=bot-copy.log
echo ==================================================
echo   COPY BOT  -  paper trading
echo   copies the wallets in COPY_WALLETS (.env)
echo   trades saved to trades-copy.csv
echo ==================================================
echo.
.venv\Scripts\python -m memebot --paper copy
pause
