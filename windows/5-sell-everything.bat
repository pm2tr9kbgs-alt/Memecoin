@echo off
cd /d "%~dp0.."
echo This sells ALL open positions now.
set /p OK=Type YES to continue: 
if /I not "%OK%"=="YES" exit /b 0
.venv\Scripts\python -m memebot sell-all
pause
