import json
import threading
import time
import urllib.request

import pytest

from wallet_monitor import scanner, web
from wallet_monitor.config import Config
from wallet_monitor.models import MintEvent, Wallet
from wallet_monitor.store import Store

A = "0x" + "a1" * 20
B = "0x" + "b2" * 20
CONTRACT = "0x" + "c3" * 20


@pytest.fixture
def cfg(tmp_path):
    config = Config()
    config.store_path = str(tmp_path / "db.sqlite")
    config.wallets_csv = str(tmp_path / "wallets.csv")
    config.signal.min_wallets = 2
    config.signal.window_minutes = 180
    return config


def seed(cfg, now=None):
    now = now or int(time.time())
    with Store(cfg.store_path) as store:
        store.upsert_wallets([
            Wallet(address=A, chain="base", rank=1, pnl_usd=250_000, tag="Inks"),
            Wallet(address=B, chain="base", rank=4),
        ])
        store.record_mints([
            MintEvent(chain="base", wallet=A, contract=CONTRACT, token_id="1",
                      tx_hash="0x1", block_number=10, timestamp=now - 300,
                      collection_name="Fresh Mint"),
            MintEvent(chain="base", wallet=B, contract=CONTRACT, token_id="2",
                      tx_hash="0x2", block_number=11, timestamp=now - 240,
                      collection_name="Fresh Mint"),
        ])
    return now


@pytest.fixture
def client(cfg):
    """A live server on an ephemeral port, torn down with the test."""
    httpd = web.serve(cfg, "127.0.0.1", 0)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()

    def call(path, payload=None):
        url = f"http://127.0.0.1:{port}{path}"
        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"},
            method="POST" if data is not None else "GET",
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            body = response.read()
            kind = response.headers.get("Content-Type", "")
            return json.loads(body) if kind.startswith("application/json") else body.decode()

    try:
        yield call
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_index_and_assets_are_served(client):
    assert "<title>Wallet Monitor</title>" in client("/")
    assert "--surface" in client("/styles.css")
    assert "loadSignals" in client("/app.js")


def test_state_reports_stats_credentials_and_coverage(cfg, client):
    seed(cfg)
    state = client("/api/state")
    assert state["stats"]["wallets"] == 2
    assert state["stats"]["mints"] == 2
    assert state["coverage"][0] == {"chain": "base", "wallets": 2, "provider": ""}
    assert state["credentials"]["alchemy"] is False
    assert any(c["key"] == "solana" for c in state["chains"])
    assert state["rules"]["min_wallets"] == 2


def test_signals_endpoint_scores_the_cluster(cfg, client):
    seed(cfg)
    data = client("/api/signals?hours=24")
    assert len(data["signals"]) == 1
    signal = data["signals"][0]
    assert signal["collection_name"] == "Fresh Mint"
    assert signal["wallet_count"] == 2
    assert signal["token_url"].startswith("https://basescan.org/token/")
    assert signal["wallets"][0]["short"].startswith("0xa1a1")
    assert signal["score"] > 0


def test_signal_filters_are_applied(cfg, client):
    seed(cfg)
    assert client("/api/signals?hours=24&min_wallets=3")["signals"] == []
    assert client("/api/signals?hours=24&window_minutes=30")["signals"] != []
    assert client("/api/signals?hours=24&chain=solana")["signals"] == []


def test_activity_returns_hourly_buckets_and_a_feed(cfg, client):
    seed(cfg)
    data = client("/api/activity?hours=24")
    assert sum(bucket["count"] for bucket in data["hourly"]) == 2
    assert len(data["mints"]) == 2
    assert data["mints"][0]["tx_url"].startswith("https://basescan.org/tx/")


def test_wallets_can_be_pasted_in_and_removed(client):
    added = client("/api/wallets", {"text": f"1 {A} https://basescan.org/address/{A}", "tag": "Inks"})
    assert added["added"] == 1
    assert added["wallets"][0]["chain"] == "base"
    assert client("/api/wallets")["wallets"][0]["tag"] == "Inks"

    assert client("/api/wallets/delete", {"chain": "base", "address": A}) == {"deleted": True}
    assert client("/api/wallets")["wallets"] == []


def test_pasting_text_without_addresses_adds_nothing(client):
    assert client("/api/wallets", {"text": "just some notes"})["added"] == 0


def test_scan_runs_in_the_background_and_records_its_outcome(cfg, client, monkeypatch):
    now = int(time.time())
    seed(cfg, now)

    class FakeProvider:
        name = "fake"

        def supports(self, chain):
            return chain == "base"

        def fetch_mints(self, chain, wallet, *, since_block=0, lookback_hours=24):
            return [MintEvent(chain="base", wallet=wallet, contract=CONTRACT,
                              token_id="9", tx_hash="0x9", block_number=99,
                              timestamp=now - 60, collection_name="Fresh Mint")]

    monkeypatch.setattr(scanner, "build_providers", lambda _cfg: [FakeProvider()])

    assert client("/api/scan", {})["started"] is True
    for _ in range(100):
        scan_state = client("/api/scan")["scan"]
        if not scan_state["running"]:
            break
        time.sleep(0.05)
    assert scan_state["running"] is False
    assert scan_state["new_mints"] == 2
    assert client("/api/state")["last_scan"]["new_signals"] >= 1


def test_a_second_scan_is_refused_while_one_is_running(cfg):
    service = web.MonitorService(cfg)
    service.scan_state.running = True
    assert service.start_scan()["started"] is False


def test_unknown_routes_and_bad_bodies_are_handled(client):
    with pytest.raises(urllib.error.HTTPError) as err:
        client("/api/nope")
    assert err.value.code == 404
    with pytest.raises(urllib.error.HTTPError):
        client("/../etc/passwd")
