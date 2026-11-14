"""Jupiter Swap API client: quotes and unsigned swap transactions."""
from __future__ import annotations

import httpx


class NoRouteError(RuntimeError):
    """Jupiter could not find a route (no liquidity, or the token is untradeable)."""


class Jupiter:
    def __init__(self, client: httpx.AsyncClient, base_url: str, api_key: str | None = None):
        self.client = client
        self.base_url = base_url.rstrip("/")
        self.headers = {"x-api-key": api_key} if api_key else {}

    async def quote(self, input_mint: str, output_mint: str, amount: int, slippage_bps: int) -> dict:
        params = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(int(amount)),
            "slippageBps": str(int(slippage_bps)),
            "restrictIntermediateTokens": "true",
        }
        resp = await self.client.get(f"{self.base_url}/quote", params=params, headers=self.headers, timeout=15)
        if resp.status_code in (400, 404):
            raise NoRouteError(resp.text[:300])
        resp.raise_for_status()
        data = resp.json()
        if "error" in data or "outAmount" not in data:
            raise NoRouteError(str(data.get("error", data))[:300])
        return data

    async def swap_transaction(self, quote: dict, user_pubkey: str, priority_fee_max_lamports: int) -> str:
        """Returns a base64-encoded, unsigned VersionedTransaction."""
        body = {
            "quoteResponse": quote,
            "userPublicKey": user_pubkey,
            "wrapAndUnwrapSol": True,
            "dynamicComputeUnitLimit": True,
            "dynamicSlippage": False,
            "prioritizationFeeLamports": {
                "priorityLevelWithMaxLamports": {
                    "maxLamports": int(priority_fee_max_lamports),
                    "priorityLevel": "veryHigh",
                }
            },
        }
        resp = await self.client.post(f"{self.base_url}/swap", json=body, headers=self.headers, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        if "swapTransaction" not in data:
            raise RuntimeError(f"Jupiter swap failed: {data}")
        return data["swapTransaction"]


def out_amount(quote: dict) -> int:
    return int(quote["outAmount"])


def price_impact_pct(quote: dict) -> float:
    # Jupiter returns priceImpactPct as a fraction string, e.g. "0.0123" == 1.23%
    try:
        return abs(float(quote.get("priceImpactPct", 0))) * 100
    except (TypeError, ValueError):
        return 0.0
