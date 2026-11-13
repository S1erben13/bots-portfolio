"""Доменная модель: компания → объекты → оборудование → заявки.

Модели не знают ни о Telegram, ни о MAX: мессенджер — это просто поле у клиента.
"""

import secrets

from django.conf import settings
from django.db import models

# Алфавит кода без похожих символов (0/O, 1/I/L) — код удобно продиктовать по телефону
CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
CODE_LENGTH = 6


def generate_equipment_code() -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))


class Messenger(models.TextChoices):
    TELEGRAM = "telegram", "Telegram"
    MAX = "max", "MAX"
    CONSOLE = "console", "Демо (консоль)"


class Company(models.Model):
    name = models.CharField("название", max_length=200)
    phone = models.CharField("телефон диспетчерской", max_length=50, blank=True)
    created_at = models.DateTimeField("создана", auto_now_add=True)

    class Meta:
        verbose_name = "компания"
        verbose_name_plural = "компании"
        ordering = ["name"]

    def __str__(self):
        return self.name


class Site(models.Model):
    """Объект обслуживания: ЖК, офис, склад, частный дом."""

    company = models.ForeignKey(
        Company, verbose_name="компания", on_delete=models.CASCADE, related_name="sites"
    )
    name = models.CharField("название", max_length=200)
    address = models.CharField("адрес", max_length=300, blank=True)
    contact = models.CharField("контакт на объекте", max_length=200, blank=True)
    notes = models.TextField("примечания", blank=True)

    class Meta:
        verbose_name = "объект"
        verbose_name_plural = "объекты"
        ordering = ["name"]

    def __str__(self):
        return self.name


class Equipment(models.Model):
    class Kind(models.TextChoices):
        CAMERA = "camera", "Камера видеонаблюдения"
        RECORDER = "recorder", "Видеорегистратор"
        INTERCOM = "intercom", "Домофон"
        ACCESS = "access", "СКУД / считыватель"
        BARRIER = "barrier", "Шлагбаум / ворота"
        OTHER = "other", "Другое"

    site = models.ForeignKey(
        Site, verbose_name="объект", on_delete=models.CASCADE, related_name="equipment"
    )
    kind = models.CharField("тип", max_length=20, choices=Kind.choices, default=Kind.OTHER)
    name = models.CharField("название", max_length=200, help_text="Например: «Камера у подъезда 2»")
    location = models.CharField("где установлено", max_length=200, blank=True)
    model_name = models.CharField("модель", max_length=200, blank=True)
    code = models.CharField(
        "код для QR",
        max_length=32,
        unique=True,
        default=generate_equipment_code,
        help_text="Уникальный код на наклейке. Передаётся боту через QR-ссылку.",
    )
    is_active = models.BooleanField("принимать заявки", default=True)

    class Meta:
        verbose_name = "оборудование"
        verbose_name_plural = "оборудование"
        ordering = ["site__name", "name"]

    def __str__(self):
        return f"{self.name} ({self.site})"

    @property
    def company(self) -> Company:
        return self.site.company


class Client(models.Model):
    """Человек, написавший боту. Идентифицируется парой (мессенджер, id в мессенджере)."""

    messenger = models.CharField("мессенджер", max_length=20, choices=Messenger.choices)
    external_id = models.CharField("ID в мессенджере", max_length=64)
    name = models.CharField("имя", max_length=200, blank=True)
    username = models.CharField("username", max_length=200, blank=True)
    phone = models.CharField("телефон", max_length=50, blank=True)
    created_at = models.DateTimeField("впервые написал", auto_now_add=True)

    class Meta:
        verbose_name = "клиент"
        verbose_name_plural = "клиенты"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["messenger", "external_id"], name="uniq_client_per_messenger"
            )
        ]

    def __str__(self):
        label = self.name or self.username or self.external_id
        return f"{label} ({self.get_messenger_display()})"


class Ticket(models.Model):
    class Status(models.TextChoices):
        NEW = "new", "Новая"
        ACCEPTED = "accepted", "Принята"
        EN_ROUTE = "en_route", "Техник выехал"
        DONE = "done", "Выполнена"
        CANCELLED = "cancelled", "Отменена"

    OPEN_STATUSES = (Status.NEW, Status.ACCEPTED, Status.EN_ROUTE)

    company = models.ForeignKey(
        Company, verbose_name="компания", on_delete=models.PROTECT, related_name="tickets"
    )
    site = models.ForeignKey(
        Site, verbose_name="объект", on_delete=models.PROTECT, related_name="tickets"
    )
    equipment = models.ForeignKey(
        Equipment,
        verbose_name="оборудование",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="tickets",
    )
    client = models.ForeignKey(
        Client,
        verbose_name="клиент",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="tickets",
    )
    description = models.TextField("описание проблемы")
    status = models.CharField(
        "статус", max_length=20, choices=Status.choices, default=Status.NEW, db_index=True
    )
    technician = models.CharField("техник", max_length=200, blank=True)
    internal_note = models.TextField("заметка диспетчера", blank=True)
    created_at = models.DateTimeField("создана", auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField("изменена", auto_now=True)

    class Meta:
        verbose_name = "заявка"
        verbose_name_plural = "заявки"
        ordering = ["-created_at"]

    def __str__(self):
        return f"Заявка №{self.number}"

    @property
    def number(self) -> str:
        return f"{self.pk:05d}" if self.pk else "—"

    @property
    def is_open(self) -> bool:
        return self.status in self.OPEN_STATUSES


class TicketStatusChange(models.Model):
    ticket = models.ForeignKey(
        Ticket, verbose_name="заявка", on_delete=models.CASCADE, related_name="history"
    )
    old_status = models.CharField(
        "был статус", max_length=20, choices=Ticket.Status.choices, blank=True
    )
    new_status = models.CharField("стал статус", max_length=20, choices=Ticket.Status.choices)
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="кто изменил",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    comment = models.CharField("комментарий", max_length=500, blank=True)
    created_at = models.DateTimeField("когда", auto_now_add=True)

    class Meta:
        verbose_name = "смена статуса"
        verbose_name_plural = "история статусов"
        ordering = ["created_at", "pk"]

    def __str__(self):
        return f"{self.get_old_status_display() or '—'} → {self.get_new_status_display()}"


def ticket_photo_path(instance: "TicketPhoto", filename: str) -> str:
    return f"tickets/{instance.ticket_id}/{secrets.token_hex(8)}_{filename}"


class TicketPhoto(models.Model):
    ticket = models.ForeignKey(
        Ticket, verbose_name="заявка", on_delete=models.CASCADE, related_name="photos"
    )
    image = models.ImageField("фото", upload_to=ticket_photo_path)
    created_at = models.DateTimeField("загружено", auto_now_add=True)

    class Meta:
        verbose_name = "фото"
        verbose_name_plural = "фото"
        ordering = ["created_at"]

    def __str__(self):
        return f"Фото к заявке №{self.ticket.number}"


class Notification(models.Model):
    """Очередь исходящих уведомлений (outbox).

    Веб-админка только кладёт сообщение в очередь; бот-процесс забирает и отправляет
    через нужный мессенджер. Так админка не зависит от доступности Telegram/MAX,
    а неотправленные уведомления видны и переотправляются.
    """

    class State(models.TextChoices):
        PENDING = "pending", "В очереди"
        SENT = "sent", "Отправлено"
        FAILED = "failed", "Ошибка"

    client = models.ForeignKey(
        Client, verbose_name="клиент", on_delete=models.CASCADE, related_name="notifications"
    )
    ticket = models.ForeignKey(
        Ticket,
        verbose_name="заявка",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="notifications",
    )
    text = models.TextField("текст")
    state = models.CharField(
        "состояние", max_length=20, choices=State.choices, default=State.PENDING, db_index=True
    )
    attempts = models.PositiveSmallIntegerField("попыток", default=0)
    next_attempt_at = models.DateTimeField("следующая попытка", null=True, blank=True)
    last_error = models.TextField("последняя ошибка", blank=True)
    created_at = models.DateTimeField("создано", auto_now_add=True)
    sent_at = models.DateTimeField("отправлено", null=True, blank=True)

    class Meta:
        verbose_name = "уведомление"
        verbose_name_plural = "уведомления клиентам"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.client}: {self.text[:40]}"
