"""Etherscan V2 provider: the EVM fallback when there is no Alchemy key.

One Etherscan key covers every supported chain through the `chainid` query
parameter, which is why this doubles as the path for networks Alchemy does
not serve (HyperEVM, for example).
"""

from __future__ import annotations

import time
from typing import Any

from .. import chains
from ..config import Config
from ..http import request_json
from ..models import MintEvent
from .base import ProviderUnavailable, hex_to_int

API = "https://api.etherscan.io/v2/api"
ACTIONS = {"erc721": "tokennfttx", "erc1155": "token1155tx"}


class EtherscanProvider:
    name = "etherscan"

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.key = cfg.etherscan_key
        self._last_call = 0.0

    def supports(self, chain: str) -> bool:
        c = chains.CHAINS.get(chain)
        return bool(self.key and c and c.kind == "evm" and c.chain_id and c.etherscan)

    def _throttle(self) -> None:
        # The free tier allows 5 calls/second; stay comfortably under it.
        elapsed = time.monotonic() - self._last_call
        if elapsed < 0.25:
            time.sleep(0.25 - elapsed)
        self._last_call = time.monotonic()

    def fetch_mints(
        self, chain: str, wallet: str, *, since_block: int = 0, lookback_hours: int = 24
    ) -> list[MintEvent]:
        if not self.supports(chain):
            raise ProviderUnavailable(f"etherscan cannot serve {chain}")
        chain_id = chains.get(chain).chain_id

        events: list[MintEvent] = []
        for standard, action in ACTIONS.items():
            self._throttle()
            data = request_json(
                API,
                params={
                    "chainid": chain_id,
                    "module": "account",
                    "action": action,
                    "address": wallet,
                    "startblock": since_block or 0,
                    "endblock": 99999999,
                    "page": 1,
                    "offset": 200,
                    "sort": "desc",
                    "apikey": self.key,
                },
                timeout=self.cfg.scan.request_timeout,
                retries=self.cfg.scan.max_retries,
            )
            events.extend(self._parse(chain, wallet, standard, data))
        return events

    @staticmethod
    def _parse(chain: str, wallet: str, standard: str, data: Any) -> list[MintEvent]:
        if not isinstance(data, dict):
            return []
        result = data.get("result")
        # Etherscan answers 200 even for a rejected key or an unsupported
        # chain, putting the reason in `result` as a string. An empty wallet
        # says so in `message`; anything else there is a real failure and must
        # not pass silently as "no mints".
        if not isinstance(result, list):
            reason = result if isinstance(result, str) else str(data.get("message", ""))
            if "no transactions found" in reason.lower() or "no records found" in reason.lower():
                return []
            raise ProviderUnavailable(f"etherscan: {reason or 'unexpected response'}")

        target = wallet.lower()
        out: list[MintEvent] = []
        for row in result:
            if (row.get("from") or "").lower() != chains.ZERO_ADDRESS:
                continue
            if (row.get("to") or "").lower() != target:
                continue
            out.append(
                MintEvent(
                    chain=chain,
                    wallet=target,
                    contract=(row.get("contractAddress") or "").lower(),
                    token_id=str(row.get("tokenID") or row.get("tokenId") or "0"),
                    tx_hash=row.get("hash", ""),
                    block_number=hex_to_int(row.get("blockNumber")),
                    timestamp=hex_to_int(row.get("timeStamp")),
                    token_standard=standard,
                    collection_name=row.get("tokenName") or "",
                    quantity=hex_to_int(row.get("tokenValue"), 1) or 1,
                )
            )
        return out
