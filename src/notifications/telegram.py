"""Telegram bot — lets you chat with NXFH02 and receive notifications.

Uses httpx (already a dependency) to call the Telegram Bot API directly.
Runs a polling loop in a background thread so it doesn't block the agent.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from typing import TYPE_CHECKING, Callable

import httpx

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

_API = "https://api.telegram.org/bot{token}"


def _escape_telegram_markdown(text: str) -> str:
    """Escape Telegram MarkdownV2 control characters in dynamic text."""
    return re.sub(r"([_\*\[\]\(\)~`>#+\-=|{}\.!])", r"\\\1", text)


class TelegramBot:
    """Lightweight Telegram bot: send messages + poll for incoming commands."""

    def __init__(self, token: str, chat_id: str):
        self.token = token
        self.chat_id = chat_id
        self._base = _API.format(token=token)
        self._client = httpx.Client(timeout=30)
        self._offset = 0
        self._running = False
        self._thread: threading.Thread | None = None
        self._handlers: dict[str, Callable[[str], str]] = {}

        # Register built-in commands
        self._register_builtins()

    # ── outbound ─────────────────────────────────────────────────────────

    def send(self, text: str, parse_mode: str = "Markdown") -> bool:
        """Send a message to the configured chat."""
        try:
            payload_text = text
            payload_parse_mode = parse_mode

            if parse_mode == "Markdown":
                payload_text = _escape_telegram_markdown(text)
                payload_parse_mode = "MarkdownV2"

            resp = self._client.post(
                f"{self._base}/sendMessage",
                json={
                    "chat_id": self.chat_id,
                    "text": payload_text,
                    "parse_mode": payload_parse_mode,
                },
            )
            if resp.status_code != 200:
                logger.warning("Telegram send failed: %s", resp.text)
                return False
            return True
        except Exception as e:
            logger.warning("Telegram send error: %s", e)
            return False

    def notify(self, text: str) -> bool:
        """Alias for send — use for agent notifications."""
        return self.send(text)

    # ── command registration ─────────────────────────────────────────────

    def register(self, command: str, handler: Callable[[str], str]) -> None:
        """Register a /command handler. Handler receives args string, returns reply."""
        self._handlers[command.lstrip("/")] = handler

    def _register_builtins(self) -> None:
        self.register("start", lambda _: (
            "NXFH02 Telegram bot active.\n\n"
            "Commands:\n"
            "/status — account & agent state\n"
            "/positions — open positions\n"
            "/performance — competition metrics\n"
            "/help — this message"
        ))
        self.register("help", self._handlers.get("start", lambda _: ""))

    # ── polling loop ─────────────────────────────────────────────────────

    def start_polling(self) -> None:
        """Start background polling thread."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._poll_loop, daemon=True, name="tg-poll")
        self._thread.start()
        logger.info("Telegram bot polling started (chat_id=%s)", self.chat_id)

    def stop(self) -> None:
        self._running = False

    def _poll_loop(self) -> None:
        while self._running:
            try:
                self._poll_once()
            except Exception as e:
                logger.warning("Telegram poll error: %s", e)
            time.sleep(2)

    def _poll_once(self) -> None:
        resp = self._client.get(
            f"{self._base}/getUpdates",
            params={"offset": self._offset, "timeout": 10},
            timeout=15,
        )
        if resp.status_code != 200:
            return

        data = resp.json()
        for update in data.get("result", []):
            self._offset = update["update_id"] + 1
            msg = update.get("message", {})
            text = msg.get("text", "")
            chat_id = str(msg.get("chat", {}).get("id", ""))

            # Only respond to messages from our authorized chat
            if chat_id != self.chat_id:
                continue

            self._handle_message(text)

    def _handle_message(self, text: str) -> None:
        text = text.strip()

        if text.startswith("/"):
            parts = text.split(maxsplit=1)
            cmd = parts[0].lstrip("/").split("@")[0]  # strip @botname suffix
            args = parts[1] if len(parts) > 1 else ""

            handler = self._handlers.get(cmd)
            if handler:
                try:
                    reply = handler(args)
                    if reply:
                        self.send(reply)
                except Exception as e:
                    self.send(f"Error: {e}")
            else:
                self.send(f"Unknown command: /{cmd}\nSend /help for available commands.")
        else:
            # Free-text message — echo back with a hint
            self.send(f"Received: {text}\nUse /help to see available commands.")


def create_bot(token: str, chat_id: str) -> TelegramBot | None:
    """Create and validate a TelegramBot. Returns None if credentials are missing."""
    if not token or not chat_id:
        logger.info("Telegram not configured (missing token or chat_id)")
        return None

    bot = TelegramBot(token, chat_id)

    # Validate token with getMe
    try:
        resp = bot._client.get(f"{bot._base}/getMe")
        if resp.status_code == 200:
            me = resp.json().get("result", {})
            logger.info("Telegram bot connected: @%s", me.get("username", "unknown"))
        else:
            logger.warning("Telegram getMe failed: %s — bot disabled", resp.text)
            return None
    except Exception as e:
        logger.warning("Telegram connection failed: %s — bot disabled", e)
        return None

    return bot
