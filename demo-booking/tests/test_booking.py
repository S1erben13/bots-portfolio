import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from threading import Barrier

import pytest

from booking.db import connect
from booking.dialog import Dialog
from booking.seed_demo import seed_demo
from booking.service import BookingError
from booking.simulate import run


DAY = date(2026, 11, 17)


def book(service, starts="2026-11-17T10:00", master=1, sid=1, uid=7):
    return service.book(uid, sid, master, starts, "Иван", "+79991234567")


def test_seed_idempotent(system):
    service, _ = system
    seed_demo(service.config)
    assert len(service.services()) == 4
    assert len(service.masters()) == 2


def test_slots_duration_breaks_weekends_and_horizon(system):
    service, clock = system
    slots = service.slots(3, 1, DAY)
    assert slots[0]["starts"] == "2026-11-17T09:00"
    assert slots[0]["ends"] == "2026-11-17T11:00"
    assert slots[-1]["starts"] == "2026-11-17T16:00"
    assert not any(slot["starts"] < "2026-11-17T14:00" and slot["ends"] > "2026-11-17T13:00" for slot in slots)
    assert service.slots(1, 1, date(2026, 11, 22)) == []
    assert service.slots(1, 1, clock[0].date() + timedelta(days=14)) == []
    assert len(service.dates()) == 14
    clock[0] = datetime(2026, 11, 16, 10, 1)
    assert service.slots(1, 1, clock[0].date())[0]["starts"] == "2026-11-16T10:15"


def test_dayoff(system):
    service, _ = system
    with connect(service.config.db_path) as conn:
        conn.execute("INSERT INTO days_off VALUES (?,?)", (1, str(DAY)))
    assert service.slots(1, 1, DAY) == []
    assert all(slot["master_id"] == 2 for slot in service.slots(1, None, DAY))


def test_overlap_and_adjacent(system):
    service, _ = system
    book(service, sid=3)
    for starts in ("09:45", "10:15", "11:45"):
        with pytest.raises(BookingError):
            book(service, "2026-11-17T" + starts)
    assert book(service, "2026-11-17T12:00")
    assert book(service, master=2)


@pytest.mark.parametrize("starts", [("10:00", "10:00"), ("10:00", "10:15")])
def test_parallel_double_booking(system, starts):
    service, _ = system
    barrier = Barrier(2)

    def attempt(start):
        barrier.wait()
        try:
            return book(service, "2026-11-17T" + start)
        except BookingError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, starts))
    assert sum(result is not None for result in results) == 1
    assert len(service.bookings()) == 1


def test_unique_index_covers_each_occupied_tick(system):
    service, _ = system
    bid = book(service)
    with connect(service.config.db_path) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO occupied VALUES (?,?,?)", (1, "2026-11-17T10:15", bid))


def test_any_master_fallback(system):
    service, _ = system
    book(service)
    bid = book(service, master=None, uid=8)
    row = next(row for row in service.bookings() if row["id"] == bid)
    assert row["master_id"] == 2


def test_cancel_deadline_ownership_and_reuse(system):
    service, clock = system
    bid = book(service)
    with pytest.raises(BookingError, match="не найдена"):
        service.cancel(bid, 8)
    clock[0] = datetime(2026, 11, 17, 7, 1)
    with pytest.raises(BookingError, match="3 ч"):
        service.cancel(bid, 7)
    clock[0] = datetime(2026, 11, 17, 7)
    service.cancel(bid, 7)  # Ровно за N часов разрешено.
    assert service.bookings(user_id=7)[0]["status"] == "cancelled"
    assert book(service) != bid


def test_move_atomic_and_reminders_reset(system):
    service, clock = system
    first = book(service)
    book(service, "2026-11-17T11:00", uid=8)
    clock[0] = datetime(2026, 11, 16, 10)
    service.queue_reminders()
    with connect(service.config.db_path) as conn:
        assert conn.execute("SELECT count(*) FROM reminders WHERE booking_id=?", (first,)).fetchone()[0] == 1
    with pytest.raises(BookingError):
        service.reschedule(first, 7, 1, "2026-11-17T11:00")
    assert service.bookings(user_id=7)[0]["starts"] == "2026-11-17T10:00"
    service.reschedule(first, 7, 2, "2026-11-18T10:00")
    assert service.bookings(user_id=7)[0]["master_id"] == 2
    with connect(service.config.db_path) as conn:
        assert conn.execute("SELECT count(*) FROM reminders WHERE booking_id=?", (first,)).fetchone()[0] == 0
    assert book(service)
    clock[0] = datetime(2026, 11, 18, 7, 1)
    with pytest.raises(BookingError):
        service.reschedule(first, 7, 1, "2026-11-19T10:00")


def test_reminders_clock_idempotency_and_cancel(system):
    service, clock = system
    bid = book(service)
    clock[0] = datetime(2026, 11, 16, 9, 59)
    service.queue_reminders()
    with connect(service.config.db_path) as conn:
        assert conn.execute("SELECT count(*) FROM reminders").fetchone()[0] == 0
    clock[0] = datetime(2026, 11, 16, 10)
    service.queue_reminders()
    service.queue_reminders()
    with connect(service.config.db_path) as conn:
        assert conn.execute("SELECT hours FROM reminders").fetchall()[0][0] == 24
    clock[0] = datetime(2026, 11, 17, 8)
    service.queue_reminders()
    service.queue_reminders()
    with connect(service.config.db_path) as conn:
        assert conn.execute("SELECT count(*) FROM reminders").fetchone()[0] == 2
    service.cancel(bid)  # Администратор может отменить в любое время.
    with connect(service.config.db_path) as conn:
        assert conn.execute("SELECT count(*) FROM reminders").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM outbox WHERE body LIKE 'Напоминание%'").fetchone()[0] == 0


def test_console_dialog(system):
    service, _ = system
    answers = iter(["1", "0", str(DAY), "1", "Иван", "+7 (999) 123-45-67", "да"])
    output = []
    bid = run(service, lambda prompt: next(answers), output.append)
    assert bid
    assert service.bookings(user_id=10001)[0]["phone"] == "+79991234567"
    assert output[-1] == f"Запись подтверждена. Номер: {bid}"


def test_dialog_move_and_invalid_flow(system):
    service, _ = system
    dialog = Dialog(service, 7)
    with pytest.raises(BookingError):
        dialog.confirm()
    bid = book(service)
    dialog.move(service.bookings(user_id=7)[0])
    dialog.choose_master(2)
    slots = dialog.choose_date(str(DAY))
    slot = slots[0]
    dialog.choose_slot(slot["master_id"], slot["starts"])
    assert dialog.confirm() == bid
    with pytest.raises(BookingError):
        dialog.confirm()


def test_invalid_phone_no_write(system):
    service, _ = system
    with pytest.raises(BookingError):
        service.book(7, 1, 1, "2026-11-17T10:00", "Иван", "x")
    assert service.bookings() == []
