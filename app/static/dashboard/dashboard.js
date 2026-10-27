/**
 * dashboard.js — Pantalla principal de MonitorPrecios.
 *
 * Columna izquierda: cartera del usuario y catálogo (pestañas, filtros, buscador).
 * Columna derecha:   cotización del activo seleccionado y su gráfico (Chart.js),
 *                    y debajo el resumen semanal de la cartera analizado por IA.
 *
 * Endpoints que consume (todos con la cookie HttpOnly de sesión):
 *   GET    /api/auth/me                         usuario actual
 *   GET    /api/assets/me                       cartera
 *   GET    /api/assets                          catálogo
 *   POST   /api/assets/me   { asset_ids: [id] } seguir un activo
 *   GET    /api/assets/search?q=                activos del mercado que no están en el catálogo
 *   POST   /api/assets/custom { ticker, asset_type } seguir un activo del mercado
 *   DELETE /api/assets/me/{asset_id}            dejar de seguir
 *   GET    /api/assets/{ticker}/history?limit=  lecturas de precio
 *   GET    /api/summary/weekly?week_offset=     resumen semanal guardado (404 si no hay)
 *   POST   /api/summary/weekly?week_offset=     generar el resumen semanal
 *
 * Cada 60 segundos se vuelven a pedir la cartera y el historial del activo
 * seleccionado. Ante un 401 se redirige al login.
 */

// ───────────────────────── Configuración ─────────────────────────

const LOGIN_URL = "/";
const ONBOARDING_URL = "/onboarding";
const REFRESH_INTERVAL_MS = 60000;
const DEFAULT_LIMIT = 60;
// Espera desde la última tecla antes de buscar en el mercado
const MARKET_SEARCH_DELAY_MS = 250;

// Preferencia guardada en el navegador desde la pantalla Perfil (settings.js)
const PREF_CURRENCY_KEY = "mp.pref.divisa";

const CATEGORY_LABELS = { stock: "Acción", bond: "Bono", cedear: "CEDEAR" };
const CURRENCY_SYMBOLS = { ARS: "$", USD: "US$" };

// Colores de la curva según el signo de la variación
const TREND_COLORS = { sube: "#10b981", baja: "#ef4444", neutro: "#94a3b8" };

// ───────────────────────── Estado ─────────────────────────

const state = {
  myAssets: [], // cartera del usuario
  catalog: [], // catálogo completo (se carga al abrir su pestaña)
  catalogLoaded: false,
  tab: "mine", // "mine" | "catalog"
  category: "all", // "all" | "stock" | "bond" | "cedear"
  currency: "todas", // "todas" | "ARS" | "USD" (preferencia del usuario)
  search: "",
  marketResults: [], // activos del mercado para marketQuery (se muestran debajo del catálogo)
  marketQuery: "", // búsqueda a la que corresponden marketResults
  marketRequestId: 0, // para descartar respuestas viejas mientras se sigue escribiendo
  marketTimer: null,
  selectedTicker: null,
  limit: DEFAULT_LIMIT,
  history: [], // lecturas válidas del activo seleccionado
  historyRequestId: 0, // para descartar respuestas viejas al cambiar rápido de activo
  chart: null,
  refreshTimer: null,
  summaryOffset: 0, // 0 = esta semana, 1 = semana pasada
  summaryRequestId: 0, // para descartar respuestas viejas al cambiar de semana
  summaryGenerating: false,
};

// ───────────────────────── Elementos de la página ─────────────────────────

const $ = (selector) => document.querySelector(selector);

const listBox = $("#lista-activos");
const searchInput = $("#buscador");
const toastsBox = $("#toasts");
const detailBox = $("#detalle");
const noSelectionBox = $("#sin-seleccion");
const chartCanvasBox = $("#grafico-lienzo");
const chartLoading = $("#grafico-cargando");
const chartEmpty = $("#grafico-vacio");
const chartError = $("#grafico-error");
const summaryStatus = $("#resumen-estado");
const summaryBody = $("#resumen-cuerpo");
const summaryButton = $("#btn-resumen");

// ───────────────────────── Utilidades ─────────────────────────

/** Hace una petición autenticada al backend y devuelve el JSON; ante un 401 redirige al login. */
async function api(path, options = {}) {
  let res;
  try {
    res = await fetch(path, {
      credentials: "include",
      headers: options.body ? { "Content-Type": "application/json" } : undefined,
      ...options,
    });
  } catch {
    throw new Error("No pudimos conectar con el servidor. Revisá tu conexión.");
  }

  if (res.status === 401) {
    stopAutoRefresh();
    window.location.href = LOGIN_URL;
    throw new Error("Tu sesión venció. Iniciá sesión de nuevo.");
  }

  if (!res.ok) {
    let detail = null;
    try {
      detail = (await res.json()).detail;
    } catch {
      // La respuesta no era JSON.
    }
    const error = new Error(typeof detail === "string" ? detail : `El servidor respondió con un error (${res.status}).`);
    error.status = res.status;
    throw error;
  }

  return res.status === 204 ? null : res.json();
}

/** Muestra un aviso flotante que desaparece solo. `type` puede ser "error" u "ok". */
function showToast(message, type = "error") {
  const toast = document.createElement("div");
  toast.className = "toast";
  toast.dataset.tipo = type;
  toast.setAttribute("role", type === "error" ? "alert" : "status");
  toast.textContent = message;
  toastsBox.appendChild(toast);
  setTimeout(() => toast.remove(), 5000);
}

/** Crea un elemento con clase y texto opcionales. */
function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

/** Indica si un valor es un precio utilizable (número finito y no negativo). */
function isValidPrice(value) {
  return typeof value === "number" && Number.isFinite(value) && value >= 0;
}

/** Formatea un precio con el símbolo de su moneda, por ejemplo "$ 5.930,00" o "US$ 62,45". */
function formatPrice(value, currency) {
  if (!isValidPrice(value)) return "Sin datos todavía";
  const symbol = CURRENCY_SYMBOLS[currency] ?? currency ?? "";
  const number = value.toLocaleString("es-AR", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  return `${symbol} ${number}`.trim();
}

/** Formatea una fecha como hora local "HH:mm". */
function formatTime(date) {
  return date.toLocaleTimeString("es-AR", { hour: "2-digit", minute: "2-digit", hour12: false });
}

/** Formatea una fecha como "dd/mm/aaaa HH:mm hs" en hora local. */
function formatDateTime(date) {
  const day = date.toLocaleDateString("es-AR", { day: "2-digit", month: "2-digit", year: "numeric" });
  return `${day} ${formatTime(date)} hs`;
}

/** Formatea una fecha como "dd/mm HH:mm" en hora local, para el eje X del gráfico. */
function formatShortDateTime(date) {
  const day = date.toLocaleDateString("es-AR", { day: "2-digit", month: "2-digit" });
  return `${day} ${formatTime(date)}`;
}

/** Devuelve el nombre legible de una categoría ("Acción", "Bono", "CEDEAR"). */
function categoryLabel(assetType) {
  return CATEGORY_LABELS[assetType] ?? assetType;
}

/** Busca en la cartera el activo seleccionado actualmente. */
function selectedAsset() {
  return state.myAssets.find((asset) => asset.ticker === state.selectedTicker) ?? null;
}

// ───────────────────────── Columna izquierda ─────────────────────────

/** Aplica el filtro de categoría y el buscador sobre un listado, sin consultar al backend. */
function applyFilters(assets) {
  const query = state.search.trim().toLowerCase();
  return assets.filter((asset) => {
    const matchesCategory = state.category === "all" || asset.asset_type === state.category;
    const matchesCurrency = state.currency === "todas" || asset.currency === state.currency;
    const matchesSearch =
      !query || asset.ticker.toLowerCase().includes(query) || asset.name.toLowerCase().includes(query);
    return matchesCategory && matchesCurrency && matchesSearch;
  });
}

/** Arma el bloque de texto de un ítem: ticker en negrita, etiqueta de categoría y nombre. */
function buildAssetInfo(asset) {
  const info = el("span", "min-w-0 flex-1");
  const firstLine = el("span", "flex items-center gap-2");
  firstLine.append(el("span", "db-item-ticker", asset.ticker));
  const badge = el("span", "db-badge", categoryLabel(asset.asset_type));
  badge.dataset.tipo = asset.asset_type;
  firstLine.append(badge);
  info.append(firstLine, el("span", "db-item-nombre", asset.name));
  return info;
}

/** Dibuja la pestaña "Mi seguimiento": activos seguidos con su último precio. */
function renderMyAssets() {
  listBox.textContent = "";

  if (state.myAssets.length === 0) {
    listBox.appendChild(buildEmptyPortfolio());
    return;
  }

  const assets = applyFilters(state.myAssets);
  if (assets.length === 0) {
    listBox.appendChild(el("p", "px-1 py-6 text-center text-sm text-slate-400", "Ningún activo coincide con el filtro."));
    return;
  }

  for (const asset of assets) {
    const item = el("button", "db-item");
    item.type = "button";
    item.dataset.ticker = asset.ticker;
    if (asset.ticker === state.selectedTicker) item.setAttribute("aria-current", "true");

    const price = el("span", "db-item-precio", formatPrice(asset.current_price, asset.currency));
    price.dataset.vacio = String(!isValidPrice(asset.current_price));

    item.append(buildAssetInfo(asset), price);
    listBox.appendChild(item);
  }
}

/** Arma la tarjeta que se muestra cuando la cartera está vacía. */
function buildEmptyPortfolio() {
  const card = el("div", "mt-2 rounded-xl border border-dashed border-slate-600 bg-[#1e293b] px-5 py-8 text-center");
  card.insertAdjacentHTML(
    "beforeend",
    '<svg class="mx-auto h-10 w-10 text-slate-500" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 17 9 11l4 4 8-8"/><path d="M15 7h6v6"/></svg>'
  );
  card.append(el("p", "mt-3 text-sm font-medium text-white", "Tu cartera de seguimiento está vacía"));
  card.append(el("p", "mt-1 text-sm text-slate-400", "Sumá activos para ver sus precios y su evolución."));
  const button = el("button", "db-btn-seguir mt-4", "Agregar activos");
  button.type = "button";
  button.dataset.accion = "ir-al-catalogo";
  card.append(button);
  return card;
}

/** Dibuja la pestaña "Agregar / quitar activos": catálogo con botón Seguir / Siguiendo. */
function renderCatalog() {
  listBox.textContent = "";

  if (!state.catalogLoaded) {
    const loading = el("div", "flex items-center justify-center gap-3 py-8 text-sm text-slate-300");
    loading.append(el("span", "spinner"), el("span", null, "Cargando catálogo..."));
    listBox.appendChild(loading);
    return;
  }

  const assets = applyFilters(state.catalog);
  const marketAssets = visibleMarketResults();
  if (assets.length === 0 && marketAssets.length === 0) {
    listBox.appendChild(el("p", "px-1 py-6 text-center text-sm text-slate-400", "Ningún activo coincide con el filtro."));
    return;
  }

  const followedIds = new Set(state.myAssets.map((asset) => asset.id));
  for (const asset of assets) {
    const item = el("div", "db-item");
    const isFollowed = followedIds.has(asset.id);

    const button = el("button", "db-btn-seguir", isFollowed ? "✓ Siguiendo" : "+ Seguir");
    button.type = "button";
    button.dataset.assetId = asset.id;
    button.dataset.siguiendo = String(isFollowed);
    button.setAttribute("aria-label", `${isFollowed ? "Dejar de seguir" : "Seguir"} ${asset.ticker}`);

    item.append(buildAssetInfo(asset), button);
    listBox.appendChild(item);
  }

  // Los activos del mercado van en la misma lista, debajo y con el mismo diseño
  for (const candidate of marketAssets) listBox.appendChild(buildMarketItem(candidate));
}

/**
 * Resultados del mercado que corresponden a la búsqueda actual: sin los que ya están en el
 * catálogo ni repetidos, y con los filtros de categoría y divisa (la divisa solo se conoce en los bonos).
 */
function visibleMarketResults() {
  if (state.marketQuery !== state.search.trim()) return [];

  const catalogTickers = new Set(state.catalog.map((asset) => asset.ticker.toUpperCase()));
  const seen = new Set();
  return state.marketResults.filter((candidate) => {
    const ticker = String(candidate.ticker).toUpperCase();
    if (catalogTickers.has(ticker) || seen.has(ticker)) return false;
    seen.add(ticker);

    const isBond = candidate.asset_type === "bond";
    const matchesCategory = state.category === "all" || (isBond ? state.category === "bond" : state.category !== "bond");
    const matchesCurrency = state.currency === "todas" || !candidate.currency || candidate.currency === state.currency;
    return matchesCategory && matchesCurrency;
  });
}

/** Tipo con que se da de alta un activo del mercado: los bonos son "bond"; en Yahoo lo decide type_hint. */
function marketAssetType(candidate) {
  if (candidate.asset_type === "bond") return "bond";
  return candidate.type_hint === "stock" ? "stock" : "cedear";
}

/** Arma la fila de un activo del mercado: igual a las del catálogo, con su tipo como badge y el botón Seguir. */
function buildMarketItem(candidate) {
  const item = el("div", "db-item");
  const assetType = marketAssetType(candidate);

  const button = el("button", "db-btn-seguir", "+ Seguir");
  button.type = "button";
  button.dataset.marketTicker = candidate.ticker;
  button.dataset.marketType = assetType;
  button.dataset.siguiendo = "false";
  button.setAttribute("aria-label", `Seguir ${candidate.ticker}`);

  item.append(buildAssetInfo({ ...candidate, asset_type: assetType }), button);
  return item;
}

/**
 * Programa la búsqueda en el mercado para el texto actual (con espera entre teclas).
 * Fuera de la pestaña del catálogo, o con el buscador vacío, descarta los resultados.
 */
function scheduleMarketSearch() {
  clearTimeout(state.marketTimer);
  state.marketRequestId++; // invalida la respuesta que esté en camino
  const query = state.search.trim();
  if (state.tab !== "catalog" || !query) {
    state.marketResults = [];
    state.marketQuery = "";
    return;
  }
  state.marketTimer = setTimeout(() => searchMarket(query), MARKET_SEARCH_DELAY_MS);
}

/** Busca en el mercado; si falla (429, 503 o cualquier error) quedan solo los resultados locales, sin avisos. */
async function searchMarket(query) {
  const requestId = ++state.marketRequestId;
  let results = [];
  try {
    results = await api(`/api/assets/search?q=${encodeURIComponent(query)}`);
  } catch {
    results = [];
  }
  if (requestId !== state.marketRequestId) return;

  state.marketResults = Array.isArray(results) ? results : [];
  state.marketQuery = query;
  if (state.tab === "catalog") renderList();
}

/** Sigue un activo del mercado: lo da de alta si hace falta y refresca la cartera y el catálogo. */
async function followMarket(button) {
  const ticker = button.dataset.marketTicker;
  const assetType = button.dataset.marketType;

  button.disabled = true;
  try {
    state.myAssets = await api("/api/assets/custom", {
      method: "POST",
      body: JSON.stringify({ ticker, asset_type: assetType }),
    });
  } catch (error) {
    // Seguir sí avisa: tope de activos (409), proveedor caído, etc. El texto viene del backend.
    showToast(error.message);
    button.disabled = false;
    return;
  }

  state.marketResults = state.marketResults.filter((candidate) => candidate.ticker !== ticker);
  try {
    state.catalog = await api("/api/assets");
  } catch {
    // Si falla, el catálogo se actualiza al volver a abrir la pestaña; la cartera ya quedó al día
  }
  renderList();
  ensureSelection();
}

/** Dibuja la lista que corresponde a la pestaña activa. */
function renderList() {
  if (state.tab === "mine") renderMyAssets();
  else renderCatalog();
}

/** Cambia de pestaña; la primera vez que se abre el catálogo, lo pide al backend. */
async function setTab(tab) {
  state.tab = tab;
  document.querySelectorAll(".db-tab").forEach((button) => {
    button.setAttribute("aria-selected", String(button.dataset.tab === tab));
  });
  renderList();
  // Al abrir el catálogo con texto en el buscador también se busca en el mercado
  scheduleMarketSearch();

  if (tab === "catalog" && !state.catalogLoaded) {
    try {
      state.catalog = await api("/api/assets");
      state.catalogLoaded = true;
    } catch (error) {
      showToast(error.message);
    }
    if (state.tab === "catalog") renderList();
  }
}

/** Cambia el filtro de categoría activo. */
function setCategory(category) {
  state.category = category;
  document.querySelectorAll(".db-chip").forEach((chip) => {
    chip.setAttribute("aria-pressed", String(chip.dataset.categoria === category));
  });
  renderList();
}

/** Sigue o deja de seguir un activo desde el catálogo y actualiza la cartera. */
async function toggleFollow(button) {
  const assetId = Number(button.dataset.assetId);
  const wasFollowed = button.dataset.siguiendo === "true";

  button.disabled = true;
  try {
    if (wasFollowed) {
      await api(`/api/assets/me/${assetId}`, { method: "DELETE" });
      state.myAssets = state.myAssets.filter((asset) => asset.id !== assetId);
    } else {
      // El backend recibe una lista de ids y devuelve la cartera actualizada
      state.myAssets = await api("/api/assets/me", {
        method: "POST",
        body: JSON.stringify({ asset_ids: [assetId] }),
      });
    }
  } catch (error) {
    showToast(error.message);
    button.disabled = false;
    return;
  }

  renderList();
  ensureSelection();
}

/** Pliega o despliega la lista en pantallas angostas para dejar lugar al gráfico. */
function toggleListCollapsed() {
  const button = $("#btn-plegar");
  const collapsed = button.getAttribute("aria-expanded") === "true";
  button.setAttribute("aria-expanded", String(!collapsed));
  listBox.classList.toggle("hidden", collapsed);
  $("#plegar-texto").textContent = collapsed ? "Mostrar lista" : "Ocultar lista";
  $("#plegar-icono").classList.toggle("rotate-180", collapsed);
}

// ───────────────────────── Columna derecha ─────────────────────────

/** Garantiza que haya un activo seleccionado válido: conserva el actual o elige el primero de la cartera. */
function ensureSelection() {
  const stillFollowed = state.myAssets.some((asset) => asset.ticker === state.selectedTicker);
  if (stillFollowed) return;

  if (state.myAssets.length === 0) {
    state.selectedTicker = null;
    state.history = [];
    renderDetail();
    return;
  }

  // Se prefiere el primer activo que pasa la preferencia de divisa; si no hay, el primero de la cartera
  const preferred = state.myAssets.find((asset) => state.currency === "todas" || asset.currency === state.currency);
  selectAsset((preferred ?? state.myAssets[0]).ticker);
}

/** Selecciona un activo de la cartera, lo resalta en la lista y carga su historial. */
function selectAsset(ticker) {
  state.selectedTicker = ticker;
  state.history = [];
  if (state.tab === "mine") renderMyAssets();
  renderDetail({ pending: true });
  loadHistory();
}

/** Muestra uno solo de los estados del gráfico: "loading", "empty", "error" o "chart". */
function setChartState(view, errorMessage) {
  chartLoading.classList.toggle("hidden", view !== "loading");
  chartLoading.classList.toggle("flex", view === "loading");
  chartEmpty.classList.toggle("hidden", view !== "empty");
  chartEmpty.classList.toggle("flex", view === "empty");
  chartError.classList.toggle("hidden", view !== "error");
  chartError.classList.toggle("flex", view === "error");
  // Mientras carga se deja el gráfico anterior de fondo; en vacío o error se oculta el lienzo
  chartCanvasBox.classList.toggle("invisible", view === "empty" || view === "error");
  if (view === "error") $("#grafico-error-texto").textContent = errorMessage;
}

/**
 * Pide las lecturas del activo seleccionado con el rango actual.
 * Con `silent` (refresco automático) no muestra el indicador de carga ni avisos de error.
 */
async function loadHistory({ silent = false } = {}) {
  const ticker = state.selectedTicker;
  if (!ticker) return;

  const requestId = ++state.historyRequestId;
  if (!silent) setChartState("loading");

  let points;
  try {
    points = await api(`/api/assets/${encodeURIComponent(ticker)}/history?limit=${state.limit}`);
  } catch (error) {
    if (requestId !== state.historyRequestId) return;
    if (!silent) setChartState("error", `No se pudieron cargar las lecturas. ${error.message}`);
    return;
  }

  // Si mientras tanto se eligió otro activo u otro rango, esta respuesta ya no sirve
  if (requestId !== state.historyRequestId) return;

  // Se descartan lecturas con precio o fecha inválidos
  state.history = (Array.isArray(points) ? points : [])
    .map((point) => ({ price: point.price, date: new Date(point.timestamp) }))
    .filter((point) => isValidPrice(point.price) && !Number.isNaN(point.date.getTime()));

  renderDetail();
}

/**
 * Calcula la variación porcentual entre el primer y el último punto del rango visible.
 * Devuelve null si no se puede calcular (menos de dos lecturas o primer precio en 0).
 */
function computeVariation(history) {
  if (history.length < 2) return null;
  const first = history[0].price;
  const last = history[history.length - 1].price;
  if (first === 0) return null;
  return ((last - first) / first) * 100;
}

/** Traduce una variación a su signo visual: "sube", "baja" o "neutro". */
function trendOf(variation) {
  if (variation == null || Math.abs(variation) < 0.005) return "neutro";
  return variation > 0 ? "sube" : "baja";
}

/**
 * Dibuja el encabezado, la cotización, la variación y el gráfico del activo seleccionado.
 * Con `pending` (historial todavía en camino) no se muestra el aviso de "sin lecturas".
 */
function renderDetail({ pending = false } = {}) {
  const asset = selectedAsset();
  const hasSelection = Boolean(asset);

  detailBox.classList.toggle("hidden", !hasSelection);
  detailBox.classList.toggle("flex", hasSelection);
  noSelectionBox.classList.toggle("hidden", hasSelection);
  noSelectionBox.classList.toggle("flex", !hasSelection);
  if (!hasSelection) return;

  // Encabezado
  $("#detalle-ticker").textContent = asset.ticker;
  $("#detalle-nombre").textContent = asset.name;
  const categoryBadge = $("#detalle-categoria");
  categoryBadge.textContent = categoryLabel(asset.asset_type);
  categoryBadge.dataset.tipo = asset.asset_type;
  $("#detalle-divisa").textContent = asset.currency;

  const history = state.history;
  const last = history[history.length - 1];

  // Cotización actual: el último registro del historial
  $("#detalle-precio").textContent = last ? formatPrice(last.price, asset.currency) : "—";
  $("#detalle-hora").textContent = last ? `Última lectura: ${formatTime(last.date)} hs` : "";
  $("#detalle-lecturas").textContent =
    history.length === 0 ? "" : history.length === 1 ? "1 lectura" : `${history.length} lecturas`;

  // Variación contra el primer punto del rango visible
  const variation = computeVariation(history);
  const trend = trendOf(variation);
  const variationBadge = $("#detalle-variacion");
  variationBadge.dataset.signo = trend;
  if (variation == null) {
    variationBadge.textContent = "";
  } else {
    const arrow = trend === "sube" ? "↑" : trend === "baja" ? "↓" : "";
    const percent = Math.abs(variation).toLocaleString("es-AR", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    const sign = trend === "sube" ? "+" : trend === "baja" ? "−" : "";
    variationBadge.textContent = `${arrow} ${sign}${percent}%`.trim();
    variationBadge.title = "Variación entre la primera y la última lectura del rango";
  }

  if (history.length === 0) {
    if (!pending) setChartState("empty");
    return;
  }

  setChartState("chart");
  renderChart(asset, history, trend);
}

/** Crea el gráfico la primera vez y después solo actualiza sus datos y colores. */
function renderChart(asset, history, trend) {
  const color = TREND_COLORS[trend];
  const labels = history.map((point) => formatShortDateTime(point.date));
  const prices = history.map((point) => point.price);

  /** Relleno degradado bajo la curva, del color de la tendencia a transparente. */
  const gradientFill = (context) => {
    const { chart } = context;
    if (!chart.chartArea) return "transparent";
    const gradient = chart.ctx.createLinearGradient(0, chart.chartArea.top, 0, chart.chartArea.bottom);
    gradient.addColorStop(0, `${color}55`);
    gradient.addColorStop(1, `${color}00`);
    return gradient;
  };

  if (!state.chart) {
    state.chart = new Chart($("#priceChart"), {
      type: "line",
      data: { labels, datasets: [{ data: prices }] },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: { duration: 250 },
        interaction: { mode: "index", intersect: false },
        plugins: {
          legend: { display: false },
          tooltip: {
            backgroundColor: "#0f172a",
            borderColor: "#334155",
            borderWidth: 1,
            titleColor: "#94a3b8",
            bodyColor: "#ffffff",
            bodyFont: { weight: "600", size: 13 },
            padding: 10,
            displayColors: false,
            callbacks: {},
          },
        },
        scales: {
          x: {
            grid: { display: false },
            border: { color: "#334155" },
            ticks: { color: "#94a3b8", maxTicksLimit: 8, maxRotation: 0, autoSkipPadding: 16 },
          },
          y: {
            grid: { color: "rgba(51, 65, 85, 0.5)" },
            border: { display: false },
            ticks: { color: "#94a3b8", maxTicksLimit: 6 },
          },
        },
      },
    });
  }

  const chart = state.chart;
  const dataset = chart.data.datasets[0];
  chart.data.labels = labels;
  dataset.data = prices;
  dataset.borderColor = color;
  dataset.backgroundColor = gradientFill;
  dataset.fill = true;
  dataset.tension = 0.25;
  dataset.borderWidth = 2;
  // Con pocas lecturas se dibujan los puntos; con muchas, solo la curva
  dataset.pointRadius = history.length <= 2 ? 3 : 0;
  dataset.pointHoverRadius = 4;
  dataset.pointBackgroundColor = color;

  // El tooltip y el eje Y dependen de la moneda del activo y de las fechas del rango actual
  chart.options.plugins.tooltip.callbacks.title = (items) => formatDateTime(history[items[0].dataIndex].date);
  chart.options.plugins.tooltip.callbacks.label = (item) => formatPrice(item.parsed.y, asset.currency);
  chart.options.scales.y.ticks.callback = (value) => formatPrice(Number(value), asset.currency);

  chart.update();
}

/** Cambia el rango de lecturas y vuelve a pedir el historial. */
function setLimit(limit) {
  state.limit = limit;
  document.querySelectorAll(".db-rango").forEach((button) => {
    button.setAttribute("aria-pressed", String(Number(button.dataset.limit) === limit));
  });
  loadHistory();
}

// ───────────────────────── Resumen semanal ─────────────────────────

// Títulos de sección que pide el prompt; se muestran destacados dentro del texto de la IA
const SUMMARY_SECTIONS = ["Panorama general", "Lo más destacado", "Alertas de la semana"];
const SUMMARY_ALERT_LABELS = { percentage: "de variación", price_max: "de techo", price_min: "de piso" };

/** Convierte "2026-10-19" en "19/10" (o "19/10/2026") sin pasar por Date, que correría el día por la zona horaria. */
function formatIsoDay(isoDate, withYear = false) {
  const [year, month, day] = String(isoDate).split("-");
  return withYear ? `${day}/${month}/${year}` : `${day}/${month}`;
}

/** Formatea una variación con signo, por ejemplo "+2,49%" o "−1,96%"; "—" si no hay dato. */
function formatSignedPercent(value) {
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  const trend = trendOf(value);
  const sign = trend === "sube" ? "+" : trend === "baja" ? "−" : "";
  const number = Math.abs(value).toLocaleString("es-AR", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  return `${sign}${number}%`;
}

/** Muestra un mensaje de estado en la tarjeta (con spinner si `loading`) y oculta el contenido del resumen. */
function setSummaryStatus(message, { loading = false, error = false } = {}) {
  summaryStatus.textContent = "";
  summaryStatus.classList.toggle("text-rose-200", error);
  summaryStatus.classList.toggle("text-slate-400", !error);
  if (loading) summaryStatus.append(el("span", "spinner"));
  summaryStatus.append(el("span", null, message));
  summaryStatus.classList.remove("hidden");
  summaryBody.classList.add("hidden");
  $("#resumen-rango").textContent = "";
}

/** Arma la fila de la tabla de un activo; si no tuvo lecturas en la semana, una sola celda "Sin datos". */
function buildSummaryRow(asset) {
  const row = el("tr");
  const tickerCell = el("td");
  tickerCell.title = asset.name;
  tickerCell.append(el("span", "font-semibold text-white", asset.ticker));
  row.append(tickerCell);

  if (!asset.has_data) {
    const empty = el("td", null, "Sin datos en la semana");
    empty.colSpan = 6;
    empty.dataset.vacio = "true";
    row.append(empty);
    return row;
  }

  for (const field of ["first_price", "last_price", "min_price", "max_price"]) {
    row.append(el("td", null, formatPrice(asset[field], asset.currency)));
  }
  const variationCell = el("td");
  const badge = el("span", "db-var", formatSignedPercent(asset.change_pct));
  badge.dataset.signo = trendOf(asset.change_pct);
  variationCell.append(badge);
  row.append(variationCell, el("td", null, Number(asset.readings).toLocaleString("es-AR")));
  return row;
}

/** Línea con el mejor y el peor activo y la cantidad de alertas disparadas por tipo. */
function summaryHighlights(stats) {
  const parts = [];
  const { best, worst } = stats.totals;
  if (best) parts.push(`Mejor: ${best.ticker} ${formatSignedPercent(best.change_pct)}`);
  if (worst) parts.push(`Peor: ${worst.ticker} ${formatSignedPercent(worst.change_pct)}`);

  const { total, by_type: byType } = stats.alerts;
  if (total === 0) {
    parts.push("Sin alertas disparadas");
  } else {
    // Orden fijo: JSONB no conserva el orden de las claves de by_type
    const detail = Object.keys(SUMMARY_ALERT_LABELS)
      .filter((type) => byType[type] > 0)
      .map((type) => `${byType[type]} ${SUMMARY_ALERT_LABELS[type]}`)
      .join(", ");
    parts.push(`${total} ${total === 1 ? "alerta disparada" : "alertas disparadas"} (${detail})`);
  }
  return parts.join(" · ");
}

/** Arma el texto de la IA con textContent: los títulos de sección destacados y el resto en párrafos. */
function buildSummaryText(content) {
  const fragment = document.createDocumentFragment();
  let paragraph = [];
  const flush = () => {
    if (paragraph.length > 0) fragment.append(el("p", "mt-1 whitespace-pre-line", paragraph.join("\n")));
    paragraph = [];
  };

  for (const line of content.split("\n")) {
    const clean = line.trim();
    if (SUMMARY_SECTIONS.includes(clean.replace(/:$/, ""))) {
      flush();
      fragment.append(el("h3", "rs-seccion", clean.replace(/:$/, "")));
    } else if (clean === "") {
      flush();
    } else {
      paragraph.push(clean);
    }
  }
  flush();
  return fragment;
}

/** Dibuja el resumen: rango, tabla por activo, destacados, aviso de error y texto de la IA. */
function renderSummary(data) {
  const { stats } = data;
  const rangeText =
    data.week_start === data.week_end
      ? `Semana del ${formatIsoDay(data.week_start, true)}`
      : `Semana del ${formatIsoDay(data.week_start)} al ${formatIsoDay(data.week_end, true)}`;
  $("#resumen-rango").textContent = rangeText + (stats.week.partial ? " · en curso" : "");

  const rows = $("#resumen-filas");
  rows.textContent = "";
  if (stats.assets.length === 0) {
    const row = el("tr");
    const cell = el("td", null, "Tu cartera no tiene activos.");
    cell.colSpan = 7;
    cell.dataset.vacio = "true";
    row.append(cell);
    rows.append(row);
  }
  for (const asset of stats.assets) rows.append(buildSummaryRow(asset));
  $("#resumen-destacados").textContent = summaryHighlights(stats);

  // Si la IA no está disponible se muestran igual las estadísticas, con un aviso
  const warning = $("#resumen-aviso");
  warning.textContent = data.error ? `${data.error} Se muestran solo las estadísticas.` : "";
  warning.classList.toggle("hidden", !data.error);

  const text = $("#resumen-texto");
  text.textContent = "";
  text.classList.toggle("hidden", !data.content);
  if (data.content) text.append(buildSummaryText(data.content));

  const meta = [`Generado el ${formatDateTime(new Date(data.created_at))}`];
  if (data.model) meta.push(`análisis con ${data.model}`);
  if (data.from_cache) meta.push("guardado");
  $("#resumen-meta").textContent = meta.join(" · ");

  summaryStatus.textContent = "";
  summaryStatus.classList.add("hidden");
  summaryBody.classList.remove("hidden");
}

/** Bloquea el botón y el selector de semana mientras se genera el resumen. */
function setSummaryGenerating(busy) {
  state.summaryGenerating = busy;
  summaryButton.disabled = busy;
  summaryButton.setAttribute("aria-busy", String(busy));
  summaryButton.textContent = "";
  if (busy) summaryButton.append(el("span", "spinner"), el("span", null, "Generando..."));
  else summaryButton.textContent = "Generar resumen";
  document.querySelectorAll(".rs-semana").forEach((button) => {
    button.disabled = busy;
  });
}

/** Pide el resumen guardado de la semana elegida; si todavía no hay, invita a generarlo. */
async function loadSummary() {
  const offset = state.summaryOffset;
  const requestId = ++state.summaryRequestId;
  setSummaryStatus("Buscando el resumen guardado...", { loading: true });

  try {
    const data = await api(`/api/summary/weekly?week_offset=${offset}`);
    if (requestId === state.summaryRequestId) renderSummary(data);
  } catch (error) {
    if (requestId !== state.summaryRequestId) return;
    if (error.status === 404) {
      const week = offset === 0 ? "esta semana" : "la semana pasada";
      setSummaryStatus(`Todavía no generaste el resumen de ${week}.`);
    } else {
      setSummaryStatus(`No se pudo cargar el resumen. ${error.message}`, { error: true });
    }
  }
}

/** Genera el resumen de la semana elegida (puede tardar varios segundos) y lo muestra. */
async function generateSummary() {
  if (state.summaryGenerating) return;
  const offset = state.summaryOffset;
  const requestId = ++state.summaryRequestId;
  setSummaryGenerating(true);
  setSummaryStatus("Generando el resumen, puede tardar unos segundos...", { loading: true });

  try {
    const data = await api(`/api/summary/weekly?week_offset=${offset}`, { method: "POST" });
    if (requestId === state.summaryRequestId) renderSummary(data);
  } catch (error) {
    if (requestId === state.summaryRequestId) {
      setSummaryStatus(`No se pudo generar el resumen. ${error.message}`, { error: true });
    }
  } finally {
    setSummaryGenerating(false);
  }
}

/** Cambia la semana del resumen y busca el guardado de esa semana. */
function setSummaryOffset(offset) {
  if (offset === state.summaryOffset || state.summaryGenerating) return;
  state.summaryOffset = offset;
  document.querySelectorAll(".rs-semana").forEach((button) => {
    button.setAttribute("aria-pressed", String(Number(button.dataset.offset) === offset));
  });
  loadSummary();
}

// ───────────────────────── Actualización automática ─────────────────────────

/** Lee una preferencia de localStorage; si el navegador lo bloquea, devuelve el valor por defecto. */
function readPref(key, fallback) {
  try {
    return localStorage.getItem(key) ?? fallback;
  } catch {
    return fallback;
  }
}

/** Aplica la preferencia de divisa a la lista y muestra el aviso con la opción de ver todas. */
function setCurrency(currency) {
  state.currency = ["ARS", "USD"].includes(currency) ? currency : "todas";
  const filtering = state.currency !== "todas";
  $("#aviso-divisa").classList.toggle("hidden", !filtering);
  if (filtering) $("#aviso-divisa-texto").textContent = `Mostrando solo activos en ${state.currency}.`;
  renderList();
}

/** Vuelve a pedir la cartera y el historial del activo seleccionado, sin indicadores de carga. */
async function refresh() {
  // Con la pestaña del navegador oculta no se consulta; se retoma al volver
  if (document.hidden) return;

  try {
    state.myAssets = await api("/api/assets/me");
  } catch {
    return; // Un fallo puntual del refresco automático no interrumpe la pantalla
  }

  renderList();
  ensureSelection();
  if (state.selectedTicker) await loadHistory({ silent: true });
}

/** Arranca el refresco automático cada 60 segundos. */
function startAutoRefresh() {
  stopAutoRefresh();
  state.refreshTimer = setInterval(refresh, REFRESH_INTERVAL_MS);
}

/** Detiene el refresco automático (al salir de la pantalla o al vencer la sesión). */
function stopAutoRefresh() {
  if (state.refreshTimer) clearInterval(state.refreshTimer);
  state.refreshTimer = null;
}

// ───────────────────────── Inicio ─────────────────────────

/** Cierra la sesión y vuelve al login. */
async function logout() {
  stopAutoRefresh();
  try {
    await api("/api/auth/logout", { method: "POST" });
  } catch {
    // Aunque falle el pedido, se vuelve al login.
  }
  window.location.href = LOGIN_URL;
}

/** Conecta los controles de la página con sus funciones. */
function bindEvents() {
  document.querySelectorAll(".db-tab").forEach((button) => {
    button.addEventListener("click", () => setTab(button.dataset.tab));
  });

  $("#filtros").addEventListener("click", (event) => {
    const chip = event.target.closest(".db-chip");
    if (chip) setCategory(chip.dataset.categoria);
  });

  // El catálogo local se filtra al instante; en paralelo se busca en el mercado
  searchInput.addEventListener("input", () => {
    state.search = searchInput.value;
    renderList();
    scheduleMarketSearch();
  });

  // Un solo listener para toda la lista: seleccionar, seguir/dejar de seguir o ir al catálogo
  listBox.addEventListener("click", (event) => {
    const followButton = event.target.closest(".db-btn-seguir[data-asset-id]");
    if (followButton) return toggleFollow(followButton);

    const marketButton = event.target.closest(".db-btn-seguir[data-market-ticker]");
    if (marketButton) return followMarket(marketButton);

    if (event.target.closest('[data-accion="ir-al-catalogo"]')) return setTab("catalog");

    const item = event.target.closest("button.db-item[data-ticker]");
    if (item && item.dataset.ticker !== state.selectedTicker) selectAsset(item.dataset.ticker);
  });

  $("#rangos").addEventListener("click", (event) => {
    const button = event.target.closest(".db-rango");
    if (button) setLimit(Number(button.dataset.limit));
  });

  $("#resumen-semanas").addEventListener("click", (event) => {
    const button = event.target.closest(".rs-semana");
    if (button) setSummaryOffset(Number(button.dataset.offset));
  });
  summaryButton.addEventListener("click", generateSummary);

  $("#btn-ver-todas").addEventListener("click", () => setCurrency("todas"));
  $("#btn-reintentar").addEventListener("click", () => loadHistory());
  $("#btn-plegar").addEventListener("click", toggleListCollapsed);
  $("#btn-salir").addEventListener("click", logout);

  // Al salir de la página se corta el temporizador; al volver a la pestaña se actualiza enseguida
  window.addEventListener("pagehide", stopAutoRefresh);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) refresh();
  });
}

/** Punto de entrada: verifica la sesión, carga la cartera, selecciona el primer activo y arranca el refresco. */
async function init() {
  bindEvents();

  try {
    const user = await api("/api/auth/me");
    if (!user.onboarding_completed) {
      window.location.href = ONBOARDING_URL;
      return;
    }
    $("#usuario-email").textContent = user.email;

    state.myAssets = await api("/api/assets/me");
  } catch (error) {
    listBox.textContent = "";
    listBox.appendChild(el("p", "px-1 py-6 text-center text-sm text-rose-200", error.message));
    return;
  }

  setCurrency(readPref(PREF_CURRENCY_KEY, "todas"));
  ensureSelection();
  startAutoRefresh();
  loadSummary();
}

init();
