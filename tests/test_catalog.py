"""Tests de app/services/catalog.py: funciones puras y llamadas simuladas (httpx.MockTransport, sin red)."""

import asyncio

import httpx
import pytest

from app.services import catalog
from app.services.catalog import (
    MarketCandidate,
    SlidingWindowLimiter,
    Validation,
    bond_bases,
    drop_known_and_variants,
    normalize_ticker,
    parse_bond_candidates,
    parse_chart_info,
    parse_yahoo_search,
    rank_and_limit,
    type_hint_from_name,
    variant_bases,
)
from app.services.market import fetch_bonds_prices


@pytest.fixture(autouse=True)
def empty_caches():
    """Cada test arranca sin cachés en memoria."""
    catalog.clear_caches()
    yield
    catalog.clear_caches()


# ── Datos de ejemplo (forma real de las respuestas) ───────────────────────────

BONDS_DATA = {
    "rueda": {"abierta": True},
    "bonds": [
        {"ticker": "AL30D", "tickerBase": "AL30", "nombre": "AL30 (Ley AR)", "tipo": "Soberano", "moneda": "USD", "precio": 54.3},
        {"ticker": "AL30C", "tickerBase": "AL30", "nombre": "AL30 (Ley AR)", "tipo": "Soberano", "moneda": "USD", "precio": 50.9},
        {"ticker": "GD46D", "tickerBase": "GD46", "nombre": "GD46 (Ley NY)", "tipo": "Soberano", "moneda": "USD", "precio": 60.0},
        # Soberano sin especie D (de prueba): moneda ARS
        {"ticker": "XS99", "tickerBase": "XS99", "nombre": "Soberano en pesos", "tipo": "Soberano", "moneda": "ARS", "precio": 120.5},
        # Otro tipo: no se ofrece
        {"ticker": "TX26", "tickerBase": "TX26", "nombre": "TX26 (CER)", "tipo": "CER", "moneda": "ARS", "precio": 762.3},
        # "TTC" captura por prefijo especies de otra base: precio ambiguo
        {"ticker": "TTC", "tickerBase": "TTC", "nombre": "Ambiguo", "tipo": "Soberano", "moneda": "USD", "precio": 10},
        {"ticker": "TTC8D", "tickerBase": "TTC8", "nombre": "ON", "tipo": "ON", "moneda": "USD", "precio": 99},
        # Base con caracteres no permitidos
        {"ticker": "BR_30", "tickerBase": "BR_30", "nombre": "Brasil", "tipo": "Soberano", "moneda": "USD", "precio": 100},
        # Sin nombre
        {"ticker": "AE38D", "tickerBase": "AE38", "tipo": "Soberano", "moneda": "USD", "precio": 71.2},
        "no es un objeto",
        {"ticker": None, "tickerBase": None},
    ],
}

SEARCH_GALICIA = {
    "quotes": [
        {"symbol": "GGAL", "exchange": "NCM", "quoteType": "EQUITY", "shortname": "Grupo Financiero Galicia S.A."},
        {"symbol": "GGAL.BA", "exchange": "BUE", "quoteType": "EQUITY",
         "shortname": "GRUPO FINANCIERO GALICIA SA", "longname": "Grupo Financiero Galicia S.A."},
        {"symbol": "GGALD.BA", "exchange": "BUE", "quoteType": "EQUITY",
         "shortname": "GRUPO FINANCIERO GALICIA SA", "longname": "Grupo Financiero Galicia S.A."},
        {"symbol": "GGALC.BA", "exchange": "BUE", "quoteType": "EQUITY", "shortname": "GRUPO FINANCIERO GALICIA SA"},
        {"symbol": "GGAL280121C00050000", "exchange": "OPR", "quoteType": "OPTION", "shortname": "GGAL Jan 2028 50.000 call"},
        {"symbol": "GF4.SG", "exchange": "STU", "quoteType": "EQUITY", "shortname": "Grupo Financiero Galicia S.A. R"},
    ]
}

SEARCH_NVDA_BA = {
    "quotes": [
        {"symbol": "NVDA.BA", "exchange": "BUE", "quoteType": "EQUITY",
         "shortname": "NVIDIA CORP CEDEAR EACH 24 REP ", "longname": "NVIDIA Corporation"},
    ]
}


def chart(meta: dict | None = None, closes: list | None = None) -> dict:
    """Respuesta de /v8/finance/chart con velas diarias y la meta indicada."""
    closes = [100.0, 101.5] if closes is None else closes
    base_meta = {
        "symbol": "NVDA.BA",
        "exchangeName": "BUE",
        "currency": "ARS",
        "longName": "NVIDIA Corporation",
        "shortName": "NVIDIA CORP CEDEAR EACH 24 REP ",
        "regularMarketTime": 1761580800,
    }
    return {
        "chart": {
            "result": [{
                "meta": {**base_meta, **(meta or {})},
                "timestamp": [1761480000 + 86400 * i for i in range(len(closes))],
                "indicators": {"quote": [{"close": closes, "high": closes, "low": closes}]},
            }],
            "error": None,
        }
    }


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# ── normalize_ticker ──────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    ("raw", "asset_type", "expected"),
    [
        ("GGAL.BA", "stock", "GGAL.BA"),
        (" ggal ", "stock", "GGAL.BA"),
        ("nvda.ba", "cedear", "NVDA.BA"),
        ("TGSU2", "stock", "TGSU2.BA"),
        ("gd46", "bond", "GD46"),
        ("AL30.BA", "bond", "AL30"),
    ],
)
def test_normalize_ticker_validos(raw, asset_type, expected):
    assert normalize_ticker(raw, asset_type) == expected


@pytest.mark.parametrize(
    ("raw", "asset_type"),
    [
        ("../x", "stock"),
        ("A B", "stock"),
        ("GGAL.BA/x", "stock"),
        ("GGAL.US", "stock"),
        ("GGAL.BA.BA", "cedear"),
        ("ABCDEFGHIJKLM", "stock"),  # 13 caracteres
        ("BR_30", "bond"),
        ("AL30.BA.BA", "bond"),
        ("", "stock"),
        ("   ", "bond"),
        ("?q=1", "stock"),
        (None, "stock"),
    ],
)
def test_normalize_ticker_rechaza_formatos_invalidos(raw, asset_type):
    assert normalize_ticker(raw, asset_type) is None


# ── type_hint_from_name (shortnames reales de Yahoo) ──────────────────────────

@pytest.mark.parametrize(
    "short_name",
    [
        "NVIDIA CORP CEDEAR EACH 24 REP ",
        "APPLE INC CEDEAR(REPR 1/20 SHR)",
        "COCA-COLA CO CEDEARS EACH 5  RE",
        "VISA INC CEDEARS (EACH 18 REP 1",
        "VISTA ENERGY S.A.B. DE C.V. CED",  # truncado
        "STATE ST SPDR S&P 500 ETF TR CE",  # truncado
        "ALIBABA GROUP HOLDING LTD CEDEA",  # truncado
    ],
)
def test_type_hint_cedear(short_name):
    assert type_hint_from_name(short_name) == "cedear"


@pytest.mark.parametrize(
    "short_name",
    [
        "GRUPO FINANCIERO GALICIA SA",
        "YPF SOCIEDAD ANONIMA",
        "PAMPA ENERGIA S.A.",
        "EMPRESA DISTR Y COMERC NORTE ED",
        "TRANSPORTADORA DE GAS DEL SUR S",
        "BOLSAS Y MERCADOS ARGENTINOS SA",
        # Caso conocido: CEDEAR sin señal en el nombre truncado; type_hint lo sugiere como acción y se aplica así
        "EXXONMOBIL HOLDINGS CORPORATION",
        None,
        "",
    ],
)
def test_type_hint_accion(short_name):
    assert type_hint_from_name(short_name) == "stock"


# ── Bonos ─────────────────────────────────────────────────────────────────────

def test_bond_bases_solo_tipos_ofrecidos():
    assert bond_bases(BONDS_DATA) == ["AE38", "AL30", "BR_30", "GD46", "TTC", "XS99"]


def test_parse_bond_candidates_con_precios_reales_de_fetch_bonds_prices():
    prices = asyncio.run(fetch_bonds_prices(bond_bases(BONDS_DATA), bonds_data=BONDS_DATA))

    candidates = parse_bond_candidates(BONDS_DATA, prices)

    assert candidates == [
        MarketCandidate("AE38", "AE38", "bond", "USD", None),
        MarketCandidate("AL30", "AL30 (Ley AR)", "bond", "USD", None),
        MarketCandidate("GD46", "GD46 (Ley NY)", "bond", "USD", None),
        MarketCandidate("XS99", "Soberano en pesos", "bond", "ARS", None),
    ]


def test_parse_bond_candidates_sin_precio_no_se_ofrece():
    candidates = parse_bond_candidates(BONDS_DATA, {"AL30": 54.3})
    assert [c.ticker for c in candidates] == ["AL30"]


def test_parse_bond_candidates_excluye_ambiguos_aunque_tengan_precio():
    candidates = parse_bond_candidates(BONDS_DATA, {"TTC": 10.0, "BR_30": 100.0, "TX26": 762.3})
    assert candidates == []


@pytest.mark.parametrize("data", [None, {}, {"bonds": None}, {"bonds": "x"}, {"bonds": [{}]}, []])
def test_parse_bond_candidates_estructura_rota(data):
    assert parse_bond_candidates(data, {"AL30": 1.0}) == []
    assert bond_bases(data) == []


# ── Yahoo: búsqueda ───────────────────────────────────────────────────────────

def test_parse_yahoo_search_filtra_buenos_aires():
    candidates = parse_yahoo_search(SEARCH_GALICIA)

    assert [c.ticker for c in candidates] == ["GGAL.BA", "GGALD.BA", "GGALC.BA"]
    first = candidates[0]
    assert first.name == "Grupo Financiero Galicia S.A."  # longname
    assert first.asset_type is None and first.currency is None
    assert first.type_hint == "stock"
    assert candidates[2].name == "GRUPO FINANCIERO GALICIA SA"  # sin longname usa shortname


def test_parse_yahoo_search_type_hint_cedear():
    [nvda] = parse_yahoo_search(SEARCH_NVDA_BA)
    assert nvda == MarketCandidate("NVDA.BA", "NVIDIA Corporation", None, None, "cedear")


def test_parse_yahoo_search_descarta_simbolos_invalidos_y_duplicados():
    data = {"quotes": [
        {"symbol": "../X.BA", "exchange": "BUE", "quoteType": "EQUITY"},
        {"symbol": "ABCDEFGHIJKLM.BA", "exchange": "BUE", "quoteType": "EQUITY"},
        {"symbol": "vist.ba", "exchange": "BUE", "quoteType": "EQUITY", "shortname": "VISTA ENERGY S.A.B. DE C.V. CED"},
        {"symbol": "VIST.BA", "exchange": "BUE", "quoteType": "EQUITY", "shortname": "otro"},
        {"symbol": "SPY.BA", "exchange": "BUE", "quoteType": "ETF", "shortname": "STATE ST SPDR S&P 500 ETF TR CE"},
        {"symbol": "XX.BA", "exchange": "BUE", "quoteType": "MUTUALFUND"},
        {"symbol": 123, "exchange": "BUE", "quoteType": "EQUITY"},
    ]}

    candidates = parse_yahoo_search(data)

    assert [(c.ticker, c.name, c.type_hint) for c in candidates] == [
        ("VIST.BA", "VISTA ENERGY S.A.B. DE C.V. CED", "cedear"),
        ("SPY.BA", "STATE ST SPDR S&P 500 ETF TR CE", "cedear"),
    ]


@pytest.mark.parametrize("data", [None, {}, {"quotes": None}, {"quotes": "x"}, {"quotes": [None, 1, "x"]}, []])
def test_parse_yahoo_search_estructura_rota(data):
    assert parse_yahoo_search(data) == []


# ── Variantes, catálogo conocido y orden ──────────────────────────────────────

def _yahoo(ticker: str) -> MarketCandidate:
    return MarketCandidate(ticker, ticker, None, None, "stock")


def test_drop_known_y_variantes():
    candidates = [_yahoo(t) for t in ("GGAL.BA", "GGALD.BA", "GGALC.BA", "YPFD.BA", "YPFDD.BA", "VIST.BA")]

    visible = drop_known_and_variants(candidates, known={"GGAL.BA", "YPFD.BA"})

    # GGAL.BA e YPFD.BA ya están en el catálogo; sus variantes se ocultan
    assert [c.ticker for c in visible] == ["VIST.BA"]


def test_variantes_se_ocultan_si_la_base_esta_en_los_resultados():
    candidates = [_yahoo(t) for t in ("GGAL.BA", "GGALD.BA", "YPFD.BA", "MELID.BA")]

    visible = drop_known_and_variants(candidates, known=set())

    # YPFD.BA queda porque YPF.BA no existe; MELID.BA queda porque MELI.BA no está ni en catálogo ni en resultados
    assert [c.ticker for c in visible] == ["GGAL.BA", "YPFD.BA", "MELID.BA"]


def test_variant_bases():
    candidates = [_yahoo("GGALD.BA"), _yahoo("YPFDD.BA"), _yahoo("V.BA"), MarketCandidate("AL30", "x", "bond", "USD", None)]
    assert variant_bases(candidates) == {"GGAL.BA", "YPFD.BA"}


def test_rank_and_limit_ordena_por_relevancia_y_deduplica():
    candidates = [
        MarketCandidate("AGD46", "Otro", "bond", "USD", None),
        _yahoo("XGD.BA"),
        MarketCandidate("GD46", "GD46 (Ley NY)", "bond", "USD", None),
        _yahoo("GD.BA"),
        _yahoo("GD.BA"),
        MarketCandidate("AL30", "Bono que menciona gd", "bond", "USD", None),
    ]

    ranked = rank_and_limit(candidates, "gd", limit=10)

    assert [c.ticker for c in ranked] == ["GD.BA", "GD46", "AGD46", "XGD.BA", "AL30"]
    assert [c.ticker for c in rank_and_limit(candidates, "gd", limit=2)] == ["GD.BA", "GD46"]


def test_rank_and_limit_a_igual_relevancia_primero_el_ticker_mas_corto():
    candidates = [_yahoo("GGALD.BA"), _yahoo("GGADB.BA"), _yahoo("GGAL.BA")]

    ranked = rank_and_limit(candidates, "galicia", limit=10)

    assert [c.ticker for c in ranked] == ["GGAL.BA", "GGADB.BA", "GGALD.BA"]


# ── Yahoo: chart ──────────────────────────────────────────────────────────────

def test_parse_chart_info_ok():
    assert parse_chart_info(chart()) == catalog.ChartInfo("NVIDIA Corporation", "ARS")


def test_parse_chart_info_moneda_usd_y_desconocida():
    assert parse_chart_info(chart({"currency": "USD"})).currency == "USD"
    assert parse_chart_info(chart({"currency": "BRL"})).currency == "ARS"


def test_parse_chart_info_sin_longname_usa_shortname():
    assert parse_chart_info(chart({"longName": None})).name == "NVIDIA CORP CEDEAR EACH 24 REP"


@pytest.mark.parametrize(
    "data",
    [
        None,
        {},
        {"chart": {"result": None}},
        {"chart": {"result": []}},
        chart(closes=[]),  # sin velas
        chart(closes=[None, None]),  # velas sin cierre
        chart({"exchangeName": "NMS"}),  # no es Buenos Aires
        chart({"longName": None, "shortName": None, "symbol": None}),  # sin nombre
    ],
)
def test_parse_chart_info_invalido(data):
    assert parse_chart_info(data) is None


# ── Límite de búsquedas ───────────────────────────────────────────────────────

def test_limiter_rechaza_la_llamada_61_y_libera_al_minuto():
    limiter = SlidingWindowLimiter(60, window_seconds=60)

    assert all(limiter.allow(1, now=float(i) / 10) for i in range(60))
    assert limiter.allow(1, now=10.0) is False
    assert limiter.allow(2, now=10.0) is True  # otro usuario no se ve afectado
    assert limiter.allow(1, now=60.1) is True  # ya salió la primera llamada de la ventana


# ── Yahoo con respuestas simuladas ────────────────────────────────────────────

def test_search_yahoo_con_forma_de_ticker_busca_tambien_con_sufijo():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.params["q"] == "NVDA.BA":
            return httpx.Response(200, json=SEARCH_NVDA_BA)
        return httpx.Response(200, json={"quotes": [{"symbol": "NVDA", "exchange": "NMS", "quoteType": "EQUITY"}]})

    async def call():
        async with _client(handler) as client:
            return await catalog.search_yahoo("nvda", client=client)

    result = asyncio.run(call())

    assert [c.ticker for c in result] == ["NVDA.BA"]
    assert sorted(r.url.params["q"] for r in requests) == ["NVDA.BA", "nvda"]
    assert all(r.url.path == "/v1/finance/search" for r in requests)
    assert all("User-Agent" in r.headers for r in requests)


def test_search_yahoo_texto_libre_viaja_como_parametro():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=SEARCH_GALICIA)

    async def call():
        async with _client(handler) as client:
            return await catalog.search_yahoo("banco/../galicia", client=client)

    result = asyncio.run(call())

    assert len(requests) == 1  # no tiene forma de ticker: una sola consulta
    assert requests[0].url.path == "/v1/finance/search"
    assert requests[0].url.params["q"] == "banco/../galicia"
    assert [c.ticker for c in result] == ["GGAL.BA", "GGALD.BA", "GGALC.BA"]


@pytest.mark.parametrize("failure", ["429", "500", "timeout", "not_json"])
def test_search_yahoo_caido_devuelve_none(failure):
    def handler(request: httpx.Request) -> httpx.Response:
        if failure == "timeout":
            raise httpx.ReadTimeout("timeout", request=request)
        if failure == "not_json":
            return httpx.Response(200, text="<html>")
        return httpx.Response(int(failure), json={})

    async def call():
        async with _client(handler) as client:
            return await catalog.search_yahoo("GGAL", client=client)

    assert asyncio.run(call()) is None


def test_search_yahoo_una_consulta_falla_y_la_otra_responde():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params["q"] == "NVDA.BA":
            return httpx.Response(200, json=SEARCH_NVDA_BA)
        return httpx.Response(429, json={})

    async def call():
        async with _client(handler) as client:
            return await catalog.search_yahoo("NVDA", client=client)

    assert [c.ticker for c in asyncio.run(call())] == ["NVDA.BA"]


def _validate(handler, ticker="NVDA.BA") -> tuple[Validation, list[httpx.Request]]:
    requests: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    async def call():
        async with _client(recording) as client:
            return await catalog.validate_yahoo(ticker, client=client)

    return asyncio.run(call()), requests


def test_validate_yahoo_ok():
    result, requests = _validate(lambda r: httpx.Response(200, json=chart({"currency": "USD"})))

    assert result == Validation("ok", "NVIDIA Corporation", "USD")
    assert requests[0].url.path == "/v8/finance/chart/NVDA.BA"
    assert requests[0].url.params["interval"] == "1d"


@pytest.mark.parametrize(
    ("response", "status"),
    [
        (httpx.Response(404, json={"chart": {"result": None, "error": {"code": "Not Found"}}}), "not_found"),
        (httpx.Response(400, json={}), "not_found"),
        (httpx.Response(200, json=chart(closes=[])), "not_found"),
        (httpx.Response(200, json=chart({"exchangeName": "NMS"})), "not_found"),
        (httpx.Response(429, text="Too Many Requests"), "unavailable"),
        (httpx.Response(503, text="error"), "unavailable"),
        (httpx.Response(200, text="<html>"), "unavailable"),
    ],
)
def test_validate_yahoo_respuestas(response, status):
    result, _ = _validate(lambda r: response)
    assert result.status == status


def test_validate_yahoo_timeout_es_unavailable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timeout", request=request)

    result, _ = _validate(handler)
    assert result == Validation("unavailable")


@pytest.mark.parametrize("ticker", ["../x", "NVDA", "NVDA.BA/../x", "nvda.ba", ""])
def test_validate_yahoo_formato_invalido_no_hace_peticiones(ticker):
    result, requests = _validate(lambda r: httpx.Response(200, json=chart()), ticker=ticker)
    assert result == Validation("not_found")
    assert requests == []


# ── Bonos con fetch_bonds_data simulado ───────────────────────────────────────

def _patch_bonds(monkeypatch, responses: list):
    """Reemplaza fetch_bonds_data por una función que devuelve `responses` en orden y cuenta llamadas."""
    calls = []

    async def fake_fetch():
        calls.append(1)
        return responses[min(len(calls), len(responses)) - 1]

    monkeypatch.setattr(catalog, "fetch_bonds_data", fake_fetch)
    return calls


def test_get_bond_candidates_usa_cache(monkeypatch):
    calls = _patch_bonds(monkeypatch, [BONDS_DATA])

    first = asyncio.run(catalog.get_bond_candidates())
    second = asyncio.run(catalog.get_bond_candidates())

    assert [c.ticker for c in first] == ["AE38", "AL30", "GD46", "XS99"]
    assert second == first
    assert len(calls) == 1


def test_get_bond_candidates_api_caida_usa_la_ultima_lista(monkeypatch):
    calls = _patch_bonds(monkeypatch, [BONDS_DATA, None])
    first = asyncio.run(catalog.get_bond_candidates())
    monkeypatch.setattr(catalog, "_bond_cache", (0.0, first))  # caché vencida

    assert asyncio.run(catalog.get_bond_candidates()) == first
    assert len(calls) == 2


def test_validate_bond(monkeypatch):
    _patch_bonds(monkeypatch, [BONDS_DATA])

    assert asyncio.run(catalog.validate_bond("GD46")) == Validation("ok", "GD46 (Ley NY)", "USD")
    assert asyncio.run(catalog.validate_bond("XS99")).currency == "ARS"
    assert asyncio.run(catalog.validate_bond("TX26")) == Validation("not_found")
    assert asyncio.run(catalog.validate_bond("../x")) == Validation("not_found")


def test_validate_bond_sin_lista_es_unavailable(monkeypatch):
    _patch_bonds(monkeypatch, [None])
    assert asyncio.run(catalog.validate_bond("GD46")) == Validation("unavailable")


# ── search_market ─────────────────────────────────────────────────────────────

def _patch_yahoo(monkeypatch, result):
    calls = []

    async def fake_search(q, *, client=None):
        calls.append(q)
        return result

    monkeypatch.setattr(catalog, "search_yahoo", fake_search)
    return calls


def test_search_market_yahoo_caido_devuelve_bonos_sin_cachear(monkeypatch):
    _patch_bonds(monkeypatch, [BONDS_DATA])
    yahoo_calls = _patch_yahoo(monkeypatch, None)

    first = asyncio.run(catalog.search_market("ley ny"))
    asyncio.run(catalog.search_market("ley ny"))

    assert [c.ticker for c in first] == ["GD46"]  # coincide por nombre
    assert len(yahoo_calls) == 2  # sin Yahoo no se guarda en caché


def test_search_market_combina_y_cachea(monkeypatch):
    _patch_bonds(monkeypatch, [BONDS_DATA])
    yahoo_calls = _patch_yahoo(monkeypatch, [MarketCandidate("AL30X.BA", "x", None, None, "stock")])

    first = asyncio.run(catalog.search_market(" AL30 "))
    second = asyncio.run(catalog.search_market("al30"))

    assert [c.ticker for c in first] == ["AL30", "AL30X.BA"]
    assert second == first
    assert yahoo_calls == ["al30"]


def test_search_market_vacio_no_consulta(monkeypatch):
    yahoo_calls = _patch_yahoo(monkeypatch, [])
    assert asyncio.run(catalog.search_market("   ")) == []
    assert yahoo_calls == []
