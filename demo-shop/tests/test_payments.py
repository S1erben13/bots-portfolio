import json
from unittest.mock import AsyncMock

import httpx
import pytest
from aiogram import Bot
from aiogram.types import Update

from shop.bot import Email, create_dispatcher
from shop.payments import TelegramProvider, YooKassaProvider
from shop.service import ShopError


def use_yookassa(shop, handler):
    shop.config.payment_provider = "yookassa"
    shop.config.yookassa_shop_id = "test-shop"
    shop.config.yookassa_secret_key = "test-key"
    shop.payment = YooKassaProvider(shop.config, httpx.MockTransport(handler))


async def test_create_receipt_and_idempotence(shop):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"id": "payment-1", "confirmation": {"confirmation_url": "https://yookassa.ru/test"}})

    use_yookassa(shop, handle)
    order = shop.create_order(42, 1, "a@example.org")
    await shop.checkout(order["id"], 42)
    await shop.checkout(order["id"], 42)
    body = json.loads(requests[0].content)
    assert requests[0].method == "POST"
    assert str(requests[0].url) == "https://api.yookassa.ru/v3/payments"
    assert requests[0].headers["Idempotence-Key"] == requests[1].headers["Idempotence-Key"] == order["idempotence_key"]
    assert body["receipt"]["customer"]["email"] == "a@example.org"
    item = body["receipt"]["items"][0]
    assert item["vat_code"] == 1 and item["payment_subject"] == "service"
    assert item["amount"] == {"value": "790.00", "currency": "RUB"}


@pytest.mark.parametrize("status,value,currency,delivered", [("pending", "790.00", "RUB", False), ("succeeded", "790.00", "RUB", True), ("succeeded", "1.00", "RUB", False), ("succeeded", "790.00", "USD", False)])
async def test_verified_webhook(shop, status, value, currency, delivered):
    methods = []

    def handle(request):
        methods.append(request.method)
        if request.method == "POST":
            return httpx.Response(200, json={"id": "payment-1", "confirmation": {"confirmation_url": "https://yookassa.ru/test"}})
        return httpx.Response(200, json={"id": "payment-1", "status": status, "amount": {"value": value, "currency": currency}})

    use_yookassa(shop, handle)
    order = shop.create_order(42, 1, "a@example.org")
    await shop.checkout(order["id"], 42)
    body = {"event": "payment.succeeded", "object": {"id": "payment-1", "status": "succeeded", "amount": {"value": "790.00", "currency": "RUB"}}}
    if status == "succeeded" and not delivered:
        with pytest.raises(ShopError):
            await shop.webhook(body)
    else:
        await shop.webhook(body)
        await shop.webhook(body)
    assert "GET" in methods
    assert len(shop.sender.documents) == int(delivered)


@pytest.mark.parametrize("payment_id", ["../payments", [1], 12, None, "https://example.org"])
async def test_fetch_blocks_path_injection(shop, payment_id):
    use_yookassa(shop, lambda request: pytest.fail("Network must not be called"))
    with pytest.raises(ValueError):
        await shop.payment.fetch(payment_id)


@pytest.mark.parametrize("awaiting_email", [False, True])
async def test_telegram_successful_payment_twice(shop, awaiting_email):
    shop.config.payment_provider = "telegram"
    shop.payment = TelegramProvider()
    order = shop.create_order(42, 1, "a@example.org")
    await shop.checkout(order["id"], 42)
    bot = Bot("123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi")
    dispatcher = create_dispatcher(shop)
    try:
        if awaiting_email:
            context = dispatcher.fsm.get_context(bot=bot, chat_id=42, user_id=42)
            await context.set_state(Email.value)
            await context.update_data(product_id=2)
        for uid in (1, 2):
            update = Update.model_validate({"update_id": uid, "message": {
                "message_id": uid, "date": 1700000000, "chat": {"id": 42, "type": "private"},
                "from": {"id": 42, "is_bot": False, "first_name": "Test"},
                "successful_payment": {"currency": "RUB", "total_amount": 79000, "invoice_payload": str(order["id"]),
                                       "telegram_payment_charge_id": "charge", "provider_payment_charge_id": "provider-charge"}}})
            await dispatcher.feed_update(bot, update)
        assert len(shop.sender.documents) == 1
    finally:
        await dispatcher.storage.close()
        await bot.session.close()


@pytest.mark.parametrize("amount,currency,user_id,ok", [(79000, "RUB", 42, True), (1, "RUB", 42, False), (79000, "USD", 42, False), (79000, "RUB", 43, False)])
async def test_precheckout_handler(shop, amount, currency, user_id, ok):
    shop.config.payment_provider = "telegram"
    shop.payment = TelegramProvider()
    order = shop.create_order(42, 1, "a@example.org")
    await shop.checkout(order["id"], 42)
    bot = Bot("123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi")
    bot.session.make_request = AsyncMock(return_value=True)
    try:
        update = Update.model_validate({"update_id": 1, "pre_checkout_query": {
            "id": "q1", "from": {"id": user_id, "is_bot": False, "first_name": "Test"},
            "currency": currency, "total_amount": amount, "invoice_payload": str(order["id"])}})
        await create_dispatcher(shop).feed_update(bot, update)
        method = bot.session.make_request.call_args.args[1]
        assert method.ok is ok
    finally:
        await bot.session.close()
