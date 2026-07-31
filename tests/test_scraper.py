"""Разбор страниц со списком новостей у изданий без RSS."""

from __future__ import annotations

from datetime import datetime

import pytest

import fetcher
import scraper
from fetcher import TOPIC_INCIDENT
from scraper import Site

SITE = Site(name="Тест", url="https://example.ru/", link_pattern=r"^/news/\d+/[\w-]+")

LISTING = """
<html><body>
  <div class="item">
    <a href="/news/126790/v-shkolah-penzenskoj-oblasti">
      В школах Пензенской области с 1 сентября введут оценку поведения
    </a>
    <span class="date">31 июля 2026, 09:15</span>
  </div>
  <div class="item">
    <a href="/news/126782/v-centre-penzy">В центре Пензы установят 10 миниатюрных скульптур</a>
  </div>
  <div class="item">
    <a href="/news/126781/pozhar">Пожар под Пензой ликвидирован 30 июля 2026</a>
  </div>
  <div class="item"><a href="/about">О редакции</a></div>
  <div class="item"><a href="/news/126790/v-shkolah-penzenskoj-oblasti">повтор ссылки на ту же новость</a></div>
</body></html>
"""


# --------------------------------------------------------------------------- #
# Заголовки и даты
# --------------------------------------------------------------------------- #


def test_numbers_inside_headline_survive():
    """Шаблон «число + слово» раньше выкусывал куски заголовков."""
    title, date = scraper.split_caption_date(
        "В центре Пензы установят 10 миниатюрных скульптур"
    )
    assert title == "В центре Пензы установят 10 миниатюрных скульптур"
    assert date is None


def test_date_inside_headline_is_not_publication_date():
    """«с 1 сентября введут» — это содержание новости, а не дата выпуска."""
    title, date = scraper.split_caption_date(
        "В школах Пензенской области с 1 сентября введут оценку поведения"
    )
    assert title.endswith("оценку поведения")
    assert date is None


def test_trailing_caption_is_split_off():
    title, date = scraper.split_caption_date(
        "Блокаднице из Пензы исполнилось 88 лет    31 июля 2026"
    )
    assert title == "Блокаднице из Пензы исполнилось 88 лет"
    assert (date.day, date.month, date.year) == (31, 7, 2026)


def test_caption_with_time():
    _, date = scraper.split_caption_date("Пожар ликвидирован 31 июля 2026, 14:35")
    assert (date.hour, date.minute) == (14, 35)


def test_date_from_url():
    date = scraper.date_from_url("https://www.penzainform.ru/news/incidents/2026/07/31/x")
    assert (date.day, date.month, date.year) == (31, 7, 2026)


def test_date_from_url_ignores_garbage():
    assert scraper.date_from_url("https://a.ru/news/2026/13/45/x") is None
    assert scraper.date_from_url("https://a.ru/news/123/x") is None


def test_time_only_means_today():
    now = datetime(2026, 7, 31, 20, 0, tzinfo=scraper._tzinfo())
    date = scraper.date_from_text("сегодня, 09:15", now=now)
    assert (date.day, date.hour, date.minute) == (31, 9, 15)


def test_require_caption_rejects_bare_dates():
    """Без года и времени это, скорее всего, часть заголовка."""
    assert scraper.date_from_text("акция пройдёт 5 августа", require_caption=True) is None
    assert scraper.date_from_text("5 августа 2026", require_caption=True) is not None


# --------------------------------------------------------------------------- #
# Разбор страницы
# --------------------------------------------------------------------------- #


def test_parse_listing_extracts_news_only():
    items = scraper.parse_listing(LISTING, SITE)
    urls = [i.url for i in items]

    assert len(items) == 3, "служебные ссылки и повтор не в счёт"
    assert all(u.startswith("https://example.ru/news/") for u in urls)
    assert len(set(urls)) == len(urls)


def test_parse_listing_takes_date_from_neighbour():
    items = scraper.parse_listing(LISTING, SITE)
    first = next(i for i in items if "shkolah" in i.url)
    assert first.published is not None, "дата лежала в соседнем элементе"
    assert (first.published.day, first.published.hour) == (31, 9)


def test_parse_listing_respects_limit():
    many = "".join(
        f'<a href="/news/{i}/tema-novosti-nomer">Заголовок новости номер {i} про Пензу</a>'
        for i in range(50)
    )
    items = scraper.parse_listing(f"<html><body>{many}</body></html>", SITE)
    assert len(items) == SITE.max_items


def test_parse_listing_survives_broken_html():
    assert scraper.parse_listing("<html><body><a href=", SITE) == []


def test_fetch_site_handles_network_error(monkeypatch):
    def boom(*args, **kwargs):
        raise scraper.requests.RequestException("нет сети")

    monkeypatch.setattr(scraper.requests, "get", boom)
    result = scraper.fetch_site(SITE)
    assert result.broken and result.items == []


def test_empty_page_counts_as_broken():
    assert scraper.SiteResult(SITE, links_total=0).broken
    assert not scraper.SiteResult(SITE, links_total=5).broken


# --------------------------------------------------------------------------- #
# Превращение в новости
# --------------------------------------------------------------------------- #


def test_articles_from_site_classifies_and_marks_penza():
    result = scraper.SiteResult(
        SITE,
        items=[
            scraper.ScrapedItem("В Пензе водитель такси попал в ДТП", "https://a.ru/news/1/x"),
            scraper.ScrapedItem("Открылась выставка акварели", "https://a.ru/news/2/x"),
        ],
        links_total=2,
    )
    feed_result = fetcher.articles_from_site(result)

    assert len(feed_result.articles) == 1, "нерелевантная новость отсеивается"
    article = feed_result.articles[0]
    assert article.is_penza and article.source == "Тест"
    assert article.summary == "", "текст дочитает extractor по прямой ссылке"


def test_incident_section_in_url_helps_classification():
    """Заголовку не хватает слов, но раздел сайта назван прямо."""
    result = scraper.SiteResult(
        SITE,
        items=[
            scraper.ScrapedItem(
                "Стало известно состояние пострадавшего",
                "https://www.penzainform.ru/news/incidents/2026/07/31/x",
            )
        ],
        links_total=1,
    )
    articles = fetcher.articles_from_site(result).articles
    assert [a.topic for a in articles] == [TOPIC_INCIDENT]


def test_articles_from_site_passes_error_through():
    result = scraper.SiteResult(SITE, error="404")
    assert fetcher.articles_from_site(result).broken


def test_scraped_links_are_direct(monkeypatch):
    """Ради этого всё и затевалось: ссылка ведёт в издание, а не на агрегатор."""
    result = scraper.SiteResult(
        SITE,
        items=[scraper.ScrapedItem("В Пензе произошёл пожар в доме", "https://riapo.ru/penza/x/y")],
        links_total=1,
    )
    article = fetcher.articles_from_site(result).articles[0]
    assert not fetcher.is_aggregator_link(article.url)
