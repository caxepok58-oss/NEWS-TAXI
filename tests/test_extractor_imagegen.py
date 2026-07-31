"""Извлечение текста статьи и подготовка картинки."""

from __future__ import annotations

import pytest

import extractor
import imagegen
from fetcher import TOPIC_AUTO, TOPIC_TAXI

HTML = """
<html><body>
<nav><p>Меню сайта и прочая навигация, которая не относится к статье совсем</p></nav>
<script>var x = "Читайте также: другая новость про пожар в городе Пензе";</script>
<article>
  <p>Короткая подпись</p>
  <p>Первый абзац статьи, в котором подробно рассказывается о том, что произошло
     и какие последствия это имело для города и его жителей.</p>
  <p>Читайте также: совсем другая новость, которую не надо тащить в пересказ ни при
     каких обстоятельствах, потому что это обвязка сайта.</p>
  <p>Второй абзац статьи с деталями события, комментариями очевидцев и уточнением
     обстоятельств произошедшего накануне вечером.</p>
</article>
</body></html>
"""


# --------------------------------------------------------------------------- #
# extractor
# --------------------------------------------------------------------------- #


def test_extract_from_html_keeps_body_and_drops_junk():
    text = extractor.extract_from_html(HTML)
    assert "Первый абзац статьи" in text
    assert "Второй абзац статьи" in text
    assert "Читайте также" not in text, "обвязка сайта не должна попадать в текст"
    assert "Меню сайта" not in text
    assert "var x" not in text


def test_extract_from_html_respects_limit():
    assert len(extractor.extract_from_html(HTML, limit=100)) <= 100


def test_fetch_text_returns_empty_on_network_error(monkeypatch):
    def boom(*args, **kwargs):
        raise extractor.requests.RequestException("нет сети")

    monkeypatch.setattr(extractor.requests, "get", boom)
    assert extractor.fetch_text("https://a.ru/1") == ""


def test_enrich_skips_aggregator_links(monkeypatch, article_factory):
    monkeypatch.setattr(extractor, "ENABLED", True)
    monkeypatch.setattr(extractor, "fetch_text", lambda url, **kw: "ТЕКСТ")

    direct = article_factory(url="https://penzaobzor.ru/1")
    aggregated = article_factory(url="https://news.google.com/rss/articles/CBMi1")
    extractor.enrich([direct, aggregated])

    assert direct.full_text == "ТЕКСТ"
    assert aggregated.full_text == "", "у ссылки на агрегатор статьи нет"


def test_enrich_is_noop_when_disabled(monkeypatch, article_factory):
    monkeypatch.setattr(extractor, "ENABLED", False)
    article = article_factory()
    extractor.enrich([article])
    assert article.full_text == ""


def test_article_body_prefers_full_text(article_factory):
    article = article_factory(summary="из ленты", full_text="из статьи")
    assert article.body == "из статьи"
    article.full_text = ""
    assert article.body == "из ленты"


# --------------------------------------------------------------------------- #
# imagegen
# --------------------------------------------------------------------------- #


def test_render_cover_produces_png(article_factory):
    data = imagegen.render_cover(
        "Дайджест: такси, авторынок и происшествия в Пензе",
        [article_factory(topic=TOPIC_TAXI), article_factory(topic=TOPIC_AUTO)],
    )
    assert data is not None
    assert data.startswith(b"\x89PNG"), "должен получиться PNG"
    assert len(data) > 5000


def test_render_cover_handles_very_long_title():
    data = imagegen.render_cover("Очень длинный заголовок выпуска. " * 12)
    assert data is not None and data.startswith(b"\x89PNG")


def test_cover_size_is_within_telegram_limits(article_factory):
    from PIL import Image
    import io

    data = imagegen.render_cover("Тест", [article_factory()])
    image = Image.open(io.BytesIO(data))
    assert image.size == (imagegen.WIDTH, imagegen.HEIGHT)
    assert len(data) < imagegen.MAX_IMAGE_BYTES


def test_short_topic_labels_cover_all_topics():
    from fetcher import TOPIC_ORDER

    assert set(imagegen._SHORT_TOPICS) == set(TOPIC_ORDER)


def test_cover_with_all_topics_fits(article_factory):
    """Строка тем не должна вылезать за правый край обложки."""
    from PIL import ImageDraw, Image
    from fetcher import TOPIC_ORDER

    articles = [article_factory(topic=topic) for topic in TOPIC_ORDER] * 4
    assert imagegen.render_cover("Заголовок выпуска", articles) is not None

    draw = ImageDraw.Draw(Image.new("RGB", (imagegen.WIDTH, imagegen.HEIGHT)))
    line = "   ·   ".join(f"{name} — 12" for name in imagegen._SHORT_TOPICS.values())
    text_width = imagegen.WIDTH - (64 + 34) - 64
    assert draw.textlength(line, font=imagegen._font(26, bold=False)) <= text_width


def test_cover_date_uses_channel_timezone(monkeypatch):
    """Сервер живёт по UTC: без пересчёта ночной пост получил бы вчерашнее число."""
    from datetime import datetime, timezone as tz

    monkeypatch.setattr(imagegen, "TIMEZONE", "Europe/Moscow")
    now = imagegen._now_local()
    utc_now = datetime.now(tz.utc)
    assert now.utcoffset() is not None, "время должно быть с зоной"
    assert now.hour == (utc_now.hour + 3) % 24


def test_cover_falls_back_on_unknown_timezone(monkeypatch):
    monkeypatch.setattr(imagegen, "TIMEZONE", "Нет/Такого")
    assert imagegen._now_local() is not None


def test_build_image_off_returns_none(article_factory):
    assert imagegen.build_image("Тест", [article_factory()], mode="off") is None


def test_build_image_source_mode_uses_feed_photo(monkeypatch, article_factory):
    monkeypatch.setattr(imagegen, "download_image", lambda url: b"JPEGDATA" if url else None)
    article = article_factory(image_url="https://a.ru/photo.jpg")
    assert imagegen.build_image("Тест", [article], mode="source") == b"JPEGDATA"


def test_build_image_source_mode_falls_back_to_cover(monkeypatch, article_factory):
    monkeypatch.setattr(imagegen, "download_image", lambda url: None)
    data = imagegen.build_image("Тест", [article_factory()], mode="source")
    assert data is not None and data.startswith(b"\x89PNG")


def test_build_image_ai_mode_falls_back_to_cover_without_keys(monkeypatch, article_factory):
    monkeypatch.setattr(imagegen, "FUSIONBRAIN_API_KEY", "")
    monkeypatch.setattr(imagegen, "FUSIONBRAIN_SECRET_KEY", "")
    data = imagegen.build_image("Тест", [article_factory()], "сцена", mode="ai")
    assert data is not None and data.startswith(b"\x89PNG")


def test_generate_image_returns_none_without_keys(monkeypatch):
    monkeypatch.setattr(imagegen, "FUSIONBRAIN_API_KEY", "")
    assert imagegen.generate_image("любой промпт") is None


def test_download_image_rejects_non_image(monkeypatch):
    class FakeResponse:
        headers = {"Content-Type": "text/html"}
        content = b"<html>"

        def raise_for_status(self):
            return None

    monkeypatch.setattr(imagegen.requests, "get", lambda *a, **k: FakeResponse())
    assert imagegen.download_image("https://a.ru/x") is None


def test_download_image_rejects_oversized(monkeypatch):
    class FakeResponse:
        headers = {"Content-Type": "image/jpeg"}
        content = b"x" * (imagegen.MAX_IMAGE_BYTES + 1)

        def raise_for_status(self):
            return None

    monkeypatch.setattr(imagegen.requests, "get", lambda *a, **k: FakeResponse())
    assert imagegen.download_image("https://a.ru/big.jpg") is None
