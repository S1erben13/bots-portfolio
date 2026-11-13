from decimal import Decimal, InvalidOperation
from typing import Protocol

import httpx


def rub(amount):
    return f"{amount // 100}.{amount % 100:02d}"


def kopecks(value):
    try:
        number = Decimal(str(value)) * 100
        if not number.is_finite() or number != number.to_integral_value() or number <= 0:
            raise ValueError("Неверная сумма")
        return int(number)
    except (InvalidOperation, TypeError) as exc:
        raise ValueError("Неверная сумма") from exc


def receipt(order, config):
    return {"customer": {"email": order["email"]}, "items": [{
        "description": order["name"][:128], "quantity": "1.00",
        "amount": {"value": rub(order["amount"]), "currency": "RUB"},
        "vat_code": config.vat_code, "payment_subject": config.payment_subject,
        "payment_mode": "full_payment"}]}


class PaymentProvider(Protocol):
    async def create(self, order) -> dict: ...


class FakeProvider:
    async def create(self, order):
        return {"id": f"fake-{order['id']}", "kind": "fake"}


class TelegramProvider:
    async def create(self, order):
        return {"id": f"telegram-{order['id']}", "kind": "telegram"}


class YooKassaProvider:
    def __init__(self, config, transport=None):
        self.config = config
        self.transport = transport

    async def request(self, method, path, **kwargs):
        if not self.config.yookassa_shop_id or not self.config.yookassa_secret_key:
            raise ValueError("Задайте ключи тестового магазина ЮKassa")
        async with httpx.AsyncClient(base_url="https://api.yookassa.ru/v3/",
                                     auth=(self.config.yookassa_shop_id, self.config.yookassa_secret_key),
                                     timeout=20, transport=self.transport) as client:
            response = await client.request(method, path, **kwargs)
            response.raise_for_status()
            return response.json()

    async def create(self, order):
        body = {"amount": {"value": rub(order["amount"]), "currency": "RUB"},
                "capture": True, "confirmation": {"type": "redirect", "return_url": self.config.payment_return_url},
                "description": f"Заказ {order['id']}", "metadata": {"order_id": str(order["id"])},
                "receipt": receipt(order, self.config)}
        data = await self.request("POST", "payments", json=body,
                                  headers={"Idempotence-Key": order["idempotence_key"]})
        return {"id": data["id"], "kind": "yookassa", "url": data["confirmation"]["confirmation_url"]}

    async def fetch(self, payment_id):
        # Не допускаем подстановку произвольного URL/пути из webhook.
        if not isinstance(payment_id, str) or not payment_id or len(payment_id) > 100 or not all(c.isascii() and (c.isalnum() or c == "-") for c in payment_id):
            raise ValueError("Неверный идентификатор платежа")
        return await self.request("GET", f"payments/{payment_id}")


def provider(config):
    return {"fake": FakeProvider, "telegram": TelegramProvider}.get(config.payment_provider, lambda: YooKassaProvider(config))()
