import os
from dataclasses import dataclass, field


def env(name, default=""):
    return field(default_factory=lambda: os.getenv(name, default))


@dataclass
class Settings:
    db_path: str = env("DB_PATH", "data/shop.sqlite3")
    files_root: str = env("FILES_ROOT", "demo-files")
    bot_token: str = env("BOT_TOKEN")
    payment_provider: str = env("PAYMENT_PROVIDER", "fake")
    payment_provider_token: str = env("PAYMENT_PROVIDER_TOKEN")
    yookassa_shop_id: str = env("YOOKASSA_SHOP_ID")
    yookassa_secret_key: str = env("YOOKASSA_SECRET_KEY")
    admin_user: str = env("ADMIN_USER", "admin")
    admin_password: str = env("ADMIN_PASSWORD")
    admin_chat_id: str = env("ADMIN_CHAT_ID")
    public_url: str = env("PUBLIC_URL", "http://localhost:8040")
    payment_return_url: str = env("PAYMENT_RETURN_URL", "http://localhost:8040/health")
    vat_code: int = field(default_factory=lambda: int(os.getenv("VAT_CODE", "1")))
    payment_subject: str = env("PAYMENT_SUBJECT", "service")

    def __post_init__(self):
        if self.payment_provider not in {"fake", "telegram", "yookassa"}:
            raise ValueError("Неизвестный провайдер")
        if self.vat_code not in range(1, 13) or self.payment_subject not in {"service", "commodity"}:
            raise ValueError("Проверьте настройки чека")
