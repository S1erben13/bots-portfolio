import os
from dataclasses import dataclass, fields

@dataclass
class Settings:
    db_path: str = 'data/crm.sqlite3'
    hook_secret: str = 'demo-secret-change-me'
    admin_user: str = 'admin'
    admin_password: str = 'demo-password-change-me'
    crm_provider: str = 'fake'
    amo_base_url: str = ''
    amo_token: str = ''
    bitrix_webhook_url: str = ''
    telegram_token: str = ''
    telegram_chat_id: str = ''
    retry_base_seconds: float = 2.0
    max_attempts: int = 8
    poll_seconds: float = 1.0

    @classmethod
    def from_env(cls):
        defaults = cls()
        return cls(**{f.name: type(getattr(defaults, f.name))(os.getenv(f.name.upper(), getattr(defaults, f.name))) for f in fields(cls)})

    def validate(self):
        if not self.hook_secret or not self.admin_user or not self.admin_password:
            raise ValueError('Задайте секрет и учётные данные администратора')
        if self.crm_provider not in {'fake', 'amo', 'bitrix'}:
            raise ValueError('CRM_PROVIDER: fake, amo или bitrix')
        if self.crm_provider == 'amo' and (not self.amo_token or not self.amo_base_url.startswith('https://')):
            raise ValueError('Нужны AMO_TOKEN и HTTPS AMO_BASE_URL')
        if self.crm_provider == 'bitrix' and not self.bitrix_webhook_url.startswith('https://'):
            raise ValueError('Нужен HTTPS BITRIX_WEBHOOK_URL')
        if self.telegram_token and not self.telegram_chat_id:
            raise ValueError('Задайте TELEGRAM_CHAT_ID')
        if min(self.retry_base_seconds, self.max_attempts, self.poll_seconds) <= 0:
            raise ValueError('Интервалы и число попыток должны быть положительными')
