"""Trade execution: LiveExecutor signs and sends real swaps; PaperExecutor simulates them from real quotes."""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Protocol

from solders.keypair import Keypair

from .config import SOL_MINT, Config
from .jupiter import Jupiter, out_amount
from .models import TradeResult
from .rpc import RPCError, SolanaRPC
from .wallet import sign_transaction

log = logging.getLogger(__name__)

BASE_FEE_LAMPORTS = 5_000
PAPER_PRIORITY_FEE_LAMPORTS = 100_000  # rough estimate used to keep paper results honest


class TxFailed(RuntimeError):
    pass


class Executor(Protocol):
    async def sol_balance(self) -> int: ...
    async def buy(self, mint: str, lamports: int, quote: dict | None = None) -> TradeResult: ...
    async def sell(self, mint: str, token_amount: int) -> TradeResult: ...
    async def token_balance(self, mint: str) -> int: ...


class LiveExecutor:
    def __init__(self, cfg: Config, rpc: SolanaRPC, jupiter: Jupiter, keypair: Keypair):
        self.cfg = cfg
        self.rpc = rpc
        self.jup = jupiter
        self.kp = keypair
        self.pubkey = str(keypair.pubkey())
        # buys and sells measure balance changes, so they must not overlap (price checks and scanning run concurrently)
        self._trade_lock = asyncio.Lock()

    async def sol_balance(self) -> int:
        return await self.rpc.get_balance(self.pubkey)

    async def token_balance(self, mint: str) -> int:
        return await self.rpc.get_token_balance(self.pubkey, mint)

    async def _send_and_confirm(self, tx_b64: str, timeout: float = 60.0) -> str:
        signed = sign_transaction(tx_b64, self.kp)
        sig = await self.rpc.send_raw_transaction(signed)
        log.info("sent %s", sig)
        deadline = time.monotonic() + timeout
        last_send = time.monotonic()
        while time.monotonic() < deadline:
            await asyncio.sleep(1.5)
            try:
                status = await self.rpc.signature_status(sig)
            except RPCError as exc:
                log.debug("status poll failed: %s", exc)
                status = None
            if status:
                if status.get("err"):
                    raise TxFailed(f"{sig} failed on-chain: {status['err']}")
                if status.get("confirmationStatus") in ("confirmed", "finalized"):
                    return sig
            if time.monotonic() - last_send > 3:  # rebroadcast until it lands or the blockhash expires
                try:
                    await self.rpc.send_raw_transaction(signed)
                except RPCError:
                    pass
                last_send = time.monotonic()
        raise TxFailed(f"{sig} not confirmed within {timeout:.0f}s")

    async def _swap(self, quote: dict, fee_cap: int | None = None) -> str:
        fee_cap = fee_cap if fee_cap is not None else self.cfg.priority_fee_max_lamports
        tx = await self.jup.swap_transaction(quote, self.pubkey, fee_cap)
        return await self._send_and_confirm(tx)

    async def _wait_token_change(self, mint: str, before: int, increase: bool) -> int:
        for _ in range(10):  # RPC nodes can lag a moment behind confirmation
            now = await self.token_balance(mint)
            if (now > before) if increase else (now < before):
                return now
            await asyncio.sleep(1)
        return await self.token_balance(mint)

    async def buy(self, mint: str, lamports: int, quote: dict | None = None) -> TradeResult:
        async with self._trade_lock:
            return await self._buy(mint, lamports, quote)

    async def sell(self, mint: str, token_amount: int) -> TradeResult:
        async with self._trade_lock:
            return await self._sell(mint, token_amount)

    async def _buy(self, mint: str, lamports: int, quote: dict | None = None) -> TradeResult:
        sol_before = await self.sol_balance()
        tok_before = await self.token_balance(mint)
        quote = quote or await self.jup.quote(SOL_MINT, mint, lamports, self.cfg.buy_slippage_bps)
        try:
            sig = await self._swap(quote)
        except TxFailed as exc:
            # A confirmation timeout doesn't mean it didn't land: check before giving up on the tokens.
            if "not confirmed" not in str(exc):
                raise
            tok_now = await self._wait_token_change(mint, tok_before, increase=True)
            if tok_now <= tok_before:
                raise
            log.warning("buy landed despite confirmation timeout: %s", exc)
            sig = str(exc).split()[0]
        tok_after = await self._wait_token_change(mint, tok_before, increase=True)
        sol_after = await self.sol_balance()
        return TradeResult(sig, sol_before - sol_after, tok_after - tok_before)

    async def _sell(self, mint: str, token_amount: int) -> TradeResult:
        sol_before = await self.sol_balance()
        tok_before = await self.token_balance(mint)
        amount = min(token_amount, tok_before)
        if amount <= 0:
            raise TxFailed(f"no {mint} balance to sell")
        quote = await self.jup.quote(mint, SOL_MINT, amount, self.cfg.sell_slippage_bps)
        sig = await self._swap(quote, int(self.cfg.priority_fee_max_lamports * self.cfg.sell_priority_fee_multiplier))
        tok_after = await self._wait_token_change(mint, tok_before, increase=False)
        sol_after = await self.sol_balance()
        return TradeResult(sig, sol_after - sol_before, tok_before - tok_after)


class PaperExecutor:
    """Fills at Jupiter's quoted output minus estimated fees. Tracks balances in memory."""

    def __init__(self, cfg: Config, jupiter: Jupiter, balance_lamports: int, holdings: dict[str, int] | None = None):
        self.cfg = cfg
        self.jup = jupiter
        self.balance = balance_lamports
        self.holdings: dict[str, int] = dict(holdings or {})

    async def sol_balance(self) -> int:
        return self.balance

    async def token_balance(self, mint: str) -> int:
        return self.holdings.get(mint, 0)

    async def buy(self, mint: str, lamports: int, quote: dict | None = None) -> TradeResult:
        quote = quote or await self.jup.quote(SOL_MINT, mint, lamports, self.cfg.buy_slippage_bps)
        fee = BASE_FEE_LAMPORTS + PAPER_PRIORITY_FEE_LAMPORTS
        cost = lamports + fee
        if cost > self.balance:
            raise TxFailed("insufficient paper balance")
        got = out_amount(quote)
        self.balance -= cost
        self.holdings[mint] = self.holdings.get(mint, 0) + got
        return TradeResult(f"paper-{uuid.uuid4().hex[:12]}", cost, got)

    async def sell(self, mint: str, token_amount: int) -> TradeResult:
        amount = min(token_amount, self.holdings.get(mint, 0))
        if amount <= 0:
            raise TxFailed(f"no paper {mint} balance to sell")
        quote = await self.jup.quote(mint, SOL_MINT, amount, self.cfg.sell_slippage_bps)
        received = max(out_amount(quote) - BASE_FEE_LAMPORTS - PAPER_PRIORITY_FEE_LAMPORTS, 0)
        self.balance += received
        self.holdings[mint] -= amount
        if self.holdings[mint] <= 0:
            del self.holdings[mint]
        return TradeResult(f"paper-{uuid.uuid4().hex[:12]}", received, amount)
