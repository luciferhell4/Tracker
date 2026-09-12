"""Reading the watchlists straight off a public Notion site.

The wallet tables are published as a Notion Site, and a published site answers
the same `queryCollection` endpoint the Notion web app uses — with no
integration token, no sharing step, and no workspace membership. That is the
whole import path: `sync` needs no credentials at all.

Notion's response is its internal record map rather than the tidy public API
shape, so the column keys are opaque ("E>FC") and the schema has to be read
alongside the rows to learn what each one means.
"""

from __future__ import annotations

from typing import Any

from . import chains
from .config import Config, PublicNotionTable
from .http import request_json
from .models import Wallet
from .notion_sync import dedupe

ROW_LIMIT = 500


def _plain(value: Any) -> str:
    """Flatten Notion's [[text, [annotations]], ...] into a string."""
    if not isinstance(value, list):
        return ""
    parts = []
    for segment in value:
        if isinstance(segment, list) and segment:
            parts.append(str(segment[0]))
        elif isinstance(segment, str):
            parts.append(segment)
    return "".join(parts)


def _unwrap(record: Any) -> dict[str, Any]:
    """Record map entries are sometimes {"value": {...}} and sometimes nested."""
    if not isinstance(record, dict):
        return {}
    value = record.get("value", record)
    if isinstance(value, dict) and isinstance(value.get("value"), dict):
        value = value["value"]
    return value if isinstance(value, dict) else {}


def _number(text: str) -> float | None:
    cleaned = text.replace("$", "").replace(",", "").strip()
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def rows_from_payload(payload: dict[str, Any]) -> list[dict[str, str]]:
    """Turn one queryCollection response into rows keyed by column name."""
    record_map = payload.get("recordMap") or {}
    blocks = record_map.get("block") or {}

    schema: dict[str, Any] = {}
    for collection in (record_map.get("collection") or {}).values():
        schema = _unwrap(collection).get("schema") or {}
        if schema:
            break

    by_name = {
        definition.get("name", key): (key, definition.get("type", ""))
        for key, definition in schema.items()
    }

    reducers = (payload.get("result") or {}).get("reducerResults") or {}
    block_ids = (reducers.get("collection_group_results") or {}).get("blockIds") or []

    rows: list[dict[str, str]] = []
    for block_id in block_ids:
        properties = _unwrap(blocks.get(block_id)).get("properties") or {}
        row = {}
        for name, (key, kind) in by_name.items():
            if kind == "formula":
                continue  # derived links add nothing the other columns lack
            row[name] = _plain(properties.get(key))
        rows.append(row)
    return rows


def wallet_from_row(row: dict[str, str], table: PublicNotionTable) -> Wallet | None:
    """One row of any of the five tables, whatever its column names are."""
    raw = row.get("Wallet") or row.get("Address") or ""
    parsed = chains.normalise_address(raw)
    if not parsed:
        return None
    address, kind = parsed

    url = (row.get("Explorer") or row.get("gmgn") or "").strip()
    chain = chains.chain_from_url(url) or table.chain_hint
    if not chain:
        chain = "solana" if kind == "svm" else chains.DEFAULT_CHAIN
    if chain not in chains.CHAINS:
        chain = chains.DEFAULT_CHAIN

    tag = row.get("Tag") or row.get("Credential") or row.get("Tokens") or ""
    return Wallet(
        address=address,
        chain=chain,
        label=(row.get("Label") or "").strip(),
        source=f"Notion · {table.name}",
        tag=tag.strip(),
        rank=_number(row.get("Rank", "")),
        pnl_usd=_number(row.get("Profit USD", "") or row.get("PNL", "")),
        url=url,
    )


def fetch_table(cfg: Config, table: PublicNotionTable) -> list[dict[str, str]]:
    payload = request_json(
        f"{cfg.notion_site.rstrip('/')}/api/v3/queryCollection",
        method="POST",
        payload={
            "source": {"type": "collection", "id": table.collection, "spaceId": cfg.notion_space_id},
            "collectionView": {"id": table.view, "spaceId": cfg.notion_space_id},
            "loader": {
                "type": "reducer",
                "reducers": {
                    "collection_group_results": {"type": "results", "limit": ROW_LIMIT}
                },
                "searchQuery": "",
                "userTimeZone": "UTC",
            },
        },
        timeout=cfg.scan.request_timeout,
        retries=cfg.scan.max_retries,
    )
    return rows_from_payload(payload or {})


def sync(cfg: Config) -> tuple[list[Wallet], list[str]]:
    """Read every configured public table. Returns (wallets, per-table notes)."""
    if not cfg.notion_site or not cfg.notion_space_id or not cfg.notion_tables:
        raise RuntimeError(
            "No public Notion tables configured. Fill in [notion.public] in config.toml."
        )

    wallets: list[Wallet] = []
    notes: list[str] = []
    for table in cfg.notion_tables:
        try:
            rows = fetch_table(cfg, table)
        except Exception as exc:  # one broken table must not sink the rest
            notes.append(f"{table.name}: FAILED ({exc})")
            continue
        found = [w for w in (wallet_from_row(row, table) for row in rows) if w]
        wallets.extend(found)
        notes.append(f"{table.name}: {len(found)} wallets from {len(rows)} rows")
    return dedupe(wallets), notes
