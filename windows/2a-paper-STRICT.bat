@echo off
cd /d "%~dp0.."
title memebot STRICT (paper)
color 1F
set BOT_NAME=STRICT
set MODE=paper
set STATE_FILE=state-strict.json
set TRADES_CSV=trades-strict.csv
set LOG_FILE=bot-strict.log
echo ==================================================
echo   STRICT BOT  -  paper trading  -  default filters
echo   trades saved to trades-strict.csv
echo ==================================================
echo.
.venv\Scripts\python -m memebot --paper run
pause
