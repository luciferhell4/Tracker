import pytest

from wallet_monitor.config import Config
from wallet_monitor.providers.base import ProviderUnavailable
from wallet_monitor.providers.rpc import (
    TRANSFER_SINGLE_TOPIC,
    TRANSFER_TOPIC,
    ZERO_TOPIC,
    RpcProvider,
)

WATCHED = "0x" + "a1" * 20
STRANGER = "0x" + "b2" * 20
CONTRACT = "0x" + "c3" * 20


def topic_for(address: str) -> str:
    return "0x" + "0" * 24 + address[2:]


def erc721_log(to: str, block: int = 100, token_id: int = 7) -> dict:
    return {
        "address": CONTRACT,
        "topics": [TRANSFER_TOPIC, ZERO_TOPIC, topic_for(to), hex(token_id)],
        "data": "0x",
        "blockNumber": hex(block),
        "transactionHash": "0xdead",
    }


def erc1155_log(to: str, block: int = 101) -> dict:
    return {
        "address": CONTRACT,
        "topics": [TRANSFER_SINGLE_TOPIC, topic_for(STRANGER), ZERO_TOPIC, topic_for(to)],
        "data": "0x" + f"{9:064x}" + f"{2:064x}",
        "blockNumber": hex(block),
        "transactionHash": "0xbeef",
    }


class FakeRpc(RpcProvider):
    """Drives the sweep without touching a network."""

    def __init__(self, erc721=None, erc1155=None, head=1000, timestamps=None):
        super().__init__(Config())
        self.erc721 = erc721 or []
        self.erc1155 = erc1155 or []
        self.head = head
        self.timestamps = timestamps or {}
        self.calls = []

    def call(self, chain, method, params):
        self.calls.append((method, params))
        if method == "eth_blockNumber":
            return hex(self.head)
        if method == "eth_getBlockByNumber":
            block = int(params[0], 16)
            return {"timestamp": hex(self.timestamps.get(block, 1_700_000_000))}
        if method == "eth_getLogs":
            topics = params[0]["topics"]
            return self.erc721 if topics[0] == TRANSFER_TOPIC else self.erc1155
        raise AssertionError(f"unexpected method {method}")


@pytest.fixture(autouse=True)
def no_sleeping(monkeypatch):
    monkeypatch.setattr("wallet_monitor.providers.rpc.time.sleep", lambda _s: None)


def test_sweep_keeps_only_watched_wallets():
    provider = FakeRpc(erc721=[erc721_log(WATCHED), erc721_log(STRANGER)])
    events, reached = provider.sweep("robinhood", 0, 500, [WATCHED])
    assert len(events) == 1
    assert events[0].wallet == WATCHED
    assert events[0].contract == CONTRACT
    assert events[0].token_id == "7"
    assert events[0].token_standard == "erc721"
    assert reached == 500


def test_sweep_reads_erc1155_recipient_from_the_fourth_topic():
    provider = FakeRpc(erc1155=[erc1155_log(WATCHED)])
    events, _ = provider.sweep("robinhood", 0, 500, [WATCHED])
    assert [e.token_standard for e in events] == ["erc1155"]
    assert events[0].token_id == "9"


def test_sweep_ignores_erc20_transfers_sharing_the_signature():
    # An ERC-20 Transfer indexes only two arguments, so it has three topics.
    erc20 = erc721_log(WATCHED)
    erc20["topics"] = erc20["topics"][:3]
    provider = FakeRpc(erc721=[erc20])
    events, _ = provider.sweep("robinhood", 0, 500, [WATCHED])
    assert events == []


def test_sweep_caps_the_window_and_reports_where_it_reached():
    provider = FakeRpc()
    _, reached = provider.sweep("robinhood", 0, 10_000_000, [WATCHED])
    # Robinhood advances 3000 blocks per pass, so it catches up over time.
    assert reached == provider.window_for("robinhood")


def test_sweep_is_a_no_op_without_watched_wallets():
    provider = FakeRpc(erc721=[erc721_log(WATCHED)])
    events, reached = provider.sweep("robinhood", 100, 500, [])
    assert events == []
    assert reached == 500
    assert provider.calls == []


def test_block_timestamps_are_read_not_guessed():
    provider = FakeRpc(erc721=[erc721_log(WATCHED, block=250)], timestamps={250: 1_699_999_999})
    events, _ = provider.sweep("robinhood", 0, 500, [WATCHED])
    assert events[0].timestamp == 1_699_999_999


def test_unknown_block_time_stays_zero_so_the_caller_can_drop_it(monkeypatch):
    provider = FakeRpc(erc721=[erc721_log(WATCHED, block=250)])
    original = provider.call

    def flaky(chain, method, params):
        if method == "eth_getBlockByNumber":
            raise RuntimeError("node refused")
        return original(chain, method, params)

    monkeypatch.setattr(provider, "call", flaky)
    events, _ = provider.sweep("robinhood", 0, 500, [WATCHED])
    assert events[0].timestamp == 0


def test_long_windows_are_split_into_slices_the_rpc_accepts():
    provider = FakeRpc()
    provider.sweep("robinhood", 0, 3000, [WATCHED])
    ranges = [p[0] for m, p in provider.calls if m == "eth_getLogs"]
    spans = {int(r["toBlock"], 16) - int(r["fromBlock"], 16) + 1 for r in ranges}
    # Robinhood's RPC rejects anything wider than 1000 blocks.
    assert max(spans) <= 1000


def test_every_endpoint_is_named_when_a_chain_is_unreachable(monkeypatch):
    provider = RpcProvider(Config())
    monkeypatch.setattr(
        "wallet_monitor.providers.rpc.request_json",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("HTTP 403")),
    )
    with pytest.raises(ProviderUnavailable) as err:
        provider.call("robinhood", "eth_blockNumber", [])
    message = str(err.value)
    assert "rpc.mainnet.chain.robinhood.com" in message
    assert "robinhood-rpc.publicnode.com" in message


def test_a_chain_without_an_rpc_is_unsupported():
    provider = RpcProvider(Config())
    assert not provider.supports("arc")
    with pytest.raises(ProviderUnavailable):
        provider.call("arc", "eth_blockNumber", [])


def test_fetch_mints_is_inert_because_sweeps_are_chain_wide():
    assert RpcProvider(Config()).fetch_mints("robinhood", WATCHED) == []
