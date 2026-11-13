"""Диалог с ботом прямо в терминале — без Telegram/MAX и без токенов.

  python manage.py simulate_chat            — интерактивно
  python manage.py simulate_chat --script   — прогнать весь сценарий автоматически
"""

import asyncio
import io

from django.core.management.base import BaseCommand, CommandError
from PIL import Image, ImageDraw

from bots.adapters.console import ConsoleAdapter, render
from bots.types import CB_MY_TICKETS, CB_SKIP_PHOTO, IncomingEvent, IncomingPhoto
from service import services
from service.models import Dispatcher, Equipment, Messenger, Notification, Ticket

USER_ID = "demo-user-1"


def fake_photo() -> IncomingPhoto:
    img = Image.new("RGB", (640, 480), "#30343a")
    ImageDraw.Draw(img).text((20, 20), "Тестовое фото: нет картинки с камеры", fill="white")
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return IncomingPhoto(buf.getvalue(), "demo.jpg")


class Command(BaseCommand):
    help = "Имитация чата клиента с ботом в консоли (для демо без токенов)."

    def add_arguments(self, parser):
        parser.add_argument("--script", action="store_true", help="Автоматический сценарий")
        parser.add_argument("--code", help="Код оборудования (по умолчанию — первый из базы)")

    def handle(self, *args, **options):
        self.adapter = ConsoleAdapter(echo=False)
        last = Notification.objects.order_by("-pk").first()
        self._last_id = last.pk if last else 0
        code = options["code"]
        if not code:
            first = Equipment.objects.filter(is_active=True).order_by("pk").first()
            if not first:
                raise CommandError("Нет оборудования. Сначала: python manage.py seed_demo")
            code = first.code
        if options["script"]:
            self.run_script(code)
        else:
            self.run_interactive(code)

    # --- вывод ---

    def say(self, event: IncomingEvent) -> None:
        shown = event.text or event.callback or f"/start {event.start_payload}"
        if event.photos:
            shown = f"[фото] {shown if event.text else ''}".strip()
        self.stdout.write(self.style.HTTP_INFO(f"\n👤 Клиент: {shown}"))
        for reply in asyncio.run(self.adapter.process(event)):
            self.stdout.write(self.style.SUCCESS("🤖 Бот: ") + render(reply))

    def event(self, **kwargs) -> IncomingEvent:
        return IncomingEvent(messenger=Messenger.CONSOLE, user_id=USER_ID, name="Демо-клиент", **kwargs)

    def deliver(self) -> None:
        # Уведомление мог раньше нас забрать запущенный контейнер `bot` — тогда
        # показываем его из очереди, а не из нашего адаптера.
        asyncio.run(self.adapter.deliver_notifications_once())
        fresh = Notification.objects.filter(
            client__messenger=Messenger.CONSOLE, client__external_id=USER_ID, pk__gt=self._last_id
        ).order_by("pk")
        for item in fresh:
            self.stdout.write(self.style.WARNING("🔔 Уведомление: ") + item.text)
            self._last_id = item.pk

    # --- режимы ---

    def run_script(self, code: str) -> None:
        equipment = Equipment.objects.select_related("site").get(code=code)
        dispatcher = Dispatcher.objects.filter(company=equipment.site.company,
            messenger=Messenger.CONSOLE, is_active=True, user__is_active=True).first()
        if dispatcher is None:
            raise CommandError("Нет демо-диспетчера. Сначала: python manage.py seed_demo")
        if not dispatcher.chat_id:
            dispatcher.renew_link_code()
            event = IncomingEvent(messenger=Messenger.CONSOLE, user_id="demo-dispatcher",
                                  text=f"/dispatcher {dispatcher.link_code}")
            for reply in asyncio.run(self.adapter.process(event)):
                self.stdout.write(render(reply))
            dispatcher.refresh_from_db()
        self.stdout.write(f"Сценарий: клиент сканирует QR с кодом {code}")
        self.say(self.event(start_payload=code))
        self.say(self.event(text="Камера не показывает картинку со вчерашнего вечера"))
        self.say(self.event(text="", photos=(fake_photo(),)))
        self.say(self.event(callback=CB_MY_TICKETS))

        ticket = Ticket.objects.filter(client__external_id=USER_ID).latest("created_at")
        self.deliver()
        from bots.types import Button, Reply
        for note in ticket.notifications.filter(dispatcher=dispatcher):
            self.stdout.write("🔔 Диспетчеру: " + render(Reply(note.text,
                [[Button(**b) for b in row] for row in note.buttons])))
        self.stdout.write("\n🧑‍💼 Диспетчер нажимает «Принять»")
        for reply in asyncio.run(self.adapter.process(IncomingEvent(
                messenger=Messenger.CONSOLE, user_id=dispatcher.chat_id,
                callback=f"ds:{ticket.pk}:accepted"))):
            self.stdout.write(render(reply))
        self.deliver()
        ticket.technician = "Иванов С."
        ticket.save(update_fields=["technician"])
        for status in (Ticket.Status.EN_ROUTE, Ticket.Status.DONE):
            self.stdout.write(f"\n🧑‍💼 Диспетчер ставит статус «{Ticket.Status(status).label}»")
            services.change_status(ticket, status)
            self.deliver()
        self.stdout.write(self.style.SUCCESS(f"\nГотово. Заявка №{ticket.number} видна в админке."))

    def run_interactive(self, code: str) -> None:
        self.stdout.write(
            "Пишите как клиент. Команды: /start <код>, photo — прислать фото, "
            "skip — «Без фото», /my, /cancel, exit.\n"
            f"Для начала попробуйте: /start {code}"
        )
        while True:
            try:
                line = input("> ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if line in {"exit", "quit"}:
                break
            if line == "photo":
                self.say(self.event(text="", photos=(fake_photo(),)))
            elif line == "skip":
                self.say(self.event(callback=CB_SKIP_PHOTO))
            else:
                self.say(self.event(text=line))
            self.deliver()
