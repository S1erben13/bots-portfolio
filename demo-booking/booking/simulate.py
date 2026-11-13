import argparse

from booking.config import settings
from booking.dialog import Dialog
from booking.seed_demo import seed_demo
from booking.service import BookingError, BookingService


def run(service, ask=input, say=print):
    dialog = Dialog(service, 10001)
    for row in service.services():
        say(f"{row['id']}: {row['name']} ({row['duration']} мин.)")
    dialog.choose_service(int(ask("Номер услуги: ")))
    say("0: Любой мастер")
    for row in service.masters():
        say(f"{row['id']}: {row['name']}")
    dialog.choose_master(int(ask("Номер мастера: ")) or None)
    say("Даты: " + ", ".join(str(day) for day in service.dates()))
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
        say("Запись не создана.")
        return None
    bid = dialog.confirm()
    say(f"Запись подтверждена. Номер: {bid}")
    return bid


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--auto", action="store_true", help="Автоматический демонстрационный сценарий")
    args = parser.parse_args()
    seed_demo()
    service = BookingService(settings)
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
