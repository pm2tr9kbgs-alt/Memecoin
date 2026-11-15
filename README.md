# memebot: Solana meme coin momentum bot

Finds newly listed Solana meme coins, filters out likely rugs, buys the ones with momentum through
Jupiter, and sells on take-profit, stop-loss, trailing stop or a time limit.

> **Warning.** Meme coins are extremely risky. Most go to zero, and rugs, sell taxes and MEV are common.
> These filters reduce risk but cannot remove it. Run it from a **dedicated hot wallet** holding only what
> you can afford to lose. Nothing here is financial advice.

## How it works

1. **Discover.** Every `SCAN_INTERVAL_SEC` it pulls the latest token profiles and boosts from the DexScreener API
   and looks up each token's most liquid Solana pair.
2. **Coordinated-pump filter.** Tokens with more than 3.5 buys per sell in 5 minutes are skipped: that is usually
   bots pumping before a dump, not real demand (`MAX_BUY_SELL_RATIO_M5`).
3. **Momentum filters.** These check liquidity, market cap, pair age, 5-minute volume and transaction count,
   the buy/sell ratio, and the 5-minute price change. That change has a ceiling so the bot doesn't buy the top of a vertical candle.
   **Graduation:** a pump.fun or bonk token that moved to a real DEX less than 10 minutes ago is skipped, because
   early buyers usually sell right after graduation (`GRADUATION_WAIT_MINUTES`).
   **Already pumped:** a token whose 1-hour price change went above +150% at any point in the last 2 hours is skipped,
   even once the 1-hour number cools off (`PUMPED_H1_PCT`, `PUMPED_MEMORY_MINUTES`). In overnight testing, these buys
   lost about three times more than all other trades combined, and included both rugs.
4. **Safety checks.** These run on-chain through your RPC, plus RugCheck:
   - mint authority and freeze authority must be revoked
   - limits on how much the top 10 wallets and the single largest wallet hold. Pools and bonding curves are
     program-derived (off-curve) addresses and are left out of this count.
   - RugCheck "danger" risks and its normalised score
   - **liquidity lock:** at least 90% of the pool's liquidity must be locked or burned, so the developer can't pull it
     (`MIN_LP_LOCKED_PCT`)
   - **creator wallet:** skipped if the creator still holds more than 5% of the supply (`MAX_CREATOR_PCT`)
   - **round-trip quote:** it quotes buying, then quotes selling those tokens back. A large loss means a sell tax or honeypot.
   - a cap on price impact
5. **Wait for a dip after a spike.** A token already up 15%+ in 5 minutes isn't bought straight away: it goes on a
   watchlist and is bought only after pulling back 5% from its high. A fall of more than 20% means it's dumping, so
   it's dropped, and so is any token that doesn't dip within 15 minutes (`DIP_TRIGGER_PCT`, `DIP_PCT`, `MAX_DIP_PCT`,
   `DIP_WATCH_MINUTES`).
6. **Buy** `BUY_AMOUNT_SOL` with a Jupiter swap: it signs locally, sends, rebroadcasts until confirmed, then reads the
   actual token and SOL balance changes.
7. **Manage.** Every `MONITOR_INTERVAL_SEC` it values each position with a real Jupiter sell quote. By default it
   **lets winners run**: there is no take-profit, and it sells once the value falls 20% below its highest point
   (`TRAILING_STOP_PCT`). Before any gain, that is the same as a −20% stop-loss. Once a position has been up +25%,
   the trail tightens to 10% below the peak to keep more of the gain (`TIGHT_TRAIL_AFTER_PCT`, `TIGHT_TRAILING_STOP_PCT`). Optional: `TAKE_PROFIT_PCT`
   to cap gains, `TRAILING_ACTIVATION_PCT` to start trailing only after a gain, `MAX_HOLD_MINUTES` for a time limit.
   Positions that aren't at least +5% up after 30 minutes are sold to free the slot
   (`STALE_AFTER_MINUTES`, `STALE_MIN_PROFIT_PCT`).
   **Partial take-profit:** at +25% it sells half the position to lock in profit, and the other half keeps running
   under the trailing stop (`PARTIAL_TAKE_PROFIT_PCT`, `PARTIAL_TAKE_PROFIT_FRACTION`; these sales show as `partial`
   in `trades.csv`).
   **Adding to winners (off by default):** with `MAX_ADDS` above 0, each time a position rises +50% above its last buy,
   it adds 0.025 SOL (`ADD_TRIGGER_PCT`, `ADD_AMOUNT_SOL`). In testing, both adds landed at the top and roughly halved
   the profit, so it is turned off.
   **Winner alerts:** each time a position gains another 25% (+25%, +50%, ...), the bot logs a `WINNER` line with the
   token address and DexScreener link and appends it to `winners.txt` (`WINNER_ALERT_PCT`).
8. **Risk limits:** `MAX_OPEN_POSITIONS`, `MIN_SOL_RESERVE`, and `DAILY_LOSS_LIMIT_SOL`, which stops new buys
   for the rest of the UTC day once that much has been lost.
   **Losing-streak pause:** after 3 losing trades in a row, it stops buying for 45 minutes. Open positions are still
   managed during the pause (`LOSS_STREAK_LIMIT`, `LOSS_PAUSE_MINUTES`).
   **Market activity sizing:** on every scan it counts how many of the newest tokens are really trading (at least
   $5,000 volume and 30 trades in 5 minutes) and averages that over 30 minutes. When the average is 6 or more, the
   market is busy and it buys the full `BUY_AMOUNT_SOL`; when it is lower, it buys half. This follows the market
   live instead of fixed hours: in testing, the US evening averaged about 8 and quiet mornings about 3
   (`ACTIVITY_HOT_MIN`, `QUIET_SIZE_FRACTION`, `ACTIVITY_WINDOW_MINUTES`, `ACTIVITY_MIN_VOLUME_M5_USD`,
   `ACTIVITY_MIN_TXNS_M5`). The log shows `MARKET HOT` / `MARKET QUIET` whenever it switches.

State (open positions, daily P&L, blacklist) is kept in `state.json`, so a restart picks up where it left off.
Every fill is appended to `trades.csv`, and logs go to `bot.log`.

## Windows (no command line needed)

1. Install Python 3.11+ from python.org and tick **"Add python.exe to PATH"**.
2. Download this repo as a ZIP and extract it (e.g. to `Documents\memebot`).
3. Open the `windows` folder and double-click, in order:
   - `1-setup.bat`: installs everything and creates `.env`
   - `2a-paper-STRICT.bat` (blue window): paper trading with the default filters → `trades-strict.csv`
   - `2b-paper-LOOSE.bat` (yellow window): paper trading with looser filters → `trades-loose.csv`
     (run them at once to compare; each has its own balance and files)
   - `3a-status-STRICT.bat` / `3b-status-LOOSE.bat`: balance and open positions for each
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
