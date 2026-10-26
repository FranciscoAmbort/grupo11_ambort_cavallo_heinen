# Propuesta grupo11_ambort_cavallo_heinen
# Monitor de Precios y Alertas Acciones Argentinas/ Producto E-commerce

Es una aplicación web enfocada en el mercado argentino que rastrea y monitorea automáticamente los precios de productos o cotizaciones en diferentes sitios web.

Scraping continuo de precios: Consulta periódicamente las fuentes de datos para registrar el valor actualizado de cada ítem.

Alertas por variación porcentual: Notifica automáticamente al usuario cada vez que detecta un cambio de precio del 1% o más (suba o baja).

Alertas personalizadas por umbral: Permite al usuario definir un precio objetivo o precio mínimo deseado; si el valor cae hasta ese punto, el sistema dispara un aviso inmediato (por ejemplo, vía Telegram o correo).

Si se elige precios de E-commerce: 
Historial visual: Muestra la evolución temporal del precio para que el usuario identifique tendencias o promociones reales.

## Variables de entorno

La app las lee en `app/config.py` (pydantic-settings) desde el entorno o desde un archivo `.env`. En local, copiar `.env.example` a `.env` y completarlo; en producción (Render) se cargan en el panel del servicio. Ningún secreto se versiona.

| Variable | Obligatoria | Por defecto | Descripción |
|---|---|---|---|
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` | No | `postgres` / `postgres` / `monitor` | Credenciales del contenedor PostgreSQL. Solo las usa `docker-compose.yml`. |
| `DATABASE_URL` | Sí, fuera de Docker Compose | — | URL de PostgreSQL con driver asyncpg (`postgresql+asyncpg://...`). En Compose se arma sola apuntando al servicio `db`. |
| `SECRET_KEY` | Sí | — | Clave para firmar los JWT de sesión. Generar una con `python -c "import secrets; print(secrets.token_urlsafe(48))"`. |
| `GOOGLE_CLIENT_ID` | Sí, para iniciar sesión | vacío | Client ID de Google OAuth. Sin él, la pantalla de login avisa que no está configurado. |
| `TELEGRAM_BOT_TOKEN` | No | vacío | Token del bot de Telegram para las alertas. Sin él, las alertas salen solo por correo. |
| `BREVO_API_KEY` | No | vacío | Clave de la API de Brevo para el correo de respaldo. Sin ella no se envían correos. |
| `EMAIL_FROM` | No | vacío | Dirección remitente verificada en Brevo. |
| `WORKER_INTERVAL_SECONDS` | No | `60` | Segundos entre ciclos del worker de monitoreo. `docker-compose.yml` no la pasa, así que en local queda en 60. |
| `GEMINI_API_KEY` | No | vacío | Clave de Gemini para el resumen semanal con IA. Sin ella el resumen muestra solo las estadísticas. |
| `GEMINI_MODEL` | No | `gemini-3.5-flash-lite` | Modelo de Gemini. Usar un nombre fijo, no un alias `-latest`. |
