"""
app/services/catalog.py
───────────────────────
Catálogo a pedido: busca en el mercado activos que todavía no están en el catálogo
y valida un ticker contra su proveedor antes de darlo de alta.

Proveedores (los mismos que usa el worker para cada asset_type):
  - Bonos soberanos: lista de CompararFondos (fetch_bonds_data y fetch_bonds_prices de
    market.py). Solo se ofrecen bases de tipo "Soberano" cuyo precio no sea ambiguo.
  - Acciones y CEDEARs: Yahoo Finance; /v1/finance/search para buscar y
    /v8/finance/chart para validar (tickers de Buenos Aires, con sufijo .BA).

Reglas aplicadas (ver AGENTS.md §5.2):
  - httpx.AsyncClient con las cabeceras y el timeout de 10 s de market.py.
  - Ningún texto del usuario se arma dentro de una URL: los tickers pasan antes por
    TICKER_RE y la búsqueda viaja como parámetro de consulta (httpx la codifica).
  - Las funciones de red no lanzan excepciones: ante un error devuelven None o
    status "unavailable", así la búsqueda sigue funcionando aunque Yahoo falle.
  - Cachés y límite de búsquedas en memoria (la app corre en un solo proceso).

Funciones públicas:
  normalize_ticker(raw, asset_type)            → str | None   (pura)
  type_hint_from_name(short_name)              → "cedear" | "stock"   (pura)
  parse_bond_candidates(data, prices)          → list[MarketCandidate]   (pura)
  parse_yahoo_search(data)                     → list[MarketCandidate]   (pura)
  parse_chart_info(data)                       → ChartInfo | None   (pura)
  variant_bases(candidates)                    → set[str]   (pura)
  drop_known_and_variants(candidates, known)   → list[MarketCandidate]   (pura)
  rank_and_limit(candidates, q, limit)         → list[MarketCandidate]   (pura)
  get_bond_candidates()                        → list[MarketCandidate] | None
  search_yahoo(q)                              → list[MarketCandidate] | None
  search_market(q)                             → list[MarketCandidate]
  validate_yahoo(ticker)                       → Validation
  validate_bond(base)                          → Validation
"""

import asyncio
from collections import OrderedDict, deque
from collections.abc import Hashable, Mapping, Sequence
from dataclasses import dataclass
import logging
import re
import time
from typing import Literal

import httpx

from app.services.market import _HEADERS, _TIMEOUT, _YF_URL, fetch_bonds_data, fetch_bonds_prices
from app.services.market_series import parse_chart_series

logger = logging.getLogger(__name__)

# ── Constantes ────────────────────────────────────────────────────────────────

# Formato aceptado para un ticker: hasta 12 letras o números, con sufijo .BA opcional.
# Se valida ANTES de usar el ticker en una URL.
TICKER_RE = re.compile(r"^[A-Z0-9]{1,12}(\.BA)?$")
# Base de un bono (sin sufijo) o búsqueda con forma de ticker
_BASE_RE = re.compile(r"^[A-Z0-9]{1,12}$")

BOND_TYPES = {"Soberano"}
YAHOO_EXCHANGE = "BUE"
YAHOO_QUOTE_TYPES = {"EQUITY", "ETF"}
# Especies de la misma acción en otra moneda o liquidación (GGALD.BA, GGALC.BA, GGALB.BA)
VARIANT_SUFFIXES = ("D", "C", "B")
CURRENCIES = {"ARS", "USD"}
# Misma longitud que la columna assets.name
MAX_NAME_LENGTH = 150

# Yahoo corta shortName en ~31 caracteres: "CEDEAR" completo o un "CE…" truncado al final
CEDEAR_HINT_RE = re.compile(r"CEDEARS?|\sCE(D(E(A(R)?)?)?)?\s*$")

_YF_SEARCH_URL = "https://query1.finance.yahoo.com/v1/finance/search"
_SEARCH_PARAMS = {"quotesCount": 20, "newsCount": 0, "listsCount": 0}
# Velas diarias de la última semana: valida también en fines de semana y feriados
_VALIDATE_PARAMS = {"interval": "1d", "range": "5d"}

BOND_CACHE_SECONDS = 10 * 60
SEARCH_CACHE_SECONDS = 5 * 60
SEARCH_CACHE_MAX_ENTRIES = 500
SEARCH_LIMIT_PER_MINUTE = 60


# ── Tipos ─────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class MarketCandidate:
    """Activo del mercado que se puede sumar al catálogo."""

    ticker: str  # "VIST.BA" (Yahoo) o "GD46" (bono)
    name: str
    asset_type: str | None  # "bond"; None en Yahoo (el tipo sale de type_hint y se aplica automáticamente)
    currency: str | None  # bonos: "USD" o "ARS"; Yahoo: None (la búsqueda no trae la moneda)
    type_hint: str | None  # Yahoo: "cedear" o "stock" según el nombre; bonos: None


@dataclass(frozen=True)
class ChartInfo:
    """Datos de un ticker validado en /v8/finance/chart."""

    name: str
    currency: str


@dataclass(frozen=True)
class Validation:
    """Resultado de validar un ticker: ok (con nombre y moneda), no existe o proveedor caído."""

    status: Literal["ok", "not_found", "unavailable"]
    name: str | None = None
    currency: str | None = None


# ── Funciones puras ───────────────────────────────────────────────────────────

def _clean_name(value) -> str | None:
    """Nombre sin espacios sobrantes y cortado al largo de la columna; None si no es texto."""
    if not isinstance(value, str):
        return None
    name = " ".join(value.split())
    return name[:MAX_NAME_LENGTH] or None


def type_hint_from_name(short_name) -> str:
    """'cedear' si el shortName de Yahoo indica un CEDEAR (completo o truncado al final); si no, 'stock'."""
    if isinstance(short_name, str) and CEDEAR_HINT_RE.search(short_name.upper()):
        return "cedear"
    return "stock"


def normalize_ticker(raw, asset_type: str) -> str | None:
    """
    Normaliza el ticker que escribió el usuario: mayúsculas y sin espacios. Los bonos quedan
    como base sin sufijo ("AL30") y las acciones y CEDEARs con ".BA" ("NVDA.BA").
    Devuelve None si el resultado no respeta el formato permitido.
    """
    if not isinstance(raw, str):
        return None
    value = raw.strip().upper()
    if asset_type == "bond":
        value = value.removesuffix(".BA")
        return value if _BASE_RE.fullmatch(value) else None
    if not value.endswith(".BA"):
        value += ".BA"
    return value if TICKER_RE.fullmatch(value) else None


def _bond_rows(data) -> list[dict]:
    """Especies de la respuesta de /api/bonos con ticker y tickerBase en mayúsculas."""
    if not isinstance(data, dict) or not isinstance(data.get("bonds"), list):
        return []
    rows = []
    for bond in data["bonds"]:
        if not isinstance(bond, dict):
            continue
        ticker = str(bond.get("ticker") or "").strip().upper()
        base = str(bond.get("tickerBase") or "").strip().upper()
        if ticker and base:
            rows.append({**bond, "ticker": ticker, "tickerBase": base})
    return rows


def ambiguous_bases(rows: Sequence[dict]) -> set[str]:
    """
    Bases cuyo precio sería ambiguo: fetch_bonds_prices también acepta especies cuyo ticker
    empieza con la base, así que una base es ambigua si captura especies de otra base
    (por ejemplo "TTC" con "TTC8D").
    """
    ambiguous = set()
    bases = {row["tickerBase"] for row in rows}
    for base in bases:
        if any(row["tickerBase"] != base and row["ticker"].startswith(base) for row in rows):
            ambiguous.add(base)
    return ambiguous


def bond_bases(data) -> list[str]:
    """Bases de los tipos ofrecidos (BOND_TYPES), para pedir sus precios a fetch_bonds_prices."""
    return sorted({row["tickerBase"] for row in _bond_rows(data) if row.get("tipo") in BOND_TYPES})


def parse_bond_candidates(data, prices: Mapping[str, float]) -> list[MarketCandidate]:
    """
    Bonos de los tipos ofrecidos con base válida, no ambigua y con precio en `prices`
    (resultado de fetch_bonds_prices). Moneda USD si existe la especie "<base>D"; si no, ARS.
    Ante una estructura inesperada devuelve una lista vacía.
    """
    rows = _bond_rows(data)
    ambiguous = ambiguous_bases(rows)
    by_base: dict[str, list[dict]] = {}
    for row in rows:
        by_base.setdefault(row["tickerBase"], []).append(row)

    candidates = []
    for base, species in sorted(by_base.items()):
        if not any(row.get("tipo") in BOND_TYPES for row in species):
            continue
        if not _BASE_RE.fullmatch(base) or base in ambiguous:
            continue
        price = prices.get(base)
        if isinstance(price, bool) or not isinstance(price, (int, float)) or price <= 0:
            continue
        has_dollar_species = any(row["ticker"] == f"{base}D" for row in species)
        name = next((n for n in (_clean_name(row.get("nombre")) for row in species) if n), base)
        candidates.append(MarketCandidate(base, name, "bond", "USD" if has_dollar_species else "ARS", None))
    return candidates


def parse_yahoo_search(data) -> list[MarketCandidate]:
    """
    Resultados de /v1/finance/search que cotizan en Buenos Aires: símbolo .BA con formato
    válido, mercado BUE y tipo EQUITY o ETF. Sin duplicados; estructura inesperada → [].
    """
    quotes = data.get("quotes") if isinstance(data, dict) else None
    if not isinstance(quotes, list):
        return []

    candidates: dict[str, MarketCandidate] = {}
    for quote in quotes:
        if not isinstance(quote, dict):
            continue
        symbol = str(quote.get("symbol") or "").strip().upper()
        if not symbol.endswith(".BA") or not TICKER_RE.fullmatch(symbol) or symbol in candidates:
            continue
        if quote.get("exchange") != YAHOO_EXCHANGE or quote.get("quoteType") not in YAHOO_QUOTE_TYPES:
            continue
        short_name = quote.get("shortname")
        name = _clean_name(quote.get("longname")) or _clean_name(short_name) or symbol
        candidates[symbol] = MarketCandidate(symbol, name, None, None, type_hint_from_name(short_name))
    return list(candidates.values())


def parse_chart_info(data) -> ChartInfo | None:
    """
    Valida la respuesta de /v8/finance/chart: tiene que traer velas y ser del mercado de
    Buenos Aires. Devuelve nombre (longName o shortName) y moneda (ARS o USD; otra cae en ARS).
    """
    if not parse_chart_series(data).candles:
        return None
    try:
        meta = data["chart"]["result"][0]["meta"]
    except (KeyError, IndexError, TypeError):
        return None
    if not isinstance(meta, dict) or meta.get("exchangeName") != YAHOO_EXCHANGE:
        return None

    name = _clean_name(meta.get("longName")) or _clean_name(meta.get("shortName")) or _clean_name(meta.get("symbol"))
    if name is None:
        return None
    currency = str(meta.get("currency") or "").upper()
    return ChartInfo(name, currency if currency in CURRENCIES else "ARS")


def _variant_base(ticker: str) -> str | None:
    """Ticker base de una posible variante (de GGALD.BA devuelve GGAL.BA); None si no puede serlo."""
    if not ticker.endswith(".BA"):
        return None
    base = ticker.removesuffix(".BA")
    if len(base) < 2 or base[-1] not in VARIANT_SUFFIXES:
        return None
    return f"{base[:-1]}.BA"


def variant_bases(candidates: Sequence[MarketCandidate]) -> set[str]:
    """Tickers base de las posibles variantes, para consultar si ya están en el catálogo."""
    return {base for base in map(_variant_base, (c.ticker for c in candidates)) if base}


def drop_known_and_variants(candidates: Sequence[MarketCandidate], known: set[str]) -> list[MarketCandidate]:
    """
    Saca los candidatos que ya están en el catálogo (`known`) y las variantes X{D,C,B}.BA
    cuando X.BA está en el catálogo o entre los mismos candidatos ("YPFD.BA" se conserva
    porque "YPF.BA" no existe).
    """
    present = known | {c.ticker for c in candidates}
    return [
        c for c in candidates
        if c.ticker not in known and _variant_base(c.ticker) not in present
    ]


def matches_query(candidate: MarketCandidate, q: str) -> bool:
    """Coincidencia por ticker o nombre, sin distinguir mayúsculas."""
    query = q.strip().lower()
    return bool(query) and (query in candidate.ticker.lower() or query in candidate.name.lower())


def rank_and_limit(candidates: Sequence[MarketCandidate], q: str, limit: int) -> list[MarketCandidate]:
    """
    Ordena por relevancia (ticker exacto, ticker que empieza con q, ticker que contiene q,
    resto por nombre) y, a igual relevancia, el ticker más corto primero (la especie principal
    antes que sus variantes). Deduplica por ticker y devuelve como máximo `limit`.
    """
    query = q.strip().upper()

    def relevance(candidate: MarketCandidate) -> tuple[int, int, str]:
        base = candidate.ticker.removesuffix(".BA")
        if query in (candidate.ticker, base):
            rank = 0
        elif base.startswith(query):
            rank = 1
        elif query in candidate.ticker:
            rank = 2
        else:
            rank = 3
        return rank, len(candidate.ticker), candidate.ticker

    unique: dict[str, MarketCandidate] = {}
    for candidate in candidates:
        unique.setdefault(candidate.ticker, candidate)
    return sorted(unique.values(), key=relevance)[:max(limit, 0)]


class SlidingWindowLimiter:
    """Límite de llamadas por clave (usuario) en una ventana móvil; vive en memoria."""

    def __init__(self, max_calls: int, window_seconds: float = 60.0):
        self.max_calls = max_calls
        self.window_seconds = window_seconds
        self._calls: dict[Hashable, deque[float]] = {}

    def allow(self, key: Hashable, now: float | None = None) -> bool:
        """Registra la llamada y devuelve True si todavía está dentro del límite."""
        now = time.monotonic() if now is None else now
        calls = self._calls.setdefault(key, deque())
        while calls and calls[0] <= now - self.window_seconds:
            calls.popleft()
        if len(calls) >= self.max_calls:
            return False
        calls.append(now)
        return True


search_limiter = SlidingWindowLimiter(SEARCH_LIMIT_PER_MINUTE)


# ── Cachés en memoria ─────────────────────────────────────────────────────────

# (vence, candidatos) de la última lista de bonos obtenida
_bond_cache: tuple[float, list[MarketCandidate]] | None = None
# q normalizada → (vence, candidatos); las entradas más viejas se descartan primero
_search_cache: OrderedDict[str, tuple[float, list[MarketCandidate]]] = OrderedDict()


def clear_caches() -> None:
    """Vacía las cachés (para los tests)."""
    global _bond_cache
    _bond_cache = None
    _search_cache.clear()


# ── Funciones de red ──────────────────────────────────────────────────────────

async def get_bond_candidates() -> list[MarketCandidate] | None:
    """
    Bonos ofrecidos según la lista de CompararFondos, con caché de BOND_CACHE_SECONDS.
    Si la API no responde devuelve la última lista conocida, o None si nunca se obtuvo.
    """
    global _bond_cache
    now = time.monotonic()
    if _bond_cache is not None and _bond_cache[0] > now:
        return _bond_cache[1]

    data = await fetch_bonds_data()
    if data is None:
        return _bond_cache[1] if _bond_cache is not None else None

    prices = await fetch_bonds_prices(bond_bases(data), bonds_data=data)
    candidates = parse_bond_candidates(data, prices)
    _bond_cache = (now + BOND_CACHE_SECONDS, candidates)
    return candidates


async def _yahoo_search_once(client: httpx.AsyncClient, query: str) -> list[MarketCandidate] | None:
    """Una consulta a /v1/finance/search; None si falla (timeout, 429, error HTTP o JSON inválido)."""
    try:
        response = await client.get(
            _YF_SEARCH_URL, params={"q": query, **_SEARCH_PARAMS}, headers=_HEADERS, timeout=_TIMEOUT
        )
    except httpx.HTTPError as exc:
        logger.warning("Error al buscar en Yahoo Finance: %s", type(exc).__name__)
        return None
    if response.status_code != 200:
        logger.warning("La búsqueda de Yahoo Finance respondió HTTP %s.", response.status_code)
        return None
    try:
        return parse_yahoo_search(response.json())
    except ValueError:
        logger.warning("La búsqueda de Yahoo Finance devolvió una respuesta que no es JSON.")
        return None


async def search_yahoo(q: str, *, client: httpx.AsyncClient | None = None) -> list[MarketCandidate] | None:
    """
    Busca en Yahoo los activos de Buenos Aires para `q`. Si `q` tiene forma de ticker también
    busca "<q>.BA", que trae el activo exacto aunque la búsqueda global no lo incluya.
    Devuelve None si todas las consultas fallaron.
    """
    query = q.strip()
    queries = [query]
    if _BASE_RE.fullmatch(query.upper()):
        queries.append(f"{query.upper()}.BA")

    async def _run(c: httpx.AsyncClient) -> list[list[MarketCandidate] | None]:
        return await asyncio.gather(*[_yahoo_search_once(c, item) for item in queries])

    if client is not None:
        results = await _run(client)
    else:
        async with httpx.AsyncClient(follow_redirects=True) as c:
            results = await _run(c)

    if all(result is None for result in results):
        return None
    merged: dict[str, MarketCandidate] = {}
    for result in results:
        for candidate in result or []:
            merged.setdefault(candidate.ticker, candidate)
    return list(merged.values())


async def search_market(q: str) -> list[MarketCandidate]:
    """
    Candidatos del mercado para `q`: bonos cuya base o nombre contienen q y resultados de Yahoo.
    Caché por q de SEARCH_CACHE_SECONDS; si Yahoo falla se devuelven solo los bonos y ese
    resultado no se guarda en caché. Nunca lanza excepciones.
    """
    key = q.strip().lower()
    if not key:
        return []

    now = time.monotonic()
    cached = _search_cache.get(key)
    if cached is not None and cached[0] > now:
        return cached[1]

    bonds, yahoo = await asyncio.gather(get_bond_candidates(), search_yahoo(key))
    candidates = [bond for bond in bonds or [] if matches_query(bond, key)] + (yahoo or [])

    if yahoo is not None:
        _search_cache[key] = (now + SEARCH_CACHE_SECONDS, candidates)
        _search_cache.move_to_end(key)
        while len(_search_cache) > SEARCH_CACHE_MAX_ENTRIES:
            _search_cache.popitem(last=False)
    return candidates


async def validate_yahoo(ticker: str, *, client: httpx.AsyncClient | None = None) -> Validation:
    """
    Valida un ticker de Buenos Aires en /v8/finance/chart: tiene que traer velas del mercado BUE.
    404 o sin velas → not_found; 429, 5xx, timeout o error de red → unavailable.
    """
    if not isinstance(ticker, str) or not ticker.endswith(".BA") or not TICKER_RE.fullmatch(ticker):
        return Validation("not_found")
    url = _YF_URL.format(ticker=ticker)

    async def _fetch(c: httpx.AsyncClient) -> Validation:
        try:
            response = await c.get(url, params=_VALIDATE_PARAMS, headers=_HEADERS, timeout=_TIMEOUT)
        except httpx.HTTPError as exc:
            logger.warning("Error al validar '%s' en Yahoo Finance: %s", ticker, type(exc).__name__)
            return Validation("unavailable")
        if response.status_code in (400, 404):
            return Validation("not_found")
        if response.status_code != 200:
            logger.warning("Yahoo Finance respondió HTTP %s al validar '%s'.", response.status_code, ticker)
            return Validation("unavailable")
        try:
            info = parse_chart_info(response.json())
        except ValueError:
            return Validation("unavailable")
        return Validation("ok", info.name, info.currency) if info else Validation("not_found")

    if client is not None:
        return await _fetch(client)
    async with httpx.AsyncClient(follow_redirects=True) as c:
        return await _fetch(c)


async def validate_bond(base: str) -> Validation:
    """Valida la base de un bono contra la lista de bonos ofrecidos (get_bond_candidates)."""
    if not isinstance(base, str) or not _BASE_RE.fullmatch(base):
        return Validation("not_found")
    candidates = await get_bond_candidates()
    if candidates is None:
        return Validation("unavailable")
    match = next((c for c in candidates if c.ticker == base), None)
    return Validation("ok", match.name, match.currency) if match else Validation("not_found")
