"""Работа с Anthropic API: отбор лучших новостей и сборка поста-дайджеста.

Модель вызывается дважды за прогон:
1. select_best — выбирает 5-8 новостей из ~20 кандидатов и склеивает сюжеты
   об одном событии (эвристики в fetcher.py этого не умеют: «Wildberries»
   и «WB» для них разные слова);
2. build_digest — пересказывает отобранное своими словами.

Между вызовами для победителей дочитывается полный текст статей, поэтому
пересказ опирается не на однострочные описания из RSS.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from html import escape
from typing import Sequence

import anthropic

from fetcher import TOPIC_ORDER, TOPIC_TITLES, Article

log = logging.getLogger(__name__)

MODEL = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")
MAX_TOKENS = int(os.getenv("ANTHROPIC_MAX_TOKENS", "4000"))
# buttons — ссылки уходят в inline-кнопки под постом, текст остаётся чистым;
# inline  — ссылка строкой под каждой новостью.
LINKS_MODE = os.getenv("LINKS_MODE", "buttons").strip().lower()

# --------------------------------------------------------------------------- #
# Промпты и схемы
# --------------------------------------------------------------------------- #

SELECT_SYSTEM_PROMPT = """\
Ты — выпускающий редактор новостного Telegram-канала о рынке такси, автомобилях
и происшествиях в Пензе. Тебе присылают список кандидатов; твоя задача — отобрать
лучшие для сегодняшнего выпуска.

Правила отбора:
1. Если несколько заметок об ОДНОМ И ТОМ ЖЕ событии (пусть даже названия
   написаны по-разному, например «Wildberries» и «WB»), оставь только одну —
   самую информативную.
2. Отбрось заметки, не относящиеся ни к одной из тем канала: рынок такси,
   автомобили и авторынок, происшествия в Пензе и области.
3. Приоритет: новости Пензы и области по темам «такси» и «происшествия»,
   затем общероссийские новости про такси и авторынок.
4. Отдавай предпочтение конкретным событиям, а не обзорам, советам и рекламе.
5. Старайся, чтобы в выпуске были представлены разные темы, а не одна.
6. Верни только id выбранных новостей, в порядке значимости.
"""

SELECT_SCHEMA = {
    "type": "object",
    "properties": {
        "selected_ids": {
            "type": "array",
            "description": "id выбранных новостей в порядке значимости",
            "items": {"type": "integer"},
        },
        "rejected_duplicates": {
            "type": "array",
            "description": "id заметок, отброшенных как дубли уже выбранного сюжета",
            "items": {"type": "integer"},
        },
    },
    "required": ["selected_ids"],
    "additionalProperties": False,
}

DIGEST_SYSTEM_PROMPT = """\
Ты — редактор ежедневного новостного дайджеста для Telegram-канала о рынке такси,
автомобилях и городских происшествиях в Пензе.

Правила:
1. Каждую новость пересказывай СВОИМИ СЛОВАМИ. Не копируй фразы и обороты из
   присланного текста, перестраивай формулировки.
2. Краткое описание — 1-2 предложения, по-русски, деловой нейтральный тон,
   без «воды», оценок и обращений к читателю.
3. Опирайся только на присланные заголовок и текст. Ничего не додумывай:
   не добавляй цифры, даты, имена и последствия, которых нет в исходных данных.
   Если текста нет — перескажи только то, что следует из заголовка.
4. Заголовок новости в дайджесте — короткий (до 80 символов), информативный,
   не дословный повтор исходного.
5. Не используй markdown, HTML и эмодзи — только обычный текст.
6. Общий заголовок поста — короткий, отражает главные темы выпуска.
   Вступление — одно предложение о том, что в выпуске.
7. Верни ровно по одному элементу на каждую присланную новость, сохранив её id.
8. В поле image_prompt опиши обложку выпуска для генератора изображений:
   краткая фраза на русском, предметная сцена без текста, надписей и логотипов,
   например «городская улица с автомобилями на рассвете, вид сверху».
"""

DIGEST_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "Заголовок поста"},
        "intro": {"type": "string", "description": "Одно вводное предложение"},
        "image_prompt": {
            "type": "string",
            "description": "Описание обложки выпуска для генератора изображений",
        },
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
# Общение с моделью
# --------------------------------------------------------------------------- #


def _first_text(message) -> str:
    for block in message.content:
        if block.type == "text":
            return block.text
    return ""


def _extract_json(text: str) -> dict:
    """Достаёт JSON-объект из ответа модели (на случай обрамляющего текста)."""
    text = (text or "").strip()
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


def _ask(
    client: anthropic.Anthropic,
    system: str,
    user: str,
    schema: dict,
    max_tokens: int,
) -> dict:
    """Запрос к модели со structured outputs и текстовым фолбэком."""
    params = {
        "model": MODEL,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }
    try:
        message = client.messages.create(
            **params, output_config={"format": {"type": "json_schema", "schema": schema}}
        )
    except (anthropic.BadRequestError, TypeError) as exc:
        # Структурированный вывод недоступен (старая версия SDK или модель) —
        # просим JSON текстом.
        log.warning("Structured output недоступен (%s), запрашиваю JSON текстом", exc)
        params["system"] = (
            system
            + "\nОтветь ТОЛЬКО валидным JSON по схеме:\n"
            + json.dumps(schema, ensure_ascii=False)
            + "\nБез пояснений и markdown."
        )
        message = client.messages.create(**params)

    if message.stop_reason == "refusal":
        raise SummarizerError("Модель отказалась обрабатывать запрос")
    if message.stop_reason == "max_tokens":
        log.warning("Ответ модели обрезан по max_tokens — возможен неполный результат")

    log.info(
        "Anthropic: модель=%s, входные токены=%s, выходные=%s",
        message.model,
        message.usage.input_tokens,
        message.usage.output_tokens,
    )
    return _extract_json(_first_text(message))


# --------------------------------------------------------------------------- #
# Шаг 1: отбор
# --------------------------------------------------------------------------- #


def _selection_prompt(candidates: Sequence[Article], min_items: int, max_items: int) -> str:
    lines = [
        f"Кандидатов: {len(candidates)}. "
        f"Выбери от {min_items} до {max_items} новостей для сегодняшнего выпуска.\n"
    ]
    for index, article in enumerate(candidates, start=1):
        region = "Пензенская область" if article.is_penza else "Россия"
        lines.append(
            f"[id: {index}] тема={TOPIC_TITLES[article.topic]}; регион={region}; "
            f"источник={article.source}"
        )
        lines.append(f"  Заголовок: {article.title}")
        if article.summary:
            lines.append(f"  Описание: {article.summary[:200]}")
    return "\n".join(lines)


def select_best(
    candidates: Sequence[Article],
    *,
    min_items: int = 5,
    max_items: int = 8,
    client: anthropic.Anthropic | None = None,
) -> list[Article]:
    """Просит модель выбрать лучшие новости из списка кандидатов.

    При любой ошибке возвращает первые max_items кандидатов: список уже
    отсортирован и сбалансирован по темам, так что выпуск всё равно выйдет.
    """
    candidates = list(candidates)
    if len(candidates) <= min_items:
        return candidates[:max_items]

    client = client or anthropic.Anthropic()
    try:
        data = _ask(
            client,
            SELECT_SYSTEM_PROMPT,
            _selection_prompt(candidates, min_items, max_items),
            SELECT_SCHEMA,
            max_tokens=1000,
        )
        ids = [int(i) for i in data.get("selected_ids", []) if isinstance(i, (int, str))]
    except (SummarizerError, anthropic.APIError, ValueError, TypeError) as exc:
        log.warning("Отбор моделью не удался (%s) — беру лучших по рейтингу", exc)
        return candidates[:max_items]

    chosen = [candidates[i - 1] for i in ids if 1 <= i <= len(candidates)][:max_items]
    if len(chosen) < min_items:
        # Добираем по рейтингу, не ломая выбор модели.
        for article in candidates:
            if len(chosen) >= min_items:
                break
            if article not in chosen:
                chosen.append(article)

    dropped = data.get("rejected_duplicates") or []
    log.info(
        "Модель отобрала %d из %d новостей (дублей отброшено: %d)",
        len(chosen),
        len(candidates),
        len(dropped),
    )
    chosen.sort(key=lambda a: (TOPIC_ORDER.index(a.topic), -a.score))
    return chosen


# --------------------------------------------------------------------------- #
# Шаг 2: пересказ
# --------------------------------------------------------------------------- #


def _digest_prompt(articles: Sequence[Article]) -> str:
    lines = [
        f"Сегодняшняя подборка: {len(articles)} новостей. "
        "Составь из них один дайджест.\n"
    ]
    for index, article in enumerate(articles, start=1):
        lines.append(f"[id: {index}]")
        lines.append(f"Тема: {TOPIC_TITLES[article.topic]}")
        lines.append(f"Источник: {article.source}")
        lines.append(f"Заголовок: {article.title}")
        lines.append(f"Текст: {article.body or '(текст недоступен)'}")
        lines.append("")
    return "\n".join(lines)


def request_digest(
    articles: Sequence[Article], client: anthropic.Anthropic | None = None
) -> dict:
    """Возвращает разобранный ответ модели: {title, intro, image_prompt, items[]}."""
    client = client or anthropic.Anthropic()
    data = _ask(
        client,
        DIGEST_SYSTEM_PROMPT,
        _digest_prompt(articles),
        DIGEST_SCHEMA,
        max_tokens=MAX_TOKENS,
    )
    if not isinstance(data.get("items"), list) or not data["items"]:
        raise SummarizerError("Модель не вернула ни одной новости")
    return data


# --------------------------------------------------------------------------- #
# Рендеринг поста
# --------------------------------------------------------------------------- #


def render(
    data: dict, articles: Sequence[Article], links_mode: str | None = None
) -> tuple[str, list[tuple[str, str]]]:
    """Собирает HTML-пост для Telegram: тексты от модели + наши ссылки.

    Возвращает (текст поста, список кнопок «подпись -> ссылка»). В режиме
    ``inline`` список кнопок пустой, а ссылки стоят строками в тексте.
    """
    links_mode = (links_mode or LINKS_MODE).lower()
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

    buttons: list[tuple[str, str]] = []
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
            if links_mode == "buttons":
                # Ссылка уедет в кнопку, в тексте оставляем только источник.
                block += f"\n<i>{escape(article.source)}</i>"
                buttons.append((f"{number}. {article.source}"[:60], article.url))
            else:
                block += (
                    f'\n<a href="{escape(article.url, quote=True)}">'
                    f"{escape(article.source)}</a>"
                )
            blocks.append(block)
        sections.append(f"<b>{escape(TOPIC_TITLES[topic])}</b>\n" + "\n\n".join(blocks))

    skipped = len(articles) - len(summaries)
    if skipped:
        log.warning("Модель пропустила %d новостей — они не попали в пост", skipped)

    return "\n\n".join(sections).strip(), buttons


@dataclass
class Digest:
    """Готовый выпуск: текст поста, кнопки, вошедшие новости и данные обложки."""

    text: str
    articles: list[Article]
    title: str
    image_prompt: str = ""
    buttons: list[tuple[str, str]] = field(default_factory=list)


def build_digest(
    articles: Sequence[Article], client: anthropic.Anthropic | None = None
) -> Digest:
    """Главная точка входа: новости -> готовый выпуск."""
    if not articles:
        raise SummarizerError("Нет новостей для дайджеста")

    data = request_digest(articles, client=client)
    text, buttons = render(data, articles)

    used_ids = {
        int(item["id"])
        for item in data["items"]
        if str(item.get("id", "")).lstrip("-").isdigit()
    }
    used = [a for i, a in enumerate(articles, start=1) if i in used_ids]
    return Digest(
        text=text,
        articles=used or list(articles),
        title=str(data.get("title") or "Новостной дайджест").strip(),
        image_prompt=str(data.get("image_prompt") or "").strip(),
        buttons=buttons,
    )
