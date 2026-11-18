@echo off
cd /d "%~dp0.."
title memebot COPY status
color 2F
set BOT_NAME=COPY
set STATE_FILE=state-copy.json
.venv\Scripts\python -m memebot --paper status
pause
