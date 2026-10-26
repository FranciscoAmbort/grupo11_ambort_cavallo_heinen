"""Tests de app/services/ai.py con respuestas simuladas de Gemini (httpx.MockTransport, sin red)."""

import asyncio
import logging

import httpx
import pytest

from app.services import ai
from app.services.ai import AIServiceError, generate_text

TEST_KEY = "clave-de-prueba-123"


@pytest.fixture(autouse=True)
def gemini_settings(monkeypatch):
    """Configura una clave y un modelo de prueba solo durante cada test."""
    monkeypatch.setattr(ai.settings, "gemini_api_key", TEST_KEY)
    monkeypatch.setattr(ai.settings, "gemini_model", "modelo-de-prueba")


def _run(response: httpx.Response | None, *, system_instruction: str | None = None):
    """
    Llama a generate_text con un cliente que responde `response` sin salir a la red.
    Devuelve (texto o excepción, lista de requests enviados).
    """
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return response

    async def call():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await generate_text("Hola", system_instruction=system_instruction, client=client)

    try:
        return asyncio.run(call()), requests
    except AIServiceError as exc:
        return exc, requests


def _assert_key_not_leaked(result, requests, caplog):
    """La clave solo puede viajar en el header: nunca en la URL, el error ni los logs."""
    for request in requests:
        assert TEST_KEY not in str(request.url)
        assert request.headers["x-goog-api-key"] == TEST_KEY
    assert TEST_KEY not in str(result)
    assert TEST_KEY not in caplog.text


def test_success_returns_text_and_sends_key_in_header(caplog):
    caplog.set_level(logging.DEBUG)
    body = {
        "candidates": [{
            "content": {"parts": [
                {"text": "razonamiento interno", "thought": True},
                {"text": "Panorama general\n"},
                {"text": "Semana tranquila."},
            ]},
            "finishReason": "STOP",
        }]
    }
    result, requests = _run(httpx.Response(200, json=body), system_instruction="Sos un analista.")

    assert result == "Panorama general\nSemana tranquila."
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST"
    assert str(request.url).endswith("/models/modelo-de-prueba:generateContent")
    payload = httpx.Response(200, content=request.content).json()
    assert payload["system_instruction"]["parts"][0]["text"] == "Sos un analista."
    assert payload["contents"][0]["parts"][0]["text"] == "Hola"
    _assert_key_not_leaked(result, requests, caplog)


def test_http_429_raises_readable_error(caplog):
    caplog.set_level(logging.DEBUG)
    body = {"error": {"code": 429, "message": "Resource has been exhausted (e.g. check quota).", "status": "RESOURCE_EXHAUSTED"}}
    result, requests = _run(httpx.Response(429, json=body))

    assert isinstance(result, AIServiceError)
    assert "429" in str(result)
    assert "Resource has been exhausted" in caplog.text
    _assert_key_not_leaked(result, requests, caplog)


def test_blocked_prompt_raises_error(caplog):
    caplog.set_level(logging.DEBUG)
    body = {"promptFeedback": {"blockReason": "SAFETY"}}
    result, requests = _run(httpx.Response(200, json=body))

    assert isinstance(result, AIServiceError)
    assert "SAFETY" in caplog.text
    _assert_key_not_leaked(result, requests, caplog)


@pytest.mark.parametrize(
    "body",
    [
        {"candidates": []},
        {"candidates": [{"content": {"parts": [{"text": "   "}]}, "finishReason": "MAX_TOKENS"}]},
        {"candidates": [{"content": {"parts": [{"text": "solo razonamiento", "thought": True}]}}]},
        {"candidates": [{"finishReason": "SAFETY"}]},
    ],
    ids=["sin-candidatos", "texto-vacio", "solo-thought", "sin-content"],
)
def test_response_without_text_raises_error(body, caplog):
    caplog.set_level(logging.DEBUG)
    result, requests = _run(httpx.Response(200, json=body))

    assert isinstance(result, AIServiceError)
    _assert_key_not_leaked(result, requests, caplog)


def test_missing_key_raises_without_calling_gemini(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(ai.settings, "gemini_api_key", "")
    result, requests = _run(httpx.Response(200, json={}))

    assert isinstance(result, AIServiceError)
    assert "GEMINI_API_KEY" in str(result)
    assert requests == []
    assert ai.ai_is_configured() is False
