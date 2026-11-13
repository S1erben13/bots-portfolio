"""Ссылки, которые зашиваются в QR-код наклейки."""

from django.conf import settings
from django.urls import reverse

from .models import Equipment


def telegram_link(code: str) -> str | None:
    if not settings.TELEGRAM_BOT_USERNAME:
        return None
    return f"https://t.me/{settings.TELEGRAM_BOT_USERNAME}?start={code}"


def max_link(code: str) -> str | None:
    if not settings.MAX_BOT_NAME:
        return None
    return f"https://max.ru/{settings.MAX_BOT_NAME}?start={code}"


def landing_link(code: str) -> str:
    return settings.PUBLIC_BASE_URL + reverse("service:landing", args=[code])


def qr_link(equipment: Equipment) -> str:
    """Куда ведёт QR. По умолчанию — на страницу выбора мессенджера,
    чтобы одна наклейка работала и для Telegram, и для MAX."""
    target = settings.QR_TARGET
    if target == "telegram" and telegram_link(equipment.code):
        return telegram_link(equipment.code)
    if target == "max" and max_link(equipment.code):
        return max_link(equipment.code)
    return landing_link(equipment.code)
