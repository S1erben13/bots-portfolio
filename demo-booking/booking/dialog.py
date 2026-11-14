"""Общая модель диалога для Telegram и консольного адаптера."""
from datetime import date

from booking.service import BookingError


class Dialog:
    def __init__(self, service, user_id):
        self.service = service
        self.user_id = user_id
        self.data = {}
        self.step = "service"

    def choose_service(self, service_id):
        if service_id not in [row["id"] for row in self.service.services()]:
            raise BookingError("Выберите доступный товар." if self.service.bakery else "Выберите доступную услугу.")
        self.data = {"service_id": service_id}
        self.step = "date" if self.service.bakery else "master"
        if self.service.bakery:
            self.data["master_id"] = 0

    def move(self, booking):
        if booking["user_id"] != self.user_id:
            raise BookingError("Запись не найдена.")
        self.data = {"service_id": booking["service_id"], "booking_id": booking["id"]}
        self.step = "date" if self.service.bakery else "master"
        if self.service.bakery:
            self.data.update(master_id=0, product_name=booking["product_name"], product_price=booking["product_price"])

    def choose_master(self, master_id):
        if self.step != "master":
            raise BookingError("Сначала выберите услугу.")
        if master_id and master_id not in [row["id"] for row in self.service.masters()]:
            raise BookingError("Выберите доступного мастера.")
        self.data["master_id"] = master_id
        self.step = "date"

    def choose_date(self, day):
        if self.step != "date":
            raise BookingError("Сначала выберите товар." if self.service.bakery else "Сначала выберите мастера.")
        day = date.fromisoformat(day)
        if day not in self.service.dates():
            raise BookingError("Выберите дату из календаря.")
        if self.service.bakery and self.service.bakery.full(day, self.data.get("booking_id")):
            raise BookingError("На эту дату лимит заказов исчерпан. Выберите другую дату.")
        self.data["day"] = day
        self.step = "slot"
        return self.slots()

    def slots(self):
        return self.service.slots(self.data["service_id"], self.data["master_id"], self.data["day"], self.data.get("booking_id"))

    def choose_slot(self, master_id, starts):
        if self.step != "slot":
            raise BookingError("Сначала выберите дату.")
        matches = [slot for slot in self.slots() if slot["master_id"] == master_id and slot["starts"] == starts]
        if not matches:
            raise BookingError("Слот больше недоступен.")
        self.data["slot"] = matches[0]
        self.step = "confirm" if self.data.get("booking_id") else "name"

    def set_name(self, name):
        if self.step != "name" or not 2 <= len(name.strip()) <= 100:
            raise BookingError("Введите имя длиной от 2 до 100 символов.")
        self.data["name"] = name.strip()
        self.step = "phone"

    def set_phone(self, phone):
        import re
        phone = re.sub(r"[\s()\-]", "", phone)
        if self.step != "phone" or not re.fullmatch(r"\+?\d{10,15}", phone):
            raise BookingError("Введите телефон: от 10 до 15 цифр, можно с +.")
        self.data["phone"] = phone
        self.step = "confirm"

    def summary(self):
        slot = self.data["slot"]
        if self.service.bakery:
            if self.data.get("booking_id"):
                title, price = self.data["product_name"], self.data["product_price"]
            else:
                product = next((row for row in self.service.services() if row["id"] == self.data["service_id"]), None)
                if product is None:
                    raise BookingError("Товар больше недоступен. Начните заказ заново.")
                title, price = product["name"], product["price"]
            address = self.service.bakery.settings()["pickup_address"]
            return f"{title} — {price} ₽ · 1 шт.\nВыдача: {slot['starts'].replace('T', ' ')}\nСамовывоз: {address}"
        title = next((row["name"] for row in self.service.services() if row["id"] == self.data["service_id"]), None)
        if title is None:
            raise BookingError("Услуга больше недоступна. Начните запись заново.")
        return f"{title}\nМастер: {slot['master']}\nДата: {slot['starts'].replace('T', ' ')}"

    def confirm(self):
        if self.step != "confirm":
            raise BookingError("Сначала заполните данные записи.")
        slot = self.data["slot"]
        bid = self.data.get("booking_id")
        if bid:
            self.service.reschedule(bid, self.user_id, slot["master_id"], slot["starts"])
        else:
            bid = self.service.book(self.user_id, self.data["service_id"], slot["master_id"], slot["starts"], self.data["name"], self.data["phone"])
        self.step = "done"
        return bid
