"""Разбиение постов и отправка в Telegram."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram.error import BadRequest, TelegramError

import sender


# --------------------------------------------------------------------------- #
# Разбиение
# --------------------------------------------------------------------------- #


def test_short_message_is_not_split():
    assert sender.split_message("коротко") == ["коротко"]


def test_long_message_split_within_limit():
    text = "\n\n".join(f"<b>Блок {i}</b>\n" + "я" * 500 for i in range(20))
    chunks = sender.split_message(text)
    assert len(chunks) > 1
    assert all(len(chunk) <= sender.MAX_MESSAGE_LEN for chunk in chunks)


def test_split_keeps_all_content():
    text = "\n\n".join(f"строка {i}" for i in range(500))
    chunks = sender.split_message(text)
    assert "строка 0" in chunks[0]
    assert "строка 499" in chunks[-1]


def test_single_oversized_block_is_hard_split():
    chunks = sender.split_message("я" * (sender.MAX_MESSAGE_LEN * 2))
    assert all(len(chunk) <= sender.MAX_MESSAGE_LEN for chunk in chunks)


def test_split_for_photo_puts_header_in_caption():
    text = "<b>Заголовок</b>\nВступление\n\n" + "\n\n".join(
        f"{i}. Новость " + "х" * 300 for i in range(10)
    )
    caption, rest = sender.split_for_photo(text)
    assert caption == "<b>Заголовок</b>\nВступление"
    assert len(caption) <= sender.MAX_CAPTION_LEN
    assert rest and all(len(chunk) <= sender.MAX_MESSAGE_LEN for chunk in rest)


def test_split_for_photo_keeps_short_post_as_caption():
    caption, rest = sender.split_for_photo("<b>Заголовок</b>\n\nОдна новость")
    assert caption == "<b>Заголовок</b>\n\nОдна новость"
    assert rest == []


def test_strip_html_keeps_links_readable():
    plain = sender._strip_html('<b>Тест</b>\n<a href="https://a.ru">Источник</a>')
    assert "<" not in plain
    assert "Источник: https://a.ru" in plain


# --------------------------------------------------------------------------- #
# Отправка
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_send_digest_without_image():
    bot = MagicMock()
    bot.send_message = AsyncMock()
    sent = await sender.send_digest(bot, "-100", "<b>пост</b>")
    assert sent == 1
    assert bot.send_message.await_args.kwargs["parse_mode"] == "HTML"


@pytest.mark.asyncio
async def test_send_digest_with_image_uses_photo_and_caption():
    bot = MagicMock()
    bot.send_photo = AsyncMock()
    bot.send_message = AsyncMock()
    text = "<b>Заголовок</b>\nВступление\n\n" + "\n\n".join(
        f"{i}. Новость " + "х" * 300 for i in range(10)
    )
    sent = await sender.send_digest(bot, "-100", text, image=b"PNGDATA")

    bot.send_photo.assert_awaited_once()
    assert bot.send_photo.await_args.kwargs["photo"] == b"PNGDATA"
    assert "Заголовок" in bot.send_photo.await_args.kwargs["caption"]
    assert sent == 1 + bot.send_message.await_count


@pytest.mark.asyncio
async def test_falls_back_to_text_when_photo_fails():
    bot = MagicMock()
    bot.send_photo = AsyncMock(side_effect=TelegramError("photo rejected"))
    bot.send_message = AsyncMock()
    await sender.send_digest(bot, "-100", "<b>пост</b>", image=b"PNGDATA")
    bot.send_message.assert_awaited_once()
    assert "пост" in bot.send_message.await_args.kwargs["text"]


@pytest.mark.asyncio
async def test_bad_html_is_resent_as_plain_text():
    bot = MagicMock()
    bot.send_message = AsyncMock(side_effect=[BadRequest("can't parse entities"), None])
    await sender.send_digest(bot, "-100", '<a href="https://a.ru">Источник</a>')
    assert bot.send_message.await_count == 2
    second = bot.send_message.await_args.kwargs
    assert "parse_mode" not in second
    assert "<a href" not in second["text"]


@pytest.mark.asyncio
async def test_send_notice_survives_failure():
    bot = MagicMock()
    bot.send_message = AsyncMock(side_effect=TelegramError("заблокирован"))
    await sender.send_notice(bot, [1, 2], "текст")  # не должно бросить
    assert bot.send_message.await_count == 2
