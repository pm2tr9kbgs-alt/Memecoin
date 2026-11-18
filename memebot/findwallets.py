"""Find wallets worth copy-trading.

Step 1 (discover): for tokens that ran up hard, read every transaction in the minutes before and during the start
of the run and note which wallets bought early. Wallets that did this on several different runners are candidates.

Step 2 (profile): read each candidate's recent transactions and work out, per token, how much SOL went in and came
back out. That gives realized profit, win rate, trade size and typical holding time, so lucky one-offs, tiny
wallets and sub-minute sniper bots (impossible to copy with a few seconds of delay) can be filtered out.

Uses only the public Solana RPC, so it is slow but free:
    python -m memebot.findwallets --runners runners.json --out wallets.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import statistics
import time
from collections import defaultdict
from dataclasses import dataclass, field

import httpx

from .config import LAMPORTS_PER_SOL
from .rpc import RPCError, SolanaRPC
from .swaps import parse_swaps

log = logging.getLogger("findwallets")

class Reader:
    """Rate-limited reads from the public RPC."""

    def __init__(self, rpc: SolanaRPC, concurrency: int = 4):
        self.rpc = rpc
        self.sem = asyncio.Semaphore(concurrency)

    async def signatures(self, address: str, start: float, end: float, max_pages: int = 150) -> list[dict]:
        """All successful signatures touching `address` with start <= blockTime <= end (newest first)."""
        out: list[dict] = []
        before = None
        for _ in range(max_pages):
            opts = {"limit": 1000}
            if before:
                opts["before"] = before
            async with self.sem:
                page = await self.rpc.call("getSignaturesForAddress", [address, opts])
            if not page:
                return out
            for s in page:
                bt = s.get("blockTime") or 0
                if start <= bt <= end and s.get("err") is None:
                    out.append(s)
            before = page[-1]["signature"]
            if (page[-1].get("blockTime") or 0) < start:
                return out
        raise RPCError(f"{address}: more than {max_pages} pages of history; skipped")

    async def tx(self, signature: str) -> dict | None:
        async with self.sem:
            try:
                return await self.rpc.call("getTransaction", [signature, {
                    "encoding": "jsonParsed", "maxSupportedTransactionVersion": 1, "commitment": "confirmed"}])
            except RPCError as exc:
                log.debug("tx %s: %s", signature[:8], exc)
                return None


async def early_buyers(reader: Reader, runner: dict, before_min: float, after_min: float, max_txs: int,
                       min_buy_sol: float) -> dict[str, float]:
    """wallet -> SOL spent buying `runner['mint']` in the window around the start of its run."""
    start = runner["start"] - before_min * 60
    end = runner["start"] + after_min * 60
    sigs = await reader.signatures(runner["mint"], start, end)
    if len(sigs) > max_txs:  # sample evenly so busy tokens don't take forever
        step = len(sigs) / max_txs
        sigs = [sigs[int(i * step)] for i in range(max_txs)]
    txs = await asyncio.gather(*(reader.tx(s["signature"]) for s in sigs))
    spent: dict[str, float] = defaultdict(float)
    for s, tx in zip(sigs, txs):
        for sw in parse_swaps(tx, s["signature"]):
            if sw.mint == runner["mint"] and sw.token_delta > 0 and -sw.sol_delta >= min_buy_sol * LAMPORTS_PER_SOL:
                spent[sw.wallet] += -sw.sol_delta / LAMPORTS_PER_SOL
    return spent


@dataclass
class Profile:
    wallet: str
    tokens_traded: int = 0
    closed: int = 0
    wins: int = 0
    realized_sol: float = 0.0
    avg_buy_sol: float = 0.0
    median_hold_min: float = 0.0
    trades_per_day: float = 0.0
    runners_bought_early: list[str] = field(default_factory=list)

    @property
    def win_rate(self) -> float:
        return self.wins / self.closed * 100 if self.closed else 0.0


async def profile_wallet(reader: Reader, wallet: str, days: float, max_txs: int) -> Profile:
    """Realized results per token over the last `days`, from the wallet's own transactions."""
    now = time.time()
    try:
        sigs = await reader.signatures(wallet, now - days * 86400, now, max_pages=10)
    except RPCError:
        sigs = []  # extremely active wallet (bot); profile from what we can't read = nothing
    sigs = sigs[:max_txs]
    txs = await asyncio.gather(*(reader.tx(s["signature"]) for s in sigs))
    per_mint: dict[str, dict] = defaultdict(lambda: {"in": 0, "out": 0, "first": None, "last_sell": None,
                                                    "bought": 0, "sold": 0, "buys": []})
    swaps = 0
    for s, tx in zip(sigs, txs):
        for sw in parse_swaps(tx, s["signature"]):
            if sw.wallet != wallet:
                continue
            swaps += 1
            m = per_mint[sw.mint]
            if sw.token_delta > 0 and sw.sol_delta < 0:
                m["in"] += -sw.sol_delta
                m["bought"] += sw.token_delta
                m["buys"].append(-sw.sol_delta)
                m["first"] = sw.time if m["first"] is None else min(m["first"], sw.time)
            elif sw.token_delta < 0 and sw.sol_delta > 0:
                m["out"] += sw.sol_delta
                m["sold"] += -sw.token_delta
                m["last_sell"] = sw.time if m["last_sell"] is None else max(m["last_sell"], sw.time)
    p = Profile(wallet)
    holds: list[float] = []
    buy_sizes: list[int] = []
    for mint, m in per_mint.items():
        if not m["in"]:
            continue  # sells of tokens bought before the window: can't judge
        p.tokens_traded += 1
        buy_sizes += m["buys"]
        if m["bought"] and m["sold"] >= 0.9 * m["bought"]:  # (mostly) closed: realized result is meaningful
            p.closed += 1
            pnl = m["out"] - m["in"]
            p.realized_sol += pnl / LAMPORTS_PER_SOL
            p.wins += pnl > 0
            if m["first"] and m["last_sell"]:
                holds.append((m["last_sell"] - m["first"]) / 60)
    p.avg_buy_sol = statistics.mean(buy_sizes) / LAMPORTS_PER_SOL if buy_sizes else 0.0
    p.median_hold_min = statistics.median(holds) if holds else 0.0
    span_days = max((now - min((s.get("blockTime") or now) for s in sigs)) / 86400, 0.1) if sigs else days
    p.trades_per_day = swaps / span_days
    return p


async def main_async(args) -> None:
    runners = [r for r in json.load(open(args.runners, encoding="utf-8")) if args.min_rise <= r["rise"] <= args.max_rise]
    runners = runners[: args.max_runners]
    async with httpx.AsyncClient() as client:
        reader = Reader(SolanaRPC(client, args.rpc), args.concurrency)
        hits: dict[str, list[str]] = defaultdict(list)
        for i, r in enumerate(runners, 1):
            try:
                spent = await early_buyers(reader, r, args.before_min, args.after_min, args.max_txs, args.min_buy_sol)
            except RPCError as exc:
                log.warning("[%d/%d] %s: %s", i, len(runners), r["sym"], exc)
                continue
            for w in spent:
                hits[w].append(r["sym"])
            log.info("[%d/%d] %s (%.1fx): %d early buyers", i, len(runners), r["sym"], r["rise"], len(spent))
        candidates = sorted((w for w, t in hits.items() if len(set(t)) >= args.min_hits),
                            key=lambda w: -len(set(hits[w])))[: args.max_candidates]
        log.info("%d wallets bought early on %d+ runners; profiling the top %d",
                 sum(1 for t in hits.values() if len(set(t)) >= args.min_hits), args.min_hits, len(candidates))
        profiles: list[Profile] = []
        for w in candidates:
            p = await profile_wallet(reader, w, args.days, args.profile_txs)
            p.runners_bought_early = sorted(set(hits[w]))
            profiles.append(p)
            log.info("%s: %d tokens, %d closed, win %.0f%%, realized %+.2f SOL, avg buy %.2f SOL, hold %.1f min, "
                     "%.0f swaps/day, early on %s", w, p.tokens_traded, p.closed, p.win_rate, p.realized_sol,
                     p.avg_buy_sol, p.median_hold_min, p.trades_per_day, ",".join(p.runners_bought_early))
    good = [p for p in profiles if p.closed >= args.min_closed and p.realized_sol > 0
            and p.median_hold_min >= args.min_hold_min and p.trades_per_day <= args.max_trades_per_day]
    good.sort(key=lambda p: -p.realized_sol)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump({"all": [vars(p) | {"win_rate": p.win_rate} for p in profiles],
                   "recommended": [p.wallet for p in good]}, fh, indent=2)
    log.info("recommended %d wallet(s): %s", len(good), ", ".join(p.wallet for p in good) or "none")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--runners", required=True, help="JSON list of {mint, sym, rise, start} (unix seconds)")
    ap.add_argument("--out", default="wallets.json")
    ap.add_argument("--rpc", default="https://api.mainnet-beta.solana.com")
    ap.add_argument("--min-rise", type=float, default=2.0)
    ap.add_argument("--max-rise", type=float, default=50.0, help="skip absurd rises (usually bad price data)")
    ap.add_argument("--max-runners", type=int, default=40)
    ap.add_argument("--before-min", type=float, default=15.0, help="minutes before the run started")
    ap.add_argument("--after-min", type=float, default=5.0, help="minutes after the run started")
    ap.add_argument("--max-txs", type=int, default=400, help="transactions read per runner window")
    ap.add_argument("--min-buy-sol", type=float, default=0.1, help="ignore smaller buys (dust, bots)")
    ap.add_argument("--min-hits", type=int, default=2, help="runners a wallet must have bought early")
    ap.add_argument("--max-candidates", type=int, default=40)
    ap.add_argument("--days", type=float, default=3.0, help="history to profile")
    ap.add_argument("--profile-txs", type=int, default=600)
    ap.add_argument("--min-closed", type=int, default=5)
    ap.add_argument("--min-hold-min", type=float, default=2.0, help="faster than this can't be copied")
    ap.add_argument("--max-trades-per-day", type=float, default=500)
    ap.add_argument("--concurrency", type=int, default=4)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
