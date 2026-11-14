"""Заказы самовывоза. Лимит проверяется под той же блокировкой, что и запись."""
import re
from datetime import datetime, time, timedelta

from booking.db import connect
from booking.service import BookingError, stamp


class BakeryOrders:
    def __init__(self, service):
        self.service = service
        self.config = service.config

    def clock(self):
        return self.service.clock()

    def settings(self, conn=None):
        if conn is None:
            with connect(self.config.db_path) as conn:
                return self.settings(conn)
        row = conn.execute("SELECT * FROM bakery_settings WHERE id=1").fetchone()
        if not row:
            raise BookingError("Сначала выполните seed_demo для кондитерской.")
        return row

    def full(self, day, exclude=None, conn=None):
        if conn is None:
            with connect(self.config.db_path) as conn:
                return self.full(day, exclude, conn)
        count = conn.execute("""SELECT count(*) FROM orders WHERE starts>=? AND starts<?
            AND status IN ('pending','confirmed') AND id!=?""",
            (str(day), str(day + timedelta(days=1)), exclude or -1)).fetchone()[0]
        return count >= self.settings(conn)["daily_limit"]

    def slots(self, conn, product_id, day, exclude=None):
        if day not in self.service.dates() or self.full(day, exclude, conn):
            return []
        # При переносе можно сохранить уже заказанный, позднее архивированный товар.
        product = conn.execute("SELECT 1 FROM products WHERE id=? AND active=1", (product_id,)).fetchone()
        existing = exclude and conn.execute("SELECT 1 FROM orders WHERE id=? AND product_id=?",
                                            (exclude, product_id)).fetchone()
        if not product and not existing:
            return []
        cursor = datetime.combine(day, time(9))
        result = []
        while cursor < datetime.combine(day, time(18)):
            if cursor > self.clock():
                result.append({"master_id": 0, "master": "Самовывоз", "starts": stamp(cursor),
                               "ends": stamp(cursor + timedelta(minutes=15))})
            cursor += timedelta(minutes=15)
        return result

    def book(self, user_id, product_id, starts, name, phone):
        name = name.strip()
        phone = re.sub(r"[\s()\-]", "", phone)
        if not 2 <= len(name) <= 100:
            raise BookingError("Введите имя длиной от 2 до 100 символов.")
        if not re.fullmatch(r"\+?\d{10,15}", phone):
            raise BookingError("Введите телефон: от 10 до 15 цифр, можно с +.")
        start = datetime.fromisoformat(starts)
        with connect(self.config.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            if not any(slot["starts"] == starts for slot in self.slots(conn, product_id, start.date())):
                raise BookingError("Дата заполнена или время выдачи недоступно.")
            product = conn.execute("SELECT * FROM products WHERE id=? AND active=1", (product_id,)).fetchone()
            cursor = conn.execute("""INSERT INTO orders(user_id,product_id,product_name,product_price,starts,name,phone)
                VALUES (?,?,?,?,?,?,?)""", (user_id, product_id, product["name"], product["price"], starts, name, phone))
            bid = cursor.lastrowid
            self.service._notify(conn, f"Новый заказ №{bid}: {product['name']}, {product['price']} ₽, {starts}, {name}, {phone}. Ожидает подтверждения.")
            return bid

    def bookings(self, user_id=None, day=None, days=1):
        sql = """SELECT *, product_id AS service_id, product_name AS service,
                 0 AS master_id, 'Самовывоз' AS master FROM orders WHERE 1=1"""
        params = []
        if user_id is not None:
            sql += " AND user_id=?"
            params.append(user_id)
        if day:
            sql += " AND starts>=? AND starts<?"
            params.extend((str(day), str(day + timedelta(days=days))))
        with connect(self.config.db_path) as conn:
            return conn.execute(sql + " ORDER BY starts,id", params).fetchall()

    def editable(self, conn, bid, user_id):
        row = conn.execute("SELECT * FROM orders WHERE id=?", (bid,)).fetchone()
        if not row or (user_id is not None and row["user_id"] != user_id):
            raise BookingError("Заказ не найден.")
        if row["status"] not in ("pending", "confirmed"):
            raise BookingError("Этот заказ уже нельзя изменить.")
        if user_id is not None and datetime.fromisoformat(row["starts"]) < self.clock() + timedelta(hours=self.config.cancel_hours):
            raise BookingError(f"Изменения возможны не позднее чем за {self.config.cancel_hours} ч.")
        return row

    def clear_reminders(self, conn, bid):
        ids = conn.execute("SELECT outbox_id FROM order_reminders WHERE booking_id=?", (bid,)).fetchall()
        conn.execute("DELETE FROM order_reminders WHERE booking_id=?", (bid,))
        for row in ids:
            conn.execute("DELETE FROM outbox WHERE id=?", (row[0],))

    def notify_client(self, conn, row, text):
        address = self.settings(conn)["pickup_address"]
        conn.execute("INSERT INTO outbox(chat_id,body) VALUES (?,?)", (str(row["user_id"]),
            f"{text}: заказ №{row['id']}, {row['product_name']} — {row['product_price']} ₽. "
            f"Выдача: {row['starts'].replace('T', ' ')}. Самовывоз: {address}."))

    def confirm(self, bid):
        with connect(self.config.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self.editable(conn, bid, None)
            if row["status"] != "pending":
                raise BookingError("Заказ уже подтверждён.")
            conn.execute("UPDATE orders SET status='confirmed' WHERE id=?", (bid,))
            self.notify_client(conn, row, "Подтверждён")

    def cancel(self, bid, user_id=None):
        with connect(self.config.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self.editable(conn, bid, user_id)
            conn.execute("UPDATE orders SET status='cancelled' WHERE id=?", (bid,))
            self.clear_reminders(conn, bid)
            self.notify_client(conn, row, "Отменён")
            self.service._notify(conn, f"Отмена заказа №{bid}, {row['starts']}, {row['name']}.")

    def reschedule(self, bid, user_id, starts):
        start = datetime.fromisoformat(starts)
        with connect(self.config.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self.editable(conn, bid, user_id)
            if start < self.clock() + timedelta(hours=self.config.cancel_hours):
                raise BookingError("Новое время слишком близко.")
            slots = self.slots(conn, row["product_id"], start.date(), bid)
            if not any(slot["starts"] == starts for slot in slots):
                raise BookingError("Дата заполнена или время выдачи недоступно.")
            conn.execute("UPDATE orders SET starts=? WHERE id=?", (starts, bid))
            self.clear_reminders(conn, bid)
            moved = conn.execute("SELECT * FROM orders WHERE id=?", (bid,)).fetchone()
            self.notify_client(conn, moved, "Перенесён")
            self.service._notify(conn, f"Перенос заказа №{bid}: {row['starts']} → {starts}.")

    def queue_reminders(self):
        now = self.clock()
        with connect(self.config.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("""UPDATE outbox SET sent=1 WHERE id IN (
                SELECT r.outbox_id FROM order_reminders r JOIN orders o ON o.id=r.booking_id
                WHERE o.status!='confirmed' OR o.starts<=? OR (r.hours=24 AND o.starts<=?))""",
                (stamp(now), stamp(now + timedelta(hours=2))))
            for row in conn.execute("SELECT * FROM orders WHERE status='confirmed' AND starts>?", (stamp(now),)).fetchall():
                remaining = datetime.fromisoformat(row["starts"]) - now
                for hours in (24, 2):
                    if remaining > timedelta(hours=hours) or (hours == 24 and remaining <= timedelta(hours=2)):
                        continue
                    if conn.execute("SELECT 1 FROM order_reminders WHERE booking_id=? AND hours=?", (row["id"], hours)).fetchone():
                        continue
                    address = self.settings(conn)["pickup_address"]
                    body = (f"Напоминание ({hours} ч): выдача заказа №{row['id']}, {row['product_name']}, "
                            f"{row['starts'].replace('T', ' ')}. Самовывоз: {address}.")
                    out = conn.execute("INSERT INTO outbox(chat_id,body) VALUES (?,?)", (str(row["user_id"]), body))
                    conn.execute("INSERT INTO order_reminders VALUES (?,?,?)", (row["id"], hours, out.lastrowid))
