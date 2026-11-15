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
            stale_after_minutes=0, tight_trail_after_pct=0)  # base 20% trail only
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
                        trailing_stop_pct=20, stale_after_minutes=0, add_trigger_pct=30, add_amount_sol=0.025, max_adds=2,
                        partial_take_profit_levels=())
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


def test_wait_out_post_graduation_selling():
    c = cfg(graduation_wait_minutes=20, min_pair_age_minutes=5)
    just_graduated = good_candidate(mint="Abcpump", dex_id="pumpswap", pair_created_at_ms=int((NOW - 8 * 60) * 1000))
    settled = good_candidate(mint="Abcpump", dex_id="pumpswap", pair_created_at_ms=int((NOW - 25 * 60) * 1000))
    not_launchpad = good_candidate(mint="Xyz111", dex_id="raydium", pair_created_at_ms=int((NOW - 8 * 60) * 1000))
    assert any("graduated" in r for r in screen_rejections(just_graduated, c, NOW * 1000))
    assert screen_rejections(settled, c, NOW * 1000) == []
    assert not any("graduated" in r for r in screen_rejections(not_launchpad, c, NOW * 1000))


def test_lopsided_buying_rejected():
    c = cfg(max_buy_sell_ratio_m5=3.5)
    pumped = good_candidate(buys_m5=150, sells_m5=30)
    assert any("coordinated pump" in r for r in screen_rejections(pumped, c, NOW * 1000))
    assert screen_rejections(good_candidate(), c, NOW * 1000) == []


async def test_winner_alerts_each_step_once(tmp_path):
    winners = tmp_path / "winners.txt"
    bot, jup = make_bot(tmp_path, [good_candidate("A")], take_profit_pct=0, add_trigger_pct=0, stale_after_minutes=0,
                        winner_alert_pct=25, winners_file=str(winners))
    await bot.scan_once(NOW)
    for rate in (1.1, 1.3, 1.35, 1.6):  # +10%: none; +30%: alert 25; +35%: same level; +60%: alert 50
        jup.sell_rate["A"] = rate
        await bot.monitor_once(NOW + 60)
    lines = winners.read_text().splitlines()
    assert len(lines) == 2  # one alert per +25% step, not one per tick
    assert "+29." in lines[0] and "+59." in lines[1]  # after fees
    assert all("  A  https://dexscreener.com/solana/A" in line for line in lines)


def test_trailing_stop_tightens_after_big_gain():
    c = cfg(take_profit_pct=0, trailing_activation_pct=0, trailing_stop_pct=20, tight_trail_after_pct=25,
            tight_trailing_stop_pct=10, stale_after_minutes=0, max_hold_minutes=0)
    small = pos()
    exit_reason(small, 1_200_000, NOW, c)                          # peak +20%: still the wide 20% trail
    assert exit_reason(small, 1_060_000, NOW, c) is None          # 11.7% off the peak
    big = pos()
    exit_reason(big, 1_300_000, NOW, c)                            # peak +30%: tight 10% trail
    assert exit_reason(big, 1_200_000, NOW, c) is None             # 7.7% off the peak
    assert exit_reason(big, 1_160_000, NOW, c) == "trailing_stop"  # 10.8% off -> sell at about +16%


async def test_already_pumped_tokens_skipped_after_h1_cools(tmp_path):
    c = good_candidate("A", price_change_h1_pct=200)  # pumping hard: remembered and skipped
    bot, _ = make_bot(tmp_path, [c])
    assert await bot.scan_once(NOW) == []
    assert "A" in bot.state.pumped
    c.price_change_h1_pct = 40  # an hour later the 1h change looks calm, but it already pumped
    assert await bot.scan_once(NOW + 60 * 60) == []
    assert await bot.scan_once(NOW + 121 * 60) == ["A"]  # memory expired
    bot.state.save()
    assert BotState.load(bot.state.path).pumped["A"] == NOW


def test_adds_off_by_default():
    assert Config().max_adds == 0


async def test_take_profit_ladder_sells_quarters_and_keeps_last_quarter(tmp_path):
    bot, jup = make_bot(tmp_path, [good_candidate("A")], partial_take_profit_levels=(25, 50, 100),
                        partial_take_profit_fraction=0.25, trailing_stop_pct=20, tight_trail_after_pct=0,
                        stale_after_minutes=0, winner_alert_pct=0)
    await bot.scan_once(NOW)
    pos = bot.state.positions["A"]
    tokens, cost = pos.token_amount, pos.entry_lamports
    left = lambda: pos.token_amount / tokens  # noqa: E731

    jup.sell_rate["A"] = 1.3  # +30%: first quarter
    await bot.monitor_once(NOW + 60)
    assert pos.partial_steps == 1 and abs(left() - 0.75) < 0.001
    assert abs(pos.pnl_pct(pos.last_value_lamports) - 30) < 1  # remaining tokens still show the same gain
    jup.sell_rate["A"] = 1.35  # no new level
    await bot.monitor_once(NOW + 65)
    assert pos.partial_steps == 1
    jup.sell_rate["A"] = 2.2  # +120%: crosses +50% and +100% at once, sold together
    await bot.monitor_once(NOW + 70)
    assert pos.partial_steps == 3 and abs(left() - 0.25) < 0.001
    assert abs(pos.entry_lamports - cost * 0.25) <= 2
    jup.sell_rate["A"] = 3.0  # the last quarter keeps riding
    assert await bot.monitor_once(NOW + 75) == [] and abs(left() - 0.25) < 0.001
    jup.sell_rate["A"] = 2.35  # -21.7% from the high: trailing stop sells the rest
    assert await bot.monitor_once(NOW + 80) == ["A"]
    rows = (tmp_path / "trades.csv").read_text().splitlines()
    assert [r.split(",")[2] for r in rows[1:]] == ["buy", "partial", "partial", "sell"]
    assert bot.state.realized_today() > 0 and bot.state.loss_streak == 0


def test_ladder_levels_from_env(monkeypatch):
    monkeypatch.setenv("PARTIAL_TAKE_PROFIT_LEVELS", "100, 25,50")
    assert Config.from_env(env_file=None, mode="paper").partial_take_profit_levels == (25, 50, 100)
    monkeypatch.setenv("PARTIAL_TAKE_PROFIT_LEVELS", "0")
    assert Config.from_env(env_file=None, mode="paper").partial_take_profit_levels == ()


async def test_losing_streak_pauses_buying(tmp_path):
    cands = [good_candidate(m) for m in ("A", "B", "C", "D")]
    bot, jup = make_bot(tmp_path, cands, max_open_positions=1, loss_streak_limit=3, loss_pause_minutes=45,
                        partial_take_profit_levels=(), stop_loss_pct=20, daily_loss_limit_sol=100)
    t = NOW
    for m in ("A", "B", "C"):
        assert await bot.scan_once(t) == [m]
        jup.sell_rate[m] = 0.7  # -30%: stop-loss
        assert await bot.monitor_once(t + 10) == [m]
        t += 60
    assert bot.paused(t)
    assert await bot.scan_once(t) == []  # paused after 3 losses in a row
    assert await bot.scan_once(t + 46 * 60) == ["D"]  # pause over
    bot.state.save()
    assert BotState.load(bot.state.path).loss_streak == 0


def test_rug_checks_lp_lock_and_creator_balance():
    from memebot.safety import rugcheck_lp_and_creator

    rc = {"totalMarketLiquidity": 50_000, "markets": [{"lp": {"lpLockedUSD": 20_000}}, {"lp": {"lpLockedUSD": 0}}],
          "token": {"supply": 1_000_000}, "creatorBalance": 80_000}
    lp, creator = rugcheck_lp_and_creator(rc)
    assert lp == 40 and creator == 8
    c = cfg(use_rugcheck=True, max_rugcheck_score=100, min_lp_locked_pct=90, max_creator_pct=5)
    reasons = safety_rejections(SafetyReport(mint="X", lp_locked_pct=lp, creator_pct=creator), c)
    assert any("liquidity locked" in r for r in reasons) and any("creator" in r for r in reasons)
    assert safety_rejections(SafetyReport(mint="X", lp_locked_pct=100, creator_pct=0), c) == []
    assert rugcheck_lp_and_creator({}) == (None, None)  # missing data doesn't block


async def test_buy_size_follows_market_activity(tmp_path):
    busy = [good_candidate(f"T{i}", liquidity_usd=10) for i in range(8)]  # active but fail filters (not bought)
    quiet = [good_candidate(f"Q{i}", liquidity_usd=10, volume_m5_usd=100, buys_m5=2, sells_m5=1) for i in range(8)]
    full = int(0.05 * LAMPORTS_PER_SOL)
    bot, _ = make_bot(tmp_path, quiet + [good_candidate("A")], buy_amount_sol=0.05, activity_hot_min=6,
                      quiet_size_fraction=0.5, activity_window_minutes=30)
    assert bot.buy_lamports() == full // 2  # no data yet: be careful
    assert await bot.scan_once(NOW) == ["A"]
    assert bot.market_activity() == 1  # only A was really trading
    assert bot.state.positions["A"].entry_lamports < full * 0.6  # quiet market: half size

    (tmp_path / "b").mkdir()
    bot2, _ = make_bot(tmp_path / "b", busy + [good_candidate("B")], buy_amount_sol=0.05, activity_hot_min=6,
                       quiet_size_fraction=0.5, state_file=str(tmp_path / "s2.json"))
    assert await bot2.scan_once(NOW) == ["B"]
    assert bot2.market_activity() == 9 and bot2.market_is_hot()
    assert bot2.state.positions["B"].entry_lamports >= full  # busy market: full size
    # old readings drop out of the 30-minute window
    bot2.record_activity(quiet, NOW + 31 * 60)
    assert bot2.market_activity() == 0 and bot2.buy_lamports() == full // 2


async def test_price_checks_continue_while_scan_is_stuck(tmp_path):
    import asyncio

    bot, jup = make_bot(tmp_path, [good_candidate("A")], monitor_interval_sec=0.01, scan_interval_sec=0.01,
                        stop_loss_pct=20, partial_take_profit_levels=())
    await bot.scan_once(NOW)
    assert "A" in bot.state.positions

    async def stuck():  # e.g. RPC retries / slow RugCheck during a scan
        await asyncio.sleep(3600)
    bot.dex.latest_token_addresses = stuck
    jup.sell_rate["A"] = 0.5  # crashes while the scan is stuck
    task = asyncio.create_task(bot.run())
    for _ in range(100):
        await asyncio.sleep(0.01)
        if "A" not in bot.state.positions:
            break
    assert "A" not in bot.state.positions  # stop-loss fired anyway
    bot.stop()
    task.cancel()
