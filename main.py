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
import sys
from datetime import datetime, time as dtime, timezone
from logging.handlers import RotatingFileHandler

from dotenv import load_dotenv

# Переменные окружения читаются на уровне модулей, поэтому .env грузим до импортов.
load_dotenv()

import fetcher  # noqa: E402
import sender  # noqa: E402
import summarizer  # noqa: E402
from telegram import Bot, Update  # noqa: E402
from telegram.ext import Application, CommandHandler, ContextTypes  # noqa: E402

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
LOG_FILE = os.getenv("LOG_FILE", "bot.log")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
ADMIN_USER_IDS = {
    int(x) for x in os.getenv("ADMIN_USER_IDS", "").replace(" ", "").split(",") if x.isdigit()
}

# Одновременно выполняется не более одного прогона.
_run_lock = asyncio.Lock()


def setup_logging() -> None:
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-8s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S"
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


def _build_digest_blocking() -> tuple[str, list[fetcher.Article]] | None:
    """Синхронная часть: сбор новостей + обращение к Anthropic.

    Возвращает (текст поста, вошедшие новости) либо None, если новостей нет.
    Выполняется в отдельном потоке, чтобы не блокировать event loop.
    """
    conn = fetcher.connect()
    try:
        fetcher.purge_old(conn)
        articles = fetcher.collect(
            conn,
            min_items=MIN_ITEMS,
            max_items=MAX_ITEMS,
            max_age_hours=MAX_AGE_HOURS,
        )
        if not articles:
            fetcher.log_run(conn, "empty", 0, "нет новых новостей")
            return None
        text, used = summarizer.build_digest(articles)
        return text, used
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


async def run_digest(bot: Bot, chat_id: str | int, *, dry_run: bool = False) -> str:
    """Полный цикл: собрать -> пересказать -> отправить -> запомнить.

    Возвращает короткий человекочитаемый статус.
    """
    if _run_lock.locked():
        return "Дайджест уже собирается, подождите."

    async with _run_lock:
        started = datetime.now(timezone.utc)
        log.info("=== Запуск дайджеста (%s) ===", "dry-run" if dry_run else "боевой")
        try:
            result = await asyncio.to_thread(_build_digest_blocking)
        except summarizer.SummarizerError as exc:
            log.error("Ошибка сборки дайджеста: %s", exc)
            await asyncio.to_thread(_mark_blocking, [], "error", str(exc))
            return f"Не удалось собрать дайджест: {exc}"
        except Exception as exc:  # noqa: BLE001 - прогон не должен ронять бота
            log.exception("Непредвиденная ошибка при сборке дайджеста")
            await asyncio.to_thread(_mark_blocking, [], "error", repr(exc))
            return f"Ошибка: {exc}"

        if result is None:
            log.info("Новых новостей нет — публиковать нечего")
            return "Свежих новостей не нашлось — дайджест не отправлен."

        text, used = result
        if dry_run:
            print(text)
            log.info("Dry-run: пост собран (%d новостей), отправка пропущена", len(used))
            return f"Dry-run: собрано {len(used)} новостей, ничего не отправлено."

        try:
            await sender.send_digest(bot, chat_id, text)
        except Exception as exc:  # noqa: BLE001
            log.exception("Не удалось отправить дайджест")
            await asyncio.to_thread(_mark_blocking, [], "send_error", repr(exc))
            return f"Дайджест собран, но не отправлен: {exc}"

        # Помечаем опубликованным только после успешной отправки.
        await asyncio.to_thread(_mark_blocking, used, "ok")
        elapsed = (datetime.now(timezone.utc) - started).total_seconds()
        log.info("=== Готово: %d новостей за %.1f с ===", len(used), elapsed)
        return f"Дайджест отправлен: {len(used)} новостей."


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
    status = await run_digest(context.bot, CHAT_ID)
    await update.message.reply_text(status)


async def scheduled_digest(context: ContextTypes.DEFAULT_TYPE) -> None:
    status = await run_digest(context.bot, CHAT_ID)
    log.info("Плановый прогон: %s", status)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("Ошибка в обработчике", exc_info=context.error)


# --------------------------------------------------------------------------- #
# Точки входа
# --------------------------------------------------------------------------- #


def run_bot() -> None:
    check_config()
    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(CommandHandler("chatid", cmd_chatid))
    application.add_handler(CommandHandler("status", cmd_status))
    application.add_handler(CommandHandler("digest", cmd_digest))
    application.add_error_handler(on_error)

    if application.job_queue is None:
        raise SystemExit(
            "JobQueue недоступна. Установите зависимости: "
            'pip install "python-telegram-bot[job-queue]"'
        )
    application.job_queue.run_daily(scheduled_digest, time=digest_time(), name="daily-digest")

    log.info("Бот запущен. Дайджест ежедневно в %s (%s)", DIGEST_TIME, TIMEZONE)
    application.run_polling(allowed_updates=Update.ALL_TYPES)


async def run_once(dry_run: bool = False) -> None:
    check_config(require_telegram=not dry_run)
    if dry_run:
        status = await run_digest(None, CHAT_ID, dry_run=True)  # type: ignore[arg-type]
        log.info(status)
        return
    bot = Bot(token=BOT_TOKEN)
    async with bot:
        status = await run_digest(bot, CHAT_ID)
    log.info(status)


def main() -> None:
    parser = argparse.ArgumentParser(description="Telegram-бот новостного дайджеста")
    parser.add_argument(
        "--once", action="store_true", help="собрать и отправить дайджест сейчас, затем выйти"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="собрать и напечатать пост, ничего не отправляя"
    )
    args = parser.parse_args()

    setup_logging()
    if args.dry_run:
        asyncio.run(run_once(dry_run=True))
    elif args.once:
        asyncio.run(run_once())
    else:
        run_bot()


if __name__ == "__main__":
    main()
