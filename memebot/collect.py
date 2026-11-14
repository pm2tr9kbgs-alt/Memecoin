"""Market data collector for tuning: snapshots new Solana tokens on DexScreener to a CSV.

Usage: python -m memebot.collect --minutes 30 --out snapshots.csv
Every token seen is re-snapshotted each cycle for --track-minutes, so you can measure
what happened after a token would have passed (or failed) the entry filters.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import os
import time

import httpx

from .dexscreener import DexScreener

FIELDS = ["ts", "mint", "symbol", "dex", "price_usd", "liquidity_usd", "market_cap_usd", "volume_m5_usd",
          "volume_h1_usd", "buys_m5", "sells_m5", "price_change_m5_pct", "price_change_h1_pct", "age_min"]


async def collect(minutes: float, out: str, interval: float, track_minutes: float) -> None:
    first_seen: dict[str, float] = {}
    new = not os.path.exists(out)
    end = time.time() + minutes * 60
    async with httpx.AsyncClient(headers={"User-Agent": "memebot-collector/1.0"}) as client:
        dex = DexScreener(client)
        with open(out, "a", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=FIELDS)
            if new:
                w.writeheader()
            while time.time() < end:
                now = time.time()
                for m in await dex.latest_token_addresses():
                    first_seen.setdefault(m, now)
                tracked = [m for m, t in first_seen.items() if now - t <= track_minutes * 60]
                rows = 0
                for c in await dex.candidates(tracked):
                    age = c.age_minutes(now * 1000)
                    w.writerow({"ts": int(now), "mint": c.mint, "symbol": c.symbol, "dex": c.dex_id,
                                "price_usd": c.price_usd, "liquidity_usd": c.liquidity_usd,
                                "market_cap_usd": c.market_cap_usd, "volume_m5_usd": c.volume_m5_usd,
                                "volume_h1_usd": c.volume_h1_usd, "buys_m5": c.buys_m5, "sells_m5": c.sells_m5,
                                "price_change_m5_pct": c.price_change_m5_pct,
                                "price_change_h1_pct": c.price_change_h1_pct,
                                "age_min": "" if age is None else round(age, 1)})
                    rows += 1
                fh.flush()
                print(f"{time.strftime('%H:%M:%S')} tracked {len(tracked)} tokens, wrote {rows} rows", flush=True)
                await asyncio.sleep(max(0, interval - (time.time() - now)))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--minutes", type=float, default=60)
    ap.add_argument("--out", default="snapshots.csv")
    ap.add_argument("--interval", type=float, default=30)
    ap.add_argument("--track-minutes", type=float, default=90)
    a = ap.parse_args()
    asyncio.run(collect(a.minutes, a.out, a.interval, a.track_minutes))


if __name__ == "__main__":
    main()
