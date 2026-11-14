@echo off
cd /d "%~dp0.."
title memebot HYPE status
color 5F
set BOT_NAME=HYPE
set STATE_FILE=state-hype.json
.venv\Scripts\python -m memebot --paper status
pause
