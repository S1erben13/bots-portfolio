import asyncio
import logging

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message

from app.leads import validate_lead


class LeadForm(StatesGroup):
    name = State()
    phone = State()


def make_dispatcher(consultant, leads):
    dp = Dispatcher(storage=MemoryStorage())

    @dp.message(CommandStart())
    async def start(message: Message, state: FSMContext):
        await state.clear()
        await message.answer("Здравствуйте! Я демо-консультант «Монтаж-Сервис». Задайте вопрос об услугах. /lead — заявка, /cancel — отмена. Контакты в демо вымышлены.")

    @dp.message(Command("cancel"))
    async def cancel(message: Message, state: FSMContext):
        await state.clear()
        await message.answer("Ввод заявки отменён. Можно задать вопрос.")

    @dp.message(Command("lead"))
    async def lead(message: Message, state: FSMContext):
        await state.set_state(LeadForm.name)
        await message.answer("Как к вам обращаться? Затем попросим телефон для обратного звонка. Отправляя данные, вы соглашаетесь на их сохранение для обработки заявки. /cancel — отмена.")

    @dp.message(LeadForm.name, F.text)
    async def name(message: Message, state: FSMContext):
        try:
            valid_name, _ = validate_lead(message.text, "+79990000000")
        except ValueError as exc:
            await message.answer(str(exc))
            return
        await state.update_data(name=valid_name)
        await state.set_state(LeadForm.phone)
        await message.answer("Введите телефон с кодом страны.")

    @dp.message(LeadForm.phone, F.text)
    async def phone(message: Message, state: FSMContext):
        data = await state.get_data()
        try:
            result = await leads.create(data["name"], message.text)
        except ValueError as exc:
            await message.answer(str(exc))
            return
        await state.clear()
        await message.answer(result["message"])

    @dp.message(F.text)
    async def question(message: Message):
        if len(message.text) > 2000:
            await message.answer("Сократите вопрос до 2000 символов.")
            return
        result = await consultant.answer(message.text)
        text = result["answer"]
        if result["sources"]:
            text += "\n\nИсточники: " + ", ".join(s["file"] for s in result["sources"])
        # Без parse_mode: документы и ввод пользователя не становятся разметкой.
        for offset in range(0, len(text), 4000):
            await message.answer(text[offset:offset + 4000])

    return dp


async def run_bot(token, consultant, leads):
    bot = Bot(token)
    try:
        await make_dispatcher(consultant, leads).start_polling(bot, handle_signals=False, close_bot_session=False)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logging.getLogger(__name__).error("Бот остановлен: %s", type(exc).__name__)
    finally:
        await bot.session.close()
