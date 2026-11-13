"""Фейковый «мессенджер»: печатает сообщения в консоль или копит их в памяти.

Нужен для демо и тестов без токенов: клиенты с messenger=console получают
уведомления в лог бот-контейнера.
"""

from __future__ import annotations

import asyncio
import logging

from service.models import Messenger

from ..types import Reply
from .base import MessengerAdapter

log = logging.getLogger(__name__)


def render(reply: Reply) -> str:
    text = reply.text
    buttons = [f"[{b.text}]" for row in reply.buttons for b in row]
    if buttons:
        text += "\n" + " ".join(buttons)
    return text


class ConsoleAdapter(MessengerAdapter):
    messenger = Messenger.CONSOLE
    send_interval = 0.0

    def __init__(self, *args, echo: bool = True, **kwargs):
        super().__init__(*args, **kwargs)
        self.echo = echo
        self.sent: list[tuple[str, Reply]] = []

    async def send(self, user_id: str, reply: Reply) -> None:
        self.sent.append((user_id, reply))
        if self.echo:
            log.info("[console → %s] %s", user_id, render(reply))

    async def poll_updates(self) -> None:
        # Входящих событий нет: диалог можно прогнать командой `manage.py simulate_chat`
        await asyncio.Event().wait()
