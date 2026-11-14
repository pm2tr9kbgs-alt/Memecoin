"""DexScreener public API client used to discover and score new Solana tokens."""
from __future__ import annotations

import logging

import httpx

from .models import TokenCandidate

log = logging.getLogger(__name__)

BASE_URL = "https://api.dexscreener.com"
CHAIN = "solana"
BATCH = 30  # max addresses per /tokens/v1 request


class DexScreener:
    def __init__(self, client: httpx.AsyncClient, base_url: str = BASE_URL):
        self.client = client
        self.base_url = base_url.rstrip("/")

    async def _get(self, path: str):
        resp = await self.client.get(f"{self.base_url}{path}", timeout=15)
        resp.raise_for_status()
        return resp.json()

    async def latest_token_addresses(self) -> list[str]:
        """New token profiles and recently boosted tokens on Solana, newest first, de-duplicated."""
        seen: dict[str, None] = {}
        for path in ("/token-profiles/latest/v1", "/token-boosts/latest/v1"):
            try:
                data = await self._get(path)
            except (httpx.HTTPError, ValueError) as exc:
                log.warning("DexScreener %s failed: %s", path, exc)
                continue
            if isinstance(data, dict):
                data = [data]
            for item in data or []:
                if item.get("chainId") == CHAIN and item.get("tokenAddress"):
                    seen.setdefault(item["tokenAddress"], None)
        return list(seen)

    async def candidates(self, mints: list[str]) -> list[TokenCandidate]:
        """Best (most liquid) Solana pair for each mint, where the mint is the base token."""
        best: dict[str, dict] = {}
        for i in range(0, len(mints), BATCH):
            chunk = mints[i : i + BATCH]
            try:
                pairs = await self._get(f"/tokens/v1/{CHAIN}/{','.join(chunk)}")
            except (httpx.HTTPError, ValueError) as exc:
                log.warning("DexScreener pairs lookup failed: %s", exc)
                continue
            for pair in pairs or []:
                if pair.get("chainId") != CHAIN:
                    continue
                mint = (pair.get("baseToken") or {}).get("address")
                if mint not in chunk:
                    continue  # token is the quote side of this pair; skip
                liq = float((pair.get("liquidity") or {}).get("usd") or 0)
                cur = best.get(mint)
                if cur is None or liq > float((cur.get("liquidity") or {}).get("usd") or 0):
                    best[mint] = pair
        return [TokenCandidate.from_pair(p) for p in best.values()]
