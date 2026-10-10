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
import threading
import time
import types
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html import escape
from typing import Sequence
from urllib.parse import urlparse

import anthropic

from fetcher import TOPIC_ORDER, TOPIC_TITLES, Article

log = logging.getLogger(__name__)

MODEL = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")
MAX_TOKENS = int(os.getenv("ANTHROPIC_MAX_TOKENS", "4000"))
# Пустое значение — официальный api.anthropic.com. Непустое — совместимый
# шлюз-посредник; у таких обычно свои имена моделей, см. ANTHROPIC_MODEL.
BASE_URL = os.getenv("ANTHROPIC_BASE_URL", "").strip()
# Официальный адрес подставляется явно: пустая ANTHROPIC_BASE_URL в окружении
# (так её оставляет .env, скопированный из .env.example) сошла бы для SDK
# за адрес, и запросы уходили бы в никуда.
OFFICIAL_BASE_URL = "https://api.anthropic.com"
# Запасной провайдер включается, когда задан его ключ. Нужен другой шлюз или
# официальный API: запасной на том же шлюзе упал бы вместе с основным.
FALLBACK_API_KEY = os.getenv("ANTHROPIC_FALLBACK_API_KEY", "").strip()
FALLBACK_BASE_URL = os.getenv("ANTHROPIC_FALLBACK_BASE_URL", "").strip()
# Пусто — та же модель, что у основного провайдера.
FALLBACK_MODEL = os.getenv("ANTHROPIC_FALLBACK_MODEL", "").strip()
# Сколько секунд ждать ответа модели, прежде чем считать провайдера недоступным.
# Без явного значения SDK ждёт десять минут, и запасной не дождался бы очереди.
LLM_TIMEOUT = float(os.getenv("LLM_TIMEOUT", "120"))
# buttons — ссылки уходят в inline-кнопки под постом, текст остаётся чистым;
# inline  — ссылка строкой под каждой новостью.
LINKS_MODE = os.getenv("LINKS_MODE", "buttons").strip().lower()
TIMEZONE = os.getenv("TIMEZONE", "Europe/Moscow")
# Длина совпадающей цепочки слов, после которой пересказ считается копией.
COPY_NGRAM = int(os.getenv("COPY_NGRAM", "7"))
# Подпись к фото в Telegram ограничена 1024 символами — берём с запасом.
CAPTION_LIMIT = 1000

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
9. Не пиши «сегодня», «вчера», «на прошлой неделе» и подобное, если это не
   следует из указанной даты публикации новости. Когда сомневаешься — обходись
   без указания времени.
"""

REPHRASE_SYSTEM_PROMPT = """\
Ты — редактор новостного дайджеста. Присланные описания слишком близки к тексту
источника: в них есть дословно совпадающие фрагменты.

Перепиши каждое описание заново своими словами: измени порядок изложения,
подбери другие формулировки, но сохрани смысл и не добавляй фактов, которых
нет в исходном тексте. Объём — 1-2 предложения. Верни те же id.
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


@dataclass(frozen=True)
class Provider:
    """Куда отправлять запросы к модели: адрес, ключ и имя модели у него."""

    label: str  # «основной» или «запасной» — для лога и сообщений админам
    base_url: str  # пусто — официальный api.anthropic.com
    api_key: str  # пусто — SDK возьмёт ANTHROPIC_API_KEY из окружения
    model: str

    @property
    def host(self) -> str:
        return urlparse(self.base_url or OFFICIAL_BASE_URL).netloc


def _clean_base_url(url: str) -> str:
    """Адрес шлюза без слэша и /v1 на конце: SDK сам допишет /v1/messages."""
    url = url.strip().rstrip("/")
    return url[: -len("/v1")] if url.endswith("/v1") else url


def providers() -> list[Provider]:
    """Основной провайдер и, если задан его ключ, запасной — в порядке опроса."""
    chain = [Provider("основной", _clean_base_url(BASE_URL), "", MODEL)]
    if FALLBACK_API_KEY:
        chain.append(
            Provider(
                "запасной",
                _clean_base_url(FALLBACK_BASE_URL),
                FALLBACK_API_KEY,
                FALLBACK_MODEL or MODEL,
            )
        )
    return chain


def _build_client(provider: Provider) -> anthropic.Anthropic:
    return anthropic.Anthropic(
        api_key=provider.api_key or None,
        base_url=provider.base_url or OFFICIAL_BASE_URL,
        timeout=LLM_TIMEOUT,
    )


# Ответы, после которых стоит идти к другому провайдеру: ключ отозван, кончились
# деньги, модель закрыта для ключа, адрес сменился, таймаут, лимит запросов.
_FAILOVER_STATUSES = {401, 402, 403, 404, 408, 429}


def _should_fail_over(exc: Exception) -> bool:
    """Поможет ли другой провайдер после этой ошибки.

    400 не повторяем: плохой запрос не станет лучше на другом шлюзе, а
    непринятый структурированный вывод и так обрабатывает _call.
    """
    if isinstance(exc, SummarizerError):  # пустой ответ
        return True
    if isinstance(exc, anthropic.APIConnectionError):  # обрыв, DNS, таймаут
        return True
    status = getattr(exc, "status_code", None)
    return status is not None and (status in _FAILOVER_STATUSES or status >= 500)


def _scrub(text: str) -> str:
    """Убирает ключи из текста: шлюзы порой повторяют ключ в сообщении об ошибке."""
    for secret in (os.getenv("ANTHROPIC_API_KEY", ""), FALLBACK_API_KEY):
        if len(secret) >= 8:
            text = text.replace(secret, "***")
    return text


def _describe(exc: Exception) -> str:
    """Причина сбоя одной строкой — по ней админ поймёт, что чинить."""
    if isinstance(exc, anthropic.APITimeoutError):
        return "таймаут"
    if isinstance(exc, anthropic.APIConnectionError):
        return "нет соединения"
    status = getattr(exc, "status_code", None)
    if status is not None:
        detail = re.sub(r"^Error code: \d+ - ", "", str(getattr(exc, "message", "") or ""))
        detail = _scrub(detail.strip())[:150]
        return f"HTTP {status}" + (f": {detail}" if detail else "")
    return _scrub(str(exc)) or exc.__class__.__name__


# Сообщения о переключении на запасного провайдера. Копятся в рабочем потоке
# прогона, забирает их main, чтобы сообщить админам: иначе основной мог бы
# неделями лежать незамеченным.
_failover_notes: list[str] = []
_notes_lock = threading.Lock()


def _remember_failover(failed: list[tuple[Provider, str]], used: Provider) -> None:
    with _notes_lock:
        for provider, reason in failed:
            note = (
                f"Провайдер модели «{provider.label}» ({provider.host}) не ответил: "
                f"{reason}. Запрос выполнил «{used.label}» ({used.host})."
            )
            if note not in _failover_notes:
                _failover_notes.append(note)


def take_failover_notes() -> list[str]:
    """Забирает накопленные сообщения о переключении провайдера."""
    with _notes_lock:
        notes = list(_failover_notes)
        _failover_notes.clear()
    return notes


class FailoverClient:
    """Клиент с запасными провайдерами и тем же `.messages.create()`, что у SDK.

    Если провайдер не ответил (обрыв, таймаут, 5xx, лимит, кончились деньги,
    отозван ключ) или прислал пустой ответ, запрос уходит следующему по списку
    со своим именем модели. Ошибка последнего пробрасывается как есть.
    """

    def __init__(self, chain: Sequence[Provider], clients: Sequence | None = None):
        self._chain = list(chain)
        self._clients = (
            list(clients) if clients is not None else [_build_client(p) for p in self._chain]
        )
        # Точки входа обращаются к клиенту как к SDK: client.messages.create(...).
        self.messages = types.SimpleNamespace(create=self._create)

    def _create(self, **params):
        failed: list[tuple[Provider, str]] = []
        last = len(self._chain) - 1
        for index, (provider, client) in enumerate(zip(self._chain, self._clients)):
            try:
                message = client.messages.create(**{**params, "model": provider.model})
                if message.stop_reason != "refusal" and not _first_text(message).strip():
                    raise SummarizerError("пустой ответ модели")
            except (anthropic.APIError, SummarizerError) as exc:
                if index == last or not _should_fail_over(exc):
                    raise
                reason = _describe(exc)
                log.warning(
                    "Провайдер «%s» (%s) не ответил: %s — пробую «%s»",
                    provider.label,
                    provider.host,
                    reason,
                    self._chain[index + 1].label,
                )
                failed.append((provider, reason))
                continue
            if failed:
                log.warning("Запрос выполнил «%s» (%s)", provider.label, provider.host)
                _remember_failover(failed, provider)
            return message
        raise SummarizerError("Не настроен ни один провайдер модели")


LLMClient = anthropic.Anthropic | FailoverClient


def make_client() -> LLMClient:
    """Клиент модели: основной провайдер, а если настроен запасной — цепочка."""
    chain = providers()
    if len(chain) == 1:
        return _build_client(chain[0])
    return FailoverClient(chain)


def _call(client: LLMClient, params: dict, schema: dict):
    """Один запрос: со structured outputs, а если они недоступны — JSON текстом.

    Возвращает ответ и признак, сработали ли structured outputs.
    """
    try:
        message = client.messages.create(
            **params, output_config={"format": {"type": "json_schema", "schema": schema}}
        )
        return message, True
    except (anthropic.BadRequestError, TypeError) as exc:
        # Структурированный вывод недоступен (старая версия SDK или модель) —
        # просим JSON текстом.
        log.warning("Structured output недоступен (%s), запрашиваю JSON текстом", exc)
        params = {
            **params,
            "system": params["system"]
            + "\nОтветь ТОЛЬКО валидным JSON по схеме:\n"
            + json.dumps(schema, ensure_ascii=False)
            + "\nБез пояснений и markdown.",
        }
        return client.messages.create(**params), False


def _ask(
    client: LLMClient,
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
    message, _ = _call(client, params, schema)

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


CHECK_SCHEMA = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}},
    "required": ["ok"],
    "additionalProperties": False,
}


def _model_family(name: str) -> str:
    """claude-haiku-4.5 и claude-haiku-4-5-20251001 — одна и та же модель."""
    return re.sub(r"-\d{8}$", "", name.strip().lower().replace(".", "-"))


@dataclass
class ProviderCheck:
    """Итог пробного запроса к одному провайдеру."""

    provider: Provider
    ok: bool = False
    reported_model: str = ""
    structured: bool = False
    seconds: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    error: str = ""

    @property
    def model_differs(self) -> bool:
        """Шлюз назвал не ту модель, что просили (может быть и честным псевдонимом)."""
        return bool(self.reported_model) and _model_family(
            self.reported_model
        ) != _model_family(self.provider.model)


def check_providers() -> list[ProviderCheck]:
    """Пробный запрос каждому настроенному провайдеру — по очереди, без переключений.

    Идёт тем же путём, что и выпуск (_call), поэтому показывает и то, принял ли
    шлюз структурированный вывод.
    """
    results = []
    for provider in providers():
        check = ProviderCheck(provider)
        started = time.monotonic()
        params = {
            "model": provider.model,
            "max_tokens": 50,
            "system": "Отвечай только JSON.",
            "messages": [{"role": "user", "content": 'Верни {"ok": true}'}],
        }
        try:
            message, check.structured = _call(_build_client(provider), params, CHECK_SCHEMA)
            check.reported_model = str(getattr(message, "model", "") or "")
            usage = getattr(message, "usage", None)
            check.input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
            check.output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
            check.ok = _extract_json(_first_text(message)).get("ok") is True
            if not check.ok:
                check.error = "модель ответила не по образцу"
        except (anthropic.APIError, SummarizerError, ValueError, TypeError) as exc:
            check.error = _describe(exc)
        check.seconds = time.monotonic() - started
        results.append(check)
    return results


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
    client: LLMClient | None = None,
) -> list[Article]:
    """Просит модель выбрать лучшие новости из списка кандидатов.

    При любой ошибке возвращает первые max_items кандидатов: список уже
    отсортирован и сбалансирован по темам, так что выпуск всё равно выйдет.
    """
    candidates = list(candidates)
    if len(candidates) <= min_items:
        return candidates[:max_items]

    client = client or make_client()
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


def _local_time(moment) -> str:
    """Время публикации в часовом поясе канала."""
    if moment is None:
        return "время неизвестно"
    try:
        from zoneinfo import ZoneInfo

        moment = moment.astimezone(ZoneInfo(TIMEZONE))
    except Exception:  # noqa: BLE001 - без зоны просто покажем как есть
        pass
    return moment.strftime("%d.%m.%Y %H:%M")


def _digest_prompt(articles: Sequence[Article]) -> str:
    now = _local_time(datetime.now(timezone.utc))
    lines = [
        f"Сегодня {now} (часовой пояс {TIMEZONE}).",
        f"Сегодняшняя подборка: {len(articles)} новостей. "
        "Составь из них один дайджест.\n",
    ]
    for index, article in enumerate(articles, start=1):
        lines.append(f"[id: {index}]")
        lines.append(f"Тема: {TOPIC_TITLES[article.topic]}")
        lines.append(f"Источник: {article.source}")
        lines.append(f"Опубликовано: {_local_time(article.published)}")
        lines.append(f"Заголовок: {article.title}")
        lines.append(f"Текст: {article.body or '(текст недоступен)'}")
        lines.append("")
    return "\n".join(lines)


def request_digest(
    articles: Sequence[Article], client: LLMClient | None = None
) -> dict:
    """Возвращает разобранный ответ модели: {title, intro, image_prompt, items[]}."""
    client = client or make_client()
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
# Шаг 3: проверка, что пересказ не скопирован
# --------------------------------------------------------------------------- #

REPHRASE_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "summary": {"type": "string"},
                },
                "required": ["id", "summary"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["items"],
    "additionalProperties": False,
}

_WORD_RE = re.compile(r"[a-zа-яё0-9]+", re.IGNORECASE)


def _words(text: str) -> list[str]:
    return _WORD_RE.findall((text or "").lower().replace("ё", "е"))


def borrowed_fragment(summary: str, source: str, n: int = COPY_NGRAM) -> str:
    """Возвращает дословно совпадающий фрагмент из источника, если он есть.

    Требование «пересказывать своими словами» до сих пор жило только в
    промпте. Здесь оно проверяется фактически: ищем цепочку из n слов подряд,
    которая встречается и в пересказе, и в тексте источника.
    """
    if n <= 0:
        return ""
    summary_words, source_words = _words(summary), _words(source)
    if len(summary_words) < n or len(source_words) < n:
        return ""

    source_ngrams = {
        tuple(source_words[i : i + n]) for i in range(len(source_words) - n + 1)
    }
    for i in range(len(summary_words) - n + 1):
        chunk = tuple(summary_words[i : i + n])
        if chunk in source_ngrams:
            return " ".join(chunk)
    return ""


def find_borrowed(
    data: dict, articles: Sequence[Article], n: int = COPY_NGRAM
) -> dict[int, str]:
    """id пересказов, слишком близких к тексту источника."""
    by_id = {index: article for index, article in enumerate(articles, start=1)}
    borrowed: dict[int, str] = {}
    for item in data.get("items", []):
        try:
            item_id = int(item["id"])
        except (KeyError, TypeError, ValueError):
            continue
        article = by_id.get(item_id)
        if article is None:
            continue
        fragment = borrowed_fragment(item.get("summary", ""), article.body, n)
        if fragment:
            borrowed[item_id] = fragment
    return borrowed


def enforce_own_words(
    data: dict,
    articles: Sequence[Article],
    client: LLMClient | None = None,
) -> dict:
    """Просит переписать скопированные описания, упрямые — убирает.

    Лучше выпустить новость с одним заголовком, чем с абзацем, скопированным
    у издания.
    """
    borrowed = find_borrowed(data, articles)
    if not borrowed:
        return data

    log.warning(
        "Слишком близко к источнику (%d из %d): %s",
        len(borrowed),
        len(data.get("items", [])),
        "; ".join(f"#{i}: «{frag}»" for i, frag in borrowed.items()),
    )

    by_id = {index: article for index, article in enumerate(articles, start=1)}
    items_by_id = {}
    for item in data["items"]:
        try:
            items_by_id[int(item["id"])] = item
        except (KeyError, TypeError, ValueError):
            continue

    prompt_lines = []
    for item_id in borrowed:
        article = by_id[item_id]
        prompt_lines.append(f"[id: {item_id}]")
        prompt_lines.append(f"Заголовок: {article.title}")
        prompt_lines.append(f"Исходный текст: {article.body}")
        prompt_lines.append(f"Твоё описание: {items_by_id[item_id].get('summary', '')}")
        prompt_lines.append("")

    try:
        rewritten = _ask(
            client or make_client(),
            REPHRASE_SYSTEM_PROMPT,
            "\n".join(prompt_lines),
            REPHRASE_SCHEMA,
            max_tokens=1500,
        )
        for item in rewritten.get("items", []):
            item_id = int(item["id"])
            if item_id in items_by_id and item.get("summary"):
                items_by_id[item_id]["summary"] = item["summary"]
    except (SummarizerError, anthropic.APIError, ValueError, TypeError, KeyError) as exc:
        log.warning("Переписать описания не удалось (%s)", exc)

    # Что не исправилось — оставляем без описания, с одним заголовком.
    still_borrowed = find_borrowed(data, articles)
    for item_id, fragment in still_borrowed.items():
        log.warning("Убираю описание #%d: всё ещё копия («%s»)", item_id, fragment)
        items_by_id[item_id]["summary"] = ""
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
class DigestItem:
    """Одна новость выпуска — для публикации отдельным постом."""

    article: Article
    headline: str
    summary: str
    image: bytes | None = None


@dataclass
class Digest:
    """Готовый выпуск: текст поста, кнопки, вошедшие новости и данные обложки."""

    text: str
    articles: list[Article]
    title: str
    image_prompt: str = ""
    buttons: list[tuple[str, str]] = field(default_factory=list)
    # Те же новости по одной, в порядке поста: по темам, внутри темы — по id.
    items: list[DigestItem] = field(default_factory=list)


def digest_items(data: dict, articles: Sequence[Article]) -> list[DigestItem]:
    """Новости выпуска по одной — в том же порядке, что и в общем посте."""
    by_id = {index: article for index, article in enumerate(articles, start=1)}
    found: dict[int, dict] = {}
    for item in data.get("items", []):
        try:
            item_id = int(item["id"])
        except (KeyError, TypeError, ValueError):
            continue
        if item_id in by_id:
            found[item_id] = item

    ordered = sorted(found, key=lambda i: (TOPIC_ORDER.index(by_id[i].topic), i))
    return [
        DigestItem(
            article=by_id[i],
            headline=str(found[i].get("headline") or by_id[i].title).strip(),
            summary=str(found[i].get("summary") or "").strip(),
        )
        for i in ordered
    ]


def render_item(
    item: DigestItem, links_mode: str | None = None
) -> tuple[str, list[tuple[str, str]]]:
    """Отдельный пост о новости: заголовок, пересказ, тема и источник.

    Пост уходит подписью к фото, а у неё лимит 1024 символа, поэтому
    слишком длинный пересказ обрезается по слову.
    """
    links_mode = (links_mode or LINKS_MODE).lower()
    article = item.article
    headline = escape(item.headline)
    topic = escape(TOPIC_TITLES.get(article.topic, ""))
    if links_mode == "buttons":
        source = f"<i>{escape(article.source)}</i>"
        buttons = [("Читать в источнике", article.url)]
    else:
        source = f'<a href="{escape(article.url, quote=True)}">{escape(article.source)}</a>'
        buttons = []
    footer = f"{topic} · {source}" if topic else source

    budget = CAPTION_LIMIT - len(headline) - len(footer) - 20
    summary = item.summary
    if len(summary) > budget:
        summary = summary[: max(budget, 0)].rsplit(" ", 1)[0].rstrip(",.;:—- ") + "…"
    parts = [f"<b>{headline}</b>"]
    if summary.strip("…"):
        parts.append(escape(summary))
    parts.append(footer)
    return "\n\n".join(parts), buttons


def build_digest(
    articles: Sequence[Article], client: LLMClient | None = None
) -> Digest:
    """Главная точка входа: новости -> готовый выпуск."""
    if not articles:
        raise SummarizerError("Нет новостей для дайджеста")

    data = request_digest(articles, client=client)
    data = enforce_own_words(data, articles, client=client)
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
        items=digest_items(data, articles),
    )
