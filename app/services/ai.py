"""
app/services/ai.py
──────────────────
Cliente asíncrono mínimo de la API REST de Google Gemini (sin SDK).

Reglas aplicadas:
  - Peticiones con httpx.AsyncClient (prohibido requests).
  - La clave viaja en el header x-goog-api-key, nunca en la URL.
  - Timeout explícito de 60 segundos: generar texto tarda bastante más que
    las consultas de precios (excepción documentada a la regla de 10 s).
  - Clave y modelo leídos desde app/config.py (GEMINI_API_KEY, GEMINI_MODEL).
  - Cualquier fallo se traduce a AIServiceError con un mensaje legible; en los
    logs nunca se escribe la clave ni el prompt.

Funciones públicas:
  ai_is_configured()                               → bool
  generate_text(prompt, system_instruction=None)   → str   (lanza AIServiceError)
"""

import asyncio
import logging
import sys

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

_TIMEOUT = httpx.Timeout(60.0)


class AIServiceError(Exception):
    """Error al generar texto con la IA (falta de clave, error HTTP, timeout o respuesta inutilizable)."""


def ai_is_configured() -> bool:
    """Indica si está cargada la clave de Gemini."""
    return bool(settings.gemini_api_key)


def _error_message(response: httpx.Response) -> str:
    """Extrae el mensaje de error que devuelve Gemini, recortado para el log."""
    try:
        message = response.json()["error"]["message"]
    except (ValueError, KeyError, TypeError):
        message = response.text
    return str(message)[:200]


def _extract_text(data: dict) -> str:
    """Devuelve el texto del primer candidato o lanza AIServiceError si la respuesta no sirve."""
    block_reason = (data.get("promptFeedback") or {}).get("blockReason")
    if block_reason:
        logger.warning("Gemini bloqueó el pedido (blockReason=%s).", block_reason)
        raise AIServiceError("La IA rechazó el pedido.")

    candidates = data.get("candidates") or []
    if not candidates:
        logger.warning("Gemini respondió sin candidatos.")
        raise AIServiceError("La IA no devolvió ninguna respuesta.")

    parts = (candidates[0].get("content") or {}).get("parts") or []
    # Las partes "thought" son el razonamiento interno del modelo, no la respuesta
    text = "".join(part.get("text", "") for part in parts if not part.get("thought")).strip()
    if not text:
        logger.warning(
            "Gemini respondió sin texto (finishReason=%s).", candidates[0].get("finishReason")
        )
        raise AIServiceError("La IA devolvió una respuesta vacía.")
    return text


async def generate_text(
    prompt: str,
    *,
    system_instruction: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> str:
    """Pide a Gemini que responda `prompt` y devuelve el texto generado; lanza AIServiceError si falla."""
    if not ai_is_configured():
        raise AIServiceError("GEMINI_API_KEY no está configurada.")

    url = _GEMINI_URL.format(model=settings.gemini_model)
    headers = {"x-goog-api-key": settings.gemini_api_key}
    payload: dict = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        # Sin maxOutputTokens: en los modelos con razonamiento esos tokens cuentan contra
        # el límite y uno bajo deja la respuesta vacía. El largo lo fija la instrucción.
        "generationConfig": {"temperature": 0.3},
    }
    if system_instruction:
        payload["system_instruction"] = {"parts": [{"text": system_instruction}]}

    async def _post(c: httpx.AsyncClient) -> str:
        """Hace la petición a Gemini y traduce cualquier error a AIServiceError."""
        try:
            response = await c.post(url, json=payload, headers=headers, timeout=_TIMEOUT)
            response.raise_for_status()
            data = response.json()
        except httpx.HTTPStatusError as exc:
            # 400 → pedido inválido o clave mal formada; 403 → clave sin permiso; 429 → cuota agotada
            logger.warning(
                "Gemini respondió HTTP %s: %s", exc.response.status_code, _error_message(exc.response)
            )
            raise AIServiceError(f"La IA respondió con un error (HTTP {exc.response.status_code}).") from None
        except httpx.TimeoutException:
            logger.warning("Timeout al consultar Gemini.")
            raise AIServiceError("La IA tardó demasiado en responder.") from None
        except httpx.RequestError as exc:
            logger.warning("Error de red al consultar Gemini: %s", type(exc).__name__)
            raise AIServiceError("No se pudo conectar con la IA.") from None
        except ValueError:
            logger.warning("Gemini devolvió una respuesta que no es JSON.")
            raise AIServiceError("La IA devolvió una respuesta inválida.") from None

        if not isinstance(data, dict):
            logger.warning("Formato inesperado en la respuesta de Gemini.")
            raise AIServiceError("La IA devolvió una respuesta inválida.")
        return _extract_text(data)

    if client is not None:
        return await _post(client)

    async with httpx.AsyncClient() as c:
        return await _post(c)


# ── Prueba rápida en consola ──────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    async def _demo() -> None:
        """Manda a Gemini el texto pasado por argumento e imprime la respuesta."""
        if len(sys.argv) < 2:
            print('Uso:\n  python -m app.services.ai "<TEXTO>"')
            sys.exit(1)

        print(f"Consultando a {settings.gemini_model} ...")
        try:
            text = await generate_text(sys.argv[1])
        except AIServiceError as exc:
            print(f"✗ Falló la consulta: {exc}")
            sys.exit(1)
        print("✓ Respuesta:\n")
        print(text)

    asyncio.run(_demo())
