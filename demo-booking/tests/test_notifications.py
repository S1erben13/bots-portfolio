import asyncio
from datetime import datetime

from booking.bot import deliver_pending
from booking.db import connect


class FakeBot:
    def __init__(self):
        self.sent = []
        self.fail = False

    async def send_message(self, chat_id, body):
        if self.fail:
            raise ConnectionError("Имитируем недоступность сети")
        self.sent.append((chat_id, body))


def test_notification_retry_and_reminders(system):
    service, clock = system
    service.book(7, 1, 1, "2026-11-17T10:00", "Иван", "+79991234567")
    bot = FakeBot()
    bot.fail = True
    asyncio.run(deliver_pending(bot, service))
    assert not bot.sent
    bot.fail = False
    clock[0] = datetime(2026, 11, 16, 10)
    asyncio.run(deliver_pending(bot, service))
    assert len(bot.sent) == 2  # Администратору + клиенту за 24 ч.
    assert bot.sent[0][0] == "999"
    assert bot.sent[1][0] == "7"
    asyncio.run(deliver_pending(bot, service))
    assert len(bot.sent) == 2
    clock[0] = datetime(2026, 11, 17, 8)
    asyncio.run(deliver_pending(bot, service))
    assert len(bot.sent) == 3
    assert "(2 ч)" in bot.sent[-1][1]


def test_expired_reminders_not_delivered(system):
    service, clock = system
    service.book(7, 1, 1, "2026-11-17T10:00", "Иван", "+79991234567")
    clock[0] = datetime(2026, 11, 16, 10)
    service.queue_reminders()
    clock[0] = datetime(2026, 11, 17, 11)
    bot = FakeBot()
    asyncio.run(deliver_pending(bot, service))
    assert all("Напоминание" not in body for _, body in bot.sent)


def test_short_notice_gets_only_two_hour_reminder(system):
    service, clock = system
    clock[0] = datetime(2026, 11, 17, 9)
    service.book(7, 1, 1, "2026-11-17T10:00", "Иван", "+79991234567")
    service.queue_reminders()
    with connect(service.config.db_path) as conn:
        rows = conn.execute("SELECT hours FROM reminders").fetchall()
        assert [row[0] for row in rows] == [2]
