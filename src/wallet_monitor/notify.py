"""Alert sinks: stdout always, Discord and Telegram when configured."""

from __future__ import annotations

from .config import Config
from .http import request_json
from .models import Signal
from .report import render, render_markdown


def _discord_body(signals: list[Signal]) -> dict[str, object]:
    lines = [f"**{len(signals)} early-mint signal(s)**", "", render_markdown(signals)]
    content = "\n".join(lines)
    return {"content": content[:1900]}


def send(cfg: Config, signals: list[Signal], *, quiet: bool = False) -> list[str]:
    """Deliver signals. Returns notes about each sink that was used."""
    notes: list[str] = []
    if not signals:
        return notes
    if not quiet:
        print(render(signals))
        notes.append("printed to stdout")

    if cfg.discord_webhook:
        try:
            request_json(
                cfg.discord_webhook,
                method="POST",
                payload=_discord_body(signals),
                timeout=cfg.scan.request_timeout,
                retries=2,
            )
            notes.append("posted to Discord")
        except Exception as exc:
            notes.append(f"Discord failed: {exc}")

    if cfg.telegram_bot_token and cfg.telegram_chat_id:
        try:
            request_json(
                f"https://api.telegram.org/bot{cfg.telegram_bot_token}/sendMessage",
                method="POST",
                payload={
                    "chat_id": cfg.telegram_chat_id,
                    "text": render(signals)[:4000],
                    "disable_web_page_preview": True,
                },
                timeout=cfg.scan.request_timeout,
                retries=2,
            )
            notes.append("sent to Telegram")
        except Exception as exc:
            notes.append(f"Telegram failed: {exc}")
    return notes
