"""Persistent bot state (open positions, daily P&L, paper balance) and a CSV trade log."""
from __future__ import annotations

import csv
import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .config import LAMPORTS_PER_SOL
from .models import Position


def utc_day(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts if ts is not None else time.time(), tz=timezone.utc).strftime("%Y-%m-%d")


@dataclass
class BotState:
    path: str
    positions: dict[str, Position] = field(default_factory=dict)
    realized_by_day: dict[str, int] = field(default_factory=dict)  # lamports
    traded_mints: dict[str, float] = field(default_factory=dict)  # mint -> last trade time
    blacklist: dict[str, str] = field(default_factory=dict)  # mint -> reason (failed safety)
    pumped: dict[str, float] = field(default_factory=dict)  # mint -> last time its 1h change was above PUMPED_H1_PCT
    loss_streak: int = 0  # losing trades in a row
    paused_until: float = 0.0  # unix time; no new buys before this (losing-streak pause)
    copy_cursor: dict[str, str] = field(default_factory=dict)  # copied wallet -> last signature processed
    paper_balance: int | None = None

    @classmethod
    def load(cls, path: str) -> "BotState":
        if not os.path.exists(path):
            return cls(path=path)
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
        return cls(
            path=path,
            positions={m: Position.from_dict(p) for m, p in raw.get("positions", {}).items()},
            realized_by_day={k: int(v) for k, v in raw.get("realized_by_day", {}).items()},
            traded_mints=raw.get("traded_mints", {}),
            blacklist=raw.get("blacklist", {}),
            pumped=raw.get("pumped", {}),
            loss_streak=int(raw.get("loss_streak", 0)),
            paused_until=float(raw.get("paused_until", 0.0)),
            copy_cursor=raw.get("copy_cursor", {}),
            paper_balance=raw.get("paper_balance"),
        )

    def save(self) -> None:
        data = {
            "positions": {m: p.to_dict() for m, p in self.positions.items()},
            "realized_by_day": self.realized_by_day,
            "traded_mints": self.traded_mints,
            "blacklist": dict(list(self.blacklist.items())[-5000:]),
            "pumped": dict(list(self.pumped.items())[-5000:]),
            "loss_streak": self.loss_streak,
            "paused_until": self.paused_until,
            "copy_cursor": self.copy_cursor,
            "paper_balance": self.paper_balance,
        }
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        os.replace(tmp, self.path)

    def realized_today(self) -> int:
        return self.realized_by_day.get(utc_day(), 0)

    def record_realized(self, lamports: int) -> None:
        day = utc_day()
        self.realized_by_day[day] = self.realized_by_day.get(day, 0) + lamports


TRADE_FIELDS = ["time", "mode", "side", "symbol", "mint", "sol", "tokens", "pnl_sol", "pnl_pct", "reason", "signature"]


def log_trade(path: str, mode: str, side: str, pos: Position, lamports: int, tokens: int,
              signature: str, reason: str = "", pnl_lamports: int | None = None) -> None:
    new = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=TRADE_FIELDS)
        if new:
            w.writeheader()
        w.writerow({
            "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "mode": mode,
            "side": side,
            "symbol": pos.symbol,
            "mint": pos.mint,
            "sol": f"{lamports / LAMPORTS_PER_SOL:.6f}",
            "tokens": tokens,
            "pnl_sol": "" if pnl_lamports is None else f"{pnl_lamports / LAMPORTS_PER_SOL:+.6f}",
            "pnl_pct": "" if pnl_lamports is None else f"{pnl_lamports / pos.entry_lamports * 100:+.2f}",
            "reason": reason,
            "signature": signature,
        })
