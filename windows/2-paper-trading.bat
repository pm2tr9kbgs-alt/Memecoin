@echo off
cd /d "%~dp0.."
echo Paper trading: simulated trades on real prices, no wallet needed.
echo Close this window or press Ctrl+C to stop. Trades are saved in trades.csv
echo.
.venv\Scripts\python -m memebot --paper run
pause
