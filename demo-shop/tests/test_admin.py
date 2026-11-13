import re

import httpx
import pytest

from shop.admin import create_app
from shop.db import connect


@pytest.fixture
async def client(shop):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(shop)), base_url="http://test") as client:
        yield client


@pytest.mark.parametrize("path", ["/admin", "/admin/catalog", "/admin/export"])
async def test_auth_required(client, path):
    assert (await client.get(path)).status_code == 401
    assert (await client.get(path, auth=("admin", "wrong"))).status_code == 401


async def test_admin_and_csv(client, shop, order):
    auth = ("admin", "test-password")
    assert (await client.get("/admin", auth=auth)).status_code == 200
    assert (await client.get("/admin/catalog", auth=auth)).status_code == 200
    csv = await client.get("/admin/export", auth=auth)
    assert csv.status_code == 200 and "79000" in csv.text
    assert "buyer@example.org" not in csv.text


async def test_csv_formula(client, shop):
    with connect(shop.config.db_path) as conn:
        conn.execute("UPDATE products SET name='=1+1' WHERE id=1")
    shop.create_order(42, 1, "a@example.org")
    response = await client.get("/admin/export", auth=("admin", "test-password"))
    assert "'=1+1" in response.text


async def test_status_filter(client, shop, order):
    response = await client.get("/admin?status=paid", auth=("admin", "test-password"))
    assert "Набор файлов" not in response.text
    assert (await client.get("/admin?status=invalid", auth=("admin", "test-password"))).status_code == 400


async def test_csrf(client):
    response = await client.post("/admin/product", auth=("admin", "test-password"), data={"name": "Bad"})
    assert response.status_code == 403


async def test_create_product(client, shop):
    auth = ("admin", "test-password")
    response = await client.get("/admin/catalog", auth=auth)
    csrf = re.search(r'name="csrf" type="hidden" value="([^"]+)"', response.text).group(1)
    data = {"csrf": csrf, "name": "Новый товар", "description": "Описание", "price": "500", "active": "1", "file": "checklist-demo.txt"}
    assert (await client.post("/admin/product", auth=auth, data=data)).status_code == 303
    assert len(shop.products()) == 3
    data["file"] = "../TASK.md"
    assert (await client.post("/admin/product", auth=auth, data=data)).status_code == 400


async def test_download_http(client, shop, order):
    await shop.checkout(order["id"], 42)
    await shop.confirm(order["id"], 79000, "RUB", "fake")
    token = shop.sender.documents[0][2].rsplit("/", 1)[1]
    for _ in range(5):
        response = await client.get(f"/d/{token}")
        assert response.status_code == 200 and response.content.startswith(b"PK")
    assert (await client.get(f"/d/{token}")).status_code == 404
    assert (await client.get("/d/foreign-token")).status_code == 404


async def test_fake_webhook_disabled(client):
    response = await client.post("/hooks/yookassa", json={"object": {"id": "fake-1"}})
    assert response.status_code == 400


async def test_manual_redelivery(client, shop, order):
    auth = ("admin", "test-password")
    await shop.checkout(order["id"], 42)
    await shop.confirm(order["id"], 79000, "RUB", "fake")
    response = await client.get("/admin", auth=auth)
    csrf = re.search(r'name="csrf" type="hidden" value="([^"]+)"', response.text).group(1)
    response = await client.post("/admin/redeliver", auth=auth, data={"csrf": csrf, "id": order["id"]})
    assert response.status_code == 303
    assert len(shop.sender.documents) == 2
