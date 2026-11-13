import io

import pytest
from PIL import Image

from bots.types import IncomingPhoto
from service.models import Client, Company, Equipment, Messenger, Site


@pytest.fixture(autouse=True)
def media_tmp(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path / "media"


@pytest.fixture
def company(db):
    return Company.objects.create(name="Тест-Сервис", phone="+7 000")


@pytest.fixture
def site(company):
    return Site.objects.create(company=company, name="ЖК Тестовый", address="ул. Пробная, 1")


@pytest.fixture
def equipment(site):
    return Equipment.objects.create(
        site=site, kind=Equipment.Kind.CAMERA, name="Камера у входа", code="TEST01"
    )


@pytest.fixture
def client_obj(db):
    return Client.objects.create(messenger=Messenger.CONSOLE, external_id="u1", name="Иван")


@pytest.fixture
def photo():
    buf = io.BytesIO()
    Image.new("RGB", (32, 32), "red").save(buf, format="JPEG")
    return IncomingPhoto(buf.getvalue(), "p.jpg")


@pytest.fixture(autouse=True)
def close_async_db_connections():
    """sync_to_async открывает соединение с БД в своём потоке — закрываем его после теста,
    иначе тестовую базу нельзя удалить."""
    yield
    import asyncio

    from asgiref.sync import sync_to_async
    from django.db import connections

    asyncio.run(sync_to_async(connections.close_all)())
