"""Local web app: the monitor with a UI instead of a command line.

`wallet-monitor serve` starts a small stdlib HTTP server that exposes the same
scanner over JSON and serves a single-page dashboard on top of it. It runs on
your machine, so it can reach the chain APIs a hosted page could not.
"""

from __future__ import annotations

import json
import mimetypes
import threading
import time
import traceback
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import __version__, chains, notify, notion_sync, paste
from .config import Config
from .models import Signal, Wallet
from .providers.registry import build_providers, provider_for
from .scanner import scan, wallet_index
from .scoring import build_signals
from .store import Store

ASSETS = Path(__file__).parent / "web_assets"
LAST_SCAN_KEY = "last_scan"


def wallet_json(wallet: Wallet) -> dict[str, Any]:
    chain = chains.CHAINS.get(wallet.chain)
    return {
        **wallet.to_row(),
        "explorer": chain.address_url(wallet.address) if chain else "",
        "short": wallet.address[:6] + "…" + wallet.address[-4:],
    }


def signal_json(signal: Signal, now: int) -> dict[str, Any]:
    chain = chains.CHAINS.get(signal.chain)
    return {
        "chain": signal.chain,
        "chain_name": chain.name if chain else signal.chain,
        "contract": signal.contract,
        "collection_name": signal.collection_name,
        "score": signal.score,
        "wallet_count": signal.wallet_count,
        "mint_count": signal.mint_count,
        "first_mint_at": signal.first_mint_at,
        "last_mint_at": signal.last_mint_at,
        "age_minutes": round((now - signal.first_mint_at) / 60, 1),
        "window_minutes": round(signal.window_minutes, 1),
        "token_url": chain.token_url(signal.contract) if chain else "",
        "tx_url": chain.tx_url(signal.sample_tx) if chain and signal.sample_tx else "",
        "wallets": [wallet_json(w) for w in signal.wallets],
    }


@dataclass
class ScanState:
    running: bool = False
    started_at: int = 0
    finished_at: int = 0
    summary: str = ""
    errors: list[str] = field(default_factory=list)
    new_mints: int = 0
    new_signals: int = 0

    def as_json(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "summary": self.summary,
            "errors": self.errors[:20],
            "new_mints": self.new_mints,
            "new_signals": self.new_signals,
        }


class MonitorService:
    """Everything the UI can ask for, with one sqlite connection per call.

    sqlite connections are not shareable across threads, and the server is
    threaded, so each operation opens and closes its own store.
    """

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.scan_state = ScanState()
        self._scan_lock = threading.Lock()

    def _store(self) -> Store:
        return Store(self.cfg.store_path)

    # ----------------------------------------------------------------- health

    def health(self) -> tuple[dict[str, Any], int]:
        """Liveness plus a real read of the database, for uptime checks.

        Returns 503 rather than 200 when the store cannot be opened, so a
        proxy or monitor sees a broken instance instead of an empty one.
        """
        try:
            with self._store() as store:
                stats = store.stats()
            return ({"status": "ok", "version": __version__, **stats}, 200)
        except Exception as exc:
            return ({"status": "error", "version": __version__, "error": str(exc)}, 503)

    # ------------------------------------------------------------------ state

    def state(self) -> dict[str, Any]:
        cfg = self.cfg
        providers = build_providers(cfg)
        with self._store() as store:
            stats = store.stats()
            wallets = store.wallets()
            last_scan = store.get_meta(LAST_SCAN_KEY, "")
            coverage = []
            for chain in store.wallet_chains():
                provider = provider_for(providers, chain) if chain in chains.CHAINS else None
                coverage.append(
                    {
                        "chain": chain,
                        "wallets": sum(1 for w in wallets if w.chain == chain),
                        "provider": provider.name if provider else "",
                    }
                )
        return {
            "stats": stats,
            "coverage": sorted(coverage, key=lambda c: -c["wallets"]),
            "credentials": {
                "notion": bool(cfg.notion_token),
                "alchemy": bool(cfg.alchemy_key),
                "etherscan": bool(cfg.etherscan_key),
                "helius": bool(cfg.helius_key),
                "discord": bool(cfg.discord_webhook),
                "telegram": bool(cfg.telegram_bot_token and cfg.telegram_chat_id),
            },
            "rules": {
                "min_wallets": cfg.signal.min_wallets,
                "window_minutes": cfg.signal.window_minutes,
                "max_contract_age_hours": cfg.signal.max_contract_age_hours,
                "lookback_hours": cfg.scan.lookback_hours,
            },
            "notion_sources": [
                {"name": s.name, "url": s.url} for s in cfg.notion_sources if s.enabled
            ],
            "last_scan": json.loads(last_scan) if last_scan else None,
            "scan": self.scan_state.as_json(),
            "chains": [
                {"key": c.key, "name": c.name} for c in chains.CHAINS.values()
            ],
            "store_path": cfg.store_path,
            "now": int(time.time()),
        }

    # ---------------------------------------------------------------- wallets

    def wallets(self, chain: str | None = None) -> list[dict[str, Any]]:
        with self._store() as store:
            return [wallet_json(w) for w in store.wallets(chain or None)]

    def add_wallets(self, text: str, chain: str | None, tag: str, source: str) -> dict[str, Any]:
        found = notion_sync.dedupe(
            paste.parse(text, source=source or "web", tag=tag, chain=chain or None)
        )
        with self._store() as store:
            store.upsert_wallets(found)
        return {"added": len(found), "wallets": [wallet_json(w) for w in found]}

    def delete_wallet(self, chain: str, address: str) -> dict[str, Any]:
        with self._store() as store:
            return {"deleted": store.delete_wallet(chain, address)}

    def sync_notion(self) -> dict[str, Any]:
        wallets, notes = notion_sync.sync(self.cfg)
        with self._store() as store:
            store.upsert_wallets(wallets)
        notion_sync.write_csv(wallets, self.cfg.wallets_csv)
        return {"imported": len(wallets), "notes": notes}

    # ---------------------------------------------------------------- signals

    def signals(
        self,
        hours: int,
        min_wallets: int | None = None,
        window_minutes: int | None = None,
        chain: str | None = None,
    ) -> dict[str, Any]:
        rules = self.cfg.signal
        rules = type(rules)(
            min_wallets=min_wallets or rules.min_wallets,
            window_minutes=window_minutes or rules.window_minutes,
            min_score=rules.min_score,
            max_contract_age_hours=rules.max_contract_age_hours,
            top_n=rules.top_n,
        )
        now = int(time.time())
        with self._store() as store:
            events = store.mints_since(now - hours * 3600)
            if chain:
                events = [e for e in events if e.chain == chain]
            signals = build_signals(
                events, wallet_index(store.wallets()), rules,
                now=now, first_seen=store.contract_first_seen,
            )
        return {
            "signals": [signal_json(s, now) for s in signals],
            "now": now,
            "rules": {"min_wallets": rules.min_wallets, "window_minutes": rules.window_minutes},
        }

    def activity(self, hours: int) -> dict[str, Any]:
        now = int(time.time())
        since = now - hours * 3600
        with self._store() as store:
            events = store.mints_since(since)
            hourly = store.mints_per_hour(since)
            labels = {w.key(): w for w in store.wallets()}
        recent = sorted(events, key=lambda e: e.timestamp, reverse=True)[:200]
        return {
            "now": now,
            "hourly": [{"bucket": b, "count": c} for b, c in hourly],
            "mints": [
                {
                    "chain": e.chain,
                    "wallet": e.wallet,
                    "wallet_short": e.wallet[:6] + "…" + e.wallet[-4:],
                    "wallet_tag": (labels.get((e.chain, e.wallet)).tag
                                   if labels.get((e.chain, e.wallet)) else ""),
                    "contract": e.contract,
                    "collection_name": e.collection_name,
                    "token_standard": e.token_standard,
                    "timestamp": e.timestamp,
                    "token_url": (chains.CHAINS[e.chain].token_url(e.contract)
                                  if e.chain in chains.CHAINS else ""),
                    "tx_url": (chains.CHAINS[e.chain].tx_url(e.tx_hash)
                               if e.chain in chains.CHAINS else ""),
                }
                for e in recent
            ],
        }

    # ------------------------------------------------------------------- scan

    def start_scan(self, only_chains: list[str] | None = None) -> dict[str, Any]:
        with self._scan_lock:
            if self.scan_state.running:
                return {"started": False, "scan": self.scan_state.as_json()}
            self.scan_state = ScanState(running=True, started_at=int(time.time()))
        threading.Thread(target=self._run_scan, args=(only_chains,), daemon=True).start()
        return {"started": True, "scan": self.scan_state.as_json()}

    def _run_scan(self, only_chains: list[str] | None) -> None:
        state = self.scan_state
        try:
            with self._store() as store:
                result = scan(self.cfg, store, only_chains)
                state.summary = result.summary()
                state.errors = result.errors
                state.new_mints = len(result.new_events)

                events = store.mints_since(int(time.time()) - self.cfg.scan.lookback_hours * 3600)
                fresh = [
                    s
                    for s in build_signals(
                        events, wallet_index(store.wallets()), self.cfg.signal,
                        first_seen=store.contract_first_seen,
                    )
                    if store.should_alert(s.chain, s.contract, s.wallet_count)
                ]
                state.new_signals = len(fresh)
                if fresh:
                    notify.send(self.cfg, fresh, quiet=True)
                    for s in fresh:
                        store.mark_alerted(s.chain, s.contract, s.wallet_count)
                store.set_meta(
                    LAST_SCAN_KEY,
                    json.dumps(
                        {
                            "at": int(time.time()),
                            "summary": state.summary,
                            "new_mints": state.new_mints,
                            "new_signals": state.new_signals,
                            "errors": state.errors[:20],
                        }
                    ),
                )
        except Exception as exc:
            state.summary = f"scan failed: {exc}"
            state.errors = [traceback.format_exc(limit=3)]
        finally:
            state.running = False
            state.finished_at = int(time.time())


# ------------------------------------------------------------------- handler

class Handler(BaseHTTPRequestHandler):
    service: MonitorService
    server_version = "wallet-monitor"

    def log_message(self, fmt: str, *args: Any) -> None:  # quieter than the default
        if self.path.startswith("/api/") and not self.path.startswith("/api/state"):
            print(f"  {self.command} {self.path}")

    # ---------------------------------------------------------------- plumbing

    def _send_json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return {}

    def _serve_asset(self, name: str) -> None:
        target = (ASSETS / name).resolve()
        if not target.is_file() or ASSETS.resolve() not in target.parents:
            self._send_json({"error": "not found"}, 404)
            return
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(name)[0] or "text/plain")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    # ----------------------------------------------------------------- routes

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        route = parsed.path

        def as_int(key: str, default: int) -> int:
            try:
                return int(query.get(key, default))
            except ValueError:
                return default

        try:
            if route in ("/", "/index.html"):
                self._serve_asset("index.html")
            elif route in ("/app.js", "/styles.css"):
                self._serve_asset(route.lstrip("/"))
            elif route == "/api/health":
                self._send_json(*self.service.health())
            elif route == "/api/state":
                self._send_json(self.service.state())
            elif route == "/api/wallets":
                self._send_json({"wallets": self.service.wallets(query.get("chain"))})
            elif route == "/api/signals":
                self._send_json(
                    self.service.signals(
                        hours=as_int("hours", 48),
                        min_wallets=as_int("min_wallets", 0) or None,
                        window_minutes=as_int("window_minutes", 0) or None,
                        chain=query.get("chain") or None,
                    )
                )
            elif route == "/api/activity":
                self._send_json(self.service.activity(as_int("hours", 24)))
            elif route == "/api/scan":
                self._send_json({"scan": self.service.scan_state.as_json()})
            else:
                self._send_json({"error": "not found"}, 404)
        except Exception as exc:
            self._send_json({"error": str(exc)}, 500)

    def do_POST(self) -> None:  # noqa: N802
        route = urlparse(self.path).path
        body = self._body()
        try:
            if route == "/api/wallets":
                self._send_json(
                    self.service.add_wallets(
                        text=str(body.get("text", "")),
                        chain=body.get("chain") or None,
                        tag=str(body.get("tag", "")),
                        source=str(body.get("source", "web")),
                    )
                )
            elif route == "/api/wallets/delete":
                self._send_json(
                    self.service.delete_wallet(
                        str(body.get("chain", "")), str(body.get("address", ""))
                    )
                )
            elif route == "/api/sync":
                self._send_json(self.service.sync_notion())
            elif route == "/api/scan":
                self._send_json(self.service.start_scan(body.get("chains") or None))
            else:
                self._send_json({"error": "not found"}, 404)
        except Exception as exc:
            self._send_json({"error": str(exc)}, 500)


def serve(cfg: Config, host: str = "127.0.0.1", port: int = 8787) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"service": MonitorService(cfg)})
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True
    return httpd
