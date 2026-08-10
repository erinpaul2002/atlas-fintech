"""Streaming out to Telegram: one message, edited as the answer builds.

Eight seconds of silence reads as broken; a message that visibly fills in reads as alive
(ARCHITECTURE.md §3). Edits are capped at roughly one a second because Telegram's per-chat
flood limit counts them.
"""

import asyncio
import html
import logging
import re
import time
from typing import Any, Awaitable, Callable

from telegram import Bot
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest, RetryAfter

log = logging.getLogger(__name__)

EDIT_INTERVAL = 1.0
CHUNK = 4000
MIN_FIRST = 12


class StreamingReply:
    """Send once, then edit. `update()` takes the whole text so far, not a delta."""

    def __init__(self, bot: Bot, chat_id: int):
        self.bot = bot
        self.chat_id = chat_id
        self.message_id: int | None = None
        self.shown = ""
        self.last_edit = 0.0

    async def typing(self) -> None:
        try:
            await self.bot.send_chat_action(self.chat_id, ChatAction.TYPING)
        except Exception:
            pass

    async def update(self, text: str) -> None:
        text = text.strip()
        if not text or text == self.shown:
            return
        if self.message_id is None:
            if len(text) >= MIN_FIRST:
                await self._send(text)
            return
        if time.monotonic() - self.last_edit < EDIT_INTERVAL:
            return
        if not _at_boundary(text) and len(text) - len(self.shown) < 160:
            return
        await self._edit(text)

    async def finish(self, text: str) -> str:
        text = text.strip()
        if not text:
            return ""
        head, *rest = _chunks(text)
        if self.message_id is None:
            await self._send(head)
        elif head != self.shown:
            await self._edit(head)
        for extra in rest:
            self.message_id = None
            await self._send(extra)
        return text

    async def _send(self, text: str) -> None:
        async def send(rendered: str, mode: str | None) -> Any:
            return await self.bot.send_message(self.chat_id, rendered, parse_mode=mode)

        msg = await _deliver(send, text)
        if msg is not None:
            self.message_id = msg.message_id
            self.shown = text
            self.last_edit = time.monotonic()

    async def _edit(self, text: str) -> None:
        async def edit(rendered: str, mode: str | None) -> Any:
            return await self.bot.edit_message_text(
                chat_id=self.chat_id, message_id=self.message_id, text=rendered, parse_mode=mode
            )

        if await _deliver(edit, text) is not None:
            self.shown = text
        self.last_edit = time.monotonic()


async def _deliver(action: Callable[[str, str | None], Awaitable[Any]], text: str) -> Any:
    """HTML first, plain text if Telegram rejects it, backing off on a flood wait.

    A malformed entity in the model's prose is enough for Telegram to refuse the whole
    message, and a refused message is a dead turn — so falling back to plain is not optional.
    """
    for formatted in (True, False):
        for _ in range(2):
            try:
                return await action(
                    _html(text) if formatted else _plain(text),
                    ParseMode.HTML if formatted else None,
                )
            except RetryAfter as exc:
                await asyncio.sleep(float(exc.retry_after) + 0.5)
            except BadRequest as exc:
                if "not modified" in str(exc).lower():
                    return None
                log.warning(
                    "telegram rejected the %s render: %s",
                    "HTML" if formatted else "plain",
                    str(exc)[:140],
                )
                break
            except Exception as exc:
                log.error("telegram send/edit failed: %s", exc)
                return None
    return None


SENTENCE_END = re.compile(r"[.!?:]\s*$|\n\s*$")
BOLD = re.compile(r"\*\*(.+?)\*\*|__(.+?)__", re.S)
INLINE_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)")
INLINE_MARKUP = re.compile(
    r"\*\*(?P<star_bold>.+?)\*\*|__(?P<under_bold>.+?)__|"
    r"\[(?P<link_text>[^\]\n]+)\]\((?P<link_url>https?://[^\s)]+)\)",
    re.S,
)


def _at_boundary(text: str) -> bool:
    return bool(SENTENCE_END.search(text))


def _html(text: str) -> str:
    """Model Markdown → Telegram HTML: bold and safe web links survive."""
    out: list[str] = []
    pos = 0
    for match in INLINE_MARKUP.finditer(text):
        out.append(html.escape(text[pos : match.start()], quote=False))
        if match.group("link_url"):
            label = html.escape(match.group("link_text") or "", quote=False)
            url = html.escape(match.group("link_url") or "", quote=True)
            out.append(f'<a href="{url}">{label}</a>')
        else:
            bold = match.group("star_bold") or match.group("under_bold") or ""
            out.append(f"<b>{html.escape(bold, quote=False)}</b>")
        pos = match.end()
    out.append(html.escape(text[pos:], quote=False))
    return "".join(out)


def _plain(text: str) -> str:
    text = BOLD.sub(lambda m: m.group(1) or m.group(2) or "", text)
    return INLINE_LINK.sub(lambda m: f"{m.group(1)} ({m.group(2)})", text)


def _chunks(text: str, size: int = CHUNK) -> list[str]:
    """Split on paragraph boundaries. Per AGENT_SPEC §5 an answer needing two messages was
    already too long — this is a guard, not a feature."""
    if len(text) <= size:
        return [text]
    out: list[str] = []
    remaining = text
    while len(remaining) > size:
        cut = max(
            remaining.rfind("\n\n", 0, size),
            remaining.rfind("\n", 0, size),
            remaining.rfind(" ", 0, size),
        )
        if cut < size // 2:
            cut = size
        out.append(remaining[:cut].strip())
        remaining = remaining[cut:].strip()
    if remaining:
        out.append(remaining)
    return out
