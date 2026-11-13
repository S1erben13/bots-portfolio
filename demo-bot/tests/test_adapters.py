"""Адаптеры MAX и Telegram без сети: HTTP подменяется httpx.MockTransport."""

import asyncio
import json
from datetime import datetime

import httpx
import pytest
from aiogram.types import Chat, Message, User

from bots.adapters import max as max_adapter
from bots.adapters.telegram import event_from_message, keyboard
from bots.types import CB_SKIP_PHOTO, Button, Reply
from service.models import Ticket

TOKEN = "secret-token"


def _jpeg() -> bytes:
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (8, 8), "blue").save(buf, format="JPEG")
    return buf.getvalue()


JPEG = _jpeg()


# --- MAX: разбор событий ---


def test_parse_bot_started_with_deep_link():
    parsed = max_adapter.parse_update(
        {
            "update_type": "bot_started",
            "timestamp": 1,
            "chat_id": 555,
            "user": {"user_id": 42, "first_name": "Борис", "username": "boris", "is_bot": False},
            "payload": "NP2CAM",
        }
    )
    assert parsed.event.messenger == "max"
    assert parsed.event.user_id == "42"
    assert parsed.event.start_payload == "NP2CAM"
    assert parsed.event.name == "Борис"


def test_parse_message_with_image():
    parsed = max_adapter.parse_update(
        {
            "update_type": "message_created",
            "timestamp": 1,
            "message": {
                "sender": {"user_id": 42, "first_name": "Борис", "is_bot": False},
                "recipient": {"chat_id": 555, "chat_type": "dialog"},
                "timestamp": 1,
                "body": {
                    "mid": "m1",
                    "seq": 1,
                    "text": "Сломался",
                    "attachments": [
                        {"type": "image", "payload": {"photo_id": 1, "token": "t", "url": "https://cdn/x.jpg"}},
                        {"type": "sticker", "payload": {"url": "https://cdn/s.webp"}},
                    ],
                },
            },
        }
    )
    assert parsed.event.text == "Сломался"
    assert parsed.photo_urls == ("https://cdn/x.jpg",)


def test_parse_ignores_groups_bots_and_unknown():
    group_msg = {
        "update_type": "message_created",
        "message": {
            "sender": {"user_id": 1, "is_bot": False},
            "recipient": {"chat_id": 9, "chat_type": "chat"},
            "body": {"text": "hi"},
        },
    }
    bot_msg = {
        "update_type": "message_created",
        "message": {"sender": {"user_id": 2, "is_bot": True}, "recipient": {"chat_type": "dialog"}, "body": {}},
    }
    assert max_adapter.parse_update(group_msg) is None
    assert max_adapter.parse_update(bot_msg) is None
    assert max_adapter.parse_update({"update_type": "dialog_muted"}) is None


def test_parse_callback():
    parsed = max_adapter.parse_update(
        {
            "update_type": "message_callback",
            "callback": {"timestamp": 1, "callback_id": "cb1", "payload": CB_SKIP_PHOTO, "user": {"user_id": 7}},
        }
    )
    assert parsed.event.callback == CB_SKIP_PHOTO
    assert parsed.callback_id == "cb1"


def test_keyboard_attachment_format():
    reply = Reply("x", [[Button("Без фото", "skip_photo")]])
    assert max_adapter.keyboard_attachments(reply) == [
        {
            "type": "inline_keyboard",
            "payload": {"buttons": [[{"type": "callback", "text": "Без фото", "payload": "skip_photo"}]]},
        }
    ]
    assert max_adapter.keyboard_attachments(Reply("x")) == []


# --- MAX: HTTP-уровень ---


class FakeMax:
    """Имитация platform-api2.max.ru + CDN с фото."""

    def __init__(self):
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host == "cdn.example":
            return httpx.Response(200, content=JPEG)
        if request.url.path == "/messages":
            return httpx.Response(200, json={"message": {"body": {"mid": "1", "seq": 1}}})
        if request.url.path == "/answers":
            return httpx.Response(200, json={"success": True})
        if request.url.path == "/me":
            return httpx.Response(401, json={"code": "verify.token", "message": "Invalid access_token"})
        return httpx.Response(404, json={"code": "not.found", "message": "nope"})

    def api(self) -> max_adapter.MaxApi:
        transport = httpx.MockTransport(self.handler)
        api = max_adapter.MaxApi(TOKEN, "https://platform-api2.max.ru")
        api.http = httpx.AsyncClient(
            base_url="https://platform-api2.max.ru", headers={"Authorization": TOKEN}, transport=transport
        )
        api.files = httpx.AsyncClient(transport=transport)
        return api

    def sent_messages(self):
        return [r for r in self.requests if r.url.path == "/messages"]


@pytest.mark.django_db(transaction=True)
def test_max_deep_link_dialog_end_to_end(equipment):
    fake = FakeMax()
    adapter = max_adapter.MaxAdapter(TOKEN, "unused", api=fake.api())

    def run(update):
        asyncio.run(adapter.handle_update(update))

    user = {"user_id": 42, "first_name": "Борис", "is_bot": False}
    run({"update_type": "bot_started", "timestamp": 1, "chat_id": 1, "user": user, "payload": "TEST01"})
    run(
        {
            "update_type": "message_created",
            "timestamp": 2,
            "message": {
                "sender": user,
                "recipient": {"chat_type": "dialog", "chat_id": 1},
                "timestamp": 2,
                "body": {"mid": "a", "seq": 1, "text": "Не работает"},
            },
        }
    )
    run(
        {
            "update_type": "message_created",
            "timestamp": 3,
            "message": {
                "sender": user,
                "recipient": {"chat_type": "dialog", "chat_id": 1},
                "timestamp": 3,
                "body": {
                    "mid": "b",
                    "seq": 2,
                    "text": None,
                    "attachments": [{"type": "image", "payload": {"photo_id": 1, "token": "t", "url": "https://cdn.example/p.jpg"}}],
                },
            },
        }
    )

    ticket = Ticket.objects.get()
    assert ticket.client.messenger == "max" and ticket.client.external_id == "42"
    assert ticket.description == "Не работает"
    assert ticket.photos.count() == 1

    sent = fake.sent_messages()
    assert len(sent) == 3
    assert all(r.url.params["user_id"] == "42" for r in sent)
    assert all(r.headers["Authorization"] == TOKEN for r in sent)
    assert "создана" in json.loads(sent[-1].content)["text"]

    cdn = [r for r in fake.requests if r.url.host == "cdn.example"]
    assert len(cdn) == 1 and "Authorization" not in cdn[0].headers  # токен не утекает на CDN


@pytest.mark.django_db(transaction=True)
def test_max_callback_is_answered(equipment):
    fake = FakeMax()
    adapter = max_adapter.MaxAdapter(TOKEN, "unused", api=fake.api())
    asyncio.run(
        adapter.handle_update(
            {
                "update_type": "message_callback",
                "timestamp": 1,
                "callback": {"timestamp": 1, "callback_id": "cb-9", "payload": "my_tickets", "user": {"user_id": 5}},
            }
        )
    )
    answers = [r for r in fake.requests if r.url.path == "/answers"]
    assert answers and answers[0].url.params["callback_id"] == "cb-9"
    assert "нет заявок" in json.loads(fake.sent_messages()[0].content)["text"]


def test_max_api_error_raised():
    fake = FakeMax()
    with pytest.raises(max_adapter.MaxApiError) as err:
        asyncio.run(fake.api().get_me())
    assert err.value.status == 401 and err.value.code == "verify.token"


def test_max_success_false_is_error():
    def handler(request):
        return httpx.Response(200, json={"success": False, "message": "bad"})

    api = max_adapter.MaxApi(TOKEN, "https://x")
    api.http = httpx.AsyncClient(base_url="https://x", transport=httpx.MockTransport(handler))
    with pytest.raises(max_adapter.MaxApiError):
        asyncio.run(api.answer_callback("1", "ok"))


# --- Telegram ---


def tg_message(text, chat_type="private"):
    return Message(
        message_id=1,
        date=datetime.now(),
        chat=Chat(id=100, type=chat_type),
        from_user=User(id=100, is_bot=False, first_name="Анна", last_name="К", username="anna"),
        text=text,
    )


def test_telegram_event_from_message():
    event = event_from_message(tg_message("/start NP2CAM"))
    assert event.messenger == "telegram"
    assert (event.user_id, event.name, event.username) == ("100", "Анна К", "anna")
    assert event.text == "/start NP2CAM"


def test_telegram_keyboard():
    markup = keyboard(Reply("x", [[Button("A", "a"), Button("B", "b")]]))
    assert [[b.callback_data for b in row] for row in markup.inline_keyboard] == [["a", "b"]]
    assert keyboard(Reply("x")) is None
