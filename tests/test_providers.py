import pytest

from wallet_monitor import chains
from wallet_monitor.config import Config
from wallet_monitor.providers.alchemy import AlchemyProvider
from wallet_monitor.providers.etherscan import EtherscanProvider
from wallet_monitor.providers.helius import HeliusProvider, WSOL
from wallet_monitor.providers.registry import build_providers, missing_credentials, provider_for

WALLET = "0x" + "ab" * 20


def cfg_with(monkeypatch, **env):
    for key in ("ALCHEMY_API_KEY", "ETHERSCAN_API_KEY", "HELIUS_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return Config()


def test_alchemy_transfer_becomes_a_mint_event():
    event = AlchemyProvider._to_event(
        "base",
        WALLET.upper(),
        {
            "blockNum": "0x1a4",
            "hash": "0xdeadbeef",
            "category": "erc721",
            "asset": "Cool Cats",
            "tokenId": "0x2a",
            "rawContract": {"address": "0xCONTRACT0000000000000000000000000000BEEF"},
            "metadata": {"blockTimestamp": "2024-05-01T12:00:00.000Z"},
        },
    )
    assert event.chain == "base"
    assert event.wallet == WALLET
    assert event.contract == "0xcontract0000000000000000000000000000beef"
    assert event.token_id == "42"
    assert event.block_number == 420
    assert event.timestamp == 1714564800
    assert event.collection_name == "Cool Cats"


def test_alchemy_erc1155_quantity_is_summed():
    event = AlchemyProvider._to_event(
        "base",
        WALLET,
        {
            "blockNum": "0x1",
            "hash": "0x1",
            "category": "erc1155",
            "rawContract": {"address": "0xabc"},
            "erc1155Metadata": [{"tokenId": "0x1", "value": "0x2"}, {"tokenId": "0x1", "value": "0x3"}],
            "metadata": {"blockTimestamp": "2024-05-01T12:00:00Z"},
        },
    )
    assert event.quantity == 5
    assert event.token_standard == "erc1155"


def test_alchemy_transfer_without_a_contract_is_ignored():
    assert AlchemyProvider._to_event("base", WALLET, {"rawContract": {}}) is None


def test_etherscan_keeps_only_zero_address_mints_to_the_wallet():
    rows = {
        "status": "1",
        "result": [
            {"from": chains.ZERO_ADDRESS, "to": WALLET, "contractAddress": "0xAAA",
             "tokenID": "7", "hash": "0x1", "blockNumber": "100", "timeStamp": "1714564800",
             "tokenName": "Mint Me"},
            {"from": "0x" + "99" * 20, "to": WALLET, "contractAddress": "0xBBB",
             "tokenID": "8", "hash": "0x2", "blockNumber": "101", "timeStamp": "1714564900"},
            {"from": chains.ZERO_ADDRESS, "to": "0x" + "cd" * 20, "contractAddress": "0xCCC",
             "tokenID": "9", "hash": "0x3", "blockNumber": "102", "timeStamp": "1714565000"},
        ],
    }
    events = EtherscanProvider._parse("ethereum", WALLET, "erc721", rows)
    assert [e.contract for e in events] == ["0xaaa"]
    assert events[0].token_id == "7"
    assert events[0].block_number == 100


def test_etherscan_handles_the_no_transactions_response():
    empty = {"status": "0", "message": "No transactions found", "result": "No transactions found"}
    assert EtherscanProvider._parse("ethereum", WALLET, "erc721", empty) == []
    assert EtherscanProvider._parse("ethereum", WALLET, "erc721", None) == []


def test_helius_swap_ignores_sol_and_records_the_bought_token(monkeypatch):
    provider = HeliusProvider(cfg_with(monkeypatch, HELIUS_API_KEY="k"))
    tx = {
        "signature": "sig1",
        "slot": 99,
        "timestamp": 1714564800,
        "description": "swapped 1 SOL for BONK",
        "tokenTransfers": [
            {"toUserAccount": "me", "mint": WSOL},
            {"toUserAccount": "me", "mint": "BonkMint111"},
            {"toUserAccount": "someone-else", "mint": "OtherMint111"},
        ],
    }
    events = provider._from_swap("me", tx)
    assert [e.contract for e in events] == ["BonkMint111"]
    assert events[0].token_standard == "spl-buy"
    assert events[0].block_number == 99


def test_provider_support_matrix(monkeypatch):
    cfg = cfg_with(monkeypatch, ALCHEMY_API_KEY="a", ETHERSCAN_API_KEY="e", HELIUS_API_KEY="h")
    providers = build_providers(cfg)
    assert [p.name for p in providers] == ["alchemy", "rpc", "etherscan", "helius"]
    assert provider_for(providers, "base").name == "alchemy"
    assert provider_for(providers, "solana").name == "helius"
    # HyperEVM has no Alchemy network but does have a public RPC, which beats
    # Etherscan because Etherscan only indexes a fixed list of chain ids.
    assert provider_for(providers, "hyperevm").name == "rpc"
    assert provider_for(providers, "robinhood").name == "rpc"
    # Arc has neither an RPC nor Etherscan coverage, so nothing serves it.
    assert provider_for(providers, "arc") is None


def test_public_rpc_chains_need_no_keys_at_all(monkeypatch):
    cfg = cfg_with(monkeypatch)
    providers = build_providers(cfg)
    assert [p.name for p in providers] == ["rpc"]
    # The chains the watchlist actually lives on work with zero credentials.
    assert provider_for(providers, "robinhood").name == "rpc"
    assert provider_for(providers, "hyperevm").name == "rpc"
    # Chains that need a commercial provider still report as uncovered.
    assert provider_for(providers, "ethereum") is None
    assert provider_for(providers, "solana") is None
    assert len(missing_credentials(cfg)) == 2


def test_etherscan_is_never_asked_about_chains_it_cannot_index(monkeypatch):
    cfg = cfg_with(monkeypatch, ETHERSCAN_API_KEY="e")
    provider = EtherscanProvider(cfg)
    assert provider.supports("ethereum")
    # Robinhood Chain and Arc carry EVM chain ids Etherscan V2 does not know.
    assert not provider.supports("robinhood")
    assert not provider.supports("arc")


def test_etherscan_surfaces_a_rejected_key_instead_of_reading_it_as_empty():
    rejected = {"status": "0", "message": "NOTOK", "result": "Invalid API Key"}
    with pytest.raises(Exception) as err:
        EtherscanProvider._parse("ethereum", WALLET, "erc721", rejected)
    assert "Invalid API Key" in str(err.value)
