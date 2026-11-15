"""Main loop: scan for new tokens, vet them, buy, and manage exits."""
from __future__ import annotations

import asyncio
from collections import deque
import logging
import time
from dataclasses import dataclass

import httpx

from .config import LAMPORTS_PER_SOL, SOL_MINT, Config
from .dexscreener import DexScreener
from .executor import Executor, TxFailed
from .jupiter import Jupiter, NoRouteError, out_amount, price_impact_pct
from .models import Position, TokenCandidate
from .rpc import RPCError
from .safety import SafetyChecker, safety_rejections
from .state import BotState, log_trade
from .strategy import exit_reason, momentum_score, screen_rejections

log = logging.getLogger("memebot")

REBUY_COOLDOWN_SEC = 24 * 3600
NO_ROUTE_SELL_AFTER = 5
FEE_BUFFER_LAMPORTS = 3_000_000  # priority fee + token account rent headroom


@dataclass
class Watch:
    """A token that spiked: wait for a pullback from its high before buying."""
    candidate: TokenCandidate
    added_at: float
    peak_price: float  # lamports per raw token unit (from Jupiter buy quotes)
    last_price: float


def sol(lamports: int) -> str:
    return f"{lamports / LAMPORTS_PER_SOL:.4f} SOL"


class Bot:
    def __init__(self, cfg: Config, dex: DexScreener, safety: SafetyChecker, jupiter: Jupiter,
                 executor: Executor, state: BotState):
        self.cfg = cfg
        self.dex = dex
        self.safety = safety
        self.jup = jupiter
        self.ex = executor
        self.state = state
        self._halt_logged = False
        self._stop = asyncio.Event()
        self.watchlist: dict[str, Watch] = {}
        self.activity: deque[tuple[float, int]] = deque()  # (time, active token count) per scan
        self._last_activity_tier = ""

    # ------------------------------------------------------------------ helpers
    def _persist(self) -> None:
        if not self.cfg.is_live:
            self.state.paper_balance = getattr(self.ex, "balance", None)
        self.state.save()

    def daily_limit_hit(self) -> bool:
        return self.state.realized_today() <= -int(self.cfg.daily_loss_limit_sol * LAMPORTS_PER_SOL)

    def _eligible(self, mint: str, now: float) -> bool:
        if mint in self.state.positions or mint in self.state.blacklist or mint in self.watchlist:
            return False
        last = self.state.traded_mints.get(mint)
        return last is None or now - last > REBUY_COOLDOWN_SEC

    # ------------------------------------------------------------------ market activity / sizing
    def record_activity(self, candidates: list[TokenCandidate], now: float) -> None:
        cfg = self.cfg
        active = sum(1 for c in candidates if c.volume_m5_usd >= cfg.activity_min_volume_m5_usd
                     and c.buys_m5 + c.sells_m5 >= cfg.activity_min_txns_m5)
        self.activity.append((now, active))
        while self.activity and now - self.activity[0][0] > cfg.activity_window_minutes * 60:
            self.activity.popleft()

    def market_activity(self) -> float | None:
        """Average number of actively traded tokens per scan over the activity window (None = no data yet)."""
        if not self.activity:
            return None
        return sum(n for _, n in self.activity) / len(self.activity)

    def market_is_hot(self) -> bool:
        a = self.market_activity()
        return a is not None and a >= self.cfg.activity_hot_min

    def buy_lamports(self) -> int:
        """Full BUY_AMOUNT_SOL when the market is busy, a fraction of it when it's quiet."""
        full = self.cfg.buy_amount_lamports
        if self.market_is_hot():
            return full
        return int(full * max(0.0, min(self.cfg.quiet_size_fraction, 1.0)))

    def _log_activity_tier(self) -> None:
        a = self.market_activity()
        tier = "hot" if self.market_is_hot() else "quiet"
        if tier != self._last_activity_tier:
            self._last_activity_tier = tier
            log.info("MARKET %s: %.1f active tokens per scan (30m avg) -> buy size %s",
                     tier.upper(), a or 0.0, sol(self.buy_lamports()))

    def paused(self, now: float) -> bool:
        """True while the losing-streak pause is on (no new buys)."""
        return now < self.state.paused_until

    def _pump_rejections(self, c: TokenCandidate, now: float) -> list[str]:
        """Remember tokens that pumped hard; skip them for a while even after the 1h change cools off."""
        cfg = self.cfg
        if cfg.pumped_h1_pct <= 0:
            return []
        if c.price_change_h1_pct > cfg.pumped_h1_pct:
            self.state.pumped[c.mint] = now
        last = self.state.pumped.get(c.mint)
        if last is not None and (now - last) / 60 < cfg.pumped_memory_minutes:
            return [f"already pumped (1h change above +{cfg.pumped_h1_pct:.0f}% {(now - last) / 60:.0f}m ago)"]
        return []

    # ------------------------------------------------------------------ entries
    async def scan_once(self, now: float | None = None) -> list[str]:
        """One discovery pass. Returns mints bought."""
        now = now if now is not None else time.time()
        all_mints = await self.dex.latest_token_addresses()
        all_candidates = await self.dex.candidates(all_mints) if all_mints else []
        self.record_activity(all_candidates, now)  # measured on every scan, even when not buying
        self._log_activity_tier()
        if len(self.state.positions) >= self.cfg.max_open_positions:
            return []
        if self.daily_limit_hit():
            if not self._halt_logged:
                log.warning("Daily loss limit reached (%s today); not opening new positions until UTC midnight",
                            sol(self.state.realized_today()))
                self._halt_logged = True
            return []
        self._halt_logged = False
        if self.paused(now):
            return []

        mints = [m for m in all_mints if self._eligible(m, now)]
        if not mints:
            return []
        candidates = [c for c in all_candidates if c.mint in set(mints)]
        passing: list[TokenCandidate] = []
        for c in candidates:
            reasons = screen_rejections(c, self.cfg, now * 1000) + self._pump_rejections(c, now)
            if reasons:
                log.debug("skip %s (%s): %s", c.symbol, c.mint[:6], "; ".join(reasons))
            else:
                passing.append(c)
        passing.sort(key=momentum_score, reverse=True)
        log.info("scan: %d new tokens, %d with pairs, %d pass momentum filters", len(mints), len(candidates), len(passing))

        bought: list[str] = []
        for c in passing:
            if len(self.state.positions) >= self.cfg.max_open_positions:
                break
            if await self._try_buy(c, now):
                bought.append(c.mint)
        return bought

    async def _try_buy(self, c: TokenCandidate, now: float) -> bool:
        cfg = self.cfg
        amount = self.buy_lamports()
        report = await self.safety.inspect(c.mint)
        reasons = safety_rejections(report, cfg)
        if reasons:
            log.info("reject %s (%s): %s", c.symbol, c.mint[:6], "; ".join(reasons))
            if not report.errors:  # only blacklist on real findings, not transient RPC errors
                self.state.blacklist[c.mint] = "; ".join(reasons)
            return False

        balance = await self.ex.sol_balance()
        reserve = int(cfg.min_sol_reserve * LAMPORTS_PER_SOL)
        if balance - amount - FEE_BUFFER_LAMPORTS < reserve:
            log.warning("balance %s too low to buy %s and keep %s reserve", sol(balance), sol(amount), sol(reserve))
            return False

        try:
            quote = await self.jup.quote(SOL_MINT, c.mint, amount, cfg.buy_slippage_bps)
            impact = price_impact_pct(quote)
            if impact > cfg.max_price_impact_pct:
                log.info("reject %s: price impact %.2f%%", c.symbol, impact)
                return False
            # Honeypot / sell-tax check: can we sell back what we would buy, and for how much?
            back = await self.jup.quote(c.mint, SOL_MINT, out_amount(quote), cfg.sell_slippage_bps)
            round_trip_loss = (1 - out_amount(back) / amount) * 100
            if round_trip_loss > cfg.max_round_trip_loss_pct:
                log.info("reject %s: round-trip loss %.1f%% (possible tax/honeypot)", c.symbol, round_trip_loss)
                self.state.blacklist[c.mint] = f"round-trip loss {round_trip_loss:.1f}%"
                return False
        except NoRouteError as exc:
            log.info("reject %s: no Jupiter route (%s)", c.symbol, exc)
            return False

        if cfg.dip_trigger_pct > 0 and c.price_change_m5_pct >= cfg.dip_trigger_pct:
            price = amount / max(out_amount(quote), 1)
            self.watchlist[c.mint] = Watch(c, now, price, price)
            log.info("WATCH %s (%s): up %+.1f%% in 5m, waiting for a %.0f%% dip before buying",
                     c.symbol, c.mint, c.price_change_m5_pct, cfg.dip_pct)
            return False
        return await self._execute_buy(c, quote, now, amount=amount)

    async def _execute_buy(self, c: TokenCandidate, quote: dict, now: float, note: str = "",
                           amount: int | None = None) -> bool:
        cfg = self.cfg
        amount = amount or self.buy_lamports()
        log.info("BUY %s (%s) %s | liq $%s mcap $%s 5m %+.1f%% b/s %.2f", c.symbol, c.mint,
                 sol(amount), f"{c.liquidity_usd:,.0f}", f"{c.market_cap_usd:,.0f}",
                 c.price_change_m5_pct, c.buy_sell_ratio_m5)
        if note:
            log.info("    %s", note)
        try:
            res = await self.ex.buy(c.mint, amount, quote)
        except (TxFailed, RPCError, httpx.HTTPError, NoRouteError) as exc:
            log.error("buy %s failed: %s", c.symbol, exc)
            self.state.traded_mints[c.mint] = now
            self._persist()
            return False
        if res.token_amount <= 0:
            log.error("buy %s confirmed (%s) but no tokens detected; check wallet manually", c.symbol, res.signature)
            self.state.traded_mints[c.mint] = now
            self._persist()
            return False

        pos = Position(mint=c.mint, symbol=c.symbol, entry_lamports=res.lamports, token_amount=res.token_amount,
                       opened_at=now, entry_signature=res.signature, pair_address=c.pair_address,
                       peak_value_lamports=res.lamports, last_value_lamports=res.lamports,
                       last_buy_price=res.lamports / res.token_amount)
        self.state.positions[c.mint] = pos
        self.state.traded_mints[c.mint] = now
        log_trade(cfg.trades_csv, cfg.mode, "buy", pos, res.lamports, res.token_amount, res.signature)
        self._persist()
        log.info("bought %s: %d tokens for %s (%s)", c.symbol, res.token_amount, sol(res.lamports), res.signature)
        return True

    async def watch_once(self, now: float | None = None) -> list[str]:
        """Re-price watched (spiked) tokens; buy the ones that pulled back enough. Returns mints bought."""
        now = now if now is not None else time.time()
        cfg = self.cfg
        amount = self.buy_lamports()
        bought: list[str] = []
        for mint, w in list(self.watchlist.items()):
            sym = w.candidate.symbol
            if (now - w.added_at) / 60 > cfg.dip_watch_minutes:
                log.info("UNWATCH %s: no dip within %.0f min", sym, cfg.dip_watch_minutes)
                del self.watchlist[mint]
                continue
            try:
                quote = await self.jup.quote(SOL_MINT, mint, amount, cfg.buy_slippage_bps)
            except NoRouteError:
                continue
            except (httpx.HTTPError, ValueError) as exc:
                log.debug("watch quote %s failed: %s", sym, exc)
                continue
            price = amount / max(out_amount(quote), 1)
            w.last_price = price
            w.peak_price = max(w.peak_price, price)
            dip = (1 - price / w.peak_price) * 100
            if dip > cfg.max_dip_pct:
                log.info("UNWATCH %s: fell %.1f%% from its high (dumping, not a dip)", sym, dip)
                del self.watchlist[mint]
                self.state.traded_mints[mint] = now  # cool down; don't re-watch a dumping token
                continue
            if dip < cfg.dip_pct:
                continue
            if len(self.state.positions) >= cfg.max_open_positions or self.daily_limit_hit() or self.paused(now):
                continue  # keep watching until a slot frees up
            if price_impact_pct(quote) > cfg.max_price_impact_pct:
                continue
            del self.watchlist[mint]
            if await self._execute_buy(w.candidate, quote, now, note=f"bought the dip: {dip:.1f}% below its high",
                                       amount=amount):
                bought.append(mint)
        return bought

    # ------------------------------------------------------------------ exits
    async def monitor_once(self, now: float | None = None) -> list[str]:
        """Re-price every open position and sell those that hit an exit rule. Returns mints sold."""
        now = now if now is not None else time.time()
        sold: list[str] = []
        for mint, pos in list(self.state.positions.items()):
            try:
                quote = await self.jup.quote(mint, SOL_MINT, pos.token_amount, self.cfg.sell_slippage_bps)
                value = out_amount(quote)
                pos.no_route_count = 0
            except NoRouteError:
                pos.no_route_count += 1
                log.warning("%s: no sell route (%d in a row)", pos.symbol, pos.no_route_count)
                if pos.no_route_count >= NO_ROUTE_SELL_AFTER and await self.close(pos, "no_route", now):
                    sold.append(mint)
                continue
            except (httpx.HTTPError, ValueError) as exc:
                log.warning("%s: quote failed: %s", pos.symbol, exc)
                continue
            self._winner_alert(pos, value)
            reason = exit_reason(pos, value, now, self.cfg)
            log.debug("%s value %s (%+.1f%%, peak %+.1f%%)", pos.symbol, sol(value), pos.pnl_pct(value),
                      pos.pnl_pct(pos.peak_value_lamports))
            if reason:
                if await self.close(pos, reason, now):
                    sold.append(mint)
            elif not await self._maybe_take_partial(pos, value):
                await self._maybe_add(pos, value, now)
        if self.state.positions:
            self._persist()
        return sold

    def _winner_alert(self, pos: Position, value: int) -> None:
        """Announce a position each time it crosses another WINNER_ALERT_PCT step (+25%, +50%, ...)."""
        step = self.cfg.winner_alert_pct
        pnl = pos.pnl_pct(value)
        if step <= 0 or pnl < step:
            return
        level = (pnl // step) * step
        if level <= pos.winner_alerted_pct:
            return
        pos.winner_alerted_pct = level
        link = f"https://dexscreener.com/solana/{pos.mint}"
        log.warning("WINNER %s %+.1f%% (%s, worth %s) address %s %s",
                    pos.symbol, pnl, f"crossed +{level:.0f}%", sol(value), pos.mint, link)
        if self.cfg.winners_file:
            stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
            with open(self.cfg.winners_file, "a", encoding="utf-8") as fh:
                fh.write(f"{stamp} UTC  {pos.symbol}  {pnl:+.1f}%  {pos.mint}  {link}\n")

    async def _maybe_add(self, pos: Position, value: int, now: float) -> bool:
        """Pyramiding: add to a winner each time it rises add_trigger_pct above the last buy price."""
        cfg = self.cfg
        if cfg.add_trigger_pct <= 0 or pos.adds >= cfg.max_adds or pos.token_amount <= 0:
            return False
        price = value / pos.token_amount
        if not pos.last_buy_price:
            pos.last_buy_price = pos.entry_lamports / pos.token_amount if not pos.adds else price
        if price < pos.last_buy_price * (1 + cfg.add_trigger_pct / 100) or self.daily_limit_hit():
            return False
        add_lamports = int(cfg.add_amount_sol * LAMPORTS_PER_SOL)
        balance = await self.ex.sol_balance()
        if balance - add_lamports - FEE_BUFFER_LAMPORTS < int(cfg.min_sol_reserve * LAMPORTS_PER_SOL):
            return False
        rise = (price / pos.last_buy_price - 1) * 100
        log.info("ADD %s %s: up %+.1f%% since last buy (add %d/%d)", pos.symbol, sol(add_lamports), rise,
                 pos.adds + 1, cfg.max_adds)
        try:
            res = await self.ex.buy(pos.mint, add_lamports)
        except (TxFailed, RPCError, httpx.HTTPError, NoRouteError) as exc:
            log.error("add to %s failed: %s", pos.symbol, exc)
            pos.adds += 1  # don't retry the same add every tick
            return False
        if res.token_amount <= 0:
            pos.adds += 1
            return False
        old_tokens = pos.token_amount
        pos.token_amount += res.token_amount
        pos.entry_lamports += res.lamports
        # keep the trailing stop anchored to the peak *price*, not the old (smaller) position value
        pos.peak_value_lamports = int(pos.peak_value_lamports * pos.token_amount / old_tokens)
        pos.last_value_lamports = int(value * pos.token_amount / old_tokens)
        pos.last_buy_price = res.lamports / res.token_amount
        pos.adds += 1
        log_trade(cfg.trades_csv, cfg.mode, "add", pos, res.lamports, res.token_amount, res.signature,
                  f"add {pos.adds}/{cfg.max_adds}")
        self._persist()
        log.info("added to %s: now %s invested, %d tokens", pos.symbol, sol(pos.entry_lamports), pos.token_amount)
        return True

    async def _maybe_take_partial(self, pos: Position, value: int) -> bool:
        """Take-profit ladder: at each level (default +25%, +50%, +100%) sell a fraction of the original position;
        the rest keeps running under the trailing stop. Levels crossed at once are sold in one transaction."""
        cfg = self.cfg
        levels = cfg.partial_take_profit_levels
        if not levels or not 0 < cfg.partial_take_profit_fraction <= 1 or pos.partial_steps >= len(levels):
            return False
        pnl_pct = pos.pnl_pct(value)
        reached = sum(1 for lvl in levels if pnl_pct >= lvl)
        if reached <= pos.partial_steps:
            return False
        if not pos.original_tokens:
            pos.original_tokens = pos.token_amount
        steps = reached - pos.partial_steps
        amount = min(int(pos.original_tokens * cfg.partial_take_profit_fraction * steps), pos.token_amount)
        if amount <= 0:
            pos.partial_steps = reached
            return False
        log.info("TAKE PROFIT %s: up %+.1f%% (level +%.0f%%), selling %.0f%% of the original position",
                 pos.symbol, pnl_pct, levels[reached - 1], cfg.partial_take_profit_fraction * steps * 100)
        try:
            res = await self.ex.sell(pos.mint, amount)
        except (TxFailed, RPCError, httpx.HTTPError, NoRouteError) as exc:
            log.error("partial sell %s failed: %s", pos.symbol, exc)
            pos.partial_steps = reached  # don't retry every tick; the trailing stop still protects the gain
            return False
        old_tokens = pos.token_amount
        cost = int(pos.entry_lamports * res.token_amount / old_tokens)  # cost basis of the part sold
        pnl = res.lamports - cost
        pos.token_amount -= res.token_amount
        pos.entry_lamports -= cost
        keep = pos.token_amount / old_tokens
        pos.peak_value_lamports = int(pos.peak_value_lamports * keep)
        pos.last_value_lamports = int(value * keep)
        pos.realized_lamports += pnl
        pos.partial_steps = reached
        self.state.record_realized(pnl)
        log_trade(cfg.trades_csv, cfg.mode, "partial", pos, res.lamports, res.token_amount, res.signature,
                  f"take_profit_{levels[reached - 1]:g}", pnl)
        if pos.token_amount <= 0:
            self.state.positions.pop(pos.mint, None)
        self._persist()
        log.info("sold part of %s for %s, P&L %s locked in (%d%% of the original position left)", pos.symbol,
                 sol(res.lamports), sol(pnl), round(pos.token_amount / pos.original_tokens * 100))
        return True

    async def close(self, pos: Position, reason: str, now: float | None = None) -> bool:
        now = now if now is not None else time.time()
        log.info("SELL %s (%s) reason=%s est %+.1f%%", pos.symbol, pos.mint, reason, pos.pnl_pct(pos.last_value_lamports))
        try:
            res = await self.ex.sell(pos.mint, pos.token_amount)
        except (TxFailed, RPCError, httpx.HTTPError, NoRouteError) as exc:
            pos.sell_failures += 1
            log.error("sell %s failed (%d): %s", pos.symbol, pos.sell_failures, exc)
            return False
        pnl = res.lamports - pos.entry_lamports
        self.state.record_realized(pnl)
        self.state.positions.pop(pos.mint, None)
        log_trade(self.cfg.trades_csv, self.cfg.mode, "sell", pos, res.lamports, res.token_amount, res.signature, reason, pnl)
        self._track_streak(pnl + pos.realized_lamports, now)
        self._persist()
        log.info("sold %s for %s, P&L %s (%+.1f%%) | today %s", pos.symbol, sol(res.lamports), sol(pnl),
                 pnl / pos.entry_lamports * 100, sol(self.state.realized_today()))
        return True

    def _track_streak(self, trade_pnl: int, now: float) -> None:
        """Count losing trades in a row; after loss_streak_limit, pause new buys for loss_pause_minutes."""
        cfg = self.cfg
        if cfg.loss_streak_limit <= 0:
            return
        if trade_pnl >= 0:
            self.state.loss_streak = 0
            return
        self.state.loss_streak += 1
        if self.state.loss_streak >= cfg.loss_streak_limit:
            self.state.paused_until = now + cfg.loss_pause_minutes * 60
            self.state.loss_streak = 0
            log.warning("PAUSE: %d losing trades in a row; no new buys for %.0f minutes (open positions still managed)",
                        cfg.loss_streak_limit, cfg.loss_pause_minutes)

    async def sell_all(self, reason: str = "manual") -> None:
        for pos in list(self.state.positions.values()):
            await self.close(pos, reason)

    async def reconcile(self) -> None:
        """Drop tracked positions whose tokens are no longer in the wallet (sold manually, etc.)."""
        for mint, pos in list(self.state.positions.items()):
            try:
                held = await self.ex.token_balance(mint)
            except RPCError as exc:
                log.warning("could not check %s balance: %s", pos.symbol, exc)
                continue
            if held <= 0:
                log.warning("%s no longer held; removing from tracked positions", pos.symbol)
                self.state.positions.pop(mint)
            elif held != pos.token_amount:
                pos.token_amount = held
        self._persist()

    # ------------------------------------------------------------------ loop
    def stop(self) -> None:
        self._stop.set()

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    async def _price_loop(self) -> None:
        """Check open positions every MONITOR_INTERVAL_SEC, independent of scanning, so slow safety checks
        or RPC retries never leave open trades unwatched."""
        while not self._stop.is_set():
            try:
                await self.monitor_once()
            except Exception:  # keep watching; positions still need managing
                log.exception("price check error")
            await self._sleep(self.cfg.monitor_interval_sec)

    async def _scan_loop(self) -> None:
        next_scan = 0.0
        while not self._stop.is_set():
            try:
                await self.watch_once()
                if time.monotonic() >= next_scan:
                    next_scan = time.monotonic() + self.cfg.scan_interval_sec
                    await self.scan_once()
            except Exception:
                log.exception("scan error")
            await self._sleep(self.cfg.monitor_interval_sec)

    async def run(self) -> None:
        await asyncio.gather(self._price_loop(), self._scan_loop())
        self._persist()
