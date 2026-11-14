"""Main loop: scan for new tokens, vet them, buy, and manage exits."""
from __future__ import annotations

import asyncio
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
from .strategy import exit_reason, hype_score, momentum_score, screen_rejections

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

    # ------------------------------------------------------------------ entries
    async def scan_once(self, now: float | None = None) -> list[str]:
        """One discovery pass. Returns mints bought."""
        now = now if now is not None else time.time()
        if len(self.state.positions) >= self.cfg.max_open_positions:
            return []
        if self.daily_limit_hit():
            if not self._halt_logged:
                log.warning("Daily loss limit reached (%s today); not opening new positions until UTC midnight",
                            sol(self.state.realized_today()))
                self._halt_logged = True
            return []
        self._halt_logged = False

        mints = [m for m in await self.dex.latest_token_addresses() if self._eligible(m, now)]
        if not mints:
            return []
        candidates = await self.dex.candidates(mints)
        passing: list[TokenCandidate] = []
        for c in candidates:
            reasons = screen_rejections(c, self.cfg, now * 1000)
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
        report = await self.safety.inspect(c.mint)
        reasons = safety_rejections(report, cfg)
        if reasons:
            log.info("reject %s (%s): %s", c.symbol, c.mint[:6], "; ".join(reasons))
            if not report.errors:  # only blacklist on real findings, not transient RPC errors
                self.state.blacklist[c.mint] = "; ".join(reasons)
            return False

        balance = await self.ex.sol_balance()
        reserve = int(cfg.min_sol_reserve * LAMPORTS_PER_SOL)
        if balance - cfg.buy_amount_lamports - FEE_BUFFER_LAMPORTS < reserve:
            log.warning("balance %s too low to buy %s and keep %s reserve", sol(balance), sol(cfg.buy_amount_lamports), sol(reserve))
            return False

        try:
            quote = await self.jup.quote(SOL_MINT, c.mint, cfg.buy_amount_lamports, cfg.buy_slippage_bps)
            impact = price_impact_pct(quote)
            if impact > cfg.max_price_impact_pct:
                log.info("reject %s: price impact %.2f%%", c.symbol, impact)
                return False
            # Honeypot / sell-tax check: can we sell back what we would buy, and for how much?
            back = await self.jup.quote(c.mint, SOL_MINT, out_amount(quote), cfg.sell_slippage_bps)
            round_trip_loss = (1 - out_amount(back) / cfg.buy_amount_lamports) * 100
            if round_trip_loss > cfg.max_round_trip_loss_pct:
                log.info("reject %s: round-trip loss %.1f%% (possible tax/honeypot)", c.symbol, round_trip_loss)
                self.state.blacklist[c.mint] = f"round-trip loss {round_trip_loss:.1f}%"
                return False
        except NoRouteError as exc:
            log.info("reject %s: no Jupiter route (%s)", c.symbol, exc)
            return False

        if cfg.dip_trigger_pct > 0 and c.price_change_m5_pct >= cfg.dip_trigger_pct:
            price = cfg.buy_amount_lamports / max(out_amount(quote), 1)
            self.watchlist[c.mint] = Watch(c, now, price, price)
            log.info("WATCH %s (%s): up %+.1f%% in 5m, waiting for a %.0f%% dip before buying",
                     c.symbol, c.mint, c.price_change_m5_pct, cfg.dip_pct)
            return False
        return await self._execute_buy(c, quote, now)

    async def _execute_buy(self, c: TokenCandidate, quote: dict, now: float, note: str = "") -> bool:
        cfg = self.cfg
        log.info("BUY %s (%s) %s | liq $%s mcap $%s 5m %+.1f%% b/s %.2f hype %.0f", c.symbol, c.mint,
                 sol(cfg.buy_amount_lamports), f"{c.liquidity_usd:,.0f}", f"{c.market_cap_usd:,.0f}",
                 c.price_change_m5_pct, c.buy_sell_ratio_m5, hype_score(c))
        if note:
            log.info("    %s", note)
        try:
            res = await self.ex.buy(c.mint, cfg.buy_amount_lamports, quote)
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
        bought: list[str] = []
        for mint, w in list(self.watchlist.items()):
            sym = w.candidate.symbol
            if (now - w.added_at) / 60 > cfg.dip_watch_minutes:
                log.info("UNWATCH %s: no dip within %.0f min", sym, cfg.dip_watch_minutes)
                del self.watchlist[mint]
                continue
            try:
                quote = await self.jup.quote(SOL_MINT, mint, cfg.buy_amount_lamports, cfg.buy_slippage_bps)
            except NoRouteError:
                continue
            except (httpx.HTTPError, ValueError) as exc:
                log.debug("watch quote %s failed: %s", sym, exc)
                continue
            price = cfg.buy_amount_lamports / max(out_amount(quote), 1)
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
            if len(self.state.positions) >= cfg.max_open_positions or self.daily_limit_hit():
                continue  # keep watching until a slot frees up
            if price_impact_pct(quote) > cfg.max_price_impact_pct:
                continue
            del self.watchlist[mint]
            if await self._execute_buy(w.candidate, quote, now, note=f"bought the dip: {dip:.1f}% below its high"):
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
                if pos.no_route_count >= NO_ROUTE_SELL_AFTER and await self.close(pos, "no_route"):
                    sold.append(mint)
                continue
            except (httpx.HTTPError, ValueError) as exc:
                log.warning("%s: quote failed: %s", pos.symbol, exc)
                continue
            reason = exit_reason(pos, value, now, self.cfg)
            log.debug("%s value %s (%+.1f%%, peak %+.1f%%)", pos.symbol, sol(value), pos.pnl_pct(value),
                      pos.pnl_pct(pos.peak_value_lamports))
            if reason:
                if await self.close(pos, reason):
                    sold.append(mint)
            else:
                await self._maybe_add(pos, value, now)
        if self.state.positions:
            self._persist()
        return sold

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

    async def close(self, pos: Position, reason: str) -> bool:
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
        self._persist()
        log.info("sold %s for %s, P&L %s (%+.1f%%) | today %s", pos.symbol, sol(res.lamports), sol(pnl),
                 pnl / pos.entry_lamports * 100, sol(self.state.realized_today()))
        return True

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

    async def run(self) -> None:
        next_scan = 0.0
        while not self._stop.is_set():
            try:
                await self.monitor_once()
                await self.watch_once()
                if time.monotonic() >= next_scan:
                    next_scan = time.monotonic() + self.cfg.scan_interval_sec
                    await self.scan_once()
            except Exception:  # keep the bot alive; positions still need managing
                log.exception("loop error")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.cfg.monitor_interval_sec)
            except asyncio.TimeoutError:
                pass
        self._persist()
