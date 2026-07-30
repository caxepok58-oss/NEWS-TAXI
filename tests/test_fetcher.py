"""Отбор, классификация и дедупликация новостей."""

from __future__ import annotations

from datetime import timedelta

import pytest

import fetcher
from fetcher import TOPIC_AUTO, TOPIC_INCIDENT, TOPIC_TAXI, Feed


# --------------------------------------------------------------------------- #
# Нормализация
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("https://WWW.Site.ru/news/1/?utm_source=a&id=5#frag", "https://site.ru/news/1?id=5"),
        ("http://site.ru/news/1", "https://site.ru/news/1"),
        ("https://site.ru/news/1?yclid=99", "https://site.ru/news/1"),
    ],
)
def test_normalize_url(raw, expected):
    assert fetcher.normalize_url(raw) == expected


def test_normalize_title_ignores_case_punctuation_and_yo():
    a = fetcher.normalize_title("Пожар: всё серьёзно!")
    b = fetcher.normalize_title("пожар  все серьезно")
    assert a == b


def test_clean_text_strips_html_and_truncates():
    text = fetcher.clean_text("<p>Привет&nbsp;<b>мир</b></p>" + "a" * 900, limit=50)
    assert "<" not in text and "&nbsp;" not in text
    assert text.startswith("Привет мир")
    assert len(text) <= 51  # плюс многоточие


# --------------------------------------------------------------------------- #
# Классификация
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "text, expected",
    [
        ("в пензе водитель такси попал в дтп", TOPIC_TAXI),
        ("автоваз повысил цены на автомобили lada", TOPIC_AUTO),
        ("в пензе произошел пожар в жилом доме", TOPIC_INCIDENT),
        ("минфин обсудил ставку по вкладам", None),
    ],
)
def test_classify(text, expected):
    topic, _ = fetcher.classify(text, Feed("Тест", "https://x", region=None))
    assert topic == expected


def test_incidents_outside_penza_are_ignored():
    topic, _ = fetcher.classify("в самаре произошел пожар", Feed("Тест", "https://x"))
    assert topic is None


@pytest.mark.parametrize(
    "text",
    [
        "минфин обсудил ставку по вкладам",  # «вкладам» содержит «лада»
        "на складе завершили укладку плитки",  # «складе», «укладку»
        "жители получили автономию в решениях",  # «автономию» содержит «авто»
    ],
)
def test_keywords_match_word_start_not_any_substring(text):
    """Поиск по подстроке затаскивал в выпуск посторонние новости."""
    topic, _ = fetcher.classify(text, Feed("Тест", "https://x"))
    assert topic is None


def test_keyword_matching_keeps_russian_morphology():
    topic, hits = fetcher.classify(
        "в автомобилях lada подорожали запчасти", Feed("Тест", "https://x")
    )
    assert topic == TOPIC_AUTO
    assert "автомобил" in hits and "lada" in hits


def test_generic_transport_words_do_not_trigger_taxi():
    """«Перевозчик» и подобные общие слова давали ложные срабатывания."""
    topic, _ = fetcher.classify(
        "ржд: перевозчик добавил места в поезда к морю", Feed("Тест", "https://x")
    )
    assert topic != TOPIC_TAXI


def test_feed_topic_used_when_keywords_silent():
    topic, _ = fetcher.classify(
        "обзор рынка за неделю", Feed("Авто", "https://x", topic=TOPIC_AUTO)
    )
    assert topic == TOPIC_AUTO


# --------------------------------------------------------------------------- #
# Рейтинг
# --------------------------------------------------------------------------- #


def test_penza_taxi_scores_above_federal(article_factory):
    local = article_factory(topic=TOPIC_TAXI, is_penza=True, keywords=("такси",))
    federal = article_factory(topic=TOPIC_TAXI, is_penza=False, keywords=("такси",))
    assert fetcher.score(local) > fetcher.score(federal)


def test_fresher_news_scores_higher(article_factory, now):
    fresh = article_factory(published=now - timedelta(hours=1))
    stale = article_factory(published=now - timedelta(hours=30))
    assert fetcher.score(fresh) > fetcher.score(stale)


def test_aggregator_link_is_penalised(article_factory):
    direct = article_factory(url="https://penzaobzor.ru/news/1")
    aggregated = article_factory(url="https://news.google.com/rss/articles/CBMi123")
    assert fetcher.score(direct) > fetcher.score(aggregated)


# --------------------------------------------------------------------------- #
# Дедупликация
# --------------------------------------------------------------------------- #


def test_same_story_matches_different_wordings():
    assert fetcher.same_story(
        "Пожар на складе Wildberries под Пензой локализован",
        "Для тушения пожара на складе Wildberries прибыл вертолет МЧС",
    )


def test_same_story_keeps_unrelated_incidents_apart():
    assert not fetcher.same_story(
        "В Пензе сбили пешехода на улице Ленина",
        "В Кузнецке произошло ДТП с участием автобуса",
    )


def test_dedupe_drops_duplicate_urls_and_stories(article_factory):
    first = article_factory(title="Пожар на складе Wildberries локализован")
    same_url = article_factory(title="Другой заголовок", url=first.url)
    same_story = article_factory(
        title="Для тушения пожара на складе Wildberries прибыл вертолет"
    )
    other = article_factory(title="В Пензе подорожал проезд в маршрутках")

    result = fetcher._dedupe([first, same_url, same_story, other])
    assert [a.title for a in result] == [first.title, other.title]


def test_published_articles_are_filtered(conn, article_factory):
    article = article_factory()
    assert not fetcher.is_published(conn, article)
    fetcher.mark_published(conn, [article])
    assert fetcher.is_published(conn, article)


def test_dedup_matches_by_title_when_url_differs(conn, article_factory):
    published = article_factory(title="Одна и та же новость", url="https://a.ru/1")
    fetcher.mark_published(conn, [published])
    reprint = article_factory(title="Одна и та же новость", url="https://b.ru/2")
    assert fetcher.is_published(conn, reprint)


def test_purge_old_removes_stale_rows(conn, article_factory):
    fetcher.mark_published(conn, [article_factory()])
    assert fetcher.stats(conn)["published_total"] == 1
    assert fetcher.purge_old(conn, days=0) == 1
    assert fetcher.stats(conn)["published_total"] == 0


# --------------------------------------------------------------------------- #
# Отбор
# --------------------------------------------------------------------------- #


def test_pick_balances_topics(article_factory):
    """Одна «горячая» тема не должна занимать весь выпуск."""
    articles = [
        article_factory(title=f"Происшествие {i}", topic=TOPIC_INCIDENT, score=10 - i)
        for i in range(10)
    ]
    articles += [
        article_factory(title=f"Авто {i}", topic=TOPIC_AUTO, score=3 - i) for i in range(3)
    ]
    articles += [article_factory(title="Такси", topic=TOPIC_TAXI, score=1)]

    picked = fetcher._pick(articles, max_items=6)
    topics = {a.topic for a in picked}
    assert topics == {TOPIC_TAXI, TOPIC_AUTO, TOPIC_INCIDENT}
    assert sum(1 for a in picked if a.topic == TOPIC_INCIDENT) <= 3


def test_pick_fills_from_available_topics(article_factory):
    articles = [
        article_factory(title=f"Авто {i}", topic=TOPIC_AUTO, score=5 - i) for i in range(5)
    ]
    picked = fetcher._pick(articles, max_items=3)
    assert len(picked) == 3


def test_pick_respects_limit(article_factory):
    articles = [
        article_factory(title=f"Новость {i}", topic=TOPIC_AUTO, score=1) for i in range(20)
    ]
    assert len(fetcher._pick(articles, max_items=8)) == 8


# --------------------------------------------------------------------------- #
# Разбор лент
# --------------------------------------------------------------------------- #

RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
<item>
  <title>В Пензе таксист попал в ДТП</title>
  <link>https://penzaobzor.ru/news/1</link>
  <description>&lt;p&gt;Подробности происшествия.&lt;/p&gt;</description>
  <pubDate>Wed, 30 Jul 2036 09:00:00 +0000</pubDate>
  <enclosure url="https://penzaobzor.ru/img/1.jpg" type="image/jpeg" length="1000"/>
</item>
<item>
  <title>Минфин обсудил ставку по вкладам</title>
  <link>https://penzaobzor.ru/news/2</link>
  <description>Не наша тема.</description>
  <pubDate>Wed, 30 Jul 2036 08:00:00 +0000</pubDate>
</item>
</channel></rss>
"""


def test_fetch_feed_parses_and_filters(monkeypatch):
    class FakeResponse:
        content = RSS.encode("utf-8")

        def raise_for_status(self):
            return None

    monkeypatch.setattr(fetcher.requests, "get", lambda *a, **k: FakeResponse())
    articles = fetcher._fetch_feed(Feed("Тест", "https://x", region="penza"))

    assert len(articles) == 1, "нерелевантная новость должна отсеяться"
    article = articles[0]
    assert article.topic == TOPIC_TAXI
    assert article.is_penza
    assert article.image_url == "https://penzaobzor.ru/img/1.jpg"
    assert "<p>" not in article.summary


def test_collect_end_to_end(monkeypatch, conn):
    """Полный путь collect(): ленты -> фильтры -> отбор кандидатов."""

    class FakeResponse:
        content = RSS.encode("utf-8")

        def raise_for_status(self):
            return None

    monkeypatch.setattr(fetcher.requests, "get", lambda *a, **k: FakeResponse())
    monkeypatch.setattr(fetcher, "FEEDS", (Feed("Тест", "https://x", region="penza"),))
    monkeypatch.setattr(fetcher, "FALLBACK_FEEDS", ())

    candidates = fetcher.collect(conn, min_items=1, limit=20, max_age_hours=10**6)
    assert len(candidates) == 1
    assert candidates[0].topic == TOPIC_TAXI

    # После публикации та же новость больше не возвращается.
    fetcher.mark_published(conn, candidates)
    assert fetcher.collect(conn, min_items=1, limit=20, max_age_hours=10**6) == []


def test_collect_respects_limit(monkeypatch, conn, article_factory):
    many = [article_factory(title=f"Авто {i}", topic=TOPIC_AUTO) for i in range(30)]
    monkeypatch.setattr(fetcher, "fetch_feeds", lambda feeds, **kw: list(many))
    monkeypatch.setattr(fetcher, "FALLBACK_FEEDS", ())
    assert len(fetcher.collect(conn, min_items=5, limit=12)) == 12


def test_collect_uses_fallback_when_topic_missing(monkeypatch, conn, article_factory):
    """Если по теме нет новостей, подключается поиск именно по ней."""
    primary = [article_factory(title=f"Авто {i}", topic=TOPIC_AUTO) for i in range(8)]
    extra = [article_factory(title="Новости такси", topic=TOPIC_TAXI)]
    used_feeds: list = []

    def fake_fetch(feeds, **kwargs):
        feeds = list(feeds)
        used_feeds.extend(feeds)
        return list(primary) if feeds and feeds[0].tier == "primary" else list(extra)

    monkeypatch.setattr(fetcher, "fetch_feeds", fake_fetch)
    candidates = fetcher.collect(conn, min_items=5, limit=20)

    assert any(f.tier == "fallback" for f in used_feeds), "поиск не подключился"
    assert {f.topic for f in used_feeds if f.tier == "fallback"} == {TOPIC_TAXI, TOPIC_INCIDENT}
    assert any(a.topic == TOPIC_TAXI for a in candidates)


def test_fetch_feed_survives_network_error(monkeypatch):
    def boom(*args, **kwargs):
        raise fetcher.requests.RequestException("нет сети")

    monkeypatch.setattr(fetcher.requests, "get", boom)
    assert fetcher._fetch_feed(Feed("Тест", "https://x")) == []
