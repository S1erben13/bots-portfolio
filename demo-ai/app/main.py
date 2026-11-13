import asyncio
import logging
import os
import ssl
from contextlib import asynccontextmanager, suppress
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.config import Settings
from app.knowledge import KnowledgeBase
from app.leads import Leads
from app.providers import HTTPProvider
from app.service import Consultant


class Question(BaseModel):
    question: str = Field(min_length=1, max_length=2000)


class LeadInput(BaseModel):
    name: str = Field(min_length=2, max_length=100)
    phone: str = Field(min_length=10, max_length=30)
    consent: bool


def create_app(settings: Settings | None = None, provider=None) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app):
        # Дополнительный CA для GigaChat можно примонтировать, проверку TLS не отключаем.
        tls = ssl.create_default_context()
        if os.getenv("EXTRA_CA_FILE"):
            tls.load_verify_locations(os.environ["EXTRA_CA_FILE"])
        async with httpx.AsyncClient(timeout=60, verify=tls) as client:
            app.state.consultant = Consultant(
                KnowledgeBase(settings.kb_dir, settings.min_score, settings.min_coverage),
                provider or HTTPProvider(settings.provider, client))
            app.state.leads = Leads(settings.db_path, client, settings.bot_token, settings.manager_chat_id)
            task = None
            if settings.bot_token:
                from app.bot import run_bot
                task = asyncio.create_task(run_bot(settings.bot_token, app.state.consultant, app.state.leads))
            try:
                yield
            finally:
                if task:
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task

    app = FastAPI(title="Монтаж-Сервис — демо-консультант", lifespan=lifespan)

    @app.get("/")
    async def index():
        return FileResponse(Path(__file__).parent / "static/index.html")

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.post("/api/chat")
    async def chat(body: Question, request: Request):
        if not body.question.strip():
            raise HTTPException(422, "Введите вопрос")
        return await request.app.state.consultant.answer(body.question)

    @app.post("/api/leads", status_code=201)
    async def lead(body: LeadInput, request: Request):
        if not body.consent:
            raise HTTPException(422, "Нужно согласие на обработку заявки")
        try:
            return await request.app.state.leads.create(body.name, body.phone)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/sources/{filename:path}")
    async def source(filename: str):
        root = settings.kb_dir.resolve()
        path = (root / filename).resolve()
        if not path.is_relative_to(root) or not path.is_file() or path.suffix.lower() not in {".md", ".txt", ".pdf", ".docx"}:
            raise HTTPException(404, "Источник не найден")
        return FileResponse(path, filename=path.name)

    return app


logging.basicConfig(level=logging.INFO)
app = create_app()
