import pytest

from service import services
from service.models import Notification, Ticket


def test_create_ticket_by_code(equipment, client_obj):
    ticket = services.create_ticket(equipment="test01 ", client=client_obj, description=" Не работает ")

    assert ticket.equipment == equipment
    assert ticket.site == equipment.site
    assert ticket.company == equipment.site.company
    assert ticket.client == client_obj
    assert ticket.description == "Не работает"
    assert ticket.status == Ticket.Status.NEW
    assert list(ticket.history.values_list("old_status", "new_status")) == [("", "new")]
    assert ticket.number == f"{ticket.pk:05d}"


def test_create_ticket_with_photo(equipment, client_obj, photo):
    ticket = services.create_ticket(
        equipment=equipment,
        client=client_obj,
        description="Сломано",
        photos=[services.PhotoUpload(photo.content, "p.jpg")],
    )
    assert ticket.photos.count() == 1
    stored = ticket.photos.first().image
    assert stored.name.endswith(".jpg")
    assert stored.read()[:3] == b"\xff\xd8\xff"  # перекодировано в JPEG


@pytest.mark.parametrize(
    "payload",
    [
        b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
        b"<html><script>alert(1)</script></html>",
        b"",
    ],
)
def test_non_image_upload_rejected(equipment, client_obj, payload):
    with pytest.raises(services.ServiceError):
        services.create_ticket(
            equipment=equipment,
            client=client_obj,
            description="x",
            photos=[services.PhotoUpload(payload, "evil.svg")],
        )
    assert Ticket.objects.count() == 0  # транзакция откатилась целиком


def test_unknown_or_inactive_code_rejected(equipment, client_obj):
    with pytest.raises(services.EquipmentNotFound):
        services.create_ticket(equipment="NOPE00", client=client_obj, description="x")
    equipment.is_active = False
    equipment.save()
    with pytest.raises(services.EquipmentNotFound):
        services.find_equipment("TEST01")


def test_empty_description_rejected(equipment, client_obj):
    with pytest.raises(services.ServiceError):
        services.create_ticket(equipment=equipment, client=client_obj, description="   ")
    assert Ticket.objects.count() == 0


def test_change_status_writes_history_and_queues_notification(equipment, client_obj, django_user_model):
    dispatcher = django_user_model.objects.create_user("disp", password="x")
    ticket = services.create_ticket(equipment=equipment, client=client_obj, description="x")
    ticket.technician = "Петров"
    ticket.save()

    assert services.change_status(ticket, Ticket.Status.ACCEPTED, user=dispatcher) is True
    assert services.change_status(ticket, Ticket.Status.EN_ROUTE, comment="Будем к 15:00") is True

    ticket.refresh_from_db()
    assert ticket.status == Ticket.Status.EN_ROUTE
    history = list(ticket.history.values_list("old_status", "new_status", "changed_by"))
    assert history == [
        ("", "new", None),
        ("new", "accepted", dispatcher.pk),
        ("accepted", "en_route", None),
    ]
    texts = list(Notification.objects.order_by("pk").values_list("text", flat=True))
    assert len(texts) == 2
    assert f"Заявка №{ticket.number}: принята в работу" == texts[0]
    assert "техник выехал" in texts[1]
    assert "Техник: Петров" in texts[1]
    assert "Комментарий: Будем к 15:00" in texts[1]
    assert all(n.state == Notification.State.PENDING for n in Notification.objects.all())
    assert Notification.objects.filter(client=client_obj).count() == 2


def test_same_status_is_noop(equipment, client_obj):
    ticket = services.create_ticket(equipment=equipment, client=client_obj, description="x")
    assert services.change_status(ticket, Ticket.Status.NEW) is False
    assert Notification.objects.count() == 0
    assert ticket.history.count() == 1


def test_unknown_status_rejected(equipment, client_obj):
    ticket = services.create_ticket(equipment=equipment, client=client_obj, description="x")
    with pytest.raises(services.ServiceError):
        services.change_status(ticket, "lost")


def test_ticket_without_client_has_no_notification(equipment, client_obj):
    ticket = services.create_ticket(equipment=equipment, client=client_obj, description="x")
    Ticket.objects.filter(pk=ticket.pk).update(client=None)
    ticket.refresh_from_db()
    assert services.change_status(ticket, Ticket.Status.DONE) is True
    assert Notification.objects.count() == 0


def test_get_or_create_client_updates_name(db):
    info = services.ContactInfo(messenger="telegram", external_id="42", name="Old")
    first = services.get_or_create_client(info)
    second = services.get_or_create_client(services.ContactInfo("telegram", "42", "New", "nick"))
    assert first.pk == second.pk
    second.refresh_from_db()
    assert (second.name, second.username) == ("New", "nick")
