"""Демо-данные: компания, 2 объекта, 6 единиц оборудования, несколько заявок."""

import os
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from service.models import (
    Client,
    Company,
    Dispatcher,
    Equipment,
    Messenger,
    Site,
    Ticket,
    TicketStatusChange,
)

COMPANY = {"name": "ООО «Видеоконтроль-Сервис»", "phone": "+7 (900) 000-00-00"}

SITES = [
    {
        "name": "ЖК «Северный парк», корпус 2",
        "address": "ул. Лесная, 12к2",
        "contact": "УК «Парк», диспетчер",
        "equipment": [
            ("NP2CAM", Equipment.Kind.CAMERA, "Камера у подъезда 1", "Козырёк подъезда 1", "Hikvision DS-2CD2043"),
            ("NP2DMF", Equipment.Kind.INTERCOM, "Домофон подъезда 1", "Входная группа", "Beward DKS15122"),
            ("NP2BAR", Equipment.Kind.BARRIER, "Шлагбаум въезда во двор", "Въезд с ул. Лесной", "CAME GARD 4000"),
        ],
    },
    {
        "name": "Бизнес-центр «Альфа»",
        "address": "пр. Мира, 45",
        "contact": "Администратор БЦ, Ольга",
        "equipment": [
            ("BCACAM", Equipment.Kind.CAMERA, "Камера парковки", "Парковка, столб 3", "Dahua IPC-HFW2431"),
            ("BCASKD", Equipment.Kind.ACCESS, "Турникет на ресепшн", "1 этаж, вход", "PERCo TTD-03"),
            ("BCANVR", Equipment.Kind.RECORDER, "Видеорегистратор в серверной", "Серверная, стойка 1", "Hikvision DS-7616NI"),
        ],
    },
]

# (код оборудования, описание, путь статусов, сколько часов назад создана, клиент)
TICKETS = [
    ("NP2CAM", "Камера не показывает картинку, на мониторе консьержа чёрный экран.", ["new"], 1, 0),
    ("NP2DMF", "Домофон не открывает дверь по ключу, по коду открывает.", ["new", "accepted"], 5, 1),
    ("NP2BAR", "Шлагбаум не закрывается после проезда машины.", ["new", "accepted", "en_route"], 20, 0),
    ("BCASKD", "Турникет не читает пропуска сотрудников.", ["new", "accepted", "en_route", "done"], 50, 2),
    ("BCACAM", "Изображение с камеры парковки размытое после дождя.", ["new", "cancelled"], 80, 2),
]

CLIENTS = [
    {"messenger": Messenger.CONSOLE, "external_id": "demo-anna", "name": "Анна (демо)", "phone": "+7 900 111-22-33"},
    {"messenger": Messenger.CONSOLE, "external_id": "demo-petr", "name": "Пётр Сергеевич (демо)"},
    {"messenger": Messenger.CONSOLE, "external_id": "demo-olga", "name": "Ольга, администратор БЦ (демо)"},
]


class Command(BaseCommand):
    help = "Заполняет базу демо-данными (повторный запуск ничего не дублирует)."

    @transaction.atomic
    def handle(self, *args, **options):
        self._ensure_admin()
        company, created = Company.objects.get_or_create(name=COMPANY["name"], defaults=COMPANY)
        user, user_created = get_user_model().objects.get_or_create(username="demo-dispatcher")
        if user_created:
            user.set_unusable_password()
            user.save(update_fields=["password"])
        dispatcher, _ = Dispatcher.objects.get_or_create(
            company=company, messenger=Messenger.CONSOLE, user=user)
        if not dispatcher.chat_id:
            if not dispatcher.link_code or dispatcher.link_code_expires_at <= timezone.now():
                dispatcher.renew_link_code()
            self.stdout.write(f"Демо-диспетчер: /dispatcher {dispatcher.link_code} (24 часа, console)")
        else:
            self.stdout.write("Демо-диспетчер уже привязан к консоли")
        if not created:
            self.stdout.write("Демо-данные уже есть — пропускаю.")
            return

        for site_data in SITES:
            items = site_data["equipment"]
            site = Site.objects.create(
                company=company, **{k: v for k, v in site_data.items() if k != "equipment"}
            )
            for code, kind, name, location, model_name in items:
                Equipment.objects.create(
                    site=site, code=code, kind=kind, name=name, location=location, model_name=model_name
                )

        clients = [Client.objects.create(**data) for data in CLIENTS]
        now = timezone.now()
        technician = "Иванов С."
        for code, description, path, hours_ago, client_idx in TICKETS:
            equipment = Equipment.objects.select_related("site").get(code=code)
            ticket = Ticket.objects.create(
                company=company,
                site=equipment.site,
                equipment=equipment,
                client=clients[client_idx],
                description=description,
                status=path[-1],
                technician=technician if "en_route" in path else "",
            )
            created_at = now - timedelta(hours=hours_ago)
            Ticket.objects.filter(pk=ticket.pk).update(created_at=created_at)
            previous = ""
            for step, status in enumerate(path):
                change = TicketStatusChange.objects.create(
                    ticket=ticket, old_status=previous, new_status=status
                )
                TicketStatusChange.objects.filter(pk=change.pk).update(
                    created_at=created_at + timedelta(hours=step)
                )
                previous = status

        self.stdout.write(
            self.style.SUCCESS(
                f"Создано: объектов {Site.objects.count()}, оборудования {Equipment.objects.count()}, "
                f"заявок {Ticket.objects.count()}."
            )
        )
        self.stdout.write("Коды для теста: " + ", ".join(e[0] for s in SITES for e in s["equipment"]))

    def _ensure_admin(self):
        username = os.environ.get("DEMO_ADMIN_USERNAME")
        password = os.environ.get("DEMO_ADMIN_PASSWORD")
        if not username or not password:
            return
        User = get_user_model()
        if not User.objects.filter(username=username).exists():
            User.objects.create_superuser(username=username, password=password, email="")
            self.stdout.write(f"Создан администратор «{username}» (пароль из DEMO_ADMIN_PASSWORD).")
