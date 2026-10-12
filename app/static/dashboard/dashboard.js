/**
 * dashboard.js — Pantalla principal de MonitorPrecios.
 *
 * Columna izquierda: cartera del usuario y catálogo (pestañas, filtros, buscador).
 * Columna derecha:   cotización del activo seleccionado y su gráfico (Chart.js).
 *
 * Endpoints que consume (todos con la cookie HttpOnly de sesión):
 *   GET    /api/auth/me                         usuario actual
 *   GET    /api/assets/me                       cartera
 *   GET    /api/assets                          catálogo
 *   POST   /api/assets/me   { asset_ids: [id] } seguir un activo
 *   DELETE /api/assets/me/{asset_id}            dejar de seguir
 *   GET    /api/assets/{ticker}/history?limit=  lecturas de precio
 *
 * Cada 60 segundos se vuelven a pedir la cartera y el historial del activo
 * seleccionado. Ante un 401 se redirige al login.
 */

// ───────────────────────── Configuración ─────────────────────────

const LOGIN_URL = "/";
const ONBOARDING_URL = "/onboarding";
const REFRESH_INTERVAL_MS = 60000;
const DEFAULT_LIMIT = 60;

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
  selectedTicker: null,
  limit: DEFAULT_LIMIT,
  history: [], // lecturas válidas del activo seleccionado
  historyRequestId: 0, // para descartar respuestas viejas al cambiar rápido de activo
  chart: null,
  refreshTimer: null,
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
    throw new Error(typeof detail === "string" ? detail : `El servidor respondió con un error (${res.status}).`);
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
  if (assets.length === 0) {
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
  const labels = history.map((point) => formatTime(point.date));
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

  searchInput.addEventListener("input", () => {
    state.search = searchInput.value;
    renderList();
  });

  // Un solo listener para toda la lista: seleccionar, seguir/dejar de seguir o ir al catálogo
  listBox.addEventListener("click", (event) => {
    const followButton = event.target.closest(".db-btn-seguir[data-asset-id]");
    if (followButton) return toggleFollow(followButton);

    if (event.target.closest('[data-accion="ir-al-catalogo"]')) return setTab("catalog");

    const item = event.target.closest("button.db-item[data-ticker]");
    if (item && item.dataset.ticker !== state.selectedTicker) selectAsset(item.dataset.ticker);
  });

  $("#rangos").addEventListener("click", (event) => {
    const button = event.target.closest(".db-rango");
    if (button) setLimit(Number(button.dataset.limit));
  });

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
}

init();
