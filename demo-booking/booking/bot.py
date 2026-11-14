import asyncio
import logging

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove

from booking.db import connect
from booking.dialog import Dialog
from booking.service import BookingError

log = logging.getLogger(__name__)


def keyboard(rows):
    buttons = []
    for row in rows:
        buttons.append([InlineKeyboardButton(text=label, callback_data=data) for label, data in row])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


MENU = keyboard([[('Записаться', 'new'), ('Мои записи', 'mine')]])


def create_dispatcher(service):
    dp = Dispatcher()
    dp.message.filter(F.chat.type == "private")
    dp.callback_query.filter(F.message.chat.type == "private")
    dialogs = {}
    menu = keyboard([[("Заказать", "new"), ("Мои заказы", "mine")]]) if service.bakery else MENU

    async def show_dates(message, dialog):
        weekdays = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")
        buttons = []
        for day in service.dates():
            full = service.bakery and service.bakery.full(day, dialog.data.get("booking_id"))
            label = day.strftime("%d.%m") + " " + weekdays[day.weekday()]
            buttons.append((label + (" · заполнено" if full else ""), "full:" if full else f"date:{day}"))
        await message.answer("Выберите дату (14 дней)", reply_markup=keyboard([buttons[i:i+2] for i in range(0, 14, 2)]))

    async def show_masters(message):
        rows = [[("Любой мастер", "master:0")]]
        for row in service.masters():
            rows.append([(row["name"], f"master:{row['id']}")])
        await message.answer("Выберите мастера", reply_markup=keyboard(rows))

    @dp.message(CommandStart())
    @dp.message(Command("menu"))
    async def start(message):
        dialogs.pop(message.from_user.id, None)
        title = service.bakery.settings()["title"] if service.bakery else "Онлайн-запись"
        await message.answer(title + ". Выберите действие.", reply_markup=ReplyKeyboardRemove())
        await message.answer("Меню", reply_markup=menu)

    @dp.message(Command("id"))
    async def chat_id(message):
        await message.answer(f"Ваш chat_id: {message.chat.id}")

    @dp.callback_query()
    async def callback(query):
        await query.answer()
        uid = query.from_user.id
        data = query.data or ""
        message = query.message
        if message is None:
            return
        try:
            if data == "new":
                dialogs[uid] = Dialog(service, uid)
                rows = []
                for row in service.services():
                    label = f"{row['name']} · {row['price']} ₽" if service.bakery else f"{row['name']} · {row['duration']} мин."
                    rows.append([(label, f"service:{row['id']}")])
                await message.answer("Выберите товар" if service.bakery else "Выберите услугу", reply_markup=keyboard(rows))
                return
            if data == "mine":
                rows = service.bookings(user_id=uid)
                if not rows:
                    await message.answer("У вас пока нет заказов." if service.bakery else "У вас пока нет записей.", reply_markup=menu)
                for row in rows:
                    statuses = {"pending": "ожидает подтверждения", "confirmed": "подтверждена", "cancelled": "отменена", "attended": "пришёл", "absent": "не пришёл"}
                    text = f"№{row['id']} · {row['service']}\n{row['starts'].replace('T', ' ')} · {row['master']}\nСтатус: {statuses[row['status']]}"
                    if service.bakery:
                        statuses.update(confirmed="подтверждён", cancelled="отменён")
                        text = (f"Заказ №{row['id']} · {row['product_name']} — {row['product_price']} ₽ · 1 шт.\n"
                                f"Выдача: {row['starts'].replace('T', ' ')}\n"
                                f"Самовывоз: {service.bakery.settings()['pickup_address']}\nСтатус: {statuses[row['status']]}")
                    buttons = None
                    if row["status"] == "confirmed" or (service.bakery and row["status"] == "pending"):
                        buttons = keyboard([[("Отменить", f"cancelask:{row['id']}"), ("Перенести", f"move:{row['id']}")]])
                    await message.answer(text, reply_markup=buttons)
                return
            action, _, value = data.partition(":")
            if action == "full":
                raise BookingError("На эту дату лимит заказов исчерпан.")
            if action == "cancelask":
                await message.answer("Подтвердить отмену?", reply_markup=keyboard([[("Да, отменить", f"cancel:{int(value)}"), ("Назад", "mine")]]))
                return
            if action == "cancel":
                service.cancel(int(value), uid)
                await message.answer("Заказ отменён." if service.bakery else "Запись отменена.", reply_markup=menu)
                return
            if action == "move":
                booking = next((row for row in service.bookings(user_id=uid) if row["id"] == int(value)), None)
                if not booking:
                    raise BookingError("Запись не найдена.")
                dialog = Dialog(service, uid)
                dialog.move(booking)
                dialogs[uid] = dialog
                if service.bakery:
                    await show_dates(message, dialog)
                else:
                    await show_masters(message)
                return
            dialog = dialogs.get(uid)
            if not dialog:
                raise BookingError("Диалог истёк. Начните запись заново через /start.")
            if action == "service":
                dialog.choose_service(int(value))
                if service.bakery:
                    product = next(row for row in service.services() if row["id"] == int(value))
                    await message.answer(product["description"] or product["name"])
                    await show_dates(message, dialog)
                else:
                    await show_masters(message)
            elif action == "master":
                dialog.choose_master(int(value) or None)
                await show_dates(message, dialog)
            elif action == "date":
                slots = dialog.choose_date(value)
                if not slots:
                    dialog.step = "date"
                    raise BookingError("Нет свободных слотов. Выберите другую дату в календаре.")
                rows = []
                for slot in slots:
                    label = slot['starts'][11:] if service.bakery else f"{slot['starts'][11:]} · {slot['master']}"
                    rows.append([(label, f"slot:{slot['master_id']}|{slot['starts']}")])
                await message.answer("Свободное время", reply_markup=keyboard(rows))
            elif action == "slot":
                mid, starts = value.split("|")
                dialog.choose_slot(int(mid), starts)
                if dialog.step == "confirm":
                    await message.answer(dialog.summary(), reply_markup=keyboard([[("Подтвердить перенос", "confirm:")]]))
                else:
                    await message.answer("Как вас зовут?", reply_markup=ReplyKeyboardRemove())
            elif action == "confirm":
                moving = bool(dialog.data.get("booking_id"))
                bid = dialog.confirm()
                dialogs.pop(uid, None)
                text = f"Запись подтверждена. Номер: {bid}"
                if service.bakery:
                    text = f"Заказ перенесён. Номер: {bid}" if moving else f"Заказ №{bid} ожидает подтверждения продавцом."
                    text += f"\nСамовывоз: {service.bakery.settings()['pickup_address']}"
                await message.answer(text, reply_markup=menu)
        except (BookingError, ValueError) as exc:
            await message.answer(str(exc), reply_markup=menu)

    @dp.message(F.contact | F.text)
    async def details(message):
        dialog = dialogs.get(message.from_user.id)
        if not dialog:
            await message.answer("Начните с /start", reply_markup=menu)
            return
        try:
            if dialog.step == "name":
                dialog.set_name(message.text or "")
                contact = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="Поделиться контактом", request_contact=True)]], resize_keyboard=True, one_time_keyboard=True)
                await message.answer("Поделитесь своим контактом или введите телефон.", reply_markup=contact)
            elif dialog.step == "phone":
                if message.contact and message.contact.user_id != message.from_user.id:
                    raise BookingError("Пожалуйста, поделитесь своим контактом.")
                phone = message.contact.phone_number if message.contact else message.text
                dialog.set_phone(phone or "")
                await message.answer("Контакт сохранён.", reply_markup=ReplyKeyboardRemove())
                await message.answer(dialog.summary() + f"\n{dialog.data['name']} · {dialog.data['phone']}", reply_markup=keyboard([[("Подтвердить", "confirm:"), ("Начать заново", "new")]]))
            else:
                await message.answer("Выберите действие кнопкой или начните заново: /start")
        except BookingError as exc:
            await message.answer(str(exc))

    return dp


async def deliver_pending(bot, service):
    service.queue_reminders()
    with connect(service.config.db_path) as conn:
        ids = conn.execute("SELECT id FROM outbox WHERE sent=0 ORDER BY id LIMIT 100").fetchall()
    for item in ids:
        # После каждой сетевой отправки запись могла быть отменена или перенесена.
        with connect(service.config.db_path) as conn:
            row = conn.execute("SELECT * FROM outbox WHERE id=? AND sent=0", (item["id"],)).fetchone()
        if row is None:
            continue
        try:
            await bot.send_message(row["chat_id"], row["body"])
        except Exception:
            log.warning("Не удалось отправить уведомление id=%s", row["id"])
        else:
            with connect(service.config.db_path) as conn:
                conn.execute("UPDATE outbox SET sent=1 WHERE id=?", (row["id"],))


async def reminder_worker(bot, service):
    while True:
        try:
            await deliver_pending(bot, service)
        except Exception:
            log.exception("Ошибка фонового обработчика")
        await asyncio.sleep(30)


async def run_bot(service):
    bot = Bot(service.config.bot_token)
    worker = asyncio.create_task(reminder_worker(bot, service))
    try:
        await create_dispatcher(service).start_polling(bot, handle_as_tasks=False)
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        await bot.session.close()
