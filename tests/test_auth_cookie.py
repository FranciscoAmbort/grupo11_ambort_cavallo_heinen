"""Atributos de la cookie de sesión en login y logout (httpx.ASGITransport, sin Google ni base de datos)."""

import asyncio
from datetime import datetime, timezone

import httpx
import pytest

from app.api import auth
from app.api.deps import ACCESS_TOKEN_COOKIE
from app.database import get_session
from app.main import app
from app.models import User


class FakeSession:
    """Sesión que siempre encuentra al mismo usuario, así el login no crea filas."""

    def __init__(self, user: User):
        self.user = user

    async def scalar(self, statement):
        return self.user


@pytest.fixture
def fake_login(monkeypatch):
    """Reemplaza la validación con Google y la sesión de base solo durante cada test."""
    user = User(
        id=1,
        email="test@example.invalid",
        google_id="google-test",
        name="Test",
        telegram_chat_id=None,
        onboarding_completed=True,
        created_at=datetime(2026, 10, 26, tzinfo=timezone.utc),
    )

    async def fake_verify(id_token: str) -> dict:
        return {"sub": user.google_id, "email": user.email, "email_verified": "true"}

    async def fake_get_session():
        yield FakeSession(user)

    monkeypatch.setattr(auth, "_verify_google_id_token", fake_verify)
    app.dependency_overrides[get_session] = fake_get_session
    yield
    app.dependency_overrides.pop(get_session, None)


def _session_cookie(response: httpx.Response) -> tuple[str, set[str]]:
    """Devuelve (valor, atributos en minúscula) del Set-Cookie de access_token."""
    for header in response.headers.get_list("set-cookie"):
        name_value, *attributes = [part.strip() for part in header.split(";")]
        name, _, value = name_value.partition("=")
        if name == ACCESS_TOKEN_COOKIE:
            return value, {attr.lower() for attr in attributes}
    raise AssertionError("La respuesta no trae la cookie access_token")


def _post(path: str, **kwargs) -> httpx.Response:
    # ASGITransport no dispara el lifespan: el worker no arranca
    async def call():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            return await client.post(path, **kwargs)

    return asyncio.run(call())


@pytest.mark.parametrize("secure", [False, True])
def test_login_setea_cookie_httponly_lax_y_secure_segun_config(fake_login, monkeypatch, secure):
    monkeypatch.setattr(auth.settings, "cookie_secure", secure)

    response = _post("/api/auth/google", json={"id_token": "token-de-prueba"})

    assert response.status_code == 200
    value, attributes = _session_cookie(response)
    assert value
    assert "httponly" in attributes
    assert "samesite=lax" in attributes
    assert ("secure" in attributes) is secure


@pytest.mark.parametrize("secure", [False, True])
def test_logout_borra_cookie_con_los_mismos_atributos(monkeypatch, secure):
    monkeypatch.setattr(auth.settings, "cookie_secure", secure)

    response = _post("/api/auth/logout")

    assert response.status_code == 200
    value, attributes = _session_cookie(response)
    assert value in ("", '""')
    assert "max-age=0" in attributes
    assert "httponly" in attributes
    assert "samesite=lax" in attributes
    assert ("secure" in attributes) is secure
