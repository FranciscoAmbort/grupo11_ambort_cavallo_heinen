/**
 * login.js — Pantalla de inicio de sesión de MonitorPrecios.
 *
 * Flujo:
 *   1. Si ya hay una sesión activa, redirige sin mostrar el login.
 *   2. Pide el Client ID de Google al backend (GET /api/config).
 *   3. Dibuja el botón oficial de Google Identity Services.
 *   4. Cuando Google entrega el id_token, lo envía a POST /api/auth/google.
 *   5. Según `onboarding_completed`, redirige a /onboarding o a /dashboard.
 *
 * La sesión viaja en una cookie HttpOnly que maneja el navegador: este archivo
 * nunca lee ni guarda el token.
 */

// Client ID de respaldo, por si /api/config no responde. Dejar vacío para no usarlo.
const FALLBACK_GOOGLE_CLIENT_ID = "";

// Tiempo máximo de espera a que cargue la biblioteca de Google (milisegundos)
const GOOGLE_LOAD_TIMEOUT_MS = 10000;

const googleButton = document.getElementById("google-signin-btn");
const actionsBox = document.getElementById("auth-actions");
const loadingBox = document.getElementById("auth-loading");
const errorBox = document.getElementById("auth-error");
const errorText = document.getElementById("auth-error-text");

/** Muestra u oculta el estado "Autenticando..." y esconde el botón mientras dura. */
function setLoading(isLoading) {
  googleButton.classList.toggle("hidden", isLoading);
  loadingBox.classList.toggle("hidden", !isLoading);
  loadingBox.classList.toggle("flex", isLoading);
}

/** Muestra el banner de error con el mensaje indicado. */
function showError(message) {
  errorText.textContent = message;
  errorBox.classList.remove("hidden");
}

/** Oculta el banner de error. */
function clearError() {
  errorBox.classList.add("hidden");
  errorText.textContent = "";
}

/** Lleva al usuario al onboarding si no lo completó, o al dashboard si ya lo hizo. */
function redirectAfterLogin(user) {
  window.location.href = user.onboarding_completed ? "/dashboard" : "/onboarding";
}

/** Traduce una respuesta de error del backend a un mensaje claro para el usuario. */
async function errorMessageFor(res) {
  if (res.status === 401) {
    return "Google no pudo validar tu cuenta. Probá de nuevo o usá otra cuenta.";
  }
  if (res.status === 502) {
    return "No pudimos comunicarnos con Google para validar tu cuenta. Probá de nuevo en unos segundos.";
  }
  try {
    const body = await res.json();
    if (typeof body.detail === "string") return body.detail;
  } catch {
    // La respuesta no era JSON: se usa el mensaje genérico de abajo.
  }
  return `No se pudo iniciar sesión (error ${res.status}). Probá de nuevo.`;
}

/** Si el navegador ya tiene una sesión válida, redirige y devuelve true. */
async function redirectIfLoggedIn() {
  try {
    const res = await fetch("/api/auth/me", { credentials: "include" });
    if (!res.ok) return false;
    redirectAfterLogin(await res.json());
    return true;
  } catch {
    return false;
  }
}

/** Obtiene el Client ID de Google desde el backend, o usa el de respaldo si falla. */
async function fetchGoogleClientId() {
  try {
    const res = await fetch("/api/config");
    if (res.ok) {
      const config = await res.json();
      if (config.google_client_id) return config.google_client_id;
    }
  } catch {
    // Sin conexión con el backend: se intenta con el valor de respaldo.
  }
  return FALLBACK_GOOGLE_CLIENT_ID;
}

/** Espera a que la biblioteca de Google termine de cargar (se incluye con async/defer). */
function waitForGoogle() {
  return new Promise((resolve, reject) => {
    const startedAt = Date.now();
    const timer = setInterval(() => {
      if (window.google?.accounts?.id) {
        clearInterval(timer);
        resolve();
      } else if (Date.now() - startedAt > GOOGLE_LOAD_TIMEOUT_MS) {
        clearInterval(timer);
        reject(new Error("La biblioteca de Google no cargó a tiempo"));
      }
    }, 100);
  });
}

/** Recibe el id_token de Google, lo envía al backend y redirige si la sesión se creó. */
async function handleCredentialResponse(response) {
  clearError();
  setLoading(true);

  try {
    const res = await fetch("/api/auth/google", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "include", // Para que el navegador guarde la cookie HttpOnly de sesión
      body: JSON.stringify({ id_token: response.credential }),
    });

    if (!res.ok) {
      showError(await errorMessageFor(res));
      setLoading(false);
      return;
    }

    redirectAfterLogin(await res.json());
  } catch {
    showError("No pudimos conectar con el servidor. Revisá tu conexión y probá de nuevo.");
    setLoading(false);
  }
}

/** Dibuja el botón oficial de Google dentro de su contenedor. */
function renderGoogleButton() {
  google.accounts.id.renderButton(googleButton, {
    theme: "filled_black",
    size: "large",
    shape: "pill",
    text: "signin_with",
    logo_alignment: "left",
    locale: "es",
    // El botón no puede ser más ancho que la tarjeta en pantallas chicas
    width: Math.min(280, actionsBox.clientWidth),
  });
}

/** Punto de entrada de la pantalla: revisa la sesión, obtiene la configuración y arma el botón. */
async function init() {
  if (await redirectIfLoggedIn()) return;

  const clientId = await fetchGoogleClientId();
  if (!clientId) {
    showError("El inicio de sesión con Google no está configurado. Falta definir GOOGLE_CLIENT_ID en el servidor.");
    return;
  }

  try {
    await waitForGoogle();
  } catch {
    showError("No se pudo cargar el inicio de sesión de Google. Revisá tu conexión o desactivá el bloqueador de anuncios.");
    return;
  }

  google.accounts.id.initialize({
    client_id: clientId,
    callback: handleCredentialResponse,
  });
  renderGoogleButton();

  // Si cambia el ancho de la ventana (por ejemplo, al girar el celular), se redibuja el botón
  let lastWidth = actionsBox.clientWidth;
  window.addEventListener("resize", () => {
    if (actionsBox.clientWidth === lastWidth) return;
    lastWidth = actionsBox.clientWidth;
    renderGoogleButton();
  });
}

init();
