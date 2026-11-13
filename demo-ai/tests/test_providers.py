import json
import time
import uuid

import httpx
import pytest
import respx

from app.providers import HTTPProvider, SYSTEM_PROMPT


@respx.mock
@pytest.mark.parametrize("model", [None, "custom-model:latest"])
async def test_ollama(monkeypatch, model):
    monkeypatch.setenv("OLLAMA_URL", "http://ollama:11434")
    if model is None:
        monkeypatch.delenv("OLLAMA_MODEL", raising=False)
    else:
        monkeypatch.setenv("OLLAMA_MODEL", model)
    route = respx.post("http://ollama:11434/api/chat").mock(return_value=httpx.Response(200, json={"message": {"content": '{"answerable":true,"answer":"Ответ по базе."}'}}))
    async with httpx.AsyncClient() as client:
        assert await HTTPProvider("ollama", client).complete("Вопрос") == '{"answerable":true,"answer":"Ответ по базе."}'
    body = json.loads(route.calls[0].request.content)
    assert body["model"] == (model or "qwen2.5:3b")
    assert body["messages"][0]["content"] == SYSTEM_PROMPT
    assert body["stream"] is False
    assert body["format"] == "json"
    assert body["options"]["temperature"] == 0


@respx.mock
async def test_yandex(monkeypatch):
    monkeypatch.setenv("YANDEX_API_KEY", "test-key")
    monkeypatch.setenv("YANDEX_FOLDER_ID", "folder")
    monkeypatch.setenv("YANDEX_MODEL", "yandexgpt-lite/latest")
    monkeypatch.setenv("YANDEX_URL", "https://ai.api.cloud.yandex.net/foundationModels/v1/completion")
    route = respx.post("https://ai.api.cloud.yandex.net/foundationModels/v1/completion").mock(return_value=httpx.Response(200, json={"result": {"alternatives": [{"message": {"text": '{"answerable":true,"answer":"Ответ по базе."}'}}]}}))
    async with httpx.AsyncClient() as client:
        assert await HTTPProvider("yandexgpt", client).complete("Вопрос") == '{"answerable":true,"answer":"Ответ по базе."}'
    request = route.calls[0].request
    assert request.headers["Authorization"] == "Api-Key test-key"
    body = json.loads(request.content)
    assert body["modelUri"] == "gpt://folder/yandexgpt-lite/latest"
    assert body["messages"][0]["text"] == SYSTEM_PROMPT
    assert body["completionOptions"]["temperature"] == 0


@respx.mock
async def test_gigachat_token_cache_and_expiry(monkeypatch):
    monkeypatch.setenv("GIGACHAT_AUTH_KEY", "test-key")
    monkeypatch.setenv("GIGACHAT_SCOPE", "GIGACHAT_API_PERS")
    monkeypatch.setenv("GIGACHAT_URL", "https://api.giga.chat/v1")
    monkeypatch.setenv("GIGACHAT_AUTH_URL", "https://ngw.devices.sberbank.ru:9443/api/v2/oauth")
    auth = respx.post("https://ngw.devices.sberbank.ru:9443/api/v2/oauth").mock(return_value=httpx.Response(200, json={"access_token": "access", "expires_at": int((time.time() + 1800) * 1000)}))
    chat = respx.post("https://api.giga.chat/v1/chat/completions").mock(return_value=httpx.Response(200, json={"choices": [{"message": {"content": '{"answerable":true,"answer":"Ответ по базе."}'}}]}))
    async with httpx.AsyncClient() as client:
        provider = HTTPProvider("gigachat", client)
        assert await provider.complete("Вопрос") == '{"answerable":true,"answer":"Ответ по базе."}'
        await provider.complete("Ещё вопрос")
        assert auth.call_count == 1
        provider.expires_at = 0
        await provider.complete("Снова")
        assert auth.call_count == 2
    assert auth.calls[0].request.headers["Authorization"] == "Basic test-key"
    assert uuid.UUID(auth.calls[0].request.headers["RqUID"]).version == 4
    assert b"scope=GIGACHAT_API_PERS" in auth.calls[0].request.content
    assert chat.calls[0].request.headers["Authorization"] == "Bearer access"
    assert json.loads(chat.calls[0].request.content)["temperature"] == 0


@respx.mock
async def test_http_error():
    respx.post("http://ollama:11434/api/chat").mock(return_value=httpx.Response(503))
    async with httpx.AsyncClient() as client:
        with pytest.raises(httpx.HTTPStatusError):
            await HTTPProvider("ollama", client).complete("Вопрос")
