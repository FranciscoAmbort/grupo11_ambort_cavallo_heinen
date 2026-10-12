/**
 * onboarding.js — Asistente de configuración inicial de MonitorPrecios.
 *
 * Tres pasos, sin recargar la página:
 *   1. Telegram        → PATCH /api/auth/telegram   (opcional)
 *   2. Activos         → se guardan al finalizar con POST /api/assets/me
 *   3. Primera alerta  → POST /api/alerts           (opcional)
 *
 * Al finalizar se envía POST /api/assets/me, que guarda la cartera y además
 * marca el onboarding como completo en el backend (aunque la lista vaya vacía).
 * Después se redirige a /dashboard.
 *
 * La sesión viaja en la cookie HttpOnly; si el backend responde 401 se vuelve al login.
 */

// ───────────────────────── Configuración ─────────────────────────

// Usuario del bot de Telegram del sistema (sin @). El backend no lo expone.
const TELEGRAM_BOT_USERNAME = "MonitorPreciosArgentinos_bot";

// Tickers sugeridos por el botón "Seleccionar recomendados" (se usan los que existan en el catálogo).
const RECOMMENDED_TICKERS = ["GGAL.BA", "YPFD.BA", "AAPL.BA", "SPY.BA", "AL30", "GD30"];

// Símbolo que acompaña a cada divisa
const CURRENCY_LABELS = { ARS: "ARS $", USD: "USD US$" };

// Tope del valor de una alerta: la columna del backend es Numeric(12, 4)
const MAX_ALERT_VALUE = 100000000;

// ───────────────────────── Estado local ─────────────────────────

const state = {
  currentStep: 1,
  user: null,
  telegramChatId: "", // último Chat ID guardado en el backend
  assets: [], // catálogo completo
  selectedIds: new Set(), // ids de los activos tildados
  filter: "todos", // "todos" | "ARS" | "USD"
  search: "",
};

// ───────────────────────── Elementos de la página ─────────────────────────

const $ = (selector) => document.querySelector(selector);

const loadingBox = $("#cargando");
const toastsBox = $("#toasts");
const telegramInput = $("#telegram-chat-id");
const telegramStatus = $("#telegram-estado");
const assetsGrid = $("#grilla-activos");
const noResults = $("#sin-resultados");
const counter = $("#contador");
const alertForm = $("#form-alerta");
const alertAsset = $("#alerta-activo");
const alertValue = $("#alerta-valor");
const alertUnit = $("#alerta-unidad");
const alertValueLabel = $("#alerta-valor-etiqueta");
const alertPrice = $("#alerta-precio");
const alertError = $("#alerta-error");
const alertNoAssets = $("#alerta-sin-activos");

// ───────────────────────── Utilidades ─────────────────────────

/** Hace una petición al backend con la cookie de sesión y devuelve el JSON; lanza un Error con un mensaje claro si falla. */
async function api(path, options = {}) {
  let res;
  try {
    res = await fetch(path, {
      credentials: "include",
      headers: options.body ? { "Content-Type": "application/json" } : undefined,
      ...options,
    });
  } catch {
    throw new Error("No pudimos conectar con el servidor. Revisá tu conexión y probá de nuevo.");
  }

  if (res.status === 401) {
    window.location.href = "/";
    throw new Error("Tu sesión venció. Iniciá sesión de nuevo.");
  }

  if (!res.ok) {
    throw new Error(await readErrorMessage(res));
  }

  return res.status === 204 ? null : res.json();
}

/** Extrae el mensaje de error de una respuesta del backend (FastAPI lo envía en `detail`). */
async function readErrorMessage(res) {
  try {
    const body = await res.json();
    if (typeof body.detail === "string") return body.detail;
    if (Array.isArray(body.detail) && body.detail[0]?.msg) {
      return body.detail[0].msg.replace(/^Value error, /, "");
    }
  } catch {
    // La respuesta no era JSON: se usa el mensaje genérico.
  }
  return `El servidor respondió con un error (${res.status}). Probá de nuevo.`;
}

/** Muestra un aviso flotante que desaparece solo. `type` puede ser "error" u "ok". */
function showToast(message, type = "error") {
  const toast = document.createElement("div");
  toast.className = "toast";
  toast.dataset.tipo = type;
  toast.setAttribute("role", type === "error" ? "alert" : "status");
  toast.textContent = message;
  toastsBox.appendChild(toast);
  setTimeout(() => toast.remove(), 6000);
}

/** Pone un botón en estado "guardando" (deshabilitado y con spinner) o lo restaura. */
function setBusy(button, isBusy) {
  if (isBusy) {
    button.dataset.textoOriginal = button.textContent;
    button.disabled = true;
    button.innerHTML = '<span class="spinner" aria-hidden="true"></span><span>Guardando...</span>';
  } else {
    button.disabled = false;
    button.textContent = button.dataset.textoOriginal ?? button.textContent;
  }
}

/** Devuelve la etiqueta de divisa de un activo, por ejemplo "ARS $" o "USD US$". */
function currencyLabel(asset) {
  return CURRENCY_LABELS[asset.currency] ?? asset.currency;
}

/** Compara tickers ignorando mayúsculas y el sufijo ".BA". */
function normalizeTicker(ticker) {
  return ticker.toUpperCase().replace(/\.BA$/, "");
}

// ───────────────────────── Navegación entre pasos ─────────────────────────

/** Muestra el paso indicado, actualiza el indicador de progreso y prepara su contenido. */
function goToStep(step) {
  state.currentStep = step;

  document.querySelectorAll("[data-step]").forEach((panel) => {
    const isCurrent = Number(panel.dataset.step) === step;
    panel.classList.toggle("hidden", !isCurrent);
    panel.classList.toggle("entrando", isCurrent);
  });

  renderStepper();
  if (step === 3) prepareAlertStep();

  // El foco va al título del paso, para quien usa teclado o lector de pantalla
  const title = $(`#titulo-paso-${step}`);
  title.setAttribute("tabindex", "-1");
  title.focus({ preventScroll: true });
  window.scrollTo({ top: 0 });
}

/** Pinta cada paso del indicador como hecho, actual o pendiente. */
function renderStepper() {
  document.querySelectorAll("#stepper .paso").forEach((item) => {
    const number = Number(item.dataset.paso);
    const status = number < state.currentStep ? "hecho" : number === state.currentStep ? "actual" : "pendiente";
    item.dataset.estado = status;
    item.querySelector(".paso-num").textContent = status === "hecho" ? "✓" : String(number);
    if (status === "actual") item.setAttribute("aria-current", "step");
    else item.removeAttribute("aria-current");
  });

  document.querySelectorAll("#stepper .paso-linea").forEach((line, index) => {
    line.dataset.estado = index + 1 < state.currentStep ? "hecho" : "pendiente";
  });
}

// ───────────────────────── Paso 1: Telegram ─────────────────────────

/** Muestra debajo del campo si el Chat ID quedó guardado o qué problema hubo. */
function setTelegramStatus(message, type) {
  telegramStatus.classList.toggle("hidden", !message);
  telegramStatus.classList.toggle("flex", Boolean(message));
  telegramStatus.textContent = "";
  if (!message) return;

  if (type === "ok") {
    telegramStatus.className = "mt-2 flex items-center gap-2 text-sm text-emerald-300";
    telegramStatus.innerHTML =
      '<svg class="h-4 w-4" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true"><path fill-rule="evenodd" d="M16.7 5.3a1 1 0 0 1 0 1.4l-7.5 7.500a1 1 0 0 1-1.4 0L3.300 9.700a1 1 0 1 1 1.400-1.400l3.800 3.800 6.800-6.800a1 1 0 0 1 1.400 0Z" clip-rule="evenodd"/></svg>';
    telegramStatus.append(message);
  } else {
    telegramStatus.className = "mt-2 flex items-center gap-2 text-sm text-rose-300";
    telegramStatus.textContent = message;
  }
}

/** Valida y guarda el Chat ID en el backend. Devuelve true si quedó guardado. */
async function saveTelegram(button) {
  const chatId = telegramInput.value.trim();

  if (!/^-?\d{5,20}$/.test(chatId)) {
    setTelegramStatus("El Chat ID es un número, por ejemplo 123456789.", "error");
    telegramInput.focus();
    return false;
  }

  setBusy(button, true);
  try {
    const user = await api("/api/auth/telegram", {
      method: "PATCH",
      body: JSON.stringify({ telegram_chat_id: chatId }),
    });
    state.telegramChatId = user.telegram_chat_id ?? "";
    setTelegramStatus("Chat ID guardado. Los avisos te van a llegar por Telegram.", "ok");
    return true;
  } catch (error) {
    setTelegramStatus("", null);
    showToast(error.message);
    return false;
  } finally {
    setBusy(button, false);
  }
}

/** Avanza al paso 2; si hay un Chat ID escrito y sin guardar, lo guarda antes. */
async function continueFromTelegram(button) {
  const typed = telegramInput.value.trim();
  if (typed && typed !== state.telegramChatId) {
    if (!(await saveTelegram(button))) return;
  }
  goToStep(2);
}

// ───────────────────────── Paso 2: Activos ─────────────────────────

/** Devuelve los activos del catálogo que pasan el filtro de divisa y la búsqueda. */
function visibleAssets() {
  const query = state.search.trim().toLowerCase();
  return state.assets.filter((asset) => {
    const matchesFilter = state.filter === "todos" || asset.currency === state.filter;
    const matchesSearch =
      !query || asset.ticker.toLowerCase().includes(query) || asset.name.toLowerCase().includes(query);
    return matchesFilter && matchesSearch;
  });
}

/** Dibuja la grilla de tarjetas seleccionables con los activos visibles. */
function renderAssets() {
  const assets = visibleAssets();
  assetsGrid.textContent = "";
  noResults.classList.toggle("hidden", assets.length > 0);

  for (const asset of assets) {
    const card = document.createElement("button");
    card.type = "button";
    card.className = "activo";
    card.dataset.id = asset.id;
    card.setAttribute("aria-pressed", String(state.selectedIds.has(asset.id)));

    const info = document.createElement("span");
    info.className = "min-w-0";
    const ticker = document.createElement("span");
    ticker.className = "activo-ticker block";
    ticker.textContent = asset.ticker;
    const name = document.createElement("span");
    name.className = "activo-nombre block";
    name.textContent = asset.name;
    info.append(ticker, name);

    const side = document.createElement("span");
    side.className = "flex flex-col";
    const currency = document.createElement("span");
    currency.className = "activo-divisa";
    currency.dataset.divisa = asset.currency;
    currency.textContent = currencyLabel(asset);
    side.append(currency);
    side.insertAdjacentHTML(
      "beforeend",
      '<svg class="activo-check" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true"><path fill-rule="evenodd" d="M10 18a8 8 0 1 0 0-16 8 8 0 0 0 0 16Zm3.700-9.300a1 1 0 0 0-1.400-1.400L9 10.600 7.700 9.300a1 1 0 0 0-1.400 1.400l2 2a1 1 0 0 0 1.400 0l4-4Z" clip-rule="evenodd"/></svg>'
    );

    card.append(info, side);
    assetsGrid.appendChild(card);
  }
}

/** Tilda o destilda un activo y actualiza su tarjeta y el contador. */
function toggleAsset(assetId) {
  if (state.selectedIds.has(assetId)) state.selectedIds.delete(assetId);
  else state.selectedIds.add(assetId);

  const card = assetsGrid.querySelector(`[data-id="${assetId}"]`);
  if (card) card.setAttribute("aria-pressed", String(state.selectedIds.has(assetId)));
  updateCounter();
}

/** Tilda los activos recomendados que existan en el catálogo. */
function selectRecommended() {
  const recommended = new Set(RECOMMENDED_TICKERS.map(normalizeTicker));
  const matches = state.assets.filter((asset) => recommended.has(normalizeTicker(asset.ticker)));

  if (matches.length === 0) {
    showToast("Ninguno de los activos recomendados está disponible en el catálogo.");
    return;
  }

  matches.forEach((asset) => state.selectedIds.add(asset.id));
  renderAssets();
  updateCounter();
}

/** Actualiza el texto "N activos seleccionados". */
function updateCounter() {
  const count = state.selectedIds.size;
  counter.textContent =
    count === 0 ? "Ningún activo seleccionado" : count === 1 ? "1 activo seleccionado" : `${count} activos seleccionados`;
}

/** Cambia el filtro de divisa activo y vuelve a dibujar la grilla. */
function setFilter(filter) {
  state.filter = filter;
  document.querySelectorAll("#filtros .pildora").forEach((pill) => {
    pill.setAttribute("aria-pressed", String(pill.dataset.filtro === filter));
  });
  renderAssets();
}

// ───────────────────────── Paso 3: Primera alerta ─────────────────────────

/** Devuelve los activos tildados en el paso 2, en el orden del catálogo. */
function selectedAssets() {
  return state.assets.filter((asset) => state.selectedIds.has(asset.id));
}

/** Arma el desplegable con los activos elegidos; si no hay ninguno, oculta el formulario. */
function prepareAlertStep() {
  const assets = selectedAssets();
  const hasAssets = assets.length > 0;

  alertForm.classList.toggle("hidden", !hasAssets);
  alertNoAssets.classList.toggle("hidden", hasAssets);
  $("#btn-omitir").classList.toggle("hidden", !hasAssets);
  alertError.classList.add("hidden");
  if (!hasAssets) return;

  const previous = alertAsset.value;
  alertAsset.textContent = "";
  for (const asset of assets) {
    const option = document.createElement("option");
    option.value = asset.id;
    option.textContent = `${asset.ticker} — ${asset.name}`;
    alertAsset.appendChild(option);
  }
  // Se conserva la elección anterior si el activo sigue tildado
  if (assets.some((asset) => String(asset.id) === previous)) alertAsset.value = previous;

  updateAlertUnit();
}

/** Devuelve el tipo de condición elegido: "ABOVE", "BELOW" o "PCT_CHANGE". */
function selectedAlertType() {
  return alertForm.querySelector('input[name="alerta-tipo"]:checked').value;
}

/** Ajusta la etiqueta, la unidad y el precio de referencia según el activo y la condición elegidos. */
function updateAlertUnit() {
  const asset = state.assets.find((item) => String(item.id) === alertAsset.value);
  if (!asset) return;

  const isPercent = selectedAlertType() === "PCT_CHANGE";
  alertValueLabel.textContent = isPercent ? "Porcentaje de variación" : "Precio objetivo";
  alertUnit.textContent = isPercent ? "%" : currencyLabel(asset);

  if (asset.current_price == null) {
    alertPrice.textContent = "Todavía no hay un precio registrado para este activo.";
  } else {
    const price = asset.current_price.toLocaleString("es-AR", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    alertPrice.textContent = `Último precio registrado: ${price} ${asset.currency}`;
  }
}

/** Lee el formulario y devuelve el cuerpo para POST /api/alerts, o null si el valor no es válido. */
function readAlertPayload() {
  const value = Number(alertValue.value);

  if (!alertValue.value.trim() || !Number.isFinite(value) || value <= 0) {
    alertError.textContent = "Ingresá un valor mayor a 0, o tocá “Omitir y configurar después”.";
    alertError.classList.remove("hidden");
    alertValue.focus();
    return null;
  }
  // La base guarda hasta 8 cifras enteras; un valor mayor haría fallar el guardado
  if (value >= MAX_ALERT_VALUE) {
    alertError.textContent = "El valor es demasiado grande. Ingresá un número menor a 100.000.000.";
    alertError.classList.remove("hidden");
    alertValue.focus();
    return null;
  }
  alertError.classList.add("hidden");

  const type = selectedAlertType();
  const payload = { asset_id: Number(alertAsset.value), alert_type: type };
  if (type === "PCT_CHANGE") payload.threshold_percent = value;
  else payload.target_price = value;
  return payload;
}

// ───────────────────────── Finalización ─────────────────────────

/**
 * Guarda la cartera (lo que marca el onboarding como completo), crea la alerta
 * si corresponde y redirige al dashboard.
 */
async function finish(button, withAlert) {
  const hasAssets = state.selectedIds.size > 0;
  const alertPayload = withAlert && hasAssets ? readAlertPayload() : null;
  if (withAlert && hasAssets && !alertPayload) return;

  setBusy(button, true);
  try {
    await api("/api/assets/me", {
      method: "POST",
      body: JSON.stringify({ asset_ids: [...state.selectedIds] }),
    });

    if (alertPayload) {
      try {
        await api("/api/alerts", { method: "POST", body: JSON.stringify(alertPayload) });
      } catch (error) {
        // La cartera ya quedó guardada: se avisa y se deja reintentar u omitir la alerta.
        showToast(`Tu cartera se guardó, pero la alerta no se pudo crear: ${error.message}`);
        setBusy(button, false);
        return;
      }
    }

    window.location.href = "/dashboard";
  } catch (error) {
    showToast(error.message);
    setBusy(button, false);
  }
}

// ───────────────────────── Inicio ─────────────────────────

/** Conecta los botones y campos de la página con sus funciones. */
function bindEvents() {
  $("#btn-guardar-telegram").addEventListener("click", (event) => saveTelegram(event.currentTarget));
  $("#btn-continuar-1").addEventListener("click", (event) => continueFromTelegram(event.currentTarget));
  telegramInput.addEventListener("input", () => setTelegramStatus("", null));

  $("#buscador").addEventListener("input", (event) => {
    state.search = event.target.value;
    renderAssets();
  });
  $("#filtros").addEventListener("click", (event) => {
    const pill = event.target.closest(".pildora");
    if (pill) setFilter(pill.dataset.filtro);
  });
  assetsGrid.addEventListener("click", (event) => {
    const card = event.target.closest(".activo");
    if (card) toggleAsset(Number(card.dataset.id));
  });
  $("#btn-recomendados").addEventListener("click", selectRecommended);
  $("#btn-continuar-2").addEventListener("click", () => goToStep(3));

  alertAsset.addEventListener("change", updateAlertUnit);
  alertForm.addEventListener("change", (event) => {
    if (event.target.name === "alerta-tipo") updateAlertUnit();
  });
  alertForm.addEventListener("submit", (event) => {
    event.preventDefault();
    finish($("#btn-finalizar"), true);
  });
  $("#btn-finalizar").addEventListener("click", (event) => finish(event.currentTarget, true));
  $("#btn-omitir").addEventListener("click", (event) => finish(event.currentTarget, false));

  document.querySelectorAll("[data-ir-a]").forEach((button) => {
    button.addEventListener("click", () => goToStep(Number(button.dataset.irA)));
  });
}

/** Punto de entrada: verifica la sesión, carga el catálogo y muestra el primer paso. */
async function init() {
  $("#link-bot").href = `https://t.me/${TELEGRAM_BOT_USERNAME}?start=onboarding`;
  bindEvents();

  try {
    state.user = await api("/api/auth/me");

    // Quien ya completó la configuración no vuelve a pasar por acá
    if (state.user.onboarding_completed) {
      window.location.href = "/dashboard";
      return;
    }

    state.assets = await api("/api/assets");
  } catch (error) {
    loadingBox.textContent = error.message;
    return;
  }

  state.telegramChatId = state.user.telegram_chat_id ?? "";
  telegramInput.value = state.telegramChatId;
  if (state.telegramChatId) setTelegramStatus("Chat ID guardado. Los avisos te van a llegar por Telegram.", "ok");

  renderAssets();
  updateCounter();
  loadingBox.classList.add("hidden");
  goToStep(1);
}

init();
