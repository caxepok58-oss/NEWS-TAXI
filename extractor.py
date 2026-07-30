"""Извлечение основного текста статьи со страницы издания.

Описания в RSS часто состоят из одной фразы, а у лент Google Новостей их нет
вовсе. Без полного текста пересказ получается бедным не из-за модели, а из-за
входных данных, поэтому для отобранных новостей мы дочитываем саму страницу.
"""

from __future__ import annotations

import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor
from html import unescape
from typing import Sequence

import requests

from fetcher import Article

log = logging.getLogger(__name__)

HTTP_TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "15"))
USER_AGENT = os.getenv(
    "USER_AGENT",
    "Mozilla/5.0 (compatible; NewsDigestBot/1.0; +https://github.com/)",
)
# Сколько символов текста статьи отдаём модели: по опыту 1500 хватает,
# чтобы пересказать суть, и не раздувает запрос.
MAX_TEXT_LEN = int(os.getenv("ARTICLE_TEXT_LEN", "1500"))
ENABLED = os.getenv("FETCH_FULL_TEXT", "1") not in ("0", "false", "False", "")

try:  # trafilatura разбирает вёрстку заметно лучше, но не обязателен
    import trafilatura

    _HAS_TRAFILATURA = True
except ImportError:  # pragma: no cover - зависит от окружения
    trafilatura = None
    _HAS_TRAFILATURA = False

_DROP_TAGS = re.compile(
    r"<(script|style|noscript|svg|form|nav|header|footer|aside)\b.*?</\1>",
    re.IGNORECASE | re.DOTALL,
)
_PARAGRAPH = re.compile(r"<p\b[^>]*>(.*?)</p>", re.IGNORECASE | re.DOTALL)
_TAG = re.compile(r"<[^>]+>")
# Строки-обвязка, которые попадаются в теле статьи почти на каждом сайте.
_JUNK = re.compile(
    r"^(читайте также|читайте по теме|подпис|поделит|фото:|источник:|реклама|"
    r"новости партн|смотрите также|ранее сообщалось, что)",
    re.IGNORECASE,
)


def _clean_paragraph(raw: str) -> str:
    text = unescape(_TAG.sub(" ", raw))
    return re.sub(r"\s+", " ", text).strip()


def extract_from_html(html: str, limit: int = MAX_TEXT_LEN) -> str:
    """Фолбэк без внешних зависимостей: собираем текст из содержательных <p>."""
    body = _DROP_TAGS.sub(" ", html)
    paragraphs = []
    for match in _PARAGRAPH.findall(body):
        text = _clean_paragraph(match)
        # Короткие абзацы — это подписи, кнопки и навигация, а не текст статьи.
        if len(text) < 80 or _JUNK.match(text):
            continue
        paragraphs.append(text)
        if sum(len(p) for p in paragraphs) >= limit:
            break
    return " ".join(paragraphs)[:limit].strip()


def fetch_text(url: str, limit: int = MAX_TEXT_LEN) -> str:
    """Возвращает основной текст статьи или пустую строку, если не получилось."""
    try:
        response = requests.get(
            url,
            timeout=HTTP_TIMEOUT,
            headers={"User-Agent": USER_AGENT, "Accept-Language": "ru,en;q=0.8"},
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        log.debug("Не удалось загрузить %s: %s", url, exc)
        return ""

    # Кодировку из заголовков сайты указывают неверно чаще, чем в самой странице.
    if response.encoding and response.encoding.lower() == "iso-8859-1":
        response.encoding = response.apparent_encoding
    html = response.text

    if _HAS_TRAFILATURA:
        try:
            text = trafilatura.extract(
                html,
                include_comments=False,
                include_tables=False,
                favor_precision=True,
            )
            if text:
                return re.sub(r"\s+", " ", text).strip()[:limit]
        except Exception as exc:  # noqa: BLE001 - падение парсера не критично
            log.debug("trafilatura не справилась с %s: %s", url, exc)

    return extract_from_html(html, limit=limit)


def enrich(articles: Sequence[Article], workers: int = 4) -> list[Article]:
    """Дописывает в article.full_text текст статьи. Ошибки не критичны.

    Если текст добыть не удалось, у новости остаётся описание из ленты —
    пересказ просто получится короче.
    """
    articles = list(articles)
    if not ENABLED or not articles:
        return articles

    def worker(article: Article) -> None:
        # У ссылок на агрегатор нет статьи как таковой — там редирект.
        if "news.google.com" in article.url:
            return
        article.full_text = fetch_text(article.url)

    with ThreadPoolExecutor(max_workers=min(workers, len(articles))) as pool:
        list(pool.map(worker, articles))

    enriched = sum(1 for a in articles if a.full_text)
    log.info("Полный текст получен для %d из %d новостей", enriched, len(articles))
    return articles
