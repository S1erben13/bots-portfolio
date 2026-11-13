from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from tests.test_service import FakeProvider


def test_web_chat_lead_and_source(tmp_path, kb_path):
    settings = Settings(kb_dir=kb_path, db_path=tmp_path / "db.sqlite3", bot_token="", manager_chat_id="")
    with TestClient(create_app(settings, FakeProvider())) as client:
        assert client.get("/").status_code == 200
        assert client.get("/health").json() == {"status": "ok"}
        result = client.post("/api/chat", json={"question": "Гарантия на монтаж"}).json()
        assert "12 месяцев" in result["answer"]
        assert client.get(result["sources"][0]["url"]).status_code == 200
        assert client.get("/sources/%2e%2e/requirements.txt").status_code == 404
        assert client.post("/api/chat", json={"question": " "}).status_code == 422
        assert client.post("/api/leads", json={"name": "Анна", "phone": "+79991234567", "consent": False}).status_code == 422
        response = client.post("/api/leads", json={"name": "Анна", "phone": "+79991234567", "consent": True})
        assert response.status_code == 201
        assert response.json()["id"] == 1
