"""Адаптер Telegram на aiogram 3 (long polling)."""

from __future__ import annotations

import logging

from aiogram import Bot, Dispatcher, Router
from aiogram.enums import ChatType
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from service.models import Messenger

from ..types import IncomingEvent, IncomingPhoto, Reply
from .base import MessengerAdapter

log = logging.getLogger(__name__)

MAX_PHOTO_BYTES = 20 * 1024 * 1024  # больше Bot API всё равно не отдаёт


def keyboard(reply: Reply) -> InlineKeyboardMarkup | None:
    if not reply.buttons:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=b.text, callback_data=b.payload) for b in row]
            for row in reply.buttons
        ]
    )


def event_from_message(message: Message, photos: tuple[IncomingPhoto, ...] = ()) -> IncomingEvent:
    user = message.from_user
    return IncomingEvent(
        messenger=Messenger.TELEGRAM,
        user_id=str(message.chat.id),
        name=user.full_name if user else "",
        username=(user.username or "") if user else "",
        text=message.text or message.caption,
        photos=photos,
    )


class TelegramAdapter(MessengerAdapter):
    messenger = Messenger.TELEGRAM

    def __init__(self, token: str, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.bot = Bot(token)
        self.dp = Dispatcher()
        router = Router()
        router.message.register(self._on_message)
        router.callback_query.register(self._on_callback)
        self.dp.include_router(router)

    async def send(self, user_id: str, reply: Reply) -> None:
        await self.bot.send_message(chat_id=int(user_id), text=reply.text, reply_markup=keyboard(reply))

    async def poll_updates(self) -> None:
        me = await self.bot.get_me()
        log.info("[telegram] бот @%s, ссылка: https://t.me/%s?start=<код>", me.username, me.username)
        await self.dp.start_polling(self.bot, handle_signals=False)

    async def _download_photos(self, message: Message) -> tuple[IncomingPhoto, ...]:
        file_id, name = None, "photo.jpg"
        if message.photo:
            file_id = message.photo[-1].file_id  # самый крупный размер
        elif message.document and (message.document.mime_type or "").startswith("image/"):
            if (message.document.file_size or 0) <= MAX_PHOTO_BYTES:
                file_id, name = message.document.file_id, message.document.file_name or name
        if not file_id:
            return ()
        buffer = await self.bot.download(file_id)
        return (IncomingPhoto(buffer.read(), name),)

    async def _on_message(self, message: Message) -> None:
        if message.chat.type != ChatType.PRIVATE:
            return  # в группах не работаем
        try:
            photos = await self._download_photos(message)
            await self.process(event_from_message(message, photos))
        except Exception:
            log.exception("[telegram] ошибка обработки сообщения")
            await message.answer("Не получилось обработать сообщение, попробуйте ещё раз.")

    async def _on_callback(self, callback: CallbackQuery) -> None:
        await callback.answer()
        chat_id = callback.message.chat.id if callback.message else callback.from_user.id
        event = IncomingEvent(
            messenger=Messenger.TELEGRAM,
            user_id=str(chat_id),
            name=callback.from_user.full_name,
            username=callback.from_user.username or "",
            callback=callback.data,
        )
        try:
            await self.process(event)
        except Exception:
            log.exception("[telegram] ошибка обработки кнопки")
