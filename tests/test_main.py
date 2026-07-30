"""Оркестрация прогона: успех, сбои, повторы и уведомления."""

from __future__ import annotations

import os
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
