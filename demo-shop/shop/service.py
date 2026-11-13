import asyncio
import re
import secrets
import time
from pathlib import Path

from shop.db import connect, init_db
from shop.payments import kopecks, provider


class ShopError(ValueError):
    pass


def validate_email(value):
    value = value.strip()
    if len(value) > 254 or not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)+", value):
        raise ShopError("Введите корректный email")
    local = value.split("@")[0]
    if local.startswith(".") or local.endswith(".") or ".." in local or len(local) > 64:
        raise ShopError("Введите корректный email")
    return value


class Shop:
    def __init__(self, config, sender, payment=None, clock=time.time):
        self.config, self.sender, self.clock = config, sender, clock
        self.payment = payment or provider(config)
        self.lock = asyncio.Lock()
        init_db(config.db_path)

    def file_path(self, name):
        root = Path(self.config.files_root).resolve()
        path = (root / name).resolve()
        if path == root or not path.is_relative_to(root) or not path.is_file():
            raise ShopError("Файл недоступен")
        return path

    def products(self, active=True):
        with connect(self.config.db_path) as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM products" + (" WHERE active=1" if active else "") + " ORDER BY id")]

    def order(self, oid, user_id=None):
        with connect(self.config.db_path) as conn:
            row = conn.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
        if not row or (user_id is not None and row["user_id"] != user_id):
            raise ShopError("Заказ не найден")
        return dict(row)

    def orders(self, user_id=None, status=None):
        clauses, args = [], []
        if user_id is not None:
            clauses.append("user_id=?")
            args.append(user_id)
        if status:
            if status not in {"new", "pending", "paid", "delivered", "canceled"}:
                raise ShopError("Неверный статус")
            clauses.append("status=?")
            args.append(status)
        with connect(self.config.db_path) as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM orders" + (" WHERE " + " AND ".join(clauses) if clauses else "") + " ORDER BY id DESC", args)]

    def create_order(self, user_id, product_id, email):
        email = validate_email(email)
        with connect(self.config.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            product = conn.execute("SELECT * FROM products WHERE id=? AND active=1", (product_id,)).fetchone()
            if not product:
                raise ShopError("Товар недоступен")
            self.file_path(product["file"])
            oid = conn.execute("INSERT INTO orders(user_id,product_id,name,amount,file,email,provider,idempotence_key,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                               (user_id, product_id, product["name"], product["price"], product["file"], email, self.config.payment_provider, secrets.token_urlsafe(24), self.clock())).lastrowid
        return self.order(oid)

    async def checkout(self, oid, user_id):
        async with self.lock:
            order = self.order(oid, user_id)
            if order["status"] not in {"new", "pending"}:
                raise ShopError("Заказ уже обработан")
            result = await self.payment.create(order)
            with connect(self.config.db_path) as conn:
                conn.execute("UPDATE orders SET status='pending',payment_id=? WHERE id=?", (result["id"], oid))
            return result

    def pre_checkout(self, oid, user_id, amount, currency):
        try:
            order = self.order(oid, user_id)
            return order["provider"] == "telegram" and order["status"] == "pending" and order["amount"] == amount and currency == "RUB"
        except ShopError:
            return False

    def cancel(self, oid, user_id):
        self.order(oid, user_id)
        with connect(self.config.db_path) as conn:
            changed = conn.execute("UPDATE orders SET status='canceled' WHERE id=? AND status IN ('new','pending')", (oid,)).rowcount
        if not changed:
            raise ShopError("Нельзя отменить оплаченный заказ")

    async def confirm(self, oid, amount, currency, kind, payment_id=None, user_id=None):
        async with self.lock:
            order = self.order(oid, user_id)
            if order["provider"] != kind or order["amount"] != amount or currency != "RUB" or order["status"] not in {"pending", "paid", "delivered"}:
                raise ShopError("Платёж не соответствует заказу")
            if kind == "yookassa" and order["payment_id"] != payment_id:
                raise ShopError("Неверный платёж")
            with connect(self.config.db_path) as conn:
                conn.execute("UPDATE orders SET status='paid',paid_at=? WHERE id=? AND status='pending'", (self.clock(), oid))
            delivered = await self._deliver(oid)
            order = self.order(oid)
            if self.config.admin_chat_id and not order["admin_notified"]:
                await self.sender.notify(self.config.admin_chat_id, f"Оплачен заказ №{oid}: {order['amount'] // 100} ₽")
                with connect(self.config.db_path) as conn:
                    conn.execute("UPDATE orders SET admin_notified=1 WHERE id=?", (oid,))
            return delivered

    async def webhook(self, body):
        if self.config.payment_provider != "yookassa":
            raise ShopError("Провайдер отключён")
        try:
            pid = body["object"]["id"]
            data = await self.payment.fetch(pid)
            if data.get("id") != pid or data.get("status") != "succeeded":
                return False
            with connect(self.config.db_path) as conn:
                row = conn.execute("SELECT id FROM orders WHERE payment_id=? AND provider='yookassa'", (pid,)).fetchone()
            if not row:
                raise ShopError("Неизвестный платёж")
            return await self.confirm(row["id"], kopecks(data["amount"]["value"]), data["amount"]["currency"], "yookassa", pid)
        except (KeyError, TypeError) as exc:
            raise ShopError("Неверное уведомление") from exc

    async def _deliver(self, oid, repeat=False):
        # Один процесс; блокировка и BEGIN IMMEDIATE исключают параллельную выдачу.
        with connect(self.config.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            order = dict(conn.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone())
            if order["status"] not in {"paid", "delivered"}:
                raise ShopError("Заказ не оплачен")
            if order["delivered_at"] is not None and not repeat:
                return False
            path = self.file_path(order["file"])
            token = secrets.token_urlsafe(32)
            conn.execute("INSERT INTO downloads(token,order_id,expires_at) VALUES (?,?,?)", (token, oid, self.clock() + 72 * 3600))
            url = f"{self.config.public_url.rstrip('/')}/d/{token}"
            await self.sender.document(order["user_id"], path, url)
            conn.execute("UPDATE orders SET status='delivered',delivered_at=? WHERE id=?", (self.clock(), oid))
        return True

    async def redeliver(self, oid, user_id=None):
        async with self.lock:
            self.order(oid, user_id)
            return await self._deliver(oid, repeat=True)

    def download(self, token):
        with connect(self.config.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT d.*,o.file,o.status FROM downloads d JOIN orders o ON o.id=d.order_id WHERE token=?", (token,)).fetchone()
            if not row or row["expires_at"] <= self.clock() or row["uses"] >= 5 or row["status"] not in {"paid", "delivered"}:
                raise ShopError("Ссылка недействительна или исчерпана")
            path = self.file_path(row["file"])
            conn.execute("UPDATE downloads SET uses=uses+1 WHERE token=?", (token,))
            return path

    def revenue(self, start, end):
        if end <= start:
            raise ShopError("Неверный период")
        with connect(self.config.db_path) as conn:
            return conn.execute("SELECT COALESCE(SUM(amount),0) FROM orders WHERE paid_at>=? AND paid_at<? AND status IN ('paid','delivered')", (start, end)).fetchone()[0]
