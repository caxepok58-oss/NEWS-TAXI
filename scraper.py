"""Разбор страниц со списком новостей у изданий без RSS.

У penzasmi.ru, riapo.ru и penzainform.ru — трёх пензенских источников из
задания — собственной ленты нет. Раньше они читались через поиск Google
Новостей, и это давало сразу три неприятности: ссылка вела на редирект
Google, полный текст статьи по такой ссылке не дочитывался, а все три
источника зависели от одного стороннего сервиса.

Модуль ничего не знает о темах и рейтингах: его дело — вернуть со страницы
списка заголовки, прямые ссылки и, по возможности, дату публикации.
"""

from __future__ import annotations

import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, time as dtime, timezone
from typing import Iterable, Sequence
from urllib.parse import urljoin, urlparse

import requests

log = logging.getLogger(__name__)

HTTP_TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "15"))
USER_AGENT = os.getenv(
    "USER_AGENT",
    "Mozilla/5.0 (compatible; NewsDigestBot/1.0; +https://github.com/)",
)
TIMEZONE = os.getenv("TIMEZONE", "Europe/Moscow")


def _tzinfo():
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(TIMEZONE)
    except Exception:  # noqa: BLE001 - без зоны считаем время в UTC
        return timezone.utc


# --------------------------------------------------------------------------- #
# Описание сайта
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Site:
    """Как читать страницу со списком новостей конкретного издания."""

    name: str
    url: str  # страница со списком
    link_pattern: str  # какие ссылки считать новостями
    min_title_len: int = 25
    # Сколько первых ссылок брать: списки отсортированы от свежих к старым,
    # поэтому ограничение заодно отсекает старьё, у которого нет даты.
    max_items: int = 25


SITES: tuple[Site, ...] = (
    Site(
        name="ПензаСМИ",
        url="https://penzasmi.ru/",
        link_pattern=r"^/(?:news|main)/\d+/[\w-]+",
    ),
    Site(
        name="РИА Пензенской области",
        url="https://riapo.ru/",
        link_pattern=r"^/penza/[\w-]+/[\w-]+",
    ),
    Site(
        name="PenzaInform",
        url="https://www.penzainform.ru/news/",
        link_pattern=r"^/news/[\w-]+/\d{4}/\d{2}/\d{2}/[\w-]+",
    ),
)


@dataclass
class ScrapedItem:
    title: str
    url: str
    published: datetime | None = None


@dataclass
class SiteResult:
    """Итог обращения к сайту — той же формы, что и результат чтения ленты."""

    site: Site
    items: list[ScrapedItem] = field(default_factory=list)
    links_total: int = 0
    error: str = ""

    @property
    def broken(self) -> bool:
        return bool(self.error) or self.links_total == 0


# --------------------------------------------------------------------------- #
# Даты
# --------------------------------------------------------------------------- #

_MONTHS = {
    "январ": 1, "феврал": 2, "март": 3, "апрел": 4, "мая": 5, "май": 5,
    "июн": 6, "июл": 7, "август": 8, "сентябр": 9, "октябр": 10,
    "ноябр": 11, "декабр": 12,
}

_URL_DATE = re.compile(r"/(20\d{2})/(\d{2})/(\d{2})/")

# Месяцы перечислены явно. Шаблон вида «число + любое слово» ловил куски
# заголовков: «установят 10 миниатюрных скульптур» терял «10 миниатюрных»,
# а «с 1 сентября введут оценку» отдавал ложную дату публикации.
_MONTH_RE = (
    r"(январ\w*|феврал\w*|март\w*|апрел\w*|ма[йя]|июн\w*|июл\w*|"
    r"август\w*|сентябр\w*|октябр\w*|ноябр\w*|декабр\w*)"
)
_TEXT_DATE = re.compile(
    rf"(\d{{1,2}})\s+{_MONTH_RE}(?:\s+(20\d{{2}})\s*г?\.?)?(?:[,\s]+(\d{{1,2}}):(\d{{2}}))?",
    re.IGNORECASE,
)
# Подпись с датой стоит в конце строки — в середине это уже часть заголовка.
_TRAILING_DATE = re.compile(
    rf"[\s,·|—-]*\d{{1,2}}\s+{_MONTH_RE}(?:\s+20\d{{2}}\s*г?\.?)?"
    rf"(?:[,\s]+\d{{1,2}}:\d{{2}})?\s*$",
    re.IGNORECASE,
)
_TIME_ONLY = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)\b")


def date_from_url(url: str) -> datetime | None:
    """Дата из адреса вида /news/incidents/2026/07/31/slug."""
    match = _URL_DATE.search(url)
    if not match:
        return None
    year, month, day = (int(part) for part in match.groups())
    try:
        # Точного времени в адресе нет: берём середину дня, чтобы заметка не
        # выглядела ни заметно свежее, ни заметно старее, чем есть.
        return datetime.combine(
            datetime(year, month, day).date(), dtime(12, 0), tzinfo=_tzinfo()
        )
    except ValueError:
        return None


def _build_date(match: re.Match, now: datetime) -> datetime | None:
    day, month_word, year, hour, minute = match.groups()
    month = next(
        (num for stem, num in _MONTHS.items() if month_word.lower().startswith(stem)),
        None,
    )
    if not month:
        return None
    try:
        return datetime(
            int(year) if year else now.year,
            month,
            int(day),
            int(hour) if hour else 12,
            int(minute) if minute else 0,
            tzinfo=_tzinfo(),
        )
    except ValueError:
        return None


def date_from_text(
    text: str, now: datetime | None = None, require_caption: bool = False
) -> datetime | None:
    """Дата из подписи рядом со ссылкой: «31 июля 2026», «14:35».

    При ``require_caption`` принимаются только те совпадения, где есть год или
    время: у подписи они обычно указаны, а «1 сентября» в середине заголовка —
    это содержание новости, а не дата её публикации.
    """
    now = now or datetime.now(_tzinfo())
    text = (text or "").lower().replace("ё", "е")

    for match in _TEXT_DATE.finditer(text):
        year, hour = match.group(3), match.group(4)
        if require_caption and not (year or hour):
            continue
        parsed = _build_date(match, now)
        if parsed is not None:
            return parsed

    # Только время — значит, сегодня.
    match = _TIME_ONLY.search(text)
    if match:
        return now.replace(
            hour=int(match.group(1)), minute=int(match.group(2)), second=0, microsecond=0
        )
    return None


def split_caption_date(raw: str) -> tuple[str, datetime | None]:
    """Отделяет подпись с датой в конце текста ссылки от самого заголовка."""
    text = re.sub(r"\s+", " ", raw or "").strip()
    match = _TRAILING_DATE.search(text)
    if not match:
        return text, None

    caption = match.group(0)
    title = text[: match.start()].strip(" ,·—-|")
    return title, date_from_text(caption)


# --------------------------------------------------------------------------- #
# Разбор страницы
# --------------------------------------------------------------------------- #


def parse_listing(html_text: str, site: Site) -> list[ScrapedItem]:
    """Достаёт новости со страницы списка. Порядок исходный — от свежих."""
    from lxml import html as lxml_html

    try:
        doc = lxml_html.fromstring(html_text)
    except Exception as exc:  # noqa: BLE001 - битая разметка не должна ронять прогон
        log.warning("Не удалось разобрать страницу %s: %s", site.name, exc)
        return []

    pattern = re.compile(site.link_pattern)
    items: list[ScrapedItem] = []
    seen: set[str] = set()

    for anchor in doc.xpath("//a[@href]"):
        href = (anchor.get("href") or "").strip()
        path = urlparse(href).path if href.startswith("http") else href
        if not pattern.match(path):
            continue

        title, caption_date = split_caption_date(anchor.text_content() or "")
        if len(title) < site.min_title_len:
            continue

        url = urljoin(site.url, href)
        if url in seen:
            continue
        seen.add(url)

        published = date_from_url(url) or caption_date
        if published is None:
            # Дата нередко лежит в соседнем элементе, а не внутри ссылки.
            # Из текста родителя вычитаем заголовок, чтобы числа из него
            # не выдали себя за дату публикации.
            parent = anchor.getparent()
            if parent is not None:
                around = re.sub(r"\s+", " ", parent.text_content() or "")
                around = around.replace(title, " ")[:200]
                published = date_from_text(around, require_caption=True)

        items.append(ScrapedItem(title=title, url=url, published=published))
        if len(items) >= site.max_items:
            break

    return items


def fetch_site(site: Site) -> SiteResult:
    """Загружает и разбирает страницу списка. Ошибки не пробрасываются."""
    try:
        response = requests.get(
            site.url,
            timeout=HTTP_TIMEOUT,
            headers={"User-Agent": USER_AGENT, "Accept-Language": "ru,en;q=0.8"},
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        log.warning("Сайт %s недоступен: %s", site.name, exc)
        return SiteResult(site, error=str(exc)[:200])

    if response.encoding and response.encoding.lower() == "iso-8859-1":
        response.encoding = response.apparent_encoding

    items = parse_listing(response.text, site)
    log.info("Сайт %s: новостей на странице — %d", site.name, len(items))
    return SiteResult(site, items=items, links_total=len(items))


def fetch_sites(sites: Iterable[Site] = SITES, workers: int = 3) -> list[SiteResult]:
    sites = list(sites)
    if not sites:
        return []
    with ThreadPoolExecutor(max_workers=min(workers, len(sites))) as pool:
        return list(pool.map(fetch_site, sites))
