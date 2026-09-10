"""Pull the watched wallets out of the Notion tables.

Notion databases are the source of truth for who we follow. This module
understands the shapes those five tables actually use ("Wallet"/"Address"
titles, an "Explorer" or "gmgn" link, a "Rank", a "PNL"/"Profit USD", and a
"Tag"/"Credential"/"Tokens" label) without hard-coding column positions.
"""

from __future__ import annotations

import csv
import re
from pathlib import Path
from typing import Any, Iterable

from . import chains
from .config import Config, NotionSource
from .http import HttpError, request_json
from .models import Wallet

NOTION_API = "https://api.notion.com/v1"
LEGACY_VERSION = "2022-06-28"
DATA_SOURCE_VERSION = "2025-09-03"

_ID_RE = re.compile(r"([0-9a-fA-F]{32})")
_DASHED_ID_RE = re.compile(r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})")


def parse_database_id(url: str) -> str:
    """Pull the database id out of a Notion URL (app.notion.com or *.notion.site)."""
    path = url.split("?", 1)[0]
    if m := _DASHED_ID_RE.search(path):
        return m.group(1).replace("-", "")
    matches = _ID_RE.findall(path)
    if not matches:
        raise ValueError(f"no Notion id found in {url!r}")
    return matches[-1]


# --------------------------------------------------------------- properties

def plain_value(prop: dict[str, Any]) -> Any:
    """Flatten one Notion property object into a scalar or string."""
    kind = prop.get("type")
    value = prop.get(kind)
    if kind in ("title", "rich_text"):
        return "".join(part.get("plain_text", "") for part in value or [])
    if kind in ("url", "email", "phone_number"):
        return value or ""
    if kind == "number":
        return value
    if kind == "checkbox":
        return bool(value)
    if kind in ("select", "status"):
        return (value or {}).get("name", "")
    if kind == "multi_select":
        return ", ".join(opt.get("name", "") for opt in value or [])
    if kind == "date":
        return (value or {}).get("start", "")
    if kind == "formula":
        inner = value or {}
        result_type = inner.get("type")
        result = inner.get(result_type)
        if result_type == "date":
            return (result or {}).get("start", "")
        if result_type == "number":
            return result
        return result if isinstance(result, (str, bool)) else ""
    if kind in ("created_time", "last_edited_time"):
        return value or ""
    if kind == "people":
        return ", ".join(p.get("name", "") for p in value or [])
    return ""


def _first_number(props: dict[str, Any], *needles: str) -> float | None:
    for name, prop in props.items():
        if prop.get("type") != "number":
            continue
        lowered = name.lower()
        if any(n in lowered for n in needles):
            v = plain_value(prop)
            if isinstance(v, (int, float)):
                return float(v)
    return None


def _first_link(props: dict[str, Any]) -> str:
    ordered = sorted(props.items(), key=lambda kv: 0 if "explorer" in kv[0].lower() else 1)
    for _, prop in ordered:
        value = plain_value(prop)
        if isinstance(value, str) and value.startswith("http"):
            return value
    return ""


def _labels(props: dict[str, Any]) -> tuple[str, str]:
    """Return (tag, label) from select-ish and text-ish columns."""
    tags: list[str] = []
    labels: list[str] = []
    for name, prop in props.items():
        kind = prop.get("type")
        value = plain_value(prop)
        if not value or not isinstance(value, str):
            continue
        if kind in ("select", "multi_select", "status"):
            tags.append(value)
        elif kind == "rich_text" and name.lower() in ("label", "note", "notes", "name", "description"):
            labels.append(value)
    return (" | ".join(tags), " | ".join(labels))


def wallet_from_page(page: dict[str, Any], source: NotionSource) -> Wallet | None:
    """Turn one Notion row into a Wallet, or None when it holds no address."""
    props: dict[str, Any] = page.get("properties", {})

    address = kind = None
    # Titles first (all five tables put the address in the title column), then
    # any other text column, so an oddly shaped table still works.
    ordered = sorted(props.items(), key=lambda kv: 0 if kv[1].get("type") == "title" else 1)
    for _, prop in ordered:
        if prop.get("type") not in ("title", "rich_text"):
            continue
        parsed = chains.normalise_address(str(plain_value(prop) or ""))
        if parsed:
            address, kind = parsed
            break
    if not address:
        return None

    url = _first_link(props)
    chain = chains.chain_from_url(url) or source.chain_hint
    if not chain:
        chain = "solana" if kind == "svm" else chains.DEFAULT_CHAIN
    if chain not in chains.CHAINS:
        chain = chains.DEFAULT_CHAIN

    tag, label = _labels(props)
    return Wallet(
        address=address,
        chain=chain,
        label=label,
        source=source.name,
        tag=tag,
        rank=_first_number(props, "rank"),
        pnl_usd=_first_number(props, "pnl", "profit", "realized"),
        url=url,
    )


# ------------------------------------------------------------------ fetching

def _headers(token: str, version: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Notion-Version": version}


def _query(url: str, token: str, version: str, cfg: Config) -> list[dict[str, Any]]:
    pages: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        payload: dict[str, Any] = {"page_size": 100}
        if cursor:
            payload["start_cursor"] = cursor
        data = request_json(
            url,
            method="POST",
            payload=payload,
            headers=_headers(token, version),
            timeout=cfg.scan.request_timeout,
            retries=cfg.scan.max_retries,
        )
        pages.extend(data.get("results", []))
        if not data.get("has_more"):
            return pages
        cursor = data.get("next_cursor")


def fetch_pages(database_id: str, token: str, cfg: Config) -> list[dict[str, Any]]:
    """Query a database, transparently handling the data-source API split.

    Notion's 2025-09-03 API moved rows behind data sources. We try the classic
    endpoint first and fall back rather than guessing which workspace is on
    which API version.
    """
    try:
        return _query(f"{NOTION_API}/databases/{database_id}/query", token, LEGACY_VERSION, cfg)
    except HttpError as exc:
        if exc.status not in (400, 404):
            raise

    db = request_json(
        f"{NOTION_API}/databases/{database_id}",
        headers=_headers(token, DATA_SOURCE_VERSION),
        timeout=cfg.scan.request_timeout,
        retries=cfg.scan.max_retries,
    )
    sources = db.get("data_sources") or []
    if not sources:
        raise RuntimeError(f"database {database_id} exposed no data sources")

    pages: list[dict[str, Any]] = []
    for src in sources:
        pages.extend(
            _query(f"{NOTION_API}/data_sources/{src['id']}/query", token, DATA_SOURCE_VERSION, cfg)
        )
    return pages


def sync(cfg: Config) -> tuple[list[Wallet], list[str]]:
    """Read every configured Notion table. Returns (wallets, per-source notes)."""
    token = cfg.notion_token
    if not token:
        raise RuntimeError(
            "NOTION_TOKEN is not set. Create an internal integration at "
            "https://www.notion.so/my-integrations, share each wallet database with it, "
            "then export NOTION_TOKEN=ntn_..."
        )

    wallets: list[Wallet] = []
    notes: list[str] = []
    for source in cfg.notion_sources:
        if not source.enabled:
            continue
        db_id = parse_database_id(source.url)
        try:
            pages = fetch_pages(db_id, token, cfg)
        except Exception as exc:  # one broken table must not sink the rest
            notes.append(f"{source.name}: FAILED ({exc})")
            continue
        found = [w for w in (wallet_from_page(p, source) for p in pages) if w]
        wallets.extend(found)
        notes.append(f"{source.name}: {len(found)} wallets from {len(pages)} rows")
    return dedupe(wallets), notes


def dedupe(wallets: Iterable[Wallet]) -> list[Wallet]:
    """Collapse duplicates, keeping the richest record for each address."""
    best: dict[tuple[str, str], Wallet] = {}
    for w in wallets:
        existing = best.get(w.key())
        if existing is None:
            best[w.key()] = w
            continue
        if existing.source != w.source and w.source not in existing.source:
            existing.source = f"{existing.source}, {w.source}"
        if w.tag and w.tag not in existing.tag:
            existing.tag = f"{existing.tag} | {w.tag}".strip(" |")
        if existing.rank is None or (w.rank is not None and w.rank < existing.rank):
            existing.rank = w.rank if w.rank is not None else existing.rank
        if w.pnl_usd is not None:
            existing.pnl_usd = max(existing.pnl_usd or 0.0, w.pnl_usd)
        existing.url = existing.url or w.url
        existing.label = existing.label or w.label
    return list(best.values())


# ------------------------------------------------------------- CSV fallback

CSV_FIELDS = ["address", "chain", "label", "source", "tag", "rank", "pnl_usd", "url"]


def write_csv(wallets: Iterable[Wallet], path: str | Path) -> int:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = list(wallets)
    with path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for w in rows:
            writer.writerow({k: ("" if v is None else v) for k, v in w.to_row().items()})
    return len(rows)


def read_csv(path: str | Path, source: str = "csv") -> list[Wallet]:
    """Load wallets from a CSV. Only `address` is required; a chain is inferred
    from the explorer link or the address shape when the column is missing."""
    path = Path(path)
    if not path.exists():
        return []
    wallets: list[Wallet] = []
    with path.open(newline="") as fh:
        for row in csv.DictReader(fh):
            raw = (row.get("address") or row.get("wallet") or row.get("Address") or "").strip()
            parsed = chains.normalise_address(raw)
            if not parsed:
                continue
            address, kind = parsed
            url = (row.get("url") or row.get("explorer") or "").strip()
            chain = (row.get("chain") or "").strip().lower() or chains.chain_from_url(url) or ""
            if chain not in chains.CHAINS:
                chain = "solana" if kind == "svm" else chains.DEFAULT_CHAIN

            def num(key: str) -> float | None:
                value = (row.get(key) or "").strip().replace("$", "").replace(",", "")
                try:
                    return float(value)
                except ValueError:
                    return None

            wallets.append(
                Wallet(
                    address=address,
                    chain=chain,
                    label=(row.get("label") or "").strip(),
                    source=(row.get("source") or source).strip(),
                    tag=(row.get("tag") or "").strip(),
                    rank=num("rank"),
                    pnl_usd=num("pnl_usd") or num("pnl"),
                    url=url,
                )
            )
    return dedupe(wallets)
