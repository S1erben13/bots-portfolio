"""Админка, наклейки и страница QR."""

import io
import re

import pytest
from django.core.management import call_command
from PIL import Image

from service import qr, services
from service.models import Equipment, Notification, Ticket


@pytest.fixture
def staff(client, django_user_model):
    user = django_user_model.objects.create_superuser("boss", password="pass12345")
    client.force_login(user)
    return user


def test_sticker_png_is_valid_image(equipment):
    image = Image.open(io.BytesIO(qr.sticker_png(equipment)))
    assert image.format == "PNG" and image.size == (600, 860)


def test_stickers_pdf_multiple_pages(site):
    for i in range(10):
        Equipment.objects.create(site=site, name=f"Камера {i}")
    pdf = qr.stickers_pdf(site.equipment.select_related("site__company"))
    assert pdf.startswith(b"%PDF")
    assert len(re.findall(rb"/Type /Page\b(?!s)", pdf)) == 2  # 10 наклеек, по 9 на лист


def test_qr_link_modes(equipment, settings):
    from service.links import qr_link

    settings.PUBLIC_BASE_URL = "https://demo.example"
    settings.QR_TARGET = "landing"
    assert qr_link(equipment) == "https://demo.example/q/TEST01/"
    settings.QR_TARGET = "telegram"
    settings.TELEGRAM_BOT_USERNAME = "svc_bot"
    assert qr_link(equipment) == "https://t.me/svc_bot?start=TEST01"
    settings.QR_TARGET = "max"
    settings.MAX_BOT_NAME = ""
    assert qr_link(equipment) == "https://demo.example/q/TEST01/"  # нет имени — запасной вариант


def test_landing_page(client, equipment, settings):
    settings.TELEGRAM_BOT_USERNAME = "svc_bot"
    settings.MAX_BOT_NAME = "svc_max"
    response = client.get("/q/test01/")
    body = response.content.decode()
    assert response.status_code == 200
    assert "Камера у входа" in body
    assert "https://t.me/svc_bot?start=TEST01" in body
    assert "https://max.ru/svc_max?start=TEST01" in body
    assert client.get("/q/NOPE00/").status_code == 404


def test_stickers_require_staff(client, equipment):
    response = client.get(f"/stickers/equipment/{equipment.pk}.png")
    assert response.status_code == 302


def test_sticker_views(client, staff, equipment):
    png = client.get(f"/stickers/equipment/{equipment.pk}.png")
    assert png.status_code == 200 and png["Content-Type"] == "image/png"
    pdf = client.get(f"/stickers/site/{equipment.site.pk}.pdf")
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")


def test_admin_pages_render(client, staff, equipment, client_obj):
    ticket = services.create_ticket(equipment=equipment, client=client_obj, description="x")
    services.change_status(ticket, Ticket.Status.ACCEPTED)
    for url in [
        "/admin/",
        "/admin/service/ticket/",
        f"/admin/service/ticket/{ticket.pk}/change/",
        "/admin/service/ticket/add/",
        "/admin/service/site/",
        f"/admin/service/site/{equipment.site.pk}/change/",
        "/admin/service/equipment/",
        f"/admin/service/equipment/{equipment.pk}/change/",
        "/admin/service/client/",
        "/admin/service/notification/",
        "/admin/service/company/",
    ]:
        assert client.get(url).status_code == 200, url


def test_admin_status_change_notifies_client(client, staff, equipment, client_obj):
    ticket = services.create_ticket(equipment=equipment, client=client_obj, description="Сломано")
    response = client.post(
        f"/admin/service/ticket/{ticket.pk}/change/",
        {
            "status": "en_route",
            "status_comment": "Будем через час",
            "technician": "Сидоров",
            "site": equipment.site.pk,
            "equipment": equipment.pk,
            "description": "Сломано",
            "client": client_obj.pk,
            "internal_note": "",
            "photos-TOTAL_FORMS": "0",
            "photos-INITIAL_FORMS": "0",
            "history-TOTAL_FORMS": "1",
            "history-INITIAL_FORMS": "1",
            "history-0-id": ticket.history.first().pk,
            "history-0-ticket": ticket.pk,
            "notifications-TOTAL_FORMS": "0",
            "notifications-INITIAL_FORMS": "0",
        },
    )
    assert response.status_code == 302, response.content.decode()[:2000]
    ticket.refresh_from_db()
    assert ticket.status == "en_route"
    assert ticket.technician == "Сидоров"
    last = ticket.history.last()
    assert (last.old_status, last.new_status, last.changed_by) == ("new", "en_route", staff)
    note = Notification.objects.get()
    assert "техник выехал" in note.text
    assert "Техник: Сидоров" in note.text
    assert "Будем через час" in note.text


def test_admin_bulk_action(client, staff, equipment, client_obj):
    t1 = services.create_ticket(equipment=equipment, client=client_obj, description="a")
    t2 = services.create_ticket(equipment=equipment, client=client_obj, description="b")
    response = client.post(
        "/admin/service/ticket/",
        {"action": "mark_accepted", "_selected_action": [t1.pk, t2.pk]},
    )
    assert response.status_code == 302
    assert set(Ticket.objects.values_list("status", flat=True)) == {"accepted"}
    assert Notification.objects.count() == 2


def test_admin_add_ticket_by_phone(client, staff, equipment):
    response = client.post(
        "/admin/service/ticket/add/",
        {
            "status": "new",
            "site": equipment.site.pk,
            "equipment": equipment.pk,
            "description": "Позвонили по телефону",
            "photos-TOTAL_FORMS": "0",
            "photos-INITIAL_FORMS": "0",
            "history-TOTAL_FORMS": "0",
            "history-INITIAL_FORMS": "0",
            "notifications-TOTAL_FORMS": "0",
            "notifications-INITIAL_FORMS": "0",
        },
    )
    assert response.status_code == 302, response.content.decode()[:2000]
    ticket = Ticket.objects.get()
    assert ticket.company == equipment.site.company
    assert ticket.history.count() == 1


def test_seed_demo_is_idempotent(db):
    call_command("seed_demo")
    call_command("seed_demo")
    assert Equipment.objects.count() == 6
    assert Ticket.objects.count() == 5
    assert Notification.objects.count() == 0


def test_admin_stale_form_does_not_revert_status(client, staff, equipment, client_obj):
    """Диспетчер А открыл форму, Б успел сменить статус; А сохраняет заметку — статус Б остаётся."""
    ticket = services.create_ticket(equipment=equipment, client=client_obj, description="Сломано")
    form_data = {
        "status": "new",  # устаревшее значение из открытой формы
        "status_seen": "new",
        "status_comment": "",
        "technician": "",
        "site": equipment.site.pk,
        "equipment": equipment.pk,
        "description": "Сломано",
        "client": client_obj.pk,
        "internal_note": "перезвонить",
        "photos-TOTAL_FORMS": "0",
        "photos-INITIAL_FORMS": "0",
        "history-TOTAL_FORMS": "0",
        "history-INITIAL_FORMS": "0",
        "notifications-TOTAL_FORMS": "0",
        "notifications-INITIAL_FORMS": "0",
    }
    services.change_status(ticket, Ticket.Status.ACCEPTED)  # действие диспетчера Б
    response = client.post(f"/admin/service/ticket/{ticket.pk}/change/", form_data)
    assert response.status_code == 302
    ticket.refresh_from_db()
    assert ticket.status == "accepted"
    assert ticket.internal_note == "перезвонить"
    assert ticket.history.count() == 2
