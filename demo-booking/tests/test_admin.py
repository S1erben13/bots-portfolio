import re

from fastapi.testclient import TestClient

from booking.admin import create_app
from booking.db import connect


def client_for(service):
    return TestClient(create_app(service))


def token(client):
    page = client.get("/admin/catalog")
    assert page.status_code == 200
    return re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)


def test_auth_csrf_crud_and_validation(system):
    service, _ = system
    client = client_for(service)
    assert client.get("/admin").status_code == 401
    client.auth = ("admin", "test-password")
    csrf = token(client)
    assert client.post("/admin/master", data={"name": "Тест"}).status_code == 403
    response = client.post("/admin/service", data={"csrf": csrf, "name": "Массаж", "duration": 60})
    assert response.status_code == 200
    sid = service.services()[-1]["id"]
    response = client.post("/admin/service", data={"csrf": csrf, "id": sid, "name": "Массаж лица", "duration": 45})
    assert response.status_code == 200
    assert service.services()[-1]["duration"] == 45
    assert client.post("/admin/service", data={"csrf": csrf, "name": "Тест", "duration": 17}).status_code == 400
    assert client.post("/admin/archive", data={"csrf": csrf, "kind": "services", "id": sid}).status_code == 200
    assert len(service.services()) == 4
    client.post("/admin/master", data={"csrf": csrf, "name": "Ольга"})
    mid = service.masters()[-1]["id"]
    client.post("/admin/master", data={"csrf": csrf, "id": mid, "name": "Ольга 2"})
    assert service.masters()[-1]["name"] == "Ольга 2"
    client.post("/admin/archive", data={"csrf": csrf, "kind": "masters", "id": mid})
    assert len(service.masters()) == 2


def test_schedule_and_bookings(system):
    service, _ = system
    client = client_for(service)
    client.auth = ("admin", "test-password")
    csrf = token(client)
    data = {"csrf": csrf, "master_id": 1, "weekday": 1, "start": "10:00", "end": "17:00", "break_start": "12:00", "break_end": "12:30"}
    assert client.post("/admin/schedule", data=data).status_code == 200
    data["break_end"] = "18:00"
    assert client.post("/admin/schedule", data=data).status_code == 400
    bid = service.book(7, 1, 1, "2026-11-17T10:00", "<script>alert(1)</script>", "+79991234567")
    page = client.get("/admin?day=2026-11-16&days=7")
    assert "&lt;script&gt;" in page.text
    assert "<script>" not in page.text
    assert client.post("/admin/booking", data={"csrf": csrf, "id": bid, "status": "attended"}).status_code == 200
    assert service.bookings()[0]["status"] == "attended"
    client.post("/admin/dayoff", data={"csrf": csrf, "master_id": 1, "day": "2026-11-18"})
    with connect(service.config.db_path) as conn:
        assert conn.execute("SELECT count(*) FROM days_off").fetchone()[0] == 1
