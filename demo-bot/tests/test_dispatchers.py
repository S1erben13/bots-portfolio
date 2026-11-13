"""Уведомления и действия диспетчера без сетевых запросов."""

import asyncio
import io
import json
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import httpx
import pytest
from aiogram.types import Chat, Message, User
from django.core.management import call_command
from django.utils import timezone

from bots.adapters.console import ConsoleAdapter
from bots.adapters.max import MaxAdapter, MaxApi, keyboard_attachments
from bots.adapters.telegram import TelegramAdapter, keyboard
from bots.engine import DialogEngine
from bots.outbox import claim_pending, mark_failed, mark_sent
from bots.types import IncomingEvent
from service import services
from service.models import Company, Dispatcher, Notification, Ticket

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def dispatcher(company, django_user_model):
    return Dispatcher.objects.create(company=company, messenger="console", chat_id="99",
        user=django_user_model.objects.create_user(username="dispatcher", first_name="Ольга"))


def create_ticket(equipment, client_obj, **kwargs):
    return services.create_ticket(equipment=equipment, client=client_obj, description="Не работает", **kwargs)


def callback(ticket, target="accepted", messenger="console", user="99"):
    return DialogEngine().handle(IncomingEvent(messenger=messenger, user_id=user,
        callback=f"ds:{ticket.pk}:{target}"))[0]


def test_dispatcher_notification_card_and_buttons(dispatcher, equipment, client_obj, photo, settings):
    settings.PUBLIC_URL = "https://service.example"
    client_obj.phone = "+7 900 123"
    client_obj.save()
    ticket = create_ticket(equipment, client_obj, photos=[services.PhotoUpload(photo.content)])
    note = Notification.objects.get(dispatcher=dispatcher)
    assert note.client_id is None
    assert all(text in note.text for text in (ticket.number, equipment.site.name, equipment.name,
        "Не работает", client_obj.name, client_obj.phone, "Фото: есть",
        f"https://service.example/admin/service/ticket/{ticket.pk}/change/"))
    adapter = ConsoleAdapter(echo=False)
    assert asyncio.run(adapter.deliver_notifications_once()) == 1
    user_id, reply = adapter.sent[0]
    assert user_id == "99" and reply.buttons[0][0].text == "Принять"
    assert reply.buttons[0][0].payload == f"ds:{ticket.pk}:accepted"
    assert keyboard(reply).inline_keyboard[0][0].callback_data == reply.buttons[0][0].payload
    assert keyboard_attachments(reply)[0]["payload"]["buttons"][0][0]["type"] == "callback"


def test_only_active_linked_company_dispatchers_notified(dispatcher, equipment, client_obj):
    Dispatcher.objects.create(company=dispatcher.company, messenger="telegram", chat_id="2", user=dispatcher.user)
    Dispatcher.objects.create(company=dispatcher.company, messenger="max", chat_id="3", user=dispatcher.user)
    Dispatcher.objects.create(company=dispatcher.company, messenger="console", user=dispatcher.user)
    Dispatcher.objects.create(company=dispatcher.company, messenger="console", chat_id="4", is_active=False, user=dispatcher.user)
    Dispatcher.objects.create(company=Company.objects.create(name="Чужие"), messenger="console", chat_id="5", user=dispatcher.user)
    ticket = create_ticket(equipment, client_obj)
    assert ticket.notifications.count() == 3
    assert "Телефон:" not in ticket.notifications.first().text
    assert "Фото: нет" in ticket.notifications.first().text


def test_status_callback_and_replays(dispatcher, equipment, client_obj):
    ticket = create_ticket(equipment, client_obj)
    reply = callback(ticket)
    assert reply.edit and not reply.error and "Ольга" in reply.text and "Принята" in reply.text
    assert {b.text for row in reply.buttons for b in row} == {"Выехал", "Выполнено"}
    assert ticket.history.get(new_status="accepted").changed_by == dispatcher.user
    assert callback(ticket).edit
    assert ticket.history.count() == 2
    assert Notification.objects.filter(client=client_obj).count() == 1
    callback(ticket, "en_route")
    callback(ticket)  # старое нажатие после продвижения
    ticket.refresh_from_db()
    assert ticket.status == "en_route" and ticket.history.count() == 3
    callback(ticket, "done")
    assert callback(ticket, "done").buttons == []
    adapter = ConsoleAdapter(echo=False)
    asyncio.run(adapter.deliver_notifications_once())
    client_messages = [r.text for uid, r in adapter.sent if uid == client_obj.external_id]
    assert len(client_messages) == 3 and "принята" in client_messages[0]
    assert "выехал" in client_messages[1] and "выполнена" in client_messages[2]


def test_two_dispatchers_accept_concurrently(dispatcher, equipment, client_obj):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from django.db import connections

    Dispatcher.objects.create(company=dispatcher.company, messenger="console", chat_id="100", user=dispatcher.user)
    ticket = create_ticket(equipment, client_obj)
    barrier = Barrier(2)
    def accept(user):
        try:
            barrier.wait(timeout=5)
            return callback(ticket, user=user)
        finally:
            connections.close_all()
    with ThreadPoolExecutor(max_workers=2) as pool:
        replies = list(pool.map(accept, ["99", "100"]))
    assert all(reply.edit for reply in replies)
    assert ticket.history.count() == 2
    assert Notification.objects.filter(client=client_obj).count() == 1


def test_bad_photo_rolls_back_ticket_and_dispatcher_queue(dispatcher, equipment, client_obj):
    with pytest.raises(services.ServiceError):
        create_ticket(equipment, client_obj, photos=[services.PhotoUpload(b"not an image")])
    assert Ticket.objects.count() == 0 and Notification.objects.count() == 0


@pytest.mark.parametrize("problem", ["company", "inactive", "user_inactive", "stranger", "messenger", "transition", "unknown", "missing"])
def test_callback_denied_without_side_effects(dispatcher, equipment, client_obj, problem):
    ticket = create_ticket(equipment, client_obj)
    kwargs = {}
    if problem == "company":
        dispatcher.company = Company.objects.create(name="Другая")
    elif problem == "inactive":
        dispatcher.is_active = False
    elif problem == "user_inactive":
        dispatcher.user.is_active = False
        dispatcher.user.save()
    elif problem == "stranger":
        kwargs["user"] = "stranger"
    elif problem == "messenger":
        kwargs["messenger"] = "telegram"
    elif problem == "transition":
        kwargs["target"] = "done"
    elif problem == "unknown":
        kwargs["target"] = "cancelled"
    elif problem == "missing":
        ticket.pk += 100000
    dispatcher.save()
    reply = callback(ticket, **kwargs)
    assert reply.error and reply.callback_answer and not reply.edit
    assert Ticket.objects.get().status == "new"
    assert Ticket.objects.get().history.count() == 1
    assert Notification.objects.filter(client=client_obj).count() == 0


def test_link_code_single_use_and_messenger_scope(dispatcher):
    dispatcher.chat_id = ""
    dispatcher.save()
    code = dispatcher.link_code
    assert len(code) == 8
    assert timedelta(hours=23) < dispatcher.link_code_expires_at - timezone.now() <= timedelta(hours=24)
    engine = DialogEngine()
    def link(messenger="console"):
        return engine.handle(IncomingEvent(messenger=messenger, user_id="123", text=f"/dispatcher {code.lower()}"))[0]
    assert "Код неверен" in link("telegram").text
    assert "привязан" in link().text
    dispatcher.refresh_from_db()
    assert dispatcher.chat_id == "123" and dispatcher.link_code is None
    assert "Код неверен" in link().text


@pytest.mark.parametrize("problem", ["expired", "invalid", "inactive", "user_inactive", "empty"])
def test_bad_link_code(dispatcher, problem):
    code = dispatcher.link_code
    if problem == "expired":
        dispatcher.link_code_expires_at = timezone.now() - timedelta(seconds=1)
    elif problem == "inactive":
        dispatcher.is_active = False
    elif problem == "user_inactive":
        dispatcher.user.is_active = False
        dispatcher.user.save()
    elif problem == "invalid":
        code = "INVALID!"
    else:
        code = ""
    dispatcher.save()
    reply = DialogEngine().handle(IncomingEvent(messenger="console", user_id="123", text=f"/dispatcher {code}"))[0]
    assert "привязан" not in reply.text
    dispatcher.refresh_from_db()
    assert dispatcher.chat_id == "99"


def test_link_cannot_replace_another_dispatcher(dispatcher):
    other = Dispatcher.objects.create(company=dispatcher.company, messenger="console", user=dispatcher.user)
    reply = DialogEngine().handle(IncomingEvent(messenger="console", user_id="99", text=f"/dispatcher {other.link_code}"))[0]
    assert "уже привязан" in reply.text
    other.refresh_from_db()
    assert other.chat_id == "" and other.link_code


def test_dispatcher_outbox_lease_retry_order(dispatcher, equipment, client_obj):
    first = create_ticket(equipment, client_obj)
    second = create_ticket(equipment, client_obj)
    batch = claim_pending("console")
    assert len(batch) == 1 and first.number in batch[0].text
    assert claim_pending("console") == []
    mark_failed(batch[0].id, "offline")
    assert claim_pending("console") == []
    Notification.objects.filter(pk=batch[0].id).update(next_attempt_at=None)
    assert claim_pending("console")[0].id == batch[0].id
    mark_sent(batch[0].id)
    assert second.number in claim_pending("console")[0].text


def test_disabled_dispatcher_not_delivered(dispatcher, equipment, client_obj):
    create_ticket(equipment, client_obj)
    dispatcher.is_active = False
    dispatcher.save()
    assert claim_pending("console") == []


def test_long_card_fits_max_limit(dispatcher, equipment, client_obj):
    ticket = services.create_ticket(equipment=equipment, client=client_obj, description="я" * 4000)
    assert len(ticket.notifications.get().text) <= 4000


def test_max_callback_edits_original_and_denies_stranger(dispatcher, equipment, client_obj):
    dispatcher.messenger = "max"
    dispatcher.save()
    ticket = create_ticket(equipment, client_obj)
    requests = []
    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"success": True})
    api = MaxApi("fake", "https://max.example", http=httpx.AsyncClient(
        base_url="https://max.example", transport=httpx.MockTransport(respond)))
    adapter = MaxAdapter("fake", "unused", api=api)
    async def run():
        for uid in (99, 99, 7):
            await adapter.handle_update({"update_type": "message_callback", "callback": {
                "callback_id": "cb", "payload": f"ds:{ticket.pk}:accepted", "user": {"user_id": uid}}})
        await adapter.handle_update({"update_type": "message_callback", "callback": {
            "callback_id": "done", "payload": f"ds:{ticket.pk}:done", "user": {"user_id": 99}}})
        await api.close()
    asyncio.run(run())
    assert all(r.url.path == "/answers" for r in requests)
    assert "Принята" in json.loads(requests[0].content)["message"]["text"]
    assert "message" not in json.loads(requests[2].content)
    assert json.loads(requests[3].content)["message"]["attachments"] == []
    assert ticket.history.count() == 3
    assert Notification.objects.filter(client=client_obj).count() == 2


def test_telegram_callback_edits_and_answers_errors(dispatcher, equipment, client_obj):
    dispatcher.messenger = "telegram"
    dispatcher.save()
    ticket = create_ticket(equipment, client_obj)
    adapter = TelegramAdapter("123456:ABCDEF")
    adapter.bot = AsyncMock()
    query = AsyncMock()
    query.data = f"ds:{ticket.pk}:accepted"
    query.from_user = User(id=99, is_bot=False, first_name="Диспетчер")
    query.message = Message(message_id=1, date=datetime.now(), chat=Chat(id=99, type="private"))
    asyncio.run(adapter._on_callback(query))
    assert adapter.bot.edit_message_text.await_count == 1
    assert "Принята" in adapter.bot.edit_message_text.call_args.kwargs["text"]
    query.data = f"ds:{ticket.pk}:new"
    asyncio.run(adapter._on_callback(query))
    assert query.answer.call_args.kwargs["show_alert"] is True
    assert adapter.bot.edit_message_text.await_count == 1


def test_seed_and_simulate_demo():
    out = io.StringIO()
    call_command("seed_demo", stdout=out)
    assert "/dispatcher " in out.getvalue()
    call_command("seed_demo", stdout=io.StringIO())
    assert Dispatcher.objects.count() == 1
    call_command("simulate_chat", script=True, stdout=out)
    assert "Диспетчеру:" in out.getvalue() and "нажимает «Принять»" in out.getvalue()
    demo_ticket = Ticket.objects.filter(client__external_id="demo-user-1").get()
    assert demo_ticket.status == "done" and demo_ticket.history.count() == 4


def test_dispatcher_admin_code_and_readonly_chat(client, django_user_model, dispatcher):
    client.force_login(django_user_model.objects.create_superuser("boss", password="x"))
    response = client.get(f"/admin/service/dispatcher/{dispatcher.pk}/change/")
    assert response.status_code == 200
    assert dispatcher.link_code.encode() in response.content
    assert b'name="chat_id"' not in response.content
    previous = dispatcher.link_code
    client.post("/admin/service/dispatcher/", {"action": "renew_codes", "_selected_action": dispatcher.pk})
    dispatcher.refresh_from_db()
    assert dispatcher.link_code != previous
