/**
 * settings.js — Pantalla de Configuración y Perfil de MonitorPrecios.
 *
 * Cuatro tarjetas: perfil de Google (solo lectura), canal de Telegram,
 * preferencias guardadas en el navegador y sesión.
 *
 * Endpoints que consume (todos con la cookie HttpOnly de sesión):
 *   GET   /api/auth/me                 datos del usuario
 *   PATCH /api/auth/telegram           guardar o desvincular el Chat ID
 *   POST  /api/auth/telegram/test      enviar un mensaje de prueba
 *   POST  /api/auth/logout             cerrar sesión
 *
 * Ante un 401 se redirige al login.
 */

// ───────────────────────── Configuración ─────────────────────────

const LOGIN_URL = "/";
const ONBOARDING_URL = "/onboarding";

// Usuario del bot de Telegram del sistema (sin @). El backend no lo expone.
const TELEGRAM_BOT_USERNAME = "MonitorPreciosArgentinos_bot";

// Clave de la preferencia en localStorage (la lee también dashboard.js)
const PREF_CURRENCY_KEY = "mp.pref.divisa";

// ───────────────────────── Estado y elementos ─────────────────────────

const state = { user: null };

const $ = (selector) => document.querySelector(selector);

const toastsBox = $("#toasts");
const chatInput = $("#telegram-chat-id");
const chatError = $("#telegram-error");
const testResult = $("#prueba-resultado");
const confirmModal = $("#modal-confirmar");

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

/** Pone un botón en estado ocupado (deshabilitado y con spinner) o lo restaura. */
function setBusy(button, isBusy, busyText = "Guardando...") {
  if (isBusy) {
    button.dataset.textoOriginal = button.textContent;
    button.disabled = true;
    button.textContent = "";
    const spinner = document.createElement("span");
    spinner.className = "spinner";
    const label = document.createElement("span");
    label.textContent = busyText;
    button.append(spinner, label);
  } else {
    button.disabled = false;
    button.textContent = button.dataset.textoOriginal ?? button.textContent;
  }
}

/**
 * Abre el diálogo de confirmación y devuelve una promesa que se resuelve en
 * true si la persona confirma, o false si cancela o cierra el diálogo.
 */
function confirmAction({ title, text, confirmLabel, danger = false }) {
  $("#confirmar-titulo").textContent = title;
  $("#confirmar-texto").textContent = text;
  const okButton = $("#btn-confirmar-ok");
  okButton.textContent = confirmLabel;
  okButton.className = danger ? "al-btn-peligro" : "al-btn-primario";

  return new Promise((resolve) => {
    const finish = (result) => {
      okButton.removeEventListener("click", onOk);
      confirmModal.removeEventListener("close", onClose);
      if (confirmModal.open) confirmModal.close();
      resolve(result);
    };
    const onOk = () => finish(true);
    const onClose = () => finish(false);
    okButton.addEventListener("click", onOk);
    confirmModal.addEventListener("close", onClose);
    confirmModal.showModal();
  });
}

/** Lee una preferencia de localStorage; si el navegador lo bloquea, devuelve el valor por defecto. */
function readPref(key, fallback) {
  try {
    return localStorage.getItem(key) ?? fallback;
  } catch {
    return fallback;
  }
}

/** Guarda una preferencia en localStorage y avisa si el navegador no lo permite. */
function writePref(key, value) {
  try {
    localStorage.setItem(key, value);
    return true;
  } catch {
    showToast("Este navegador no permite guardar preferencias.", "error");
    return false;
  }
}

// ───────────────────────── 1. Perfil ─────────────────────────

/** Devuelve hasta dos iniciales a partir del nombre (o del correo si no hay nombre). */
function initialsOf(user) {
  const source = (user.name || user.email || "?").trim();
  const words = source.split(/\s+/).filter(Boolean);
  const letters = words.length >= 2 ? words[0][0] + words[1][0] : source.slice(0, 2);
  return letters.toUpperCase();
}

/** Dibuja el avatar: la foto de Google si el backend la envía y carga bien; si no, las iniciales. */
function renderAvatar(user) {
  const avatar = $("#avatar");
  avatar.textContent = initialsOf(user);

  // El backend actual no guarda la foto; si en el futuro la envía, se usa con las iniciales como respaldo
  if (!user.picture) return;
  const img = new Image();
  img.alt = "";
  img.referrerPolicy = "no-referrer";
  img.className = "h-full w-full rounded-full object-cover";
  img.onload = () => avatar.replaceChildren(img);
  img.src = user.picture;
}

/** Dibuja la tarjeta de perfil con los datos de solo lectura de Google. */
function renderProfile() {
  const user = state.user;
  $("#usuario-email").textContent = user.email;
  $("#perfil-nombre").textContent = user.name || "Sin nombre";
  $("#perfil-email").textContent = user.email;
  renderAvatar(user);

  const date = new Date(user.created_at);
  $("#perfil-registro").textContent = Number.isNaN(date.getTime())
    ? "—"
    : date.toLocaleDateString("es-AR", { day: "numeric", month: "long", year: "numeric" });
}

// ───────────────────────── 2. Telegram ─────────────────────────

/** Enmascara el Chat ID dejando visibles solo los últimos 3 dígitos, por ejemplo "••••••842". */
function maskChatId(chatId) {
  const visible = chatId.slice(-3);
  return "•".repeat(Math.max(chatId.length - 3, 3)) + visible;
}

/** Actualiza el estado del canal: insignia, resumen, campo y qué botones están disponibles. */
function renderTelegram() {
  const chatId = state.user.telegram_chat_id;
  const linked = Boolean(chatId);

  const badge = $("#telegram-badge");
  badge.dataset.estado = linked ? "conectado" : "sin-vincular";
  badge.textContent = linked ? "✓ Conectado" : "Sin vincular";

  $("#telegram-resumen").textContent = linked
    ? `Chat ID: ${maskChatId(chatId)}. Los avisos de tus alertas llegan por Telegram.`
    : "Todavía no vinculaste Telegram. Mientras tanto, los avisos de tus alertas llegan a tu correo.";

  chatInput.value = chatId ?? "";
  chatError.classList.add("hidden");
  $("#btn-desvincular").classList.toggle("hidden", !linked);
  updateTestButton();
}

/** Habilita "Enviar mensaje de prueba" solo si hay un Chat ID guardado y el campo no tiene cambios sin guardar. */
function updateTestButton() {
  const saved = state.user.telegram_chat_id ?? "";
  const button = $("#btn-probar");
  const hasUnsavedChanges = chatInput.value.trim() !== saved;
  button.disabled = !saved || hasUnsavedChanges;
  button.title = !saved
    ? "Primero guardá un Chat ID"
    : hasUnsavedChanges
      ? "Guardá los cambios antes de probar"
      : "";
}

/** Valida y guarda el Chat ID escrito en el campo. */
async function saveTelegram(event) {
  event.preventDefault();
  const chatId = chatInput.value.trim();

  if (!/^-?\d{5,20}$/.test(chatId)) {
    chatError.textContent = chatId
      ? "El Chat ID es un número, por ejemplo 123456789."
      : "Ingresá tu Chat ID. Si querés quitar Telegram, usá “Desvincular”.";
    chatError.classList.remove("hidden");
    chatInput.focus();
    return;
  }
  chatError.classList.add("hidden");

  const button = $("#btn-guardar-telegram");
  setBusy(button, true);
  try {
    state.user = await api("/api/auth/telegram", {
      method: "PATCH",
      body: JSON.stringify({ telegram_chat_id: chatId }),
    });
    testResult.textContent = "";
    showToast("Canal de Telegram actualizado correctamente");
  } catch (error) {
    showToast(error.message, "error");
  } finally {
    setBusy(button, false);
    renderTelegram();
  }
}

/** Pide al backend que envíe un mensaje de prueba y muestra si Telegram lo entregó. */
async function sendTestMessage() {
  const button = $("#btn-probar");
  testResult.textContent = "";
  setBusy(button, true, "Enviando...");
  try {
    await api("/api/auth/telegram/test", { method: "POST" });
    testResult.className = "basis-full text-sm text-emerald-300";
    testResult.textContent = "✓ Mensaje enviado. Revisá tu Telegram.";
  } catch (error) {
    testResult.className = "basis-full text-sm text-rose-300";
    testResult.textContent = error.message;
  } finally {
    setBusy(button, false);
    updateTestButton();
  }
}

/** Quita el Chat ID del usuario, previa confirmación. */
async function unlinkTelegram() {
  const confirmed = await confirmAction({
    title: "¿Desvincular Telegram?",
    text: "Vas a dejar de recibir avisos por Telegram. Tus alertas siguen activas y los avisos pasan a llegar por correo.",
    confirmLabel: "Desvincular",
    danger: true,
  });
  if (!confirmed) return;

  try {
    state.user = await api("/api/auth/telegram", {
      method: "PATCH",
      body: JSON.stringify({ telegram_chat_id: null }),
    });
    testResult.textContent = "";
    showToast("Telegram desvinculado");
  } catch (error) {
    showToast(error.message, "error");
  }
  renderTelegram();
}

// ───────────────────────── 3. Preferencias ─────────────────────────

/** Carga la preferencia de divisa guardada en el navegador y conecta su selector. */
function initPreferences() {
  const currencySelect = $("#pref-divisa");
  const saved = readPref(PREF_CURRENCY_KEY, "todas");
  currencySelect.value = ["todas", "ARS", "USD"].includes(saved) ? saved : "todas";
  currencySelect.addEventListener("change", () => {
    if (writePref(PREF_CURRENCY_KEY, currencySelect.value)) showToast("Preferencia guardada");
  });
}

// ───────────────────────── 4. Sesión ─────────────────────────

/** Cierra la sesión, borra los datos guardados en el navegador y vuelve al login. */
async function handleLogout() {
  try {
    await fetch("/api/auth/logout", { method: "POST", credentials: "include" });
  } finally {
    try {
      localStorage.clear();
    } catch {
      // Si el navegador bloquea localStorage no hay nada que limpiar.
    }
    window.location.href = LOGIN_URL;
  }
}

// ───────────────────────── Inicio ─────────────────────────

/** Conecta los controles de la página con sus funciones. */
function bindEvents() {
  $("#form-telegram").addEventListener("submit", saveTelegram);
  chatInput.addEventListener("input", () => {
    chatError.classList.add("hidden");
    updateTestButton();
  });
  $("#btn-probar").addEventListener("click", sendTestMessage);
  $("#btn-desvincular").addEventListener("click", unlinkTelegram);
  $("#btn-salir").addEventListener("click", handleLogout);

  $("#btn-confirmar-cancelar").addEventListener("click", () => confirmModal.close());
  confirmModal.addEventListener("click", (event) => {
    if (event.target === confirmModal) confirmModal.close();
  });
}

/** Punto de entrada: carga el usuario y dibuja las tarjetas. */
async function init() {
  $("#link-bot").href = `https://t.me/${TELEGRAM_BOT_USERNAME}`;
  bindEvents();
  initPreferences();

  try {
    state.user = await api("/api/auth/me");
  } catch (error) {
    $("#cargando").textContent = error.message;
    return;
  }

  // Quien todavía no pasó por el asistente de inicio va primero ahí
  if (!state.user.onboarding_completed) {
    window.location.href = ONBOARDING_URL;
    return;
  }

  renderProfile();
  renderTelegram();
  $("#cargando").classList.add("hidden");
  $("#contenido").classList.remove("hidden");
}

init();
