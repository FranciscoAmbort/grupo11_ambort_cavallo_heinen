"""
app/services/telegram.py
────────────────────────
Servicio asíncrono para el despacho de alertas push vía Telegram Bot API.

Reglas de arquitectura aplicadas (ver AGENTS.md §5.5):
  - Despacho ligero y asíncrono con httpx.AsyncClient (prohibido requests).
  - Timeout explícito de 10 segundos por petición.
  - Token leído desde app/config.py (nunca hardcodeado en el código fuente).
  - Errores HTTP 400 / 403 (chat_id inválido o bot bloqueado) se capturan con
    try/except y se loguean sin interrumpir la ejecución del worker ni las
    alertas de otros usuarios.

Función pública:
  send_telegram_alert(chat_id, message) → bool
"""

import asyncio
import logging
import os
import sys

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

# Endpoint de la Telegram Bot API
_TG_URL = "https://api.telegram.org/bot{token}/sendMessage"

# Timeout máximo por petición (AGENTS.md §5.2)
_TIMEOUT = httpx.Timeout(10.0)


async def send_telegram_alert(
    chat_id: str | int,
    message: str,
    *,
    parse_mode: str = "HTML",
    client: httpx.AsyncClient | None = None,
) -> bool:
    """
    Envía un mensaje de alerta al chat de Telegram del usuario.

    Parámetros:
      chat_id    – ID del chat destino (int o string numérico).
      message    – Texto del mensaje. Puede contener etiquetas HTML o Markdown
                   dependiendo del valor de parse_mode.
      parse_mode – Formato del mensaje: "HTML" (por defecto) o "Markdown".
      client     – AsyncClient externo opcional para reutilizar conexiones
                   (útil cuando el worker envía múltiples alertas en un ciclo).

    Retorna:
      True  → petición exitosa (HTTP 200).
      False → cualquier fallo (red, token inválido, chat bloqueado, etc.).

    Manejo de errores (AGENTS.md §5.5):
      - HTTP 400 / 403 (chat_id inválido o bot bloqueado): se loguea y se
        retorna False sin lanzar excepción.
      - TimeoutException / RequestError: ídem.
      - El worker no debe detener su ejecución por fallos en este servicio.
    """
    token = settings.telegram_bot_token
    if not token:
        logger.error(
            "TELEGRAM_BOT_TOKEN no está configurado en .env / settings. "
            "No se puede enviar la alerta al chat_id %s.",
            chat_id,
        )
        return False

    url = _TG_URL.format(token=token)
    payload = {
        "chat_id": str(chat_id),
        "text": message,
        "parse_mode": parse_mode,
    }

    async def _post(c: httpx.AsyncClient) -> bool:
        try:
            response = await c.post(url, json=payload, timeout=_TIMEOUT)
            response.raise_for_status()
            logger.info("Alerta enviada correctamente a chat_id=%s.", chat_id)
            return True
        except httpx.HTTPStatusError as exc:
            # 400 → payload inválido o chat_id mal formado
            # 403 → el usuario bloqueó el bot
            logger.warning(
                "HTTP %s al enviar alerta a chat_id=%s: %s",
                exc.response.status_code,
                chat_id,
                exc.response.text[:300],
            )
            return False
        except httpx.TimeoutException:
            logger.warning("Timeout al enviar alerta a chat_id=%s.", chat_id)
            return False
        except httpx.RequestError as exc:
            logger.warning("Error de red al enviar alerta a chat_id=%s: %s", chat_id, exc)
            return False

    if client is not None:
        return await _post(client)

    async with httpx.AsyncClient() as c:
        return await _post(c)


# ── Prueba rápida en consola ──────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    async def _demo() -> None:
        # Leer TEST_CHAT_ID desde argumento posicional o variable de entorno
        chat_id = sys.argv[1] if len(sys.argv) > 1 else os.getenv("TEST_CHAT_ID")

        if not chat_id:
            print(
                "Uso:\n"
                "  python -m app.services.telegram <CHAT_ID>\n"
                "  TEST_CHAT_ID=<CHAT_ID> python -m app.services.telegram"
            )
            sys.exit(1)

        print(f"Enviando mensaje de prueba al chat_id: {chat_id} ...")
        ok = await send_telegram_alert(
            chat_id,
            "🚀 Conexión con Telegram exitosa desde Mercado Monitor",
        )
        if ok:
            print("✓ Mensaje enviado correctamente.")
        else:
            print("✗ Falló el envío. Revisá los logs y el token en el .env.")

    asyncio.run(_demo())

