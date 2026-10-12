"""
app/services/email.py
─────────────────────
Servicio asíncrono para enviar alertas por correo electrónico mediante la API
HTTP de Brevo. Es el canal de respaldo cuando el usuario no vinculó Telegram
o el envío por Telegram falla.

Sigue las mismas reglas que services/telegram.py:
  - Peticiones con httpx.AsyncClient y timeout explícito de 10 segundos.
  - Clave y remitente leídos desde app/config.py (nunca fijos en el código).
  - Cualquier fallo se loguea y devuelve False, sin interrumpir al worker.

Función pública:
  send_email_alert(to_email, subject, html_message) → bool
"""

import asyncio
import logging
import sys

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

# Endpoint de correos transaccionales de Brevo
_BREVO_URL = "https://api.brevo.com/v3/smtp/email"

# Timeout máximo por petición (AGENTS.md §5.2)
_TIMEOUT = httpx.Timeout(10.0)


def email_is_configured() -> bool:
    """Indica si están cargados la clave de Brevo y el correo remitente."""
    return bool(settings.brevo_api_key and settings.email_from)


async def send_email_alert(
    to_email: str,
    subject: str,
    html_message: str,
    *,
    client: httpx.AsyncClient | None = None,
) -> bool:
    """Envía un correo con la alerta al usuario. Devuelve True si Brevo lo aceptó y False si falló."""
    if not email_is_configured():
        logger.warning(
            "BREVO_API_KEY o EMAIL_FROM no están configurados. No se puede enviar el correo a %s.",
            to_email,
        )
        return False

    headers = {"api-key": settings.brevo_api_key, "accept": "application/json"}
    payload = {
        "sender": {"name": settings.app_name, "email": settings.email_from},
        "to": [{"email": to_email}],
        "subject": subject,
        # Los mensajes del worker usan saltos de línea; en HTML hay que convertirlos.
        "htmlContent": html_message.replace("\n", "<br>"),
    }

    async def _post(c: httpx.AsyncClient) -> bool:
        """Hace la petición a Brevo y traduce cualquier error a False."""
        try:
            response = await c.post(_BREVO_URL, json=payload, headers=headers, timeout=_TIMEOUT)
            response.raise_for_status()
            logger.info("Alerta enviada por correo a %s.", to_email)
            return True
        except httpx.HTTPStatusError as exc:
            # 401 → clave inválida; 400 → remitente no verificado o destinatario mal formado
            logger.warning(
                "HTTP %s al enviar correo a %s: %s",
                exc.response.status_code,
                to_email,
                exc.response.text[:300],
            )
            return False
        except httpx.TimeoutException:
            logger.warning("Timeout al enviar correo a %s.", to_email)
            return False
        except httpx.RequestError as exc:
            logger.warning("Error de red al enviar correo a %s: %s", to_email, exc)
            return False

    if client is not None:
        return await _post(client)

    async with httpx.AsyncClient() as c:
        return await _post(c)


# ── Prueba rápida en consola ──────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    async def _demo() -> None:
        """Envía un correo de prueba a la dirección pasada por argumento."""
        if len(sys.argv) < 2:
            print("Uso:\n  python -m app.services.email <CORREO_DESTINO>")
            sys.exit(1)

        destino = sys.argv[1]
        print(f"Enviando correo de prueba a: {destino} ...")
        ok = await send_email_alert(
            destino,
            "Prueba de alertas por correo",
            "🚀 <b>Conexión por correo exitosa</b>\nEste es un mensaje de prueba del monitor de precios.",
        )
        if ok:
            print("✓ Brevo aceptó el correo. Revisá la bandeja de entrada y la carpeta de spam.")
        else:
            print("✗ Falló el envío. Revisá los logs, BREVO_API_KEY y EMAIL_FROM en el .env.")

    asyncio.run(_demo())
