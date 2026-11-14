# memebot: Solana meme coin momentum bot

Finds newly listed Solana meme coins, filters out likely rugs, buys the ones with momentum through
Jupiter, and sells on take-profit, stop-loss, trailing stop or a time limit.

> **Warning.** Meme coins are extremely risky. Most go to zero, and rugs, sell taxes and MEV are common.
> These filters reduce risk but cannot remove it. Run it from a **dedicated hot wallet** holding only what
> you can afford to lose. Nothing here is financial advice.

## How it works

1. **Discover.** Every `SCAN_INTERVAL_SEC` it pulls the latest token profiles and boosts from the DexScreener API
   and looks up each token's most liquid Solana pair.
2. **Momentum filters.** These check liquidity, market cap, pair age, 5-minute volume and transaction count,
   the buy/sell ratio, and the 5-minute price change. That change has a ceiling so the bot doesn't buy the top of a vertical candle.
3. **Safety checks.** These run on-chain through your RPC, plus RugCheck:
   - mint authority and freeze authority must be revoked
   - limits on how much the top 10 wallets and the single largest wallet hold. Pools and bonding curves are
     program-derived (off-curve) addresses and are left out of this count.
   - RugCheck "danger" risks and its normalised score
   - **round-trip quote:** it quotes buying, then quotes selling those tokens back. A large loss means a sell tax or honeypot.
   - a cap on price impact
4. **Buy** `BUY_AMOUNT_SOL` with a Jupiter swap: it signs locally, sends, rebroadcasts until confirmed, then reads the
   actual token and SOL balance changes.
5. **Manage.** Every `MONITOR_INTERVAL_SEC` it values each position with a real Jupiter sell quote and exits on
   `TAKE_PROFIT_PCT`, `STOP_LOSS_PCT`, a trailing stop (`TRAILING_STOP_PCT` below the peak once the gain has
   reached `TRAILING_ACTIVATION_PCT`) or `MAX_HOLD_MINUTES`.
6. **Risk limits:** `MAX_OPEN_POSITIONS`, `MIN_SOL_RESERVE`, and `DAILY_LOSS_LIMIT_SOL`, which stops new buys
   for the rest of the UTC day once that much has been lost.

State (open positions, daily P&L, blacklist) is kept in `state.json`, so a restart picks up where it left off.
Every fill is appended to `trades.csv`, and logs go to `bot.log`.

## Windows (no command line needed)

1. Install Python 3.11+ from python.org and tick **"Add python.exe to PATH"**.
2. Download this repo as a ZIP and extract it (e.g. to `Documents\memebot`).
3. Open the `windows` folder and double-click, in order:
   - `1-setup.bat`: installs everything and creates `.env`
   - `2-paper-trading.bat`: simulated trading (no wallet needed)
   - `3-status.bat`: balance and open positions
   - `4-live-trading.bat`: real trading (needs `PRIVATE_KEY` and `MODE=live` in `.env`)
   - `5-sell-everything.bat`: emergency exit
4. Edit settings with Notepad: right-click `.env` → Open with → Notepad.
5. Keep the PC awake (Settings → System → Power → Screen and sleep → Never when plugged in);
   the bot only trades while its window is open.

## Setup (Mac / Linux)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then set PRIVATE_KEY and RPC_URL
```

## Usage

```bash
python -m memebot wallet          # print the wallet address the bot will trade from
python -m memebot --paper run     # dry run: simulated fills on real quotes, no transactions
python -m memebot run             # live trading (asks you to type LIVE; add --yes to skip)
python -m memebot status          # balance, today's P&L, open positions marked to market
python -m memebot sell-all        # emergency: sell everything now
python -m memebot -v run          # debug log showing why each token was rejected
```

Ctrl+C stops the bot cleanly. Open positions are saved and resumed on the next `run`.

**Recommended:** run `--paper` for a while first and tune the filters from `trades.csv` before going live.

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```
