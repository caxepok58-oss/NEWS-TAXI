"""Сборка поста-дайджеста из отобранных новостей через Anthropic API.

Модель пересказывает каждую новость своими словами; ссылки и названия источников
подставляются нашим кодом, а не моделью, — так в посте не может появиться
выдуманный URL.
"""

from __future__ import annotations

import json
import logging
import os
import re
from html import escape
from typing import Sequence

import anthropic

from fetcher import TOPIC_ORDER, TOPIC_TITLES, Article

log = logging.getLogger(__name__)

MODEL = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")
MAX_TOKENS = int(os.getenv("ANTHROPIC_MAX_TOKENS", "4000"))

SYSTEM_PROMPT = """\
Ты — редактор ежедневного новостного дайджеста для Telegram-канала о рынке такси,
автомобилях и городских происшествиях в Пензе.

Правила:
1. Каждую новость пересказывай СВОИМИ СЛОВАМИ. Не копируй фразы и обороты из
   присланного описания, перестраивай формулировки.
2. Краткое описание — 1-2 предложения, по-русски, деловой нейтральный тон,
   без «воды», оценок и обращений к читателю.
3. Опирайся только на присланные заголовок и описание. Ничего не додумывай:
   не добавляй цифры, даты, имена и последствия, которых нет в исходных данных.
   Если описание пустое — перескажи только то, что следует из заголовка.
4. Заголовок новости в дайджесте — короткий (до 80 символов), информативный,
   не дословный повтор исходного.
5. Не используй markdown, HTML и эмодзи — только обычный текст.
6. Общий заголовок поста — короткий, отражает главные темы выпуска.
   Вступление — одно предложение о том, что в выпуске.
7. Верни ровно по одному элементу на каждую присланную новость, сохранив её id.
"""

DIGEST_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "Заголовок поста"},
        "intro": {"type": "string", "description": "Одно вводное предложение"},
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer", "description": "id присланной новости"},
                    "headline": {"type": "string"},
                    "summary": {"type": "string"},
                },
                "required": ["id", "headline", "summary"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["title", "intro", "items"],
    "additionalProperties": False,
}


class SummarizerError(RuntimeError):
    """Не удалось получить корректный дайджест от модели."""


# --------------------------------------------------------------------------- #
# Обращение к модели
# --------------------------------------------------------------------------- #


def _build_user_prompt(articles: Sequence[Article]) -> str:
    lines = [
        f"Сегодняшняя подборка: {len(articles)} новостей. "
        "Составь из них один дайджест.\n"
    ]
    for index, article in enumerate(articles, start=1):
        lines.append(f"[id: {index}]")
        lines.append(f"Тема: {TOPIC_TITLES[article.topic]}")
        lines.append(f"Источник: {article.source}")
        lines.append(f"Заголовок: {article.title}")
        lines.append(f"Описание: {article.summary or '(описание отсутствует)'}")
        lines.append("")
    return "\n".join(lines)


def _extract_json(text: str) -> dict:
    """Достаёт JSON-объект из ответа модели (на случай обрамляющего текста)."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.DOTALL)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if not match:
            raise SummarizerError("Модель вернула ответ без JSON")
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            raise SummarizerError(f"Не удалось разобрать JSON: {exc}") from exc


def _first_text(message) -> str:
    for block in message.content:
        if block.type == "text":
            return block.text
    return ""


def request_digest(articles: Sequence[Article], client: anthropic.Anthropic | None = None) -> dict:
    """Возвращает разобранный ответ модели: {title, intro, items[]}."""
    client = client or anthropic.Anthropic()
    params = {
        "model": MODEL,
        "max_tokens": MAX_TOKENS,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": _build_user_prompt(articles)}],
    }

    try:
        message = client.messages.create(
            **params,
            output_config={"format": {"type": "json_schema", "schema": DIGEST_SCHEMA}},
        )
    except (anthropic.BadRequestError, TypeError) as exc:
        # Структурированный вывод недоступен (старая версия SDK или модель) —
        # просим JSON текстом.
        log.warning("Structured output недоступен (%s), запрашиваю JSON текстом", exc)
        params["system"] = (
            SYSTEM_PROMPT
            + "\nОтветь ТОЛЬКО валидным JSON вида "
            '{"title": "...", "intro": "...", '
            '"items": [{"id": 1, "headline": "...", "summary": "..."}]} '
            "без пояснений и markdown."
        )
        message = client.messages.create(**params)

    if message.stop_reason == "refusal":
        raise SummarizerError("Модель отказалась обрабатывать запрос")
    if message.stop_reason == "max_tokens":
        log.warning("Ответ модели обрезан по max_tokens — возможен неполный дайджест")

    log.info(
        "Anthropic: модель=%s, входные токены=%s, выходные=%s",
        message.model,
        message.usage.input_tokens,
        message.usage.output_tokens,
    )

    data = _extract_json(_first_text(message))
    if not isinstance(data.get("items"), list) or not data["items"]:
        raise SummarizerError("Модель не вернула ни одной новости")
    return data


# --------------------------------------------------------------------------- #
# Рендеринг поста
# --------------------------------------------------------------------------- #


def render(data: dict, articles: Sequence[Article]) -> str:
    """Собирает HTML-пост для Telegram: тексты от модели + наши ссылки."""
    by_id = {index: article for index, article in enumerate(articles, start=1)}
    summaries: dict[int, dict] = {}
    for item in data["items"]:
        try:
            item_id = int(item["id"])
        except (KeyError, TypeError, ValueError):
            continue
        if item_id in by_id:
            summaries[item_id] = item

    if not summaries:
        raise SummarizerError("Ни один пересказ не сопоставился с новостью")

    title = escape(str(data.get("title") or "Новостной дайджест").strip())
    intro = escape(str(data.get("intro") or "").strip())

    header = f"<b>{title}</b>"
    if intro:
        header += f"\n{intro}"
    sections = [header]

    number = 0
    for topic in TOPIC_ORDER:
        topic_ids = [i for i in sorted(summaries) if by_id[i].topic == topic]
        if not topic_ids:
            continue
        blocks = []
        for item_id in topic_ids:
            article = by_id[item_id]
            item = summaries[item_id]
            number += 1
            headline = escape(str(item.get("headline") or article.title).strip())
            summary = escape(str(item.get("summary") or "").strip())
            block = f"{number}. <b>{headline}</b>"
            if summary:
                block += f"\n{summary}"
            block += f'\n<a href="{escape(article.url, quote=True)}">{escape(article.source)}</a>'
            blocks.append(block)
        sections.append(f"<b>{escape(TOPIC_TITLES[topic])}</b>\n" + "\n\n".join(blocks))

    skipped = len(articles) - len(summaries)
    if skipped:
        log.warning("Модель пропустила %d новостей — они не попали в пост", skipped)

    return "\n\n".join(sections).strip()


def build_digest(
    articles: Sequence[Article], client: anthropic.Anthropic | None = None
) -> tuple[str, list[Article]]:
    """Главная точка входа: новости -> (готовый HTML-пост, вошедшие новости)."""
    if not articles:
        raise SummarizerError("Нет новостей для дайджеста")

    data = request_digest(articles, client=client)
    text = render(data, articles)

    used_ids = {int(item["id"]) for item in data["items"] if str(item.get("id", "")).isdigit()}
    used = [a for i, a in enumerate(articles, start=1) if i in used_ids]
    return text, used or list(articles)
