"""Ядро бизнес-логики. Не зависит от мессенджеров и от HTTP.

Все изменения заявок проходят через эти функции — и из бота, и из админки,
поэтому история статусов и уведомления клиентам всегда консистентны.
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass

from PIL import Image, ImageOps, UnidentifiedImageError

from django.contrib.auth.models import AbstractBaseUser
from django.core.files.base import ContentFile
from django.db import transaction

from .models import (
    Client,
    Equipment,
    Notification,
    Ticket,
    TicketPhoto,
    TicketStatusChange,
)

log = logging.getLogger(__name__)

MAX_DESCRIPTION_LENGTH = 4000

STATUS_MESSAGES = {
    Ticket.Status.NEW: "зарегистрирована и ждёт диспетчера",
    Ticket.Status.ACCEPTED: "принята в работу",
    Ticket.Status.EN_ROUTE: "техник выехал на объект",
    Ticket.Status.DONE: "выполнена. Спасибо, что сообщили о проблеме!",
    Ticket.Status.CANCELLED: "отменена",
}


class ServiceError(Exception):
    """Ошибка, понятная пользователю."""


class EquipmentNotFound(ServiceError):
    pass


@dataclass(frozen=True)
class ContactInfo:
    """Кто пишет — в терминах, не зависящих от мессенджера."""

    messenger: str
    external_id: str
    name: str = ""
    username: str = ""


@dataclass(frozen=True)
class PhotoUpload:
    content: bytes
    filename: str = "photo.jpg"


def normalize_code(raw: str) -> str:
    return (raw or "").strip().upper()


def find_equipment(code: str) -> Equipment:
    """Ищет активное оборудование по коду с наклейки."""
    code = normalize_code(code)
    if not code:
        raise EquipmentNotFound("Пустой код")
    try:
        return Equipment.objects.select_related("site__company").get(code=code, is_active=True)
    except Equipment.DoesNotExist as exc:
        raise EquipmentNotFound(f"Оборудование с кодом {code} не найдено") from exc


def get_or_create_client(contact: ContactInfo) -> Client:
    client, created = Client.objects.get_or_create(
        messenger=contact.messenger,
        external_id=str(contact.external_id),
        defaults={"name": contact.name[:200], "username": contact.username[:200]},
    )
    if not created:
        changed = []
        if contact.name and client.name != contact.name[:200]:
            client.name = contact.name[:200]
            changed.append("name")
        if contact.username and client.username != contact.username[:200]:
            client.username = contact.username[:200]
            changed.append("username")
        if changed:
            client.save(update_fields=changed)
    return client


@transaction.atomic
def create_ticket(
    *,
    equipment: Equipment | str,
    client: Client,
    description: str,
    photos: list[PhotoUpload] | None = None,
) -> Ticket:
    """Создаёт заявку по оборудованию (объекту или коду с QR)."""
    if isinstance(equipment, str):
        equipment = find_equipment(equipment)
    description = (description or "").strip()
    if not description:
        raise ServiceError("Опишите проблему")
    site = equipment.site
    ticket = Ticket.objects.create(
        company=site.company,
        site=site,
        equipment=equipment,
        client=client,
        description=description[:MAX_DESCRIPTION_LENGTH],
    )
    TicketStatusChange.objects.create(ticket=ticket, old_status="", new_status=ticket.status)
    for photo in photos or []:
        add_photo(ticket, photo)
    log.info("Создана заявка №%s (оборудование %s)", ticket.number, equipment.code)
    return ticket


MAX_PHOTO_SIDE = 2560


def sanitize_image(content: bytes) -> bytes:
    """Перекодирует присланный файл в JPEG.

    Так в хранилище никогда не попадёт SVG/HTML со скриптом под видом картинки,
    а заодно срезаются EXIF-метаданные и лишнее разрешение.
    """
    try:
        with Image.open(io.BytesIO(content)) as img:
            img.verify()
        with Image.open(io.BytesIO(content)) as img:
            img = ImageOps.exif_transpose(img).convert("RGB")
            img.thumbnail((MAX_PHOTO_SIDE, MAX_PHOTO_SIDE))
            out = io.BytesIO()
            img.save(out, format="JPEG", quality=85)
            return out.getvalue()
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
        raise ServiceError("Не удалось прочитать фото — пришлите обычную картинку") from exc


def add_photo(ticket: Ticket, photo: PhotoUpload) -> TicketPhoto:
    item = TicketPhoto(ticket=ticket)
    item.image.save("photo.jpg", ContentFile(sanitize_image(photo.content)), save=True)
    return item


def status_text(ticket: Ticket) -> str:
    return f"Заявка №{ticket.number}: {STATUS_MESSAGES[ticket.status]}"


@transaction.atomic
def change_status(
    ticket: Ticket,
    new_status: str,
    *,
    user: AbstractBaseUser | None = None,
    comment: str = "",
) -> bool:
    """Меняет статус, пишет историю и ставит уведомление клиенту в очередь.

    Возвращает False, если статус не изменился.
    """
    if new_status not in Ticket.Status.values:
        raise ServiceError(f"Неизвестный статус: {new_status}")
    # Блокируем строку, чтобы два диспетчера не записали историю вперемешку
    locked = Ticket.objects.select_for_update().get(pk=ticket.pk)
    old_status = locked.status
    if old_status == new_status:
        return False

    locked.status = new_status
    locked.save(update_fields=["status", "updated_at"])
    ticket.status = new_status
    ticket.updated_at = locked.updated_at

    TicketStatusChange.objects.create(
        ticket=locked,
        old_status=old_status,
        new_status=new_status,
        changed_by=user if user is not None and user.is_authenticated else None,
        comment=comment[:500],
    )
    if locked.client_id:
        text = status_text(locked)
        if new_status == Ticket.Status.EN_ROUTE and locked.technician:
            text += f"\nТехник: {locked.technician}"
        if comment:
            text += f"\nКомментарий: {comment}"
        Notification.objects.create(client_id=locked.client_id, ticket=locked, text=text)
    log.info("Заявка №%s: %s → %s", locked.number, old_status, new_status)
    return True


def client_tickets(client: Client, limit: int = 5) -> list[Ticket]:
    return list(
        client.tickets.select_related("site", "equipment").order_by("-created_at")[:limit]
    )
