@echo off
cd /d "%~dp0.."
title memebot STRICT status
color 1F
set BOT_NAME=STRICT
set STATE_FILE=state-strict.json
.venv\Scripts\python -m memebot --paper status
pause
