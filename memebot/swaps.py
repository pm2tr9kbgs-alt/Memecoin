"""Read what a wallet bought or sold from a parsed Solana transaction."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from .config import SOL_MINT


@dataclass
class Swap:
    """The fee payer's (trader's) net effect in one transaction for one token."""

    signature: str
    time: float  # block time, unix seconds
    wallet: str
    mint: str
    token_delta: int  # raw token units (+ bought / received, - sold / sent)
    sol_delta: int  # lamports incl. wrapped SOL (+ received, - spent), fees included
    token_after: int  # the wallet's balance of this token after the transaction

    @property
    def is_buy(self) -> bool:
        return self.token_delta > 0 and self.sol_delta < 0

    @property
    def is_sell(self) -> bool:
        return self.token_delta < 0  # incl. moving tokens to another wallet: treat as an exit


def parse_swaps(tx: dict | None, signature: str = "") -> list[Swap]:
    """Net token and SOL change of the transaction's fee payer, per token. Failed transactions give []."""
    if not tx or (tx.get("meta") or {}).get("err") is not None:
        return []
    meta = tx["meta"]
    keys = tx["transaction"]["message"]["accountKeys"]
    wallet = keys[0]["pubkey"] if isinstance(keys[0], dict) else keys[0]
    sol = meta["postBalances"][0] - meta["preBalances"][0]
    deltas: dict[str, int] = defaultdict(int)
    after: dict[str, int] = defaultdict(int)
    for side, sign in (("preTokenBalances", -1), ("postTokenBalances", 1)):
        for b in meta.get(side) or []:
            if b.get("owner") != wallet:
                continue
            amount = int((b.get("uiTokenAmount") or {}).get("amount") or 0)
            if b["mint"] == SOL_MINT:
                sol += sign * amount  # wrapped SOL counts as SOL
            else:
                deltas[b["mint"]] += sign * amount
                if sign > 0:
                    after[b["mint"]] += amount
    t = float(tx.get("blockTime") or 0)
    return [Swap(signature, t, wallet, m, d, sol, after[m]) for m, d in deltas.items() if d != 0]
