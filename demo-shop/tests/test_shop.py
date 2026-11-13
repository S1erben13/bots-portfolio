import asyncio
from pathlib import Path

import pytest

from shop.db import connect
from shop.payments import kopecks, rub
from shop.seed_demo import seed_demo
from shop.service import ShopError, validate_email
from shop.simulate import simulate


@pytest.mark.parametrize("amount,text", [(79000, "790.00"), (19000, "190.00"), (1, "0.01"), (12345, "123.45")])
def test_money(amount, text):
    assert rub(amount) == text
    assert kopecks(text) == amount


@pytest.mark.parametrize("value", ["0", "-1", "1.001", "NaN", "Infinity", "oops"])
def test_invalid_money(value):
    with pytest.raises(ValueError):
        kopecks(value)


@pytest.mark.parametrize("email", ["a@example.org", " x+y@example.ru ", "first.last@sub.example.org"])
def test_email(email):
    assert validate_email(email) == email.strip()


@pytest.mark.parametrize("email", ["", "a", "a@host", ".a@example.org", "a..b@example.org", "x@-host.org", "x@y.org\nBcc: x@z.org", "a" * 65 + "@host.org"])
def test_bad_email(email):
    with pytest.raises(ShopError):
        validate_email(email)


def test_seed_idempotent(shop):
    seed_demo(shop.config)
    assert [p["price"] for p in shop.products()] == [79000, 19000]


async def test_lifecycle(shop, order):
    assert order["status"] == "new"
    await shop.checkout(order["id"], 42)
    assert shop.order(order["id"])["status"] == "pending"
    await shop.confirm(order["id"], 79000, "RUB", "fake")
    result = shop.order(order["id"])
    assert result["status"] == "delivered" and result["paid_at"] and result["delivered_at"]
    assert len(shop.sender.notifications) == 1


async def test_concurrent_notifications(shop, order):
    await shop.checkout(order["id"], 42)
    results = await asyncio.gather(*(shop.confirm(order["id"], 79000, "RUB", "fake") for _ in range(5)))
    assert results.count(True) == 1
    assert len(shop.sender.documents) == len(shop.sender.notifications) == 1


@pytest.mark.parametrize("amount,currency,kind", [(1, "RUB", "fake"), (79000, "USD", "fake"), (79000, "RUB", "telegram")])
async def test_wrong_payment(shop, order, amount, currency, kind):
    await shop.checkout(order["id"], 42)
    with pytest.raises(ShopError):
        await shop.confirm(order["id"], amount, currency, kind)
    assert not shop.sender.documents


def test_foreign_order(shop, order):
    with pytest.raises(ShopError):
        shop.order(order["id"], 43)


async def test_foreign_redelivery(shop, order):
    await shop.checkout(order["id"], 42)
    await shop.confirm(order["id"], 79000, "RUB", "fake")
    with pytest.raises(ShopError):
        await shop.redeliver(order["id"], 43)
    await shop.redeliver(order["id"], 42)
    assert len(shop.sender.documents) == 2


async def test_unpaid_redelivery(shop, order):
    with pytest.raises(ShopError):
        await shop.redeliver(order["id"], 42)


async def test_cancel(shop, order):
    shop.cancel(order["id"], 42)
    with pytest.raises(ShopError):
        await shop.confirm(order["id"], 79000, "RUB", "fake")


async def test_paid_cancel_rejected(shop, order):
    await shop.checkout(order["id"], 42)
    await shop.confirm(order["id"], 79000, "RUB", "fake")
    with pytest.raises(ShopError):
        shop.cancel(order["id"], 42)


def test_inactive_product(shop):
    with connect(shop.config.db_path) as conn:
        conn.execute("UPDATE products SET active=0 WHERE id=1")
    with pytest.raises(ShopError):
        shop.create_order(42, 1, "a@example.org")


async def test_order_snapshot(shop, order):
    with connect(shop.config.db_path) as conn:
        conn.execute("UPDATE products SET price=1,file='missing.txt' WHERE id=1")
    await shop.checkout(order["id"], 42)
    await shop.confirm(order["id"], 79000, "RUB", "fake")
    assert shop.order(order["id"])["amount"] == 79000


@pytest.mark.parametrize("name", ["../TASK.md", "/etc/passwd", "missing.zip"])
def test_bad_path(shop, name):
    with pytest.raises(ShopError):
        shop.file_path(name)


async def token_for(shop, order):
    await shop.checkout(order["id"], 42)
    await shop.confirm(order["id"], 79000, "RUB", "fake")
    return shop.sender.documents[0][2].rsplit("/", 1)[1]


async def test_download_limit(shop, order):
    token = await token_for(shop, order)
    assert len(token) >= 43
    for _ in range(5):
        assert shop.download(token).is_file()
    with pytest.raises(ShopError):
        shop.download(token)


async def test_expired_token(shop, order):
    token = await token_for(shop, order)
    with connect(shop.config.db_path) as conn:
        expires = conn.execute("SELECT expires_at FROM downloads WHERE token=?", (token,)).fetchone()[0]
    shop.clock = lambda: expires
    with pytest.raises(ShopError):
        shop.download(token)


def test_unknown_token(shop):
    with pytest.raises(ShopError):
        shop.download("foreign-token")


async def test_download_traversal(shop, order):
    token = await token_for(shop, order)
    with connect(shop.config.db_path) as conn:
        conn.execute("UPDATE orders SET file='../TASK.md'")
    with pytest.raises(ShopError):
        shop.download(token)


async def test_delivery_failure_can_retry(shop, order):
    await shop.checkout(order["id"], 42)
    original = shop.sender.document

    async def fail(*args):
        raise RuntimeError("network unavailable")

    shop.sender.document = fail
    with pytest.raises(RuntimeError):
        await shop.confirm(order["id"], 79000, "RUB", "fake")
    assert shop.order(order["id"])["status"] == "paid"
    with connect(shop.config.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM downloads").fetchone()[0] == 0
    shop.sender.document = original
    await shop.confirm(order["id"], 79000, "RUB", "fake")
    assert len(shop.sender.documents) == 1


async def test_revenue(shop, order):
    await token_for(shop, order)
    paid = shop.order(order["id"])["paid_at"]
    assert shop.revenue(paid, paid + 1) == 79000
    assert shop.revenue(paid + 1, paid + 2) == 0


async def test_simulate():
    assert await simulate()
