import asyncio

import uvicorn
from aiogram import Bot

from shop.admin import create_app
from shop.bot import TelegramSender, create_dispatcher
from shop.config import Settings
from shop.seed_demo import seed_demo
from shop.service import Shop


async def main():
    config = Settings()
    if not config.admin_password:
        raise ValueError("Задайте ADMIN_PASSWORD в .env")
    seed_demo(config)
    bot = Bot(config.bot_token) if config.bot_token else None
    shop = Shop(config, TelegramSender(bot))
    # Без access log: URL скачивания содержит секретный токен.
    server = uvicorn.Server(uvicorn.Config(create_app(shop), host="0.0.0.0", port=8040, access_log=False))
    tasks = [asyncio.create_task(server.serve())]
    if bot:
        tasks.append(asyncio.create_task(create_dispatcher(shop).start_polling(bot, handle_signals=False)))
    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        server.should_exit = True
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if bot:
            await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
