"""Replay collected snapshots through entry filters + exit rules to compare filter settings.

Usage: python -m memebot.tune snapshots.csv [--cost-pct 5]
For each filter combination, every token is "bought" at its first snapshot that passes the filters
and "sold" by the bot's exit rules applied to its later snapshots (30s resolution). Results are a
rough guide only: short samples overfit easily, and real fills include slippage beyond --cost-pct.
"""
from __future__ import annotations

import argparse
import csv
import itertools
from collections import defaultdict
from statistics import mean, median


def load(path: str) -> dict[str, list[dict]]:
    by_mint: dict[str, list[dict]] = defaultdict(list)
    with open(path, encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            for k in ("ts", "price_usd", "liquidity_usd", "market_cap_usd", "volume_m5_usd", "buys_m5",
                      "sells_m5", "price_change_m5_pct", "price_change_h1_pct"):
                r[k] = float(r[k] or 0)
            r["age_min"] = float(r["age_min"]) if r["age_min"] else None
            if r["price_usd"] > 0:
                by_mint[r["mint"]].append(r)
    for rows in by_mint.values():
        rows.sort(key=lambda r: r["ts"])
    return by_mint


def passes(r: dict, f: dict) -> bool:
    ratio = r["buys_m5"] / max(r["sells_m5"], 1)
    age = r["age_min"]
    return (r["liquidity_usd"] >= f["min_liq"]
            and f["min_mcap"] <= r["market_cap_usd"] <= f["max_mcap"]
            and r["volume_m5_usd"] >= f["min_vol"]
            and r["buys_m5"] + r["sells_m5"] >= f["min_txns"]
            and ratio >= f["min_ratio"]
            and f["min_chg"] <= r["price_change_m5_pct"] <= f["max_chg"]
            and age is not None and f["min_age"] <= age <= f["max_age"])


def simulate(path: list[dict], entry_i: int, ex: dict) -> tuple[float, str]:
    """Return % P&L (before costs) and exit reason, following the bot's exit rules."""
    p0, t0 = path[entry_i]["price_usd"], path[entry_i]["ts"]
    peak = p0
    for r in path[entry_i + 1:]:
        p = r["price_usd"]
        peak = max(peak, p)
        pnl = (p / p0 - 1) * 100
        if pnl >= ex["tp"]:
            return pnl, "take_profit"
        if pnl <= -ex["sl"]:
            return pnl, "stop_loss"
        if (peak / p0 - 1) * 100 >= ex["trail_act"] and (1 - p / peak) * 100 >= ex["trail"]:
            return pnl, "trailing_stop"
        if (r["ts"] - t0) / 60 >= ex["max_hold"]:
            return pnl, "max_hold"
    return (path[-1]["price_usd"] / p0 - 1) * 100, "open_at_end"


def evaluate(data, f, ex, cost_pct, min_follow_sec=300):
    results = []
    for rows in data.values():
        for i, r in enumerate(rows):
            if passes(r, f):
                if rows[-1]["ts"] - r["ts"] < min_follow_sec:
                    break  # not enough data after entry to judge
                pnl, why = simulate(rows, i, ex)
                results.append((pnl - cost_pct, why, r["symbol"]))
                break
    return results


GRID = {
    "min_liq": [0, 5_000, 15_000, 30_000],
    "min_mcap": [10_000, 30_000],
    "max_mcap": [5_000_000],
    "min_vol": [1_000, 5_000, 15_000],
    "min_txns": [10, 30],
    "min_ratio": [1.0, 1.3, 1.8],
    "min_chg": [-5, 0, 3, 8],
    "max_chg": [40, 100],
    "min_age": [5, 10],
    "max_age": [1440, 100_000],
}
DEFAULT_FILTERS = dict(min_liq=15_000, min_mcap=30_000, max_mcap=5_000_000, min_vol=5_000, min_txns=30,
                       min_ratio=1.3, min_chg=3, max_chg=40, min_age=10, max_age=1440)
DEFAULT_EXITS = dict(tp=60, sl=20, trail_act=25, trail=15, max_hold=120)


def summarize(res):
    pnls = [p for p, _, _ in res]
    return dict(n=len(pnls), mean=mean(pnls), median=median(pnls),
                win=sum(p > 0 for p in pnls) / len(pnls) * 100, total=sum(pnls))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("snapshots")
    ap.add_argument("--cost-pct", type=float, default=5.0, help="round-trip fees + slippage per trade, %%")
    ap.add_argument("--min-trades", type=int, default=5)
    a = ap.parse_args()
    data = load(a.snapshots)
    span = max(r["ts"] for rows in data.values() for r in rows) - min(r["ts"] for rows in data.values() for r in rows)
    print(f"{len(data)} tokens, {sum(map(len, data.values()))} snapshots over {span / 60:.0f} min\n")

    base = evaluate(data, DEFAULT_FILTERS, DEFAULT_EXITS, a.cost_pct)
    print("current default filters:", summarize(base) if base else "no trades")
    for p, why, sym in base:
        print(f"   {sym:<12} {p:+7.1f}%  {why}")

    keys = list(GRID)
    scored = []
    for combo in itertools.product(*(GRID[k] for k in keys)):
        f = dict(zip(keys, combo))
        res = evaluate(data, f, DEFAULT_EXITS, a.cost_pct)
        if len(res) >= a.min_trades:
            scored.append((summarize(res), f))
    scored.sort(key=lambda s: (s[0]["mean"], s[0]["n"]), reverse=True)
    print(f"\ntop filter sets (>= {a.min_trades} trades, default exits, {a.cost_pct}% cost per trade):")
    for s, f in scored[:8]:
        print(f"  n={s['n']:<3} mean {s['mean']:+6.1f}%  median {s['median']:+6.1f}%  win {s['win']:4.0f}%  {f}")

    if scored:
        best_f = scored[0][1]
        print("\nexit settings with the best filters:")
        ex_rows = []
        for tp, sl, ta, tr in itertools.product([30, 60, 100], [10, 20, 30], [15, 25], [10, 15, 25]):
            ex = dict(tp=tp, sl=sl, trail_act=ta, trail=tr, max_hold=120)
            res = evaluate(data, best_f, ex, a.cost_pct)
            if res:
                ex_rows.append((summarize(res), ex))
        ex_rows.sort(key=lambda s: s[0]["mean"], reverse=True)
        for s, ex in ex_rows[:5]:
            print(f"  n={s['n']:<3} mean {s['mean']:+6.1f}%  win {s['win']:4.0f}%  {ex}")


if __name__ == "__main__":
    main()
