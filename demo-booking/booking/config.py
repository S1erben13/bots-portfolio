import os
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo


@dataclass
class Settings:
    db_path: str = os.getenv("DB_PATH", "data/booking.sqlite3")
    timezone: str = os.getenv("TIMEZONE", "Europe/Moscow")
    cancel_hours: int = int(os.getenv("CANCEL_HOURS", "3"))
    bot_token: str = os.getenv("BOT_TOKEN", "")
    admin_chat_id: str = os.getenv("ADMIN_CHAT_ID", "")
    admin_user: str = os.getenv("ADMIN_USER", "admin")
    admin_password: str = os.getenv("ADMIN_PASSWORD", "")

    def now(self):
        return datetime.now(ZoneInfo(self.timezone)).replace(tzinfo=None, microsecond=0)


settings = Settings()
