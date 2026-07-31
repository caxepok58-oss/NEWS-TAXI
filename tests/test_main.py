"""Оркестрация прогона: успех, сбои, повторы и уведомления."""

from __future__ import annotations

import logging
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:TEST")
os.environ.setdefault("TELEGRAM_CHAT_ID", "-1001234567890")

import main  # noqa: E402
import summarizer  # noqa: E402


@pytest.fixture
def digest(article_factory):
    articles = [article_factory(title="Новость")]
    return summarizer.Digest(
        text="<b>пост</b>", articles=articles, title="Дайджест", image_prompt="сцена"
    )


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Каждый тест работает со своей базой."""
    import fetcher

    db = tmp_path / "test.db"
    monkeypatch.setattr(fetcher, "DB_PATH", str(db))
    yield


def _bot() -> MagicMock:
    bot = MagicMock()
    bot.send_message = AsyncMock()
    bot.send_photo = AsyncMock()
    return bot


# --------------------------------------------------------------------------- #
# Конфигурация
# --------------------------------------------------------------------------- #


def test_digest_time_parses_config(monkeypatch):
    monkeypatch.setattr(main, "DIGEST_TIME", "07:45")
    assert (main.digest_time().hour, main.digest_time().minute) == (7, 45)
    assert main.digest_time().tzinfo is not None


def test_digest_time_falls_back_on_garbage(monkeypatch):
    monkeypatch.setattr(main, "DIGEST_TIME", "не время")
    assert main.digest_time().hour == 8


def test_check_config_reports_missing_keys(monkeypatch):
    monkeypatch.setattr(main, "BOT_TOKEN", "")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(SystemExit) as exc:
        main.check_config()
    assert "TELEGRAM_BOT_TOKEN" in str(exc.value)
    assert "ANTHROPIC_API_KEY" in str(exc.value)


# --------------------------------------------------------------------------- #
# Прогон
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_successful_run_sends_and_marks_published(digest):
    import fetcher

    bot = _bot()
    with patch.object(main, "_build_digest_blocking", return_value=main.BuildResult(digest, b"PNG")), patch.object(
        main.sender, "send_digest", new=AsyncMock(return_value=1)
    ) as send:
        result = await main.run_digest(bot, "-100")

    assert result.ok
    assert send.await_args.kwargs["image"] == b"PNG"
    conn = fetcher.connect()
    assert fetcher.is_published(conn, digest.articles[0])
    conn.close()


@pytest.mark.asyncio
async def test_send_failure_keeps_news_unpublished(digest):
    import fetcher

    with patch.object(main, "_build_digest_blocking", return_value=main.BuildResult(digest)), patch.object(
        main.sender, "send_digest", new=AsyncMock(side_effect=RuntimeError("нет сети"))
    ):
        result = await main.run_digest(_bot(), "-100")

    assert not result.ok and result.retryable
    conn = fetcher.connect()
    assert not fetcher.is_published(conn, digest.articles[0]), (
        "неотправленные новости должны попасть в следующий выпуск"
    )
    conn.close()


@pytest.mark.asyncio
async def test_no_news_is_not_a_failure():
    with patch.object(main, "_build_digest_blocking", return_value=main.BuildResult()):
        result = await main.run_digest(_bot(), "-100")
    assert result.ok and not result.retryable


@pytest.mark.asyncio
async def test_summarizer_error_is_retryable():
    with patch.object(
        main, "_build_digest_blocking", side_effect=summarizer.SummarizerError("API упал")
    ):
        result = await main.run_digest(_bot(), "-100")
    assert not result.ok and result.retryable


@pytest.mark.asyncio
async def test_dry_run_does_not_send(digest, tmp_path, monkeypatch):
    monkeypatch.setenv("DRY_RUN_IMAGE", str(tmp_path / "preview.png"))
    with patch.object(main, "_build_digest_blocking", return_value=main.BuildResult(digest, b"PNGDATA")), patch.object(
        main.sender, "send_digest", new=AsyncMock()
    ) as send:
        result = await main.run_digest(None, "-100", dry_run=True)

    assert result.ok
    send.assert_not_awaited()
    assert (tmp_path / "preview.png").read_bytes() == b"PNGDATA"


# --------------------------------------------------------------------------- #
# Повторы и уведомления
# --------------------------------------------------------------------------- #


def _context(bot, attempt: int | None = None) -> MagicMock:
    context = MagicMock()
    context.bot = bot
    context.job = MagicMock()
    context.job.data = {"attempt": attempt} if attempt is not None else None
    context.job_queue = MagicMock()
    return context


@pytest.mark.asyncio
async def test_scheduled_run_schedules_retry_and_alerts(monkeypatch):
    monkeypatch.setattr(main, "ADMIN_USER_IDS", {42})
    bot = _bot()
    context = _context(bot)
    failure = main.RunResult(False, "API недоступен", retryable=True)

    with patch.object(main, "run_digest", new=AsyncMock(return_value=failure)):
        await main.scheduled_digest(context)

    context.job_queue.run_once.assert_called_once()
    assert context.job_queue.run_once.call_args.kwargs["data"] == {"attempt": 1}
    bot.send_message.assert_awaited_once()
    assert "не вышел" in bot.send_message.await_args.kwargs["text"]


@pytest.mark.asyncio
async def test_retries_stop_after_limit(monkeypatch):
    monkeypatch.setattr(main, "ADMIN_USER_IDS", {42})
    monkeypatch.setattr(main, "MAX_RETRY_ATTEMPTS", 2)
    bot = _bot()
    context = _context(bot, attempt=2)
    failure = main.RunResult(False, "снова сбой", retryable=True)

    with patch.object(main, "run_digest", new=AsyncMock(return_value=failure)):
        await main.scheduled_digest(context)

    context.job_queue.run_once.assert_not_called()
    assert "Повторов больше не будет" in bot.send_message.await_args.kwargs["text"]


@pytest.mark.asyncio
async def test_successful_run_is_silent():
    bot = _bot()
    context = _context(bot)
    with patch.object(
        main, "run_digest", new=AsyncMock(return_value=main.RunResult(True, "ок"))
    ):
        await main.scheduled_digest(context)

    context.job_queue.run_once.assert_not_called()
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_news_does_not_wake_admins(monkeypatch):
    """Отсутствие новостей — штатная ситуация, а не повод для тревоги."""
    monkeypatch.setattr(main, "ADMIN_USER_IDS", {42})
    bot = _bot()
    context = _context(bot)
    with patch.object(main, "_build_digest_blocking", return_value=main.BuildResult()):
        await main.scheduled_digest(context)

    bot.send_message.assert_not_awaited()
    context.job_queue.run_once.assert_not_called()


@pytest.mark.asyncio
async def test_notify_admins_without_admins_configured(monkeypatch):
    monkeypatch.setattr(main, "ADMIN_USER_IDS", set())
    bot = _bot()
    await main.notify_admins(bot, "проблема")
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_feed_alerts_reach_admins(monkeypatch, digest):
    monkeypatch.setattr(main, "ADMIN_USER_IDS", {42})
    bot = _bot()
    build = main.BuildResult(digest, None, feed_alerts=[("Пенза-Обзор", 3, "404")])

    with patch.object(main, "_build_digest_blocking", return_value=build), patch.object(
        main.sender, "send_digest", new=AsyncMock(return_value=1)
    ):
        result = await main.run_digest(bot, "-100")

    assert result.ok
    bot.send_message.assert_awaited_once()
    text = bot.send_message.await_args.kwargs["text"]
    assert "Пенза-Обзор" in text and "404" in text


@pytest.mark.asyncio
async def test_digest_is_pinned_when_enabled(monkeypatch, digest):
    monkeypatch.setattr(main, "PIN_DIGEST", True)
    with patch.object(main, "_build_digest_blocking", return_value=main.BuildResult(digest)), patch.object(
        main.sender, "send_digest", new=AsyncMock(return_value=1)
    ) as send:
        await main.run_digest(_bot(), "-100")
    assert send.await_args.kwargs["pin"] is True


# --------------------------------------------------------------------------- #
# Срочные новости
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_breaking_publishes_and_marks(digest):
    import fetcher

    bot = _bot()
    with patch.object(main, "_breaking_blocking", return_value=(digest, None)), patch.object(
        main.sender, "send_digest", new=AsyncMock(return_value=1)
    ) as send:
        result = await main.run_breaking(bot, "-100")

    assert result.ok and "срочная" in result.message
    send.assert_awaited_once()
    assert send.await_args.kwargs.get("pin") is None, "срочные посты не закрепляем"

    conn = fetcher.connect()
    assert fetcher.is_published(conn, digest.articles[0])
    last = fetcher.stats(conn)["last_run"]
    assert last["status"] == "breaking"
    conn.close()


@pytest.mark.asyncio
async def test_breaking_silent_when_nothing_urgent():
    bot = _bot()
    with patch.object(main, "_breaking_blocking", return_value=None), patch.object(
        main.sender, "send_digest", new=AsyncMock()
    ) as send:
        result = await main.run_breaking(bot, "-100")

    assert result.ok
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_breaking_skipped_outside_window(monkeypatch):
    """Ночью бот молчит, даже если что-то произошло."""
    monkeypatch.setattr(main, "BREAKING_FROM_HOUR", 9)
    monkeypatch.setattr(main, "BREAKING_TO_HOUR", 21)

    class FakeDatetime(main.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 7, 30, 3, 0, tzinfo=tz)

    monkeypatch.setattr(main, "datetime", FakeDatetime)
    with patch.object(main, "run_breaking", new=AsyncMock()) as run:
        await main.scheduled_breaking(_context(_bot()))
    run.assert_not_awaited()


@pytest.mark.asyncio
async def test_breaking_runs_inside_window(monkeypatch):
    monkeypatch.setattr(main, "BREAKING_FROM_HOUR", 9)
    monkeypatch.setattr(main, "BREAKING_TO_HOUR", 21)

    class FakeDatetime(main.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 7, 30, 14, 0, tzinfo=tz)

    monkeypatch.setattr(main, "datetime", FakeDatetime)
    with patch.object(
        main, "run_breaking", new=AsyncMock(return_value=main.RunResult(True, "ок"))
    ) as run:
        await main.scheduled_breaking(_context(_bot()))
    run.assert_awaited_once()


def test_breaking_respects_daily_limit(monkeypatch, article_factory):
    import fetcher

    monkeypatch.setattr(main, "BREAKING_MAX_PER_DAY", 1)
    conn = fetcher.connect()
    fetcher.log_run(conn, "breaking", 1, "")
    conn.close()

    with patch.object(fetcher, "collect") as collect:
        assert main._breaking_blocking() is None
    collect.assert_not_called(), "лимит проверяется до обращения к лентам"


# --------------------------------------------------------------------------- #
# Секреты в логах
# --------------------------------------------------------------------------- #


def _format(record_msg: str, secrets=(), exc_info=None) -> str:
    formatter = main.RedactingFormatter("%(message)s", "%H:%M:%S", secrets=secrets)
    record = logging.LogRecord("t", logging.ERROR, __file__, 1, record_msg, (), exc_info)
    return formatter.format(record)


def test_known_secrets_are_redacted():
    out = _format("ключ sk-ant-secret12345 в тексте", secrets=("sk-ant-secret12345",))
    assert "sk-ant-secret12345" not in out and "***" in out


def test_bot_token_in_url_is_redacted_without_being_configured():
    """Токен приходит внутри URL в ошибках сети, даже если строка не в конфиге."""
    out = _format("POST https://api.telegram.org/bot123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw/sendMessage")
    assert "AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw" not in out
    assert "***" in out


def test_short_values_are_not_redacted():
    """Короткое значение затёрло бы пол-лога."""
    out = _format("сообщение про ok", secrets=("ok",))
    assert "сообщение про ok" == out


def test_traceback_is_redacted():
    try:
        raise RuntimeError("упало на sk-ant-secret12345")
    except RuntimeError:
        out = _format("ошибка", secrets=("sk-ant-secret12345",), exc_info=sys.exc_info())
    assert "sk-ant-secret12345" not in out
    assert "RuntimeError" in out


# --------------------------------------------------------------------------- #
# Наверстывание пропущенного выпуска
# --------------------------------------------------------------------------- #


def test_missed_digest_detected_after_scheduled_time(monkeypatch):
    monkeypatch.setattr(main, "DIGEST_TIME", "00:01")
    assert main._missed_todays_digest() is True


def test_not_missed_before_scheduled_time(monkeypatch):
    monkeypatch.setattr(main, "DIGEST_TIME", "23:59")
    assert main._missed_todays_digest() is False


def test_not_missed_when_digest_already_ran(monkeypatch):
    import fetcher

    monkeypatch.setattr(main, "DIGEST_TIME", "00:01")
    conn = fetcher.connect()
    fetcher.log_run(conn, "ok", 5, "")
    conn.close()
    assert main._missed_todays_digest() is False


def test_breaking_post_does_not_count_as_digest(monkeypatch):
    """Срочная новость выпуск не заменяет."""
    import fetcher

    monkeypatch.setattr(main, "DIGEST_TIME", "00:01")
    conn = fetcher.connect()
    fetcher.log_run(conn, "breaking", 1, "")
    conn.close()
    assert main._missed_todays_digest() is True


@pytest.mark.asyncio
async def test_catch_up_schedules_run(monkeypatch):
    monkeypatch.setattr(main, "CATCH_UP_MISSED", True)
    monkeypatch.setattr(main, "_missed_todays_digest", lambda: True)
    app = MagicMock()
    await main.catch_up(app)
    app.job_queue.run_once.assert_called_once()


@pytest.mark.asyncio
async def test_catch_up_disabled(monkeypatch):
    monkeypatch.setattr(main, "CATCH_UP_MISSED", False)
    app = MagicMock()
    await main.catch_up(app)
    app.job_queue.run_once.assert_not_called()


# --------------------------------------------------------------------------- #
# Предпросмотр с подтверждением
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def clear_pending():
    main._pending.clear()
    yield
    main._pending.clear()


def _preview_on(monkeypatch):
    monkeypatch.setattr(main, "PREVIEW_BEFORE_PUBLISH", True)
    monkeypatch.setattr(main, "ADMIN_USER_IDS", {42})


@pytest.mark.asyncio
async def test_preview_holds_publication(monkeypatch, digest):
    import fetcher

    _preview_on(monkeypatch)
    bot = _bot()
    queue = MagicMock()

    with patch.object(main, "_build_digest_blocking", return_value=main.BuildResult(digest)), patch.object(
        main.sender, "send_digest", new=AsyncMock()
    ) as send:
        result = await main.run_digest(bot, "-100", job_queue=queue)

    assert result.ok and "подтверждени" in result.message
    assert len(main._pending) == 1
    queue.run_once.assert_called_once()
    # Отправка была только админу, не в канал.
    assert all(call.args[1] == 42 for call in send.await_args_list)

    conn = fetcher.connect()
    assert not fetcher.is_published(conn, digest.articles[0]), "до подтверждения не публикуем"
    conn.close()


@pytest.mark.asyncio
async def test_preview_publishes_on_approval(monkeypatch, digest):
    import fetcher

    _preview_on(monkeypatch)
    bot = _bot()
    key = await main.send_preview(bot, digest, None)

    with patch.object(main.sender, "send_digest", new=AsyncMock(return_value=1)) as send:
        result = await main.publish_pending(bot, key)

    assert result.ok
    assert send.await_args.args[1] == main.CHAT_ID
    assert key not in main._pending
    conn = fetcher.connect()
    assert fetcher.is_published(conn, digest.articles[0])
    conn.close()


@pytest.mark.asyncio
async def test_cancel_keeps_news_for_next_issue(monkeypatch, digest):
    import fetcher

    _preview_on(monkeypatch)
    bot = _bot()
    key = await main.send_preview(bot, digest, None)

    query = MagicMock()
    query.data = f"skip:{key}"
    query.from_user.id = 42
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    update = MagicMock(callback_query=query)
    context = MagicMock(bot=bot)

    await main.on_preview_button(update, context)

    assert key not in main._pending
    conn = fetcher.connect()
    assert not fetcher.is_published(conn, digest.articles[0]), (
        "отменённые новости должны вернуться в следующий выпуск"
    )
    conn.close()


@pytest.mark.asyncio
async def test_preview_button_rejects_strangers(monkeypatch, digest):
    _preview_on(monkeypatch)
    key = await main.send_preview(_bot(), digest, None)

    query = MagicMock()
    query.data = f"pub:{key}"
    query.from_user.id = 999
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()

    await main.on_preview_button(MagicMock(callback_query=query), MagicMock(bot=_bot()))

    assert key in main._pending, "чужак не должен публиковать выпуск"
    assert query.answer.await_args.kwargs.get("show_alert") is True


@pytest.mark.asyncio
async def test_second_click_is_harmless(monkeypatch, digest):
    _preview_on(monkeypatch)
    bot = _bot()
    key = await main.send_preview(bot, digest, None)
    main._pending.pop(key)

    query = MagicMock()
    query.data = f"pub:{key}"
    query.from_user.id = 42
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()

    with patch.object(main.sender, "send_digest", new=AsyncMock()) as send:
        await main.on_preview_button(MagicMock(callback_query=query), MagicMock(bot=bot))
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_timeout_publishes_by_default(monkeypatch, digest):
    _preview_on(monkeypatch)
    monkeypatch.setattr(main, "PREVIEW_ON_TIMEOUT", "publish")
    bot = _bot()
    key = await main.send_preview(bot, digest, None)

    context = MagicMock(bot=bot)
    context.job.data = {"key": key}
    with patch.object(main.sender, "send_digest", new=AsyncMock(return_value=1)) as send:
        await main.preview_timed_out(context)

    assert send.await_args.args[1] == main.CHAT_ID
    assert key not in main._pending


@pytest.mark.asyncio
async def test_timeout_can_cancel_instead(monkeypatch, digest):
    _preview_on(monkeypatch)
    monkeypatch.setattr(main, "PREVIEW_ON_TIMEOUT", "cancel")
    bot = _bot()
    key = await main.send_preview(bot, digest, None)

    context = MagicMock(bot=bot)
    context.job.data = {"key": key}
    with patch.object(main.sender, "send_digest", new=AsyncMock()) as send:
        await main.preview_timed_out(context)

    send.assert_not_awaited()
    assert key not in main._pending


@pytest.mark.asyncio
async def test_preview_falls_open_without_admins(monkeypatch, digest):
    """Некому подтверждать — публикуем сразу: пропуск выпуска хуже."""
    monkeypatch.setattr(main, "PREVIEW_BEFORE_PUBLISH", True)
    monkeypatch.setattr(main, "ADMIN_USER_IDS", set())

    with patch.object(main, "_build_digest_blocking", return_value=main.BuildResult(digest)), patch.object(
        main.sender, "send_digest", new=AsyncMock(return_value=1)
    ) as send:
        result = await main.run_digest(_bot(), "-100", job_queue=MagicMock())

    assert result.ok and "отправлен" in result.message
    send.assert_awaited_once()
    assert not main._pending


@pytest.mark.asyncio
async def test_preview_falls_open_without_job_queue(monkeypatch, digest):
    _preview_on(monkeypatch)
    with patch.object(main, "_build_digest_blocking", return_value=main.BuildResult(digest)), patch.object(
        main.sender, "send_digest", new=AsyncMock(return_value=1)
    ) as send:
        result = await main.run_digest(_bot(), "-100", job_queue=None)

    assert result.ok
    send.assert_awaited_once()


# --------------------------------------------------------------------------- #
# Закрепление и бэкап
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_pinned_id_is_remembered_and_reused(digest):
    """Чтобы завтра было что откреплять."""
    with patch.object(main, "_build_digest_blocking", return_value=main.BuildResult(digest)), patch.object(
        main.sender,
        "send_digest",
        new=AsyncMock(return_value=main.sender.SendResult(messages=1, pinned_id=321)),
    ) as send:
        await main.run_digest(_bot(), "-100")

    assert send.await_args.kwargs["unpin"] is None, "в первый раз откреплять нечего"
    assert main._pinned_id() == 321

    with patch.object(main, "_build_digest_blocking", return_value=main.BuildResult(digest)), patch.object(
        main.sender,
        "send_digest",
        new=AsyncMock(return_value=main.sender.SendResult(messages=1, pinned_id=654)),
    ) as send:
        await main.run_digest(_bot(), "-100")

    assert send.await_args.kwargs["unpin"] == 321, "снимаем вчерашнее закрепление"
    assert main._pinned_id() == 654


def test_backup_writes_file(tmp_path, capsys):
    target = tmp_path / "copy.db"
    assert main.backup_db(str(target)) == 0
    assert target.exists()
    assert "Копия базы" in capsys.readouterr().out


def test_backup_reports_failure(tmp_path, capsys):
    assert main.backup_db(str(tmp_path / "нет" / "такой" / "папки" / "\0")) == 1


def test_old_backups_are_pruned(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "BACKUP_DIR", str(tmp_path))
    monkeypatch.setattr(main, "BACKUP_KEEP", 3)
    for i in range(6):
        main.backup_db(str(tmp_path / f"copy{i}.db"))

    assert len(list(tmp_path.glob("*.db"))) == 3


# --------------------------------------------------------------------------- #
# Доступ к командам
# --------------------------------------------------------------------------- #


def _update(user_id: int, chat_id: str) -> MagicMock:
    update = MagicMock()
    update.effective_user.id = user_id
    update.effective_chat.id = chat_id
    return update


def test_admin_list_controls_access(monkeypatch):
    monkeypatch.setattr(main, "ADMIN_USER_IDS", {42})
    assert main._is_allowed(_update(42, "-100"))
    assert not main._is_allowed(_update(7, "-100"))


def test_without_admins_only_work_chat_allowed(monkeypatch):
    monkeypatch.setattr(main, "ADMIN_USER_IDS", set())
    monkeypatch.setattr(main, "CHAT_ID", "-100")
    assert main._is_allowed(_update(7, "-100"))
    assert not main._is_allowed(_update(7, "-999"))
