import asyncio
import json

import httpx
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice

from shop.payments import receipt, rub
from shop.service import ShopError


class Email(StatesGroup):
    value = State()


def keyboard(rows):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=label, callback_data=value)] for label, value in rows])


class TelegramSender:
    def __init__(self, bot):
        self.bot = bot

    async def document(self, user_id, path, url):
        if not self.bot:
            raise ShopError("Для выдачи в Telegram задайте BOT_TOKEN")
        await self.bot.send_document(user_id, FSInputFile(path), caption=f"Ваш файл. Резервная ссылка (72 ч, до 5 скачиваний):\n{url}")

    async def notify(self, chat_id, text):
        if not self.bot:
            raise ShopError("Telegram не подключён")
        await self.bot.send_message(chat_id, text)


def create_dispatcher(shop):
    router = Router()

    @router.message(CommandStart())
    async def start(message, state: FSMContext):
        await state.clear()
        await message.answer("Каталог цифровых товаров (демо)", reply_markup=keyboard([
            (f"{p['name']} — {rub(p['price'])} ₽", f"product:{p['id']}") for p in shop.products()]))

    @router.message(Command("orders"))
    async def orders(message, state: FSMContext):
        await state.clear()
        rows = [o for o in shop.orders(user_id=message.from_user.id) if o["status"] in {"paid", "delivered"}]
        await message.answer("Ваши оплаченные заказы" if rows else "Оплаченных заказов пока нет.",
                             reply_markup=keyboard([(f"№{o['id']}: {o['name']} — получить снова", f"repeat:{o['id']}") for o in rows]) if rows else None)

    @router.callback_query(F.data.startswith("product:"))
    async def product(query):
        pid = int(query.data.split(":")[1])
        item = next((p for p in shop.products() if p["id"] == pid), None)
        if item:
            await query.message.answer(f"{item['name']}\n{item['description']}\n{rub(item['price'])} ₽", reply_markup=keyboard([("Купить", f"buy:{pid}")]))
        await query.answer()

    @router.callback_query(F.data.startswith("buy:"))
    async def buy(query, state: FSMContext):
        await state.set_state(Email.value)
        await state.update_data(product_id=int(query.data.split(":")[1]))
        await query.message.answer("Введите email для чека. /start — отменить ввод.")
        await query.answer()

    @router.message(Email.value, F.text)
    async def email(message, state: FSMContext):
        try:
            data = await state.get_data()
            order = await asyncio.to_thread(shop.create_order, message.from_user.id, data["product_id"], message.text or "")
            result = await shop.checkout(order["id"], message.from_user.id)
        except (ShopError, ValueError, httpx.HTTPError):
            await message.answer("Не удалось создать оплату. Проверьте email и настройки платежей; /start — начать заново.")
            return
        await state.clear()
        if result["kind"] == "fake":
            await message.answer("Тестовая оплата: деньги не списываются.", reply_markup=keyboard([("Оплатить (тест)", f"fake:{order['id']}")]))
        elif result["kind"] == "telegram":
            await message.answer_invoice(title=order["name"][:32], description="Цифровой товар — демонстрация интеграции",
                                         payload=str(order["id"]), currency="RUB",
                                         prices=[LabeledPrice(label=order["name"][:32], amount=order["amount"])],
                                         provider_token=shop.config.payment_provider_token,
                                         provider_data=json.dumps({"receipt": receipt(order, shop.config)}, ensure_ascii=False),
                                         need_email=True, send_email_to_provider=True)
        else:
            await message.answer("Перейдите к оплате", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="Оплатить", url=result["url"])]]))

    @router.pre_checkout_query()
    async def pre_checkout(query):
        try:
            ok = shop.pre_checkout(int(query.invoice_payload), query.from_user.id, query.total_amount, query.currency)
        except ValueError:
            ok = False
        await query.answer(ok=ok, error_message=None if ok else "Заказ недоступен или сумма изменилась")

    @router.message(F.successful_payment)
    async def successful(message):
        payment = message.successful_payment
        try:
            await shop.confirm(int(payment.invoice_payload), payment.total_amount, payment.currency, "telegram", user_id=message.from_user.id)
        except (ShopError, ValueError):
            await message.answer("Платёж требует проверки администратором.")

    @router.callback_query(F.data.startswith("fake:"))
    async def fake(query):
        oid = int(query.data.split(":")[1])
        try:
            order = shop.order(oid, query.from_user.id)
            await shop.confirm(oid, order["amount"], "RUB", "fake", user_id=query.from_user.id)
            await query.answer("Оплата подтверждена")
        except ShopError:
            await query.answer("Заказ недоступен", show_alert=True)

    @router.callback_query(F.data.startswith("repeat:"))
    async def repeat(query):
        try:
            await shop.redeliver(int(query.data.split(":")[1]), query.from_user.id)
            await query.answer("Файл отправлен")
        except ShopError:
            await query.answer("Заказ недоступен", show_alert=True)

    dispatcher = Dispatcher()
    dispatcher.include_router(router)
    return dispatcher
