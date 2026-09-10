"""SQLite persistence: the wallet list, seen mints, and per-wallet cursors.

The store is deliberately small. It exists so that repeated scans are
incremental and so a signal is only alerted on once.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Iterable, Iterator

from .models import MintEvent, Wallet

SCHEMA = """
CREATE TABLE IF NOT EXISTS wallets (
    address    TEXT NOT NULL,
    chain      TEXT NOT NULL,
    label      TEXT DEFAULT '',
    source     TEXT DEFAULT '',
    tag        TEXT DEFAULT '',
    rank       REAL,
    pnl_usd    REAL,
    url        TEXT DEFAULT '',
    added_at   INTEGER NOT NULL,
    PRIMARY KEY (chain, address)
);

CREATE TABLE IF NOT EXISTS mints (
    uid             TEXT PRIMARY KEY,
    chain           TEXT NOT NULL,
    wallet          TEXT NOT NULL,
    contract        TEXT NOT NULL,
    token_id        TEXT NOT NULL,
    tx_hash         TEXT NOT NULL,
    block_number    INTEGER NOT NULL,
    timestamp       INTEGER NOT NULL,
    token_standard  TEXT DEFAULT 'erc721',
    collection_name TEXT DEFAULT '',
    quantity        INTEGER DEFAULT 1
);
CREATE INDEX IF NOT EXISTS mints_by_contract ON mints (chain, contract, timestamp);
CREATE INDEX IF NOT EXISTS mints_by_time ON mints (timestamp);

CREATE TABLE IF NOT EXISTS cursors (
    chain      TEXT NOT NULL,
    wallet     TEXT NOT NULL,
    last_block INTEGER NOT NULL DEFAULT 0,
    checked_at INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (chain, wallet)
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS alerted (
    chain        TEXT NOT NULL,
    contract     TEXT NOT NULL,
    wallet_count INTEGER NOT NULL,
    alerted_at   INTEGER NOT NULL,
    PRIMARY KEY (chain, contract)
);
"""


class Store:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---------------------------------------------------------------- wallets

    def upsert_wallets(self, wallets: Iterable[Wallet]) -> int:
        now = int(time.time())
        rows = [
            (w.address, w.chain, w.label, w.source, w.tag, w.rank, w.pnl_usd, w.url, now)
            for w in wallets
        ]
        if not rows:
            return 0
        self.conn.executemany(
            """
            INSERT INTO wallets (address, chain, label, source, tag, rank, pnl_usd, url, added_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (chain, address) DO UPDATE SET
                label   = excluded.label,
                source  = excluded.source,
                tag     = excluded.tag,
                rank    = COALESCE(excluded.rank, wallets.rank),
                pnl_usd = COALESCE(excluded.pnl_usd, wallets.pnl_usd),
                url     = excluded.url
            """,
            rows,
        )
        self.conn.commit()
        return len(rows)

    def wallets(self, chain: str | None = None) -> list[Wallet]:
        sql = "SELECT * FROM wallets"
        args: tuple[object, ...] = ()
        if chain:
            sql += " WHERE chain = ?"
            args = (chain,)
        sql += " ORDER BY chain, COALESCE(rank, 1e9), address"
        return [
            Wallet(
                address=r["address"],
                chain=r["chain"],
                label=r["label"],
                source=r["source"],
                tag=r["tag"],
                rank=r["rank"],
                pnl_usd=r["pnl_usd"],
                url=r["url"],
            )
            for r in self.conn.execute(sql, args)
        ]

    def delete_wallet(self, chain: str, address: str) -> bool:
        cur = self.conn.execute(
            "DELETE FROM wallets WHERE chain = ? AND address = ?", (chain, address)
        )
        self.conn.commit()
        return bool(cur.rowcount)

    def wallet_chains(self) -> list[str]:
        return [r[0] for r in self.conn.execute("SELECT DISTINCT chain FROM wallets ORDER BY chain")]

    # ------------------------------------------------------------------ mints

    def record_mints(self, events: Iterable[MintEvent]) -> list[MintEvent]:
        """Insert events, returning only the ones we had not seen before."""
        fresh: list[MintEvent] = []
        for e in events:
            cur = self.conn.execute(
                """
                INSERT OR IGNORE INTO mints
                    (uid, chain, wallet, contract, token_id, tx_hash, block_number,
                     timestamp, token_standard, collection_name, quantity)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    e.uid(), e.chain, e.wallet, e.contract, e.token_id, e.tx_hash,
                    e.block_number, e.timestamp, e.token_standard, e.collection_name,
                    e.quantity,
                ),
            )
            if cur.rowcount:
                fresh.append(e)
        self.conn.commit()
        return fresh

    def mints_since(self, since_ts: int) -> list[MintEvent]:
        rows = self.conn.execute(
            "SELECT * FROM mints WHERE timestamp >= ? ORDER BY timestamp", (since_ts,)
        )
        return [
            MintEvent(
                chain=r["chain"], wallet=r["wallet"], contract=r["contract"],
                token_id=r["token_id"], tx_hash=r["tx_hash"], block_number=r["block_number"],
                timestamp=r["timestamp"], token_standard=r["token_standard"],
                collection_name=r["collection_name"], quantity=r["quantity"],
            )
            for r in rows
        ]

    def contract_first_seen(self, chain: str, contract: str) -> int | None:
        row = self.conn.execute(
            "SELECT MIN(timestamp) FROM mints WHERE chain = ? AND contract = ?",
            (chain, contract),
        ).fetchone()
        return row[0] if row and row[0] is not None else None

    # ---------------------------------------------------------------- cursors

    def cursor(self, chain: str, wallet: str) -> int:
        row = self.conn.execute(
            "SELECT last_block FROM cursors WHERE chain = ? AND wallet = ?", (chain, wallet)
        ).fetchone()
        return int(row[0]) if row else 0

    def set_cursor(self, chain: str, wallet: str, last_block: int) -> None:
        self.conn.execute(
            """
            INSERT INTO cursors (chain, wallet, last_block, checked_at) VALUES (?, ?, ?, ?)
            ON CONFLICT (chain, wallet) DO UPDATE SET
                last_block = MAX(excluded.last_block, cursors.last_block),
                checked_at = excluded.checked_at
            """,
            (chain, wallet, last_block, int(time.time())),
        )
        self.conn.commit()

    # ---------------------------------------------------------------- alerting

    def should_alert(self, chain: str, contract: str, wallet_count: int) -> bool:
        """True the first time a contract clears the bar, and again only when
        strictly more tracked wallets have piled in since the last alert."""
        row = self.conn.execute(
            "SELECT wallet_count FROM alerted WHERE chain = ? AND contract = ?",
            (chain, contract),
        ).fetchone()
        return row is None or wallet_count > int(row[0])

    def mark_alerted(self, chain: str, contract: str, wallet_count: int) -> None:
        self.conn.execute(
            """
            INSERT INTO alerted (chain, contract, wallet_count, alerted_at) VALUES (?, ?, ?, ?)
            ON CONFLICT (chain, contract) DO UPDATE SET
                wallet_count = excluded.wallet_count,
                alerted_at   = excluded.alerted_at
            """,
            (chain, contract, wallet_count, int(time.time())),
        )
        self.conn.commit()

    # ------------------------------------------------------------------- meta

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self.conn.commit()

    def get_meta(self, key: str, default: str = "") -> str:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default

    def mints_per_hour(self, since_ts: int) -> list[tuple[int, int]]:
        """(hour-bucket unix ts, mint count), oldest first."""
        rows = self.conn.execute(
            """
            SELECT (timestamp / 3600) * 3600 AS bucket, COUNT(*)
            FROM mints WHERE timestamp >= ?
            GROUP BY bucket ORDER BY bucket
            """,
            (since_ts,),
        )
        return [(int(r[0]), int(r[1])) for r in rows]

    def stats(self) -> dict[str, int]:
        def one(sql: str) -> int:
            return int(self.conn.execute(sql).fetchone()[0])

        return {
            "wallets": one("SELECT COUNT(*) FROM wallets"),
            "mints": one("SELECT COUNT(*) FROM mints"),
            "contracts": one("SELECT COUNT(DISTINCT chain || contract) FROM mints"),
            "alerted": one("SELECT COUNT(*) FROM alerted"),
        }

    def iter_wallets_by_chain(self) -> Iterator[tuple[str, list[Wallet]]]:
        for chain in self.wallet_chains():
            yield chain, self.wallets(chain)
