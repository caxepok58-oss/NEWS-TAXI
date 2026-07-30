"""Отправка готового поста в Telegram через Bot API."""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Iterable, Sequence

from telegram import Bot
from telegram.constants import ParseMode
from telegram.error import BadRequest, RetryAfter, TelegramError

log = logging.getLogger(__name__)

# Лимит Telegram — 4096 символов на сообщение и 1024 на подпись к фото;
# берём с запасом.
MAX_MESSAGE_LEN = 3900
MAX_CAPTION_LEN = 1000
MAX_RETRIES = 3


def split_message(text: str, limit: int = MAX_MESSAGE_LEN) -> list[str]:
    """Режет длинный пост по границам абзацев, не ломая HTML-теги внутри строки."""
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    current = ""
    for block in text.split("\n\n"):
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
        # Отдельный блок сам по себе длиннее лимита — режем по строкам.
        if len(block) <= limit:
            current = block
            continue
        current = ""
        for line in block.split("\n"):
            candidate = f"{current}\n{line}" if current else line
            if len(candidate) <= limit:
                current = candidate
            else:
                if current:
                    chunks.append(current)
                current = line[:limit]
    if current:
        chunks.append(current)
    return chunks


def split_for_photo(text: str) -> tuple[str, list[str]]:
    """Делит пост на подпись к фото и остальные сообщения.

    Подпись ограничена 1024 символами, поэтому в неё уходит заголовок с
    вступлением, а список новостей — отдельными сообщениями.
    """
    if len(text) <= MAX_CAPTION_LEN:
        return text, []

    head, _, tail = text.partition("\n\n")
    if len(head) <= MAX_CAPTION_LEN and tail:
        return head, split_message(tail)

    # Заголовок сам по себе длиннее подписи — отправляем фото без неё.
    return "", split_message(text)


def _strip_html(text: str) -> str:
    """Аварийный вариант: убираем разметку, ссылки оставляем текстом."""
    text = re.sub(r'<a href="([^"]+)">([^<]*)</a>', r"\2: \1", text)
    return re.sub(r"<[^>]+>", "", text)


async def _send_one(bot: Bot, chat_id: str | int, text: str) -> None:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
            return
        except RetryAfter as exc:
            wait = float(getattr(exc, "retry_after", 5))
            log.warning("Telegram просит подождать %.1f с (попытка %d)", wait, attempt)
            await asyncio.sleep(wait + 1)
        except BadRequest as exc:
            # Чаще всего — невалидный HTML. Отправляем то же самое без разметки.
            log.error("Telegram отклонил сообщение (%s), пробую без HTML", exc)
            await bot.send_message(
                chat_id=chat_id,
                text=_strip_html(text),
                disable_web_page_preview=True,
            )
            return
        except TelegramError as exc:
            if attempt == MAX_RETRIES:
                raise
            log.warning("Ошибка отправки (%s), повтор %d/%d", exc, attempt, MAX_RETRIES)
            await asyncio.sleep(2 * attempt)
    raise TelegramError("Не удалось отправить сообщение после нескольких попыток")


async def _send_photo(bot: Bot, chat_id: str | int, image: bytes, caption: str) -> bool:
    """Отправляет фото с подписью. Возвращает False, если не получилось."""
    try:
        await bot.send_photo(
            chat_id=chat_id,
            photo=image,
            caption=caption or None,
            parse_mode=ParseMode.HTML if caption else None,
        )
        return True
    except RetryAfter as exc:
        wait = float(getattr(exc, "retry_after", 5))
        log.warning("Telegram просит подождать %.1f с перед отправкой фото", wait)
        await asyncio.sleep(wait + 1)
        try:
            await bot.send_photo(
                chat_id=chat_id,
                photo=image,
                caption=caption or None,
                parse_mode=ParseMode.HTML if caption else None,
            )
            return True
        except TelegramError as exc2:
            log.error("Фото не отправлено: %s", exc2)
    except TelegramError as exc:
        log.error("Фото не отправлено (%s) — публикую пост без картинки", exc)
    return False


async def send_digest(
    bot: Bot, chat_id: str | int, text: str, image: bytes | None = None
) -> int:
    """Отправляет пост (при необходимости — несколькими сообщениями).

    Возвращает количество отправленных сообщений.
    """
    chunks: Sequence[str]
    sent = 0

    if image:
        caption, rest = split_for_photo(text)
        if await _send_photo(bot, chat_id, image, caption):
            sent += 1
            chunks = rest
        else:
            # Картинку отправить не удалось — публикуем полный текст.
            chunks = split_message(text)
    else:
        chunks = split_message(text)

    for index, chunk in enumerate(chunks, start=1):
        await _send_one(bot, chat_id, chunk)
        sent += 1
        if index < len(chunks):
            await asyncio.sleep(1)  # не упираемся в лимит частоты

    log.info("Дайджест отправлен в %s (%d сообщ.)", chat_id, sent)
    return sent


async def send_notice(bot: Bot, chat_ids: Iterable[str | int], text: str) -> None:
    """Служебное сообщение (ошибка, статус) — без разметки, ошибки гасятся."""
    for chat_id in chat_ids:
        try:
            await bot.send_message(
                chat_id=chat_id, text=text, disable_web_page_preview=True
            )
        except TelegramError as exc:
            log.error("Не удалось отправить служебное сообщение в %s: %s", chat_id, exc)
