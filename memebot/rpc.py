"""Minimal async Solana JSON-RPC client (only the calls the bot needs)."""
from __future__ import annotations

import asyncio
import itertools
import logging

import httpx

log = logging.getLogger(__name__)


class RPCError(RuntimeError):
    pass


class SolanaRPC:
    def __init__(self, client: httpx.AsyncClient, url: str):
        self.client = client
        self.url = url
        self._ids = itertools.count(1)

    async def call(self, method: str, params: list | None = None, retries: int = 5):
        payload = {"jsonrpc": "2.0", "id": next(self._ids), "method": method, "params": params or []}
        last_exc: Exception | None = None
        for attempt in range(retries):
            try:
                resp = await self.client.post(self.url, json=payload, timeout=20)
                if resp.status_code == 429:
                    raise httpx.HTTPStatusError("rate limited", request=resp.request, response=resp)
                resp.raise_for_status()
                body = resp.json()
                if "error" in body:
                    raise RPCError(f"{method}: {body['error']}")
                return body.get("result")
            except (httpx.HTTPError, ValueError) as exc:
                last_exc = exc
                if attempt < retries - 1:
                    # free public RPCs rate-limit hard (HTTP 429); back off 1, 2, 4, 8s
                    await asyncio.sleep(2**attempt)
        raise RPCError(f"{method} failed after {retries} attempts: {last_exc}")

    async def get_balance(self, pubkey: str) -> int:
        result = await self.call("getBalance", [pubkey, {"commitment": "confirmed"}])
        return int(result["value"])

    async def get_mint_info(self, mint: str) -> dict:
        result = await self.call("getAccountInfo", [mint, {"encoding": "jsonParsed", "commitment": "confirmed"}])
        value = (result or {}).get("value")
        if not value:
            raise RPCError(f"mint {mint} not found")
        parsed = (value.get("data") or {}).get("parsed") or {}
        if parsed.get("type") != "mint":
            raise RPCError(f"{mint} is not a token mint")
        info = parsed["info"]
        return {
            "mint_authority": info.get("mintAuthority"),
            "freeze_authority": info.get("freezeAuthority"),
            "decimals": int(info.get("decimals", 0)),
            "supply": int(info.get("supply", 0)),
            "program": value.get("owner"),
        }

    async def get_largest_accounts(self, mint: str) -> list[dict]:
        """Up to 20 largest token accounts: [{address, amount(int)}]."""
        result = await self.call("getTokenLargestAccounts", [mint, {"commitment": "confirmed"}])
        return [{"address": a["address"], "amount": int(a["amount"])} for a in (result or {}).get("value", [])]

    async def get_token_account_owners(self, addresses: list[str]) -> dict[str, str]:
        if not addresses:
            return {}
        result = await self.call("getMultipleAccounts", [addresses, {"encoding": "jsonParsed", "commitment": "confirmed"}])
        owners: dict[str, str] = {}
        for addr, acct in zip(addresses, (result or {}).get("value", [])):
            try:
                owners[addr] = acct["data"]["parsed"]["info"]["owner"]
            except (TypeError, KeyError):
                continue
        return owners

    async def get_token_balance(self, owner: str, mint: str) -> int:
        """Total raw amount of `mint` held by `owner` across all its token accounts (SPL and Token-2022)."""
        result = await self.call(
            "getTokenAccountsByOwner",
            [owner, {"mint": mint}, {"encoding": "jsonParsed", "commitment": "confirmed"}],
        )
        total = 0
        for acct in (result or {}).get("value", []):
            try:
                total += int(acct["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"])
            except (KeyError, TypeError, ValueError):
                continue
        return total

    async def send_raw_transaction(self, tx_b64: str) -> str:
        return await self.call(
            "sendTransaction",
            [tx_b64, {"encoding": "base64", "skipPreflight": True, "maxRetries": 0}],
            retries=1,
        )

    async def signature_status(self, signature: str) -> dict | None:
        result = await self.call("getSignatureStatuses", [[signature], {"searchTransactionHistory": False}])
        values = (result or {}).get("value") or [None]
        return values[0]
