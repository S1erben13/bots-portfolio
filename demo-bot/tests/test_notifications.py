"""Смена статуса → уведомление уходит клиенту через адаптер его мессенджера."""

import asyncio

import pytest

from bots.adapters.console import ConsoleAdapter
from bots.types import Reply
from service import services
from service.models import Client, Messenger, Notification, Ticket

pytestmark = pytest.mark.django_db(transaction=True)  # адаптеры ходят в БД из другого потока


class BrokenAdapter(ConsoleAdapter):
    async def send(self, user_id: str, reply: Reply) -> None:
        raise ConnectionError("network down")


def make_ticket(equipment, messenger=Messenger.CONSOLE, user="u1"):
    client = Client.objects.create(messenger=messenger, external_id=user)
    return services.create_ticket(equipment=equipment, client=client, description="x")


def test_status_change_is_delivered_to_client(equipment):
    ticket = make_ticket(equipment)
    services.change_status(ticket, Ticket.Status.ACCEPTED)

    adapter = ConsoleAdapter(echo=False)
    assert asyncio.run(adapter.deliver_notifications_once()) == 1

    assert adapter.sent == [("u1", Reply(f"Заявка №{ticket.number}: принята в работу"))]
    note = Notification.objects.get()
    assert note.state == Notification.State.SENT
    assert note.sent_at is not None
    # повторно не отправляется
    assert asyncio.run(adapter.deliver_notifications_once()) == 0


def test_full_status_chain_notifies_each_step(equipment):
    ticket = make_ticket(equipment)
    adapter = ConsoleAdapter(echo=False)
    for status in (Ticket.Status.ACCEPTED, Ticket.Status.EN_ROUTE, Ticket.Status.DONE):
        services.change_status(ticket, status)
        asyncio.run(adapter.deliver_notifications_once())
    sent = [reply.text for _, reply in adapter.sent]
    assert len(sent) == 3
    assert "принята" in sent[0] and "выехал" in sent[1] and "выполнена" in sent[2]


def test_adapter_only_takes_own_messenger(equipment):
    ticket = make_ticket(equipment, messenger=Messenger.TELEGRAM, user="tg1")
    services.change_status(ticket, Ticket.Status.ACCEPTED)
    adapter = ConsoleAdapter(echo=False)
    assert asyncio.run(adapter.deliver_notifications_once()) == 0
    assert Notification.objects.get().state == Notification.State.PENDING


def test_failed_delivery_is_retried_then_marked_failed(equipment, settings):
    settings.NOTIFICATION_MAX_ATTEMPTS = 2
    ticket = make_ticket(equipment)
    services.change_status(ticket, Ticket.Status.ACCEPTED)
    adapter = BrokenAdapter(echo=False)

    asyncio.run(adapter.deliver_notifications_once())
    note = Notification.objects.get()
    assert (note.state, note.attempts) == (Notification.State.PENDING, 1)
    assert "network down" in note.last_error
    assert note.next_attempt_at is not None

    # до next_attempt_at повторной попытки нет
    assert asyncio.run(adapter.deliver_notifications_once()) == 0

    Notification.objects.update(next_attempt_at=None)
    asyncio.run(adapter.deliver_notifications_once())
    note.refresh_from_db()
    assert (note.state, note.attempts) == (Notification.State.FAILED, 2)


def test_adapter_process_sends_engine_replies(equipment):
    adapter = ConsoleAdapter(echo=False)
    from bots.types import IncomingEvent

    asyncio.run(
        adapter.process(IncomingEvent(messenger=Messenger.CONSOLE, user_id="u9", text="/start TEST01"))
    )
    assert adapter.sent and adapter.sent[0][0] == "u9"
    assert "Камера у входа" in adapter.sent[0][1].text


def test_claimed_notification_is_leased(equipment):
    """Пока первое взятие не завершено, второй воркер то же уведомление не получит."""
    from bots.outbox import claim_pending

    ticket = make_ticket(equipment)
    services.change_status(ticket, Ticket.Status.ACCEPTED)
    assert len(claim_pending(Messenger.CONSOLE)) == 1
    assert claim_pending(Messenger.CONSOLE) == []


def test_notifications_keep_order_after_failure(equipment):
    ticket = make_ticket(equipment)
    services.change_status(ticket, Ticket.Status.ACCEPTED)
    asyncio.run(BrokenAdapter(echo=False).deliver_notifications_once())  # первое ждёт повтора
    services.change_status(ticket, Ticket.Status.DONE)

    adapter = ConsoleAdapter(echo=False)
    assert asyncio.run(adapter.deliver_notifications_once()) == 0  # «выполнена» не обгоняет

    Notification.objects.update(next_attempt_at=None)
    asyncio.run(adapter.deliver_notifications_once())
    asyncio.run(adapter.deliver_notifications_once())
    sent = [reply.text for _, reply in adapter.sent]
    assert len(sent) == 2 and "принята" in sent[0] and "выполнена" in sent[1]


def test_mark_failed_does_not_override_sent(equipment):
    from bots.outbox import mark_failed, mark_sent

    ticket = make_ticket(equipment)
    services.change_status(ticket, Ticket.Status.ACCEPTED)
    note = Notification.objects.get()
    mark_sent(note.pk)
    mark_failed(note.pk, "late error")
    note.refresh_from_db()
    assert note.state == Notification.State.SENT and note.last_error == ""


def test_adapter_crash_cancels_outbox_loop():
    """Если приём событий упал, цикл очереди не должен остаться висеть (иначе дубли при рестарте)."""

    class Crashing(ConsoleAdapter):
        outbox_cancelled = False

        async def poll_updates(self):
            await asyncio.sleep(0.05)
            raise RuntimeError("boom")

        async def run_outbox(self):
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                Crashing.outbox_cancelled = True
                raise

    with pytest.raises(ExceptionGroup):
        asyncio.run(Crashing(echo=False).run())
    assert Crashing.outbox_cancelled


def test_broken_telegram_token_does_not_disable_max(settings):
    from bots.management.commands.run_bots import build_adapters

    settings.TELEGRAM_BOT_TOKEN = "not-a-token"
    settings.MAX_BOT_TOKEN = "max-token"
    names = [a.messenger for a in build_adapters()]
    assert names == ["console", "max"]
