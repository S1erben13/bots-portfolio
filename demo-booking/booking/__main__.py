import asyncio
import logging

import uvicorn

from booking.admin import create_app
from booking.bot import run_bot
from booking.config import settings
from booking.seed_demo import seed_demo
from booking.service import BookingService


async def main():
    logging.basicConfig(level=logging.INFO)
    if not settings.admin_password:
        raise SystemExit("Задайте ADMIN_PASSWORD в .env или окружении.")
    if settings.cancel_hours < 0:
        raise SystemExit("CANCEL_HOURS не может быть отрицательным.")
    seed_demo()
    service = BookingService()
    server = uvicorn.Server(uvicorn.Config(create_app(service), host="0.0.0.0", port=8030, log_level="info"))
    web = asyncio.create_task(server.serve())
    bot = None
    if settings.bot_token:
        bot = asyncio.create_task(run_bot(service))
    else:
        logging.info("BOT_TOKEN не задан: работает только веб-админка; доступен simulate.")
    tasks = [web] + ([bot] if bot else [])
    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        server.should_exit = True
        if bot:
            bot.cancel()
            await asyncio.gather(bot, return_exceptions=True)
        await web


if __name__ == "__main__":
    asyncio.run(main())
