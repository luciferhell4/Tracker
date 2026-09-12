"""Parsing Notion's internal record map, as a published site actually returns it."""

from wallet_monitor import notion_public
from wallet_monitor.config import Config, PublicNotionTable

MARS = PublicNotionTable(name="Project Mars Land", collection="c1", view="v1")
STABLE = PublicNotionTable(name="Scrap wallet stable", collection="c2", view="v2",
                           chain_hint="robinhood")
ARC = PublicNotionTable(name="Arc Smart Money", collection="c3", view="v3")

ADDRESS = "0x1795011ea0d47f3dbd757b77fdaa3f0366208237"


def payload(schema: dict, rows: list[dict]) -> dict:
    """Mirrors the real response: doubly nested values, opaque column keys."""
    blocks = {
        f"b{i}": {"spaceId": "s", "value": {"value": {"id": f"b{i}", "properties": row}}}
        for i, row in enumerate(rows)
    }
    return {
        "recordMap": {
            "block": blocks,
            "collection": {"c1": {"value": {"value": {"schema": schema}}}},
        },
        "result": {"reducerResults": {"collection_group_results": {"blockIds": list(blocks)}}},
    }


MARS_SCHEMA = {
    "BKwH": {"name": "Tag", "type": "select"},
    "E>FC": {"name": "Rank", "type": "number"},
    "]m^l": {"name": "OpenSea", "type": "formula"},
    "zrXe": {"name": "Explorer", "type": "url"},
    "title": {"name": "Wallet", "type": "title"},
}


def test_rows_are_keyed_by_column_name_not_by_notion_key():
    rows = notion_public.rows_from_payload(payload(MARS_SCHEMA, [{
        "BKwH": [["Project Mars Land"]],
        "E>FC": [["43"]],
        "zrXe": [[f"https://robinhoodchain.blockscout.com/address/{ADDRESS}"]],
        "title": [[ADDRESS]],
    }]))
    assert len(rows) == 1
    assert rows[0]["Wallet"] == ADDRESS
    assert rows[0]["Rank"] == "43"
    assert rows[0]["Tag"] == "Project Mars Land"


def test_formula_columns_are_skipped():
    rows = notion_public.rows_from_payload(payload(MARS_SCHEMA, [{
        "title": [[ADDRESS]],
        "]m^l": [["https://opensea.io/" + ADDRESS]],
    }]))
    assert "OpenSea" not in rows[0]


def test_wallet_chain_comes_from_the_explorer_link():
    wallet = notion_public.wallet_from_row(
        {"Wallet": ADDRESS, "Rank": "43", "Tag": "Project Mars Land",
         "Explorer": f"https://robinhoodchain.blockscout.com/address/{ADDRESS}"},
        MARS,
    )
    assert wallet.chain == "robinhood"
    assert wallet.rank == 43
    assert wallet.tag == "Project Mars Land"
    assert wallet.source == "Notion · Project Mars Land"


def test_gmgn_link_resolves_to_arc_and_profit_is_read():
    wallet = notion_public.wallet_from_row(
        {"Wallet": ADDRESS, "Credential": "Top 6 COOL", "Profit USD": "1286",
         "gmgn": f"https://gmgn.ai/arc/address/{ADDRESS}"},
        ARC,
    )
    assert wallet.chain == "arc"
    assert wallet.pnl_usd == 1286
    assert wallet.tag == "Top 6 COOL"


def test_table_without_a_link_column_falls_back_to_its_chain_hint():
    wallet = notion_public.wallet_from_row(
        {"Address": ADDRESS, "Label": "Top 12 REALCOIN", "Tokens": "REALCOIN", "PNL": "7224.07"},
        STABLE,
    )
    assert wallet.chain == "robinhood"
    assert wallet.label == "Top 12 REALCOIN"
    assert wallet.pnl_usd == 7224.07


def test_rows_without_an_address_are_skipped():
    assert notion_public.wallet_from_row({"Wallet": "TBD", "Rank": "1"}, MARS) is None


def test_dollar_signs_and_thousands_separators_survive():
    wallet = notion_public.wallet_from_row(
        {"Address": ADDRESS, "PNL": "$125,000"}, STABLE
    )
    assert wallet.pnl_usd == 125000


def test_sync_reports_a_failing_table_without_losing_the_others(monkeypatch):
    cfg = Config()
    cfg.notion_site = "https://example.notion.site"
    cfg.notion_space_id = "space"
    cfg.notion_tables = [MARS, STABLE]

    def fetch(_cfg, table):
        if table is MARS:
            raise RuntimeError("502 from Notion")
        return [{"Address": ADDRESS, "PNL": "10"}]

    monkeypatch.setattr(notion_public, "fetch_table", fetch)
    wallets, notes = notion_public.sync(cfg)
    assert len(wallets) == 1
    assert any("FAILED" in note for note in notes)
    assert any("1 wallets" in note for note in notes)


def test_sync_without_configuration_says_so():
    try:
        notion_public.sync(Config())
    except RuntimeError as exc:
        assert "config.toml" in str(exc)
    else:
        raise AssertionError("expected a RuntimeError")
