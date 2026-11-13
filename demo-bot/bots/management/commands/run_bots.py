import asyncio
import logging

from django.conf import settings
from django.core.management.base import BaseCommand

from bots.adapters.base import MessengerAdapter
from bots.adapters.console import ConsoleAdapter

log = logging.getLogger("bots")

RESTART_DELAY = 15


def _make_telegram() -> MessengerAdapter:
    from bots.adapters.telegram import TelegramAdapter

    return TelegramAdapter(settings.TELEGRAM_BOT_TOKEN)


def _make_max() -> MessengerAdapter:
    from bots.adapters.max import MaxAdapter

    return MaxAdapter(settings.MAX_BOT_TOKEN, settings.MAX_API_URL)


def build_adapters() -> list[MessengerAdapter]:
    """Каждый мессенджер создаётся независимо: ошибка в токене Telegram не выключает MAX."""
    adapters: list[MessengerAdapter] = [ConsoleAdapter()]
    channels = [
        ("Telegram", "TELEGRAM_BOT_TOKEN", settings.TELEGRAM_BOT_TOKEN, _make_telegram),
        ("MAX", "MAX_BOT_TOKEN", settings.MAX_BOT_TOKEN, _make_max),
    ]
    for title, env_name, token, factory in channels:
        if not token:
            log.warning("%s не задан — %s-бот выключен", env_name, title)
            continue
        try:
            adapters.append(factory())
        except Exception:
            log.exception("%s-бот НЕ запущен: проверьте %s в .env", title, env_name)
    return adapters


async def supervise(adapter: MessengerAdapter) -> None:
    """Падение одного мессенджера не останавливает остальные; упавший перезапускается."""
    while True:
        try:
            await adapter.run()
        except Exception:
            log.exception("[%s] адаптер упал, перезапуск через %s с", adapter.messenger, RESTART_DELAY)
        await asyncio.sleep(RESTART_DELAY)


class Command(BaseCommand):
    help = "Запускает ботов (Telegram/MAX по токенам из .env) и рассылку уведомлений."

    def handle(self, *args, **options):
        adapters = build_adapters()
        log.info("Запуск адаптеров: %s", ", ".join(a.messenger for a in adapters))
        asyncio.run(self._main(adapters))

    async def _main(self, adapters):
        await asyncio.gather(*(supervise(a) for a in adapters))
