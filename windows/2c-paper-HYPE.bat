@echo off
cd /d "%~dp0.."
title memebot HYPE (paper)
color 5F
set BOT_NAME=HYPE
set MODE=paper
set STATE_FILE=state-hype.json
set TRADES_CSV=trades-hype.csv
set LOG_FILE=bot-hype.log
set WINNERS_FILE=winners-hype.txt
rem Loose filters, but only tokens with a hype score of 25-60 (moderate attention; extreme hype is skipped as a likely pump)
set MIN_HYPE_SCORE=25
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
echo   HYPE BOT  -  paper trading  -  hype score 25-60
echo   trades saved to trades-hype.csv
echo ==================================================
echo.
.venv\Scripts\python -m memebot --paper run
pause
