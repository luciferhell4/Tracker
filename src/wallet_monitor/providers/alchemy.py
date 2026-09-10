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


class AlchemyProvider:
    name = "alchemy"

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.key = cfg.alchemy_key

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

        events: list[MintEvent] = []
        page_key: str | None = None
        while True:
            params: dict[str, Any] = {
                "fromBlock": hex(since_block) if since_block else "0x0",
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
