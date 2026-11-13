import re
from datetime import datetime, timedelta, time

from booking.config import settings
from booking.db import connect, init_db


class BookingError(ValueError):
    pass


def stamp(value):
    return value.isoformat(timespec="minutes")


class BookingService:
    def __init__(self, config=settings, clock=None):
        self.config = config
        self.clock = clock or config.now
        init_db(config.db_path)

    def services(self):
        with connect(self.config.db_path) as conn:
            return conn.execute("SELECT * FROM services WHERE active=1 ORDER BY id").fetchall()

    def masters(self):
        with connect(self.config.db_path) as conn:
            return conn.execute("SELECT * FROM masters WHERE active=1 ORDER BY id").fetchall()

    def dates(self):
        return [self.clock().date() + timedelta(days=i) for i in range(14)]

    def _slots(self, conn, service_id, master_id, day, exclude=None):
        if day not in self.dates():
            return []
        service = conn.execute("SELECT * FROM services WHERE id=? AND active=1", (service_id,)).fetchone()
        if not service:
            return []
        sql = "SELECT * FROM masters WHERE active=1"
        params = ()
        if master_id:
            sql += " AND id=?"
            params = (master_id,)
        result = []
        for master in conn.execute(sql, params):
            mid = master["id"]
            if conn.execute("SELECT 1 FROM days_off WHERE master_id=? AND day=?", (mid, str(day))).fetchone():
                continue
            work = conn.execute("SELECT * FROM schedule WHERE master_id=? AND weekday=?", (mid, day.weekday())).fetchone()
            if not work:
                continue
            cursor = datetime.combine(day, time.fromisoformat(work["start"]))
            finish = datetime.combine(day, time.fromisoformat(work["end"]))
            busy = conn.execute(
                "SELECT starts, ends FROM bookings WHERE master_id=? AND status!='cancelled' AND id!=?",
                (mid, exclude or -1),
            ).fetchall()
            while cursor + timedelta(minutes=service["duration"]) <= finish:
                end = cursor + timedelta(minutes=service["duration"])
                available = cursor > self.clock()
                if work["break_start"]:
                    bs = datetime.combine(day, time.fromisoformat(work["break_start"]))
                    be = datetime.combine(day, time.fromisoformat(work["break_end"]))
                    available = available and not (cursor < be and end > bs)
                for booking in busy:
                    if stamp(cursor) < booking["ends"] and stamp(end) > booking["starts"]:
                        available = False
                if available:
                    result.append({"master_id": mid, "master": master["name"], "starts": stamp(cursor), "ends": stamp(end)})
                cursor += timedelta(minutes=15)
        return sorted(result, key=lambda item: (item["starts"], item["master_id"]))

    def slots(self, service_id, master_id, day, exclude=None):
        with connect(self.config.db_path) as conn:
            return self._slots(conn, service_id, master_id, day, exclude)

    def _notify(self, conn, body):
        if self.config.admin_chat_id:
            conn.execute("INSERT INTO outbox(chat_id, body) VALUES (?, ?)", (self.config.admin_chat_id, body))

    def _occupy(self, conn, booking_id, slot):
        cursor = datetime.fromisoformat(slot["starts"])
        end = datetime.fromisoformat(slot["ends"])
        while cursor < end:
            conn.execute("INSERT INTO occupied VALUES (?, ?, ?)", (slot["master_id"], stamp(cursor), booking_id))
            cursor += timedelta(minutes=15)

    def book(self, user_id, service_id, master_id, starts, name, phone):
        name = name.strip()
        phone = re.sub(r"[\s()\-]", "", phone)
        if not 2 <= len(name) <= 100:
            raise BookingError("Введите имя длиной от 2 до 100 символов.")
        if not re.fullmatch(r"\+?\d{10,15}", phone):
            raise BookingError("Введите телефон: от 10 до 15 цифр, можно с +.")
        try:
            start = datetime.fromisoformat(starts)
        except ValueError:
            raise BookingError("Некорректная дата.") from None
        with connect(self.config.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            chosen = None
            for slot in self._slots(conn, service_id, master_id, start.date()):
                if slot["starts"] == starts:
                    chosen = slot
                    break
            if not chosen:
                raise BookingError("Слот уже занят или недоступен. Выберите другое время.")
            cursor = conn.execute(
                "INSERT INTO bookings(user_id,service_id,master_id,starts,ends,name,phone) VALUES (?,?,?,?,?,?,?)",
                (user_id, service_id, chosen["master_id"], starts, chosen["ends"], name, phone),
            )
            bid = cursor.lastrowid
            self._occupy(conn, bid, chosen)
            self._notify(conn, f"Новая запись №{bid}: {name}, {phone}, {starts}, мастер {chosen['master']}.")
            return bid

    def bookings(self, user_id=None, day=None, days=1):
        sql = """SELECT b.*, s.name AS service, m.name AS master FROM bookings b
                 JOIN services s ON s.id=b.service_id JOIN masters m ON m.id=b.master_id WHERE 1=1"""
        params = []
        if user_id is not None:
            sql += " AND b.user_id=?"
            params.append(user_id)
        if day:
            sql += " AND b.starts>=? AND b.starts<?"
            params.extend([str(day), str(day + timedelta(days=days))])
        sql += " ORDER BY b.starts"
        with connect(self.config.db_path) as conn:
            return conn.execute(sql, params).fetchall()

    def _editable(self, conn, booking_id, user_id):
        row = conn.execute("SELECT * FROM bookings WHERE id=?", (booking_id,)).fetchone()
        if not row or (user_id is not None and row["user_id"] != user_id):
            raise BookingError("Запись не найдена.")
        if row["status"] != "confirmed":
            raise BookingError("Эту запись уже нельзя изменить.")
        if user_id is not None:
            deadline = self.clock() + timedelta(hours=self.config.cancel_hours)
            if datetime.fromisoformat(row["starts"]) < deadline:
                raise BookingError(f"Изменения возможны не позднее чем за {self.config.cancel_hours} ч.")
        return row

    def _clear_reminders(self, conn, booking_id):
        # Сначала удаляем ссылки, затем сообщения: внешние ключи остаются корректны.
        ids = conn.execute("SELECT outbox_id FROM reminders WHERE booking_id=?", (booking_id,)).fetchall()
        conn.execute("DELETE FROM reminders WHERE booking_id=?", (booking_id,))
        for row in ids:
            conn.execute("DELETE FROM outbox WHERE id=?", (row[0],))

    def cancel(self, booking_id, user_id=None):
        with connect(self.config.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._editable(conn, booking_id, user_id)
            conn.execute("UPDATE bookings SET status='cancelled' WHERE id=?", (booking_id,))
            conn.execute("DELETE FROM occupied WHERE booking_id=?", (booking_id,))
            self._clear_reminders(conn, booking_id)
            self._notify(conn, f"Отмена записи №{booking_id}, {row['starts']}, {row['name']}.")

    def reschedule(self, booking_id, user_id, master_id, starts):
        with connect(self.config.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._editable(conn, booking_id, user_id)
            start = datetime.fromisoformat(starts)
            if start < self.clock() + timedelta(hours=self.config.cancel_hours):
                raise BookingError("Новое время слишком близко. Выберите более поздний слот.")
            chosen = None
            for slot in self._slots(conn, row["service_id"], master_id, start.date(), booking_id):
                if slot["starts"] == starts:
                    chosen = slot
                    break
            if not chosen:
                raise BookingError("Слот уже занят или недоступен.")
            conn.execute("DELETE FROM occupied WHERE booking_id=?", (booking_id,))
            conn.execute("UPDATE bookings SET master_id=?, starts=?, ends=? WHERE id=?",
                         (chosen["master_id"], starts, chosen["ends"], booking_id))
            self._occupy(conn, booking_id, chosen)
            self._clear_reminders(conn, booking_id)
            self._notify(conn, f"Перенос записи №{booking_id}: {row['starts']} → {starts}.")

    def queue_reminders(self):
        now = self.clock()
        with connect(self.config.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("""UPDATE outbox SET sent=1 WHERE id IN (
                SELECT r.outbox_id FROM reminders r JOIN bookings b ON b.id=r.booking_id
                WHERE b.status!='confirmed' OR b.starts<=?
                OR (r.hours=24 AND b.starts<=?))""",
                (stamp(now), stamp(now + timedelta(hours=2))))
            rows = conn.execute("SELECT * FROM bookings WHERE status='confirmed' AND starts>?", (stamp(now),)).fetchall()
            for row in rows:
                remaining = datetime.fromisoformat(row["starts"]) - now
                # Не отправляем 24-часовое напоминание, если запись создана ближе чем за 2 часа.
                for hours in (24, 2):
                    due = remaining <= timedelta(hours=hours)
                    if hours == 24 and remaining <= timedelta(hours=2):
                        continue
                    exists = conn.execute("SELECT 1 FROM reminders WHERE booking_id=? AND hours=?", (row["id"], hours)).fetchone()
                    if due and not exists:
                        body = f"Напоминание ({hours} ч): запись №{row['id']} на {row['starts']}."
                        out = conn.execute("INSERT INTO outbox(chat_id,body) VALUES (?,?)", (str(row["user_id"]), body))
                        conn.execute("INSERT INTO reminders VALUES (?,?,?)", (row["id"], hours, out.lastrowid))
