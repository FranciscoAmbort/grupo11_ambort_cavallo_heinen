"""Caché de los archivos estáticos: Cache-Control en el frontend y versión en las URLs locales (sin base ni red)."""

import asyncio
from pathlib import Path
import re
from urllib.parse import urlsplit

import httpx
import pytest

from app.database import get_session
from app.main import STATIC_DIR, app

HTML_FILES = sorted(STATIC_DIR.rglob("*.html"))
# Valor de src o href, aunque el tag ocupe varias líneas
URL_ATTR_RE = re.compile(r"""\b(?:src|href)\s*=\s*["']([^"']+)["']""")
EXTERNAL_RE = re.compile(r"^(?:[a-z][a-z0-9+.-]*:|//)", re.IGNORECASE)


def _get(path: str, **kwargs) -> httpx.Response:
    # ASGITransport no dispara el lifespan: el worker no arranca
    async def call():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            return await client.get(path, **kwargs)

    return asyncio.run(call())


def _asset_urls(html: Path) -> tuple[list[str], list[str]]:
    """(locales, externas) de los scripts y hojas de estilo .js/.css que referencia el HTML."""
    local, external = [], []
    for url in URL_ATTR_RE.findall(html.read_text(encoding="utf-8")):
        if EXTERNAL_RE.match(url):
            external.append(url)
        elif urlsplit(url).path.endswith((".js", ".css")):
            local.append(url)
    return local, [url for url in external if urlsplit(url).path.endswith((".js", ".css")) or "?v=" in url]


# ── Cache-Control ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("path", ["/dashboard/dashboard.js", "/styles.css", "/styles.css?v=20261027", "/dashboard/"])
def test_frontend_lleva_no_cache(path):
    response = _get(path)

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-cache"


def test_api_health_no_cambia():
    class FakeSession:
        async def execute(self, statement):
            return None

    async def fake_get_session():
        yield FakeSession()

    app.dependency_overrides[get_session] = fake_get_session
    try:
        response = _get("/api/health")
    finally:
        app.dependency_overrides.pop(get_session, None)

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": True}
    assert "cache-control" not in response.headers


# ── Versión en los HTML ───────────────────────────────────────────────────────

def test_hay_paginas_html():
    assert {path.relative_to(STATIC_DIR).as_posix() for path in HTML_FILES} >= {
        "index.html",
        "onboarding/index.html",
        "dashboard/index.html",
        "alerts/index.html",
        "settings/index.html",
    }


@pytest.mark.parametrize("html", HTML_FILES, ids=lambda path: path.relative_to(STATIC_DIR).as_posix())
def test_scripts_y_estilos_locales_llevan_version(html):
    local, external = _asset_urls(html)

    # Si la regex dejara una página afuera, no encontraría ninguna referencia local
    assert local, f"{html.name} no tiene scripts ni estilos locales detectados"
    sin_version = [url for url in local if "v=" not in urlsplit(url).query]
    assert sin_version == [], f"Referencias locales sin ?v=: {sin_version}"
    # Los CDN externos no llevan el parámetro de versión
    assert all("?v=" not in url for url in external), external
