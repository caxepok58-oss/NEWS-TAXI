"""Отбор моделью, разбор ответа и сборка поста."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import anthropic
import httpx
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


# --------------------------------------------------------------------------- #
# Даты в промпте
# --------------------------------------------------------------------------- #


def test_prompt_carries_dates(article_factory):
    """Без дат модель не может корректно написать «сегодня» — и выдумывает."""
    from datetime import datetime, timezone

    article = article_factory(published=datetime(2026, 7, 30, 6, 15, tzinfo=timezone.utc))
    prompt = summarizer._digest_prompt([article])
    assert "Сегодня" in prompt
    assert "Опубликовано: 30.07.2026" in prompt


def test_prompt_handles_missing_date(article_factory):
    article = article_factory(published=None)
    assert "время неизвестно" in summarizer._digest_prompt([article])


# --------------------------------------------------------------------------- #
# Пересказ своими словами
# --------------------------------------------------------------------------- #

SOURCE_TEXT = (
    "В региональной Госавтоинспекции сообщили подробности аварии, которая "
    "произошла двадцать девятого июля в Каменке на улице Чернышевского."
)


def test_borrowed_fragment_detects_copy():
    copied = "В региональной Госавтоинспекции сообщили подробности аварии, которая произошла"
    assert summarizer.borrowed_fragment(copied, SOURCE_TEXT, n=7)


def test_borrowed_fragment_allows_paraphrase():
    own = "Автоинспекция раскрыла обстоятельства ДТП в Каменке."
    assert summarizer.borrowed_fragment(own, SOURCE_TEXT, n=7) == ""


def test_borrowed_fragment_ignores_short_summaries():
    assert summarizer.borrowed_fragment("Авария в Каменке", SOURCE_TEXT, n=7) == ""


def test_enforce_own_words_asks_for_rewrite(article_factory):
    article = article_factory(full_text=SOURCE_TEXT)
    data = {
        "title": "T",
        "intro": "I",
        "items": [
            {
                "id": 1,
                "headline": "H",
                "summary": "В региональной Госавтоинспекции сообщили подробности аварии, которая произошла",
            }
        ],
    }
    client = client_returning({"items": [{"id": 1, "summary": "Автоинспекция раскрыла детали ДТП."}]})

    result = summarizer.enforce_own_words(data, [article], client=client)

    assert result["items"][0]["summary"] == "Автоинспекция раскрыла детали ДТП."
    client.messages.create.assert_called_once()


def test_enforce_own_words_drops_summary_if_still_copied(article_factory):
    """Лучше один заголовок, чем абзац, скопированный у издания."""
    copied = "В региональной Госавтоинспекции сообщили подробности аварии, которая произошла"
    article = article_factory(full_text=SOURCE_TEXT)
    data = {"title": "T", "intro": "I", "items": [{"id": 1, "headline": "H", "summary": copied}]}
    client = client_returning({"items": [{"id": 1, "summary": copied}]})

    result = summarizer.enforce_own_words(data, [article], client=client)
    assert result["items"][0]["summary"] == ""


def test_enforce_own_words_skips_clean_digest(article_factory):
    article = article_factory(full_text=SOURCE_TEXT)
    data = {
        "title": "T",
        "intro": "I",
        "items": [{"id": 1, "headline": "H", "summary": "Автоинспекция раскрыла детали ДТП."}],
    }
    client = MagicMock()
    assert summarizer.enforce_own_words(data, [article], client=client) is data
    client.messages.create.assert_not_called()


def test_enforce_own_words_survives_api_error(article_factory):
    copied = "В региональной Госавтоинспекции сообщили подробности аварии, которая произошла"
    article = article_factory(full_text=SOURCE_TEXT)
    data = {"title": "T", "intro": "I", "items": [{"id": 1, "headline": "H", "summary": copied}]}
    client = MagicMock()
    client.messages.create.side_effect = anthropic.APIConnectionError(request=MagicMock())

    result = summarizer.enforce_own_words(data, [article], client=client)
    assert result["items"][0]["summary"] == "", "при сбое описание убираем"


def test_build_digest_runs_copy_check(two_articles):
    """Проверка на копирование встроена в основной путь."""
    copied = " ".join(f"слово{i}" for i in range(12))
    two_articles[0].full_text = copied
    payload = {
        "title": "T",
        "intro": "I",
        "items": [
            {"id": 1, "headline": "H1", "summary": copied},
            {"id": 2, "headline": "H2", "summary": "Свой текст."},
        ],
    }
    client = client_returning(payload, {"items": [{"id": 1, "summary": "Переписано иначе."}]})
    digest = summarizer.build_digest(two_articles, client=client)
    assert copied not in digest.text
    assert "Переписано иначе." in digest.text


# --------------------------------------------------------------------------- #
# Выбор адреса API
# --------------------------------------------------------------------------- #


@pytest.fixture
def no_fallback(monkeypatch):
    monkeypatch.setattr(summarizer, "FALLBACK_API_KEY", "")


def test_make_client_uses_official_api_by_default(monkeypatch, no_fallback):
    """Пустой ANTHROPIC_BASE_URL — обращаемся напрямую к Anthropic."""
    # SDK сам читает одноимённую переменную окружения, поэтому убираем её:
    # иначе тест зависел бы от настроек машины, на которой запущен.
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.setattr(summarizer, "BASE_URL", "")
    assert "api.anthropic.com" in str(summarizer.make_client().base_url)


def test_make_client_ignores_empty_base_url_in_environment(monkeypatch, no_fallback):
    """Пустая ANTHROPIC_BASE_URL из .env не должна сходить за адрес.

    load_dotenv кладёт пустую строку в окружение, а SDK принимал её за base_url —
    и каждый запрос падал с «Request URL is missing a protocol».
    """
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "")
    monkeypatch.setattr(summarizer, "BASE_URL", "")
    assert "api.anthropic.com" in str(summarizer.make_client().base_url)


def test_make_client_honours_custom_base_url(monkeypatch, no_fallback):
    """Непустой ANTHROPIC_BASE_URL — запросы идут через указанный шлюз."""
    monkeypatch.setattr(summarizer, "BASE_URL", "https://gateway.example/")
    assert "gateway.example" in str(summarizer.make_client().base_url)


def test_trailing_v1_is_dropped_from_base_url(monkeypatch, no_fallback):
    """Шлюзы пишут адрес с /v1, а SDK допишет его сам — вышло бы /v1/v1/messages."""
    monkeypatch.setattr(summarizer, "BASE_URL", "https://gateway.example/v1/")
    assert str(summarizer.make_client().base_url).rstrip("/") == "https://gateway.example"


@pytest.mark.parametrize("factory", ["select_best", "request_digest"])
def test_entry_points_build_client_via_make_client(monkeypatch, two_articles, factory):
    """Ни одна точка входа не создаёт клиент в обход make_client."""
    created = MagicMock()
    created.messages.create.return_value = anthropic_message(
        json.dumps({"selected_ids": [1], "title": "T", "intro": "I", "items": []})
    )
    monkeypatch.setattr(summarizer, "make_client", lambda: created)
    # min_items=1: иначе select_best вернёт короткий список кандидатов, не спросив модель.
    kwargs = {"min_items": 1} if factory == "select_best" else {}
    try:
        getattr(summarizer, factory)(two_articles, **kwargs)
    except summarizer.SummarizerError:
        pass  # пустой items — здесь неважно, проверяем источник клиента
    assert created.messages.create.called


# --------------------------------------------------------------------------- #
# Запасной провайдер
# --------------------------------------------------------------------------- #

PRIMARY = summarizer.Provider("основной", "https://a.example", "", "model-a")
SECONDARY = summarizer.Provider("запасной", "https://b.example", "key-b", "model-b")


@pytest.fixture(autouse=True)
def clean_failover_notes():
    summarizer.take_failover_notes()
    yield
    summarizer.take_failover_notes()


def _request() -> httpx.Request:
    return httpx.Request("POST", "https://a.example/v1/messages")


def status_error(code: int) -> anthropic.APIStatusError:
    response = httpx.Response(code, request=_request(), json={"error": {"message": "сбой"}})
    return anthropic.APIStatusError("сбой", response=response, body=None)


def mock_client(*effects):
    client = MagicMock()
    client.messages.create.side_effect = list(effects)
    return client


def failover(primary, secondary):
    return summarizer.FailoverClient([PRIMARY, SECONDARY], clients=[primary, secondary])


def test_failover_stays_on_primary_when_it_answers():
    primary = mock_client(anthropic_message('{"ok": true}'))
    secondary = mock_client()
    failover(primary, secondary).messages.create(max_tokens=10, messages=[])

    assert primary.messages.create.call_args.kwargs["model"] == "model-a"
    secondary.messages.create.assert_not_called()
    assert summarizer.take_failover_notes() == []


@pytest.mark.parametrize(
    "error",
    [
        anthropic.APIConnectionError(request=_request()),
        anthropic.APITimeoutError(request=_request()),
        status_error(500),
        status_error(429),
        status_error(401),
        status_error(402),
        status_error(403),
        status_error(404),
    ],
    ids=["connection", "timeout", "500", "429", "401", "402", "403", "404"],
)
def test_failover_switches_when_provider_is_down(error):
    secondary = mock_client(anthropic_message('{"ok": true}'))
    message = failover(mock_client(error), secondary).messages.create(max_tokens=10, messages=[])

    assert _first_text_of(message) == '{"ok": true}'
    assert secondary.messages.create.call_args.kwargs["model"] == "model-b"
    notes = summarizer.take_failover_notes()
    assert len(notes) == 1 and "a.example" in notes[0] and "b.example" in notes[0]


def _first_text_of(message) -> str:
    return summarizer._first_text(message)


def test_failover_switches_on_empty_answer():
    """Шлюз ответил 200, но пусто — для выпуска это тот же сбой."""
    secondary = mock_client(anthropic_message('{"ok": true}'))
    failover(mock_client(anthropic_message("")), secondary).messages.create(messages=[])
    secondary.messages.create.assert_called_once()


def test_failover_does_not_retry_bad_request():
    """400 — плохой запрос: на другом шлюзе он не станет лучше."""
    secondary = mock_client()
    with pytest.raises(anthropic.APIStatusError):
        failover(mock_client(status_error(400)), secondary).messages.create(messages=[])
    secondary.messages.create.assert_not_called()


def test_failover_returns_refusal_as_is():
    """Отказ модели — не сбой провайдера: его разбирает _ask."""
    refusal = anthropic_message("", stop_reason="refusal")
    secondary = mock_client()
    assert failover(mock_client(refusal), secondary).messages.create(messages=[]) is refusal
    secondary.messages.create.assert_not_called()


def test_failover_raises_last_error_when_everyone_is_down():
    last = status_error(503)
    with pytest.raises(anthropic.APIStatusError) as exc:
        failover(mock_client(status_error(500)), mock_client(last)).messages.create(messages=[])
    assert exc.value is last
    assert summarizer.take_failover_notes() == [], "о переключении сообщаем, только если оно помогло"


def test_failover_notes_are_deduplicated():
    """Отбор, пересказ и переписывание падают одинаково — админу хватит одной строки."""
    client = failover(
        mock_client(status_error(402), status_error(402)),
        mock_client(anthropic_message("{}"), anthropic_message("{}")),
    )
    client.messages.create(messages=[])
    client.messages.create(messages=[])
    assert len(summarizer.take_failover_notes()) == 1
    assert summarizer.take_failover_notes() == [], "сообщения забираются один раз"


def test_failover_hides_keys_in_error_text(monkeypatch):
    monkeypatch.setattr(summarizer, "FALLBACK_API_KEY", "sk-secret-fallback-key")
    error = status_error(401)
    error.message = "Error code: 401 - неверный ключ sk-secret-fallback-key"
    assert summarizer._describe(error) == "HTTP 401: неверный ключ ***"


def test_request_digest_survives_primary_outage(two_articles):
    """Сквозной путь: шлюз лежит, выпуск собирается через запасного."""
    secondary = mock_client(anthropic_message(json.dumps(DIGEST_PAYLOAD, ensure_ascii=False)))
    client = failover(mock_client(anthropic.APIConnectionError(request=_request())), secondary)

    data = summarizer.request_digest(two_articles, client=client)

    assert data["title"] == "Дайджест дня"
    kwargs = secondary.messages.create.call_args.kwargs
    assert kwargs["model"] == "model-b"
    assert "output_config" in kwargs, "структурированный вывод доходит до запасного"


def test_providers_without_fallback(monkeypatch, no_fallback):
    assert [p.label for p in summarizer.providers()] == ["основной"]


def test_providers_with_fallback(monkeypatch):
    monkeypatch.setattr(summarizer, "FALLBACK_API_KEY", "key-b")
    monkeypatch.setattr(summarizer, "FALLBACK_BASE_URL", "https://b.example/v1")
    monkeypatch.setattr(summarizer, "FALLBACK_MODEL", "")

    chain = summarizer.providers()

    assert [p.label for p in chain] == ["основной", "запасной"]
    assert chain[1].base_url == "https://b.example"
    assert chain[1].model == summarizer.MODEL, "без своей модели берётся основная"
    assert isinstance(summarizer.make_client(), summarizer.FailoverClient)


# --------------------------------------------------------------------------- #
# Проверка провайдеров (--check-llm)
# --------------------------------------------------------------------------- #


def _check_with(monkeypatch, *clients):
    """check_providers с подменёнными клиентами — по одному на провайдера."""
    pool = iter(clients)
    monkeypatch.setattr(summarizer, "_build_client", lambda provider: next(pool))
    return summarizer.check_providers()


def test_check_reports_working_provider(monkeypatch, no_fallback):
    [check] = _check_with(monkeypatch, mock_client(anthropic_message('{"ok": true}')))
    assert check.ok and check.structured
    assert check.reported_model == "claude-haiku-4-5-20251001"
    assert (check.input_tokens, check.output_tokens) == (100, 200)


def test_check_notices_missing_structured_output(monkeypatch, no_fallback):
    unsupported = anthropic.BadRequestError(
        "unsupported", response=MagicMock(status_code=400, headers={}), body=None
    )
    [check] = _check_with(
        monkeypatch, mock_client(unsupported, anthropic_message('{"ok": true}'))
    )
    assert check.ok and not check.structured


def test_check_reports_each_provider_separately(monkeypatch):
    monkeypatch.setattr(summarizer, "FALLBACK_API_KEY", "key-b")
    results = _check_with(
        monkeypatch,
        mock_client(status_error(402), status_error(402)),
        mock_client(anthropic_message('{"ok": true}')),
    )
    assert [r.ok for r in results] == [False, True]
    assert results[0].error.startswith("HTTP 402")


@pytest.mark.parametrize(
    ("asked", "answered", "differs"),
    [
        ("claude-haiku-4-5", "claude-haiku-4-5-20251001", False),
        ("claude-haiku-4.5", "claude-haiku-4-5-20251001", False),
        ("claude-haiku-4-5", "claude-sonnet-4-5", True),
        ("claude-haiku-4-5", "", False),
    ],
)
def test_check_flags_other_model(asked, answered, differs):
    check = summarizer.ProviderCheck(
        summarizer.Provider("основной", "", "", asked), reported_model=answered
    )
    assert check.model_differs is differs
