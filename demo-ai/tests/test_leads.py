import sqlite3

import httpx
import pytest
import respx

from app.leads import Leads, validate_lead


async def test_save_without_token(tmp_path, caplog):
    async with httpx.AsyncClient() as client:
        leads = Leads(tmp_path / "db.sqlite3", client)
        result = await leads.create(" Анна ", "8 (999) 123-45-67")
    assert not result["notified"]
    with sqlite3.connect(leads.path) as db:
        assert db.execute("SELECT name,phone,notified FROM leads").fetchone() == ("Анна", "+79991234567", 0)


@pytest.mark.parametrize("status,ok,expected", [(200, True, True), (200, False, False), (500, False, False)])
@respx.mock
async def test_notification(tmp_path, status, ok, expected):
    route = respx.post("https://api.telegram.org/botTEST/sendMessage").mock(return_value=httpx.Response(status, json={"ok": ok}))
    async with httpx.AsyncClient() as client:
        leads = Leads(tmp_path / "db.sqlite3", client, "TEST", "123")
        result = await leads.create("Иван", "+79991234567")
    assert route.called
    assert result["notified"] is expected
    with leads.connect() as db:
        assert db.execute("SELECT notified FROM leads").fetchone()[0] == int(expected)


@pytest.mark.parametrize("name,phone", [("А", "+79991234567"), ("Анна", "abc1234567"), ("Анна", "123"), ("Анна\nИван", "+79991234567")])
def test_validation(name, phone):
    with pytest.raises(ValueError):
        validate_lead(name, phone)
