"""Entry screening and exit rules for the new-token momentum strategy. Pure functions, easy to test."""
from __future__ import annotations

from .config import Config
from .models import Position, TokenCandidate


def screen_rejections(c: TokenCandidate, cfg: Config, now_ms: float | None = None) -> list[str]:
    """Reasons the candidate fails the market-data filters. Empty list == passes."""
    r: list[str] = []
    if not c.mint:
        return ["missing mint"]
    if not cfg.min_liquidity_usd <= c.liquidity_usd <= cfg.max_liquidity_usd:
        r.append(f"liquidity ${c.liquidity_usd:,.0f}")
    if not cfg.min_market_cap_usd <= c.market_cap_usd <= cfg.max_market_cap_usd:
        r.append(f"mcap ${c.market_cap_usd:,.0f}")
    age = c.age_minutes(now_ms)
    if age is None or not cfg.min_pair_age_minutes <= age <= cfg.max_pair_age_minutes:
        r.append(f"age {age if age is None else round(age)}m")
    if c.volume_m5_usd < cfg.min_volume_m5_usd:
        r.append(f"5m volume ${c.volume_m5_usd:,.0f}")
    if c.buys_m5 + c.sells_m5 < cfg.min_txns_m5:
        r.append(f"5m txns {c.buys_m5 + c.sells_m5}")
    if c.buy_sell_ratio_m5 < cfg.min_buy_sell_ratio_m5:
        r.append(f"5m buy/sell {c.buy_sell_ratio_m5:.2f}")
    if not cfg.min_price_change_m5_pct <= c.price_change_m5_pct <= cfg.max_price_change_m5_pct:
        r.append(f"5m change {c.price_change_m5_pct:+.1f}%")
    if c.price_change_h1_pct > cfg.max_price_change_h1_pct:
        r.append(f"1h change {c.price_change_h1_pct:+.0f}%")
    if cfg.min_hype_score > 0 and (h := hype_score(c)) < cfg.min_hype_score:
        r.append(f"hype {h:.0f}")
    return r


def _ramp(x: float, lo: float, hi: float) -> float:
    return max(0.0, min(1.0, (x - lo) / (hi - lo)))


def hype_score(c: TokenCandidate) -> float:
    """0-100 estimate of how much attention a token is getting *right now*, from free DexScreener data.

    - buy acceleration (35): buys in the last 5 min vs the average 5 min of the last hour
    - volume acceleration (20): same for volume
    - buyer dominance (15): 5-min buy/sell ratio
    - socials (15): X account, Telegram, website
    - paid boosts (15): projects pay DexScreener to promote tokens they're pushing
    """
    import math

    avg_buys_5m = c.buys_h1 / 12 if c.buys_h1 else 0
    buy_accel = c.buys_m5 / avg_buys_5m if avg_buys_5m else (3.0 if c.buys_m5 >= 20 else 1.0)
    avg_vol_5m = c.volume_h1_usd / 12 if c.volume_h1_usd else 0
    vol_accel = c.volume_m5_usd / avg_vol_5m if avg_vol_5m else (3.0 if c.volume_m5_usd >= 5_000 else 1.0)
    # acceleration only counts once there's real activity (6 buys "accelerating" from 6/hour isn't hype)
    size_buys = min(c.buys_m5 / 30, 1.0)
    size_vol = min(c.volume_m5_usd / 10_000, 1.0)
    score = 35 * _ramp(buy_accel, 1.0, 3.0) * size_buys
    score += 20 * _ramp(vol_accel, 1.0, 3.0) * size_vol
    score += 15 * _ramp(c.buy_sell_ratio_m5, 1.0, 2.5) * size_buys
    score += 8 * c.has_twitter + 4 * c.has_telegram + 3 * c.has_website
    score += 15 * min(math.log10(1 + c.boosts_active) / 2, 1.0)
    return round(score, 1)


def momentum_score(c: TokenCandidate) -> float:
    """Rank passing candidates: favour buy pressure and volume relative to liquidity."""
    vol_to_liq = c.volume_m5_usd / max(c.liquidity_usd, 1)
    return c.buy_sell_ratio_m5 * 2 + vol_to_liq * 10 + c.price_change_m5_pct / 10


def exit_reason(pos: Position, value_lamports: int, now: float, cfg: Config) -> str | None:
    """Update the position's peak and return why it should be sold now, or None to keep holding."""
    pos.last_value_lamports = value_lamports
    if value_lamports > pos.peak_value_lamports:
        pos.peak_value_lamports = value_lamports

    pnl = pos.pnl_pct(value_lamports)
    if cfg.take_profit_pct > 0 and pnl >= cfg.take_profit_pct:  # 0 disables take-profit: let winners run
        return "take_profit"
    if pnl <= -cfg.stop_loss_pct:
        return "stop_loss"
    peak_pnl = pos.pnl_pct(pos.peak_value_lamports)
    if peak_pnl >= cfg.trailing_activation_pct:
        drawdown = (1 - value_lamports / pos.peak_value_lamports) * 100
        if drawdown >= cfg.trailing_stop_pct:
            return "trailing_stop"
    held_min = (now - pos.opened_at) / 60
    if cfg.stale_after_minutes > 0 and held_min >= cfg.stale_after_minutes and pnl < cfg.stale_min_profit_pct:
        return "stale"  # not making money: free the slot for a better token
    if cfg.max_hold_minutes > 0 and held_min >= cfg.max_hold_minutes:
        return "max_hold"
    return None
