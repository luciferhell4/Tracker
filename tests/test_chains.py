from wallet_monitor import chains


def test_chain_inferred_from_explorer_host():
    assert chains.chain_from_url("https://abscan.org/address/0xdead") == "abstract"
    assert chains.chain_from_url("https://basescan.org/token/0xdead") == "base"
    assert chains.chain_from_url("https://solscan.io/account/abc") == "solana"
    assert chains.chain_from_url("https://gmgn.ai/sol/token/xyz") == "solana"
    assert chains.chain_from_url(None) is None


def test_optimism_wins_over_bare_etherscan():
    assert chains.chain_from_url("https://optimistic.etherscan.io/address/0x1") == "optimism"
    assert chains.chain_from_url("https://etherscan.io/address/0x1") == "ethereum"


def test_normalise_address_evm_is_lowercased():
    address, kind = chains.normalise_address("0xAbCdEf0123456789AbCdEf0123456789AbCdEf01")
    assert kind == "evm"
    assert address == "0xabcdef0123456789abcdef0123456789abcdef01"


def test_normalise_address_accepts_base58_and_rejects_junk():
    address, kind = chains.normalise_address("So11111111111111111111111111111111111111112")
    assert (address, kind) == ("So11111111111111111111111111111111111111112", "svm")
    assert chains.normalise_address("not an address at all") is None
    assert chains.normalise_address("") is None
    assert chains.normalise_address("0x1234") is None


def test_explorer_links_are_chain_specific():
    assert chains.get("base").address_url("0x1").startswith("https://basescan.org/address/")
    assert chains.get("solana").address_url("abc") == "https://solscan.io/account/abc"
