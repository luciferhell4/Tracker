from wallet_monitor.models import MintEvent, Wallet
from wallet_monitor.store import Store

WALLET = "0x" + "ab" * 20


def event(wallet=WALLET, token_id="1", ts=1_700_000_000, block=100):
    return MintEvent(
        chain="base", wallet=wallet, contract="0xcafe", token_id=token_id,
        tx_hash="0xtx", block_number=block, timestamp=ts,
    )


def test_wallets_upsert_and_merge(tmp_path):
    with Store(tmp_path / "db.sqlite") as store:
        store.upsert_wallets([Wallet(address=WALLET, chain="base", rank=5, pnl_usd=100, tag="a")])
        store.upsert_wallets([Wallet(address=WALLET, chain="base", tag="b")])
        wallets = store.wallets()
        assert len(wallets) == 1
        assert wallets[0].tag == "b"
        # Nulls must not wipe values an earlier import supplied.
        assert wallets[0].rank == 5
        assert wallets[0].pnl_usd == 100


def test_wallets_are_listed_best_rank_first(tmp_path):
    with Store(tmp_path / "db.sqlite") as store:
        store.upsert_wallets([
            Wallet(address="0x" + "22" * 20, chain="base", rank=9),
            Wallet(address="0x" + "11" * 20, chain="base", rank=1),
            Wallet(address="0x" + "33" * 20, chain="base"),
        ])
        ranks = [w.rank for w in store.wallets("base")]
        assert ranks == [1, 9, None]
        assert store.wallet_chains() == ["base"]


def test_record_mints_returns_only_new_rows(tmp_path):
    with Store(tmp_path / "db.sqlite") as store:
        assert len(store.record_mints([event(), event(token_id="2")])) == 2
        assert store.record_mints([event()]) == []
        assert len(store.mints_since(0)) == 2
        assert store.mints_since(1_700_000_001) == []


def test_contract_first_seen_is_the_earliest_timestamp(tmp_path):
    with Store(tmp_path / "db.sqlite") as store:
        store.record_mints([event(ts=200, token_id="1"), event(ts=100, token_id="2")])
        assert store.contract_first_seen("base", "0xcafe") == 100
        assert store.contract_first_seen("base", "0xnope") is None


def test_cursor_never_moves_backwards(tmp_path):
    with Store(tmp_path / "db.sqlite") as store:
        assert store.cursor("base", WALLET) == 0
        store.set_cursor("base", WALLET, 500)
        store.set_cursor("base", WALLET, 100)
        assert store.cursor("base", WALLET) == 500


def test_alerting_repeats_only_when_more_wallets_join(tmp_path):
    with Store(tmp_path / "db.sqlite") as store:
        assert store.should_alert("base", "0xcafe", 2)
        store.mark_alerted("base", "0xcafe", 2)
        assert not store.should_alert("base", "0xcafe", 2)
        assert store.should_alert("base", "0xcafe", 3)
        store.mark_alerted("base", "0xcafe", 3)
        assert not store.should_alert("base", "0xcafe", 3)


def test_stats_counts_distinct_contracts(tmp_path):
    with Store(tmp_path / "db.sqlite") as store:
        store.upsert_wallets([Wallet(address=WALLET, chain="base")])
        store.record_mints([event(token_id="1"), event(token_id="2")])
        stats = store.stats()
        assert stats["wallets"] == 1
        assert stats["mints"] == 2
        assert stats["contracts"] == 1
