"""Helius provider: Solana mints and first buys.

On Solana the "early mint" that matters is usually an NFT mint or the first
swap into a brand-new token, so both are surfaced. Swaps carry the token
standard `spl-buy` so the scorer and the report can tell them apart.
"""

from __future__ import annotations

from typing import Any

from ..config import Config
from ..http import request_json
from ..models import MintEvent
from .base import ProviderUnavailable

API = "https://api.helius.xyz/v0"
MINT_TYPES = {"NFT_MINT", "COMPRESSED_NFT_MINT", "TOKEN_MINT"}
BUY_TYPES = {"SWAP"}
WSOL = "So11111111111111111111111111111111111111112"
STABLES = {
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
}


class HeliusProvider:
    name = "helius"

    def __init__(self, cfg: Config, include_swaps: bool = True) -> None:
        self.cfg = cfg
        self.key = cfg.helius_key
        self.include_swaps = include_swaps

    def supports(self, chain: str) -> bool:
        return bool(self.key and chain == "solana")

    def fetch_mints(
        self, chain: str, wallet: str, *, since_block: int = 0, lookback_hours: int = 24
    ) -> list[MintEvent]:
        if not self.supports(chain):
            raise ProviderUnavailable("helius only serves solana and needs HELIUS_API_KEY")

        txs = request_json(
            f"{API}/addresses/{wallet}/transactions",
            params={"api-key": self.key, "limit": 100},
            timeout=self.cfg.scan.request_timeout,
            retries=self.cfg.scan.max_retries,
        )
        if not isinstance(txs, list):
            return []

        events: list[MintEvent] = []
        for tx in txs:
            timestamp = int(tx.get("timestamp") or 0)
            # Solana wallets are cursored on unix time rather than slot, so
            # `since_block` carries the last timestamp we already ingested.
            if since_block and timestamp and timestamp <= since_block:
                continue
            kind = tx.get("type") or ""
            if kind in MINT_TYPES:
                events.extend(self._from_mint(wallet, tx))
            elif self.include_swaps and kind in BUY_TYPES:
                events.extend(self._from_swap(wallet, tx))
        return events

    def _from_mint(self, wallet: str, tx: dict[str, Any]) -> list[MintEvent]:
        out: list[MintEvent] = []
        for transfer in tx.get("tokenTransfers") or []:
            if transfer.get("toUserAccount") != wallet:
                continue
            mint = transfer.get("mint") or ""
            if not mint:
                continue
            out.append(
                MintEvent(
                    chain="solana",
                    wallet=wallet,
                    contract=mint,
                    token_id=mint,
                    tx_hash=tx.get("signature", ""),
                    block_number=int(tx.get("slot") or 0),
                    timestamp=int(tx.get("timestamp") or 0),
                    token_standard="spl-mint",
                    collection_name=(tx.get("description") or "")[:120],
                    quantity=int(transfer.get("tokenAmount") or 1) or 1,
                )
            )
        return out

    def _from_swap(self, wallet: str, tx: dict[str, Any]) -> list[MintEvent]:
        """Record the token a wallet swapped *into*, ignoring SOL and stables."""
        out: list[MintEvent] = []
        for transfer in tx.get("tokenTransfers") or []:
            mint = transfer.get("mint") or ""
            if transfer.get("toUserAccount") != wallet or not mint:
                continue
            if mint == WSOL or mint in STABLES:
                continue
            out.append(
                MintEvent(
                    chain="solana",
                    wallet=wallet,
                    contract=mint,
                    token_id=mint,
                    tx_hash=tx.get("signature", ""),
                    block_number=int(tx.get("slot") or 0),
                    timestamp=int(tx.get("timestamp") or 0),
                    token_standard="spl-buy",
                    collection_name=(tx.get("description") or "")[:120],
                    quantity=1,
                )
            )
        return out
