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
async def test_buttons_attach_to_last_message():
    bot = MagicMock()
    bot.send_message = AsyncMock()
    text = "<b>Заголовок</b>\n\n" + "\n\n".join(f"{i}. Новость " + "х" * 900 for i in range(6))
    buttons = [("1. АвтоСтат", "https://a.ru/1"), ("2. Колёса", "https://b.ru/2")]

    await sender.send_digest(bot, "-100", text, buttons=buttons)

    calls = bot.send_message.await_args_list
    assert len(calls) > 1
    assert all(c.kwargs["reply_markup"] is None for c in calls[:-1])
    markup = calls[-1].kwargs["reply_markup"]
    assert markup is not None
    urls = [b.url for row in markup.inline_keyboard for b in row]
    assert urls == ["https://a.ru/1", "https://b.ru/2"]


@pytest.mark.asyncio
async def test_buttons_attach_to_photo_when_post_fits_caption():
    bot = MagicMock()
    bot.send_photo = AsyncMock()
    bot.send_message = AsyncMock()
    buttons = [("1. АвтоСтат", "https://a.ru/1")]

    await sender.send_digest(bot, "-100", "<b>Короткий пост</b>", image=b"PNG", buttons=buttons)

    bot.send_message.assert_not_awaited()
    assert bot.send_photo.await_args.kwargs["reply_markup"] is not None


def test_build_keyboard_lays_out_rows():
    markup = sender.build_keyboard([(f"{i}. Источник", f"https://a.ru/{i}") for i in range(5)])
    rows = markup.inline_keyboard
    assert len(rows) == 3, "5 кнопок по 2 в ряд"
    assert [len(r) for r in rows] == [2, 2, 1]


def test_build_keyboard_without_buttons():
    assert sender.build_keyboard([]) is None


@pytest.mark.asyncio
async def test_pin_requested_only_when_asked():
    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=MagicMock(message_id=555))
    bot.pin_chat_message = AsyncMock()

    await sender.send_digest(bot, "-100", "<b>пост</b>", pin=False)
    bot.pin_chat_message.assert_not_awaited()

    await sender.send_digest(bot, "-100", "<b>пост</b>", pin=True)
    bot.pin_chat_message.assert_awaited_once()
    assert bot.pin_chat_message.await_args.kwargs["message_id"] == 555


@pytest.mark.asyncio
async def test_pin_pins_the_photo_message():
    bot = MagicMock()
    bot.send_photo = AsyncMock(return_value=MagicMock(message_id=111))
    bot.send_message = AsyncMock(return_value=MagicMock(message_id=222))
    bot.pin_chat_message = AsyncMock()
    text = "<b>Заголовок</b>\nВступление\n\n" + "\n\n".join(
        f"{i}. Новость " + "х" * 300 for i in range(10)
    )

    await sender.send_digest(bot, "-100", text, image=b"PNG", pin=True)

    assert bot.pin_chat_message.await_args.kwargs["message_id"] == 111


@pytest.mark.asyncio
async def test_missing_pin_rights_do_not_break_publication():
    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=MagicMock(message_id=1))
    bot.pin_chat_message = AsyncMock(side_effect=TelegramError("not enough rights"))

    sent = await sender.send_digest(bot, "-100", "<b>пост</b>", pin=True)
    assert sent == 1, "пост опубликован, несмотря на невозможность закрепить"


@pytest.mark.asyncio
async def test_send_notice_survives_failure():
    bot = MagicMock()
    bot.send_message = AsyncMock(side_effect=TelegramError("заблокирован"))
    await sender.send_notice(bot, [1, 2], "текст")  # не должно бросить
    assert bot.send_message.await_count == 2
