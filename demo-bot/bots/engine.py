"""Движок диалога: сценарий «QR → описание → фото → заявка».

Работает синхронно с Django ORM и ничего не знает о конкретном мессенджере:
на вход IncomingEvent, на выход список Reply.
"""

from __future__ import annotations

import logging
import re
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from service import services
from service.models import Equipment, Ticket

from .models import Conversation
from .types import (
    CB_CANCEL,
    CB_MY_TICKETS,
    CB_NEW_TICKET,
    CB_SKIP_PHOTO,
    Button,
    IncomingEvent,
    Reply,
)

log = logging.getLogger(__name__)

# Фото, присланные вскоре после создания заявки (например, альбомом), докладываются к ней
ATTACH_PHOTO_WINDOW = timedelta(minutes=10)
START_RE = re.compile(r"^/start(?:@\w+)?(?:\s+(?P<payload>\S+))?", re.IGNORECASE)

BTN_MY = Button("📋 Мои заявки", CB_MY_TICKETS)
BTN_NEW = Button("➕ Новая заявка", CB_NEW_TICKET)
BTN_SKIP_PHOTO = Button("Без фото", CB_SKIP_PHOTO)
BTN_CANCEL = Button("Отмена", CB_CANCEL)

WELCOME = (
    "Здравствуйте! Это бот сервисной службы.\n\n"
    "Чтобы сообщить о неисправности, отсканируйте QR-код на наклейке оборудования "
    "или пришлите код с наклейки (6 символов)."
)
HELP = (
    "Я принимаю заявки по оборудованию.\n"
    "• Отсканируйте QR-код на наклейке или пришлите код с неё.\n"
    "• /my — мои заявки\n"
    "• /cancel — отменить текущую заявку"
)


def equipment_card(equipment: Equipment) -> str:
    site = equipment.site
    address = f", {site.address}" if site.address else ""
    location = f" ({equipment.location})" if equipment.location else ""
    return f"Объект: {site.name}{address}\nОборудование: {equipment.name}{location}"


class DialogEngine:
    def handle(self, event: IncomingEvent) -> list[Reply]:
        with transaction.atomic():
            client = services.get_or_create_client(
                services.ContactInfo(
                    messenger=event.messenger,
                    external_id=event.user_id,
                    name=event.name,
                    username=event.username,
                )
            )
            conv, _ = Conversation.objects.select_for_update().get_or_create(client=client)
            try:
                with transaction.atomic():
                    replies = self._dispatch(conv, event)
            except services.ServiceError as exc:
                conv.reset()
                replies = [Reply(f"{exc}. Попробуйте ещё раз.", [[BTN_NEW, BTN_MY]])]
            conv.save()
            return replies

    # --- маршрутизация ---

    def _dispatch(self, conv: Conversation, event: IncomingEvent) -> list[Reply]:
        text = (event.text or "").strip()
        start_payload = event.start_payload
        if start_payload is None and (match := START_RE.match(text)):
            start_payload = match.group("payload") or ""

        if start_payload is not None:
            if start_payload:
                return self._start_with_code(conv, start_payload)
            conv.reset()
            return [Reply(WELCOME, [[BTN_MY]])]

        command = event.callback or text.lower()
        if command in {CB_CANCEL, "/cancel", "отмена"}:
            conv.reset()
            return [Reply("Хорошо, заявка не создана.", [[BTN_NEW, BTN_MY]])]
        if command in {CB_MY_TICKETS, "/my", "мои заявки"}:
            return [self._my_tickets(conv)]
        if command in {"/help", "помощь"}:
            return [Reply(HELP, [[BTN_NEW, BTN_MY]])]
        if command == CB_NEW_TICKET:
            conv.reset()
            conv.state = Conversation.State.AWAIT_CODE
            return [Reply("Пришлите код с наклейки на оборудовании (6 символов).", [[BTN_CANCEL]])]

        state = conv.state
        if state == Conversation.State.AWAIT_DESCRIPTION:
            return self._on_description(conv, event, text)
        if state == Conversation.State.AWAIT_PHOTO:
            return self._on_photo_step(conv, event, text)
        return self._on_idle(conv, event, text)

    # --- шаги сценария ---

    def _start_with_code(self, conv: Conversation, code: str) -> list[Reply]:
        try:
            equipment = services.find_equipment(code)
        except services.EquipmentNotFound:
            conv.reset()
            conv.state = Conversation.State.AWAIT_CODE
            return [
                Reply(
                    "Не нашёл оборудование с таким кодом. Проверьте код на наклейке "
                    "и пришлите его сообщением.",
                    [[BTN_CANCEL]],
                )
            ]
        conv.reset()
        conv.equipment = equipment
        conv.state = Conversation.State.AWAIT_DESCRIPTION
        return [
            Reply(
                f"{equipment_card(equipment)}\n\n"
                "Опишите, пожалуйста, что случилось. Можно сразу прислать фото с подписью.",
                [[BTN_CANCEL]],
            )
        ]

    def _on_description(self, conv: Conversation, event: IncomingEvent, text: str) -> list[Reply]:
        if event.photos and text:
            return self._create_ticket(conv, text, event)
        if event.photos:
            return [Reply("Добавьте к фото подпись с описанием проблемы или напишите её текстом.")]
        if not text:
            return [Reply("Напишите текстом, что случилось.", [[BTN_CANCEL]])]
        conv.draft_description = text
        conv.state = Conversation.State.AWAIT_PHOTO
        return [
            Reply(
                "Спасибо! Пришлите фото неисправности — так технику будет проще. "
                "Если фото нет, нажмите «Без фото».",
                [[BTN_SKIP_PHOTO], [BTN_CANCEL]],
            )
        ]

    def _on_photo_step(self, conv: Conversation, event: IncomingEvent, text: str) -> list[Reply]:
        if event.photos:
            description = conv.draft_description
            if text:
                description = f"{description}\n{text}"
            return self._create_ticket(conv, description, event)
        if event.callback == CB_SKIP_PHOTO:
            return self._create_ticket(conv, conv.draft_description, event)
        if text:
            conv.draft_description = f"{conv.draft_description}\n{text}".strip()
            return [
                Reply(
                    "Добавил к описанию. Пришлите фото или нажмите «Без фото».",
                    [[BTN_SKIP_PHOTO], [BTN_CANCEL]],
                )
            ]
        return [Reply("Пришлите фото или нажмите «Без фото».", [[BTN_SKIP_PHOTO], [BTN_CANCEL]])]

    def _on_idle(self, conv: Conversation, event: IncomingEvent, text: str) -> list[Reply]:
        if event.photos:
            recent = (
                conv.client.tickets.filter(
                    status__in=Ticket.OPEN_STATUSES,
                    created_at__gte=timezone.now() - ATTACH_PHOTO_WINDOW,
                )
                .order_by("-created_at")
                .first()
            )
            if recent:
                for photo in event.photos:
                    services.add_photo(recent, services.PhotoUpload(photo.content, photo.filename))
                return [Reply(f"Фото добавлено к заявке №{recent.number}.")]
        if text and not text.startswith("/"):
            try:
                services.find_equipment(text)
            except services.EquipmentNotFound:
                if conv.state == Conversation.State.AWAIT_CODE:
                    return [Reply("Такого кода нет. Проверьте и пришлите ещё раз.", [[BTN_CANCEL]])]
            else:
                return self._start_with_code(conv, text)
        return [Reply(HELP, [[BTN_NEW, BTN_MY]])]

    def _create_ticket(self, conv: Conversation, description: str, event: IncomingEvent):
        equipment = conv.equipment
        if equipment is None or not equipment.is_active:
            conv.reset()
            return [Reply("Не удалось определить оборудование. Отсканируйте QR-код ещё раз.")]
        ticket = services.create_ticket(
            equipment=equipment,
            client=conv.client,
            description=description,
            photos=[services.PhotoUpload(p.content, p.filename) for p in event.photos],
        )
        conv.reset()
        return [
            Reply(
                f"✅ Заявка №{ticket.number} создана.\n{equipment_card(equipment)}\n\n"
                f"Статус: {ticket.get_status_display()}. "
                "Я сообщу, когда диспетчер примет заявку и когда выедет техник.",
                [[BTN_MY]],
            )
        ]

    def _my_tickets(self, conv: Conversation) -> Reply:
        tickets = services.client_tickets(conv.client)
        if not tickets:
            return Reply("У вас пока нет заявок.", [[BTN_NEW]])
        lines = ["Ваши последние заявки:"]
        for t in tickets:
            what = t.equipment.name if t.equipment else t.site.name
            lines.append(
                f"№{t.number} от {timezone.localtime(t.created_at):%d.%m %H:%M} — "
                f"{t.get_status_display()}\n   {what}"
            )
        return Reply("\n".join(lines), [[BTN_NEW]])
