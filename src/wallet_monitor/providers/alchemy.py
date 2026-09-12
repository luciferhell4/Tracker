"""Alchemy provider: NFT mints via alchemy_getAssetTransfers.

A mint is a transfer whose sender is the zero address, so we ask Alchemy for
exactly that and nothing else. This is the primary EVM path because one key
covers every supported network.
"""

from __future__ import annotations

from typing import Any

from .. import chains
from ..config import Config
from ..http import request_json
from ..models import MintEvent
from .base import ProviderUnavailable, hex_to_int, iso_to_unix

CATEGORIES = ["erc721", "erc1155"]
PAGE_SIZE = 100
MAX_TIMESTAMP_LOOKUPS = 40


class AlchemyProvider:
    name = "alchemy"

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.key = cfg.alchemy_key
        self._floor_cache: dict[tuple[str, int], int] = {}

    def supports(self, chain: str) -> bool:
        c = chains.CHAINS.get(chain)
        return bool(self.key and c and c.kind == "evm" and c.alchemy_network)

    def _url(self, chain: str) -> str:
        c = chains.get(chain)
        if not c.alchemy_network:
            raise ProviderUnavailable(f"alchemy has no network slug for {chain}")
        return f"https://{c.alchemy_network}.g.alchemy.com/v2/{self.key}"

    def latest_block(self, chain: str) -> int:
        data = self._rpc(chain, "eth_blockNumber", [])
        return hex_to_int(data)

    def lookback_floor_block(self, chain: str, hours: int) -> int:
        """The block roughly `hours` ago, from the chain's measured block time.

        Alchemy does not honour withMetadata on every network — Ink returns no
        metadata at all — so the window has to be bounded by BLOCK rather than
        filtered on a timestamp afterwards. Without this, the query returns a
        wallet's entire mint history.
        """
        cached = self._floor_cache.get((chain, hours))
        if cached is not None:
            return cached

        head = self.latest_block(chain)
        span = min(5000, head)
        recent = self._rpc(chain, "eth_getBlockByNumber", [hex(head), False]) or {}
        older = self._rpc(chain, "eth_getBlockByNumber", [hex(head - span), False]) or {}
        elapsed = hex_to_int(recent.get("timestamp")) - hex_to_int(older.get("timestamp"))
        seconds_per_block = (elapsed / span) if elapsed > 0 and span > 0 else 12.0

        floor = max(0, head - int(hours * 3600 / seconds_per_block) - 1)
        self._floor_cache[(chain, hours)] = floor
        return floor

    def resolve_missing_timestamps(self, chain: str, events: list[MintEvent]) -> None:
        """Look up block times for mints whose response carried none."""
        missing = list({e.block_number for e in events if not e.timestamp})
        if not missing:
            return
        by_block: dict[int, int] = {}
        for block_number in missing[:MAX_TIMESTAMP_LOOKUPS]:
            try:
                block = self._rpc(chain, "eth_getBlockByNumber", [hex(block_number), False]) or {}
                by_block[block_number] = hex_to_int(block.get("timestamp"))
            except Exception:
                continue  # left at zero; the caller drops what it cannot place
        for event in events:
            if not event.timestamp:
                event.timestamp = by_block.get(event.block_number, 0)

    def _rpc(self, chain: str, method: str, params: list[Any]) -> Any:
        data = request_json(
            self._url(chain),
            method="POST",
            payload={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            timeout=self.cfg.scan.request_timeout,
            retries=self.cfg.scan.max_retries,
        )
        if isinstance(data, dict) and data.get("error"):
            raise ProviderUnavailable(f"alchemy {method} on {chain}: {data['error']}")
        return (data or {}).get("result")

    def fetch_mints(
        self, chain: str, wallet: str, *, since_block: int = 0, lookback_hours: int = 24
    ) -> list[MintEvent]:
        if not self.supports(chain):
            raise ProviderUnavailable(f"alchemy cannot serve {chain}")

        from_block = since_block or self.lookback_floor_block(chain, lookback_hours)

        events: list[MintEvent] = []
        page_key: str | None = None
        while True:
            params: dict[str, Any] = {
                "fromBlock": hex(max(0, from_block)),
                "toBlock": "latest",
                "fromAddress": chains.ZERO_ADDRESS,
                "toAddress": wallet,
                "category": CATEGORIES,
                "withMetadata": True,
                "excludeZeroValue": False,
                "maxCount": hex(PAGE_SIZE),
                "order": "desc",
            }
            if page_key:
                params["pageKey"] = page_key
            result = self._rpc(chain, "alchemy_getAssetTransfers", [params]) or {}
            for transfer in result.get("transfers", []):
                event = self._to_event(chain, wallet, transfer)
                if event:
                    events.append(event)
            page_key = result.get("pageKey")
            if not page_key or len(events) >= PAGE_SIZE * 5:
                break

        # Fill in any timestamp the response omitted. A mint with an unknown
        # time is useless to the scorer, so it is never guessed.
        self.resolve_missing_timestamps(chain, events)
        return events

    @staticmethod
    def _to_event(chain: str, wallet: str, transfer: dict[str, Any]) -> MintEvent | None:
        contract = ((transfer.get("rawContract") or {}).get("address") or "").lower()
        if not contract:
            return None
        category = (transfer.get("category") or "erc721").lower()

        token_id = transfer.get("tokenId")
        quantity = 1
        if erc1155 := transfer.get("erc1155Metadata"):
            token_id = token_id or erc1155[0].get("tokenId")
            quantity = sum(hex_to_int(item.get("value"), 1) for item in erc1155) or 1

        return MintEvent(
            chain=chain,
            wallet=wallet.lower(),
            contract=contract,
            token_id=str(hex_to_int(token_id)) if token_id else "0",
            tx_hash=transfer.get("hash", ""),
            block_number=hex_to_int(transfer.get("blockNum")),
            timestamp=iso_to_unix((transfer.get("metadata") or {}).get("blockTimestamp")),
            token_standard=category,
            collection_name=transfer.get("asset") or "",
            quantity=quantity,
        )
