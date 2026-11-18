"""Command line entry point: python -m memebot {run,copy,status,sell-all,wallet}."""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys

import httpx

from .bot import Bot, sol
from .config import LAMPORTS_PER_SOL, SOL_MINT, Config
from .copytrade import CopyTrader
from .dexscreener import DexScreener
from .executor import LiveExecutor, PaperExecutor
from .jupiter import Jupiter, NoRouteError, out_amount
from .rpc import SolanaRPC
from .safety import SafetyChecker
from .state import BotState
from .wallet import load_keypair


def setup_logging(cfg: Config, verbose: bool) -> None:
    name = os.getenv("BOT_NAME", "").strip()
    prefix = f"[{name}] " if name else ""
    fmt = logging.Formatter(f"%(asctime)s {prefix}%(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    for stream in (sys.stdout, sys.stderr):  # Windows consoles default to cp1252; token names can be any unicode
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)
    if cfg.log_file:
        fh = logging.FileHandler(cfg.log_file, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def build(cfg: Config, client: httpx.AsyncClient) -> Bot:
    rpc = SolanaRPC(client, cfg.rpc_url)
    jup = Jupiter(client, cfg.jupiter_base_url, cfg.jupiter_api_key)
    state = BotState.load(cfg.state_file)
    if cfg.is_live:
        executor = LiveExecutor(cfg, rpc, jup, load_keypair(cfg.private_key))
    else:
        balance = state.paper_balance if state.paper_balance is not None else int(cfg.paper_balance_sol * LAMPORTS_PER_SOL)
        holdings = {m: p.token_amount for m, p in state.positions.items()}
        executor = PaperExecutor(cfg, jup, balance, holdings)
    return Bot(cfg, DexScreener(client), SafetyChecker(rpc, client, cfg), jup, executor, state)


async def cmd_run(cfg: Config, assume_yes: bool) -> None:
    async with httpx.AsyncClient(headers={"User-Agent": "memebot/1.0"}) as client:
        bot = build(cfg, client)
        log = logging.getLogger("memebot")
        balance = await bot.ex.sol_balance()
        if cfg.is_live:
            log.warning("LIVE TRADING with wallet %s, balance %s", bot.ex.pubkey, sol(balance))
            log.warning("buy size %s, max %d positions, daily loss limit %.3f SOL",
                        sol(cfg.buy_amount_lamports), cfg.max_open_positions, cfg.daily_loss_limit_sol)
            if not assume_yes:
                if not sys.stdin.isatty() or input("Type LIVE to start trading real funds: ").strip() != "LIVE":
                    log.error("Not confirmed; exiting. Use --yes to skip this prompt.")
                    return
            await bot.reconcile()
        else:
            log.info("PAPER TRADING, simulated balance %s", sol(balance))

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, bot.stop)
            except NotImplementedError:  # Windows
                pass
        log.info("bot started; Ctrl+C to stop (open positions are kept and resumed next run)")
        await bot.run()
        log.info("stopped; %d open position(s) saved to %s", len(bot.state.positions), cfg.state_file)


async def cmd_copy(cfg: Config, assume_yes: bool) -> None:
    async with httpx.AsyncClient(headers={"User-Agent": "memebot/1.0"}) as client:
        bot = build(cfg, client)  # same executor, state, safety checks and Jupiter client as the momentum bot
        copier = CopyTrader(cfg, SolanaRPC(client, cfg.rpc_url), bot.safety, bot.jup, bot.ex, bot.state, bot.dex)
        log = logging.getLogger("memebot")
        balance = await bot.ex.sol_balance()
        if cfg.is_live:
            log.warning("LIVE COPY TRADING with wallet %s, balance %s", bot.ex.pubkey, sol(balance))
            if not assume_yes:
                if not sys.stdin.isatty() or input("Type LIVE to start copying with real funds: ").strip() != "LIVE":
                    log.error("Not confirmed; exiting. Use --yes to skip this prompt.")
                    return
            await bot.reconcile()
        else:
            log.info("PAPER COPY TRADING, simulated balance %s", sol(balance))
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, copier.stop)
            except NotImplementedError:  # Windows
                pass
        await copier.run()
        log.info("stopped; %d open position(s) saved to %s", len(bot.state.positions), cfg.state_file)


async def cmd_status(cfg: Config) -> None:
    async with httpx.AsyncClient() as client:
        bot = build(cfg, client)
        name = os.getenv("BOT_NAME")
        print(f"bot: {name}" if name else "bot: default")
        print(f"mode: {cfg.mode}")
        if cfg.is_live:
            print(f"wallet: {bot.ex.pubkey}")
        print(f"SOL balance: {sol(await bot.ex.sol_balance())}")
        print(f"realized P&L today: {sol(bot.state.realized_today())}")
        if not bot.state.positions:
            print("no open positions")
        for pos in bot.state.positions.values():
            try:
                q = await bot.jup.quote(pos.mint, SOL_MINT, pos.token_amount, cfg.sell_slippage_bps)
                value = out_amount(q)
                extra = f"value {sol(value)} ({pos.pnl_pct(value):+.1f}%)"
            except (NoRouteError, httpx.HTTPError) as exc:
                extra = f"value unknown ({exc})"
            print(f"  {pos.symbol:<10} {pos.mint}  cost {sol(pos.entry_lamports)}  {extra}")


async def cmd_sell_all(cfg: Config) -> None:
    async with httpx.AsyncClient() as client:
        bot = build(cfg, client)
        await bot.sell_all("manual")
        print(f"{len(bot.state.positions)} position(s) still open")


def cmd_wallet(cfg: Config) -> None:
    if not cfg.private_key:
        print("PRIVATE_KEY is not set")
        return
    print(load_keypair(cfg.private_key).pubkey())


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="memebot", description="Solana meme coin momentum trading bot")
    ap.add_argument("--env", default=".env", help="env file to load (default .env)")
    ap.add_argument("--paper", action="store_true", help="force paper trading regardless of MODE")
    ap.add_argument("-v", "--verbose", action="store_true", help="debug logging (shows every rejected token)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    run_p = sub.add_parser("run", help="start trading")
    run_p.add_argument("--yes", action="store_true", help="skip the live-trading confirmation prompt")
    copy_p = sub.add_parser("copy", help="copy the trades of the wallets in COPY_WALLETS")
    copy_p.add_argument("--yes", action="store_true", help="skip the live-trading confirmation prompt")
    sub.add_parser("status", help="show balance and open positions")
    sub.add_parser("sell-all", help="sell every open position now")
    sub.add_parser("wallet", help="print the wallet public key")
    args = ap.parse_args(argv)

    overrides = {"mode": "paper"} if args.paper else {}
    try:
        cfg = Config.from_env(args.env, **overrides)
    except ValueError as exc:
        sys.exit(f"config error: {exc}")
    setup_logging(cfg, args.verbose)

    try:
        _dispatch(args, cfg)
    except KeyboardInterrupt:  # Ctrl+C on Windows (no asyncio signal handlers there); state is saved after every trade
        logging.getLogger("memebot").info("stopped by Ctrl+C")


def _dispatch(args, cfg: Config) -> None:
    if args.cmd == "run":
        asyncio.run(cmd_run(cfg, args.yes))
    elif args.cmd == "copy":
        asyncio.run(cmd_copy(cfg, args.yes))
    elif args.cmd == "status":
        asyncio.run(cmd_status(cfg))
    elif args.cmd == "sell-all":
        asyncio.run(cmd_sell_all(cfg))
    elif args.cmd == "wallet":
        cmd_wallet(cfg)


if __name__ == "__main__":
    main()
