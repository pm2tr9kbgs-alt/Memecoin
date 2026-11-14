@echo off
cd /d "%~dp0.."
title memebot LOOSE (paper)
color 6F
set BOT_NAME=LOOSE
set MODE=paper
set STATE_FILE=state-loose.json
set TRADES_CSV=trades-loose.csv
set LOG_FILE=bot-loose.log
set WINNERS_FILE=winners-loose.txt
rem Looser entry filters (same as the cloud test)
set MIN_LIQUIDITY_USD=10000
set MIN_MARKET_CAP_USD=20000
set MIN_VOLUME_M5_USD=1500
set MIN_TXNS_M5=15
set MIN_BUY_SELL_RATIO_M5=1.1
set MIN_PRICE_CHANGE_M5_PCT=0
set MAX_PRICE_CHANGE_M5_PCT=60
set MIN_PAIR_AGE_MINUTES=5
set MAX_PAIR_AGE_MINUTES=4320
echo ==================================================
echo   LOOSE BOT  -  paper trading  -  looser filters
echo   trades saved to trades-loose.csv
echo ==================================================
echo.
.venv\Scripts\python -m memebot --paper run
pause
