"""Plain data types shared across the bot."""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field


def _num(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


@dataclass
class TokenCandidate:
    """A token's most liquid Solana pair, as reported by DexScreener."""

    mint: str
    symbol: str
    name: str
    pair_address: str
    dex_id: str
    price_usd: float
    liquidity_usd: float
    market_cap_usd: float
    volume_m5_usd: float
    volume_h1_usd: float
    buys_m5: int
    sells_m5: int
    price_change_m5_pct: float
    price_change_h1_pct: float
    pair_created_at_ms: int | None
    buys_h1: int = 0
    sells_h1: int = 0
    has_twitter: bool = False
    has_telegram: bool = False
    has_website: bool = False
    boosts_active: int = 0

    @classmethod
    def from_pair(cls, pair: dict) -> "TokenCandidate":
        base = pair.get("baseToken") or {}
        txns_m5 = (pair.get("txns") or {}).get("m5") or {}
        volume = pair.get("volume") or {}
        change = pair.get("priceChange") or {}
        created = pair.get("pairCreatedAt")
        txns_h1 = (pair.get("txns") or {}).get("h1") or {}
        info = pair.get("info") or {}
        social_types = {str(x.get("type", "")).lower() for x in info.get("socials") or []}
        return cls(
            mint=base.get("address", ""),
            symbol=base.get("symbol", "?"),
            name=base.get("name", ""),
            pair_address=pair.get("pairAddress", ""),
            dex_id=pair.get("dexId", ""),
            price_usd=_num(pair.get("priceUsd")),
            liquidity_usd=_num((pair.get("liquidity") or {}).get("usd")),
            market_cap_usd=_num(pair.get("marketCap") or pair.get("fdv")),
            volume_m5_usd=_num(volume.get("m5")),
            volume_h1_usd=_num(volume.get("h1")),
            buys_m5=int(_num(txns_m5.get("buys"))),
            sells_m5=int(_num(txns_m5.get("sells"))),
            price_change_m5_pct=_num(change.get("m5")),
            price_change_h1_pct=_num(change.get("h1")),
            pair_created_at_ms=int(created) if created else None,
            buys_h1=int(_num(txns_h1.get("buys"))),
            sells_h1=int(_num(txns_h1.get("sells"))),
            has_twitter=bool(social_types & {"twitter", "x"}),
            has_telegram="telegram" in social_types,
            has_website=bool(info.get("websites")),
            boosts_active=int(_num((pair.get("boosts") or {}).get("active"))),
        )

    def age_minutes(self, now_ms: float | None = None) -> float | None:
        if not self.pair_created_at_ms:
            return None
        now_ms = now_ms if now_ms is not None else time.time() * 1000
        return (now_ms - self.pair_created_at_ms) / 60_000

    @property
    def buy_sell_ratio_m5(self) -> float:
        return self.buys_m5 / max(self.sells_m5, 1)


@dataclass
class SafetyReport:
    mint: str
    mint_authority: str | None = None
    freeze_authority: str | None = None
    decimals: int = 0
    supply: int = 0
    top10_holders_pct: float = 0.0
    top_holder_pct: float = 0.0
    rugcheck_score: float | None = None
    rugcheck_dangers: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass
class Position:
    mint: str
    symbol: str
    entry_lamports: int  # SOL actually spent, incl. fees
    token_amount: int  # raw token units held
    opened_at: float  # unix seconds
    entry_signature: str = ""
    pair_address: str = ""
    peak_value_lamports: int = 0
    last_value_lamports: int = 0
    no_route_count: int = 0
    sell_failures: int = 0
    adds: int = 0  # times we added to this position (pyramiding)
    last_buy_price: float = 0.0  # lamports per raw token at the most recent buy/add

    def pnl_pct(self, value_lamports: int) -> float:
        if self.entry_lamports <= 0:
            return 0.0
        return (value_lamports / self.entry_lamports - 1) * 100

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Position":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class TradeResult:
    signature: str
    lamports: int  # SOL spent (buy) or received (sell), net of fees
    token_amount: int  # tokens received (buy) or sold (sell)
