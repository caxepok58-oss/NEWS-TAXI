"""Картинка к посту дайджеста.

Три режима, задаются переменной IMAGE_MODE:

* ``cover``  — обложка выпуска рисуется локально через Pillow (по умолчанию).
               Ключей не требует, работает всегда.
* ``ai``     — иллюстрация генерируется нейросетью Kandinsky (FusionBrain API)
               по описанию, которое составила модель. Нужны ключи
               FUSIONBRAIN_API_KEY и FUSIONBRAIN_SECRET_KEY (бесплатные).
* ``source`` — берётся фотография из вложения RSS у главной новости выпуска.
* ``off``    — пост отправляется без картинки.

Любой сбой в режимах ``ai`` и ``source`` откатывается на локальную обложку,
чтобы выпуск не остался без изображения.

Anthropic API изображения не генерирует — Claude умеет только писать описание
сцены, поэтому за саму картинку отвечает либо Pillow, либо внешний сервис.
"""

from __future__ import annotations

import io
import logging
import os
import random
import time
from datetime import datetime
from typing import Sequence

import requests

from fetcher import TOPIC_TITLES, Article

log = logging.getLogger(__name__)

MODE = os.getenv("IMAGE_MODE", "cover").strip().lower()
HTTP_TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "15"))
USER_AGENT = os.getenv(
    "USER_AGENT",
    "Mozilla/5.0 (compatible; NewsDigestBot/1.0; +https://github.com/)",
)

WIDTH, HEIGHT = 1024, 576  # 16:9 — Telegram показывает такую картинку крупно
MAX_IMAGE_BYTES = 9 * 1024 * 1024  # лимит Telegram на фото по URL — 10 МБ

# Палитра обложек: (цвет верха, цвет низа, цвет акцента).
_PALETTES = (
    ((18, 32, 60), (10, 16, 32), (255, 196, 61)),  # тёмно-синий + жёлтый
    ((28, 22, 54), (12, 10, 26), (120, 200, 255)),  # фиолетовый + голубой
    ((14, 42, 42), (8, 20, 22), (126, 231, 168)),  # изумрудный + мятный
    ((48, 24, 24), (20, 10, 12), (255, 140, 105)),  # тёмно-красный + коралл
)

_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "C:\\Windows\\Fonts\\arialbd.ttf",
)
_FONT_CANDIDATES_REGULAR = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "C:\\Windows\\Fonts\\arial.ttf",
)

# Короткие подписи тем для нижней строки обложки — полные названия туда не влезают.
_SHORT_TOPICS = {"taxi": "Такси", "auto": "Авто", "incident": "Происшествия"}

_MONTHS = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)


def _font(size: int, bold: bool = True):
    from PIL import ImageFont

    for path in _FONT_CANDIDATES if bold else _FONT_CANDIDATES_REGULAR:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except OSError:  # pragma: no cover - битый файл шрифта
                continue
    log.warning("Не найден TrueType-шрифт, обложка будет со встроенным шрифтом")
    return ImageFont.load_default()


def _wrap(draw, text: str, font, max_width: int) -> list[str]:
    """Перенос текста по словам под заданную ширину."""
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if draw.textlength(candidate, font=font) <= max_width or not current:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def render_cover(
    title: str,
    articles: Sequence[Article] = (),
    when: datetime | None = None,
) -> bytes | None:
    """Рисует обложку выпуска: дата, заголовок и темы. Возвращает PNG."""
    try:
        from PIL import Image, ImageDraw
    except ImportError:  # pragma: no cover - Pillow не установлен
        log.warning("Pillow не установлен — пост уйдёт без картинки")
        return None

    when = when or datetime.now()
    top, bottom, accent = random.choice(_PALETTES)

    image = Image.new("RGB", (WIDTH, HEIGHT), top)
    draw = ImageDraw.Draw(image)

    # Вертикальный градиент.
    for y in range(HEIGHT):
        ratio = y / HEIGHT
        draw.line(
            [(0, y), (WIDTH, y)],
            fill=tuple(int(top[i] + (bottom[i] - top[i]) * ratio) for i in range(3)),
        )

    margin = 64
    # Акцентная полоса слева.
    draw.rectangle([margin, margin, margin + 6, HEIGHT - margin], fill=accent)
    text_left = margin + 34
    text_width = WIDTH - text_left - margin

    date_font = _font(28, bold=False)
    date_text = f"{when.day} {_MONTHS[when.month - 1]} {when.year}"
    draw.text((text_left, margin - 4), date_text.upper(), font=date_font, fill=accent)

    # Заголовок: подбираем размер так, чтобы уместиться в 4 строки.
    for size in (58, 52, 46, 40, 34):
        title_font = _font(size, bold=True)
        lines = _wrap(draw, title, title_font, text_width)[:4]
        if len(lines) <= 4:
            break
    line_height = int(size * 1.24)

    # Блок заголовка центрируем между датой и строкой тем, иначе при коротком
    # заголовке середина обложки остаётся пустой.
    area_top = margin + 74
    area_bottom = HEIGHT - margin - 58
    y = area_top + max(0, (area_bottom - area_top - len(lines) * line_height) // 2)
    for line in lines:
        draw.text((text_left, y), line, font=title_font, fill=(255, 255, 255))
        y += line_height

    # Темы выпуска — внизу, по числу новостей в каждой.
    counts: dict[str, int] = {}
    for article in articles:
        counts[article.topic] = counts.get(article.topic, 0) + 1
    if counts:
        parts = [
            f"{_SHORT_TOPICS.get(topic) or TOPIC_TITLES[topic].split(' ', 1)[-1]} — {count}"
            for topic, count in counts.items()
            if topic in TOPIC_TITLES
        ]
        line = "   ·   ".join(parts)
        # Строка не должна вылезать за правый край: сначала уменьшаем шрифт,
        # затем отбрасываем хвост.
        for topic_size in (26, 23, 20):
            topic_font = _font(topic_size, bold=False)
            if draw.textlength(line, font=topic_font) <= text_width:
                break
        while parts and draw.textlength(line, font=topic_font) > text_width:
            parts.pop()
            line = "   ·   ".join(parts) + "   ·   …"
        draw.text(
            (text_left, HEIGHT - margin - 34),
            line,
            font=topic_font,
            fill=(210, 214, 226),
        )

    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


# --------------------------------------------------------------------------- #
# Картинка из ленты
# --------------------------------------------------------------------------- #


def download_image(url: str) -> bytes | None:
    """Скачивает картинку новости, проверяя, что это действительно изображение."""
    if not url:
        return None
    try:
        response = requests.get(
            url, timeout=HTTP_TIMEOUT, headers={"User-Agent": USER_AGENT}, stream=True
        )
        response.raise_for_status()
        if not response.headers.get("Content-Type", "").lower().startswith("image/"):
            log.debug("По ссылке %s не изображение", url)
            return None
        data = response.content
    except requests.RequestException as exc:
        log.debug("Не удалось скачать картинку %s: %s", url, exc)
        return None

    if len(data) > MAX_IMAGE_BYTES:
        log.debug("Картинка %s слишком большая (%d байт)", url, len(data))
        return None
    return data


# --------------------------------------------------------------------------- #
# Генерация нейросетью (Kandinsky / FusionBrain)
# --------------------------------------------------------------------------- #

FUSIONBRAIN_URL = os.getenv("FUSIONBRAIN_URL", "https://api-key.fusionbrain.ai/")
FUSIONBRAIN_API_KEY = os.getenv("FUSIONBRAIN_API_KEY", "")
FUSIONBRAIN_SECRET_KEY = os.getenv("FUSIONBRAIN_SECRET_KEY", "")
_GEN_POLL_ATTEMPTS = 12
_GEN_POLL_DELAY = 5

_NEGATIVE_PROMPT = "текст, надписи, буквы, логотипы, водяные знаки, коллаж, рамка"


def generate_image(prompt: str) -> bytes | None:
    """Генерирует иллюстрацию через FusionBrain API (Kandinsky).

    Возвращает None, если ключи не заданы или сервис не ответил, — вызывающий
    код в этом случае рисует обычную обложку.
    """
    import base64
    import json

    if not (FUSIONBRAIN_API_KEY and FUSIONBRAIN_SECRET_KEY):
        log.info("Ключи FusionBrain не заданы — генерация пропущена")
        return None
    if not prompt:
        return None

    headers = {
        "X-Key": f"Key {FUSIONBRAIN_API_KEY}",
        "X-Secret": f"Secret {FUSIONBRAIN_SECRET_KEY}",
    }
    base = FUSIONBRAIN_URL.rstrip("/")

    try:
        pipelines = requests.get(
            f"{base}/key/api/v1/pipelines", headers=headers, timeout=HTTP_TIMEOUT
        )
        pipelines.raise_for_status()
        pipeline_id = pipelines.json()[0]["id"]

        params = {
            "type": "GENERATE",
            "numImages": 1,
            "width": WIDTH,
            "height": HEIGHT,
            "negativePromptDecoder": _NEGATIVE_PROMPT,
            "generateParams": {"query": prompt[:900]},
        }
        run = requests.post(
            f"{base}/key/api/v1/pipeline/run",
            headers=headers,
            files={
                "pipeline_id": (None, pipeline_id),
                "params": (None, json.dumps(params), "application/json"),
            },
            timeout=HTTP_TIMEOUT,
        )
        run.raise_for_status()
        task_id = run.json()["uuid"]

        for _ in range(_GEN_POLL_ATTEMPTS):
            time.sleep(_GEN_POLL_DELAY)
            status = requests.get(
                f"{base}/key/api/v1/pipeline/status/{task_id}",
                headers=headers,
                timeout=HTTP_TIMEOUT,
            )
            status.raise_for_status()
            payload = status.json()
            state = payload.get("status")
            if state == "DONE":
                files = payload.get("result", {}).get("files") or []
                if not files:
                    return None
                log.info("Иллюстрация сгенерирована Kandinsky")
                return base64.b64decode(files[0])
            if state == "FAIL":
                log.warning("FusionBrain вернул FAIL: %s", payload.get("errorDescription"))
                return None
        log.warning("FusionBrain не успел сгенерировать картинку за отведённое время")
    except (requests.RequestException, KeyError, IndexError, ValueError) as exc:
        log.warning("Генерация иллюстрации не удалась: %s", exc)
    return None


# --------------------------------------------------------------------------- #
# Точка входа
# --------------------------------------------------------------------------- #


def build_image(
    title: str,
    articles: Sequence[Article],
    image_prompt: str = "",
    mode: str | None = None,
) -> bytes | None:
    """Готовит картинку к посту согласно выбранному режиму."""
    mode = (mode or MODE).lower()
    if mode == "off":
        return None

    if mode == "source":
        for article in articles:
            data = download_image(article.image_url)
            if data:
                log.info("Использую фото из ленты: %s", article.source)
                return data
        log.info("Подходящего фото в лентах нет — рисую обложку")

    elif mode == "ai":
        prompt = image_prompt or f"иллюстрация к новостям: {title}"
        data = generate_image(prompt)
        if data:
            return data
        log.info("Генерация недоступна — рисую обложку")

    elif mode != "cover":
        log.warning("Неизвестный IMAGE_MODE=%r, рисую обложку", mode)

    try:
        return render_cover(title, articles)
    except Exception as exc:  # noqa: BLE001 - картинка не должна ронять выпуск
        log.warning("Не удалось нарисовать обложку: %s", exc)
        return None
