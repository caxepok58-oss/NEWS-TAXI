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


def test_search_feed_topic_is_not_trusted_blindly():
    """Поиск возвращает что угодно похожее — от него требуем совпадения слов."""
    search_feed = Feed("Поиск", "https://x", topic=TOPIC_AUTO, tier="fallback")
    topic, _ = fetcher.classify("суд рассмотрит спор двух компаний", search_feed)
    assert topic is None

    topic, _ = fetcher.classify("продажи автомобилей выросли", search_feed)
    assert topic == TOPIC_AUTO


@pytest.mark.parametrize(
    "text",
    [
        "в воронежской области автомобиль столкнулся с локомотивом",
        "на урале в дтп погиб подросток",
        "в москве сгорел автомобиль на парковке",
    ],
)
def test_incidents_outside_penza_do_not_leak_into_auto(text):
    """Чужое ДТП — не новость авторынка и не наша рубрика происшествий."""
    topic, _ = fetcher.classify(text, Feed("Тест", "https://x"))
    assert topic is None


def test_market_news_survives_incident_words():
    """Отзыв партии машин — новость рынка, хотя в тексте есть слово «аварий»."""
    topic, _ = fetcher.classify(
        "отзыв автомобилей lada после аварий: завод меняет тормоза",
        Feed("Тест", "https://x"),
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
    result = fetcher._fetch_feed(Feed("Тест", "https://x", region="penza"))

    assert result.entries_total == 2
    assert not result.broken
    assert len(result.articles) == 1, "нерелевантная новость должна отсеяться"
    article = result.articles[0]
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

    result = fetcher.collect(conn, min_items=1, limit=20, max_age_hours=10**6)
    assert len(result.candidates) == 1
    assert result.candidates[0].topic == TOPIC_TAXI

    # После публикации та же новость больше не возвращается.
    fetcher.mark_published(conn, result.candidates)
    assert fetcher.collect(conn, min_items=1, limit=20, max_age_hours=10**6).candidates == []


def _feed_results(articles, tier: str = "primary"):
    """Ответ fetch_feeds: одна лента со всеми переданными новостями."""
    feed = Feed("Тест", "https://x", tier=tier)
    return [fetcher.FeedResult(feed, articles=list(articles), entries_total=len(articles))]


def test_collect_respects_limit(monkeypatch, conn, article_factory):
    many = [article_factory(title=f"Авто {i}", topic=TOPIC_AUTO) for i in range(30)]
    monkeypatch.setattr(fetcher, "fetch_feeds", lambda feeds, **kw: _feed_results(many))
    monkeypatch.setattr(fetcher, "FALLBACK_FEEDS", ())
    assert len(fetcher.collect(conn, min_items=5, limit=12).candidates) == 12


def test_collect_uses_fallback_when_topic_missing(monkeypatch, conn, article_factory):
    """Если по теме нет новостей, подключается поиск именно по ней."""
    primary = [article_factory(title=f"Авто {i}", topic=TOPIC_AUTO) for i in range(8)]
    extra = [article_factory(title="Новости такси", topic=TOPIC_TAXI)]
    used_feeds: list = []

    def fake_fetch(feeds, **kwargs):
        feeds = list(feeds)
        used_feeds.extend(feeds)
        if feeds and feeds[0].tier == "primary":
            return _feed_results(primary)
        return _feed_results(extra, tier="fallback")

    monkeypatch.setattr(fetcher, "fetch_feeds", fake_fetch)
    result = fetcher.collect(conn, min_items=5, limit=20)

    assert any(f.tier == "fallback" for f in used_feeds), "поиск не подключился"
    assert {f.topic for f in used_feeds if f.tier == "fallback"} == {TOPIC_TAXI, TOPIC_INCIDENT}
    assert any(a.topic == TOPIC_TAXI for a in result.candidates)


# --------------------------------------------------------------------------- #
# Дедупликация сюжетов между выпусками
# --------------------------------------------------------------------------- #


def test_known_story_is_filtered_across_runs(monkeypatch, conn, article_factory):
    """Вчерашний сюжет, переписанный другим изданием, не должен пройти снова."""
    yesterday = article_factory(
        title="Пожар на складе Wildberries под Пензой локализован",
        url="https://a.ru/1",
        source="Издание А",
    )
    fetcher.mark_published(conn, [yesterday])

    today = article_factory(
        title="Для тушения пожара на складе Wildberries прибыл вертолет МЧС",
        url="https://b.ru/2",
        source="Издание Б",
        topic=TOPIC_INCIDENT,
        is_penza=True,
    )
    assert not fetcher.is_published(conn, today), "хэши разные — точная проверка не поймает"
    assert fetcher.is_known_story(today, fetcher.recent_titles(conn))

    monkeypatch.setattr(fetcher, "fetch_feeds", lambda feeds, **kw: _feed_results([today]))
    monkeypatch.setattr(fetcher, "FALLBACK_FEEDS", ())
    assert fetcher.collect(conn, min_items=1, limit=20).candidates == []


def test_unrelated_news_passes_story_dedup(conn, article_factory):
    fetcher.mark_published(conn, [article_factory(title="Пожар на складе под Пензой")])
    other = article_factory(title="В Пензе подорожал проезд в маршрутках")
    assert not fetcher.is_known_story(other, fetcher.recent_titles(conn))


def test_recent_titles_respects_window(conn, article_factory):
    fetcher.mark_published(conn, [article_factory(title="Старая новость")])
    assert fetcher.recent_titles(conn, days=3) == ["Старая новость"]
    assert fetcher.recent_titles(conn, days=0) == []


# --------------------------------------------------------------------------- #
# Лимит на источник
# --------------------------------------------------------------------------- #


def test_limit_per_source_trims_and_backfills(article_factory):
    selected = [
        article_factory(title=f"АвтоСтат {i}", source="АвтоСтат", topic=TOPIC_AUTO)
        for i in range(4)
    ]
    pool = selected + [
        article_factory(title="Колёса", source="Kolesa.ru", topic=TOPIC_AUTO),
        article_factory(title="Пять колёс", source="5 колесо", topic=TOPIC_AUTO),
    ]
    result = fetcher.limit_per_source(selected, pool, max_per_source=2)

    assert len(result) == len(selected), "выпуск не должен худеть"
    assert sum(1 for a in result if a.source == "АвтоСтат") == 2
    assert {a.source for a in result} == {"АвтоСтат", "Kolesa.ru", "5 колесо"}


def test_limit_per_source_without_replacements(article_factory):
    selected = [
        article_factory(title=f"АвтоСтат {i}", source="АвтоСтат", topic=TOPIC_AUTO)
        for i in range(4)
    ]
    result = fetcher.limit_per_source(selected, [], max_per_source=2)
    assert len(result) == 2


def test_limit_per_source_disabled(article_factory):
    selected = [article_factory(title=f"N{i}", source="Один") for i in range(5)]
    assert len(fetcher.limit_per_source(selected, [], max_per_source=0)) == 5


# --------------------------------------------------------------------------- #
# Здоровье лент
# --------------------------------------------------------------------------- #


def test_feed_health_alerts_after_threshold(conn, monkeypatch):
    monkeypatch.setattr(fetcher, "FEED_ALERT_AFTER", 3)
    broken = fetcher.FeedResult(Feed("Сломанная", "https://x"), error="404")

    assert fetcher.record_feed_health(conn, [broken]) == []
    assert fetcher.record_feed_health(conn, [broken]) == []
    alerts = fetcher.record_feed_health(conn, [broken])
    assert [name for name, _, _ in alerts] == ["Сломанная"]

    # Повторно о той же ленте не сообщаем.
    assert fetcher.record_feed_health(conn, [broken]) == []


def test_feed_health_resets_after_recovery(conn, monkeypatch, article_factory):
    monkeypatch.setattr(fetcher, "FEED_ALERT_AFTER", 2)
    feed = Feed("Лента", "https://x")
    broken = fetcher.FeedResult(feed, error="500")
    healthy = fetcher.FeedResult(feed, articles=[article_factory()], entries_total=5)

    fetcher.record_feed_health(conn, [broken])
    assert fetcher.record_feed_health(conn, [broken])  # предупредили
    fetcher.record_feed_health(conn, [healthy])

    health = {row["name"]: row for row in fetcher.feed_health(conn)}
    assert health["Лента"]["broken_streak"] == 0
    assert health["Лента"]["alerted_at"] is None

    # Сломалась снова — предупреждаем заново.
    fetcher.record_feed_health(conn, [broken])
    assert fetcher.record_feed_health(conn, [broken])


def test_empty_feed_counts_as_broken_but_irrelevant_news_does_not(conn):
    feed = Feed("Лента", "https://x")
    assert fetcher.FeedResult(feed, entries_total=0).broken
    # Записи есть, но ни одна не по нашим темам — это нормально.
    assert not fetcher.FeedResult(feed, articles=[], entries_total=20).broken


# --------------------------------------------------------------------------- #
# Срочные новости
# --------------------------------------------------------------------------- #


def test_breaking_detects_severe_incident(article_factory, now):
    from datetime import timedelta as td

    severe = article_factory(
        title="В Пензе при взрыве газа погиб человек",
        summary="Идёт эвакуация жильцов.",
        topic=TOPIC_INCIDENT,
        is_penza=True,
        published=now - td(hours=1),
    )
    routine = article_factory(
        title="В Пензе на улице Ленина столкнулись две легковушки",
        topic=TOPIC_INCIDENT,
        is_penza=True,
        published=now - td(hours=1),
    )
    urgent = fetcher.find_breaking([severe, routine])
    assert [a.title for a in urgent] == [severe.title]


def test_breaking_detects_multi_source_story(article_factory, now):
    from datetime import timedelta as td

    first = article_factory(
        title="Крупная авария на проспекте Строителей в Пензе",
        source="Издание А",
        topic=TOPIC_INCIDENT,
        is_penza=True,
        published=now - td(hours=1),
    )
    second = article_factory(
        title="Авария на проспекте Строителей в Пензе собрала пробку",
        source="Издание Б",
        topic=TOPIC_INCIDENT,
        is_penza=True,
        published=now - td(hours=1),
    )
    assert fetcher.find_breaking([first, second], min_sources=2)


def test_breaking_ignores_stale_and_non_penza(article_factory, now):
    from datetime import timedelta as td

    stale = article_factory(
        title="В Пензе при взрыве газа погиб человек",
        topic=TOPIC_INCIDENT,
        is_penza=True,
        published=now - td(hours=20),
    )
    elsewhere = article_factory(
        title="В Самаре при взрыве газа погиб человек",
        topic=TOPIC_INCIDENT,
        is_penza=False,
        published=now - td(hours=1),
    )
    assert fetcher.find_breaking([stale, elsewhere]) == []


def test_breaking_ignores_auto_topic(article_factory, now):
    from datetime import timedelta as td

    article = article_factory(
        title="Массово отзывают автомобили в Пензе",
        topic=TOPIC_AUTO,
        is_penza=True,
        published=now - td(hours=1),
    )
    assert fetcher.find_breaking([article]) == []


def test_fetch_feed_survives_network_error(monkeypatch):
    def boom(*args, **kwargs):
        raise fetcher.requests.RequestException("нет сети")

    monkeypatch.setattr(fetcher.requests, "get", boom)
    result = fetcher._fetch_feed(Feed("Тест", "https://x"))
    assert result.articles == []
    assert result.broken and "нет сети" in result.error
