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


def _levels(name: str, default: tuple[float, ...]) -> tuple[float, ...]:
    """Comma-separated gain levels, e.g. "25,50,100" (empty or 0 = off)."""
    raw = _env(name, ",".join(f"{x:g}" for x in default))
    return tuple(sorted(float(x) for x in raw.replace(";", ",").split(",") if x.strip() and float(x) > 0))


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
    sell_priority_fee_multiplier: float = 1.05  # sells may pay slightly more to confirm faster (1 = same as buys)

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
    # skip tokens whose 1h change was above this at any point in the last pumped_memory_minutes (0 = off);
    # in testing, buying after a big pump lost far more than it made (incl. most rugs)
    pumped_h1_pct: float = 150.0
    pumped_memory_minutes: float = 120.0
    graduation_wait_minutes: float = 10.0  # skip launchpad tokens that moved to a DEX pool less than this ago
    max_buy_sell_ratio_m5: float = 3.5  # very lopsided buying is usually bots pumping before a dump (0 = off)
    # buy-the-dip: tokens up >= dip_trigger_pct in 5m go on a watchlist and are bought only after
    # pulling back dip_pct from their high; a drop beyond max_dip_pct means dumping -> skip. 0 = off
    dip_trigger_pct: float = 15.0
    dip_pct: float = 5.0
    max_dip_pct: float = 20.0
    dip_watch_minutes: float = 15.0

    # --- on-chain safety filters ---
    require_mint_authority_revoked: bool = True
    require_freeze_authority_revoked: bool = True
    max_top10_holders_pct: float = 30.0  # of supply, wallet holders only (pools excluded)
    max_single_holder_pct: float = 10.0
    min_lp_locked_pct: float = 90.0  # share of pool liquidity that must be locked/burned, so the dev can't pull it (0 = off)
    max_creator_pct: float = 5.0  # skip if the creator's wallet still holds more than this share of supply (0 = off)
    use_rugcheck: bool = True
    max_rugcheck_score: float = 50.0  # normalised 0-100, lower is safer
    rugcheck_required: bool = False  # if True, a failed RugCheck lookup blocks the buy

    # --- exits ---
    take_profit_pct: float = 0.0  # 0 = no take-profit (exit only via stops / max hold)
    stop_loss_pct: float = 15.0
    trailing_activation_pct: float = 0.0  # 0 = trailing stop active from entry (stop follows the peak)
    trailing_stop_pct: float = 15.0  # sell when value falls this far below the peak (before any gain: a -15% stop)
    tight_trail_after_pct: float = 25.0  # once the peak gain reaches this... (0 = off)
    tight_trailing_stop_pct: float = 10.0  # ...trail this close instead, to keep more of the gain
    runner_trail_after_pct: float = 100.0  # once the peak gain reaches this... (0 = off)
    runner_trailing_stop_pct: float = 20.0  # ...give big runners more room again
    max_hold_minutes: float = 0.0  # 0 = no time limit
    rebuy_cooldown_minutes: float = 180.0  # don't buy a token again within this long after trading it
    stale_after_minutes: float = 30.0  # sell positions that haven't made STALE_MIN_PROFIT_PCT by then (0 = off)
    stale_min_profit_pct: float = 5.0
    # take-profit ladder: at each gain level, sell partial_take_profit_fraction of the ORIGINAL position;
    # whatever is left rides under the trailing stop. Default: 25% at +25%, +50% and +100%, keep the last 25%.
    partial_take_profit_levels: tuple[float, ...] = (25.0, 50.0, 100.0)
    partial_take_profit_fraction: float = 0.25
    # losing-streak pause: after this many losing trades in a row, stop buying for loss_pause_minutes (0 = off)
    loss_streak_limit: int = 3
    loss_pause_minutes: float = 45.0
    # market activity meter: on each scan, count tokens with real trading (5m volume and txns above these),
    # averaged over activity_window_minutes. Below activity_hot_min the market is quiet and buys are
    # scaled by quiet_size_fraction (1 = always full size). In testing: busy US evening ~8, quiet mornings ~3.
    # copy trading (python -m memebot copy): mirror the buys and sells of these wallets
    copy_wallets: str = ""  # comma-separated wallet addresses
    copy_buy_amount_sol: float = 0.0  # SOL per copied buy (0 = BUY_AMOUNT_SOL)
    copy_poll_sec: float = 2.0  # how often each wallet is checked for new transactions
    copy_min_leader_sol: float = 0.1  # ignore the wallet's buys smaller than this (dust, tests)
    copy_max_delay_sec: float = 30.0  # don't copy a buy we noticed later than this (price has moved)
    copy_stop_loss_pct: float = 0.0  # optional own stop-loss on copied positions (0 = only sell when they sell)
    activity_window_minutes: float = 30.0
    activity_min_volume_m5_usd: float = 5_000
    activity_min_txns_m5: int = 30
    activity_hot_min: float = 6.0
    quiet_size_fraction: float = 0.5
    # pyramiding: add add_amount_sol each time the price rises add_trigger_pct above the last buy (0 = off)
    add_trigger_pct: float = 50.0
    add_amount_sol: float = 0.025
    max_adds: int = 0  # off: in testing, adds landed at the top and cut profits
    winner_alert_pct: float = 25.0  # announce a position each time it gains another this-many % (0 = off)
    winners_file: str = "winners.txt"

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

    @property
    def copy_wallet_list(self) -> list[str]:
        return [w.strip() for w in self.copy_wallets.replace(";", ",").split(",") if w.strip()]

    @property
    def copy_buy_lamports(self) -> int:
        return int((self.copy_buy_amount_sol or self.buy_amount_sol) * LAMPORTS_PER_SOL)

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
            sell_priority_fee_multiplier=_float("SELL_PRIORITY_FEE_MULTIPLIER", d.sell_priority_fee_multiplier),
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
            pumped_h1_pct=_float("PUMPED_H1_PCT", d.pumped_h1_pct),
            pumped_memory_minutes=_float("PUMPED_MEMORY_MINUTES", d.pumped_memory_minutes),
            graduation_wait_minutes=_float("GRADUATION_WAIT_MINUTES", d.graduation_wait_minutes),
            max_buy_sell_ratio_m5=_float("MAX_BUY_SELL_RATIO_M5", d.max_buy_sell_ratio_m5),
            dip_trigger_pct=_float("DIP_TRIGGER_PCT", d.dip_trigger_pct),
            dip_pct=_float("DIP_PCT", d.dip_pct),
            max_dip_pct=_float("MAX_DIP_PCT", d.max_dip_pct),
            dip_watch_minutes=_float("DIP_WATCH_MINUTES", d.dip_watch_minutes),
            require_mint_authority_revoked=_bool("REQUIRE_MINT_AUTHORITY_REVOKED", d.require_mint_authority_revoked),
            require_freeze_authority_revoked=_bool("REQUIRE_FREEZE_AUTHORITY_REVOKED", d.require_freeze_authority_revoked),
            max_top10_holders_pct=_float("MAX_TOP10_HOLDERS_PCT", d.max_top10_holders_pct),
            max_single_holder_pct=_float("MAX_SINGLE_HOLDER_PCT", d.max_single_holder_pct),
            min_lp_locked_pct=_float("MIN_LP_LOCKED_PCT", d.min_lp_locked_pct),
            max_creator_pct=_float("MAX_CREATOR_PCT", d.max_creator_pct),
            use_rugcheck=_bool("USE_RUGCHECK", d.use_rugcheck),
            max_rugcheck_score=_float("MAX_RUGCHECK_SCORE", d.max_rugcheck_score),
            rugcheck_required=_bool("RUGCHECK_REQUIRED", d.rugcheck_required),
            take_profit_pct=_float("TAKE_PROFIT_PCT", d.take_profit_pct),
            stop_loss_pct=_float("STOP_LOSS_PCT", d.stop_loss_pct),
            trailing_activation_pct=_float("TRAILING_ACTIVATION_PCT", d.trailing_activation_pct),
            trailing_stop_pct=_float("TRAILING_STOP_PCT", d.trailing_stop_pct),
            tight_trail_after_pct=_float("TIGHT_TRAIL_AFTER_PCT", d.tight_trail_after_pct),
            tight_trailing_stop_pct=_float("TIGHT_TRAILING_STOP_PCT", d.tight_trailing_stop_pct),
            runner_trail_after_pct=_float("RUNNER_TRAIL_AFTER_PCT", d.runner_trail_after_pct),
            runner_trailing_stop_pct=_float("RUNNER_TRAILING_STOP_PCT", d.runner_trailing_stop_pct),
            max_hold_minutes=_float("MAX_HOLD_MINUTES", d.max_hold_minutes),
            rebuy_cooldown_minutes=_float("REBUY_COOLDOWN_MINUTES", d.rebuy_cooldown_minutes),
            stale_after_minutes=_float("STALE_AFTER_MINUTES", d.stale_after_minutes),
            stale_min_profit_pct=_float("STALE_MIN_PROFIT_PCT", d.stale_min_profit_pct),
            partial_take_profit_levels=_levels("PARTIAL_TAKE_PROFIT_LEVELS", d.partial_take_profit_levels),
            partial_take_profit_fraction=_float("PARTIAL_TAKE_PROFIT_FRACTION", d.partial_take_profit_fraction),
            loss_streak_limit=_int("LOSS_STREAK_LIMIT", d.loss_streak_limit),
            loss_pause_minutes=_float("LOSS_PAUSE_MINUTES", d.loss_pause_minutes),
            copy_wallets=_env("COPY_WALLETS", d.copy_wallets),
            copy_buy_amount_sol=_float("COPY_BUY_AMOUNT_SOL", d.copy_buy_amount_sol),
            copy_poll_sec=_float("COPY_POLL_SEC", d.copy_poll_sec),
            copy_min_leader_sol=_float("COPY_MIN_LEADER_SOL", d.copy_min_leader_sol),
            copy_max_delay_sec=_float("COPY_MAX_DELAY_SEC", d.copy_max_delay_sec),
            copy_stop_loss_pct=_float("COPY_STOP_LOSS_PCT", d.copy_stop_loss_pct),
            activity_window_minutes=_float("ACTIVITY_WINDOW_MINUTES", d.activity_window_minutes),
            activity_min_volume_m5_usd=_float("ACTIVITY_MIN_VOLUME_M5_USD", d.activity_min_volume_m5_usd),
            activity_min_txns_m5=_int("ACTIVITY_MIN_TXNS_M5", d.activity_min_txns_m5),
            activity_hot_min=_float("ACTIVITY_HOT_MIN", d.activity_hot_min),
            quiet_size_fraction=_float("QUIET_SIZE_FRACTION", d.quiet_size_fraction),
            add_trigger_pct=_float("ADD_TRIGGER_PCT", d.add_trigger_pct),
            add_amount_sol=_float("ADD_AMOUNT_SOL", d.add_amount_sol),
            max_adds=_int("MAX_ADDS", d.max_adds),
            winner_alert_pct=_float("WINNER_ALERT_PCT", d.winner_alert_pct),
            winners_file=_env("WINNERS_FILE", d.winners_file),
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
