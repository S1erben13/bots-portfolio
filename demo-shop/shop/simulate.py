import argparse
import asyncio
import tempfile
from pathlib import Path

from shop.admin import create_app
from shop.config import Settings
from shop.seed_demo import seed_demo
from shop.service import Shop


class RecordingSender:
    def __init__(self):
        self.documents = []
        self.notifications = []

    async def document(self, user_id, path, url):
        self.documents.append((user_id, path, url))

    async def notify(self, chat_id, text):
        self.notifications.append((chat_id, text))


async def simulate():
    import httpx
    with tempfile.TemporaryDirectory() as folder:
        config = Settings(db_path=str(Path(folder) / "shop.sqlite3"),
                          files_root=str(Path(__file__).resolve().parent.parent / "demo-files"), payment_provider="fake")
        seed_demo(config)
        sender = RecordingSender()
        shop = Shop(config, sender)
        print("Каталог:", len(shop.products()), "товара")
        order = shop.create_order(123, 1, "demo@example.org")
        await shop.checkout(order["id"], 123)
        await shop.confirm(order["id"], 79000, "RUB", "fake")
        assert shop.order(order["id"])["status"] == "delivered"
        await shop.confirm(order["id"], 79000, "RUB", "fake")
        assert len(sender.documents) == 1
        token = sender.documents[0][2].rsplit("/", 1)[1]
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(shop)), base_url="http://test") as client:
            for _ in range(5):
                assert (await client.get(f"/d/{token}")).status_code == 200
            assert (await client.get(f"/d/{token}")).status_code == 404
        print("Покупка → fake-оплата → выдача: OK")
        print("Повторное уведомление: без дубля; 5 скачиваний: OK; 6-е отклонено")
        return True


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--auto", action="store_true", help="Прогон без ввода и без Telegram")
    parser.parse_args()
    asyncio.run(simulate())
