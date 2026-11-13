import json
from unittest.mock import Mock

import pytest

from app.knowledge import Chunk, KnowledgeBase
from app.service import Consultant, NO_ANSWER, fallback_excerpt


class FakeProvider:
    def __init__(self, result='{"answerable":true,"answer":"Гарантия на монтаж — 12 месяцев. Срок начинается с даты подписания акта."}'):
        self.result, self.calls = result, 0

    async def complete(self, prompt):
        self.calls += 1
        self.prompt = prompt
        return self.result


async def test_refusal_before_llm(kb_path):
    provider = FakeProvider()
    result = await Consultant(KnowledgeBase(kb_path), provider).answer("Как приготовить борщ?")
    assert result == {"answer": NO_ANSWER, "sources": []}
    assert provider.calls == 0


async def test_llm_answer_and_sources(kb_path):
    kb = KnowledgeBase(kb_path)
    question = "Какая гарантия на монтаж?"
    provider = FakeProvider()
    result = await Consultant(kb, provider).answer(question)
    assert result["answer"] == json.loads(provider.result)["answer"]
    assert provider.calls == 1
    assert json.loads(provider.prompt) == {"question": question, "fragments": [
        {"id": i, "text": c.text} for i, c in enumerate(kb.search(question), 1)
    ]}
    assert result["sources"][0]["url"] == "/sources/06-warranty.md"


async def test_plain_text_response_falls_back(kb_path):
    result = await Consultant(KnowledgeBase(kb_path), FakeProvider('Я обещаю 100 лет гарантии')).answer("Гарантия на монтаж")
    assert "100 лет" not in result["answer"]
    assert "12 месяцев" in result["answer"]


async def test_llm_refusal():
    kb = Mock()
    kb.search.return_value = [Chunk("diagnostics.md", "Диагностика видеонаблюдения — 1500 рублей.")]
    provider = FakeProvider('{"answerable":false,"answer":"Диагностика стоит 1500 рублей."}')
    result = await Consultant(kb, provider).answer("Сколько стоит ремонт стиральной машины?")
    assert result == {"answer": NO_ANSWER, "sources": []}
    assert provider.calls == 1


def test_fallback_empty_heading_does_not_consume_next_paragraph():
    assert fallback_excerpt("#\r\n\r\nПолезный текст.") == "Полезный текст."


def test_fallback_limits_long_paragraph():
    excerpt = fallback_excerpt("Слово " * 150)
    assert len(excerpt) <= 601
    assert excerpt.endswith("…")


async def test_llm_unavailable_keeps_excerpt(kb_path):
    class BrokenProvider:
        async def complete(self, prompt):
            raise TimeoutError()

    result = await Consultant(KnowledgeBase(kb_path), BrokenProvider()).answer("Гарантия на монтаж")
    assert "12 месяцев" in result["answer"]
    assert result["sources"]
    assert "#" not in result["answer"]


@pytest.mark.parametrize("response", [
    '{"answerable":true,"answer":""}',
    '{"answerable":true,"answer":"   "}',
    '{"answerable":true,"answer":null}',
    '{"answerable":"false","answer":"Неверное решение"}',
    '{"ids":[1]}',
    '[]',
    'не JSON',
])
async def test_fallback_uses_first_two_paragraphs_of_top_chunk(response):
    kb = Mock()
    kb.search.return_value = [
        Chunk("top.md", "# Условия\n\nПервый абзац.\n\n## Подробности\n\nВторой абзац.\n\nТретий абзац."),
        Chunk("other.md", "Другой фрагмент."),
    ]
    result = await Consultant(kb, FakeProvider(response)).answer("Условия")
    assert result == {
        "answer": "Первый абзац.\n\nВторой абзац.",
        "sources": [{"file": "top.md", "url": "/sources/top.md"}],
    }


async def test_empty_fallback_refuses():
    kb = Mock()
    kb.search.return_value = [Chunk("empty.md", "# Только заголовок")]
    result = await Consultant(kb, FakeProvider('')).answer("Вопрос")
    assert result == {"answer": NO_ANSWER, "sources": []}
