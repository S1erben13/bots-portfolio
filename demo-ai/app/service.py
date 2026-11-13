import json
import logging
import re
from urllib.parse import quote

from app.knowledge import KnowledgeBase
from app.providers import Provider

logger = logging.getLogger(__name__)
NO_ANSWER = "В базе нет ответа на этот вопрос. Предлагаем оставить заявку: укажите имя и телефон в форме или отправьте /lead в Telegram."


def fallback_excerpt(text: str) -> str:
    """Берём до двух абзацев, исключая Markdown-заголовки."""
    text = text.replace("\r\n", "\n")
    text = re.sub(r"(?m)^[ \t]{0,3}#{1,6}(?:[ \t]+.*|[ \t]*)$", "", text)
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    excerpt = "\n\n".join(paragraphs[:2])
    if len(excerpt) > 600:
        excerpt = excerpt[:600].rsplit(" ", 1)[0].rstrip() + "…"
    return excerpt


class Consultant:
    def __init__(self, kb: KnowledgeBase, provider: Provider):
        self.kb, self.provider = kb, provider

    async def answer(self, question: str) -> dict:
        chunks = self.kb.search(question)
        if not chunks:
            return {"answer": NO_ANSWER, "sources": []}
        prompt = json.dumps({"question": question, "fragments": [
            {"id": i, "text": c.text} for i, c in enumerate(chunks, 1)
        ]}, ensure_ascii=False)
        try:
            result = json.loads(await self.provider.complete(prompt))
            if not isinstance(result, dict) or type(result.get("answerable")) is not bool:
                raise ValueError("Неверный формат решения модели")
            if not result["answerable"]:
                return {"answer": NO_ANSWER, "sources": []}
            answer = result.get("answer")
            if not isinstance(answer, str) or not answer.strip():
                raise ValueError("Пустой или неверный ответ модели")
            answer = answer.strip()
        except Exception as exc:
            # Не логируем тело HTTP-ответа, ключи и пользовательский вопрос.
            logger.warning("LLM недоступна или неверный формат: %s", type(exc).__name__)
            chunks = chunks[:1]
            answer = fallback_excerpt(chunks[0].text)
        if not answer:
            return {"answer": NO_ANSWER, "sources": []}
        sources = list(dict.fromkeys(c.source for c in chunks))
        return {"answer": answer,
                "sources": [{"file": s, "url": "/sources/" + quote(s, safe="/")} for s in sources]}
