/**
 * alerts.js — Pantalla "Mis Alertas" de MonitorPrecios.
 *
 * Lista, crea, edita, pausa/reactiva y elimina las reglas de alerta del usuario,
 * y muestra una vista previa del aviso que envía el worker.
 *
 * Endpoints que consume (todos con la cookie HttpOnly de sesión):
 *   GET    /api/auth/me            usuario actual
 *   GET    /api/assets             catálogo (nombre, divisa y último precio de cada activo)
 *   GET    /api/assets/me          activos seguidos (opciones del formulario)
 *   GET    /api/alerts             alertas del usuario
 *   POST   /api/alerts             crear
 *   PATCH  /api/alerts/{id}        editar la condición o pausar/reactivar
 *   DELETE /api/alerts/{id}        eliminar
 *
 * Ante un 401 se redirige al login.
 */

// ───────────────────────── Configuración ─────────────────────────

const LOGIN_URL = "/";
const ONBOARDING_URL = "/onboarding";

// Tope del valor de una alerta: la columna del backend es Numeric(12, 4)
const MAX_ALERT_VALUE = 100000000;

const CURRENCY_SYMBOLS = { ARS: "$", USD: "US$" };

// ───────────────────────── Estado ─────────────────────────

const state = {
  user: null,
  alerts: [],
  assetsById: new Map(), // catálogo completo, indexado por id
  myAssets: [], // activos seguidos
  filter: "todas", // "todas" | "activa" | "pausada" | "disparada"
  search: "",
  editingId: null, // id de la alerta en edición, o null si se está creando
  deletingId: null,
};

// ───────────────────────── Elementos de la página ─────────────────────────

const $ = (selector) => document.querySelector(selector);

const listBox = $("#lista");
const summary = $("#resumen");
const toastsBox = $("#toasts");
const formModal = $("#modal-form");
const form = $("#form-alerta");
const assetSelect = $("#campo-activo");
const valueInput = $("#campo-valor");
const valueError = $("#error-valor");
const deleteModal = $("#modal-eliminar");
const previewModal = $("#modal-preview");

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
    window.location.href = LOGIN_URL;
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
    // La respuesta no era JSON.
  }
  return `El servidor respondió con un error (${res.status}).`;
}

/** Muestra un aviso flotante que desaparece solo. `type` puede ser "ok" o "error". */
function showToast(message, type = "ok") {
  const toast = document.createElement("div");
  toast.className = "toast";
  toast.dataset.tipo = type;
  toast.setAttribute("role", type === "error" ? "alert" : "status");
  toast.textContent = message;
  toastsBox.appendChild(toast);
  setTimeout(() => toast.remove(), 4500);
}

/** Crea un elemento con clase y texto opcionales. */
function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

/** Pone un botón en estado "guardando" (deshabilitado y con spinner) o lo restaura. */
function setBusy(button, isBusy, busyText = "Guardando...") {
  if (isBusy) {
    button.dataset.textoOriginal = button.textContent;
    button.disabled = true;
    button.textContent = "";
    button.append(el("span", "spinner"), el("span", null, busyText));
  } else {
    button.disabled = false;
    button.textContent = button.dataset.textoOriginal ?? button.textContent;
  }
}

/** Devuelve el símbolo de una divisa: "$" para ARS, "US$" para USD. */
function currencySymbol(currency) {
  return CURRENCY_SYMBOLS[currency] ?? currency ?? "";
}

/** Formatea un número al estilo argentino, con 2 decimales (o hasta 4 si los tiene). */
function formatNumber(value) {
  return value.toLocaleString("es-AR", { minimumFractionDigits: 2, maximumFractionDigits: 4 });
}

/** Formatea un precio con el símbolo de su moneda, por ejemplo "$ 1.250,50" o "US$ 62,45". */
function formatPrice(value, currency) {
  return `${currencySymbol(currency)} ${formatNumber(value)}`.trim();
}

/** Formatea una fecha ISO como "dd/mm/aaaa HH:mm hs" en hora local. */
function formatDateTime(iso) {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "";
  const day = date.toLocaleDateString("es-AR", { day: "2-digit", month: "2-digit", year: "numeric" });
  const time = date.toLocaleTimeString("es-AR", { hour: "2-digit", minute: "2-digit", hour12: false });
  return `${day} ${time} hs`;
}

/** Devuelve los datos del activo de una alerta (nombre, divisa, precio); usa valores neutros si no está en el catálogo. */
function assetOf(alert) {
  return (
    state.assetsById.get(alert.asset_id) ?? {
      id: alert.asset_id,
      ticker: alert.ticker,
      name: "",
      currency: "ARS",
      current_price: null,
    }
  );
}

/** Devuelve el valor numérico de la regla: el precio objetivo o el porcentaje. */
function alertValue(alert) {
  return alert.alert_type === "PCT_CHANGE" ? alert.threshold_percent : alert.target_price;
}

/**
 * Clasifica una alerta en "activa", "pausada" o "disparada".
 * Las alertas de precio se desactivan solas al dispararse; por eso una alerta de
 * precio inactiva que tiene fecha de disparo se considera disparada, no pausada.
 */
function statusOf(alert) {
  if (alert.is_active) return "activa";
  const isPriceRule = alert.alert_type !== "PCT_CHANGE";
  return isPriceRule && alert.last_triggered_at ? "disparada" : "pausada";
}

/** Arma el texto de la regla, por ejemplo "Precio > $ 1.500,00" o "Variación > 5%". */
function ruleText(alert) {
  const asset = assetOf(alert);
  const value = alertValue(alert);
  if (alert.alert_type === "PCT_CHANGE") return `Variación > ${formatNumber(value).replace(/,00$/, "")}%`;
  return `Precio ${alert.alert_type === "ABOVE" ? ">" : "<"} ${formatPrice(value, asset.currency)}`;
}

// ───────────────────────── Lista de alertas ─────────────────────────

const ICONS = {
  campana:
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9"/><path d="M10.300 21a1.940 1.940 0 0 0 3.400 0"/></svg>',
  pausa:
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M9 5v14M15 5v14"/></svg>',
  check:
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="m8.500 12.500 2.500 2.500 4.500-5"/></svg>',
  mensaje:
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M6 12 3.269 3.125A59.769 59.769 0 0 1 21.485 12 59.768 59.768 0 0 1 3.270 20.875L5.999 12Zm0 0h7.500"/></svg>',
  editar:
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 20h9"/><path d="M16.500 3.500a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.500 3.500Z"/></svg>',
  eliminar:
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 6h18"/><path d="M8 6V4h8v2"/><path d="M19 6l-1 14H6L5 6"/><path d="M10 11v6M14 11v6"/></svg>',
};

/** Devuelve las alertas que pasan el filtro de estado y el buscador. */
function visibleAlerts() {
  const query = state.search.trim().toLowerCase();
  return state.alerts.filter((alert) => {
    const matchesStatus = state.filter === "todas" || statusOf(alert) === state.filter;
    const asset = assetOf(alert);
    const matchesSearch =
      !query || alert.ticker.toLowerCase().includes(query) || asset.name.toLowerCase().includes(query);
    return matchesStatus && matchesSearch;
  });
}

/** Actualiza el resumen del encabezado, por ejemplo "5 activas / 1 pausada / 2 disparadas". */
function renderSummary() {
  const counts = { activa: 0, pausada: 0, disparada: 0 };
  state.alerts.forEach((alert) => counts[statusOf(alert)]++);

  if (state.alerts.length === 0) {
    summary.textContent = "Todavía no configuraste ninguna alerta.";
    return;
  }

  const parts = [`${counts.activa} ${counts.activa === 1 ? "activa" : "activas"}`];
  if (counts.pausada) parts.push(`${counts.pausada} ${counts.pausada === 1 ? "pausada" : "pausadas"}`);
  if (counts.disparada) parts.push(`${counts.disparada} ${counts.disparada === 1 ? "disparada" : "disparadas"}`);
  summary.textContent = parts.join(" / ");
}

/** Arma el indicador de estado de una alerta (en monitoreo, pausada o disparada con fecha). */
function buildStatus(alert) {
  const status = statusOf(alert);
  const box = el("p", "al-estado");
  box.dataset.estado = status;

  if (status === "activa") {
    box.insertAdjacentHTML("beforeend", ICONS.campana);
    const last = alert.last_triggered_at ? ` · último aviso ${formatDateTime(alert.last_triggered_at)}` : "";
    box.append(`En monitoreo${last}`);
  } else if (status === "disparada") {
    box.insertAdjacentHTML("beforeend", ICONS.check);
    box.append(`Disparada el ${formatDateTime(alert.last_triggered_at)}`);
  } else {
    box.insertAdjacentHTML("beforeend", ICONS.pausa);
    box.append("Pausada");
  }
  return box;
}

/** Arma un botón de ícono para las acciones de una tarjeta. */
function buildIconButton(action, label, alertId) {
  const button = el("button", "al-icono");
  button.type = "button";
  button.dataset.accion = action;
  button.dataset.id = alertId;
  button.setAttribute("aria-label", label);
  button.title = label;
  button.innerHTML = ICONS[action === "preview" ? "mensaje" : action];
  return button;
}

/** Arma la tarjeta completa de una alerta. */
function buildCard(alert) {
  const asset = assetOf(alert);
  const card = el("article", "al-card");
  card.dataset.id = alert.id;
  card.dataset.estado = statusOf(alert);

  // Identificación: ticker, divisa y nombre
  const identity = el("div", "min-w-0");
  const tickerLine = el("div", "flex items-center gap-2");
  tickerLine.append(el("span", "text-lg font-bold text-white", alert.ticker));
  const currency = el("span", "db-badge", asset.currency);
  currency.dataset.tipo = "divisa";
  tickerLine.append(currency);
  identity.append(tickerLine, el("p", "mt-0.5 truncate text-sm text-slate-400", asset.name));

  // Regla y estado
  const rule = el("div", "min-w-0");
  const badge = el("span", "al-regla", ruleText(alert));
  badge.dataset.tipo = alert.alert_type;
  rule.append(badge, buildStatus(alert));

  // Acciones: interruptor y botones
  const actions = el("div", "flex items-center gap-1 md:justify-end");
  const toggle = el("button", "al-switch mr-2");
  toggle.type = "button";
  toggle.setAttribute("role", "switch");
  toggle.setAttribute("aria-checked", String(alert.is_active));
  toggle.setAttribute("aria-label", `${alert.is_active ? "Pausar" : "Activar"} la alerta de ${alert.ticker}`);
  toggle.dataset.accion = "alternar";
  toggle.dataset.id = alert.id;
  actions.append(
    toggle,
    buildIconButton("preview", "Ver cómo llega el aviso", alert.id),
    buildIconButton("editar", "Editar alerta", alert.id),
    buildIconButton("eliminar", "Eliminar alerta", alert.id)
  );

  card.append(identity, rule, actions);
  return card;
}

/** Arma la tarjeta que se muestra cuando el usuario no tiene ninguna alerta. */
function buildEmptyState() {
  const card = el("div", "rounded-xl border border-dashed border-slate-600 bg-[#1e293b] px-6 py-12 text-center");
  card.insertAdjacentHTML(
    "beforeend",
    '<svg class="mx-auto h-12 w-12 text-slate-500" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.500" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9"/><path d="M10.300 21a1.940 1.940 0 0 0 3.400 0"/><path d="M2 4.500 4 3M22 4.500 20 3"/></svg>'
  );
  card.append(
    el(
      "p",
      "mx-auto mt-4 max-w-md text-sm leading-relaxed text-slate-300",
      "No tenés alertas configuradas. Creá tu primera alerta para recibir avisos automáticos en Telegram cuando el mercado se mueva."
    )
  );
  const button = el("button", "al-btn-primario mt-5", "+ Crear primera alerta");
  button.type = "button";
  button.dataset.accion = "nueva";
  card.append(button);
  return card;
}

/** Dibuja el resumen y la lista de alertas según el filtro y la búsqueda actuales. */
function render() {
  renderSummary();
  listBox.textContent = "";
  $("#herramientas").classList.toggle("hidden", state.alerts.length === 0);

  if (state.alerts.length === 0) {
    listBox.appendChild(buildEmptyState());
    return;
  }

  const alerts = visibleAlerts();
  if (alerts.length === 0) {
    listBox.appendChild(el("p", "py-10 text-center text-sm text-slate-400", "Ninguna alerta coincide con el filtro."));
    return;
  }

  alerts.forEach((alert) => listBox.appendChild(buildCard(alert)));
}

/** Cambia el filtro de estado activo. */
function setFilter(filter) {
  state.filter = filter;
  document.querySelectorAll("#filtros .db-chip").forEach((chip) => {
    chip.setAttribute("aria-pressed", String(chip.dataset.estado === filter));
  });
  render();
}

// ───────────────────────── Activar / pausar ─────────────────────────

/**
 * Activa o pausa una alerta con actualización optimista: la pantalla cambia al
 * instante y, si el backend rechaza el cambio, vuelve al estado anterior.
 */
async function toggleAlert(alertId) {
  const alert = state.alerts.find((item) => item.id === alertId);
  if (!alert) return;

  const previous = { ...alert };
  const willBeActive = !alert.is_active;

  alert.is_active = willBeActive;
  // Reactivar vuelve a armar la alerta: el backend borra la fecha del último disparo
  if (willBeActive) alert.last_triggered_at = null;
  render();

  try {
    const updated = await api(`/api/alerts/${alertId}`, {
      method: "PATCH",
      body: JSON.stringify({ is_active: willBeActive }),
    });
    Object.assign(alert, updated);
    showToast(willBeActive ? "Alerta activada" : "Alerta pausada");
  } catch (error) {
    Object.assign(alert, previous);
    showToast(`No se pudo cambiar el estado: ${error.message}`, "error");
  }
  render();
}

// ───────────────────────── Formulario de creación / edición ─────────────────────────

/** Devuelve el tipo de condición elegido en el formulario. */
function selectedType() {
  return form.querySelector('input[name="tipo"]:checked').value;
}

/** Devuelve el activo elegido en el desplegable del formulario. */
function selectedFormAsset() {
  return state.assetsById.get(Number(assetSelect.value)) ?? null;
}

/**
 * Interpreta el texto del campo de valor. Acepta coma o punto como separador
 * decimal y hasta 4 decimales. Devuelve el número, o null si no es válido.
 */
function parseValue(text) {
  const clean = text.trim().replace(/\s/g, "");
  if (!/^\d+([.,]\d{1,4})?$/.test(clean)) return null;
  const value = Number(clean.replace(",", "."));
  return Number.isFinite(value) ? value : null;
}

/** Actualiza la unidad, la etiqueta, la cotización de referencia y la distancia al objetivo. */
function updateFormHints() {
  const asset = selectedFormAsset();
  const type = selectedType();
  const isPercent = type === "PCT_CHANGE";

  $("#etiqueta-valor").textContent = isPercent ? "Porcentaje de variación" : "Precio objetivo";
  $("#unidad-valor").textContent = isPercent ? "%" : currencySymbol(asset?.currency);

  const hasPrice = asset && typeof asset.current_price === "number";
  $("#ayuda-cotizacion").textContent = !asset
    ? ""
    : hasPrice
      ? `Cotización actual: ${formatPrice(asset.current_price, asset.currency)} ${asset.currency}`
      : "Todavía no hay una cotización registrada para este activo.";

  // Ayuda dinámica: qué tan lejos está el objetivo, o entre qué precios avisaría
  const value = parseValue(valueInput.value);
  const hint = $("#ayuda-distancia");
  hint.textContent = "";
  if (!hasPrice || value == null || value <= 0) return;

  const price = asset.current_price;
  if (isPercent) {
    const up = formatPrice(price * (1 + value / 100), asset.currency);
    const down = formatPrice(price * (1 - value / 100), asset.currency);
    hint.textContent = `Con la cotización actual, avisaría al llegar a ${up} o a ${down}.`;
  } else {
    const alreadyMet = type === "ABOVE" ? price >= value : price <= value;
    if (alreadyMet) {
      hint.textContent = "La cotización actual ya cumple esta condición: la alerta se dispararía en el próximo ciclo.";
    } else {
      const distance = Math.abs(((value - price) / price) * 100);
      const percent = distance.toLocaleString("es-AR", { maximumFractionDigits: 2 });
      hint.textContent = `El objetivo está a ${percent}% de la cotización actual.`;
    }
  }
}

/** Carga el desplegable de activos: los seguidos y, al editar, también el de la alerta. */
function fillAssetOptions(extraAssetId) {
  const assets = [...state.myAssets];
  if (extraAssetId != null && !assets.some((asset) => asset.id === extraAssetId)) {
    const extra = state.assetsById.get(extraAssetId);
    if (extra) assets.push(extra);
  }

  assetSelect.textContent = "";
  for (const asset of assets) {
    const option = el("option", null, `${asset.ticker} — ${asset.name}`);
    option.value = asset.id;
    assetSelect.appendChild(option);
  }
  return assets.length;
}

/** Abre el formulario vacío para crear una alerta. */
function openCreateForm() {
  if (fillAssetOptions(null) === 0) {
    showToast("Primero seguí al menos un activo desde el Dashboard para poder crear una alerta.", "error");
    return;
  }

  state.editingId = null;
  $("#form-titulo").textContent = "Nueva alerta";
  assetSelect.disabled = false;
  form.querySelector('input[name="tipo"][value="ABOVE"]').checked = true;
  valueInput.value = "";
  valueError.classList.add("hidden");
  updateFormHints();
  formModal.showModal();
  assetSelect.focus();
}

/** Abre el formulario con los datos de una alerta existente para editarla. */
function openEditForm(alertId) {
  const alert = state.alerts.find((item) => item.id === alertId);
  if (!alert) return;

  state.editingId = alertId;
  $("#form-titulo").textContent = `Editar alerta de ${alert.ticker}`;
  fillAssetOptions(alert.asset_id);
  assetSelect.value = alert.asset_id;
  assetSelect.disabled = true; // El activo de una alerta no se cambia: para eso se crea otra
  form.querySelector(`input[name="tipo"][value="${alert.alert_type}"]`).checked = true;
  valueInput.value = String(alertValue(alert)).replace(".", ",");
  valueError.classList.add("hidden");
  updateFormHints();
  formModal.showModal();
  valueInput.focus();
}

/** Valida el formulario y devuelve el cuerpo a enviar, o null si hay un error (que se muestra en pantalla). */
function readFormPayload() {
  const value = parseValue(valueInput.value);
  let message = "";

  if (valueInput.value.trim() === "") message = "Ingresá un valor.";
  else if (value == null) message = "Ingresá un número válido, sin separador de miles y con hasta 4 decimales (por ejemplo 1250,50).";
  else if (value <= 0) message = "El valor tiene que ser mayor a 0.";
  else if (value >= MAX_ALERT_VALUE) message = "El valor es demasiado grande. Ingresá un número menor a 100.000.000.";

  if (message) {
    valueError.textContent = message;
    valueError.classList.remove("hidden");
    valueInput.focus();
    return null;
  }
  valueError.classList.add("hidden");

  const type = selectedType();
  const payload = { alert_type: type };
  if (type === "PCT_CHANGE") payload.threshold_percent = value;
  else payload.target_price = value;
  return payload;
}

/** Guarda el formulario: crea una alerta nueva o actualiza la que se está editando. */
async function submitForm(event) {
  event.preventDefault();
  const payload = readFormPayload();
  if (!payload) return;

  const saveButton = $("#btn-guardar");
  setBusy(saveButton, true);
  try {
    if (state.editingId == null) {
      const created = await api("/api/alerts", {
        method: "POST",
        body: JSON.stringify({ asset_id: Number(assetSelect.value), ...payload }),
      });
      state.alerts.unshift(created);
      showToast("Alerta creada correctamente");
    } else {
      const updated = await api(`/api/alerts/${state.editingId}`, {
        method: "PATCH",
        body: JSON.stringify(payload),
      });
      const index = state.alerts.findIndex((item) => item.id === state.editingId);
      if (index !== -1) state.alerts[index] = updated;
      showToast("Alerta actualizada");
    }
    formModal.close();
    render();
  } catch (error) {
    valueError.textContent = error.message;
    valueError.classList.remove("hidden");
  } finally {
    setBusy(saveButton, false);
  }
}

// ───────────────────────── Eliminación ─────────────────────────

/** Abre el diálogo de confirmación para eliminar una alerta. */
function openDeleteConfirm(alertId) {
  const alert = state.alerts.find((item) => item.id === alertId);
  if (!alert) return;

  state.deletingId = alertId;
  $("#eliminar-texto").textContent = `Se va a eliminar la alerta "${ruleText(alert)}" de ${alert.ticker}. Esta acción no se puede deshacer.`;
  deleteModal.showModal();
}

/** Elimina la alerta confirmada y, cuando el backend responde bien, quita su tarjeta con una transición. */
async function confirmDelete() {
  const alertId = state.deletingId;
  const button = $("#btn-eliminar-confirmar");

  setBusy(button, true, "Eliminando...");
  try {
    await api(`/api/alerts/${alertId}`, { method: "DELETE" });
  } catch (error) {
    showToast(`No se pudo eliminar: ${error.message}`, "error");
    setBusy(button, false);
    return;
  }
  setBusy(button, false);
  deleteModal.close();

  state.alerts = state.alerts.filter((item) => item.id !== alertId);
  const card = listBox.querySelector(`.al-card[data-id="${alertId}"]`);
  if (card) {
    card.classList.add("saliendo");
    setTimeout(render, 200);
  } else {
    render();
  }
  showToast("Alerta eliminada");
}

// ───────────────────────── Vista previa del aviso ─────────────────────────

/** Formatea un precio como lo hace el worker en sus mensajes: "$1,505.20 ARS". */
function workerPrice(value, currency) {
  const number = value.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  return `$${number} ${currency}`;
}

/**
 * Arma el mismo texto que envía el worker (app/worker/checker.py) cuando la alerta
 * se dispara, con valores de ejemplo. Devuelve una lista de líneas [texto, enNegrita].
 */
function buildPreviewLines(alert) {
  const asset = assetOf(alert);
  const value = alertValue(alert);
  const current = typeof asset.current_price === "number" ? asset.current_price : null;
  const assetLine = [["Activo: "], [alert.ticker, true], [` (${asset.name})`]];

  if (alert.alert_type === "PCT_CHANGE") {
    const reference = current ?? 1000;
    const price = reference * (1 + value / 100);
    return [
      [["📈 "], ["Alerta de Variación Porcentual", true]],
      assetLine,
      [["Variación: "], [`${value.toFixed(2)}%`, true], [" (subió)"]],
      [["Precio actual: "], [workerPrice(price, asset.currency), true]],
      [["Precio referencia anterior: "], [workerPrice(reference, asset.currency), true]],
      [["Umbral: "], [`${value.toFixed(2)}%`, true]],
    ];
  }

  // En una alerta de precio, el ejemplo usa la cotización actual si ya cumple la condición; si no, el propio umbral
  const isAbove = alert.alert_type === "ABOVE";
  const meets = current != null && (isAbove ? current >= value : current <= value);
  const price = meets ? current : value;
  return [
    [[isAbove ? "🎯 " : "⚠️ "], [isAbove ? "Alerta Take Profit / Techo Superado" : "Alerta Stop Loss / Piso Alcanzado", true]],
    assetLine,
    [["Precio actual: "], [workerPrice(price, asset.currency), true]],
    [["Umbral fijado: "], [workerPrice(value, asset.currency), true]],
  ];
}

/** Abre la vista previa del aviso de una alerta, dibujado como una burbuja de Telegram. */
function openPreview(alertId) {
  const alert = state.alerts.find((item) => item.id === alertId);
  if (!alert) return;

  const bubble = $("#preview-mensaje");
  bubble.textContent = "";
  buildPreviewLines(alert).forEach((line, index) => {
    if (index > 0) bubble.append("\n");
    line.forEach(([text, bold]) => bubble.append(bold ? el("strong", null, text) : text));
  });
  previewModal.showModal();
}

// ───────────────────────── Inicio ─────────────────────────

/** Cierra la sesión y vuelve al login. */
async function logout() {
  try {
    await api("/api/auth/logout", { method: "POST" });
  } catch {
    // Aunque falle el pedido, se vuelve al login.
  }
  window.location.href = LOGIN_URL;
}

/** Conecta los controles de la página con sus funciones. */
function bindEvents() {
  $("#btn-nueva").addEventListener("click", openCreateForm);
  $("#btn-salir").addEventListener("click", logout);

  $("#buscador").addEventListener("input", (event) => {
    state.search = event.target.value;
    render();
  });
  $("#filtros").addEventListener("click", (event) => {
    const chip = event.target.closest(".db-chip");
    if (chip) setFilter(chip.dataset.estado);
  });

  // Un solo listener para todas las acciones de las tarjetas
  listBox.addEventListener("click", (event) => {
    const target = event.target.closest("[data-accion]");
    if (!target) return;
    const alertId = Number(target.dataset.id);
    const actions = {
      nueva: openCreateForm,
      alternar: () => toggleAlert(alertId),
      preview: () => openPreview(alertId),
      editar: () => openEditForm(alertId),
      eliminar: () => openDeleteConfirm(alertId),
    };
    actions[target.dataset.accion]?.();
  });

  form.addEventListener("submit", submitForm);
  form.addEventListener("change", updateFormHints);
  valueInput.addEventListener("input", () => {
    valueError.classList.add("hidden");
    updateFormHints();
  });
  $("#btn-cancelar").addEventListener("click", () => formModal.close());

  $("#btn-eliminar-cancelar").addEventListener("click", () => deleteModal.close());
  $("#btn-eliminar-confirmar").addEventListener("click", confirmDelete);
  $("#btn-preview-cerrar").addEventListener("click", () => previewModal.close());

  // Un clic sobre el fondo oscuro cierra el modal
  [formModal, deleteModal, previewModal].forEach((modal) => {
    modal.addEventListener("click", (event) => {
      if (event.target === modal) modal.close();
    });
  });
}

/** Punto de entrada: verifica la sesión y carga el catálogo, la cartera y las alertas. */
async function init() {
  bindEvents();

  try {
    state.user = await api("/api/auth/me");
    if (!state.user.onboarding_completed) {
      window.location.href = ONBOARDING_URL;
      return;
    }
    $("#usuario-email").textContent = state.user.email;
    $("#aviso-telegram").classList.toggle("hidden", Boolean(state.user.telegram_chat_id));

    const [catalog, myAssets, alerts] = await Promise.all([
      api("/api/assets"),
      api("/api/assets/me"),
      api("/api/alerts"),
    ]);
    state.assetsById = new Map(catalog.map((asset) => [asset.id, asset]));
    state.myAssets = myAssets;
    state.alerts = alerts;
  } catch (error) {
    listBox.textContent = "";
    listBox.appendChild(el("p", "py-10 text-center text-sm text-rose-200", error.message));
    return;
  }

  render();
}

init();
