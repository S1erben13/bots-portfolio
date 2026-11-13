"""Работа с очередью уведомлений (service.models.Notification).

- Захват с арендой: взятое в работу уведомление получает next_attempt_at = now + LEASE,
  поэтому второй воркер (или перезапущенный цикл) не отправит его параллельно.
  Если процесс упал между отправкой и mark_sent, после аренды будет повтор —
  доставка «как минимум один раз».
- Порядок: у клиента/диспетчера не отправляется уведомление, пока не решилась судьба
  более раннего (иначе после сбоя «выполнена» могла бы прийти раньше «принята»).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Exists, F, OuterRef, Q
from django.utils import timezone

from service.models import Notification
from .types import Button

RETRY_BASE_SECONDS = 15
LEASE = timedelta(seconds=60)


@dataclass(frozen=True)
class OutgoingNotification:
    id: int
    user_id: str
    text: str
    buttons: tuple = ()


def claim_pending(messenger: str, limit: int = 20) -> list[OutgoingNotification]:
    now = timezone.now()
    earlier_pending = Notification.objects.filter(
        state=Notification.State.PENDING,
        pk__lt=OuterRef("pk"),
    ).filter(Q(client_id=OuterRef("client_id"), client__isnull=False)
             | Q(dispatcher_id=OuterRef("dispatcher_id"), dispatcher__isnull=False))
    with transaction.atomic():
        items = list(
            Notification.objects.select_for_update(skip_locked=True, of=("self",))
            .select_related("client", "dispatcher")
            .filter(state=Notification.State.PENDING)
            .filter(Q(client__messenger=messenger)
                    | Q(dispatcher__messenger=messenger, dispatcher__is_active=True,
                        dispatcher__user__is_active=True))
            .exclude(dispatcher__chat_id="")
            .filter(Q(next_attempt_at__isnull=True) | Q(next_attempt_at__lte=now))
            .exclude(Exists(earlier_pending))
            .order_by("pk")[:limit]
        )
        if items:
            Notification.objects.filter(pk__in=[n.pk for n in items]).update(
                attempts=F("attempts") + 1, next_attempt_at=now + LEASE
            )
    return [OutgoingNotification(n.pk, n.dispatcher.chat_id if n.dispatcher_id else n.client.external_id,
                                 n.text, tuple(tuple(Button(**b) for b in row) for row in n.buttons))
            for n in items]


def mark_sent(notification_id: int) -> None:
    Notification.objects.filter(pk=notification_id, state=Notification.State.PENDING).update(
        state=Notification.State.SENT, sent_at=timezone.now(), last_error="", next_attempt_at=None
    )


def mark_failed(notification_id: int, error: str) -> None:
    with transaction.atomic():
        item = (
            Notification.objects.select_for_update()
            .filter(pk=notification_id, state=Notification.State.PENDING)
            .first()
        )
        if item is None:  # уже отправлено/закрыто другим воркером
            return
        item.last_error = error[:2000]
        if item.attempts >= settings.NOTIFICATION_MAX_ATTEMPTS:
            item.state = Notification.State.FAILED
            item.next_attempt_at = None
        else:
            delay = RETRY_BASE_SECONDS * 2 ** (item.attempts - 1)
            item.next_attempt_at = timezone.now() + timedelta(seconds=delay)
        item.save(update_fields=["last_error", "state", "next_attempt_at"])
