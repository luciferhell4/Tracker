from wallet_monitor import paste

A = "0x" + "a1" * 20
B = "0x" + "b2" * 20
SOL = "So11111111111111111111111111111111111111112"


def test_addresses_are_found_inside_explorer_links():
    found = paste.addresses_in(f"1  {A}  https://abscan.org/address/{A}  Project Mars Land")
    assert found == [(A, "abstract")]


def test_link_only_row_still_yields_the_address():
    found = paste.addresses_in(f"https://basescan.org/address/{B}")
    assert found == [(B, "base")]


def test_solana_address_is_recognised_from_a_gmgn_link():
    found = paste.addresses_in(f"{SOL}  https://gmgn.ai/sol/address/{SOL}")
    assert found == [(SOL, "solana")]


def test_prose_is_not_mistaken_for_a_solana_address():
    line = "thisisaverylongwordthatisdefinitelynotanaddress and more words here"
    assert paste.addresses_in(line) == []


def test_chain_hint_applies_when_a_row_has_no_link():
    assert paste.addresses_in(A, chain_hint="base") == [(A, "base")]
    assert paste.addresses_in(A) == [(A, "ethereum")]


def test_rank_and_pnl_are_read_off_a_pasted_table_row():
    wallets = paste.parse(f"3\t{A}\thttps://basescan.org/address/{A}\t$125,000\tInks")
    assert len(wallets) == 1
    wallet = wallets[0]
    assert wallet.chain == "base"
    assert wallet.rank == 3
    assert wallet.pnl_usd == 125000


def test_parse_handles_a_multi_line_paste_and_blank_lines():
    text = f"""
    1  {A}  https://abscan.org/address/{A}
    2  {B}  https://basescan.org/address/{B}

    {SOL}
    """
    wallets = paste.parse(text, source="notion-copy", tag="Mars")
    assert [w.address for w in wallets] == [A, B, SOL]
    assert [w.chain for w in wallets] == ["abstract", "base", "solana"]
    assert all(w.source == "notion-copy" and w.tag == "Mars" for w in wallets)


def test_forced_chain_overrides_the_link():
    wallets = paste.parse(f"{A} https://basescan.org/address/{A}", chain="abstract")
    assert wallets[0].chain == "abstract"


def test_duplicate_address_on_one_line_is_recorded_once():
    assert len(paste.addresses_in(f"{A} {A.upper()}")) == 1


def test_empty_input_is_empty_output():
    assert paste.parse("") == []
    assert paste.parse("   \n\n  ") == []
