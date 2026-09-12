"""Chain registry and address/URL heuristics.

Everything the monitor knows about a chain lives here: how to talk to it
through a provider, how to build explorer links, and how to recognise it from
the explorer URLs that the Notion tables carry.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Chain:
    key: str
    name: str
    chain_id: int | None       # EVM chain id, None for non-EVM
    alchemy_network: str | None
    explorer: str              # base URL, no trailing slash
    kind: str = "evm"          # "evm" or "svm"

    # Public JSON-RPC endpoints, for chains no commercial provider serves.
    rpc_urls: tuple[str, ...] = ()
    # Widest block span this chain's RPC accepts for eth_getLogs.
    log_range: int = 2000
    # Blocks advanced per sweep. Sized in chain TIME, not blocks: Robinhood
    # produces a block every ~0.1s and HyperEVM every ~1s, so equal block
    # counts would mean wildly different windows.
    max_blocks_per_scan: int = 1000
    # False for chains Etherscan V2 does not index, so it is never asked.
    etherscan: bool = True

    def address_url(self, address: str) -> str:
        if self.kind == "svm":
            return f"{self.explorer}/account/{address}"
        return f"{self.explorer}/address/{address}"

    def token_url(self, contract: str) -> str:
        if self.kind == "svm":
            return f"{self.explorer}/token/{contract}"
        return f"{self.explorer}/token/{contract}"

    def tx_url(self, tx_hash: str) -> str:
        if self.kind == "svm":
            return f"{self.explorer}/tx/{tx_hash}"
        return f"{self.explorer}/tx/{tx_hash}"


CHAINS: dict[str, Chain] = {
    c.key: c
    for c in [
        Chain("ethereum", "Ethereum", 1, "eth-mainnet", "https://etherscan.io"),
        Chain("base", "Base", 8453, "base-mainnet", "https://basescan.org"),
        Chain("arbitrum", "Arbitrum One", 42161, "arb-mainnet", "https://arbiscan.io"),
        Chain("optimism", "OP Mainnet", 10, "opt-mainnet", "https://optimistic.etherscan.io"),
        Chain("polygon", "Polygon", 137, "polygon-mainnet", "https://polygonscan.com"),
        Chain("abstract", "Abstract", 2741, "abstract-mainnet", "https://abscan.org"),
        Chain("shape", "Shape", 360, "shape-mainnet", "https://shapescan.xyz"),
        Chain("zora", "Zora", 7777777, "zora-mainnet", "https://explorer.zora.energy"),
        Chain("ink", "Ink", 57073, "ink-mainnet", "https://explorer.inkonchain.com",
              rpc_urls=("https://rpc-gel.inkonchain.com", "https://rpc-qnd.inkonchain.com"),
              max_blocks_per_scan=600),
        Chain("berachain", "Berachain", 80094, "berachain-mainnet", "https://berascan.com"),
        Chain("apechain", "ApeChain", 33139, "apechain-mainnet", "https://apescan.io"),
        Chain("blast", "Blast", 81457, "blast-mainnet", "https://blastscan.io"),
        Chain("hyperevm", "HyperEVM", 999, None, "https://hyperevmscan.io",
              rpc_urls=("https://rpc.hyperliquid.xyz/evm",),
              log_range=1000, max_blocks_per_scan=500),
        # The two chains the Notion watchlists actually live on. Neither is
        # served by Alchemy or Etherscan V2.
        Chain("robinhood", "Robinhood Chain", 4663, None,
              "https://robinhoodchain.blockscout.com",
              rpc_urls=("https://rpc.mainnet.chain.robinhood.com",
                        "https://robinhood-rpc.publicnode.com"),
              log_range=1000, max_blocks_per_scan=3000, etherscan=False),
        Chain("arc", "Arc", 5042, None, "https://gmgn.ai/arc", etherscan=False),
        Chain("solana", "Solana", None, "solana-mainnet", "https://solscan.io", kind="svm"),
    ]
}

DEFAULT_CHAIN = "ethereum"

# Explorer host fragment -> chain key. Longest match wins, so the
# optimistic.etherscan.io entry has to be checked before plain etherscan.io.
_HOST_HINTS: list[tuple[str, str]] = [
    ("optimistic.etherscan.io", "optimism"),
    ("etherscan.io", "ethereum"),
    ("basescan.org", "base"),
    ("arbiscan.io", "arbitrum"),
    ("polygonscan.com", "polygon"),
    ("abscan.org", "abstract"),
    ("abstract.blockscout.com", "abstract"),
    ("shapescan.xyz", "shape"),
    ("explorer.shape.network", "shape"),
    ("explorer.zora.energy", "zora"),
    ("explorer.inkonchain.com", "ink"),
    ("inkscan", "ink"),
    ("berascan.com", "berachain"),
    ("beratrail.io", "berachain"),
    ("apescan.io", "apechain"),
    ("blastscan.io", "blast"),
    ("hyperevmscan.io", "hyperevm"),
    ("purrsec.com", "hyperevm"),
    ("hypurrscan.io", "hyperevm"),
    ("robinhoodchain.blockscout.com", "robinhood"),
    ("gmgn.ai/arc", "arc"),
    ("solscan.io", "solana"),
    ("solana.fm", "solana"),
    ("xray.helius.xyz", "solana"),
    ("gmgn.ai/sol", "solana"),
    ("gmgn.ai/eth", "ethereum"),
    ("gmgn.ai/base", "base"),
]

EVM_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
BASE58_ADDRESS_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"


def get(key: str) -> Chain:
    return CHAINS[key]


def evm_chains() -> list[Chain]:
    return [c for c in CHAINS.values() if c.kind == "evm"]


def chain_from_url(url: str | None) -> str | None:
    """Best-effort chain key from an explorer or gmgn link."""
    if not url:
        return None
    lowered = url.lower()
    best: tuple[int, str] | None = None
    for fragment, key in _HOST_HINTS:
        if fragment in lowered and (best is None or len(fragment) > best[0]):
            best = (len(fragment), key)
    return best[1] if best else None


def normalise_address(raw: str) -> tuple[str, str] | None:
    """Return (address, inferred_chain_kind) or None when it isn't an address.

    EVM addresses are lowercased so they compare cleanly; Solana addresses are
    case-sensitive base58 and stay untouched.
    """
    candidate = raw.strip().strip("`").split()[0] if raw.strip() else ""
    if EVM_ADDRESS_RE.match(candidate):
        return candidate.lower(), "evm"
    if BASE58_ADDRESS_RE.match(candidate):
        return candidate, "svm"
    return None
