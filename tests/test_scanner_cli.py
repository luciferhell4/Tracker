import time

from wallet_monitor import cli, notion_sync, report, scanner
from wallet_monitor.config import Config
from wallet_monitor.models import MintEvent, Signal, Wallet
from wallet_monitor.store import Store

A = "0x" + "a1" * 20
B = "0x" + "b2" * 20
CONTRACT = "0x" + "c3" * 20


class FakeProvider:
    """Stands in for Alchemy: serves a fixed set of mints, records its calls."""

    name = "fake"

    def __init__(self, events_by_wallet):
        self.events_by_wallet = events_by_wallet
        self.calls = []

    def supports(self, chain):
        return chain == "base"

    def fetch_mints(self, chain, wallet, *, since_block=0, lookback_hours=24):
        self.calls.append((chain, wallet, since_block))
        return list(self.events_by_wallet.get(wallet, []))


def mint(wallet, minutes_ago, block, now):
    return MintEvent(
        chain="base", wallet=wallet, contract=CONTRACT, token_id=str(block),
        tx_hash=f"0x{block}", block_number=block, timestamp=now - minutes_ago * 60,
        collection_name="Fresh Mint",
    )


def make_config(tmp_path):
    cfg = Config()
    cfg.store_path = str(tmp_path / "db.sqlite")
    cfg.wallets_csv = str(tmp_path / "wallets.csv")
    cfg.signal.min_wallets = 2
    cfg.signal.window_minutes = 180
    return cfg


def test_scan_records_mints_and_advances_cursors(tmp_path, monkeypatch):
    now = int(time.time())
    cfg = make_config(tmp_path)
    provider = FakeProvider({A: [mint(A, 10, 900, now)], B: [mint(B, 12, 880, now)]})
    monkeypatch.setattr(scanner, "build_providers", lambda _cfg: [provider])

    with Store(cfg.store_path) as store:
        store.upsert_wallets([
            Wallet(address=A, chain="base", rank=1),
            Wallet(address=B, chain="base", rank=2),
        ])
        result = scanner.scan(cfg, store)
        assert len(result.new_events) == 2
        assert result.per_chain == {"base": 2}
        assert result.scanned_wallets == 2
        assert store.cursor("base", A) == 900

        # A second pass sees the same data and records nothing new.
        again = scanner.scan(cfg, store)
        assert again.new_events == []
        second_pass = [c for c in provider.calls if c[1] == A][-1]
        assert second_pass[2] == 900  # the stored cursor was passed through


def test_scan_skips_chains_without_a_provider(tmp_path, monkeypatch):
    cfg = make_config(tmp_path)
    monkeypatch.setattr(scanner, "build_providers", lambda _cfg: [FakeProvider({})])
    with Store(cfg.store_path) as store:
        store.upsert_wallets([Wallet(address="So1111", chain="solana")])
        result = scanner.scan(cfg, store)
        assert "solana" in result.skipped_chains
        assert result.new_events == []


def test_scan_reports_provider_errors_without_stopping(tmp_path, monkeypatch):
    cfg = make_config(tmp_path)

    class Broken(FakeProvider):
        def fetch_mints(self, chain, wallet, *, since_block=0, lookback_hours=24):
            if wallet == A:
                raise RuntimeError("rate limited")
            return [mint(B, 5, 800, int(time.time()))]

    monkeypatch.setattr(scanner, "build_providers", lambda _cfg: [Broken({})])
    with Store(cfg.store_path) as store:
        store.upsert_wallets([
            Wallet(address=A, chain="base"), Wallet(address=B, chain="base"),
        ])
        result = scanner.scan(cfg, store)
        assert len(result.errors) == 1
        assert "rate limited" in result.errors[0]
        assert len(result.new_events) == 1


def test_scan_drops_mints_older_than_the_lookback(tmp_path, monkeypatch):
    now = int(time.time())
    cfg = make_config(tmp_path)
    cfg.scan.lookback_hours = 1
    monkeypatch.setattr(
        scanner, "build_providers", lambda _cfg: [FakeProvider({A: [mint(A, 600, 700, now)]})]
    )
    with Store(cfg.store_path) as store:
        store.upsert_wallets([Wallet(address=A, chain="base")])
        assert scanner.scan(cfg, store).new_events == []


def test_no_providers_is_reported_not_raised(tmp_path, monkeypatch):
    cfg = make_config(tmp_path)
    monkeypatch.setattr(scanner, "build_providers", lambda _cfg: [])
    with Store(cfg.store_path) as store:
        result = scanner.scan(cfg, store)
        assert result.errors and "No chain provider is available" in result.errors[0]


def test_scan_command_end_to_end(tmp_path, monkeypatch, capsys):
    now = int(time.time())
    cfg = make_config(tmp_path)
    provider = FakeProvider({A: [mint(A, 10, 900, now)], B: [mint(B, 12, 880, now)]})
    monkeypatch.setattr(scanner, "build_providers", lambda _cfg: [provider])
    monkeypatch.setattr(cli.Config, "load", classmethod(lambda cls, path=None: cfg))

    csv_path = tmp_path / "in.csv"
    csv_path.write_text(f"address,chain,rank\n{A},base,1\n{B},base,2\n")
    assert cli.main(["import-csv", str(csv_path)]) == 0
    assert cli.main(["scan"]) == 0

    out = capsys.readouterr().out
    assert "2 new mints" in out
    assert "Fresh Mint" in out
    assert CONTRACT in out

    # The same contract must not alert twice at the same wallet count.
    assert cli.main(["scan"]) == 0
    assert "No new clustered mints" in capsys.readouterr().out


def test_wallets_and_doctor_commands(tmp_path, monkeypatch, capsys):
    cfg = make_config(tmp_path)
    monkeypatch.setattr(cli.Config, "load", classmethod(lambda cls, path=None: cfg))
    assert cli.main(["wallets"]) == 1          # nothing imported yet
    notion_sync.write_csv([Wallet(address=A, chain="base", rank=1)], cfg.wallets_csv)
    assert cli.main(["import-csv"]) == 0
    assert cli.main(["wallets", "--chain", "base"]) == 0
    assert A in capsys.readouterr().out
    assert cli.main(["doctor"]) == 0
    assert "NOTION_TOKEN" in capsys.readouterr().out


def test_sync_without_a_token_fails_with_guidance(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("NOTION_TOKEN", raising=False)
    cfg = make_config(tmp_path)
    monkeypatch.setattr(cli.Config, "load", classmethod(lambda cls, path=None: cfg))
    assert cli.main(["sync"]) == 1
    assert "NOTION_TOKEN" in capsys.readouterr().err


def test_report_rendering():
    now = int(time.time())
    signal = Signal(
        chain="base", contract=CONTRACT, collection_name="Fresh Mint", score=12.5,
        wallet_count=3, mint_count=4, first_mint_at=now - 3600, last_mint_at=now - 600,
        wallets=[Wallet(address=A, chain="base", rank=1, pnl_usd=250_000, tag="Inks")],
        sample_tx="0xabc",
    )
    text = report.render([signal], now)
    assert "Fresh Mint" in text
    assert "basescan.org" in text
    assert "rank 1" in text
    assert "$250,000 pnl" in text

    markdown = report.render_markdown([signal], now)
    assert markdown.startswith("| Score |")
    assert "12.50" in markdown
    assert report.render([]) .startswith("No clustered mints")


def test_add_command_from_arguments(tmp_path, monkeypatch, capsys):
    cfg = make_config(tmp_path)
    monkeypatch.setattr(cli.Config, "load", classmethod(lambda cls, path=None: cfg))
    assert cli.main(["add", A, B, "--chain", "base", "--tag", "Inks"]) == 0
    assert "Added 2 wallets" in capsys.readouterr().out
    with Store(cfg.store_path) as store:
        wallets = store.wallets("base")
        assert {w.address for w in wallets} == {A, B}
        assert all(w.tag == "Inks" for w in wallets)


def test_add_command_from_stdin(tmp_path, monkeypatch, capsys):
    import io

    cfg = make_config(tmp_path)
    monkeypatch.setattr(cli.Config, "load", classmethod(lambda cls, path=None: cfg))
    pasted = f"1\t{A}\thttps://basescan.org/address/{A}\t$50,000\n"
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(pasted))
    assert cli.main(["add"]) == 0
    with Store(cfg.store_path) as store:
        wallet = store.wallets("base")[0]
        assert wallet.rank == 1
        assert wallet.pnl_usd == 50_000


def test_add_command_rejects_input_without_addresses(tmp_path, monkeypatch, capsys):
    import io

    cfg = make_config(tmp_path)
    monkeypatch.setattr(cli.Config, "load", classmethod(lambda cls, path=None: cfg))
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO("nothing useful here\n"))
    assert cli.main(["add"]) == 1
    assert "No addresses found" in capsys.readouterr().out
