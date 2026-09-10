import time

from wallet_monitor.config import SignalRules
from wallet_monitor.models import MintEvent, Wallet
from wallet_monitor.scoring import best_window, build_signals, wallet_weight

NOW = 1_700_000_000
CONTRACT = "0x" + "11" * 20


def mint(wallet, minutes_ago, contract=CONTRACT, chain="base", token_id="1"):
    return MintEvent(
        chain=chain,
        wallet=wallet,
        contract=contract,
        token_id=token_id,
        tx_hash=f"0x{wallet[-4:]}{minutes_ago}",
        block_number=1000 + minutes_ago,
        timestamp=NOW - minutes_ago * 60,
        collection_name="Test Collection",
    )


def wallets(*specs):
    out = {}
    for address, rank, pnl in specs:
        w = Wallet(address=address, chain="base", rank=rank, pnl_usd=pnl)
        out[w.key()] = w
    return out


def test_wallet_weight_rewards_rank_and_pnl():
    assert wallet_weight(None) == 1.0
    assert wallet_weight(Wallet(address="a", chain="base")) == 1.0
    top = wallet_weight(Wallet(address="a", chain="base", rank=1))
    mid = wallet_weight(Wallet(address="b", chain="base", rank=40))
    assert top > mid > 1.0
    assert wallet_weight(Wallet(address="c", chain="base", rank=200)) == 1.0
    rich = wallet_weight(Wallet(address="d", chain="base", pnl_usd=1_000_000))
    assert rich > wallet_weight(Wallet(address="e", chain="base", pnl_usd=10_000))


def test_best_window_finds_the_densest_slice():
    events = [(0, "a"), (10, "b"), (5000, "c"), (5010, "d"), (5020, "e")]
    members, start, end = best_window(events, window_seconds=120)
    assert members == ["c", "d", "e"]
    assert (start, end) == (5000, 5020)


def test_best_window_counts_each_wallet_once():
    members, _, _ = best_window([(0, "a"), (5, "a"), (10, "a")], window_seconds=60)
    assert members == ["a"]


def test_best_window_on_empty_input():
    assert best_window([], 60) == ([], 0, 0)


def test_signal_requires_the_minimum_distinct_wallets():
    rules = SignalRules(min_wallets=3, window_minutes=60)
    index = wallets(("0xa", None, None), ("0xb", None, None))
    events = [mint("0xa", 5), mint("0xb", 6, token_id="2")]
    assert build_signals(events, index, rules, now=NOW) == []

    rules = SignalRules(min_wallets=2, window_minutes=60)
    signals = build_signals(events, index, rules, now=NOW)
    assert len(signals) == 1
    assert signals[0].wallet_count == 2
    assert signals[0].collection_name == "Test Collection"


def test_mints_spread_beyond_the_window_do_not_cluster():
    rules = SignalRules(min_wallets=2, window_minutes=30)
    index = wallets(("0xa", None, None), ("0xb", None, None))
    signals = build_signals([mint("0xa", 5), mint("0xb", 600)], index, rules, now=NOW)
    assert signals == []


def test_stale_contracts_are_dropped():
    rules = SignalRules(min_wallets=2, window_minutes=180, max_contract_age_hours=6)
    index = wallets(("0xa", None, None), ("0xb", None, None))
    events = [mint("0xa", 10), mint("0xb", 20)]
    assert build_signals(events, index, rules, now=NOW)
    stale = build_signals(
        events, index, rules, now=NOW, first_seen=lambda c, k: NOW - 48 * 3600
    )
    assert stale == []


def test_better_wallets_outrank_more_recent_ones():
    rules = SignalRules(min_wallets=2, window_minutes=180)
    other = "0x" + "22" * 20
    index = {
        ("base", "0xa"): Wallet(address="0xa", chain="base", rank=1, pnl_usd=500_000),
        ("base", "0xb"): Wallet(address="0xb", chain="base", rank=2, pnl_usd=400_000),
        ("base", "0xc"): Wallet(address="0xc", chain="base"),
        ("base", "0xd"): Wallet(address="0xd", chain="base"),
    }
    events = [
        mint("0xa", 30), mint("0xb", 32),
        mint("0xc", 5, contract=other), mint("0xd", 7, contract=other),
    ]
    signals = build_signals(events, index, rules, now=NOW)
    assert [s.contract for s in signals] == [CONTRACT, other]
    assert signals[0].score > signals[1].score


def test_top_n_truncates_the_report():
    rules = SignalRules(min_wallets=2, window_minutes=180, top_n=1)
    index = wallets(("0xa", None, None), ("0xb", None, None))
    events = [
        mint("0xa", 5), mint("0xb", 6),
        mint("0xa", 5, contract="0x" + "33" * 20),
        mint("0xb", 6, contract="0x" + "33" * 20),
    ]
    assert len(build_signals(events, index, rules, now=NOW)) == 1


def test_signals_default_to_wall_clock_now():
    rules = SignalRules(min_wallets=2, window_minutes=180)
    now = int(time.time())
    index = wallets(("0xa", None, None), ("0xb", None, None))
    events = [
        MintEvent(chain="base", wallet="0xa", contract=CONTRACT, token_id="1",
                  tx_hash="0x1", block_number=1, timestamp=now - 60),
        MintEvent(chain="base", wallet="0xb", contract=CONTRACT, token_id="2",
                  tx_hash="0x2", block_number=2, timestamp=now - 30),
    ]
    signals = build_signals(events, index, rules)
    assert len(signals) == 1
    assert signals[0].window_minutes == 0.5
