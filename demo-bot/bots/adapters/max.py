"""Адаптер мессенджера MAX (Bot API platform-api2.max.ru, long polling).

Сделано по официальной OpenAPI-спецификации (github.com/max-messenger/api-schema,
версия 0.0.34) и документации dev.max.ru:
- авторизация: заголовок `Authorization: <token>` (query-параметр больше не принимается);
- события: GET /updates (long polling, `marker`), типы bot_started / message_created /
  message_callback;
- deep link: https://max.ru/<botName>?start=<payload> → событие `bot_started` с полем `payload`;
- отправка: POST /messages?user_id=..., кнопки — attachment `inline_keyboard`;
- ответ на нажатие кнопки: POST /answers?callback_id=...
- входящие фото: attachment `image`, payload.url.

Неясные места (проверить на живом боте) вынесены в константы и описаны в README.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, replace

import httpx
from asgiref.sync import sync_to_async

from service.models import Messenger

from ..types import IncomingEvent, IncomingPhoto, Reply
from .base import MessengerAdapter

log = logging.getLogger(__name__)

UPDATE_TYPES = ("bot_started", "message_created", "message_callback")
POLL_TIMEOUT = 30  # сек, максимум по API — 90
MIN_POLL_INTERVAL = 0.3  # «Minimum polling time - 300ms» из спецификации
MAX_PHOTO_BYTES = 20 * 1024 * 1024
# Текст всплывающего уведомления при нажатии кнопки. Спецификация не говорит, можно ли
# ответить на callback пустым телом, поэтому отправляем короткое уведомление.
CALLBACK_ACK_TEXT = "Принято"


class MaxApiError(Exception):
    def __init__(self, status: int, code: str | None, message: str):
        super().__init__(f"MAX API {status} {code or ''}: {message}".strip())
        self.status = status
        self.code = code


class MaxApi:
    """Тонкий клиент MAX Bot API: только нужные боту методы."""

    def __init__(self, token: str, base_url: str, http: httpx.AsyncClient | None = None):
        self.http = http or httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": token},
            timeout=httpx.Timeout(POLL_TIMEOUT + 15, connect=10),
        )
        # Фото скачиваются с CDN — без заголовка авторизации, чтобы не светить токен
        self.files = httpx.AsyncClient(timeout=30, follow_redirects=True)

    @staticmethod
    def _check(response: httpx.Response) -> dict:
        try:
            data = response.json()
        except ValueError:
            data = {}
        if response.status_code >= 400 or (isinstance(data, dict) and data.get("success") is False):
            raise MaxApiError(
                response.status_code, data.get("code"), data.get("message") or response.text[:200]
            )
        return data

    async def get_me(self) -> dict:
        return self._check(await self.http.get("/me"))

    async def get_updates(self, marker: int | None) -> tuple[list[dict], int | None]:
        params: dict = {"timeout": POLL_TIMEOUT, "limit": 100, "types": ",".join(UPDATE_TYPES)}
        if marker is not None:
            params["marker"] = marker
        data = self._check(await self.http.get("/updates", params=params))
        return data.get("updates") or [], data.get("marker")

    async def send_message(self, user_id: str, text: str, attachments: list | None = None) -> dict:
        body: dict = {"text": text}
        if attachments:
            body["attachments"] = attachments
        return self._check(await self.http.post("/messages", params={"user_id": user_id}, json=body))

    async def answer_callback(self, callback_id: str, notification: str | None = None,
                              message: dict | None = None) -> dict:
        body = {"notification": notification} if notification else {}
        if message is not None:
            body["message"] = message
        return self._check(
            await self.http.post("/answers", params={"callback_id": callback_id}, json=body)
        )

    async def download(self, url: str) -> bytes:
        async with self.files.stream("GET", url) as response:
            response.raise_for_status()
            chunks, size = [], 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > MAX_PHOTO_BYTES:
                    raise ValueError("Фото слишком большое")
                chunks.append(chunk)
        return b"".join(chunks)

    async def close(self) -> None:
        await self.http.aclose()
        await self.files.aclose()


# --- преобразование событий MAX → IncomingEvent (чистые функции, покрыты тестами) ---


@dataclass(frozen=True)
class ParsedUpdate:
    event: IncomingEvent
    photo_urls: tuple[str, ...] = ()
    callback_id: str | None = None


def _user_name(user: dict) -> str:
    full = " ".join(p for p in (user.get("first_name"), user.get("last_name")) if p)
    return full or user.get("name") or ""


def _event_base(user: dict) -> dict:
    return {
        "messenger": Messenger.MAX,
        "user_id": str(user["user_id"]),
        "name": _user_name(user),
        "username": user.get("username") or "",
    }


def parse_update(update: dict) -> ParsedUpdate | None:
    kind = update.get("update_type")

    if kind == "bot_started":
        user = update.get("user") or {}
        if "user_id" not in user:
            return None
        payload = (update.get("payload") or "").strip()
        return ParsedUpdate(IncomingEvent(**_event_base(user), start_payload=payload))

    if kind == "message_created":
        message = update.get("message") or {}
        sender = message.get("sender") or {}
        recipient = message.get("recipient") or {}
        if not sender or sender.get("is_bot") or "user_id" not in sender:
            return None
        if recipient.get("chat_type") not in (None, "dialog"):
            return None  # группы и каналы не обслуживаем
        body = message.get("body") or {}
        urls = tuple(
            att["payload"]["url"]
            for att in body.get("attachments") or []
            if att.get("type") == "image" and (att.get("payload") or {}).get("url")
        )
        return ParsedUpdate(IncomingEvent(**_event_base(sender), text=body.get("text")), urls)

    if kind == "message_callback":
        callback = update.get("callback") or {}
        user = callback.get("user") or {}
        if "user_id" not in user:
            return None
        recipient = (update.get("message") or {}).get("recipient") or {}
        if recipient.get("chat_type") not in (None, "dialog"):
            return None
        return ParsedUpdate(
            IncomingEvent(**_event_base(user), callback=callback.get("payload") or ""),
            callback_id=callback.get("callback_id"),
        )

    return None


def keyboard_attachments(reply: Reply) -> list[dict]:
    if not reply.buttons:
        return []
    return [
        {
            "type": "inline_keyboard",
            "payload": {
                "buttons": [
                    [{"type": "callback", "text": b.text, "payload": b.payload} for b in row]
                    for row in reply.buttons
                ]
            },
        }
    ]


class MaxAdapter(MessengerAdapter):
    messenger = Messenger.MAX

    def __init__(self, token: str, base_url: str, *args, api: MaxApi | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.api = api or MaxApi(token, base_url)

    async def send(self, user_id: str, reply: Reply) -> None:
        await self.api.send_message(user_id, reply.text, keyboard_attachments(reply))

    async def handle_update(self, update: dict) -> None:
        parsed = parse_update(update)
        if parsed is None:
            return
        if parsed.event.callback and parsed.event.callback.startswith("ds:"):
            if not parsed.callback_id:
                return
            reply = (await sync_to_async(self.engine.handle)(parsed.event))[0]
            message = {"text": reply.text, "attachments": keyboard_attachments(reply)} if reply.edit else None
            await self.api.answer_callback(parsed.callback_id, reply.callback_answer, message=message)
            return
        if parsed.callback_id:
            try:
                await self.api.answer_callback(parsed.callback_id, CALLBACK_ACK_TEXT)
            except Exception as exc:  # noqa: BLE001 — не критично для сценария
                log.warning("[max] не удалось ответить на callback: %s", exc)
        event = parsed.event
        if parsed.photo_urls:
            photos = []
            for index, url in enumerate(parsed.photo_urls):
                photos.append(IncomingPhoto(await self.api.download(url), f"max_{index}.jpg"))
            event = replace(event, photos=tuple(photos))
        await self.process(event)

    async def poll_updates(self) -> None:
        me = await self.api.get_me()
        log.info("[max] бот %s (@%s) подключён", me.get("first_name") or me.get("name"), me.get("username"))
        marker: int | None = None
        backoff = 1.0
        while True:
            try:
                updates, next_marker = await self.api.get_updates(marker)
                backoff = 1.0
            except httpx.TimeoutException:
                continue
            except (httpx.HTTPError, MaxApiError) as exc:
                if isinstance(exc, MaxApiError) and exc.status == 401:
                    raise  # неверный токен — дальше смысла нет
                log.warning("[max] ошибка получения событий: %s; повтор через %.0f с", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
                continue

            for update in updates:
                try:
                    await self.handle_update(update)
                except Exception:
                    log.exception("[max] ошибка обработки события %s", update.get("update_type"))
                    user_id = self._user_id(update)
                    if user_id:
                        try:
                            await self.api.send_message(
                                user_id, "Не получилось обработать сообщение, попробуйте ещё раз."
                            )
                        except Exception:  # noqa: BLE001
                            pass
            if next_marker is not None:
                marker = next_marker
            if not updates:
                await asyncio.sleep(MIN_POLL_INTERVAL)

    @staticmethod
    def _user_id(update: dict) -> str | None:
        parsed = parse_update(update)
        return parsed.event.user_id if parsed else None
