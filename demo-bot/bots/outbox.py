"""Работа с очередью уведомлений (service.models.Notification).

- Захват с арендой: взятое в работу уведомление получает next_attempt_at = now + LEASE,
  поэтому второй воркер (или перезапущенный цикл) не отправит его параллельно.
  Если процесс упал между отправкой и mark_sent, после аренды будет повтор —
  доставка «как минимум один раз».
- Порядок: у клиента не отправляется уведомление, пока не решилась судьба
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

RETRY_BASE_SECONDS = 15
LEASE = timedelta(seconds=60)


@dataclass(frozen=True)
class OutgoingNotification:
    id: int
    user_id: str
    text: str


def claim_pending(messenger: str, limit: int = 20) -> list[OutgoingNotification]:
    now = timezone.now()
    earlier_pending = Notification.objects.filter(
        client_id=OuterRef("client_id"),
        state=Notification.State.PENDING,
        pk__lt=OuterRef("pk"),
    )
    with transaction.atomic():
        items = list(
            Notification.objects.select_for_update(skip_locked=True, of=("self",))
            .select_related("client")
            .filter(state=Notification.State.PENDING, client__messenger=messenger)
            .filter(Q(next_attempt_at__isnull=True) | Q(next_attempt_at__lte=now))
            .exclude(Exists(earlier_pending))
            .order_by("pk")[:limit]
        )
        if items:
            Notification.objects.filter(pk__in=[n.pk for n in items]).update(
                attempts=F("attempts") + 1, next_attempt_at=now + LEASE
            )
    return [OutgoingNotification(n.pk, n.client.external_id, n.text) for n in items]


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
