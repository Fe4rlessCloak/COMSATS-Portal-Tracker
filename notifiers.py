#!/usr/bin/env python3
"""
Pluggable open-standard notification backends (Python stdlib only).

Supports:
- ntfy (https://ntfy.sh or self-hosted)
- Generic JSON Webhook (Home Assistant, n8n, custom servers)
- Discord Webhook
- Telegram Bot API
- Console logging (always enabled)
"""

from __future__ import annotations

import http.client
import json
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from config import NOTIFIER_TIMEOUT_DEFAULT, parse_notifier_timeout


@dataclass(frozen=True)
class Notification:
    event: str
    title: str
    message: str
    priority: str = "default"  # min, low, default, high, urgent
    tags: Sequence[str] = field(default_factory=tuple)
    url: Optional[str] = None
    data: Optional[Dict[str, Any]] = None


class BaseNotifier:
    name: str = "base"

    def send(self, note: Notification) -> None:
        raise NotImplementedError


class ConsoleNotifier(BaseNotifier):
    name = "console"

    def send(self, note: Notification) -> None:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{ts}] [{note.event.upper()}] {note.title}")
        for line in note.message.splitlines():
            print(f"    {line}")


def _latin1_safe(text: str) -> str:
    """Ensure HTTP header values cannot raise UnicodeEncodeError in http.client."""
    normalized = text.replace("—", "-").replace("–", "-").replace("→", "->")
    return normalized.encode("latin-1", errors="ignore").decode("latin-1").strip()


class NtfyNotifier(BaseNotifier):
    """Publishes notifications to an ntfy topic URL (e.g. https://ntfy.sh/my-topic)."""

    name = "ntfy"

    def __init__(self, topic_url: str, token: str = "", timeout: float = NOTIFIER_TIMEOUT_DEFAULT) -> None:
        self.topic_url = topic_url.strip()
        self.token = token.strip()
        self.timeout = timeout

    def send(self, note: Notification) -> None:
        headers: Dict[str, str] = {
            "Title": _latin1_safe(note.title),
            "Priority": note.priority,
            "Content-Type": "text/plain; charset=utf-8",
            "User-Agent": "comsats-portal-tracker/1.0",
        }
        if note.tags:
            headers["Tags"] = ",".join(_latin1_safe(t) for t in note.tags)
        if note.url:
            headers["Click"] = _latin1_safe(note.url)
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        req = urllib.request.Request(
            self.topic_url,
            data=note.message.encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            resp.read()


class WebhookNotifier(BaseNotifier):
    """Sends a clean JSON payload to any HTTP webhook endpoint."""

    name = "webhook"

    def __init__(self, webhook_url: str, timeout: float = NOTIFIER_TIMEOUT_DEFAULT) -> None:
        self.webhook_url = webhook_url.strip()
        self.timeout = timeout

    def send(self, note: Notification) -> None:
        payload = {
            "event": note.event,
            "title": note.title,
            "message": note.message,
            "priority": note.priority,
            "tags": list(note.tags),
            "url": note.url,
            "data": note.data or {},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.webhook_url,
            data=body,
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "User-Agent": "comsats-portal-tracker/1.0",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            resp.read()


class DiscordNotifier(BaseNotifier):
    """Posts formatted messages to a Discord webhook URL."""

    name = "discord"

    def __init__(self, webhook_url: str, timeout: float = NOTIFIER_TIMEOUT_DEFAULT) -> None:
        self.webhook_url = webhook_url.strip()
        self.timeout = timeout

    def send(self, note: Notification) -> None:
        content = f"**{note.title}**\n{note.message}"
        if note.url:
            content += f"\n{note.url}"
        payload = {"content": content[:1990]}
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.webhook_url,
            data=body,
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "User-Agent": "comsats-portal-tracker/1.0",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            resp.read()


class TelegramNotifier(BaseNotifier):
    """Sends messages via Telegram Bot API sendMessage."""

    name = "telegram"

    def __init__(self, bot_token: str, chat_id: str, timeout: float = NOTIFIER_TIMEOUT_DEFAULT) -> None:
        self.bot_token = bot_token.strip()
        self.chat_id = chat_id.strip()
        self.timeout = timeout

    def send(self, note: Notification) -> None:
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        text = f"{note.title}\n\n{note.message}"
        if note.url:
            text += f"\n\n{note.url}"
        payload = {
            "chat_id": self.chat_id,
            "text": text[:4090],
            "disable_web_page_preview": True,
        }
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            resp.read()


def build_notifiers(
    config: Dict[str, str], timeout: Optional[float] = None
) -> List[BaseNotifier]:
    """Instantiate all configured notifiers (ConsoleNotifier is always first).

    ``timeout`` is the unified NOTIFIER_TIMEOUT from the Config module; when omitted
    the caller-supplied config mapping is read through the same canonical rule
    (default 20.0s, minimum 1.0s) so there is exactly one timeout rule in the project.
    """
    notifiers: List[BaseNotifier] = [ConsoleNotifier()]
    if timeout is None:
        timeout = parse_notifier_timeout(config)

    ntfy_url = config.get("NTFY_URL", "").strip()
    if ntfy_url:
        notifiers.append(NtfyNotifier(ntfy_url, config.get("NTFY_TOKEN", ""), timeout=timeout))

    webhook_url = config.get("WEBHOOK_URL", "").strip()
    if webhook_url:
        notifiers.append(WebhookNotifier(webhook_url, timeout=timeout))

    discord_url = config.get("DISCORD_WEBHOOK_URL", "").strip()
    if discord_url:
        notifiers.append(DiscordNotifier(discord_url, timeout=timeout))

    tg_token = config.get("TELEGRAM_BOT_TOKEN", "").strip()
    tg_chat = config.get("TELEGRAM_CHAT_ID", "").strip()
    if tg_token and tg_chat:
        notifiers.append(TelegramNotifier(tg_token, tg_chat, timeout=timeout))

    return notifiers


def dispatch_all(
    notifiers: Sequence[BaseNotifier],
    note: Notification,
    timeout: Optional[float] = None,
) -> None:
    """Send a notification through every backend without letting one failure or stall block."""
    async_notifiers: List[BaseNotifier] = []
    for notifier in notifiers:
        if isinstance(notifier, ConsoleNotifier):
            try:
                notifier.send(note)
            except Exception as exc:
                print(f"[WARN] Notifier '{notifier.name}' failed: {exc}")
        else:
            async_notifiers.append(notifier)

    if not async_notifiers:
        return

    def _send_one(n: BaseNotifier) -> None:
        try:
            n.send(note)
        except (
            urllib.error.URLError,
            urllib.error.HTTPError,
            http.client.HTTPException,
            TimeoutError,
            OSError,
            ValueError,
            Exception,
        ) as exc:
            print(f"[WARN] Notifier '{n.name}' failed: {exc}")

    workers: List[Tuple[BaseNotifier, threading.Thread, float, float]] = []
    start = time.monotonic()
    for notifier in async_notifiers:
        per_timeout = (
            max(0.1, float(timeout))
            if timeout is not None
            else float(getattr(notifier, "timeout", NOTIFIER_TIMEOUT_DEFAULT)) + 5.0
        )
        deadline = start + per_timeout
        t = threading.Thread(
            target=_send_one,
            args=(notifier,),
            name=f"notify-{notifier.name}",
            daemon=True,
        )
        t.start()
        workers.append((notifier, t, per_timeout, deadline))

    for notifier, t, per_timeout, deadline in workers:
        remaining = max(0.0, deadline - time.monotonic())
        t.join(timeout=remaining)
        if t.is_alive():
            print(f"[WARN] Notifier '{notifier.name}' timed out after {per_timeout:.1f}s")
