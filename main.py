"""Telegram-бот ежедневного новостного дайджеста.

Запуск:
    python main.py             # бот с расписанием (ежедневно утром)
    python main.py --once      # собрать и отправить дайджест сейчас, затем выйти
    python main.py --dry-run   # собрать и напечатать в консоль, ничего не отправляя
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
import secrets as secrets_mod
import sys
from dataclasses import dataclass, field
from datetime import datetime, time as dtime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from typing import Sequence

from dotenv import load_dotenv

# Переменные окружения читаются на уровне модулей, поэтому .env грузим до импортов.
# override=True: .env — единственный источник настроек бота, и он должен побеждать
# случайные одноимённые переменные из окружения (их легко не заметить: настройка
# в .env молча не применится, а бот пойдёт не туда с невнятной ошибкой).
load_dotenv(override=True)

import extractor  # noqa: E402
import fetcher  # noqa: E402
import imagegen  # noqa: E402
import sender  # noqa: E402
import summarizer  # noqa: E402
from telegram import (  # noqa: E402
    Bot,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.ext import (  # noqa: E402
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

log = logging.getLogger("digest")

# --------------------------------------------------------------------------- #
# Конфигурация
# --------------------------------------------------------------------------- #

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
DIGEST_TIME = os.getenv("DIGEST_TIME", "08:00")
TIMEZONE = os.getenv("TIMEZONE", "Europe/Moscow")
MIN_ITEMS = int(os.getenv("MIN_ITEMS", "5"))
MAX_ITEMS = int(os.getenv("MAX_ITEMS", "8"))
MAX_AGE_HOURS = int(os.getenv("MAX_AGE_HOURS", "36"))
# Сколько кандидатов уходит модели на финальный отбор.
CANDIDATE_LIMIT = int(os.getenv("CANDIDATE_LIMIT", "20"))
# Не больше стольких заметок от одного издания в выпуске.
MAX_PER_SOURCE = int(os.getenv("MAX_PER_SOURCE", "2"))
# Повтор после неудачного планового прогона.
RETRY_MINUTES = int(os.getenv("RETRY_MINUTES", "30"))
MAX_RETRY_ATTEMPTS = int(os.getenv("MAX_RETRY_ATTEMPTS", "2"))
# На сколько минут опоздания планировщик всё ещё выполнит задание.
MISFIRE_GRACE_MINUTES = int(os.getenv("MISFIRE_GRACE_MINUTES", "60"))
# Догонять выпуск, пропущенный из-за простоя бота.
CATCH_UP_MISSED = os.getenv("CATCH_UP_MISSED", "1") not in ("0", "false", "False", "")
# Закреплять утренний дайджест в чате (нужны права администратора).
PIN_DIGEST = os.getenv("PIN_DIGEST", "1") not in ("0", "false", "False", "")

# --- Предпросмотр ---------------------------------------------------------
# Показывать выпуск администратору и ждать подтверждения перед публикацией.
PREVIEW_BEFORE_PUBLISH = os.getenv("PREVIEW_BEFORE_PUBLISH", "0") not in (
    "0",
    "false",
    "False",
    "",
)
PREVIEW_TIMEOUT_MINUTES = int(os.getenv("PREVIEW_TIMEOUT_MINUTES", "60"))
# Что делать, если админ не ответил: publish (по умолчанию) или cancel.
PREVIEW_ON_TIMEOUT = os.getenv("PREVIEW_ON_TIMEOUT", "publish").strip().lower()

# --- Срочные новости ------------------------------------------------------
BREAKING_ENABLED = os.getenv("BREAKING_ENABLED", "1") not in ("0", "false", "False", "")
BREAKING_EVERY_HOURS = int(os.getenv("BREAKING_EVERY_HOURS", "2"))
BREAKING_MAX_AGE_HOURS = int(os.getenv("BREAKING_MAX_AGE_HOURS", "4"))
BREAKING_MAX_PER_DAY = int(os.getenv("BREAKING_MAX_PER_DAY", "2"))
# Окно, в котором разрешено выходить вне расписания (по TIMEZONE).
BREAKING_FROM_HOUR = int(os.getenv("BREAKING_FROM_HOUR", "9"))
BREAKING_TO_HOUR = int(os.getenv("BREAKING_TO_HOUR", "21"))
# Резервные копии базы: куда складывать и сколько хранить.
BACKUP_DIR = os.getenv("BACKUP_DIR", "backups")
BACKUP_KEEP = int(os.getenv("BACKUP_KEEP", "14"))
LOG_FILE = os.getenv("LOG_FILE", "bot.log")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
ADMIN_USER_IDS = {
    int(x) for x in os.getenv("ADMIN_USER_IDS", "").replace(" ", "").split(",") if x.isdigit()
}

# Одновременно выполняется не более одного прогона.
_run_lock = asyncio.Lock()


# Шаблоны секретов на случай, если в лог попадёт строка, которой нет в конфиге
# (например, URL Telegram с токеном внутри сообщения об ошибке сети).
_SECRET_PATTERNS = (
    re.compile(r"bot\d{6,}:[A-Za-z0-9_-]{20,}"),
    re.compile(r"sk-ant-[A-Za-z0-9_-]{10,}"),
    re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{30,}\b"),  # голый токен бота
)


class RedactingFormatter(logging.Formatter):
    """Форматтер, вычищающий секреты из сообщений и трейсбеков.

    Сетевые ошибки Telegram несут внутри URL с токеном бота, а файл лога
    обычно лежит без всякой защиты. Затираем на выходе, а не в записи, —
    так под фильтр попадает и текст исключения.
    """

    def __init__(self, fmt: str, datefmt: str, secrets: Sequence[str] = ()):
        super().__init__(fmt, datefmt)
        # Слишком короткие значения не трогаем: затрут пол-лога.
        self._secrets = sorted({s for s in secrets if s and len(s) >= 8}, key=len, reverse=True)

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        for secret in self._secrets:
            text = text.replace(secret, "***")
        for pattern in _SECRET_PATTERNS:
            text = pattern.sub("***", text)
        return text


def setup_logging() -> None:
    formatter = RedactingFormatter(
        "%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        "%Y-%m-%d %H:%M:%S",
        secrets=(BOT_TOKEN, os.getenv("ANTHROPIC_API_KEY", ""),
                 os.getenv("FUSIONBRAIN_API_KEY", ""), os.getenv("FUSIONBRAIN_SECRET_KEY", "")),
    )
    file_handler = RotatingFileHandler(
        LOG_FILE, maxBytes=2_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(LOG_LEVEL)
    root.handlers.clear()
    root.addHandler(file_handler)
    root.addHandler(console)

    # Библиотеки шумят на INFO.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.INFO)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)


def check_config(require_telegram: bool = True) -> None:
    missing = []
    if require_telegram and not BOT_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")
    if require_telegram and not CHAT_ID:
        missing.append("TELEGRAM_CHAT_ID")
    if not os.getenv("ANTHROPIC_API_KEY"):
        missing.append("ANTHROPIC_API_KEY")
    if missing:
        raise SystemExit(
            "Не заданы переменные окружения: " + ", ".join(missing) + ". См. .env.example"
        )


def digest_time() -> dtime:
    """Время ежедневного запуска с учётом часового пояса из конфига."""
    try:
        hour, minute = (int(part) for part in DIGEST_TIME.split(":", 1))
    except ValueError:
        log.warning("Некорректное DIGEST_TIME=%r, использую 08:00", DIGEST_TIME)
        hour, minute = 8, 0

    tzinfo = None
    try:  # APScheduler внутри python-telegram-bot ожидает pytz-совместимую зону
        import pytz

        tzinfo = pytz.timezone(TIMEZONE)
    except Exception:  # noqa: BLE001 - любой сбой -> пробуем zoneinfo
        try:
            from zoneinfo import ZoneInfo

            tzinfo = ZoneInfo(TIMEZONE)
        except Exception:  # noqa: BLE001
            log.warning("Часовой пояс %s не найден, использую UTC", TIMEZONE)
            tzinfo = timezone.utc
    return dtime(hour=hour, minute=minute, tzinfo=tzinfo)


# --------------------------------------------------------------------------- #
# Основной сценарий
# --------------------------------------------------------------------------- #


@dataclass
class RunResult:
    """Итог прогона: успех, сообщение для человека и нужен ли повтор."""

    ok: bool
    message: str
    retryable: bool = False


@dataclass
class BuildResult:
    """Собранный выпуск и замечания к лентам, накопленные по пути."""

    digest: summarizer.Digest | None = None
    image: bytes | None = None
    feed_alerts: list[tuple[str, int, str]] = field(default_factory=list)


def _build_digest_blocking() -> BuildResult:
    """Синхронная часть прогона. digest=None — если публиковать нечего.

    Порядок: сбор кандидатов -> отбор моделью -> ограничение по источникам ->
    дочитывание статей -> пересказ -> картинка. Выполняется в отдельном
    потоке, чтобы не блокировать event loop бота.
    """
    conn = fetcher.connect()
    try:
        fetcher.purge_old(conn)
        collected = fetcher.collect(
            conn,
            min_items=MIN_ITEMS,
            limit=CANDIDATE_LIMIT,
            max_age_hours=MAX_AGE_HOURS,
        )
        candidates = collected.candidates
        alerts = collected.feed_alerts
        if not candidates:
            fetcher.log_run(conn, "empty", 0, "нет новых новостей")
            return BuildResult(feed_alerts=alerts)

        selected = summarizer.select_best(
            candidates, min_items=MIN_ITEMS, max_items=MAX_ITEMS
        )
        selected = fetcher.limit_per_source(selected, candidates, MAX_PER_SOURCE)
        if not selected:
            fetcher.log_run(conn, "empty", 0, "модель не отобрала ни одной новости")
            return BuildResult(feed_alerts=alerts)

        extractor.enrich(selected)
        digest = summarizer.build_digest(selected)
        image = imagegen.build_image(digest.title, digest.articles, digest.image_prompt)
        return BuildResult(digest=digest, image=image, feed_alerts=alerts)
    finally:
        conn.close()


PINNED_KEY = "last_pinned_message_id"


def _pinned_id() -> int | None:
    """id закреплённого дайджеста, чтобы снять его перед новым."""
    conn = fetcher.connect()
    try:
        value = fetcher.get_setting(conn, PINNED_KEY)
        return int(value) if value and value.isdigit() else None
    finally:
        conn.close()


def _remember_pinned(message_id: int | None) -> None:
    conn = fetcher.connect()
    try:
        fetcher.set_setting(conn, PINNED_KEY, message_id)
    finally:
        conn.close()


def _mark_blocking(articles: list[fetcher.Article], status: str, details: str = "") -> None:
    conn = fetcher.connect()
    try:
        if articles:
            fetcher.mark_published(conn, articles)
        fetcher.log_run(conn, status, len(articles), details)
    finally:
        conn.close()


def _preview_available(job_queue) -> bool:
    """Можно ли отправить выпуск на подтверждение.

    Если предпросмотр включён, но некому показывать или негде отсчитать
    таймаут, публикуем сразу: пропущенный выпуск хуже неотсмотренного.
    """
    if not PREVIEW_BEFORE_PUBLISH:
        return False
    if not ADMIN_USER_IDS:
        log.warning("Предпросмотр включён, но ADMIN_USER_IDS пусты — публикую сразу")
        return False
    if job_queue is None:
        log.warning("Предпросмотр недоступен без планировщика — публикую сразу")
        return False
    return True


async def run_digest(
    bot: Bot, chat_id: str | int, *, dry_run: bool = False, job_queue=None
) -> RunResult:
    """Полный цикл: собрать -> пересказать -> отправить -> запомнить."""
    if _run_lock.locked():
        return RunResult(False, "Дайджест уже собирается, подождите.")

    async with _run_lock:
        started = datetime.now(timezone.utc)
        log.info("=== Запуск дайджеста (%s) ===", "dry-run" if dry_run else "боевой")
        try:
            result = await asyncio.to_thread(_build_digest_blocking)
        except summarizer.SummarizerError as exc:
            log.error("Ошибка сборки дайджеста: %s", exc)
            await asyncio.to_thread(_mark_blocking, [], "error", str(exc))
            return RunResult(False, f"не удалось собрать дайджест: {exc}", retryable=True)
        except Exception as exc:  # noqa: BLE001 - прогон не должен ронять бота
            log.exception("Непредвиденная ошибка при сборке дайджеста")
            await asyncio.to_thread(_mark_blocking, [], "error", repr(exc))
            return RunResult(False, f"ошибка сборки: {exc}", retryable=True)

        if result.feed_alerts and bot is not None:
            lines = "\n".join(
                f"• {name}: {streak} прогонов подряд, {reason}"
                for name, streak, reason in result.feed_alerts
            )
            await notify_admins(bot, f"⚠️ Ленты не отвечают:\n{lines}")

        if result.digest is None:
            log.info("Новых новостей нет — публиковать нечего")
            # Это штатная ситуация, а не сбой: повторять и будить админов незачем.
            return RunResult(True, "свежих новостей не нашлось, дайджест не отправлен")

        digest, image = result.digest, result.image
        if dry_run:
            print(digest.text)
            if image:
                path = os.getenv("DRY_RUN_IMAGE", "digest-preview.png")
                with open(path, "wb") as handle:
                    handle.write(image)
                print(f"\n[картинка сохранена: {path}, {len(image) // 1024} КБ]")
            log.info(
                "Dry-run: пост собран (%d новостей), отправка пропущена",
                len(digest.articles),
            )
            return RunResult(
                True, f"dry-run: собрано {len(digest.articles)} новостей, ничего не отправлено"
            )

        if _preview_available(job_queue):
            key = await send_preview(bot, digest, image)
            job_queue.run_once(
                preview_timed_out,
                when=timedelta(minutes=PREVIEW_TIMEOUT_MINUTES),
                data={"key": key},
                name=f"preview-timeout-{key}",
            )
            log.info("Выпуск отправлен на подтверждение администратору")
            return RunResult(
                True, f"выпуск на подтверждении у администратора ({len(digest.articles)} новостей)"
            )

        try:
            previous_pin = await asyncio.to_thread(_pinned_id) if PIN_DIGEST else None
            sent = await sender.send_digest(
                bot,
                chat_id,
                digest.text,
                image=image,
                buttons=digest.buttons,
                pin=PIN_DIGEST,
                unpin=previous_pin,
            )
            if getattr(sent, "pinned_id", None):
                await asyncio.to_thread(_remember_pinned, sent.pinned_id)
        except Exception as exc:  # noqa: BLE001
            log.exception("Не удалось отправить дайджест")
            await asyncio.to_thread(_mark_blocking, [], "send_error", repr(exc))
            return RunResult(
                False, f"дайджест собран, но не отправлен: {exc}", retryable=True
            )

        # Помечаем опубликованным только после успешной отправки.
        await asyncio.to_thread(_mark_blocking, digest.articles, "ok")
        elapsed = (datetime.now(timezone.utc) - started).total_seconds()
        log.info("=== Готово: %d новостей за %.1f с ===", len(digest.articles), elapsed)
        return RunResult(True, f"дайджест отправлен: {len(digest.articles)} новостей")


def _breaking_blocking() -> tuple[summarizer.Digest, bytes | None] | None:
    """Ищет новость, ради которой стоит выйти вне расписания."""
    conn = fetcher.connect()
    try:
        today = datetime.now(timezone.utc).date().isoformat()
        already = conn.execute(
            "SELECT COUNT(*) AS c FROM runs WHERE status = 'breaking' AND started_at >= ?",
            (today,),
        ).fetchone()["c"]
        if already >= BREAKING_MAX_PER_DAY:
            log.info("Лимит срочных постов на сегодня исчерпан (%d)", already)
            return None

        collected = fetcher.collect(
            conn,
            min_items=1,
            limit=CANDIDATE_LIMIT,
            max_age_hours=BREAKING_MAX_AGE_HOURS,
        )
        urgent = fetcher.find_breaking(
            collected.candidates, max_age_hours=BREAKING_MAX_AGE_HOURS
        )
        if not urgent:
            return None

        log.info("Найдена срочная новость: %s", urgent[0].title[:90])
        extractor.enrich(urgent[:1])
        digest = summarizer.build_digest(urgent[:1])
        image = imagegen.build_image(digest.title, digest.articles, digest.image_prompt)
        return digest, image
    finally:
        conn.close()


async def run_breaking(bot: Bot, chat_id: str | int) -> RunResult:
    """Проверяет ленты между выпусками и публикует срочную новость."""
    if _run_lock.locked():
        return RunResult(True, "основной прогон занят, проверку пропускаю")

    async with _run_lock:
        try:
            found = await asyncio.to_thread(_breaking_blocking)
        except Exception as exc:  # noqa: BLE001 - фоновая проверка не критична
            log.exception("Ошибка при поиске срочных новостей")
            return RunResult(False, f"проверка срочных новостей не удалась: {exc}")

        if found is None:
            return RunResult(True, "срочных новостей нет")

        digest, image = found
        try:
            await sender.send_digest(
                bot, chat_id, digest.text, image=image, buttons=digest.buttons
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("Не удалось отправить срочную новость")
            return RunResult(False, f"срочная новость не отправлена: {exc}")

        await asyncio.to_thread(_mark_blocking, digest.articles, "breaking")
        log.info("Опубликована срочная новость")
        return RunResult(True, "опубликована срочная новость")


@dataclass
class PendingDigest:
    """Выпуск, ожидающий решения администратора."""

    digest: summarizer.Digest
    image: bytes | None
    created_at: datetime


# Ключ -> выпуск. Хранится в памяти: после перезапуска ожидающий
# подтверждения выпуск теряется и будет собран заново в следующий прогон.
_pending: dict[str, PendingDigest] = {}


async def send_preview(bot: Bot, digest: summarizer.Digest, image: bytes | None) -> str:
    """Отправляет выпуск админам и возвращает ключ ожидания."""
    key = secrets_mod.token_urlsafe(8)
    _pending[key] = PendingDigest(digest, image, datetime.now(timezone.utc))

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Опубликовать", callback_data=f"pub:{key}"),
                InlineKeyboardButton("✖️ Отменить", callback_data=f"skip:{key}"),
            ]
        ]
    )
    head = (
        f"Предпросмотр выпуска ({len(digest.articles)} новостей). "
        "Так он уйдёт в канал:"
    )
    for admin_id in sorted(ADMIN_USER_IDS):
        await sender.send_notice(bot, [admin_id], head)
        await sender.send_digest(
            bot, admin_id, digest.text, image=image, buttons=digest.buttons
        )
        await bot.send_message(
            chat_id=admin_id,
            text=f"Публикуем? Без ответа через {PREVIEW_TIMEOUT_MINUTES} мин — "
            + ("выпуск уйдёт сам." if PREVIEW_ON_TIMEOUT == "publish" else "выпуск отменится."),
            reply_markup=keyboard,
        )
    return key


async def publish_pending(bot: Bot, key: str, reason: str = "") -> RunResult:
    """Публикует выпуск, ожидавший подтверждения."""
    pending = _pending.pop(key, None)
    if pending is None:
        return RunResult(True, "выпуск уже обработан")

    digest = pending.digest
    try:
        previous_pin = await asyncio.to_thread(_pinned_id) if PIN_DIGEST else None
        sent = await sender.send_digest(
            bot,
            CHAT_ID,
            digest.text,
            image=pending.image,
            buttons=digest.buttons,
            pin=PIN_DIGEST,
            unpin=previous_pin,
        )
        if getattr(sent, "pinned_id", None):
            await asyncio.to_thread(_remember_pinned, sent.pinned_id)
    except Exception as exc:  # noqa: BLE001
        log.exception("Не удалось отправить подтверждённый выпуск")
        await asyncio.to_thread(_mark_blocking, [], "send_error", repr(exc))
        return RunResult(False, f"выпуск не отправлен: {exc}", retryable=True)

    await asyncio.to_thread(_mark_blocking, digest.articles, "ok")
    log.info("Выпуск опубликован%s: %d новостей", reason, len(digest.articles))
    return RunResult(True, f"дайджест отправлен: {len(digest.articles)} новостей")


async def on_preview_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обрабатывает нажатие «Опубликовать» / «Отменить»."""
    query = update.callback_query
    action, _, key = (query.data or "").partition(":")

    if ADMIN_USER_IDS and query.from_user.id not in ADMIN_USER_IDS:
        await query.answer("Только для администраторов", show_alert=True)
        return

    if key not in _pending:
        await query.answer("Этот выпуск уже обработан")
        await query.edit_message_text("Выпуск уже обработан.")
        return

    if action == "pub":
        await query.answer("Публикую…")
        result = await publish_pending(context.bot, key, reason=" по кнопке")
        await query.edit_message_text(
            "✅ Опубликовано." if result.ok else f"Не удалось: {result.message}"
        )
    else:
        pending = _pending.pop(key, None)
        await query.answer("Отменено")
        await query.edit_message_text("✖️ Выпуск отменён, новости остались в очереди.")
        if pending is not None:
            # Новости НЕ помечаем опубликованными — вернутся в следующий выпуск.
            await asyncio.to_thread(_mark_blocking, [], "cancelled", "отменён админом")


async def preview_timed_out(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Админ не ответил — поступаем по настройке PREVIEW_ON_TIMEOUT."""
    key = (context.job.data or {}).get("key", "")
    if key not in _pending:
        return

    if PREVIEW_ON_TIMEOUT == "cancel":
        _pending.pop(key, None)
        log.warning("Подтверждение не получено — выпуск отменён")
        await notify_admins(context.bot, "✖️ Подтверждение не получено, выпуск отменён.")
        return

    log.warning("Подтверждение не получено — публикую без него")
    result = await publish_pending(context.bot, key, reason=" по таймауту")
    await notify_admins(
        context.bot,
        "Подтверждение не получено, "
        + ("выпуск опубликован автоматически." if result.ok else f"сбой: {result.message}"),
    )


async def notify_admins(bot: Bot, text: str) -> None:
    """Пишет о проблеме администраторам.

    В общий чат такие сообщения не уходят: подписчикам они не нужны.
    """
    if not ADMIN_USER_IDS:
        log.warning("ADMIN_USER_IDS не заданы — некому сообщить: %s", text)
        return
    await sender.send_notice(bot, ADMIN_USER_IDS, text)


# --------------------------------------------------------------------------- #
# Обработчики Telegram
# --------------------------------------------------------------------------- #


def _is_allowed(update: Update) -> bool:
    user = update.effective_user
    chat = update.effective_chat
    if ADMIN_USER_IDS:
        return bool(user and user.id in ADMIN_USER_IDS)
    # Список админов не задан — принимаем команды только из рабочего чата.
    return bool(chat and str(chat.id) == str(CHAT_ID))


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Бот собирает ежедневный дайджест: рынок такси (Пенза и Россия), "
        "авто-тематика и происшествия в Пензе.\n\n"
        f"Расписание: каждый день в {DIGEST_TIME} ({TIMEZONE}).\n"
        "Команды: /digest — собрать сейчас, /status — состояние, /help — справка."
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "/digest — собрать и отправить дайджест вне расписания\n"
        "/status — когда был последний прогон и сколько новостей в базе\n"
        "/chatid — показать id текущего чата (для настройки TELEGRAM_CHAT_ID)\n"
        "/help — эта справка"
    )


async def cmd_chatid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    await update.message.reply_text(f"chat_id: {chat.id}")


def _next_run_text(context: ContextTypes.DEFAULT_TYPE) -> str:
    """Время следующего планового запуска.

    Job.next_t доступен только после того, как планировщик поставил задачу
    в очередь, — до этого обращение к нему бросает AttributeError.
    """
    queue = context.application.job_queue
    if queue is None:
        return "расписание отключено"
    jobs = queue.get_jobs_by_name("daily-digest")
    if not jobs:
        return "—"
    try:
        next_t = jobs[0].next_t
    except AttributeError:
        return "ещё не запланирован"
    return next_t.strftime("%Y-%m-%d %H:%M %Z") if next_t else "—"


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    def _stats() -> dict:
        conn = fetcher.connect()
        try:
            return fetcher.stats(conn)
        finally:
            conn.close()

    data = await asyncio.to_thread(_stats)
    last = data["last_run"]
    last_text = (
        f"{last['started_at']} — {last['status']} ({last['items']} новостей)"
        if last
        else "прогонов ещё не было"
    )
    await update.message.reply_text(
        f"Опубликовано всего: {data['published_total']}\n"
        f"Последний прогон: {last_text}\n"
        f"Следующий по расписанию: {_next_run_text(context)}"
    )


async def cmd_digest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        await update.message.reply_text("Команда доступна только администраторам бота.")
        return
    await update.message.reply_text("Собираю дайджест, это займёт до минуты…")
    result = await run_digest(context.bot, CHAT_ID, job_queue=context.job_queue)
    await update.message.reply_text(result.message.capitalize())


async def scheduled_digest(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Плановый прогон: при сбое сообщает админам и планирует повтор."""
    result = await run_digest(context.bot, CHAT_ID, job_queue=context.job_queue)
    log.info("Плановый прогон: %s", result.message)
    if result.ok:
        return

    attempt = 0
    if context.job is not None and isinstance(context.job.data, dict):
        attempt = int(context.job.data.get("attempt", 0))

    if result.retryable and attempt < MAX_RETRY_ATTEMPTS and RETRY_MINUTES > 0:
        context.job_queue.run_once(
            scheduled_digest,
            when=timedelta(minutes=RETRY_MINUTES),
            data={"attempt": attempt + 1},
            name=f"digest-retry-{attempt + 1}",
        )
        tail = (
            f"Повтор через {RETRY_MINUTES} мин "
            f"(попытка {attempt + 1} из {MAX_RETRY_ATTEMPTS})."
        )
    else:
        tail = "Повторов больше не будет — нужно вмешательство."

    log.warning("Плановый прогон не удался. %s", tail)
    await notify_admins(context.bot, f"⚠️ Дайджест не вышел: {result.message}. {tail}")


def _missed_todays_digest() -> bool:
    """Пора ли наверстать сегодняшний выпуск (проверяется при старте)."""
    tzinfo = digest_time().tzinfo
    now = datetime.now(tzinfo)
    scheduled = now.replace(
        hour=digest_time().hour, minute=digest_time().minute, second=0, microsecond=0
    )
    if now < scheduled:
        return False  # время публикации ещё не пришло

    conn = fetcher.connect()
    try:
        return not fetcher.had_digest_today(conn, tzinfo)
    finally:
        conn.close()


async def catch_up(application: Application) -> None:
    """Догоняет выпуск, пропущенный из-за простоя бота.

    Планировщик просроченные задания не выполняет: если сервер перезагрузился
    в момент публикации, дайджест за день молча не вышел бы вовсе.
    """
    if not CATCH_UP_MISSED:
        return
    try:
        missed = await asyncio.to_thread(_missed_todays_digest)
    except Exception:  # noqa: BLE001 - проверка не должна мешать запуску
        log.exception("Не удалось проверить пропущенный выпуск")
        return

    if missed:
        log.warning("Сегодняшний выпуск пропущен — запускаю с опозданием")
        application.job_queue.run_once(
            scheduled_digest, when=timedelta(seconds=30), name="digest-catch-up"
        )


async def scheduled_breaking(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Проверка срочных новостей между выпусками — только в дневном окне."""
    hour = datetime.now(digest_time().tzinfo).hour
    if not BREAKING_FROM_HOUR <= hour < BREAKING_TO_HOUR:
        log.debug("Вне окна срочных новостей (%d ч) — пропускаю", hour)
        return
    result = await run_breaking(context.bot, CHAT_ID)
    log.info("Проверка срочных новостей: %s", result.message)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("Ошибка в обработчике", exc_info=context.error)


# --------------------------------------------------------------------------- #
# Точки входа
# --------------------------------------------------------------------------- #


def run_bot() -> None:
    check_config()
    application = Application.builder().token(BOT_TOKEN).post_init(catch_up).build()

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(CommandHandler("chatid", cmd_chatid))
    application.add_handler(CommandHandler("status", cmd_status))
    application.add_handler(CommandHandler("digest", cmd_digest))
    application.add_handler(CallbackQueryHandler(on_preview_button, pattern=r"^(pub|skip):"))
    application.add_error_handler(on_error)

    if application.job_queue is None:
        raise SystemExit(
            "JobQueue недоступна. Установите зависимости: "
            'pip install "python-telegram-bot[job-queue]"'
        )
    application.job_queue.run_daily(
        scheduled_digest,
        time=digest_time(),
        name="daily-digest",
        # Планировщик по умолчанию пропускает задание, просроченное больше чем
        # на секунду: перезагрузка сервера в момент публикации стоила бы выпуска.
        job_kwargs={"misfire_grace_time": MISFIRE_GRACE_MINUTES * 60},
    )
    log.info("Бот запущен. Дайджест ежедневно в %s (%s)", DIGEST_TIME, TIMEZONE)

    if BREAKING_ENABLED and BREAKING_EVERY_HOURS > 0:
        application.job_queue.run_repeating(
            scheduled_breaking,
            interval=timedelta(hours=BREAKING_EVERY_HOURS),
            first=timedelta(minutes=5),
            name="breaking-check",
        )
        log.info(
            "Проверка срочных новостей: раз в %d ч, окно %02d:00–%02d:00, "
            "не более %d постов в сутки",
            BREAKING_EVERY_HOURS,
            BREAKING_FROM_HOUR,
            BREAKING_TO_HOUR,
            BREAKING_MAX_PER_DAY,
        )

    application.run_polling(allowed_updates=Update.ALL_TYPES)


async def run_once(dry_run: bool = False) -> int:
    """Разовый прогон. Возвращает код выхода процесса."""
    check_config(require_telegram=not dry_run)
    if dry_run:
        result = await run_digest(None, CHAT_ID, dry_run=True)  # type: ignore[arg-type]
    else:
        bot = Bot(token=BOT_TOKEN)
        async with bot:
            result = await run_digest(bot, CHAT_ID)
            if not result.ok:
                await notify_admins(bot, f"⚠️ Дайджест не вышел: {result.message}")
    log.info("Итог: %s", result.message)
    return 0 if result.ok else 1


def check_feeds() -> int:
    """Разовая проверка всех источников: адрес, число записей, ошибка."""
    all_feeds = list(fetcher.FEEDS) + list(fetcher.FALLBACK_FEEDS)
    # Сайты без RSS проверяем вместе с лентами: они такой же источник,
    # и вёрстка у них меняется чаще, чем адреса лент.
    results = fetcher.fetch_feeds(all_feeds) + fetcher.fetch_sites()

    name_width = max(len(r.feed.name) for r in results)
    broken = 0
    print(f"\n{'ИСТОЧНИК'.ljust(name_width)}  ЗАПИСЕЙ  ПОДХОДИТ  СОСТОЯНИЕ")
    print("-" * (name_width + 34))
    for result in sorted(results, key=lambda r: (not r.broken, r.feed.name)):
        state = "OK"
        if result.error:
            state = f"ОШИБКА: {result.error[:60]}"
            broken += 1
        elif result.entries_total == 0:
            state = "ПУСТО — проверьте адрес"
            broken += 1
        print(
            f"{result.feed.name.ljust(name_width)}  "
            f"{result.entries_total:>7}  {len(result.articles):>8}  {state}"
        )

    print(f"\nВсего источников: {len(results)}, с проблемами: {broken}")
    if not broken:
        print("Все источники отвечают.")

    # Заодно обновляем статистику, чтобы бот не слал предупреждение
    # о ленте, которую вы только что починили.
    conn = fetcher.connect()
    try:
        fetcher.record_feed_health(conn, results)
    finally:
        conn.close()
    return 1 if broken else 0


def backup_db(destination: str | None = None) -> int:
    """Сохраняет копию базы. Путь по умолчанию — с датой в имени."""
    if not destination:
        stamp = datetime.now(digest_time().tzinfo).strftime("%Y%m%d-%H%M%S")
        base = os.path.splitext(os.path.basename(fetcher.DB_PATH))[0]
        destination = os.path.join(BACKUP_DIR, f"{base}-{stamp}.db")

    os.makedirs(os.path.dirname(os.path.abspath(destination)), exist_ok=True)
    try:
        fetcher.backup(destination)
    except Exception as exc:  # noqa: BLE001
        log.error("Резервная копия не создана: %s", exc)
        print(f"Не удалось создать копию: {exc}")
        return 1

    size = os.path.getsize(destination)
    log.info("Резервная копия базы: %s (%d КБ)", destination, size // 1024)
    print(f"Копия базы: {destination} ({size // 1024} КБ)")
    _prune_backups()
    return 0


def _prune_backups() -> None:
    """Оставляет последние BACKUP_KEEP копий, старые удаляет."""
    if BACKUP_KEEP <= 0 or not os.path.isdir(BACKUP_DIR):
        return
    copies = sorted(
        (os.path.join(BACKUP_DIR, name) for name in os.listdir(BACKUP_DIR) if name.endswith(".db")),
        key=os.path.getmtime,
        reverse=True,
    )
    for stale in copies[BACKUP_KEEP:]:
        try:
            os.remove(stale)
            log.info("Удалена старая копия базы: %s", stale)
        except OSError as exc:
            log.warning("Не удалось удалить %s: %s", stale, exc)


def main() -> None:
    parser = argparse.ArgumentParser(description="Telegram-бот новостного дайджеста")
    parser.add_argument(
        "--once", action="store_true", help="собрать и отправить дайджест сейчас, затем выйти"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="собрать и напечатать пост, ничего не отправляя"
    )
    parser.add_argument(
        "--check-feeds", action="store_true", help="проверить доступность всех лент и выйти"
    )
    parser.add_argument(
        "--backup",
        nargs="?",
        const="",
        metavar="ПУТЬ",
        help="сделать резервную копию базы и выйти",
    )
    args = parser.parse_args()

    setup_logging()
    if args.backup is not None:
        raise SystemExit(backup_db(args.backup or None))
    if args.check_feeds:
        raise SystemExit(check_feeds())
    if args.dry_run:
        raise SystemExit(asyncio.run(run_once(dry_run=True)))
    if args.once:
        raise SystemExit(asyncio.run(run_once()))
    run_bot()


if __name__ == "__main__":
    main()
