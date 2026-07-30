"""Общие фикстуры тестов."""

from __future__ import annotations

import os
import sys
import types
from datetime import datetime, timezone
from pathlib import Path

import pytest

# Тесты не должны трогать боевую базу и ходить в сеть.
os.environ.setdefault("DB_PATH", ":memory:")
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test")
os.environ.setdefault("FETCH_FULL_TEXT", "0")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fetcher import Article  # noqa: E402


@pytest.fixture
def now() -> datetime:
    return datetime.now(timezone.utc)


@pytest.fixture
def article_factory(now):
    """Создаёт новость с разумными значениями по умолчанию."""

    def make(
        title: str = "Заголовок",
        topic: str = "taxi",
        url: str | None = None,
        summary: str = "Описание новости.",
        source: str = "Источник",
        is_penza: bool = False,
        **kwargs,
    ) -> Article:
        return Article(
            title=title,
            url=url or f"https://example.ru/{abs(hash(title)) % 10**6}",
            summary=summary,
            source=source,
            topic=topic,
            published=kwargs.pop("published", now),
            is_penza=is_penza,
            **kwargs,
        )

    return make


@pytest.fixture
def conn():
    """Отдельная база в памяти на каждый тест."""
    import fetcher

    connection = fetcher.connect(":memory:")
    yield connection
    connection.close()


def anthropic_message(text: str, stop_reason: str = "end_turn"):
    """Ответ Anthropic API в том виде, в каком его читает summarizer."""
    return types.SimpleNamespace(
        content=[types.SimpleNamespace(type="text", text=text)],
        stop_reason=stop_reason,
        model="claude-haiku-4-5-20251001",
        usage=types.SimpleNamespace(input_tokens=100, output_tokens=200),
    )
