"""Configuration loading.

Config lives in a TOML file; anything secret comes from the environment so the
file itself is safe to commit. Environment variables always win.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_PATH = "config.toml"


@dataclass
class PublicNotionTable:
    """One table on a published Notion site, readable without a token."""

    name: str
    collection: str
    view: str
    chain_hint: str = ""


@dataclass
class NotionSource:
    name: str
    url: str
    enabled: bool = True
    chain_hint: str = ""     # used when a row has no explorer link to infer from


@dataclass
class SignalRules:
    min_wallets: int = 2          # distinct tracked wallets in one contract
    window_minutes: int = 180     # they must land within this window
    min_score: float = 0.0
    max_contract_age_hours: float = 72.0   # ignore contracts we first saw long ago
    top_n: int = 25


@dataclass
class ScanSettings:
    lookback_hours: int = 24
    chains: list[str] = field(default_factory=list)   # empty = every chain we have wallets on
    max_wallets_per_chain: int = 0                    # 0 = no cap
    request_timeout: int = 30
    max_retries: int = 3
    interval_seconds: int = 300                       # for `watch`


@dataclass
class Config:
    store_path: str = "data/tracker.sqlite"
    wallets_csv: str = "data/wallets.csv"
    notion_sources: list[NotionSource] = field(default_factory=list)
    notion_site: str = ""
    notion_space_id: str = ""
    notion_tables: list[PublicNotionTable] = field(default_factory=list)
    scan: ScanSettings = field(default_factory=ScanSettings)
    signal: SignalRules = field(default_factory=SignalRules)
    discord_webhook: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # --- credentials, environment only -----------------------------------
    @property
    def notion_token(self) -> str:
        return os.environ.get("NOTION_TOKEN", "")

    @property
    def alchemy_key(self) -> str:
        return os.environ.get("ALCHEMY_API_KEY", "")

    @property
    def etherscan_key(self) -> str:
        return os.environ.get("ETHERSCAN_API_KEY", "")

    @property
    def helius_key(self) -> str:
        return os.environ.get("HELIUS_API_KEY", "")

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Config":
        path = Path(path or os.environ.get("TRACKER_CONFIG", DEFAULT_CONFIG_PATH))
        raw: dict[str, Any] = {}
        if path.exists():
            raw = tomllib.loads(path.read_text())

        cfg = cls()
        store = raw.get("store", {})
        cfg.store_path = store.get("path", cfg.store_path)
        cfg.wallets_csv = store.get("wallets_csv", cfg.wallets_csv)

        cfg.notion_sources = [
            NotionSource(
                name=s.get("name", s.get("url", "")),
                url=s.get("url", ""),
                enabled=s.get("enabled", True),
                chain_hint=s.get("chain_hint", ""),
            )
            for s in raw.get("notion", {}).get("databases", [])
            if s.get("url")
        ]

        public = raw.get("notion", {}).get("public", {})
        cfg.notion_site = public.get("site", "")
        cfg.notion_space_id = public.get("space_id", "")
        cfg.notion_tables = [
            PublicNotionTable(
                name=t.get("name", t.get("collection", "")),
                collection=t.get("collection", ""),
                view=t.get("view", ""),
                chain_hint=t.get("chain_hint", ""),
            )
            for t in public.get("tables", [])
            if t.get("collection") and t.get("view")
        ]

        scan = raw.get("scan", {})
        cfg.scan = ScanSettings(
            lookback_hours=scan.get("lookback_hours", 24),
            chains=list(scan.get("chains", [])),
            max_wallets_per_chain=scan.get("max_wallets_per_chain", 0),
            request_timeout=scan.get("request_timeout", 30),
            max_retries=scan.get("max_retries", 3),
            interval_seconds=scan.get("interval_seconds", 300),
        )

        sig = raw.get("signal", {})
        cfg.signal = SignalRules(
            min_wallets=sig.get("min_wallets", 2),
            window_minutes=sig.get("window_minutes", 180),
            min_score=sig.get("min_score", 0.0),
            max_contract_age_hours=sig.get("max_contract_age_hours", 72.0),
            top_n=sig.get("top_n", 25),
        )

        notify = raw.get("notify", {})
        cfg.discord_webhook = os.environ.get("DISCORD_WEBHOOK_URL", notify.get("discord_webhook", ""))
        cfg.telegram_bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", notify.get("telegram_bot_token", ""))
        cfg.telegram_chat_id = os.environ.get("TELEGRAM_CHAT_ID", notify.get("telegram_chat_id", ""))

        # Environment overrides for the knobs people tune most often.
        if v := os.environ.get("TRACKER_DB"):
            cfg.store_path = v
        if v := os.environ.get("TRACKER_MIN_WALLETS"):
            cfg.signal.min_wallets = int(v)
        if v := os.environ.get("TRACKER_WINDOW_MINUTES"):
            cfg.signal.window_minutes = int(v)
        if v := os.environ.get("TRACKER_LOOKBACK_HOURS"):
            cfg.scan.lookback_hours = int(v)
        return cfg
