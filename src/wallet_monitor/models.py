"""Value types shared by the loaders, providers and scorer."""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass
class Wallet:
    """A wallet we watch, as imported from Notion or a CSV."""

    address: str
    chain: str
    label: str = ""
    source: str = ""            # which Notion table / file it came from
    tag: str = ""               # Notion "Tag" / "Credential" / "Tokens"
    rank: float | None = None   # lower is better, when the source ranks wallets
    pnl_usd: float | None = None
    url: str = ""               # explorer or gmgn link from the source row

    def key(self) -> tuple[str, str]:
        return (self.chain, self.address)

    def to_row(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class MintEvent:
    """One tracked wallet acquiring a freshly minted token."""

    chain: str
    wallet: str
    contract: str
    token_id: str
    tx_hash: str
    block_number: int
    timestamp: int              # unix seconds
    token_standard: str = "erc721"
    collection_name: str = ""
    quantity: int = 1

    def uid(self) -> str:
        return f"{self.chain}:{self.tx_hash}:{self.contract}:{self.token_id}:{self.wallet}"


@dataclass
class Signal:
    """A scored cluster of mints into one contract by tracked wallets."""

    chain: str
    contract: str
    collection_name: str
    score: float
    wallet_count: int
    mint_count: int
    first_mint_at: int
    last_mint_at: int
    wallets: list[Wallet] = field(default_factory=list)
    sample_tx: str = ""

    @property
    def window_minutes(self) -> float:
        return (self.last_mint_at - self.first_mint_at) / 60.0
