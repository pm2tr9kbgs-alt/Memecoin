"""Configuration loaded from environment variables (and an optional .env file)."""
from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

LAMPORTS_PER_SOL = 1_000_000_000
SOL_MINT = "So11111111111111111111111111111111111111112"


def _env(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def _float(name: str, default: float) -> float:
    return float(_env(name, str(default)))


def _int(name: str, default: int) -> int:
    return int(float(_env(name, str(default))))


def _bool(name: str, default: bool) -> bool:
    return _env(name, "true" if default else "false").lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Config:
    # --- mode & connectivity ---
    mode: str = "live"  # "live" or "paper"
    rpc_url: str = "https://api.mainnet-beta.solana.com"
    private_key: str | None = None  # base58 secret key, or path to a JSON keypair file
    jupiter_base_url: str = "https://lite-api.jup.ag/swap/v1"
    jupiter_api_key: str | None = None
    paper_balance_sol: float = 5.0

    # --- sizing & risk ---
    buy_amount_sol: float = 0.05
    max_open_positions: int = 3
    min_sol_reserve: float = 0.05  # never spend below this (keeps SOL for fees / token accounts)
    daily_loss_limit_sol: float = 0.25  # stop opening positions after this realized loss per UTC day
    buy_slippage_bps: int = 1500
    sell_slippage_bps: int = 2500
    max_price_impact_pct: float = 5.0
    max_round_trip_loss_pct: float = 15.0  # buy-then-sell quote loss; catches sell taxes / honeypots
    priority_fee_max_lamports: int = 1_000_000  # 0.001 SOL cap

    # --- entry filters (DexScreener data) ---
    min_liquidity_usd: float = 15_000
    max_liquidity_usd: float = 2_000_000
    min_market_cap_usd: float = 30_000
    max_market_cap_usd: float = 5_000_000
    min_pair_age_minutes: float = 10
    max_pair_age_minutes: float = 24 * 60
    min_volume_m5_usd: float = 5_000
    min_txns_m5: int = 30
    min_buy_sell_ratio_m5: float = 1.3
    min_price_change_m5_pct: float = 3.0
    max_price_change_m5_pct: float = 40.0  # don't chase vertical candles
    max_price_change_h1_pct: float = 300.0

    # --- on-chain safety filters ---
    require_mint_authority_revoked: bool = True
    require_freeze_authority_revoked: bool = True
    max_top10_holders_pct: float = 30.0  # of supply, wallet holders only (pools excluded)
    max_single_holder_pct: float = 10.0
    use_rugcheck: bool = True
    max_rugcheck_score: float = 50.0  # normalised 0-100, lower is safer
    rugcheck_required: bool = False  # if True, a failed RugCheck lookup blocks the buy

    # --- exits ---
    take_profit_pct: float = 0.0  # 0 = no take-profit (exit only via stops / max hold)
    stop_loss_pct: float = 20.0
    trailing_activation_pct: float = 0.0  # 0 = trailing stop active from entry (stop follows the peak)
    trailing_stop_pct: float = 20.0  # sell when value falls this far below the peak
    max_hold_minutes: float = 0.0  # 0 = no time limit

    # --- loop & storage ---
    scan_interval_sec: float = 20.0
    monitor_interval_sec: float = 5.0
    state_file: str = "state.json"
    trades_csv: str = "trades.csv"
    log_file: str = "bot.log"

    @property
    def is_live(self) -> bool:
        return self.mode == "live"

    @property
    def buy_amount_lamports(self) -> int:
        return int(self.buy_amount_sol * LAMPORTS_PER_SOL)

    @classmethod
    def from_env(cls, env_file: str | None = ".env", **overrides) -> "Config":
        if env_file:
            load_dotenv(env_file, override=False)
        d = cls()  # defaults
        values = dict(
            mode=(_env("MODE", d.mode) or d.mode).lower(),
            rpc_url=_env("RPC_URL", d.rpc_url),
            private_key=_env("PRIVATE_KEY"),
            jupiter_base_url=_env("JUPITER_BASE_URL", d.jupiter_base_url).rstrip("/"),
            jupiter_api_key=_env("JUPITER_API_KEY"),
            paper_balance_sol=_float("PAPER_BALANCE_SOL", d.paper_balance_sol),
            buy_amount_sol=_float("BUY_AMOUNT_SOL", d.buy_amount_sol),
            max_open_positions=_int("MAX_OPEN_POSITIONS", d.max_open_positions),
            min_sol_reserve=_float("MIN_SOL_RESERVE", d.min_sol_reserve),
            daily_loss_limit_sol=_float("DAILY_LOSS_LIMIT_SOL", d.daily_loss_limit_sol),
            buy_slippage_bps=_int("BUY_SLIPPAGE_BPS", d.buy_slippage_bps),
            sell_slippage_bps=_int("SELL_SLIPPAGE_BPS", d.sell_slippage_bps),
            max_price_impact_pct=_float("MAX_PRICE_IMPACT_PCT", d.max_price_impact_pct),
            max_round_trip_loss_pct=_float("MAX_ROUND_TRIP_LOSS_PCT", d.max_round_trip_loss_pct),
            priority_fee_max_lamports=_int("PRIORITY_FEE_MAX_LAMPORTS", d.priority_fee_max_lamports),
            min_liquidity_usd=_float("MIN_LIQUIDITY_USD", d.min_liquidity_usd),
            max_liquidity_usd=_float("MAX_LIQUIDITY_USD", d.max_liquidity_usd),
            min_market_cap_usd=_float("MIN_MARKET_CAP_USD", d.min_market_cap_usd),
            max_market_cap_usd=_float("MAX_MARKET_CAP_USD", d.max_market_cap_usd),
            min_pair_age_minutes=_float("MIN_PAIR_AGE_MINUTES", d.min_pair_age_minutes),
            max_pair_age_minutes=_float("MAX_PAIR_AGE_MINUTES", d.max_pair_age_minutes),
            min_volume_m5_usd=_float("MIN_VOLUME_M5_USD", d.min_volume_m5_usd),
            min_txns_m5=_int("MIN_TXNS_M5", d.min_txns_m5),
            min_buy_sell_ratio_m5=_float("MIN_BUY_SELL_RATIO_M5", d.min_buy_sell_ratio_m5),
            min_price_change_m5_pct=_float("MIN_PRICE_CHANGE_M5_PCT", d.min_price_change_m5_pct),
            max_price_change_m5_pct=_float("MAX_PRICE_CHANGE_M5_PCT", d.max_price_change_m5_pct),
            max_price_change_h1_pct=_float("MAX_PRICE_CHANGE_H1_PCT", d.max_price_change_h1_pct),
            require_mint_authority_revoked=_bool("REQUIRE_MINT_AUTHORITY_REVOKED", d.require_mint_authority_revoked),
            require_freeze_authority_revoked=_bool("REQUIRE_FREEZE_AUTHORITY_REVOKED", d.require_freeze_authority_revoked),
            max_top10_holders_pct=_float("MAX_TOP10_HOLDERS_PCT", d.max_top10_holders_pct),
            max_single_holder_pct=_float("MAX_SINGLE_HOLDER_PCT", d.max_single_holder_pct),
            use_rugcheck=_bool("USE_RUGCHECK", d.use_rugcheck),
            max_rugcheck_score=_float("MAX_RUGCHECK_SCORE", d.max_rugcheck_score),
            rugcheck_required=_bool("RUGCHECK_REQUIRED", d.rugcheck_required),
            take_profit_pct=_float("TAKE_PROFIT_PCT", d.take_profit_pct),
            stop_loss_pct=_float("STOP_LOSS_PCT", d.stop_loss_pct),
            trailing_activation_pct=_float("TRAILING_ACTIVATION_PCT", d.trailing_activation_pct),
            trailing_stop_pct=_float("TRAILING_STOP_PCT", d.trailing_stop_pct),
            max_hold_minutes=_float("MAX_HOLD_MINUTES", d.max_hold_minutes),
            scan_interval_sec=_float("SCAN_INTERVAL_SEC", d.scan_interval_sec),
            monitor_interval_sec=_float("MONITOR_INTERVAL_SEC", d.monitor_interval_sec),
            state_file=_env("STATE_FILE", d.state_file),
            trades_csv=_env("TRADES_CSV", d.trades_csv),
            log_file=_env("LOG_FILE", d.log_file),
        )
        values.update(overrides)
        cfg = cls(**values)
        cfg.validate()
        return cfg

    def validate(self) -> None:
        if self.mode not in {"live", "paper"}:
            raise ValueError(f"MODE must be 'live' or 'paper', got {self.mode!r}")
        if self.is_live and not self.private_key:
            raise ValueError("MODE=live requires PRIVATE_KEY (base58 secret or path to keypair JSON)")
        if self.buy_amount_sol <= 0:
            raise ValueError("BUY_AMOUNT_SOL must be > 0")
        if not 0 < self.stop_loss_pct < 100:
            raise ValueError("STOP_LOSS_PCT must be between 0 and 100")
        if self.max_open_positions < 1:
            raise ValueError("MAX_OPEN_POSITIONS must be >= 1")
