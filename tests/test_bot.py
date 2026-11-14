import pytest
from solders.keypair import Keypair

from memebot.bot import Bot
from memebot.config import LAMPORTS_PER_SOL, SOL_MINT, Config
from memebot.executor import PaperExecutor
from memebot.jupiter import NoRouteError
from memebot.models import Position, SafetyReport, TokenCandidate
from memebot.safety import holder_concentration, safety_rejections
from memebot.state import BotState
from memebot.strategy import exit_reason, screen_rejections
from memebot.wallet import load_keypair

NOW = 1_800_000_000.0


def cfg(tmp_path=None, **kw) -> Config:
    base = dict(mode="paper", use_rugcheck=False)
    if tmp_path is not None:
        base.update(state_file=str(tmp_path / "state.json"), trades_csv=str(tmp_path / "trades.csv"))
    base.update(kw)
    return Config(**base)


def good_candidate(mint="MintGood111", **kw) -> TokenCandidate:
    d = dict(mint=mint, symbol="GOOD", name="Good", pair_address="Pair1", dex_id="raydium", price_usd=0.001,
             liquidity_usd=50_000, market_cap_usd=300_000, volume_m5_usd=20_000, volume_h1_usd=100_000,
             buys_m5=120, sells_m5=60, price_change_m5_pct=8, price_change_h1_pct=40,
             pair_created_at_ms=int((NOW - 60 * 60) * 1000))
    d.update(kw)
    return TokenCandidate(**d)


# ---------------------------------------------------------------- strategy
def test_good_candidate_passes():
    assert screen_rejections(good_candidate(), cfg(), NOW * 1000) == []


@pytest.mark.parametrize("field,value", [
    ("liquidity_usd", 1_000),
    ("market_cap_usd", 50_000_000),
    ("volume_m5_usd", 100),
    ("buys_m5", 20),            # buy/sell ratio too low
    ("price_change_m5_pct", -5),
    ("price_change_m5_pct", 90),  # chasing a vertical candle
    ("pair_created_at_ms", int((NOW - 60) * 1000)),  # 1 minute old
    ("pair_created_at_ms", None),
])
def test_bad_candidates_rejected(field, value):
    assert screen_rejections(good_candidate(**{field: value}), cfg(), NOW * 1000)


def test_candidate_from_dexscreener_pair():
    pair = {"chainId": "solana", "dexId": "raydium", "pairAddress": "P", "priceUsd": "0.0012",
            "baseToken": {"address": "M", "symbol": "DOG", "name": "Dog"},
            "txns": {"m5": {"buys": 10, "sells": 5}}, "volume": {"m5": 1234.5, "h1": 9000},
            "priceChange": {"m5": 4.2, "h1": 12}, "liquidity": {"usd": 42000}, "fdv": 500000,
            "pairCreatedAt": 1700000000000}
    c = TokenCandidate.from_pair(pair)
    assert (c.mint, c.symbol, c.liquidity_usd, c.market_cap_usd, c.buys_m5) == ("M", "DOG", 42000, 500000, 10)
    assert c.buy_sell_ratio_m5 == 2


def pos(entry=1_000_000, peak=None, opened=NOW):
    return Position(mint="M", symbol="X", entry_lamports=entry, token_amount=1000, opened_at=opened,
                    peak_value_lamports=peak or entry)


def test_exits():
    c = cfg(take_profit_pct=60, stop_loss_pct=20, trailing_activation_pct=25, trailing_stop_pct=15, max_hold_minutes=120,
            stale_after_minutes=0)
    assert exit_reason(pos(), 1_700_000, NOW, c) == "take_profit"
    assert exit_reason(pos(), 790_000, NOW, c) == "stop_loss"
    assert exit_reason(pos(), 1_100_000, NOW, c) is None
    # ran to +40%, now 16% off the peak -> trailing stop
    p = pos()
    assert exit_reason(p, 1_400_000, NOW, c) is None
    assert exit_reason(p, 1_170_000, NOW, c) == "trailing_stop"
    # peak only +10%: trailing not active yet
    p = pos()
    exit_reason(p, 1_100_000, NOW, c)
    assert exit_reason(p, 900_000, NOW, c) is None
    assert exit_reason(pos(opened=NOW - 121 * 60), 1_000_000, NOW, c) == "max_hold"


# ---------------------------------------------------------------- safety
def test_holder_concentration_ignores_pools():
    wallet_a, wallet_b = str(Keypair().pubkey()), str(Keypair().pubkey())
    # A program-derived address (off curve) standing in for a liquidity pool
    from solders.pubkey import Pubkey
    pool, _ = Pubkey.find_program_address([b"pool"], Pubkey.from_string("11111111111111111111111111111111"))
    largest = [{"address": "acct_pool", "amount": 600}, {"address": "acct_a1", "amount": 50},
               {"address": "acct_a2", "amount": 30}, {"address": "acct_b", "amount": 20}]
    owners = {"acct_pool": str(pool), "acct_a1": wallet_a, "acct_a2": wallet_a, "acct_b": wallet_b}
    top10, top1 = holder_concentration(largest, owners, supply=1000)
    assert top10 == pytest.approx(10.0)  # 80 + 20 out of 1000, pool excluded
    assert top1 == pytest.approx(8.0)  # wallet_a's two accounts merged


def test_safety_rejections():
    c = cfg(use_rugcheck=True, max_rugcheck_score=50)
    assert safety_rejections(SafetyReport(mint="M"), c) == []
    bad = SafetyReport(mint="M", mint_authority="X", freeze_authority="Y", top10_holders_pct=45,
                       top_holder_pct=20, rugcheck_score=80, rugcheck_dangers=["Mutable metadata"])
    assert len(safety_rejections(bad, c)) == 6


def test_load_keypair_formats(tmp_path):
    kp = Keypair()
    assert load_keypair(str(kp)).pubkey() == kp.pubkey()
    f = tmp_path / "id.json"
    f.write_text(str(list(bytes(kp))))
    assert load_keypair(str(f)).pubkey() == kp.pubkey()


# ---------------------------------------------------------------- bot loop with fakes
class FakeDex:
    def __init__(self, candidates):
        self._c = candidates

    async def latest_token_addresses(self):
        return [c.mint for c in self._c]

    async def candidates(self, mints):
        return [c for c in self._c if c.mint in mints]


class FakeSafety:
    def __init__(self, bad=()):
        self.bad = set(bad)

    async def inspect(self, mint):
        return SafetyReport(mint=mint, mint_authority="auth" if mint in self.bad else None)


class FakeJupiter:
    """1 lamport buys 1000 tokens; sell value per token is adjustable to move the price."""

    def __init__(self):
        self.sell_rate = {}  # mint -> lamports per 1000 tokens
        self.buy_price = {}  # mint -> price multiplier for buys (2.0 = tokens cost twice as much)
        self.no_route = set()

    async def quote(self, input_mint, output_mint, amount, slippage_bps):
        if SOL_MINT == input_mint:
            mult = self.buy_price.get(output_mint, 1.0)
            return {"outAmount": str(int(amount * 1000 / mult)), "priceImpactPct": "0.01"}
        if input_mint in self.no_route:
            raise NoRouteError("no route")
        rate = self.sell_rate.get(input_mint, 1.0)
        return {"outAmount": str(int(amount / 1000 * rate)), "priceImpactPct": "0.01"}


def make_bot(tmp_path, candidates, bad=(), **kw):
    c = cfg(tmp_path, **kw)
    jup = FakeJupiter()
    ex = PaperExecutor(c, jup, 2 * LAMPORTS_PER_SOL)
    return Bot(c, FakeDex(candidates), FakeSafety(bad), jup, ex, BotState(path=c.state_file)), jup


async def test_scan_buys_only_safe_passing_tokens(tmp_path):
    cands = [good_candidate("A"), good_candidate("B"), good_candidate("C", liquidity_usd=10)]
    bot, _ = make_bot(tmp_path, cands, bad={"B"})
    bought = await bot.scan_once(NOW)
    assert bought == ["A"]
    assert "B" in bot.state.blacklist
    assert set(bot.state.positions) == {"A"}
    # second scan does not rebuy A
    assert await bot.scan_once(NOW + 30) == []


async def test_max_open_positions(tmp_path):
    bot, _ = make_bot(tmp_path, [good_candidate(m) for m in "ABCDE"], max_open_positions=2)
    await bot.scan_once(NOW)
    assert len(bot.state.positions) == 2


async def test_take_profit_round_trip_and_state(tmp_path):
    bot, jup = make_bot(tmp_path, [good_candidate("A")], take_profit_pct=60)
    await bot.scan_once(NOW)
    jup.sell_rate["A"] = 2.0  # price doubles
    assert await bot.monitor_once(NOW + 60) == ["A"]
    assert bot.state.positions == {}
    assert bot.state.realized_today() > 0
    assert bot.ex.balance > 2 * LAMPORTS_PER_SOL
    reloaded = BotState.load(bot.cfg.state_file)
    assert reloaded.paper_balance == bot.ex.balance
    assert "take_profit" in (tmp_path / "trades.csv").read_text()


async def test_honeypot_rejected(tmp_path):
    bot, jup = make_bot(tmp_path, [good_candidate("A")])
    jup.sell_rate["A"] = 0.5  # selling back loses 50% -> tax/honeypot
    assert await bot.scan_once(NOW) == []
    assert "round-trip" in bot.state.blacklist["A"]


async def test_daily_loss_limit_halts_buying(tmp_path):
    bot, jup = make_bot(tmp_path, [good_candidate("A"), good_candidate("B")], daily_loss_limit_sol=0.01,
                        max_open_positions=1)
    assert await bot.scan_once(NOW) == ["A"]
    jup.sell_rate["A"] = 0.5
    assert await bot.monitor_once(NOW + 60) == ["A"]  # stop loss
    assert bot.state.realized_today() < 0
    assert await bot.scan_once(NOW + 120) == []  # B would pass, but the day's loss limit is hit


def test_sign_jupiter_style_transaction():
    import base64

    from solders.hash import Hash
    from solders.message import MessageV0
    from solders.system_program import TransferParams, transfer
    from solders.transaction import VersionedTransaction

    from memebot.wallet import sign_transaction

    kp = Keypair()
    ix = transfer(TransferParams(from_pubkey=kp.pubkey(), to_pubkey=Keypair().pubkey(), lamports=1))
    msg = MessageV0.try_compile(kp.pubkey(), [ix], [], Hash.default())
    unsigned = VersionedTransaction.populate(msg, [])  # Jupiter returns the tx with empty signatures
    signed = VersionedTransaction.from_bytes(base64.b64decode(sign_transaction(base64.b64encode(bytes(unsigned)).decode(), kp)))
    assert signed.verify_with_results() == [True]


def test_rugcheck_concentration_excludes_pool():
    from memebot.safety import rugcheck_concentration

    a, b = str(Keypair().pubkey()), str(Keypair().pubkey())
    pool = str(Keypair().pubkey())  # even an on-curve market key is excluded when listed as a market
    report = {
        "markets": [{"pubkey": pool, "liquidityA": "vaultA"}],
        "topHolders": [
            {"address": "vaultA", "owner": pool, "pct": 40.0},
            {"address": "x1", "owner": a, "pct": 6.0},
            {"address": "x2", "owner": a, "pct": 1.0},
            {"address": "x3", "owner": b, "pct": 3.0},
        ],
    }
    top10, top1 = rugcheck_concentration(report)
    assert top10 == pytest.approx(10.0) and top1 == pytest.approx(7.0)


def test_let_winners_run_with_trailing_stop_from_peak():
    c = cfg(take_profit_pct=0, stop_loss_pct=20, trailing_activation_pct=0, trailing_stop_pct=20, max_hold_minutes=0,
            stale_after_minutes=0)
    p = pos()
    assert exit_reason(p, 3_000_000, NOW, c) is None            # +200%: no take-profit, keep holding
    assert exit_reason(p, 2_500_000, NOW, c) is None            # 17% off the peak
    assert exit_reason(p, 2_390_000, NOW, c) == "trailing_stop"  # 20%+ off the peak -> sell at about +139%
    assert exit_reason(pos(), 810_000, NOW, c) is None
    assert exit_reason(pos(), 790_000, NOW, c) == "stop_loss"   # never went up: plain -20% stop
    assert exit_reason(pos(opened=NOW - 10_000 * 60), 1_000_000, NOW, c) is None  # no max hold


def test_stale_positions_sold_winners_kept():
    c = cfg(take_profit_pct=0, trailing_activation_pct=0, trailing_stop_pct=20, max_hold_minutes=0,
            stale_after_minutes=30, stale_min_profit_pct=5)
    assert exit_reason(pos(opened=NOW - 29 * 60), 1_000_000, NOW, c) is None       # flat, but only 29 min
    assert exit_reason(pos(opened=NOW - 31 * 60), 1_030_000, NOW, c) == "stale"    # +3% after 31 min
    assert exit_reason(pos(opened=NOW - 31 * 60), 1_100_000, NOW, c) is None       # +10%: keep running
    assert exit_reason(pos(opened=NOW - 600 * 60), 1_500_000, NOW, c) is None      # big winner, any age


async def test_spiked_token_bought_only_after_dip(tmp_path):
    bot, jup = make_bot(tmp_path, [good_candidate("A", price_change_m5_pct=30), good_candidate("B", price_change_m5_pct=30)],
                        dip_trigger_pct=15, dip_pct=5, max_dip_pct=20, dip_watch_minutes=15)
    assert await bot.scan_once(NOW) == []  # spiked: watched, not bought
    assert set(bot.watchlist) == {"A", "B"}
    jup.buy_price.update(A=1.2, B=1.2)  # both keep rising: new highs
    assert await bot.watch_once(NOW + 10) == []
    jup.buy_price.update(A=1.13, B=0.9)  # A dips ~6% from the high, B dumps 25%
    assert await bot.watch_once(NOW + 20) == ["A"]
    assert "A" in bot.state.positions and bot.watchlist == {}
    assert "B" not in bot.state.positions and "B" in bot.state.traded_mints  # dumped: cooled down


async def test_watch_expires_without_dip(tmp_path):
    bot, jup = make_bot(tmp_path, [good_candidate("A", price_change_m5_pct=30)], dip_watch_minutes=15)
    await bot.scan_once(NOW)
    assert await bot.watch_once(NOW + 16 * 60) == []
    assert bot.watchlist == {} and bot.state.positions == {}


async def test_small_moves_bought_immediately(tmp_path):
    bot, _ = make_bot(tmp_path, [good_candidate("A", price_change_m5_pct=8)], dip_trigger_pct=15)
    assert await bot.scan_once(NOW) == ["A"]


async def test_pyramiding_adds_to_winners_up_to_limit(tmp_path):
    bot, jup = make_bot(tmp_path, [good_candidate("A")], take_profit_pct=0, trailing_activation_pct=0,
                        trailing_stop_pct=20, stale_after_minutes=0, add_trigger_pct=30, add_amount_sol=0.025, max_adds=2)
    await bot.scan_once(NOW)
    pos = bot.state.positions["A"]
    first_cost, first_tokens = pos.entry_lamports, pos.token_amount

    jup.sell_rate["A"] = jup.buy_price["A"] = 1.35  # +35%: first add
    await bot.monitor_once(NOW + 60)
    assert pos.adds == 1 and pos.entry_lamports > first_cost and pos.token_amount > first_tokens

    jup.sell_rate["A"] = jup.buy_price["A"] = 1.35 * 1.2  # only +20% since the add: no new add
    await bot.monitor_once(NOW + 120)
    assert pos.adds == 1

    jup.sell_rate["A"] = jup.buy_price["A"] = 1.35 * 1.35  # +35% since the add: second add
    await bot.monitor_once(NOW + 180)
    assert pos.adds == 2

    jup.sell_rate["A"] = jup.buy_price["A"] = 1.35 * 1.35 * 1.5  # limit reached
    await bot.monitor_once(NOW + 240)
    assert pos.adds == 2 and "A" in bot.state.positions

    # a 25% drop from the peak sells the whole position, adds included
    jup.sell_rate["A"] *= 0.75
    assert await bot.monitor_once(NOW + 300) == ["A"]
    csv_text = (tmp_path / "trades.csv").read_text()
    assert csv_text.count(",add,") == 2 and "trailing_stop" in csv_text


def test_hype_score():
    from memebot.strategy import hype_score

    quiet = good_candidate(buys_m5=6, sells_m5=1, buys_h1=6, volume_m5_usd=500, volume_h1_usd=500)
    hyped = good_candidate(buys_m5=150, sells_m5=50, buys_h1=300, volume_m5_usd=40_000, volume_h1_usd=120_000,
                           has_twitter=True, has_telegram=True, has_website=True, boosts_active=100)
    fading = good_candidate(buys_m5=40, sells_m5=60, buys_h1=1200, volume_m5_usd=10_000, volume_h1_usd=300_000,
                            has_twitter=True)
    assert hype_score(hyped) > 85
    assert hype_score(quiet) < 15
    assert hype_score(fading) < 20
    c = cfg(min_hype_score=50)
    assert screen_rejections(hyped, c, NOW * 1000) == []
    assert any("hype" in r for r in screen_rejections(fading, c, NOW * 1000))
