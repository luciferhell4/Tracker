from wallet_monitor import notion_sync
from wallet_monitor.config import NotionSource
from wallet_monitor.models import Wallet

MARS = NotionSource(name="Project Mars Land", url="https://x.notion.site/52c36ac9a1074b8897f7e413caa0597d")
ARC = NotionSource(name="Arc Smart Money", url="https://x.notion.site/d0afecae9b1445fc8f73f1b44b43b75c")
STABLE = NotionSource(name="Scrap wallet stable", url="https://x.notion.site/80f2d75809244415bae1ff4b48d78cf1")


def title(text):
    return {"type": "title", "title": [{"plain_text": text}]}


def rich(text):
    return {"type": "rich_text", "rich_text": [{"plain_text": text}]}


def test_parse_database_id_from_site_and_dashed_urls():
    assert notion_sync.parse_database_id(MARS.url) == "52c36ac9a1074b8897f7e413caa0597d"
    dashed = "https://www.notion.so/team/d0afecae-9b14-45fc-8f73-f1b44b43b75c?v=abc"
    assert notion_sync.parse_database_id(dashed) == "d0afecae9b1445fc8f73f1b44b43b75c"


def test_wallet_row_with_explorer_link_and_rank():
    page = {
        "properties": {
            "Wallet": title("0xAAaaAAaaAAaaAAaaAAaaAAaaAAaaAAaaAAaaAAaa"),
            "Explorer": {"type": "url", "url": "https://abscan.org/address/0xaa"},
            "Rank": {"type": "number", "number": 3},
            "Tag": {"type": "select", "select": {"name": "Project Mars Land"}},
        }
    }
    wallet = notion_sync.wallet_from_page(page, MARS)
    assert wallet.chain == "abstract"
    assert wallet.address == "0x" + "aa" * 20
    assert wallet.rank == 3.0
    assert wallet.tag == "Project Mars Land"
    assert wallet.source == "Project Mars Land"


def test_wallet_row_with_gmgn_link_and_profit():
    page = {
        "properties": {
            "Wallet": title("So11111111111111111111111111111111111111112"),
            "gmgn": {"type": "url", "url": "https://gmgn.ai/sol/address/So111"},
            "Profit USD": {"type": "number", "number": 125000},
            "Credential": {
                "type": "multi_select",
                "multi_select": [{"name": "Top 3 COOL"}, {"name": "Early ARCANINE @$12k"}],
            },
        }
    }
    wallet = notion_sync.wallet_from_page(page, ARC)
    assert wallet.chain == "solana"
    assert wallet.pnl_usd == 125000.0
    assert wallet.tag == "Top 3 COOL, Early ARCANINE @$12k"


def test_chain_falls_back_to_address_shape_when_no_link():
    page = {
        "properties": {
            "Address": title("0x" + "bb" * 20),
            "Label": rich("Sniper"),
            "PNL": {"type": "number", "number": 42000},
            "Tokens": {"type": "multi_select", "multi_select": [{"name": "FEFER"}]},
        }
    }
    wallet = notion_sync.wallet_from_page(page, STABLE)
    assert wallet.chain == "ethereum"
    assert wallet.label == "Sniper"
    assert wallet.pnl_usd == 42000.0


def test_chain_hint_is_used_before_the_address_shape():
    hinted = NotionSource(name="hinted", url=STABLE.url, chain_hint="base")
    page = {"properties": {"Address": title("0x" + "cc" * 20)}}
    assert notion_sync.wallet_from_page(page, hinted).chain == "base"


def test_rows_without_an_address_are_skipped():
    page = {"properties": {"Wallet": title("TBD"), "Rank": {"type": "number", "number": 1}}}
    assert notion_sync.wallet_from_page(page, MARS) is None


def test_formula_property_is_flattened():
    prop = {"type": "formula", "formula": {"type": "string", "string": "https://opensea.io/0xabc"}}
    assert notion_sync.plain_value(prop) == "https://opensea.io/0xabc"


def test_dedupe_merges_sources_and_keeps_best_rank_and_pnl():
    address = "0x" + "dd" * 20
    merged = notion_sync.dedupe([
        Wallet(address=address, chain="base", source="A", tag="Inks", rank=9, pnl_usd=1000),
        Wallet(address=address, chain="base", source="B", tag="PonsGuy", rank=2, pnl_usd=50000),
        Wallet(address=address, chain="ethereum", source="C"),
    ])
    by_chain = {w.chain: w for w in merged}
    assert len(merged) == 2
    base = by_chain["base"]
    assert base.rank == 2
    assert base.pnl_usd == 50000
    assert "A" in base.source and "B" in base.source
    assert "Inks" in base.tag and "PonsGuy" in base.tag


def test_csv_round_trip(tmp_path):
    wallets = [
        Wallet(address="0x" + "ee" * 20, chain="base", tag="Inks", rank=1, pnl_usd=1234.5,
               url="https://basescan.org/address/0xee", source="notion"),
        Wallet(address="So11111111111111111111111111111111111111112", chain="solana"),
    ]
    path = tmp_path / "wallets.csv"
    assert notion_sync.write_csv(wallets, path) == 2
    loaded = {w.address: w for w in notion_sync.read_csv(path)}
    assert len(loaded) == 2
    restored = loaded["0x" + "ee" * 20]
    assert restored.chain == "base"
    assert restored.rank == 1
    assert restored.pnl_usd == 1234.5


def test_csv_import_tolerates_a_bare_address_column(tmp_path):
    path = tmp_path / "pasted.csv"
    path.write_text("address\n0x" + "ff" * 20 + "\nnot-a-wallet\n")
    wallets = notion_sync.read_csv(path)
    assert len(wallets) == 1
    assert wallets[0].chain == "ethereum"


def test_read_csv_on_missing_file_is_empty(tmp_path):
    assert notion_sync.read_csv(tmp_path / "nope.csv") == []
