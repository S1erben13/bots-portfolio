from pathlib import Path

import pytest

from shop.config import Settings
from shop.seed_demo import seed_demo
from shop.service import Shop
from shop.simulate import RecordingSender


@pytest.fixture
def shop(tmp_path):
    config = Settings(db_path=str(tmp_path / "shop.sqlite3"),
                      files_root=str(Path(__file__).resolve().parent.parent / "demo-files"),
                      payment_provider="fake", admin_password="test-password", admin_chat_id="999")
    seed_demo(config)
    return Shop(config, RecordingSender())


@pytest.fixture
def order(shop):
    return shop.create_order(42, 1, "buyer@example.org")
