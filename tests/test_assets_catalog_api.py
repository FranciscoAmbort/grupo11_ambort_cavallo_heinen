"""
Endpoints del catálogo a pedido (app/api/assets.py) sin base de datos ni red:
httpx.ASGITransport (sin lifespan ni worker), sesión simulada y funciones de catalog parcheadas.
Los casos que necesitan PostgreSQL real (alta en el catálogo, advisory lock) se verifican aparte.
"""

import asyncio
from datetime import datetime, timezone

import httpx
import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql.dml import Insert

from app.api import assets
from app.api.deps import get_current_user
from app.database import get_session
from app.main import app
from app.models import User
from app.services import catalog
from app.services.catalog import MarketCandidate, SlidingWindowLimiter

BONDS_DATA = {
    "bonds": [
        {"ticker": "AL30D", "tickerBase": "AL30", "nombre": "AL30 (Ley AR)", "tipo": "Soberano", "precio": 54.3},
        {"ticker": "GD46D", "tickerBase": "GD46", "nombre": "GD46 (Ley NY)", "tipo": "Soberano", "precio": 60.0},
    ]
}


class FakeScalars:
    def __init__(self, rows):
        self._rows = list(rows)

    def all(self):
        return self._rows


class FakeResult:
    def all(self):
        return []


class FakeSession:
    """
    Responde las consultas de los endpoints según su SQL: ids seguidos por el usuario,
    ids existentes del catálogo y tickers ya conocidos. Registra lo que se ejecuta.
    """

    def __init__(self, followed=(), catalog_ids=(), catalog_tickers=()):
        self.followed = set(followed)
        self.catalog_ids = set(catalog_ids)
        self.catalog_tickers = set(catalog_tickers)
        self.executed = []
        self.scalar_queries = []
        self.commits = 0

    async def scalars(self, statement):
        sql = str(statement)
        self.scalar_queries.append(statement)
        if "FROM user_assets" in sql:
            return FakeScalars(self.followed)
        if "assets.ticker" in sql:
            return FakeScalars(self.catalog_tickers)
        return FakeScalars(self.catalog_ids)

    async def scalar(self, statement):
        raise AssertionError(f"Consulta inesperada en este test: {statement}")

    async def execute(self, statement):
        self.executed.append(statement)
        return FakeResult()

    async def commit(self):
        self.commits += 1

    def inserts_into(self, table: str) -> list[Insert]:
        return [s for s in self.executed if isinstance(s, Insert) and s.table.name == table]


@pytest.fixture
def user():
    return User(
        id=7,
        email="test@example.invalid",
        google_id="google-test",
        name="Test",
        telegram_chat_id=None,
        onboarding_completed=True,
        created_at=datetime(2026, 10, 26, tzinfo=timezone.utc),
    )


@pytest.fixture
def use_session(user):
    """Instala el usuario y una sesión simulada como dependencias; devuelve una función para elegir la sesión."""
    holder = {}

    async def fake_get_session():
        yield holder["session"]

    def install(session: FakeSession) -> FakeSession:
        holder["session"] = session
        return session

    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_session] = fake_get_session
    yield install
    app.dependency_overrides.pop(get_current_user, None)
    app.dependency_overrides.pop(get_session, None)


@pytest.fixture(autouse=True)
def clean_catalog_state(monkeypatch):
    """Cachés vacías y un limitador nuevo en cada test."""
    catalog.clear_caches()
    monkeypatch.setattr(catalog, "search_limiter", SlidingWindowLimiter(catalog.SEARCH_LIMIT_PER_MINUTE))
    yield
    catalog.clear_caches()


def _request(method: str, path: str, **kwargs) -> httpx.Response:
    # ASGITransport no dispara el lifespan: el worker no arranca
    async def call():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            return await client.request(method, path, **kwargs)

    return asyncio.run(call())


# ── POST /api/assets/custom: formato ──────────────────────────────────────────

@pytest.mark.parametrize(
    "payload",
    [
        {"ticker": "../x", "asset_type": "cedear"},
        {"ticker": "GGAL.BA/x", "asset_type": "stock"},
        {"ticker": "ABCDEFGHIJKLM", "asset_type": "stock"},
        {"ticker": "BR_30", "asset_type": "bond"},
        {"ticker": "", "asset_type": "stock"},
        {"ticker": "GGAL", "asset_type": "fondo"},
        {"ticker": "GGAL", "asset_type": "STOCK"},
    ],
)
def test_custom_formato_o_tipo_invalido_da_400_sin_tocar_base_ni_proveedor(use_session, monkeypatch, payload):
    session = use_session(FakeSession())

    async def fail(*args, **kwargs):
        raise AssertionError("No debería consultarse al proveedor")

    monkeypatch.setattr(catalog, "validate_yahoo", fail)
    monkeypatch.setattr(catalog, "validate_bond", fail)

    response = _request("POST", "/api/assets/custom", json=payload)

    assert response.status_code == 400
    assert session.scalar_queries == [] and session.executed == [] and session.commits == 0


# ── POST /api/assets/me: tope por usuario ─────────────────────────────────────

def test_me_reenviar_activos_ya_seguidos_en_el_tope_es_idempotente(use_session, monkeypatch):
    monkeypatch.setattr(assets.settings, "max_assets_per_user", 2)
    session = use_session(FakeSession(followed={1, 2}, catalog_ids={1, 2, 3}))

    response = _request("POST", "/api/assets/me", json={"asset_ids": [1, 2]})

    assert response.status_code == 200
    assert session.inserts_into("user_assets") == []  # nada nuevo que insertar
    assert session.commits == 1


def test_me_sumar_uno_nuevo_por_encima_del_tope_da_409(use_session, monkeypatch):
    monkeypatch.setattr(assets.settings, "max_assets_per_user", 2)
    session = use_session(FakeSession(followed={1, 2}, catalog_ids={1, 2, 3}))

    response = _request("POST", "/api/assets/me", json={"asset_ids": [1, 3]})

    assert response.status_code == 409
    assert response.json()["detail"] == "Llegaste al máximo de activos que podés seguir"
    assert session.inserts_into("user_assets") == [] and session.commits == 0


def test_me_por_debajo_del_tope_inserta_solo_los_nuevos(use_session, monkeypatch):
    monkeypatch.setattr(assets.settings, "max_assets_per_user", 3)
    session = use_session(FakeSession(followed={1, 2}, catalog_ids={1, 2, 3}))

    response = _request("POST", "/api/assets/me", json={"asset_ids": [2, 3]})

    assert response.status_code == 200
    [stmt] = session.inserts_into("user_assets")
    assert stmt.compile(dialect=postgresql.dialect()).params == {"user_id_m0": 7, "asset_id_m0": 3}
    assert session.commits == 1


def test_me_tope_bajado_no_impide_reenviar_lo_ya_seguido(use_session, monkeypatch):
    # El usuario sigue más activos que el tope actual (por ejemplo, porque se bajó el tope)
    monkeypatch.setattr(assets.settings, "max_assets_per_user", 2)
    session = use_session(FakeSession(followed={1, 2, 3}, catalog_ids={1, 2, 3}))

    response = _request("POST", "/api/assets/me", json={"asset_ids": [3]})

    assert response.status_code == 200
    assert session.inserts_into("user_assets") == []


def test_me_activo_inexistente_sigue_dando_404(use_session):
    use_session(FakeSession(catalog_ids={1}))

    response = _request("POST", "/api/assets/me", json={"asset_ids": [1, 99]})

    assert response.status_code == 404
    assert "99" in response.json()["detail"]


# ── GET /api/assets/search ────────────────────────────────────────────────────

def test_search_con_yahoo_caido_devuelve_bonos_que_no_estan_en_el_catalogo(use_session, monkeypatch):
    use_session(FakeSession(catalog_tickers={"AL30"}))

    async def fake_bonds():
        return BONDS_DATA

    async def yahoo_down(q, *, client=None):
        return None

    monkeypatch.setattr(catalog, "fetch_bonds_data", fake_bonds)
    monkeypatch.setattr(catalog, "search_yahoo", yahoo_down)

    response = _request("GET", "/api/assets/search", params={"q": "ley"})

    assert response.status_code == 200
    assert response.json() == [
        {"ticker": "GD46", "name": "GD46 (Ley NY)", "asset_type": "bond", "currency": "USD", "type_hint": None}
    ]


def test_search_oculta_conocidos_y_variantes(use_session, monkeypatch):
    session = use_session(FakeSession(catalog_tickers={"GGAL.BA"}))
    candidates = [
        MarketCandidate("GGALD.BA", "Grupo Financiero Galicia S.A.", None, None, "stock"),
        MarketCandidate("VIST.BA", "Vista Energy, S.A.B. de C.V.", None, None, "cedear"),
    ]

    async def fake_search(q):
        return candidates

    monkeypatch.setattr(catalog, "search_market", fake_search)

    response = _request("GET", "/api/assets/search", params={"q": "a"})

    assert [item["ticker"] for item in response.json()] == ["VIST.BA"]
    assert response.json()[0]["type_hint"] == "cedear"
    # La consulta al catálogo incluye la base de la variante
    [lookup] = session.scalar_queries
    assert "GGAL.BA" in str(lookup.compile(compile_kwargs={"literal_binds": True}))


def test_search_respeta_el_limite_de_resultados(use_session, monkeypatch):
    use_session(FakeSession())

    async def many(q):
        return [MarketCandidate(f"X{i}.BA", f"Activo {i}", None, None, "stock") for i in range(30)]

    monkeypatch.setattr(catalog, "search_market", many)

    response = _request("GET", "/api/assets/search", params={"q": "x"})

    assert len(response.json()) == assets.SEARCH_RESULTS_LIMIT


def test_search_la_busqueda_61_del_minuto_da_429(use_session, monkeypatch):
    use_session(FakeSession())

    async def nothing(q):
        return []

    monkeypatch.setattr(catalog, "search_market", nothing)

    statuses = [_request("GET", "/api/assets/search", params={"q": "gd"}).status_code for _ in range(61)]

    assert statuses[:60] == [200] * 60
    assert statuses[60] == 429


@pytest.mark.parametrize("params", [{}, {"q": ""}, {"q": "x" * 41}])
def test_search_q_invalida_da_422(use_session, params):
    use_session(FakeSession())
    assert _request("GET", "/api/assets/search", params=params).status_code == 422


def test_search_solo_espacios_devuelve_lista_vacia(use_session, monkeypatch):
    use_session(FakeSession())

    async def fail(q):
        raise AssertionError("No debería buscar")

    monkeypatch.setattr(catalog, "search_market", fail)

    response = _request("GET", "/api/assets/search", params={"q": "   "})

    assert response.status_code == 200 and response.json() == []
