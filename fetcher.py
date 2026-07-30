"""Сбор новостей из RSS-источников, классификация по темам и дедупликация (SQLite).

Модуль ничего не публикует: он только отдаёт список отобранных новостей и ведёт
базу уже опубликованного, чтобы одна и та же новость не попала в два дайджеста.
"""

from __future__ import annotations

import hashlib
import html
import logging
import os
import re
import sqlite3
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable, Sequence
from urllib.parse import parse_qsl, quote_plus, urlencode, urlparse, urlunparse

import feedparser
import requests

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Темы
# --------------------------------------------------------------------------- #

TOPIC_TAXI = "taxi"
TOPIC_AUTO = "auto"
TOPIC_INCIDENT = "incident"

# Порядок задаёт порядок блоков в готовом дайджесте.
TOPIC_ORDER = (TOPIC_TAXI, TOPIC_AUTO, TOPIC_INCIDENT)

TOPIC_TITLES = {
    TOPIC_TAXI: "🚕 Рынок такси",
    TOPIC_AUTO: "🚗 Авто и авторынок",
    TOPIC_INCIDENT: "🚨 Происшествия в Пензе",
}

# --------------------------------------------------------------------------- #
# Источники
# --------------------------------------------------------------------------- #

_GOOGLE_NEWS = "https://news.google.com/rss/search?q={query}&hl=ru&gl=RU&ceid=RU:ru"


def google_news(query: str) -> str:
    """RSS-лента поисковой выдачи Google Новостей (используется как фолбэк
    и как способ читать издания без собственного RSS)."""
    return _GOOGLE_NEWS.format(query=quote_plus(query))


@dataclass(frozen=True)
class Feed:
    name: str
    url: str
    topic: str | None = None  # None -> тему определяем по ключевым словам
    region: str | None = None  # "penza" -> заведомо пензенский источник
    tier: str = "primary"  # primary | fallback


# Основные ленты. У penzasmi.ru, riapo.ru и penzainform.ru собственного RSS нет,
# поэтому они читаются через site-запрос к Google Новостям (см. README).
FEEDS: tuple[Feed, ...] = (
    Feed("Пенза-Обзор", "https://penzaobzor.ru/rss", region="penza"),
    Feed("Пронедра/Progorod58", "https://progorod58.ru/rss", region="penza"),
    Feed("Пензенская правда", "https://pravda-news.ru/rss", region="penza"),
    Feed("ПензаСМИ", google_news("site:penzasmi.ru"), region="penza"),
    Feed("РИА Пензенской области", google_news("site:riapo.ru"), region="penza"),
    Feed("PenzaInform", google_news("site:penzainform.ru"), region="penza"),
    Feed("АвтоСтат", "https://www.autostat.ru/news/rss/", topic=TOPIC_AUTO),
    Feed("Kolesa.ru", "https://www.kolesa.ru/feed", topic=TOPIC_AUTO),
    Feed("5 колесо", "https://5koleso.ru/feed/", topic=TOPIC_AUTO),
    Feed("ТАСС", "https://tass.ru/rss/v2.xml"),
    Feed("Лента.ру", "https://lenta.ru/rss/news"),
)

# Фолбэк: подключается, только если основных лент не хватило на минимум новостей.
FALLBACK_FEEDS: tuple[Feed, ...] = (
    Feed(
        "Поиск: такси в Пензенской области",
        google_news('такси "Пензенская область" OR Пенза'),
        topic=TOPIC_TAXI,
        region="penza",
        tier="fallback",
    ),
    Feed(
        "Поиск: рынок такси в России",
        google_news('"рынок такси" OR "закон о такси" OR таксопарк Россия'),
        topic=TOPIC_TAXI,
        tier="fallback",
    ),
    Feed(
        "Поиск: авторынок и новые модели",
        google_news('авторынок OR "новая модель" OR локализация автомобилей Россия'),
        topic=TOPIC_AUTO,
        tier="fallback",
    ),
    Feed(
        "Поиск: происшествия в Пензе",
        google_news("Пенза ДТП OR происшествие OR пожар"),
        topic=TOPIC_INCIDENT,
        region="penza",
        tier="fallback",
    ),
)

# --------------------------------------------------------------------------- #
# Ключевые слова (по основам слов, регистр не важен)
# --------------------------------------------------------------------------- #

# Ключевые слова темы «такси» намеренно узкие: общие слова вроде «перевозчик»
# дают ложные срабатывания на новостях о ЖД и авиабилетах.
TAXI_KEYWORDS = (
    "такси",
    "таксист",
    "таксопарк",
    "таксомотор",
    "яндекс go",
    "яндекс.go",
    "ситимобил",
    "агрегатор поездок",
    "каршеринг",
    "райдхейл",
)

AUTO_KEYWORDS = (
    "автомобил",
    "авторынок",
    "автопром",
    "автоваз",
    "автодилер",
    "автосалон",
    "автоконцерн",
    "кроссовер",
    "седан",
    "внедорожник",
    "электромобил",
    "локализац",
    "утильсбор",
    "автокредит",
    "lada",
    "лада",
    "москвич",
    "haval",
    "chery",
    "geely",
    "omoda",
    "belgee",
    "solaris",
    "новая модель",
    "автозавод",
)

INCIDENT_KEYWORDS = (
    "дтп",
    "авари",
    "столкнул",
    "сбил",
    "наезд",
    "пожар",
    "возгоран",
    "сгорел",
    "сгорев",
    "загорел",
    "тушени",
    "погиб",
    "пострадал",
    "скончал",
    "происшеств",
    "задержан",
    "поджог",
    "взрыв",
    "утонул",
    "мошенник",
    "кража",
    "уголовное дело",
    "следственн",
    "спасател",
    "мчс",
)

# Признаки серьёзного ЧП: ради такого имеет смысл выйти вне расписания.
# Обычное ДТП или бытовой пожар сюда намеренно не попадают.
BREAKING_KEYWORDS = (
    "погиб",
    "взрыв",
    "эвакуац",
    "чрезвычайн",
    "режим чс",
    "обрушен",
    "беспилотник",
    "атака дрон",
    "массов",
    "крупный пожар",
    "введен режим",
    "перекрыт",
    "отключен",
    "пропал ребенок",
    "разыскивают ребенка",
)

# Признаки того, что заметка про РЫНОК, а не про случившееся на дороге.
# Нужны, чтобы отличить «отзыв партии машин после аварий» (рынок, берём)
# от «в другом регионе машина столкнулась с локомотивом» (чужое ДТП, не берём).
MARKET_KEYWORDS = (
    "рынок",
    "продаж",
    "цен",
    "подорожа",
    "подешеве",
    "модел",
    "завод",
    "конвейер",
    "локализац",
    "отзыв",
    "закон",
    "тариф",
    "пошлин",
    "утильсбор",
    "спрос",
    "выпуск",
    "премьер",
    "комплектац",
    "лицензи",
    "штраф",
)

PENZA_KEYWORDS = (
    "пенз",
    "заречн",
    "кузнецк",
    "никольск",
    "сердобск",
    "каменк",
    "бессонов",
    "нижнеломов",
    "58 регион",
)

_TOPIC_KEYWORDS = {
    TOPIC_TAXI: TAXI_KEYWORDS,
    TOPIC_AUTO: AUTO_KEYWORDS,
    TOPIC_INCIDENT: INCIDENT_KEYWORDS,
}

# --------------------------------------------------------------------------- #
# Настройки из окружения
# --------------------------------------------------------------------------- #

DB_PATH = os.getenv("DB_PATH", "digest.db")
# За сколько дней сверять сюжеты уже опубликованных новостей.
STORY_DEDUP_DAYS = int(os.getenv("STORY_DEDUP_DAYS", "3"))
# После скольких неудачных прогонов подряд предупреждать о сломанной ленте.
FEED_ALERT_AFTER = int(os.getenv("FEED_ALERT_AFTER", "3"))
HTTP_TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "15"))
USER_AGENT = os.getenv(
    "USER_AGENT",
    "Mozilla/5.0 (compatible; NewsDigestBot/1.0; +https://github.com/)",
)
KEEP_HISTORY_DAYS = int(os.getenv("KEEP_HISTORY_DAYS", "120"))

_TRACKING_PARAMS = ("utm_", "yclid", "gclid", "fbclid", "from", "_openstat")


# --------------------------------------------------------------------------- #
# Модель новости
# --------------------------------------------------------------------------- #


@dataclass
class Article:
    title: str
    url: str
    summary: str
    source: str
    topic: str
    published: datetime | None = None
    is_penza: bool = False
    tier: str = "primary"
    score: float = 0.0
    keywords: tuple[str, ...] = field(default_factory=tuple)
    image_url: str = ""  # картинка из вложения ленты, если есть
    full_text: str = ""  # текст статьи, дочитывается в extractor.py

    @property
    def body(self) -> str:
        """Самый содержательный доступный текст новости."""
        return self.full_text or self.summary

    @property
    def url_hash(self) -> str:
        return _sha1(normalize_url(self.url))

    @property
    def title_hash(self) -> str:
        return _sha1(normalize_title(self.title))

    @property
    def age_hours(self) -> float | None:
        if self.published is None:
            return None
        return (_now() - self.published).total_seconds() / 3600


# --------------------------------------------------------------------------- #
# Вспомогательные функции
# --------------------------------------------------------------------------- #


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _sha1(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()


def normalize_url(url: str) -> str:
    """Убирает трекинговые параметры, якорь и хвостовой слэш."""
    try:
        parts = urlparse(url.strip())
    except ValueError:
        return url.strip().lower()
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not any(k.lower().startswith(p) for p in _TRACKING_PARAMS)
    ]
    path = parts.path.rstrip("/") or "/"
    netloc = parts.netloc.lower().removeprefix("www.")
    return urlunparse(("https", netloc, path, "", urlencode(query), ""))


def normalize_title(title: str) -> str:
    """Заголовок к сравнимому виду: без пунктуации, регистра и лишних пробелов."""
    text = unicodedata.normalize("NFKC", html.unescape(title)).lower().replace("ё", "е")
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()


# Слова, встречающиеся почти в каждой региональной новости: по ним нельзя
# судить о том, что две заметки об одном событии.
_COMMON_STEMS = frozenset(
    {
        "пенза",
        "пензе",
        "пензы",
        "пензо",
        "пензе",
        "облас",
        "район",
        "город",
        "сообщ",
        "расск",
        "стало",
        "будет",
        "может",
        "нового",
        "после",
        "время",
        "также",
        "котор",
        "перед",
        "через",
    }
)


def _stems(title: str, size: int = 5) -> set[str]:
    """Грубая нормализация словоформ: обрезаем слово до основы фиксированной длины.

    Русская морфология ломает сравнение по целым словам («пожар» и «пожара» —
    разные токены), а полноценный стеммер ради дедупликации избыточен.
    """
    words = (w for w in normalize_title(title).split() if len(w) >= 4)
    return {w[:size] for w in words} - _COMMON_STEMS


def _similar(a: str, b: str) -> float:
    """Коэффициент Жаккара по основам слов заголовков."""
    sa, sb = _stems(a), _stems(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def same_story(a: str, b: str) -> bool:
    """Похоже ли, что два заголовка описывают одно и то же событие.

    Пример: «Пожар на складе Wildberries локализован» и «Для тушения пожара на
    складе Wildberries прибыл вертолёт МЧС» — одно событие, в дайджест должна
    попасть только одна заметка.
    """
    sa, sb = _stems(a), _stems(b)
    if not sa or not sb:
        return False
    shared = sa & sb
    if len(shared) < 2:
        return False
    containment = len(shared) / min(len(sa), len(sb))
    return containment >= 0.4 or _similar(a, b) >= 0.5


def clean_text(raw: str, limit: int = 600) -> str:
    """HTML-описание из ленты -> плоский текст."""
    text = re.sub(r"<[^>]+>", " ", raw or "")
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0] + "…"
    return text


_WORD_RE = re.compile(r"[a-zа-я0-9]+")


def _match_keywords(text: str, keywords: Sequence[str]) -> tuple[str, ...]:
    """Ищет ключевые слова как НАЧАЛО слова, а не как произвольную подстроку.

    Поиск по подстроке даёт грубые ложные срабатывания: «лада» находится внутри
    «вкладам», «авто» — внутри «автономии». Сравнение по началу слова при этом
    сохраняет русскую морфологию: «автомобил» по-прежнему ловит «автомобили»
    и «автомобилях».
    """
    words = _WORD_RE.findall(text)
    hits = []
    for keyword in keywords:
        if " " in keyword or "." in keyword:
            # Составные ключи («яндекс go», «новая модель») ищем целиком.
            if keyword in text:
                hits.append(keyword)
        elif any(word.startswith(keyword) for word in words):
            hits.append(keyword)
    return tuple(hits)


# --------------------------------------------------------------------------- #
# Классификация и оценка
# --------------------------------------------------------------------------- #


def classify(text: str, feed: Feed) -> tuple[str | None, tuple[str, ...]]:
    """Определяет тему новости. Возвращает (тема, сработавшие ключевые слова)."""
    hits = {topic: _match_keywords(text, words) for topic, words in _TOPIC_KEYWORDS.items()}
    is_penza = bool(_match_keywords(text, PENZA_KEYWORDS)) or feed.region == "penza"

    # Происшествия берём только пензенские — так поставлена задача.
    if not is_penza:
        # Чужое ДТП или пожар не должны перетекать в авторубрику: если заметка
        # похожа на происшествие и в ней нет рыночных слов, отбрасываем её.
        if hits[TOPIC_INCIDENT] and not _match_keywords(text, MARKET_KEYWORDS):
            return None, ()
        hits[TOPIC_INCIDENT] = ()

    # Про такси и происшествия речь идёт чаще, чем про авто вообще,
    # поэтому при совпадении нескольких тем приоритет отдаём более узкой.
    for topic in (TOPIC_TAXI, TOPIC_INCIDENT, TOPIC_AUTO):
        if hits[topic]:
            return topic, hits[topic]

    # Тема профильного издания — достаточное основание: там всё по делу.
    # Поисковым лентам так доверять нельзя: они возвращают что угодно
    # похожее на запрос, поэтому от них требуем совпадения по словам.
    if feed.topic and feed.topic != TOPIC_INCIDENT and feed.tier == "primary":
        return feed.topic, ()
    return None, ()


def score(article: Article) -> float:
    """Чем выше, тем раньше новость попадёт в дайджест."""
    value = 2.0 * min(len(article.keywords), 3)

    if article.is_penza:
        # Пензенская область — приоритет для такси и происшествий.
        value += 5.0 if article.topic in (TOPIC_TAXI, TOPIC_INCIDENT) else 1.0

    age = article.age_hours
    if age is None:
        value += 0.5
    elif age < 6:
        value += 3.0
    elif age < 12:
        value += 2.0
    elif age < 24:
        value += 1.0

    if article.tier == "fallback":
        value -= 1.0
    if is_aggregator_link(article.url):
        # При равных условиях в дайджест лучше поставить заметку с прямой
        # ссылкой на издание, а не на агрегатор.
        value -= 0.5
    return value


# --------------------------------------------------------------------------- #
# Чтение лент
# --------------------------------------------------------------------------- #


_IMAGE_EXT = (".jpg", ".jpeg", ".png", ".webp")


def _entry_image(entry) -> str:
    """Ищет картинку новости среди вложений и медиа-тегов ленты."""
    for enclosure in getattr(entry, "enclosures", []) or []:
        href = enclosure.get("href") or enclosure.get("url") or ""
        mime = (enclosure.get("type") or "").lower()
        if href and (mime.startswith("image/") or href.lower().endswith(_IMAGE_EXT)):
            return href

    for key in ("media_content", "media_thumbnail"):
        for media in getattr(entry, key, []) or []:
            href = media.get("url") or ""
            if href:
                return href

    # Последний шанс — первая картинка в HTML-описании.
    html_body = getattr(entry, "summary", "") or getattr(entry, "description", "")
    match = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', html_body or "")
    return match.group(1) if match else ""


def _entry_datetime(entry) -> datetime | None:
    for attr in ("published_parsed", "updated_parsed"):
        parsed = getattr(entry, attr, None)
        if parsed:
            try:
                return datetime(*parsed[:6], tzinfo=timezone.utc)
            except (TypeError, ValueError):
                continue
    return None


@dataclass
class FeedResult:
    """Итог обращения к одной ленте — нужен и для отбора, и для контроля здоровья."""

    feed: Feed
    articles: list[Article] = field(default_factory=list)
    entries_total: int = 0  # сколько записей вообще было в ленте
    error: str = ""

    @property
    def broken(self) -> bool:
        """Лента сломана: не ответила, не разобралась или пуста.

        Ноль ПОДХОДЯЩИХ новостей — нормальная ситуация (сегодня издание просто
        не писало по нашим темам), а ноль записей вообще — уже повод проверить
        адрес ленты.
        """
        return bool(self.error) or self.entries_total == 0


def _fetch_feed(feed: Feed) -> FeedResult:
    """Загружает и разбирает одну ленту. Ошибки не пробрасываются."""
    try:
        response = requests.get(
            feed.url,
            timeout=HTTP_TIMEOUT,
            headers={"User-Agent": USER_AGENT, "Accept": "application/rss+xml, */*"},
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        log.warning("Лента %s недоступна: %s", feed.name, exc)
        return FeedResult(feed, error=str(exc)[:200])

    parsed = feedparser.parse(response.content)
    if parsed.bozo and not parsed.entries:
        reason = str(parsed.get("bozo_exception") or "лента не разобрана")
        log.warning("Лента %s не разобрана: %s", feed.name, reason)
        return FeedResult(feed, error=reason[:200])

    articles: list[Article] = []
    for entry in parsed.entries:
        title = clean_text(getattr(entry, "title", ""), limit=300)
        link = (getattr(entry, "link", "") or "").strip()
        if not title or not link:
            continue

        summary = clean_text(
            getattr(entry, "summary", "") or getattr(entry, "description", "")
        )
        # У Google Новостей в описании лежит только разметка со ссылками.
        if "news.google.com" in link and len(summary) < 40:
            summary = ""

        haystack = f"{title} {summary}".lower().replace("ё", "е")
        topic, keywords = classify(haystack, feed)
        if topic is None:
            continue

        # Источник у Google Новостей указан отдельным полем.
        source = feed.name
        entry_source = getattr(entry, "source", None)
        if entry_source is not None:
            source = getattr(entry_source, "title", None) or source

        articles.append(
            Article(
                title=title,
                url=link,
                summary=summary,
                source=source,
                topic=topic,
                published=_entry_datetime(entry),
                is_penza=bool(_match_keywords(haystack, PENZA_KEYWORDS))
                or feed.region == "penza",
                tier=feed.tier,
                keywords=keywords,
                image_url=_entry_image(entry),
            )
        )

    log.info(
        "Лента %s: записей — %d, подходящих новостей — %d",
        feed.name,
        len(parsed.entries),
        len(articles),
    )
    return FeedResult(feed, articles=articles, entries_total=len(parsed.entries))


def fetch_feeds(feeds: Iterable[Feed], workers: int = 6) -> list[FeedResult]:
    """Читает ленты параллельно. Возвращает результат по каждой."""
    feeds = list(feeds)
    if not feeds:
        return []
    with ThreadPoolExecutor(max_workers=min(workers, len(feeds))) as pool:
        return list(pool.map(_fetch_feed, feeds))


def flatten(results: Iterable[FeedResult]) -> list[Article]:
    return [article for result in results for article in result.articles]


def is_aggregator_link(url: str) -> bool:
    """Ссылка ведёт на агрегатор, а не напрямую в издание.

    Google Новости отдают ссылку вида /rss/articles/CBMi..., внутри которой
    лежит непрозрачный идентификатор: раскрыть его можно только через
    внутренний RPC Google, поэтому такие ссылки оставляем как есть
    (в браузере и в Telegram они открываются нормально), но при прочих
    равных предпочитаем заметку с прямой ссылкой.
    """
    return "news.google.com" in url


# --------------------------------------------------------------------------- #
# Хранилище опубликованного
# --------------------------------------------------------------------------- #


def connect(db_path: str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path or DB_PATH)
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS published (
            url_hash    TEXT PRIMARY KEY,
            title_hash  TEXT NOT NULL,
            url         TEXT NOT NULL,
            title       TEXT NOT NULL,
            topic       TEXT NOT NULL,
            source      TEXT,
            published_at TEXT,
            created_at  TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_published_title ON published(title_hash);
        CREATE INDEX IF NOT EXISTS idx_published_created ON published(created_at);

        CREATE TABLE IF NOT EXISTS runs (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL,
            status     TEXT NOT NULL,
            items      INTEGER NOT NULL DEFAULT 0,
            details    TEXT
        );

        CREATE TABLE IF NOT EXISTS feed_health (
            name         TEXT PRIMARY KEY,
            url          TEXT NOT NULL,
            last_ok_at   TEXT,
            broken_streak INTEGER NOT NULL DEFAULT 0,
            alerted_at   TEXT,
            last_error   TEXT
        );
        """
    )
    conn.commit()


def is_published(conn: sqlite3.Connection, article: Article) -> bool:
    row = conn.execute(
        "SELECT 1 FROM published WHERE url_hash = ? OR title_hash = ? LIMIT 1",
        (article.url_hash, article.title_hash),
    ).fetchone()
    return row is not None


def mark_published(conn: sqlite3.Connection, articles: Iterable[Article]) -> int:
    now = _now().isoformat()
    rows = [
        (
            a.url_hash,
            a.title_hash,
            a.url,
            a.title,
            a.topic,
            a.source,
            a.published.isoformat() if a.published else None,
            now,
        )
        for a in articles
    ]
    conn.executemany(
        """
        INSERT OR IGNORE INTO published
            (url_hash, title_hash, url, title, topic, source, published_at, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    conn.commit()
    return len(rows)


def recent_titles(conn: sqlite3.Connection, days: int = STORY_DEDUP_DAYS) -> list[str]:
    """Заголовки, опубликованные за последние `days` дней."""
    if days <= 0:
        return []
    cutoff = (_now() - timedelta(days=days)).isoformat()
    rows = conn.execute(
        "SELECT title FROM published WHERE created_at >= ? ORDER BY created_at DESC",
        (cutoff,),
    ).fetchall()
    return [row["title"] for row in rows]


def is_known_story(article: Article, published_titles: Sequence[str]) -> bool:
    """Писали ли мы уже об этом событии.

    Точных хэшей мало: то же происшествие другое издание назовёт другими
    словами и на следующий день оно пройдёт как новое.
    """
    return any(same_story(article.title, title) for title in published_titles)


# --------------------------------------------------------------------------- #
# Здоровье лент
# --------------------------------------------------------------------------- #


def record_feed_health(
    conn: sqlite3.Connection, results: Iterable[FeedResult]
) -> list[tuple[str, int, str]]:
    """Обновляет статистику по лентам.

    Возвращает список (имя, серия сбоев, ошибка) для лент, о которых пора
    предупредить: серия достигла порога, а предупреждение ещё не отправлялось.
    """
    now = _now().isoformat()
    to_alert: list[tuple[str, int, str]] = []

    for result in results:
        feed = result.feed
        row = conn.execute(
            "SELECT broken_streak, alerted_at FROM feed_health WHERE name = ?",
            (feed.name,),
        ).fetchone()
        streak = row["broken_streak"] if row else 0

        if result.broken:
            streak += 1
            reason = result.error or "лента пуста"
            conn.execute(
                """
                INSERT INTO feed_health (name, url, broken_streak, last_error)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    url = excluded.url,
                    broken_streak = excluded.broken_streak,
                    last_error = excluded.last_error
                """,
                (feed.name, feed.url, streak, reason),
            )
            already_alerted = bool(row and row["alerted_at"])
            if streak >= FEED_ALERT_AFTER and not already_alerted:
                conn.execute(
                    "UPDATE feed_health SET alerted_at = ? WHERE name = ?", (now, feed.name)
                )
                to_alert.append((feed.name, streak, reason))
        else:
            # Лента ожила — сбрасываем и серию, и отметку об уведомлении.
            conn.execute(
                """
                INSERT INTO feed_health (name, url, last_ok_at, broken_streak, alerted_at)
                VALUES (?, ?, ?, 0, NULL)
                ON CONFLICT(name) DO UPDATE SET
                    url = excluded.url,
                    last_ok_at = excluded.last_ok_at,
                    broken_streak = 0,
                    alerted_at = NULL
                """,
                (feed.name, feed.url, now),
            )

    conn.commit()
    return to_alert


def feed_health(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM feed_health ORDER BY broken_streak DESC, name"
    ).fetchall()
    return [dict(row) for row in rows]


def purge_old(conn: sqlite3.Connection, days: int = KEEP_HISTORY_DAYS) -> int:
    cutoff = (_now() - timedelta(days=days)).isoformat()
    cur = conn.execute("DELETE FROM published WHERE created_at < ?", (cutoff,))
    conn.commit()
    return cur.rowcount


def log_run(conn: sqlite3.Connection, status: str, items: int, details: str = "") -> None:
    conn.execute(
        "INSERT INTO runs (started_at, status, items, details) VALUES (?, ?, ?, ?)",
        (_now().isoformat(), status, items, details[:500]),
    )
    conn.commit()


def stats(conn: sqlite3.Connection) -> dict:
    total = conn.execute("SELECT COUNT(*) AS c FROM published").fetchone()["c"]
    last = conn.execute(
        "SELECT started_at, status, items FROM runs ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return {
        "published_total": total,
        "last_run": dict(last) if last else None,
    }


# --------------------------------------------------------------------------- #
# Отбор новостей для дайджеста
# --------------------------------------------------------------------------- #


def _dedupe(articles: Sequence[Article]) -> list[Article]:
    """Внутрибатчевая дедупликация: одинаковые ссылки, заголовки и сюжеты.

    Предполагается, что на входе новости отсортированы по убыванию рейтинга, —
    тогда из группы про одно событие остаётся лучшая заметка.
    """
    unique: list[Article] = []
    seen_urls: set[str] = set()
    seen_titles: set[str] = set()
    for article in articles:
        if article.url_hash in seen_urls or article.title_hash in seen_titles:
            continue
        if any(same_story(article.title, kept.title) for kept in unique):
            log.debug("Пропускаю дубль сюжета: %s", article.title[:80])
            continue
        seen_urls.add(article.url_hash)
        seen_titles.add(article.title_hash)
        unique.append(article)
    return unique


def _pick(articles: Sequence[Article], max_items: int) -> list[Article]:
    """Отбор по кругу: по одной лучшей новости из каждой темы, пока есть место.

    Так одна «горячая» тема (например, крупное происшествие) не занимает
    весь выпуск, а темы без новостей не резервируют слоты впустую.
    """
    pools = {
        topic: sorted(
            (a for a in articles if a.topic == topic), key=lambda a: a.score, reverse=True
        )
        for topic in TOPIC_ORDER
    }

    picked: list[Article] = []
    while len(picked) < max_items and any(pools.values()):
        for topic in TOPIC_ORDER:
            if len(picked) >= max_items:
                break
            if pools[topic]:
                picked.append(pools[topic].pop(0))

    picked.sort(key=lambda a: (TOPIC_ORDER.index(a.topic), -a.score))
    return picked


def limit_per_source(
    selected: Sequence[Article],
    pool: Sequence[Article] = (),
    max_per_source: int = 2,
) -> list[Article]:
    """Не даёт одному изданию занять половину выпуска.

    Лишние заметки сверх квоты выбрасываются, а освободившиеся места по
    возможности добираются из остальных кандидатов — так выпуск не худеет.
    """
    if max_per_source <= 0:
        return list(selected)

    counts: dict[str, int] = {}
    kept: list[Article] = []
    dropped = 0
    for article in selected:
        if counts.get(article.source, 0) >= max_per_source:
            dropped += 1
            continue
        counts[article.source] = counts.get(article.source, 0) + 1
        kept.append(article)

    if dropped:
        chosen_urls = {a.url_hash for a in kept}
        for article in pool:
            if len(kept) >= len(selected):
                break
            if article.url_hash in chosen_urls:
                continue
            if counts.get(article.source, 0) >= max_per_source:
                continue
            counts[article.source] = counts.get(article.source, 0) + 1
            chosen_urls.add(article.url_hash)
            kept.append(article)
        log.info(
            "Ограничение по источникам: убрано %d заметок, добрано %d",
            dropped,
            len(kept) - (len(selected) - dropped),
        )

    kept.sort(key=lambda a: (TOPIC_ORDER.index(a.topic), -a.score))
    return kept


def find_breaking(
    candidates: Sequence[Article],
    *,
    max_age_hours: int = 4,
    min_sources: int = 2,
) -> list[Article]:
    """Отбирает новости, ради которых стоит нарушить утреннее расписание.

    Срочной считается свежая пензенская новость, которая либо описывает
    серьёзное ЧП (по ключевым словам), либо уже подхвачена несколькими
    изданиями — это надёжный признак значимости.
    """
    urgent: list[Article] = []
    for article in candidates:
        if not article.is_penza or article.topic == TOPIC_AUTO:
            continue
        age = article.age_hours
        if age is None or age > max_age_hours:
            continue

        text = f"{article.title} {article.summary}".lower().replace("ё", "е")
        severe = bool(_match_keywords(text, BREAKING_KEYWORDS))
        corroboration = sum(
            1
            for other in candidates
            if other is not article
            and other.source != article.source
            and same_story(article.title, other.title)
        )
        if severe or corroboration + 1 >= min_sources:
            urgent.append(article)

    urgent.sort(key=lambda a: a.score, reverse=True)
    return _dedupe(urgent)


@dataclass
class CollectResult:
    """Кандидаты для выпуска и замечания к лентам, набранные по пути."""

    candidates: list[Article] = field(default_factory=list)
    feed_alerts: list[tuple[str, int, str]] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.candidates)

    def __iter__(self):
        return iter(self.candidates)


def collect(
    conn: sqlite3.Connection,
    *,
    min_items: int = 5,
    limit: int = 20,
    max_age_hours: int = 36,
) -> CollectResult:
    """Возвращает до `limit` свежих неопубликованных новостей-кандидатов.

    Это не финальная подборка: список отсортирован и сбалансирован по темам,
    а выбор 5-8 лучших делает модель (см. summarizer.select_best).
    """
    cutoff = _now() - timedelta(hours=max_age_hours)
    published_titles = recent_titles(conn)

    def prepare(raw: Sequence[Article]) -> list[Article]:
        fresh = [a for a in raw if a.published is None or a.published >= cutoff]
        for article in fresh:
            article.score = score(article)
        fresh.sort(key=lambda a: a.score, reverse=True)
        result = []
        for article in _dedupe(fresh):
            if is_published(conn, article):
                continue
            if is_known_story(article, published_titles):
                log.debug("Об этом сюжете уже писали: %s", article.title[:80])
                continue
            result.append(article)
        return result

    results = fetch_feeds(FEEDS)
    alerts = record_feed_health(conn, results)
    for name, streak, reason in alerts:
        log.error("Лента %s не отвечает %d прогонов подряд: %s", name, streak, reason)
    candidates = prepare(flatten(results))
    log.info("Основные ленты: кандидатов после дедупликации — %d", len(candidates))

    # Фолбэк через поиск нужен в двух случаях: новостей в принципе мало
    # или какая-то из тем осталась без единой заметки.
    missing = [t for t in TOPIC_ORDER if not any(a.topic == t for a in candidates)]
    if len(candidates) < min_items or missing:
        if len(candidates) < min_items:
            search_feeds = list(FALLBACK_FEEDS)
            reason = f"кандидатов меньше {min_items}"
        else:
            search_feeds = [f for f in FALLBACK_FEEDS if f.topic in missing]
            reason = "нет новостей по темам: " + ", ".join(missing)
        log.info("Подключаю поиск (%s)", reason)

        extra = prepare(flatten(fetch_feeds(search_feeds)))
        candidates = _dedupe(sorted(candidates + extra, key=lambda a: a.score, reverse=True))
        log.info("После фолбэка кандидатов — %d", len(candidates))

    selected = _pick(candidates, limit)
    log.info(
        "Кандидатов отобрано: %d (%s)",
        len(selected),
        ", ".join(f"{t}={sum(1 for a in selected if a.topic == t)}" for t in TOPIC_ORDER),
    )
    return CollectResult(candidates=selected, feed_alerts=alerts)
