"""On-chain and RugCheck safety checks for a token before buying."""
from __future__ import annotations

import logging

import httpx
from solders.pubkey import Pubkey

from .config import Config
from .models import SafetyReport
from .rpc import RPCError, SolanaRPC

log = logging.getLogger(__name__)

RUGCHECK_URL = "https://api.rugcheck.xyz/v1/tokens/{mint}/report/summary"


def is_wallet(owner: str) -> bool:
    """True if the owner is a normal keypair wallet (on the ed25519 curve).

    Liquidity pools, bonding curves and vaults are program-derived addresses (off-curve),
    so excluding off-curve owners leaves only real holder wallets in the concentration check.
    """
    try:
        return Pubkey.from_string(owner).is_on_curve()
    except ValueError:
        return False


def holder_concentration(largest: list[dict], owners: dict[str, str], supply: int) -> tuple[float, float]:
    """(top10 %, top1 %) of supply held by wallets, merging accounts with the same owner."""
    if supply <= 0:
        return 0.0, 0.0
    per_owner: dict[str, int] = {}
    for acct in largest:
        owner = owners.get(acct["address"])
        if owner is None or not is_wallet(owner):
            continue
        per_owner[owner] = per_owner.get(owner, 0) + acct["amount"]
    amounts = sorted(per_owner.values(), reverse=True)
    top10 = sum(amounts[:10]) / supply * 100
    top1 = (amounts[0] / supply * 100) if amounts else 0.0
    return top10, top1


class SafetyChecker:
    def __init__(self, rpc: SolanaRPC, client: httpx.AsyncClient, cfg: Config):
        self.rpc = rpc
        self.client = client
        self.cfg = cfg

    async def inspect(self, mint: str) -> SafetyReport:
        report = SafetyReport(mint=mint)
        try:
            info = await self.rpc.get_mint_info(mint)
            report.mint_authority = info["mint_authority"]
            report.freeze_authority = info["freeze_authority"]
            report.decimals = info["decimals"]
            report.supply = info["supply"]
            largest = await self.rpc.get_largest_accounts(mint)
            owners = await self.rpc.get_token_account_owners([a["address"] for a in largest])
            report.top10_holders_pct, report.top_holder_pct = holder_concentration(largest, owners, report.supply)
        except RPCError as exc:
            report.errors.append(f"rpc: {exc}")

        if self.cfg.use_rugcheck:
            try:
                resp = await self.client.get(RUGCHECK_URL.format(mint=mint), timeout=10)
                resp.raise_for_status()
                data = resp.json()
                score = data.get("score_normalised", data.get("score"))
                report.rugcheck_score = float(score) if score is not None else None
                report.rugcheck_dangers = [
                    r.get("name", "?") for r in data.get("risks", []) if str(r.get("level", "")).lower() == "danger"
                ]
            except (httpx.HTTPError, ValueError) as exc:
                log.debug("RugCheck lookup failed for %s: %s", mint, exc)
                if self.cfg.rugcheck_required:
                    report.errors.append(f"rugcheck: {exc}")
        return report


def safety_rejections(report: SafetyReport, cfg: Config) -> list[str]:
    reasons = list(report.errors)
    if cfg.require_mint_authority_revoked and report.mint_authority:
        reasons.append("mint authority not revoked")
    if cfg.require_freeze_authority_revoked and report.freeze_authority:
        reasons.append("freeze authority not revoked")
    if report.top10_holders_pct > cfg.max_top10_holders_pct:
        reasons.append(f"top10 wallets hold {report.top10_holders_pct:.1f}%")
    if report.top_holder_pct > cfg.max_single_holder_pct:
        reasons.append(f"top wallet holds {report.top_holder_pct:.1f}%")
    if cfg.use_rugcheck:
        if report.rugcheck_dangers:
            reasons.append("rugcheck dangers: " + ", ".join(report.rugcheck_dangers))
        if report.rugcheck_score is not None and report.rugcheck_score > cfg.max_rugcheck_score:
            reasons.append(f"rugcheck score {report.rugcheck_score:.0f}")
    return reasons
