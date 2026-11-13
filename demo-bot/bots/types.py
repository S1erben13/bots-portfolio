"""Общий для всех мессенджеров формат входящих событий и ответов.

Адаптер Telegram/MAX переводит «своё» событие в IncomingEvent, отдаёт его движку
диалога и рисует полученные Reply своими средствами (inline-кнопки и т.п.).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Button:
    text: str
    payload: str  # уходит обратно боту как IncomingEvent.callback


@dataclass(frozen=True)
class Reply:
    text: str
    buttons: list[list[Button]] = field(default_factory=list)
    edit: bool = False
    callback_answer: str = ""
    error: bool = False


@dataclass(frozen=True)
class IncomingPhoto:
    content: bytes
    filename: str = "photo.jpg"


@dataclass(frozen=True)
class IncomingEvent:
    messenger: str
    user_id: str  # кому отвечать (id пользователя/диалога в мессенджере)
    name: str = ""
    username: str = ""
    text: str | None = None
    start_payload: str | None = None  # параметр deep link (?start=...), "" — /start без кода
    callback: str | None = None  # payload нажатой inline-кнопки
    photos: tuple[IncomingPhoto, ...] = ()


# payload кнопок
CB_SKIP_PHOTO = "skip_photo"
CB_MY_TICKETS = "my_tickets"
CB_NEW_TICKET = "new_ticket"
CB_CANCEL = "cancel"
