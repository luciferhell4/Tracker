"""Reading mints straight off a chain's public JSON-RPC.

Chains like Robinhood Chain are served by no commercial provider, so mints are
read from `eth_getLogs` instead. The query is deliberately chain-wide rather
than per wallet: one request covers the whole watchlist on that chain, where
per-wallet filtering would mean hundreds of requests per pass.

Robinhood Chain produces a block every ~0.1s, so a day is ~850k blocks and a
full backfill is out of the question. The scanner is therefore incremental: it
advances a per-chain cursor a bounded number of blocks at a time and catches up
over successive passes.
"""

from __future__ import annotations

import time
from typing import Any, Iterable

from .. import chains
from ..config import Config
from ..http import request_json
from ..models import MintEvent
from .base import ProviderUnavailable

# Transfer(address indexed from, address indexed to, uint256 indexed tokenId)
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
# TransferSingle(address indexed operator, address indexed from,
#                address indexed to, uint256 id, uint256 value)
TRANSFER_SINGLE_TOPIC = "0xc3d58168c5ae7397731d063d5bbf3d657854427343f4c083240f7aacaa2d0f62"
ZERO_TOPIC = "0x" + "0" * 64

# Free public endpoints throttle hard, so requests are spaced and retried.
SLICE_PAUSE = 0.35
RETRY_DELAYS = (0.6, 1.8)
MAX_TIMESTAMP_LOOKUPS = 40

# Some public RPCs sit behind a bot filter that rejects a bare server-side
# request. A conventional browser User-Agent is enough to be served.
BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def _hex_to_int(value: Any, default: int = 0) -> int:
    if isinstance(value, int):
        return value
    if not isinstance(value, str) or not value:
        return default
    try:
        return int(value, 16) if value.startswith("0x") else int(value)
    except ValueError:
        return default


def _topic_to_address(topic: str | None) -> str:
    return ("0x" + topic[-40:]).lower() if topic else ""


def _is_throttled(message: str) -> bool:
    lowered = message.lower()
    return any(
        needle in lowered
        for needle in ("429", "rate limit", "rate-limited", "too many requests", "capacity")
    )


class RpcProvider:
    """Chain-wide mint sweeps over public JSON-RPC."""

    name = "rpc"

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg

    def supports(self, chain: str) -> bool:
        entry = chains.CHAINS.get(chain)
        return bool(entry and entry.rpc_urls)

    def window_for(self, chain: str) -> int:
        entry = chains.CHAINS.get(chain)
        return entry.max_blocks_per_scan if entry else 1000

    # ---------------------------------------------------------------- plumbing

    def call(self, chain: str, method: str, params: list[Any]) -> Any:
        """Try each configured endpoint before giving up on the chain."""
        entry = chains.CHAINS.get(chain)
        if not entry or not entry.rpc_urls:
            raise ProviderUnavailable(f"no RPC endpoint configured for {chain}")

        failures: list[str] = []
        for url in entry.rpc_urls:
            host = url.split("://", 1)[-1].split("/", 1)[0]
            # Throttling is transient, so back off and try the same endpoint
            # again before moving on to the next one.
            for attempt in range(len(RETRY_DELAYS) + 1):
                try:
                    data = request_json(
                        url,
                        method="POST",
                        payload={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                        headers={"User-Agent": BROWSER_UA},
                        timeout=self.cfg.scan.request_timeout,
                        retries=1,
                    )
                    if isinstance(data, dict) and data.get("error"):
                        raise RuntimeError(str(data["error"].get("message", "rpc error")))
                    return (data or {}).get("result")
                except Exception as exc:
                    message = str(exc)
                    if _is_throttled(message) and attempt < len(RETRY_DELAYS):
                        time.sleep(RETRY_DELAYS[attempt])
                        continue
                    failures.append(f"{host}: {message}")
                    break
        # Naming every endpoint that refused makes a dead chain diagnosable.
        raise ProviderUnavailable(f"{chain} RPC failed — {' | '.join(failures)}")

    def head_block(self, chain: str) -> int:
        return _hex_to_int(self.call(chain, "eth_blockNumber", []))

    # ------------------------------------------------------------------- sweep

    def sweep(
        self,
        chain: str,
        from_block: int,
        head_block: int,
        watched: Iterable[str],
    ) -> tuple[list[MintEvent], int]:
        """Every mint on `chain` in the next window that landed in `watched`.

        Returns the events and the block actually reached, which may be short
        of the head when the window was capped.
        """
        watched_set = {address.lower() for address in watched}
        to_block = min(head_block, from_block + self.window_for(chain))
        if to_block <= from_block or not watched_set:
            return ([], max(from_block, to_block))

        entry = chains.get(chain)
        matched: list[tuple[dict[str, Any], str, str]] = []

        start = from_block + 1
        while start <= to_block:
            end = min(start + entry.log_range - 1, to_block)
            block_range = {"fromBlock": hex(start), "toBlock": hex(end)}

            # Both queries pin the sender to the zero address, so only genuine
            # mints come back; secondary transfers never enter the result set.
            # They run one after another rather than together to stay under
            # the endpoints' rate limits.
            erc721 = self.call(
                chain, "eth_getLogs", [{**block_range, "topics": [TRANSFER_TOPIC, ZERO_TOPIC]}]
            ) or []
            time.sleep(SLICE_PAUSE)
            erc1155 = self.call(
                chain,
                "eth_getLogs",
                [{**block_range, "topics": [TRANSFER_SINGLE_TOPIC, None, ZERO_TOPIC]}],
            ) or []
            time.sleep(SLICE_PAUSE)

            for log in erc721:
                # Four topics means the third indexed argument is a tokenId, so
                # this is ERC-721. ERC-20 shares the signature but indexes two.
                topics = log.get("topics") or []
                if len(topics) != 4:
                    continue
                wallet = _topic_to_address(topics[2])
                if wallet in watched_set:
                    matched.append((log, "erc721", wallet))

            for log in erc1155:
                topics = log.get("topics") or []
                if len(topics) != 4:
                    continue
                wallet = _topic_to_address(topics[3])
                if wallet in watched_set:
                    matched.append((log, "erc1155", wallet))

            start = end + 1

        return (self._to_events(chain, matched), to_block)

    def _to_events(
        self, chain: str, matched: list[tuple[dict[str, Any], str, str]]
    ) -> list[MintEvent]:
        # Logs carry no timestamp, so fetch only the blocks that actually
        # matched rather than every block in the range.
        block_numbers = list({_hex_to_int(log.get("blockNumber")) for log, _, _ in matched})
        timestamps: dict[int, int] = {}
        for block_number in block_numbers[:MAX_TIMESTAMP_LOOKUPS]:
            try:
                block = self.call(chain, "eth_getBlockByNumber", [hex(block_number), False])
                timestamps[block_number] = _hex_to_int((block or {}).get("timestamp"))
            except Exception:
                # Left unset; the caller drops mints it cannot place in time.
                continue
            time.sleep(0.12)

        events: list[MintEvent] = []
        for log, standard, wallet in matched:
            block_number = _hex_to_int(log.get("blockNumber"))
            if standard == "erc721":
                token_id = str(_hex_to_int((log.get("topics") or ["", "", "", "0x0"])[3]))
            else:
                data = (log.get("data") or "0x")[2:66] or "0"
                token_id = str(_hex_to_int("0x" + data))
            events.append(
                MintEvent(
                    chain=chain,
                    wallet=wallet,
                    contract=(log.get("address") or "").lower(),
                    token_id=token_id,
                    tx_hash=log.get("transactionHash", ""),
                    block_number=block_number,
                    # Zero means "unknown"; the caller drops those, never guesses.
                    timestamp=timestamps.get(block_number, 0),
                    token_standard=standard,
                )
            )
        return events

    def fetch_mints(
        self, chain: str, wallet: str, *, since_block: int = 0, lookback_hours: int = 24
    ) -> list[MintEvent]:
        """RPC chains are swept chain-wide once per scan, not wallet by wallet."""
        return []
