"""Проверки демо без запросов к реальным CRM и Telegram."""

import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import Database
from app.main import create_app
from app.normalize import normalize_phone


@pytest.mark.parametrize("value", ["8 (999) 123-45-67", "+7 999 123 45 67", "79991234567", "9991234567"])
def test_phone_normalization(value):
    assert normalize_phone(value) == "+79991234567"


@pytest.fixture
def settings(tmp_path):
    return Settings(db_path=str(tmp_path / "test.sqlite"), hook_secret="test-secret",
                    admin_user="admin", admin_password="test-password")


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings, start_worker=False)) as value:
        yield value


def test_webhook_requires_secret(client):
    response = client.post("/hooks/generic", json={"name": "Иван", "phone": "9991234567"})
    assert response.status_code in (401, 403)


def test_json_and_form_hooks(client):
    for index, source in enumerate(("tilda", "getcourse", "generic")):
        response = client.post(
            f"/hooks/{source}?secret=test-secret",
            headers={"Idempotency-Key": f"form-{index}"},
            data={"name": "Иван", "phone": "8 (999) 123-45-67"},
        )
        assert response.is_success, response.text


def test_repeated_webhook_returns_same_id(client):
    kwargs = {"headers": {"X-Hook-Secret": "test-secret", "Idempotency-Key": "event-1"},
              "json": {"name": "Иван", "phone": "9991234567"}}
    first = client.post("/hooks/generic", **kwargs)
    second = client.post("/hooks/generic", **kwargs)
    assert first.is_success and second.is_success
    assert first.json()["id"] == second.json()["id"]


def test_admin_requires_basic_auth(client):
    assert client.get("/admin").status_code == 401
    response = client.get("/admin", auth=("admin", "test-password"))
    assert response.status_code == 200


def test_admin_escapes_untrusted_content(client):
    response = client.post("/hooks/generic", headers={"X-Hook-Secret": "test-secret"},
                           json={"name": "<script>alert(1)</script>", "phone": "9991234567"})
    assert response.is_success
    html = client.get("/admin", auth=("admin", "test-password")).text
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html

@pytest.mark.parametrize("value", ["123", "+12025550123", "abcdef123"])
def test_invalid_phone_rejected(value):
    with pytest.raises(ValueError):
        normalize_phone(value)


def test_database_idempotency_and_conflict(tmp_path):
    db = Database(str(tmp_path / "db.sqlite"))
    db.init()
    lead = {"name": "Иван", "phone": "+79991234567", "email": "", "source": "generic", "utm": {}, "comment": ""}
    first, duplicate = db.enqueue("one", lead)
    assert not duplicate
    assert db.enqueue("one", lead) == (first, True)
    with pytest.raises(ValueError):
        db.enqueue("one", {**lead, "name": "Другой"})
    assert len(db.latest()) == 1
    assert db.get(first)["lead"] == lead


def test_database_recovers_expired_claim(tmp_path):
    db = Database(str(tmp_path / "db.sqlite"))
    db.init()
    job_id, _ = db.enqueue("event", {"name": "Иван"})
    assert db.claim()["id"] == job_id
    assert db.claim() is None
    db.save(job_id, lease_until=0)
    assert db.claim()["id"] == job_id


def test_database_retry_preserves_crm_progress(tmp_path):
    db = Database(str(tmp_path / "db.sqlite"))
    db.init()
    job_id, _ = db.enqueue("event", {"name": "Иван"})
    db.save(job_id, status="failed", attempts=8, state={"deal_id": 42}, error="failure")
    assert db.retry(job_id)
    job = db.get(job_id)
    assert job["status"] == "pending"
    assert job["state"] == {"deal_id": 42}
    assert job["attempts"] == 0
    assert job["error"] == ""

@pytest.fixture
def lead():
    return {"name": "Анна", "phone": "+79991234567", "email": "anna@example.test",
            "source": "tilda", "utm": {"utm_source": "demo"}, "comment": "Консультация"}


@pytest.fixture
def db(tmp_path):
    value = Database(str(tmp_path / "queue.sqlite"))
    value.init()
    return value


def test_fake_deduplicates_contact_but_keeps_distinct_deals(db, lead):
    from app.crm import FakeCRM
    crm = FakeCRM(db)
    first = asyncio.run(crm.deliver(lead, {"job_key": "first"}))
    second = asyncio.run(crm.deliver({**lead, "phone": ""}, {"job_key": "second"}))
    repeat = asyncio.run(crm.deliver(lead, {"job_key": "first"}))
    assert first["contact_id"] == second["contact_id"]
    assert first["deal_id"] != second["deal_id"]
    assert repeat["deal_id"] == first["deal_id"]
    with db.connect() as connection:
        assert connection.execute("SELECT count(*) FROM contacts").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM deals").fetchone()[0] == 2


@pytest.mark.parametrize("status", [429, 500, 503])
def test_worker_retries_transient_http_errors(db, lead, settings, status):
    from app.crm import request
    from app.worker import Worker
    import time
    job_id, _ = db.enqueue("retry", lead)

    class FailingCRM:
        async def deliver(self, lead, state, checkpoint):
            async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda req: httpx.Response(status, json={"error": "temporary"})
            )) as client:
                await request(client, "POST", "https://crm.example.test/create")

    class UnusedNotifier:
        async def send(self, lead, result):
            pytest.fail("Уведомление до доставки в CRM")

    worker = Worker(db, FailingCRM(), UnusedNotifier(), settings)
    before = time.time()
    assert asyncio.run(worker.run_once())
    first = db.get(job_id)
    assert first["status"] == "pending" and first["attempts"] == 1
    assert first["next_at"] >= before + settings.retry_base_seconds
    assert not asyncio.run(worker.run_once())
    db.save(job_id, next_at=0)
    before_second = time.time()
    assert asyncio.run(worker.run_once())
    second = db.get(job_id)
    assert second["attempts"] == 2
    assert second["next_at"] >= before_second + settings.retry_base_seconds * 2


def test_worker_notification_retry_does_not_recreate_deal(db, lead, settings):
    from app.crm import FakeCRM, RemoteError
    from app.worker import Worker
    job_id, _ = db.enqueue("notification", lead)

    class FlakyNotifier:
        calls = 0
        async def send(self, lead, result):
            self.calls += 1
            if self.calls == 1:
                raise RemoteError("Telegram HTTP 503")

    notifier = FlakyNotifier()
    worker = Worker(db, FakeCRM(db), notifier, settings)
    asyncio.run(worker.run_once())
    first = db.get(job_id)
    assert first["state"]["crm_done"]
    deal_id = first["state"]["deal_id"]
    db.save(job_id, next_at=0)
    asyncio.run(worker.run_once())
    final = db.get(job_id)
    assert final["status"] == "sent"
    assert final["state"]["deal_id"] == deal_id
    assert final["state"]["notified"]
    with db.connect() as connection:
        assert connection.execute("SELECT count(*) FROM deals").fetchone()[0] == 1


def test_worker_permanent_error_and_manual_retry(db, lead, settings):
    from app.crm import RemoteError
    from app.worker import Worker
    job_id, _ = db.enqueue("permanent", lead)

    class InvalidCRM:
        async def deliver(self, lead, state, checkpoint):
            raise RemoteError("CRM HTTP 400", retryable=False)

    worker = Worker(db, InvalidCRM(), None, settings)
    asyncio.run(worker.run_once())
    assert db.get(job_id)["status"] == "failed"
    assert db.retry(job_id)
    assert db.get(job_id)["status"] == "pending"


@pytest.mark.parametrize("existing", [False, True])
def test_amocrm_contact_lookup_complex_lead_and_note(settings, lead, existing):
    import json
    from app.crm import AmoCRM
    settings.amo_base_url = "https://amo.example.test"
    settings.amo_token = "fake-token"
    requests = []
    checkpoints = []

    def handler(request):
        requests.append(request)
        assert request.headers["Authorization"] == "Bearer fake-token"
        if request.url.path == "/api/v4/contacts":
            contacts = [{"id": 17, "custom_fields_values": [{"field_code": "PHONE", "values": [{"value": "8 (999) 123-45-67"}]}]}] if existing else []
            return httpx.Response(200, json={"_embedded": {"contacts": contacts}})
        if request.url.path == "/api/v4/leads/complex":
            body = json.loads(request.content)
            contact = body[0]["_embedded"]["contacts"][0]
            if existing:
                assert contact == {"id": 17}
            else:
                assert contact["name"] == lead["name"]
                assert contact["custom_fields_values"][0]["field_code"] == "PHONE"
            return httpx.Response(200, json=[{"id": 23, "contact_id": 17}])
        assert request.url.path == "/api/v4/leads/23/notes"
        assert "utm_source" in json.loads(request.content)[0]["params"]["text"]
        return httpx.Response(200, json={"_embedded": {"notes": [{"id": 99}]}})

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            adapter = AmoCRM(settings, client)
            result = await adapter.deliver(lead, {"job_key": "event"}, lambda state: checkpoints.append(dict(state)))
            before = len(requests)
            await adapter.deliver(lead, result)
            assert len(requests) == before
            return result

    result = asyncio.run(scenario())
    assert result["contact_id"] == 17 and result["deal_id"] == 23
    assert checkpoints[0]["deal_id"] == 23
    assert checkpoints[-1]["note_done"]


@pytest.mark.parametrize("existing", [False, True])
def test_bitrix_contact_dedup_and_deal(settings, lead, existing):
    import json
    from app.crm import BitrixCRM
    settings.bitrix_webhook_url = "https://bitrix.example.test/rest/1/fake"
    methods = []

    def handler(request):
        method = request.url.path.rsplit("/", 1)[-1].removesuffix(".json")
        methods.append(method)
        body = json.loads(request.content)
        if method == "crm.duplicate.findbycomm":
            assert body["entity_type"] == "CONTACT"
            return httpx.Response(200, json={"result": {"CONTACT": [17]} if existing else {}})
        if method == "crm.contact.add":
            assert not existing
            assert body["fields"]["PHONE"][0]["VALUE"] == lead["phone"]
            return httpx.Response(200, json={"result": 17})
        assert method == "crm.deal.add"
        assert body["fields"]["CONTACT_ID"] == 17
        assert body["fields"]["UTM_SOURCE"] == "demo"
        return httpx.Response(200, json={"result": 23})

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            adapter = BitrixCRM(settings, client)
            result = await adapter.deliver(lead, {"job_key": "event"})
            before = len(methods)
            await adapter.deliver(lead, result)
            assert len(methods) == before
            return result

    result = asyncio.run(scenario())
    assert result["deal_id"] == 23 and result["contact_id"] == 17
    assert ("crm.contact.add" in methods) is not existing


def test_telegram_notification(settings, lead):
    import json
    from app.worker import Notifier
    settings.telegram_token = "fake-token"
    settings.telegram_chat_id = "123"
    requests = []

    def handler(request):
        requests.append(request)
        assert request.url.path == "/botfake-token/sendMessage"
        body = json.loads(request.content)
        assert body["chat_id"] == "123"
        assert lead["name"] in body["text"]
        assert "23" in body["text"]
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await Notifier(settings, client).send(lead, {"deal_id": 23})
    asyncio.run(scenario())
    assert len(requests) == 1


def test_telegram_without_token_only_logs(settings, lead, caplog):
    from app.worker import Notifier
    import logging

    def handler(request):
        pytest.fail("Без токена сетевых запросов быть не должно")

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await Notifier(settings, client).send(lead, {"deal_id": 23})
    with caplog.at_level(logging.INFO):
        asyncio.run(scenario())
    assert "Демо-уведомление" in caplog.text


def test_fallback_key_is_stable_after_normalization(client):
    headers = {"X-Hook-Secret": "test-secret"}
    first = client.post("/hooks/generic", headers=headers, json={"phone": "8 (999) 123-45-67"})
    second = client.post("/hooks/generic", headers=headers, json={"phone": "+79991234567"})
    assert first.status_code == second.status_code == 202
    assert first.json()["id"] == second.json()["id"]
    assert second.json()["duplicate"]


def test_reusing_key_with_changed_payload_returns_conflict(client):
    headers = {"X-Hook-Secret": "test-secret", "Idempotency-Key": "unique"}
    assert client.post("/hooks/generic", headers=headers, json={"phone": "9991234567"}).status_code == 202
    assert client.post("/hooks/generic", headers=headers, json={"phone": "9997654321"}).status_code == 409


@pytest.mark.parametrize("payload", [{"phone": "123"}, {"email": "wrong"}, [], {"name": "Без контакта"}])
def test_invalid_webhooks_not_queued(client, payload):
    response = client.post("/hooks/generic", headers={"X-Hook-Secret": "test-secret"}, json=payload)
    assert response.status_code == 422
    assert client.app.state.db.latest() == []


def test_admin_manual_retry_requires_csrf(client, lead):
    import re
    db = client.app.state.db
    job_id, _ = db.enqueue("admin-retry", lead)
    db.save(job_id, status="failed", attempts=8, error="CRM HTTP 503")
    path = f"/admin/retry/{job_id}"
    auth = ("admin", "test-password")
    assert client.post(path, auth=auth).status_code == 403
    html = client.get("/admin", auth=auth).text
    token = re.search(r'name="csrf" value="([0-9a-f]+)"', html).group(1)
    response = client.post(path, auth=auth, data={"csrf": token}, follow_redirects=False)
    assert response.status_code == 303
    assert db.get(job_id)["status"] == "pending"


def test_worker_attempt_limit_stops_retry(db, lead, settings):
    from app.crm import RemoteError
    from app.worker import Worker
    settings.max_attempts = 1
    job_id, _ = db.enqueue("exhausted", lead)

    class UnavailableCRM:
        async def deliver(self, lead, state, checkpoint):
            raise RemoteError("CRM HTTP 503")

    asyncio.run(Worker(db, UnavailableCRM(), None, settings).run_once())
    assert db.get(job_id)["status"] == "failed"
    assert db.claim() is None


def test_amocrm_falls_back_to_exact_email_match(settings, lead):
    import json
    from app.crm import AmoCRM
    settings.amo_base_url = "https://amo.example.test"
    queries = []

    def handler(request):
        if request.url.path == "/api/v4/contacts":
            query = request.url.params["query"]
            queries.append(query)
            if query == lead["phone"]:
                return httpx.Response(204)
            return httpx.Response(200, json={"_embedded": {"contacts": [
                {"id": 17, "custom_fields_values": [{"field_code": "EMAIL", "values": [{"value": "ANNA@EXAMPLE.TEST"}]}]}
            ]}})
        if request.url.path == "/api/v4/leads/complex":
            assert json.loads(request.content)[0]["_embedded"]["contacts"] == [{"id": 17}]
            return httpx.Response(200, json=[{"id": 23, "contact_id": 17}])
        return httpx.Response(200, json={})

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await AmoCRM(settings, client).deliver(lead, {"job_key": "email-dedup"})
    result = asyncio.run(scenario())
    assert queries == [lead["phone"], lead["email"]]
    assert result["contact_id"] == 17


def test_bitrix_empty_array_search_result(settings, lead):
    from app.crm import BitrixCRM
    settings.bitrix_webhook_url = "https://bitrix.example.test/rest/1/fake"

    def handler(request):
        if "crm.duplicate.findbycomm" in request.url.path:
            return httpx.Response(200, json={"result": []})
        if "crm.contact.add" in request.url.path:
            return httpx.Response(200, json={"result": 17})
        assert "crm.deal.add" in request.url.path
        return httpx.Response(200, json={"result": 23})

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await BitrixCRM(settings, client).deliver(lead, {"job_key": "empty-result"})
    result = asyncio.run(scenario())
    assert result["contact_id"] == 17 and result["deal_id"] == 23
