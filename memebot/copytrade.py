"""Copy trading: mirror the buys and sells of chosen wallets.

Every COPY_POLL_SEC it asks the RPC for each wallet's new transactions. When a wallet buys a token, the bot runs the
same safety checks as the momentum bot (rug checks + buy/sell quote test) and buys COPY_BUY_AMOUNT_SOL of it. When
that wallet later sells, the bot sells the same share of its own position (all of it if they sold everything).
There is no other exit unless COPY_STOP_LOSS_PCT is set.

With the free public RPC, a copied trade lands a few seconds after the original; a paid RPC is faster.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import replace

import httpx

from .bot import FEE_BUFFER_LAMPORTS, sol
from .config import LAMPORTS_PER_SOL, SOL_MINT, Config
from .dexscreener import DexScreener
from .executor import Executor, TxFailed
from .jupiter import Jupiter, NoRouteError, out_amount, price_impact_pct
from .models import Position
from .rpc import RPCError, SolanaRPC
from .safety import SafetyChecker, safety_rejections
from .state import BotState, log_trade
from .swaps import Swap, parse_swaps

log = logging.getLogger("memebot.copy")

TX_OPTS = {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 1, "commitment": "confirmed"}


def short(wallet: str) -> str:
    return f"{wallet[:4]}..{wallet[-4:]}"


class CopyTrader:
    def __init__(self, cfg: Config, rpc: SolanaRPC, safety: SafetyChecker, jupiter: Jupiter, executor: Executor,
                 state: BotState, dex: DexScreener | None = None):
        self.cfg = cfg
        self.rpc = rpc
        self.safety = safety
        self.jup = jupiter
        self.ex = executor
        self.state = state
        self.dex = dex
        self.wallets = cfg.copy_wallet_list
        self._stop = asyncio.Event()

    # ------------------------------------------------------------------ helpers
    def _persist(self) -> None:
        if not self.cfg.is_live:
            self.state.paper_balance = getattr(self.ex, "balance", None)
        self.state.save()

    def daily_limit_hit(self) -> bool:
        return self.state.realized_today() <= -int(self.cfg.daily_loss_limit_sol * LAMPORTS_PER_SOL)

    async def _symbol(self, mint: str) -> str:
        if self.dex is None:
            return mint[:6]
        try:
            cands = await self.dex.candidates([mint])
            return cands[0].symbol if cands else mint[:6]
        except (httpx.HTTPError, ValueError, KeyError):
            return mint[:6]

    # ------------------------------------------------------------------ watching
    async def start(self) -> None:
        """Begin from each wallet's latest transaction, so old trades aren't copied."""
        for w in self.wallets:
            if w in self.state.copy_cursor:
                continue
            sigs = await self.rpc.call("getSignaturesForAddress", [w, {"limit": 1}])
            if sigs:
                self.state.copy_cursor[w] = sigs[0]["signature"]
        self._persist()

    async def new_swaps(self, wallet: str) -> list[Swap]:
        """The wallet's swaps since the last poll, oldest first."""
        opts: dict = {"limit": 25}
        cursor = self.state.copy_cursor.get(wallet)
        if cursor:
            opts["until"] = cursor
        sigs = await self.rpc.call("getSignaturesForAddress", [wallet, opts])
        if not sigs:
            return []
        self.state.copy_cursor[wallet] = sigs[0]["signature"]
        swaps: list[Swap] = []
        for s in reversed(sigs):
            if s.get("err") is not None:
                continue
            try:
                tx = await self.rpc.call("getTransaction", [s["signature"], TX_OPTS])
            except RPCError as exc:
                log.warning("%s: could not read %s: %s", short(wallet), s["signature"][:8], exc)
                continue
            swaps += [sw for sw in parse_swaps(tx, s["signature"]) if sw.wallet == wallet]
        return swaps

    async def poll_once(self, now: float | None = None) -> None:
        for wallet in self.wallets:
            try:
                swaps = await self.new_swaps(wallet)
            except RPCError as exc:
                log.warning("%s: %s", short(wallet), exc)
                continue
            for sw in swaps:
                if sw.is_buy:
                    await self.on_buy(sw, now if now is not None else time.time())
                elif sw.is_sell:
                    await self.on_sell(sw)
        self._persist()

    # ------------------------------------------------------------------ copying
    async def on_buy(self, sw: Swap, now: float) -> bool:
        cfg = self.cfg
        who = short(sw.wallet)
        pos = self.state.positions.get(sw.mint)
        if pos is not None:
            if pos.copied_from == sw.wallet:
                pos.leader_tokens = sw.token_after  # they added; keep their total so partial sells mirror correctly
            return False
        leader_sol = -sw.sol_delta / LAMPORTS_PER_SOL
        delay = now - sw.time
        if leader_sol < cfg.copy_min_leader_sol:
            log.debug("%s bought %s with %.3f SOL: too small to copy", who, sw.mint[:6], leader_sol)
            return False
        symbol = await self._symbol(sw.mint)
        log.info("%s BOUGHT %s (%s) for %.2f SOL, %.0fs ago", who, symbol, sw.mint, leader_sol, delay)
        if delay > cfg.copy_max_delay_sec:
            log.info("  skip: noticed %.0fs after their buy (limit %.0fs)", delay, cfg.copy_max_delay_sec)
            return False
        if sw.mint in self.state.blacklist:
            log.info("  skip: blacklisted (%s)", self.state.blacklist[sw.mint])
            return False
        if len(self.state.positions) >= cfg.max_open_positions:
            log.info("  skip: already %d open positions", len(self.state.positions))
            return False
        if self.daily_limit_hit():
            log.info("  skip: daily loss limit reached")
            return False
        amount = cfg.copy_buy_lamports
        balance = await self.ex.sol_balance()
        if balance - amount - FEE_BUFFER_LAMPORTS < int(cfg.min_sol_reserve * LAMPORTS_PER_SOL):
            log.warning("  skip: balance %s too low", sol(balance))
            return False

        report = await self.safety.inspect(sw.mint)
        reasons = safety_rejections(report, cfg)
        if reasons:
            log.info("  skip: %s", "; ".join(reasons))
            if not report.errors:
                self.state.blacklist[sw.mint] = "; ".join(reasons)
            return False
        try:
            quote = await self.jup.quote(SOL_MINT, sw.mint, amount, cfg.buy_slippage_bps)
            impact = price_impact_pct(quote)
            if impact > cfg.max_price_impact_pct:
                log.info("  skip: price impact %.2f%%", impact)
                return False
            back = await self.jup.quote(sw.mint, SOL_MINT, out_amount(quote), cfg.sell_slippage_bps)
            round_trip = (1 - out_amount(back) / amount) * 100
            if round_trip > cfg.max_round_trip_loss_pct:
                log.info("  skip: round-trip loss %.1f%% (possible tax/honeypot)", round_trip)
                self.state.blacklist[sw.mint] = f"round-trip loss {round_trip:.1f}%"
                return False
            res = await self.ex.buy(sw.mint, amount, quote)
        except (NoRouteError, TxFailed, RPCError, httpx.HTTPError) as exc:
            log.error("  copy buy failed: %s", exc)
            return False
        if res.token_amount <= 0:
            log.error("  copy buy confirmed (%s) but no tokens detected; check wallet manually", res.signature)
            return False
        pos = Position(mint=sw.mint, symbol=symbol, entry_lamports=res.lamports, token_amount=res.token_amount,
                       opened_at=now, entry_signature=res.signature, peak_value_lamports=res.lamports,
                       last_value_lamports=res.lamports, copied_from=sw.wallet, leader_tokens=sw.token_after)
        self.state.positions[sw.mint] = pos
        self.state.traded_mints[sw.mint] = now
        log_trade(cfg.trades_csv, cfg.mode, "buy", pos, res.lamports, res.token_amount, res.signature,
                  f"copy {who}")
        self._persist()
        log.info("  COPIED: bought %s for %s, %.1fs after them", symbol, sol(res.lamports), time.time() - sw.time)
        return True

    async def on_sell(self, sw: Swap) -> bool:
        pos = self.state.positions.get(sw.mint)
        if pos is None or pos.copied_from != sw.wallet:
            return False
        before = sw.token_after - sw.token_delta  # their balance before this sale
        share = min(-sw.token_delta / before, 1.0) if before > 0 else 1.0
        pos.leader_tokens = sw.token_after
        if share >= 0.95 or sw.token_after <= before * 0.05:
            share = 1.0
        log.info("%s SOLD %.0f%% of their %s; copying", short(sw.wallet), share * 100, pos.symbol)
        return await self.sell(pos, share, f"copy_sell {short(sw.wallet)}")

    async def sell(self, pos: Position, share: float, reason: str) -> bool:
        amount = pos.token_amount if share >= 1.0 else int(pos.token_amount * share)
        if amount <= 0:
            return False
        try:
            res = await self.ex.sell(pos.mint, amount)
        except (TxFailed, RPCError, httpx.HTTPError, NoRouteError) as exc:
            pos.sell_failures += 1
            log.error("sell %s failed (%d): %s", pos.symbol, pos.sell_failures, exc)
            return False
        cost = int(pos.entry_lamports * res.token_amount / pos.token_amount)
        pnl = res.lamports - cost
        pos.token_amount -= res.token_amount
        pos.entry_lamports -= cost
        pos.realized_lamports += pnl
        self.state.record_realized(pnl)
        closed = pos.token_amount <= 0
        if closed:
            self.state.positions.pop(pos.mint, None)
        # log against the cost of the part sold, so pnl_pct is that sale's own return
        log_trade(self.cfg.trades_csv, self.cfg.mode, "sell" if closed else "partial", replace(pos, entry_lamports=max(cost, 1)),
                  res.lamports, res.token_amount, res.signature, reason, pnl)
        self._persist()
        log.info("  sold %s of %s for %s, P&L %s (%+.1f%%)%s", "all" if closed else f"{share:.0%}", pos.symbol,
                 sol(res.lamports), sol(pnl), pnl / max(cost, 1) * 100,
                 f" | trade total {sol(pos.realized_lamports)}" if closed else "")
        return True

    async def check_stops(self) -> None:
        """Value open copied positions; sell on the optional own stop-loss."""
        for pos in list(self.state.positions.values()):
            try:
                quote = await self.jup.quote(pos.mint, SOL_MINT, pos.token_amount, self.cfg.sell_slippage_bps)
            except (NoRouteError, httpx.HTTPError, ValueError):
                continue
            value = out_amount(quote)
            pos.last_value_lamports = value
            pos.peak_value_lamports = max(pos.peak_value_lamports, value)
            if self.cfg.copy_stop_loss_pct > 0 and pos.pnl_pct(value) <= -self.cfg.copy_stop_loss_pct:
                log.info("STOP-LOSS %s at %+.1f%%", pos.symbol, pos.pnl_pct(value))
                await self.sell(pos, 1.0, "copy_stop_loss")

    # ------------------------------------------------------------------ loop
    def stop(self) -> None:
        self._stop.set()

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    async def run(self) -> None:
        if not self.wallets:
            log.error("COPY_WALLETS is empty: nothing to copy")
            return
        await self.start()
        log.info("copying %d wallet(s): %s | %s per trade", len(self.wallets),
                 ", ".join(short(w) for w in self.wallets), sol(self.cfg.copy_buy_lamports))
        last_value = 0.0
        while not self._stop.is_set():
            try:
                await self.poll_once()
                if time.monotonic() - last_value >= 15:
                    last_value = time.monotonic()
                    await self.check_stops()
            except Exception:  # keep copying; a bad transaction shouldn't stop the bot
                log.exception("copy loop error")
            await self._sleep(self.cfg.copy_poll_sec)
        self._persist()
