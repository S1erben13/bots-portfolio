from datetime import datetime, timezone

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import SendMessage
from aiogram.types import Chat, Message, Update, User

from app.bot import make_dispatcher


class FakeSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.sent = []

    async def close(self):
        pass

    async def make_request(self, bot, method, timeout=None):
        assert isinstance(method, SendMessage)
        self.sent.append(method.text)
        return Message(message_id=100, date=datetime.now(timezone.utc),
                       chat=Chat(id=int(method.chat_id), type="private"), text=method.text)

    async def stream_content(self, url, headers=None, timeout=30, chunk_size=65536, raise_for_status=True):
        yield b""


class FakeLeads:
    def __init__(self):
        self.saved = []

    async def create(self, name, phone):
        self.saved.append((name, phone))
        return {"message": "Заявка сохранена"}


async def test_telegram_lead_and_cancel():
    session, leads = FakeSession(), FakeLeads()
    bot = Bot("123456:TEST", session=session)
    dp = make_dispatcher(None, leads)

    async def send(text, n):
        message = Message(message_id=n, date=datetime.now(timezone.utc),
                          chat=Chat(id=1, type="private"),
                          from_user=User(id=1, is_bot=False, first_name="Тест"), text=text)
        await dp.feed_update(bot, Update(update_id=n, message=message))

    try:
        await send("/lead", 1)
        await send("Анна", 2)
        await send("+79991234567", 3)
        assert leads.saved == [("Анна", "+79991234567")]
        assert session.sent[-1] == "Заявка сохранена"
        await send("/lead", 4)
        await send("/cancel", 5)
        assert "отменён" in session.sent[-1]
    finally:
        await dp.storage.close()
        await bot.session.close()
