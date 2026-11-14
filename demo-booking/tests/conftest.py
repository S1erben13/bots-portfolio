from datetime import datetime

import pytest

from booking.config import Settings
from booking.seed_demo import seed_demo
from booking.service import BookingService


@pytest.fixture
def system(tmp_path):
    config = Settings(mode="salon", db_path=str(tmp_path / "test.sqlite3"), admin_password="test-password", admin_chat_id="999")
    clock = [datetime(2026, 11, 16, 8, 0)]  # Понедельник
    seed_demo(config)
    service = BookingService(config, clock=lambda: clock[0])
    return service, clock
