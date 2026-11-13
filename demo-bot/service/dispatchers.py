"""Привязка диспетчеров, карточки заявок и защищённые переходы из бота."""

from django.conf import settings
from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import timezone

from .models import Dispatcher, Notification, Ticket
from .services import ServiceError, change_status, normalize_code


# В боте статусы движутся только вперёд; админка сохраняет прежние возможности.
TRANSITIONS = {
    Ticket.Status.NEW: (Ticket.Status.ACCEPTED,),
    Ticket.Status.ACCEPTED: (Ticket.Status.EN_ROUTE, Ticket.Status.DONE),
    Ticket.Status.EN_ROUTE: (Ticket.Status.DONE,),
}
LABELS = {Ticket.Status.ACCEPTED: "Принять", Ticket.Status.EN_ROUTE: "Выехал",
          Ticket.Status.DONE: "Выполнено"}


def ticket_buttons(ticket):
    return [[{"text": LABELS[target], "payload": f"ds:{ticket.pk}:{target}"}]
            for target in TRANSITIONS.get(ticket.status, ())]


def ticket_card(ticket):
    client = ticket.client
    client_name = (client.name or client.username or client.external_id) if client else "—"
    lines = [f"Заявка №{ticket.number}", f"Объект: {ticket.site.name}",
             f"Оборудование: {ticket.equipment.name if ticket.equipment else '—'}",
             # Карточка укладывается в лимит 4000 символов MAX, включая длинные имена.
             f"Описание: {ticket.description[:2200]}",
             f"Клиент: {client_name}"]
    if client and client.phone:
        lines.append(f"Телефон: {client.phone}")
    lines += [f"Фото: {'есть' if ticket.photos.exists() else 'нет'}",
              f"Статус: {ticket.get_status_display()}"]
    latest = ticket.history.select_related("changed_by").order_by("-pk").first()
    if latest and latest.changed_by:
        lines.append(f"Кто изменил: {latest.changed_by.get_full_name() or latest.changed_by.get_username()}")
    url = settings.PUBLIC_URL + reverse("admin:service_ticket_change", args=[ticket.pk])
    lines.append(url)
    return "\n".join(lines)


def notify_dispatchers(ticket):
    text, buttons = ticket_card(ticket), ticket_buttons(ticket)
    for dispatcher in ticket.company.dispatchers.filter(is_active=True, user__is_active=True).exclude(chat_id=""):
        Notification.objects.create(dispatcher=dispatcher, ticket=ticket, text=text, buttons=buttons)


@transaction.atomic
def link_dispatcher(messenger, chat_id, code):
    dispatcher = (Dispatcher.objects.select_for_update().filter(
        messenger=messenger, link_code=normalize_code(code), is_active=True,
        user__is_active=True, link_code_expires_at__gt=timezone.now()).first())
    if dispatcher is None:
        raise ServiceError("Код неверен, уже использован или истёк")
    if not chat_id or len(str(chat_id)) > 64:
        raise ServiceError("Неверный диалог")
    if Dispatcher.objects.filter(messenger=messenger, chat_id=str(chat_id)).exclude(pk=dispatcher.pk).exists():
        raise ServiceError("Диалог уже привязан к диспетчеру")
    dispatcher.chat_id = str(chat_id)
    dispatcher.link_code = None
    try:
        with transaction.atomic():
            dispatcher.save(update_fields=["chat_id", "link_code"])
    except IntegrityError as exc:
        raise ServiceError("Диалог уже привязан к диспетчеру") from exc
    return dispatcher


@transaction.atomic
def dispatcher_callback(messenger, chat_id, payload):
    dispatcher = (Dispatcher.objects.select_for_update(of=("self",)).select_related("user")
                  .filter(messenger=messenger, chat_id=str(chat_id), is_active=True,
                          user__is_active=True).first())
    if dispatcher is None:
        raise ServiceError("Доступ диспетчера запрещён")
    try:
        prefix, ticket_id, target = payload.split(":")
        if prefix != "ds" or not ticket_id.isascii() or not ticket_id.isdigit() or len(ticket_id) > 18:
            raise ValueError
    except ValueError as exc:
        raise ServiceError("Некорректная кнопка") from exc
    ticket = (Ticket.objects.select_for_update(of=("self",)).select_related("site", "equipment", "client")
              .filter(pk=int(ticket_id), company=dispatcher.company).first())
    if ticket is None:
        raise ServiceError("Заявка недоступна")
    if target not in LABELS:
        raise ServiceError("Недопустимый переход статуса")
    # Проверка истории под блокировкой заявки: повтор старой кнопки после дальнейшего
    # продвижения тоже не создаёт историю/уведомление и не откатывает статус.
    if not ticket.history.filter(new_status=target).exists():
        if target not in TRANSITIONS.get(ticket.status, ()):
            raise ServiceError("Недопустимый переход статуса")
        change_status(ticket, target, user=dispatcher.user, comment="")
    return ticket
