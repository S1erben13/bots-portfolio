"""Сценарий диалога через движок — так же, как его видят Telegram и MAX."""

from datetime import timedelta

import pytest

from bots.engine import DialogEngine
from bots.models import Conversation
from bots.types import CB_CANCEL, CB_MY_TICKETS, CB_NEW_TICKET, CB_SKIP_PHOTO, IncomingEvent
from service.models import Client, Ticket


@pytest.fixture
def chat(db):
    engine = DialogEngine()

    def send(**kwargs):
        event = IncomingEvent(messenger="telegram", user_id="777", name="Мария", **kwargs)
        return engine.handle(event)

    return send


def texts(replies):
    return "\n".join(r.text for r in replies)


def test_full_flow_via_deep_link_without_photo(chat, equipment):
    replies = chat(text="/start TEST01")
    assert "Камера у входа" in texts(replies)
    assert "ЖК Тестовый" in texts(replies)

    replies = chat(text="Нет изображения")
    assert any(b.payload == CB_SKIP_PHOTO for row in replies[0].buttons for b in row)

    replies = chat(callback=CB_SKIP_PHOTO)
    ticket = Ticket.objects.get()
    assert f"Заявка №{ticket.number} создана" in texts(replies)
    assert ticket.description == "Нет изображения"
    assert ticket.equipment == equipment
    assert ticket.client == Client.objects.get(messenger="telegram", external_id="777")
    assert ticket.client.name == "Мария"
    assert Conversation.objects.get().state == Conversation.State.IDLE


def test_flow_with_photo_and_start_payload_field(chat, equipment, photo):
    chat(start_payload="test01")  # MAX присылает код отдельным полем bot_started.payload
    chat(text="Шлагбаум не открывается")
    replies = chat(text="", photos=(photo,))
    ticket = Ticket.objects.get()
    assert "создана" in texts(replies)
    assert ticket.photos.count() == 1


def test_photo_with_caption_creates_ticket_immediately(chat, equipment, photo):
    chat(text="/start TEST01")
    chat(text="Разбит корпус", photos=(photo,))
    ticket = Ticket.objects.get()
    assert ticket.description == "Разбит корпус"
    assert ticket.photos.count() == 1


def test_extra_photos_attach_to_recent_ticket(chat, equipment, photo):
    chat(text="/start TEST01")
    chat(text="Разбит корпус", photos=(photo,))
    replies = chat(text="", photos=(photo,))  # второе фото из альбома
    ticket = Ticket.objects.get()
    assert ticket.photos.count() == 2
    assert f"№{ticket.number}" in texts(replies)


def test_old_ticket_does_not_receive_photos(chat, equipment, photo):
    chat(text="/start TEST01")
    chat(text="Разбит корпус", photos=(photo,))
    Ticket.objects.update(created_at=Ticket.objects.get().created_at - timedelta(hours=1))
    chat(text="", photos=(photo,))
    assert Ticket.objects.get().photos.count() == 1


def test_unknown_code_then_manual_code(chat, equipment):
    replies = chat(text="/start WRONG1")
    assert "Не нашёл" in texts(replies)
    assert Conversation.objects.get().state == Conversation.State.AWAIT_CODE
    replies = chat(text="test01")
    assert "Камера у входа" in texts(replies)
    assert Conversation.objects.get().state == Conversation.State.AWAIT_DESCRIPTION


def test_code_typed_without_qr(chat, equipment):
    replies = chat(callback=CB_NEW_TICKET)
    assert "код" in texts(replies).lower()
    replies = chat(text="TEST01")
    assert "Опишите" in texts(replies)


def test_plain_start_shows_welcome(chat, equipment):
    replies = chat(text="/start")
    assert "QR" in texts(replies)


def test_cancel_resets_dialog(chat, equipment):
    chat(text="/start TEST01")
    chat(text="Сломано")
    replies = chat(callback=CB_CANCEL)
    assert "не создана" in texts(replies)
    assert Ticket.objects.count() == 0
    assert Conversation.objects.get().state == Conversation.State.IDLE


def test_my_tickets_lists_statuses(chat, equipment):
    assert "нет заявок" in texts(chat(callback=CB_MY_TICKETS))
    chat(text="/start TEST01")
    chat(text="Сломано")
    chat(callback=CB_SKIP_PHOTO)
    replies = chat(text="/my")
    ticket = Ticket.objects.get()
    assert f"№{ticket.number}" in texts(replies)
    assert "Новая" in texts(replies)


def test_equipment_disabled_mid_dialog(chat, equipment):
    chat(text="/start TEST01")
    chat(text="Сломано")
    equipment.is_active = False
    equipment.save()
    replies = chat(callback=CB_SKIP_PHOTO)
    assert Ticket.objects.count() == 0
    assert "QR" in texts(replies)


def test_clients_from_different_messengers_are_separate(equipment):
    engine = DialogEngine()
    engine.handle(IncomingEvent(messenger="telegram", user_id="1", text="/start TEST01"))
    engine.handle(IncomingEvent(messenger="max", user_id="1", text="Привет"))
    assert Client.objects.count() == 2
    states = dict(Conversation.objects.values_list("client__messenger", "state"))
    assert states == {"telegram": "await_description", "max": "idle"}


def test_bad_photo_gives_friendly_error(chat, equipment):
    from bots.types import IncomingPhoto

    chat(text="/start TEST01")
    replies = chat(text="Сломано", photos=(IncomingPhoto(b"<svg><script/></svg>", "x.svg"),))
    assert "фото" in texts(replies).lower()
    assert Ticket.objects.count() == 0
