"""Thin stdlib HTTP helper.

The project has no third-party dependencies, so this wraps urllib with the
retry and JSON handling every provider needs.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

USER_AGENT = "wallet-monitor/1.0 (+https://github.com/luciferhell4/Tracker)"


class HttpError(RuntimeError):
    def __init__(self, status: int, body: str, url: str) -> None:
        super().__init__(f"HTTP {status} from {url}: {body[:300]}")
        self.status = status
        self.body = body
        self.url = url


def request_json(
    url: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
    timeout: int = 30,
    retries: int = 3,
    backoff: float = 1.5,
) -> Any:
    """Call a JSON endpoint, retrying on 429/5xx and transport errors."""
    if params:
        clean = {k: v for k, v in params.items() if v is not None}
        url = f"{url}?{urllib.parse.urlencode(clean)}"

    body = json.dumps(payload).encode() if payload is not None else None
    all_headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
    if body is not None:
        all_headers["Content-Type"] = "application/json"
    all_headers.update(headers or {})

    last: Exception | None = None
    for attempt in range(retries):
        req = urllib.request.Request(url, data=body, headers=all_headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                text = resp.read().decode("utf-8", "replace")
                return json.loads(text) if text else None
        except urllib.error.HTTPError as exc:
            text = exc.read().decode("utf-8", "replace")
            last = HttpError(exc.code, text, url)
            if exc.code not in (408, 429, 500, 502, 503, 504):
                raise last
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last = exc
        if attempt < retries - 1:
            time.sleep(backoff ** attempt)
    assert last is not None
    raise last
