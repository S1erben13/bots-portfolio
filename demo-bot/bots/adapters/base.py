"""Общий каркас адаптера мессенджера.

Адаптер умеет три вещи:
1) получать события из мессенджера и превращать их в IncomingEvent;
2) отправлять Reply пользователю (send);
3) забирать из очереди Notification уведомления для «своих» клиентов и отправлять их.
Вся бизнес-логика — в bots.engine и service.services.
"""

from __future__ import annotations

import abc
import asyncio
import logging

from asgiref.sync import sync_to_async

from ..engine import DialogEngine
from ..outbox import claim_pending, mark_failed, mark_sent
from ..types import IncomingEvent, Reply

log = logging.getLogger(__name__)


class MessengerAdapter(abc.ABC):
    #: значение service.models.Messenger
    messenger: str = ""
    outbox_interval: float = 3.0
    #: пауза между уведомлениями из очереди, сек
    send_interval: float = 0.6

    def __init__(self, engine: DialogEngine | None = None):
        self.engine = engine or DialogEngine()

    @abc.abstractmethod
    async def send(self, user_id: str, reply: Reply) -> None:
        """Отправить сообщение пользователю. Ошибку доставки — пробросить исключением."""

    @abc.abstractmethod
    async def poll_updates(self) -> None:
        """Бесконечный цикл получения событий из мессенджера."""

    async def process(self, event: IncomingEvent) -> list[Reply]:
        replies = await sync_to_async(self.engine.handle)(event)
        for reply in replies:
            await self.send(event.user_id, reply)
        return replies

    async def deliver_notifications_once(self, max_items: int = 20) -> int:
        sent = 0
        while sent < max_items:
            # Берём по одному: аренда выдаётся непосредственно перед отправкой
            batch = await sync_to_async(claim_pending)(self.messenger, limit=1)
            if not batch:
                break
            item = batch[0]
            if sent and self.send_interval:
                # Мягкий лимит скорости (MAX — до 2 сообщений/с в диалог; у Telegram ~30/с на бота)
                await asyncio.sleep(self.send_interval)
            sent += 1
            try:
                await self.send(item.user_id, Reply(item.text, [list(row) for row in item.buttons]))
            except Exception as exc:  # noqa: BLE001 — любую ошибку сети фиксируем в outbox
                log.warning("[%s] уведомление %s не доставлено: %s", self.messenger, item.id, exc)
                await sync_to_async(mark_failed)(item.id, str(exc))
            else:
                await sync_to_async(mark_sent)(item.id)
        return sent

    async def run_outbox(self) -> None:
        while True:
            try:
                await self.deliver_notifications_once()
            except Exception:  # noqa: BLE001
                log.exception("[%s] ошибка обработки очереди уведомлений", self.messenger)
            await asyncio.sleep(self.outbox_interval)

    async def run(self) -> None:
        log.info("[%s] адаптер запущен", self.messenger)
        # TaskGroup: если упадёт приём событий, цикл очереди тоже будет отменён,
        # и супервизор не наплодит параллельных обработчиков outbox при перезапуске.
        async with asyncio.TaskGroup() as group:
            group.create_task(self.poll_updates())
            group.create_task(self.run_outbox())
