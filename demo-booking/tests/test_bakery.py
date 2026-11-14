import asyncio
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from threading import Barrier

import pytest
from fastapi.testclient import TestClient

from booking.admin import create_app
from booking.bot import deliver_pending
from booking.config import Settings
from booking.db import connect
from booking.dialog import Dialog
from booking.seed_demo import seed_demo
from booking.service import BookingError, BookingService
from booking.simulate import run, run_bakery_auto


DAY = date(2026, 11, 17)
START = "2026-11-17T10:00"


@pytest.fixture
def bakery(tmp_path):
    config = Settings(mode="bakery", db_path=str(tmp_path / "bakery.sqlite3"),
                      admin_password="test-password", admin_chat_id="999")
    seed_demo(config)
    clock = [datetime(2026, 11, 16, 8)]
    return BookingService(config, clock=lambda: clock[0]), clock


def order(service, starts=START, uid=7):
    return service.book(uid, 1, None, starts, "Анна Демо", "+79990000000")


def set_limit(service, limit):
    with connect(service.config.db_path) as conn:
        conn.execute("UPDATE bakery_settings SET daily_limit=?", (limit,))


def test_seed_and_snapshot(bakery):
    service, _ = bakery
    seed_demo(service.config)
    assert len(service.services()) == 4
    assert service.bakery.settings()["daily_limit"] == 10
    bid = order(service)
    with connect(service.config.db_path) as conn:
        conn.execute("UPDATE products SET name='Другое имя',price=9999,active=0 WHERE id=1")
    row = service.bookings()[0]
    assert (row["id"], row["product_name"], row["product_price"], row["status"]) == (bid, "Пряничный набор", 900, "pending")
    service.reschedule(bid, 7, 0, "2026-11-18T10:00")
    assert service.bookings()[0]["product_price"] == 900
    with pytest.raises(BookingError):
        order(service)


def test_status_notifications_and_delivery(bakery):
    service, clock = bakery
    bid = order(service)
    clock[0] = datetime(2026, 11, 16, 10)
    service.queue_reminders()
    with connect(service.config.db_path) as conn:
        assert conn.execute("SELECT count(*) FROM order_reminders").fetchone()[0] == 0
    service.confirm(bid)
    with pytest.raises(BookingError):
        service.confirm(bid)
    assert service.bookings()[0]["status"] == "confirmed"

    class FakeBot:
        def __init__(self):
            self.sent = []
            self.fail = True

        async def send_message(self, chat, body):
            if self.fail:
                raise ConnectionError("offline")
            self.sent.append((chat, body))

    bot = FakeBot()
    asyncio.run(deliver_pending(bot, service))
    assert bot.sent == []
    bot.fail = False
    asyncio.run(deliver_pending(bot, service))
    client = [body for chat, body in bot.sent if chat == "7"]
    assert any("Подтверждён" in body for body in client)
    assert any("Напоминание" in body and "Пряничный набор" in body and "2026-11-17 10:00" in body
               and service.bakery.settings()["pickup_address"] in body for body in client)
    count = len(bot.sent)
    asyncio.run(deliver_pending(bot, service))
    assert len(bot.sent) == count
    service.cancel(bid)
    asyncio.run(deliver_pending(bot, service))
    assert any(chat == "7" and "Отменён" in body for chat, body in bot.sent)
    with pytest.raises(BookingError):
        service.confirm(bid)


def test_limit_pending_confirmed_and_cancel(bakery):
    service, _ = bakery
    set_limit(service, 2)
    first, second = order(service), order(service, uid=8)
    service.confirm(first)
    assert service.bakery.full(DAY)
    assert service.slots(1, None, DAY) == []
    with pytest.raises(BookingError):
        order(service, starts="2026-11-17T11:00")
    service.cancel(second, 8)
    assert not service.bakery.full(DAY)
    assert order(service)
    assert len(service.bookings()) == 3


def test_parallel_last_place(bakery):
    service, _ = bakery
    set_limit(service, 2)
    order(service)
    barrier = Barrier(2)

    def attempt(uid):
        barrier.wait()
        try:
            return order(service, uid=uid)
        except BookingError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, (8, 9)))
    assert sum(bid is not None for bid in results) == 1
    assert len(service.bookings()) == 2


def test_move_full_date_atomic_same_day_and_reminders(bakery):
    service, clock = bakery
    set_limit(service, 1)
    first = order(service)
    other = order(service, "2026-11-18T10:00", uid=8)
    service.confirm(first)
    clock[0] = datetime(2026, 11, 16, 10)
    service.queue_reminders()
    with pytest.raises(BookingError):
        service.reschedule(first, 7, 0, "2026-11-18T11:00")
    assert service.bookings(user_id=7)[0]["starts"] == START
    with connect(service.config.db_path) as conn:
        assert conn.execute("SELECT count(*) FROM order_reminders").fetchone()[0] == 1
    service.reschedule(first, 7, 0, "2026-11-17T11:00")
    assert service.bookings(user_id=7)[0]["status"] == "confirmed"
    with connect(service.config.db_path) as conn:
        assert conn.execute("SELECT count(*) FROM order_reminders").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM outbox WHERE body LIKE 'Напоминание%'").fetchone()[0] == 0
    service.cancel(other)
    service.reschedule(first, 7, 0, "2026-11-18T10:00")
    assert not service.bakery.full(DAY)
    clock[0] = datetime(2026, 11, 18, 8)
    service.queue_reminders()
    with connect(service.config.db_path) as conn:
        reminders = conn.execute("SELECT * FROM order_reminders").fetchall()
        assert len(reminders) == 1 and reminders[0]["hours"] == 2
    service.cancel(first)
    with connect(service.config.db_path) as conn:
        assert conn.execute("SELECT count(*) FROM order_reminders").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM outbox WHERE body LIKE 'Напоминание%'").fetchone()[0] == 0


def test_ownership_deadline_and_invalid_data(bakery):
    service, clock = bakery
    for name, phone in (("А", "+79990000000"), ("Анна", "wrong")):
        with pytest.raises(BookingError):
            service.book(7, 1, 0, START, name, phone)
    bid = order(service)
    with pytest.raises(BookingError):
        service.cancel(bid, 8)
    with pytest.raises(BookingError):
        service.reschedule(bid, 8, 0, "2026-11-18T10:00")
    clock[0] = datetime(2026, 11, 17, 7, 1)
    with pytest.raises(BookingError):
        service.cancel(bid, 7)
    clock[0] = datetime(2026, 11, 17, 7)
    service.cancel(bid, 7)


def test_dialog_calendar_and_console(bakery):
    service, _ = bakery
    answers = iter(["1", str(DAY), "1", "Анна Демо", "+79990000000", "да"])
    output = []
    bid = run(service, lambda prompt: next(answers), output.append)
    assert bid and "ожидает подтверждения" in output[-1]
    assert any(service.bakery.settings()["pickup_address"] in text for text in output)
    set_limit(service, 1)
    dialog = Dialog(service, 8)
    dialog.choose_service(1)
    assert dialog.step == "date"
    with pytest.raises(BookingError, match="лимит"):
        dialog.choose_date(str(DAY))
    assert dialog.step == "date"
    move = Dialog(service, 10001)
    move.move(service.bookings()[0])
    slot = move.choose_date(str(DAY))[0]
    move.choose_slot(0, slot["starts"])
    assert "900 ₽" in move.summary()
    assert move.confirm() == bid


def test_admin_products_settings_statuses(bakery):
    service, _ = bakery
    client = TestClient(create_app(service))
    assert client.get("/admin").status_code == 401
    client.auth = ("admin", "test-password")
    page = client.get("/admin/catalog")
    csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
    assert client.post("/admin/product", data={"name": "Торт"}).status_code == 403

    def post(action, **values):
        return client.post(f"/admin/{action}", data={"csrf": csrf, **values})

    assert post("product", name="Тестовый торт", description="<script>demo</script>", price=1500).status_code == 200
    pid = service.services()[-1]["id"]
    assert "&lt;script&gt;" in client.get("/admin/catalog").text
    assert post("product", id=pid, name="Торт новый", description="Описание", price=2000, active=0).status_code == 200
    assert pid not in [p["id"] for p in service.services()]
    assert post("product", name="Торт", price=-1).status_code == 400
    assert post("bakery_settings", title="Сахарный декабрь", pickup_address="Демо-адрес", daily_limit=0).status_code == 400
    assert post("bakery_settings", title="Сахарный декабрь", pickup_address="Новый адрес", daily_limit=1).status_code == 200
    bid = order(service)
    assert "Ожидает подтверждения" in client.get(f"/admin?day={DAY}").text
    assert post("booking", id=bid, status="confirmed").status_code == 200
    assert service.bookings()[0]["status"] == "confirmed"
    assert post("bakery_settings", title="Сахарный декабрь", pickup_address="Новый адрес", daily_limit=2).status_code == 200
    order(service, uid=8)
    assert post("bakery_settings", title="Сахарный декабрь", pickup_address="Новый адрес", daily_limit=1).status_code == 400
    assert post("booking", id=bid, status="attended").status_code == 400
    assert post("booking", id=bid, status="cancelled").status_code == 200
    assert not service.bakery.full(DAY)
    assert service.bakery.settings()["pickup_address"] == "Новый адрес"


def test_auto_scenario(bakery):
    service, _ = bakery
    set_limit(service, 2)
    output = []
    assert run_bakery_auto(service, output.append)
    assert any("сверх лимита отклонён" in text for text in output)
    assert any("Отмена освободила" in text for text in output)
    assert any("Напоминание (2 ч)" in text for text in output)


def test_expired_reminder_and_salon_database_coexist(bakery):
    service, clock = bakery
    bid = order(service)
    service.confirm(bid)
    clock[0] = datetime(2026, 11, 16, 10)
    service.queue_reminders()
    clock[0] = datetime(2026, 11, 17, 11)
    service.queue_reminders()
    with connect(service.config.db_path) as conn:
        assert conn.execute("SELECT count(*) FROM outbox WHERE sent=0 AND body LIKE 'Напоминание%'").fetchone()[0] == 0
    salon_config = Settings(mode="salon", db_path=service.config.db_path)
    seed_demo(salon_config)
    salon = BookingService(salon_config, clock=lambda: datetime(2026, 11, 16, 8))
    salon.book(10, 1, 1, START, "Иван", "+79990000000")
    assert len(salon.bookings()) == 1 and len(service.bookings()) == 1
    assert service.bookings()[0]["product_name"] == "Пряничный набор"


def test_telegram_flow_and_full_calendar_without_network(bakery, monkeypatch):
    from aiogram import Bot
    from aiogram.methods import SendMessage
    from aiogram.types import CallbackQuery, Chat, Message, Update, User
    from booking.bot import create_dispatcher

    service, _ = bakery
    set_limit(service, 1)
    order(service, uid=8)
    sent = []

    async def fake_call(self, method, **kwargs):
        if isinstance(method, SendMessage):
            sent.append(method)
        return True

    monkeypatch.setattr(Bot, "__call__", fake_call)

    async def scenario():
        bot = Bot("123456:TEST_TOKEN_FOR_OFFLINE_TESTS")
        dp = create_dispatcher(service)
        user = User(id=7, is_bot=False, first_name="Анна")
        chat = Chat(id=7, type="private")
        sequence = 0

        async def callback(data):
            nonlocal sequence
            sequence += 1
            message = Message(message_id=sequence, date=datetime(2026, 11, 16, 8), chat=chat)
            query = CallbackQuery(id=str(sequence), from_user=user, chat_instance="offline", message=message, data=data)
            await dp.feed_update(bot, Update(update_id=sequence, callback_query=query))

        async def text(value):
            nonlocal sequence
            sequence += 1
            message = Message(message_id=sequence, date=datetime(2026, 11, 16, 8), chat=chat, from_user=user, text=value)
            await dp.feed_update(bot, Update(update_id=sequence, message=message))

        try:
            await callback("new")
            assert sent[-1].text == "Выберите товар"
            await callback("service:1")
            calendar = sent[-1].reply_markup.inline_keyboard
            full_buttons = [button for row in calendar for button in row if "заполнено" in button.text]
            assert len(full_buttons) == 1 and full_buttons[0].callback_data == "full:"
            await callback("full:")
            assert "лимит" in sent[-1].text
            await callback("date:2026-11-18")
            await callback("slot:0|2026-11-18T10:00")
            await text("Анна Демо")
            await text("+79990000000")
            assert "Самовывоз:" in sent[-1].text and "900 ₽" in sent[-1].text
            await callback("confirm:")
            assert "ожидает подтверждения" in sent[-1].text
            await callback("mine")
            assert "ожидает подтверждения" in sent[-1].text and "Пряничный набор" in sent[-1].text
        finally:
            await dp.storage.close()
            await bot.session.close()

    asyncio.run(scenario())
    assert len(service.bookings(user_id=7)) == 1
