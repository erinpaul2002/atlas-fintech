"""Telegram update → a normalized message with its media already downloaded.

Voice, image and PDF bytes attach directly to the model. Excel is parsed locally into a bounded
text attachment because binary workbook support varies across model versions.
"""

import asyncio
import logging
from dataclasses import dataclass, field

from telegram import Bot, Update

from atlas.services.excel import extract_workbook, is_excel

log = logging.getLogger(__name__)

MAX_BYTES = 15 * 1024 * 1024


@dataclass
class Inbound:
    chat_id: int
    telegram_user_id: int
    first_name: str
    text: str = ""
    is_start: bool = False
    media: list[tuple[str, bytes]] = field(default_factory=list)
    media_kind: str | None = None
    media_ref: str | None = None
    filename: str | None = None
    note: str = ""


async def normalize(update: Update, bot: Bot) -> Inbound | None:
    msg = update.effective_message
    chat = update.effective_chat
    if msg is None or chat is None:
        return None

    sender = update.effective_user
    inbound = Inbound(
        chat_id=chat.id,
        telegram_user_id=sender.id if sender else chat.id,
        first_name=(sender.first_name if sender else "") or "",
        text=(msg.text or msg.caption or "").strip(),
    )

    # /start arrives from Telegram itself. It is a hello, not a command, but retaining
    # the signal lets the handler recover an intro that was never shown for this user.
    inbound.is_start = inbound.text == "/start"
    if inbound.is_start:
        inbound.text = "Hi"

    if msg.voice or msg.audio:
        media = msg.voice or msg.audio
        inbound.media_kind = "voice"
        await _attach(inbound, bot, media.file_id, media.mime_type or "audio/ogg", media.file_size)
    elif msg.photo:
        photo = msg.photo[-1]  # largest rendition
        inbound.media_kind = "photo"
        await _attach(inbound, bot, photo.file_id, "image/jpeg", photo.file_size)
    elif msg.document:
        doc = msg.document
        inbound.media_kind = "document"
        inbound.filename = doc.file_name
        await _attach(inbound, bot, doc.file_id, doc.mime_type or "application/octet-stream", doc.file_size)
        if inbound.media and is_excel(doc.file_name, doc.mime_type):
            await _extract_excel(inbound)

    if not inbound.text and not inbound.media and not inbound.note:
        return None
    return inbound


async def _attach(inbound: Inbound, bot: Bot, file_id: str, mime: str, size: int | None) -> None:
    if size and size > MAX_BYTES:
        inbound.note = f"The file is {size // 1024 // 1024}MB, too big for me to read (limit 15MB)."
        return
    try:
        handle = await bot.get_file(file_id)
        data = bytes(await handle.download_as_bytearray())
    except Exception as exc:
        log.warning("media download failed: %s", exc)
        inbound.note = "I couldn't download that file from Telegram."
        return
    inbound.media.append((mime, data))
    inbound.media_ref = file_id


async def _extract_excel(inbound: Inbound) -> None:
    """Replace a binary workbook with bounded text; never execute formulas or macros."""
    _, data = inbound.media[-1]
    try:
        extracted = await asyncio.to_thread(extract_workbook, data, inbound.filename or "workbook.xlsx")
    except Exception as exc:
        log.warning("Excel extraction failed for %s: %s", inbound.filename, exc)
        inbound.note = "I couldn't parse this Excel workbook, so I received it as a raw file."
        return

    inbound.media[-1] = ("text/plain", extracted.encode("utf-8"))
    inbound.media_kind = "spreadsheet"
    inbound.note = "The Excel workbook was parsed locally; formulas use cached values and macros were not run."
