import argparse
from dataclasses import replace
from datetime import datetime, timedelta

from booking.config import settings
from booking.dialog import Dialog
from booking.db import connect
from booking.seed_demo import seed_demo
from booking.service import BookingError, BookingService


def run(service, ask=input, say=print):
    dialog = Dialog(service, 10001)
    for row in service.services():
        if service.bakery:
            say(f"{row['id']}: {row['name']} — {row['price']} ₽. {row['description']}")
        else:
            say(f"{row['id']}: {row['name']} ({row['duration']} мин.)")
    dialog.choose_service(int(ask("Номер товара: " if service.bakery else "Номер услуги: ")))
    if not service.bakery:
        say("0: Любой мастер")
        for row in service.masters():
            say(f"{row['id']}: {row['name']}")
        dialog.choose_master(int(ask("Номер мастера: ")) or None)
    say("Даты: " + ", ".join(str(day) + (" (заполнено)" if service.bakery and service.bakery.full(day) else "")
                              for day in service.dates()))
    slots = dialog.choose_date(ask("Дата ГГГГ-ММ-ДД: "))
    if not slots:
        raise BookingError("На эту дату свободных слотов нет.")
    for index, slot in enumerate(slots, 1):
        say(f"{index}: {slot['starts']} — {slot['master']}")
    index = int(ask("Номер слота: "))
    if not 1 <= index <= len(slots):
        raise BookingError("Неверный номер слота.")
    slot = slots[index - 1]
    dialog.choose_slot(slot["master_id"], slot["starts"])
    dialog.set_name(ask("Ваше имя: "))
    dialog.set_phone(ask("Телефон: "))
    say(dialog.summary())
    if ask("Подтвердить (да): ").strip().lower() != "да":
        say("Заказ не создан." if service.bakery else "Запись не создана.")
        return None
    bid = dialog.confirm()
    say(f"Заказ №{bid} ожидает подтверждения продавцом." if service.bakery else f"Запись подтверждена. Номер: {bid}")
    return bid


def run_bakery_auto(service, say=print):
    """Локальный сценарий, без сетевых отправок; часы сдвигаются только в памяти."""
    products = service.services()
    if not products:
        raise BookingError("Нет активных товаров для демонстрации.")
    product = products[0]
    for day in service.dates()[1:]:
        slots = service.slots(product["id"], None, day)
        if slots:
            break
    else:
        raise BookingError("Нет доступной даты для демонстрации.")
    starts = slots[0]["starts"]
    answers = iter([str(product["id"]), str(day), "1", "Анна Демо", "+79990000000", "да"])
    bid = run(service, lambda prompt: next(answers), say)
    service.confirm(bid)
    say(f"Заказ №{bid} подтверждён продавцом.")
    created = [bid]
    while not service.bakery.full(day):
        created.append(service.book(10002, product["id"], 0, starts, "Демо клиент", "+79990000001"))
    say(f"Дата {day} заполнена: лимит {service.bakery.settings()['daily_limit']}.")
    try:
        service.book(10003, product["id"], 0, starts, "Демо клиент", "+79990000002")
    except BookingError:
        say("Заказ сверх лимита отклонён.")
    else:
        raise AssertionError("Заказ превысил лимит")
    service.cancel(created[-1])
    assert not service.bakery.full(day)
    replacement = service.book(10004, product["id"], 0, starts, "Демо клиент", "+79990000003")
    service.confirm(replacement)
    say(f"Отмена освободила место; новый заказ №{replacement} принят и подтверждён.")
    original_clock = service.clock
    try:
        service.clock = lambda: datetime.fromisoformat(starts) - timedelta(hours=2)
        service.queue_reminders()
        with connect(service.config.db_path) as conn:
            rows = conn.execute("""SELECT o.body FROM outbox o JOIN order_reminders r ON r.outbox_id=o.id
                WHERE r.booking_id=?""", (replacement,)).fetchall()
        assert rows
        for row in rows:
            say(row["body"])
    finally:
        service.clock = original_clock
    return replacement


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--auto", action="store_true", help="Автоматический демонстрационный сценарий")
    parser.add_argument("--mode", choices=("salon", "bakery"), default=settings.mode)
    args = parser.parse_args()
    config = replace(settings, mode=args.mode)
    seed_demo(config)
    service = BookingService(config)
    if args.auto and service.bakery:
        try:
            run_bakery_auto(service)
        except (BookingError, ValueError) as exc:
            raise SystemExit(str(exc)) from None
        return
    ask = input
    if args.auto:
        for day in service.dates():
            if service.slots(1, None, day):
                answers = iter(["1", "0", str(day), "1", "Демо клиент", "+79991234567", "да"])
                ask = lambda prompt: next(answers)
                break
        else:
            raise SystemExit("На ближайшие 14 дней нет свободных слотов.")
    try:
        run(service, ask)
    except (BookingError, ValueError) as exc:
        raise SystemExit(str(exc)) from None


if __name__ == "__main__":
    main()
