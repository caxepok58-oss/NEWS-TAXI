"""Отбор моделью, разбор ответа и сборка поста."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import anthropic
import pytest

import summarizer
from conftest import anthropic_message
from fetcher import TOPIC_AUTO, TOPIC_INCIDENT, TOPIC_TAXI

DIGEST_PAYLOAD = {
    "title": "Дайджест дня",
    "intro": "Такси, авто и происшествия.",
    "image_prompt": "городская улица с автомобилями на рассвете",
    "items": [
        {"id": 1, "headline": "Список такси расширили", "summary": "Добавили шесть моделей."},
        {"id": 2, "headline": "Кроссовер сняли с выпуска", "summary": "Производство закрыто."},
    ],
}


@pytest.fixture
def two_articles(article_factory):
    return [
        article_factory(title="Локализованные авто для такси", topic=TOPIC_TAXI),
        article_factory(title="Tenet T4 сняли с производства", topic=TOPIC_AUTO),
    ]


def client_returning(*payloads):
    """Мок клиента Anthropic, отдающий заготовленные ответы по очереди."""
    client = MagicMock()
    responses = [
        anthropic_message(p if isinstance(p, str) else json.dumps(p, ensure_ascii=False))
        for p in payloads
    ]
    client.messages.create.side_effect = responses
    return client


# --------------------------------------------------------------------------- #
# Разбор ответа
# --------------------------------------------------------------------------- #


def test_extract_json_plain():
    assert summarizer._extract_json('{"a": 1}') == {"a": 1}


def test_extract_json_in_code_fence():
    assert summarizer._extract_json('```json\n{"a": 1}\n```') == {"a": 1}


def test_extract_json_with_surrounding_text():
    assert summarizer._extract_json('Вот ответ: {"a": 1} — готово') == {"a": 1}


def test_extract_json_raises_without_json():
    with pytest.raises(summarizer.SummarizerError):
        summarizer._extract_json("никакого json тут нет")


# --------------------------------------------------------------------------- #
# Запрос к модели
# --------------------------------------------------------------------------- #


def test_uses_configured_model_and_structured_output(two_articles):
    client = client_returning(DIGEST_PAYLOAD)
    summarizer.request_digest(two_articles, client=client)
    kwargs = client.messages.create.call_args.kwargs
    assert kwargs["model"] == summarizer.MODEL
    assert kwargs["output_config"]["format"]["type"] == "json_schema"


def test_falls_back_to_plain_json_when_structured_output_unsupported(two_articles):
    client = MagicMock()
    error = anthropic.BadRequestError(
        "unsupported", response=MagicMock(status_code=400, headers={}), body=None
    )
    client.messages.create.side_effect = [
        error,
        anthropic_message(json.dumps(DIGEST_PAYLOAD, ensure_ascii=False)),
    ]
    data = summarizer.request_digest(two_articles, client=client)
    assert data["title"] == "Дайджест дня"
    assert "output_config" not in client.messages.create.call_args.kwargs


def test_refusal_raises(two_articles):
    client = MagicMock()
    client.messages.create.return_value = anthropic_message("", stop_reason="refusal")
    with pytest.raises(summarizer.SummarizerError):
        summarizer.request_digest(two_articles, client=client)


def test_prompt_prefers_full_text_over_rss_summary(article_factory):
    article = article_factory(summary="Коротко из ленты.", full_text="Полный текст статьи.")
    prompt = summarizer._digest_prompt([article])
    assert "Полный текст статьи." in prompt
    assert "Коротко из ленты." not in prompt


# --------------------------------------------------------------------------- #
# Отбор моделью
# --------------------------------------------------------------------------- #


def test_select_best_returns_model_choice(article_factory):
    candidates = [article_factory(title=f"Новость {i}") for i in range(10)]
    client = client_returning({"selected_ids": [3, 1, 7, 2, 9]})
    chosen = summarizer.select_best(candidates, min_items=5, max_items=8, client=client)
    assert len(chosen) == 5
    assert {a.title for a in chosen} == {f"Новость {i - 1}" for i in (3, 1, 7, 2, 9)}


def test_select_best_ignores_out_of_range_ids(article_factory):
    candidates = [article_factory(title=f"Новость {i}") for i in range(6)]
    client = client_returning({"selected_ids": [1, 99, -5, 2]})
    chosen = summarizer.select_best(candidates, min_items=2, max_items=8, client=client)
    assert len(chosen) == 2


def test_select_best_falls_back_to_ranking_on_api_error(article_factory):
    candidates = [article_factory(title=f"Новость {i}") for i in range(10)]
    client = MagicMock()
    client.messages.create.side_effect = anthropic.APIConnectionError(request=MagicMock())
    chosen = summarizer.select_best(candidates, min_items=5, max_items=8, client=client)
    assert chosen == candidates[:8], "при сбое берём лучших по рейтингу"


def test_select_best_skips_api_call_for_small_pools(article_factory):
    candidates = [article_factory(title=f"Новость {i}") for i in range(3)]
    client = MagicMock()
    chosen = summarizer.select_best(candidates, min_items=5, max_items=8, client=client)
    assert chosen == candidates
    client.messages.create.assert_not_called()


def test_select_best_tops_up_when_model_returns_too_few(article_factory):
    candidates = [article_factory(title=f"Новость {i}") for i in range(10)]
    client = client_returning({"selected_ids": [1]})
    chosen = summarizer.select_best(candidates, min_items=5, max_items=8, client=client)
    assert len(chosen) >= 5


# --------------------------------------------------------------------------- #
# Рендеринг
# --------------------------------------------------------------------------- #


def test_render_uses_our_links_not_models(two_articles):
    payload = dict(DIGEST_PAYLOAD)
    text, _ = summarizer.render(payload, two_articles, links_mode="inline")
    for article in two_articles:
        assert article.url in text
        assert article.source in text


def test_render_escapes_html(article_factory):
    article = article_factory(title="Тест", source="A & B")
    payload = {
        "title": "Заголовок <b>",
        "intro": "",
        "items": [{"id": 1, "headline": "A & B", "summary": "1 < 2"}],
    }
    text, _ = summarizer.render(payload, [article], links_mode="inline")
    assert "&lt;b&gt;" in text and "&amp;" in text and "1 &lt; 2" in text


def test_render_groups_by_topic_and_numbers_sequentially(article_factory):
    articles = [
        article_factory(title="Такси", topic=TOPIC_TAXI),
        article_factory(title="Происшествие", topic=TOPIC_INCIDENT),
        article_factory(title="Авто", topic=TOPIC_AUTO),
    ]
    payload = {
        "title": "T",
        "intro": "I",
        "items": [
            {"id": i, "headline": f"H{i}", "summary": f"S{i}"} for i in (1, 2, 3)
        ],
    }
    text, _ = summarizer.render(payload, articles, links_mode="inline")
    assert text.index("Рынок такси") < text.index("Авто и авторынок") < text.index(
        "Происшествия"
    )
    assert "1. <b>H1</b>" in text and "2. <b>H3</b>" in text and "3. <b>H2</b>" in text


def test_render_drops_items_without_matching_article(article_factory):
    article = article_factory(title="Реальная новость")
    payload = {
        "title": "T",
        "intro": "I",
        "items": [
            {"id": 99, "headline": "Придуманная", "summary": "x"},
            {"id": 1, "headline": "Реальная", "summary": "y"},
        ],
    }
    text, _ = summarizer.render(payload, [article], links_mode="inline")
    assert "Придуманная" not in text
    assert "Реальная" in text


def test_render_raises_when_nothing_matches(article_factory):
    payload = {"title": "T", "intro": "I", "items": [{"id": 42, "headline": "x", "summary": "y"}]}
    with pytest.raises(summarizer.SummarizerError):
        summarizer.render(payload, [article_factory()])


def test_render_buttons_mode_moves_links_out_of_text(two_articles):
    text, buttons = summarizer.render(dict(DIGEST_PAYLOAD), two_articles, links_mode="buttons")
    assert "<a href" not in text, "в режиме кнопок ссылок в тексте быть не должно"
    assert len(buttons) == 2
    labels, urls = zip(*buttons)
    assert urls == tuple(a.url for a in two_articles)
    assert labels[0].startswith("1. ")
    for article in two_articles:
        assert article.source in text, "источник остаётся подписью в тексте"


def test_render_button_labels_are_short_enough(article_factory):
    article = article_factory(source="Очень длинное название издания " * 4)
    payload = {"title": "T", "intro": "", "items": [{"id": 1, "headline": "H", "summary": "S"}]}
    _, buttons = summarizer.render(payload, [article], links_mode="buttons")
    assert len(buttons[0][0]) <= 60, "Telegram не примет слишком длинную подпись"


def test_build_digest_reports_only_used_articles(two_articles, article_factory):
    articles = two_articles + [article_factory(title="Лишняя", topic=TOPIC_AUTO)]
    client = client_returning(DIGEST_PAYLOAD)
    digest = summarizer.build_digest(articles, client=client)
    assert len(digest.articles) == 2
    assert digest.title == "Дайджест дня"
    assert digest.image_prompt


def test_build_digest_requires_articles():
    with pytest.raises(summarizer.SummarizerError):
        summarizer.build_digest([], client=MagicMock())
