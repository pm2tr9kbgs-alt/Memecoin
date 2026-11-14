@echo off
cd /d "%~dp0.."
title memebot LOOSE status
color 6F
set BOT_NAME=LOOSE
set STATE_FILE=state-loose.json
.venv\Scripts\python -m memebot --paper status
pause
