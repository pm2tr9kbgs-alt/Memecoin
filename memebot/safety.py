"""On-chain and RugCheck safety checks for a token before buying."""
from __future__ import annotations

import logging

import httpx
from solders.pubkey import Pubkey

from .config import Config
from .models import SafetyReport
from .rpc import RPCError, SolanaRPC

log = logging.getLogger(__name__)

RUGCHECK_URL = "https://api.rugcheck.xyz/v1/tokens/{mint}/report"


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


def rugcheck_concentration(report: dict) -> tuple[float, float]:
    """(top10 %, top1 %) from a RugCheck full report, excluding pools/markets and program-owned accounts."""
    excluded: set[str] = set(report.get("knownAccounts") or {})
    for m in report.get("markets") or []:
        for key in ("pubkey", "liquidityA", "liquidityB", "liquidityAAccount", "liquidityBAccount"):
            v = m.get(key)
            if isinstance(v, dict):  # some RugCheck fields are account objects
                v = v.get("pubkey") or v.get("address")
            if isinstance(v, str):
                excluded.add(v)
    per_owner: dict[str, float] = {}
    for h in report.get("topHolders") or []:
        owner = h.get("owner") or ""
        if owner in excluded or h.get("address") in excluded or not is_wallet(owner):
            continue
        per_owner[owner] = per_owner.get(owner, 0.0) + float(h.get("pct") or 0)
    pcts = sorted(per_owner.values(), reverse=True)
    return sum(pcts[:10]), (pcts[0] if pcts else 0.0)


def rugcheck_lp_and_creator(report: dict) -> tuple[float | None, float | None]:
    """(% of pool liquidity locked or burned, % of supply in the creator's wallet) from a RugCheck report."""
    lp_pct = None
    total = float(report.get("totalMarketLiquidity") or 0)
    if total > 0:
        locked = sum(float(((m.get("lp") or {}).get("lpLockedUSD")) or 0) for m in report.get("markets") or [])
        lp_pct = min(locked / total * 100, 100.0)
    creator_pct = None
    supply = float((report.get("token") or {}).get("supply") or 0)
    if supply > 0 and report.get("creatorBalance") is not None:
        creator_pct = float(report["creatorBalance"]) / supply * 100
    return lp_pct, creator_pct


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
        except RPCError as exc:
            report.errors.append(f"rpc: {exc}")
            return report

        holders_known = False
        try:
            largest = await self.rpc.get_largest_accounts(mint)
            owners = await self.rpc.get_token_account_owners([a["address"] for a in largest])
            report.top10_holders_pct, report.top_holder_pct = holder_concentration(largest, owners, report.supply)
            holders_known = True
        except RPCError as exc:
            # getTokenLargestAccounts is heavily rate-limited on free RPCs; fall back to RugCheck's holder list
            log.debug("holder lookup via RPC failed for %s: %s", mint, exc)

        if self.cfg.use_rugcheck:
            try:
                resp = await self.client.get(RUGCHECK_URL.format(mint=mint), timeout=15)
                resp.raise_for_status()
                data = resp.json()
                score = data.get("score_normalised", data.get("score"))
                report.rugcheck_score = float(score) if score is not None else None
                report.rugcheck_dangers = [
                    r.get("name", "?") for r in data.get("risks") or [] if str(r.get("level", "")).lower() == "danger"
                ]
                if data.get("rugged"):
                    report.rugcheck_dangers.append("marked rugged")
                fee = (data.get("transferFee") or {}).get("pct") or 0
                if fee:
                    report.rugcheck_dangers.append(f"transfer fee {fee}%")
                report.lp_locked_pct, report.creator_pct = rugcheck_lp_and_creator(data)
                if not holders_known and data.get("topHolders") is not None:
                    report.top10_holders_pct, report.top_holder_pct = rugcheck_concentration(data)
                    holders_known = True
            except (httpx.HTTPError, ValueError, TypeError, KeyError, AttributeError) as exc:
                log.debug("RugCheck lookup failed for %s: %s", mint, exc)
                if self.cfg.rugcheck_required:
                    report.errors.append(f"rugcheck: {exc}")

        if not holders_known:
            report.errors.append("holder data unavailable (RPC rate-limited, no RugCheck data)")
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
        if cfg.min_lp_locked_pct > 0 and report.lp_locked_pct is not None and report.lp_locked_pct < cfg.min_lp_locked_pct:
            reasons.append(f"only {report.lp_locked_pct:.0f}% of liquidity locked (dev could pull it)")
        if cfg.max_creator_pct > 0 and report.creator_pct is not None and report.creator_pct > cfg.max_creator_pct:
            reasons.append(f"creator still holds {report.creator_pct:.1f}% of supply")
    return reasons
