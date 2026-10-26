# AGENTS.md: Monitor de Precios y Alertas del Mercado Argentino

Guía para agentes que trabajan en este repositorio. El código es la fuente de verdad: si algo de este documento no coincide con él, gana el código y hay que corregir el documento.

## 1. Visión del proyecto y alcance

### 1.1 Propósito general

Aplicación web para el mercado financiero argentino que hace seguimiento, monitoreo y alerta automática sobre cotizaciones de activos: acciones locales de BYMA, bonos soberanos y CEDEARs.

El sistema resuelve un problema de I/O masivo: un proceso en segundo plano consulta los precios en internet de forma concurrente, evalúa las reglas que fijó cada usuario y avisa por Telegram (con correo como respaldo).

### 1.2 Flujo principal de usuario

1. El usuario inicia sesión con Google OAuth (no hay registro con contraseña).
2. Primer ingreso (onboarding en 3 pasos): el usuario elige del catálogo, o presiona "Omitir" en cada paso:
   1. Acciones locales (BYMA).
   2. Bonos soberanos.
   3. CEDEARs.

   Al finalizar, los activos elegidos quedan guardados en su cartera y se marca `onboarding_completed`.
3. Después puede agregar o quitar activos de la cartera. **Al quitar un activo se borran también todas sus alertas sobre ese activo** (activas y pausadas); el historial de disparos se conserva.
4. Para cada activo puede definir alertas:
   - Variación porcentual: avisa si el precio sube o baja al menos X % respecto de un precio de referencia.
   - Precio mínimo (stop loss): avisa si el precio cae a $X o menos.
   - Precio máximo (take profit): avisa si el precio llega a $X o más.
5. En su perfil vincula el chat de Telegram para recibir los avisos (con un botón para enviar un mensaje de prueba).
6. En el panel elige un activo de su cartera y ve un gráfico interactivo con la evolución de su cotización.
7. Puede generar un resumen semanal de su cartera redactado por IA (sección 6).

### 1.3 Alcance técnico

- **Monitoreo I/O-bound:** acciones y CEDEARs se consultan en el endpoint público de Yahoo Finance `/v8/finance/chart/{ticker}` (series intradiarias de velas). Los bonos soberanos y el estado del mercado (`rueda.abierta`) salen de la API pública de CompararFondos (`https://compararfondos.com.ar/api/bonos`). Todas las llamadas son asíncronas y concurrentes.
- **Procesamiento en background:** un worker periódico consulta precios, guarda el histórico en PostgreSQL y evalúa las alertas sin bloquear la API web.
- **Notificación externa:** despacho asíncrono a la API de Telegram, con correo por Brevo como respaldo.

### 1.4 Fuera de alcance (no implementar)

- Ejecución de órdenes de compra o venta, o integración con brokers reales.
- Dinero ficticio, billeteras o balances de inversión.
- WebSockets de alta frecuencia: el monitoreo es por polling periódico.
- Registro tradicional con usuario y contraseña (todo pasa por Google OAuth).
- Scraping con navegadores (Selenium, Playwright) o APIs pagas de brokers. Las consultas de precios se limitan a los endpoints REST de Yahoo Finance y CompararFondos.

## 2. Stack tecnológico

### 2.1 Backend y runtime

- **Python 3.11** (imagen `python:3.11-slim`).
- **FastAPI** con endpoints `async def`, validación con Pydantic y documentación en `/docs`.
- **Uvicorn** como servidor ASGI.
- **httpx** con `httpx.AsyncClient` para toda llamada HTTP externa. Prohibido usar `requests`.
- **asyncio** de la biblioteca estándar: `asyncio.gather` para llamadas simultáneas y `asyncio.Semaphore` para limitar la concurrencia.

### 2.2 Base de datos y persistencia

- **PostgreSQL 15** (`postgres:15-alpine` en Docker).
- **SQLAlchemy 2.0** en modo asíncrono, con el driver **asyncpg**.
- **Alembic** para las migraciones (`alembic/versions/0001` a `0005`). Se aplican solas al arrancar el contenedor (`alembic upgrade head`), tanto en Docker Compose como en el `Dockerfile` que usa producción.

### 2.3 Background y notificaciones

- **Worker:** corrutina `run_checker_loop` lanzada con `asyncio.create_task` en el `lifespan` de FastAPI (`app/main.py`), con pausas de `asyncio.sleep(WORKER_INTERVAL_SECONDS)`.
- **Telegram:** llamadas HTTP directas con httpx a `https://api.telegram.org/bot<TOKEN>/sendMessage`, con `parse_mode="HTML"`.
- **Correo de respaldo:** API HTTP de Brevo (`https://api.brevo.com/v3/smtp/email`), usada si el usuario no vinculó Telegram o si el envío por Telegram falla.

### 2.4 Autenticación y seguridad

- **Proveedor de identidad:** Google Identity Services en el navegador.
- **Verificación del `id_token`:** `app/api/auth.py` lo valida contra `https://oauth2.googleapis.com/tokeninfo` con httpx (no se usa `google-auth`). Exige `sub`, `email` y `email_verified`. El `aud` se compara con `GOOGLE_CLIENT_ID` solo si está definido; si está vacío, la app loguea un warning al arrancar.
- **Sesión interna:** JWT firmado con HS256 (PyJWT), con `sub` = id del usuario y vigencia de `ACCESS_TOKEN_EXPIRE_MINUTES` (24 h por defecto).
- **Transporte de la sesión:** cookie `access_token` con `HttpOnly` y `SameSite=Lax`. El atributo `Secure` depende de `COOKIE_SECURE` (`false` por defecto para que funcione en `http://localhost`; `true` en producción con HTTPS). El logout borra la cookie con los mismos atributos. Como alternativa para probar desde `/docs`, `app/api/deps.py` acepta también `Authorization: Bearer <token>`.

### 2.5 Frontend

- Páginas HTML estáticas servidas por FastAPI desde `app/static/` (login, onboarding, dashboard, alertas y ajustes), con JavaScript nativo y Fetch API.
- **Tailwind CSS** por CDN, con la configuración de colores y tipografías en `theme.js`; estilos propios en `styles.css`.
- **Chart.js 4** por CDN para el gráfico de cotizaciones.

### 2.6 Infraestructura y despliegue

- **Entorno local:** Docker y Docker Compose con dos servicios, `app` y `db`.
- **Secretos:** variables de entorno y archivo `.env` leídos con `pydantic-settings`. El `.env` nunca se versiona; `.env.example` es la plantilla.
- **Producción:** Render, desplegado desde `main` con el `Dockerfile`, que aplica las migraciones al arrancar. Ningún cambio puede requerir pasos manuales en el servidor, y los valores por defecto tienen que dejar la app funcionando.

## 3. Estructura del proyecto

### 3.1 Árbol de archivos

```text
.
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
├── alembic.ini
├── .env.example
├── .gitignore
├── README.md
├── AGENTS.md
├── CLAUDE.md                  # Solo importa este archivo (@AGENTS.md)
├── alembic/
│   ├── env.py
│   ├── script.py.mako
│   └── versions/              # 0001 a 0005
├── tests/
│   ├── test_ai.py
│   ├── test_alert_evaluation.py
│   ├── test_assets_api.py
│   ├── test_auth_cookie.py
│   ├── test_market_series.py
│   └── test_weekly_summary.py
└── app/
    ├── main.py                # App FastAPI, lifespan (worker) y montaje del frontend
    ├── config.py              # Settings (pydantic-settings)
    ├── database.py            # Engine asíncrono, async_session_maker y get_session
    ├── models.py
    ├── schemas.py
    ├── seed.py                # Catálogo inicial de activos
    ├── dev_fake_week.py       # Datos falsos para probar el resumen semanal (solo desarrollo)
    ├── api/
    │   ├── deps.py            # JWT y get_current_user
    │   ├── auth.py            # /api/auth
    │   ├── assets.py          # /api/assets
    │   ├── alerts.py          # /api/alerts
    │   └── summary.py         # /api/summary
    ├── services/
    │   ├── market.py          # URL, cabeceras y timeout de Yahoo; bonos y estado del mercado (CompararFondos)
    │   ├── market_series.py   # Series intradiarias de velas (lo que usa el worker)
    │   ├── telegram.py
    │   ├── email.py           # Correo de respaldo (Brevo)
    │   ├── ai.py              # Cliente de Gemini
    │   └── weekly_summary.py
    ├── worker/
    │   ├── checker.py         # Bucle del worker y ventana de gracia
    │   └── evaluation.py      # Funciones puras de velas y evaluación de alertas
    └── static/
        ├── index.html         # Login
        ├── login.js
        ├── theme.js
        ├── styles.css
        ├── onboarding/
        ├── dashboard/
        ├── alerts/
        └── settings/
```

`app/services/market.py` conserva `get_ticker_price` y `get_multiple_prices` (precio puntual), pero el worker usa `get_multiple_series` de `market_series.py`, que reutiliza la URL, las cabeceras, el timeout y la consulta de bonos de `market.py`.

### 3.2 Endpoints

| Método y ruta | Descripción |
|---|---|
| `POST /api/auth/google` | Login con el `id_token` de Google; crea el usuario si no existe y setea la cookie |
| `GET /api/auth/me` | Usuario autenticado |
| `PATCH /api/auth/telegram` | Guarda o desvincula el `telegram_chat_id` |
| `POST /api/auth/telegram/test` | Envía un mensaje de prueba por Telegram |
| `POST /api/auth/logout` | Borra la cookie de sesión |
| `GET /api/assets?asset_type=` | Catálogo de activos activos con su último precio |
| `GET /api/assets/me` | Cartera del usuario |
| `POST /api/assets/me` | Agrega activos (idempotente) y completa el onboarding |
| `DELETE /api/assets/me/{asset_id}` | Quita el activo y borra las alertas del usuario sobre él, en la misma transacción (idempotente, 204) |
| `GET /api/assets/{ticker}/history?limit=` | Últimas cotizaciones para el gráfico |
| `GET /api/alerts` | Alertas del usuario (activas e inactivas) |
| `POST /api/alerts` | Crea una alerta (`ABOVE`, `BELOW` o `PCT_CHANGE`) |
| `PATCH /api/alerts/{id}` | Pausa, reactiva o cambia la condición |
| `DELETE /api/alerts/{id}` | Borra una alerta |
| `GET` y `POST /api/summary/weekly?week_offset=` | Resumen semanal: `0` = semana actual, `1` = semana pasada (sección 6) |
| `GET /api/config` | Configuración pública para el navegador (`google_client_id`) |
| `GET /api/health` | Chequeo de la app y de PostgreSQL |

La API expone los tipos de alerta como `ABOVE`, `BELOW` y `PCT_CHANGE`; en la base se guardan como `price_max`, `price_min` y `percentage`.

### 3.3 Comandos

```bash
# Primera vez: crear el .env a partir de la plantilla y completar SECRET_KEY
cp .env.example .env

# Levantar la app y la base (aplica las migraciones y arranca uvicorn con --reload)
docker compose up --build

# Cargar el catálogo de activos
docker compose exec app python -m app.seed

# Tests (no necesitan la base; con los tokens vacíos no sale ningún aviso real)
docker compose run --rm --no-deps -e TELEGRAM_BOT_TOKEN= -e BREVO_API_KEY= -e GEMINI_API_KEY= app pytest

# Datos falsos para probar el resumen semanal (solo desarrollo, base local)
docker compose exec app python -m app.dev_fake_week <email> --yes
```

La documentación interactiva queda en `http://localhost:8000/docs`.

## 4. Esquema de datos (PostgreSQL)

### 4.1 Principios de diseño

- Esquema relacional normalizado, administrado con SQLAlchemy 2.0 asíncrono.
- Todos los `DateTime` son `timezone=True` y se guardan en UTC.
- Índices explícitos en los campos de búsqueda frecuente y en las claves foráneas.
- Todo cambio de esquema va en una migración nueva de Alembic, que tiene que poder aplicarse sola al arrancar en producción.

### 4.2 Tablas

#### `users`

| Columna | Tipo | Restricciones | Descripción |
|---|---|---|---|
| `id` | `Integer` | PK, autoincremental | Identificador interno |
| `email` | `String(255)` | Único, índice | Correo provisto por Google |
| `google_id` | `String(255)` | Único, índice | `sub` emitido por Google |
| `name` | `String(255)` | Nullable | Nombre visible |
| `telegram_chat_id` | `String(64)` | Nullable | Chat de Telegram para los avisos |
| `onboarding_completed` | `Boolean` | Default `false` | Controla el onboarding de 3 pasos |
| `created_at` | `DateTime(tz)` | Default `now()` | Fecha de registro |

#### `assets`

| Columna | Tipo | Restricciones | Descripción |
|---|---|---|---|
| `id` | `Integer` | PK, autoincremental | Identificador del activo |
| `ticker` | `String(20)` | Único, índice | Símbolo de consulta: `GGAL.BA` y `AAPL.BA` en Yahoo; `AL30`, `GD30`, etc. en CompararFondos |
| `name` | `String(150)` | | Nombre descriptivo |
| `asset_type` | `String(20)` | Índice | `stock`, `bond` o `cedear` |
| `currency` | `String(10)` | Default `ARS` | Moneda del precio (`ARS` o `USD`; los bonos cotizan en USD) |
| `is_active` | `Boolean` | Default `true` | Si el activo está operativo en el sistema |

#### `user_assets` (cartera)

| Columna | Tipo | Restricciones | Descripción |
|---|---|---|---|
| `user_id` | `Integer` | FK `users.id` `ON DELETE CASCADE`, PK compuesta | Usuario |
| `asset_id` | `Integer` | FK `assets.id` `ON DELETE CASCADE`, PK compuesta | Activo seguido |
| `added_at` | `DateTime(tz)` | Default `now()` | Fecha de incorporación |

#### `alerts`

| Columna | Tipo | Restricciones | Descripción |
|---|---|---|---|
| `id` | `Integer` | PK, autoincremental | Identificador de la alerta |
| `user_id` | `Integer` | FK `users.id` `ON DELETE CASCADE`, índice | Dueño |
| `asset_id` | `Integer` | FK `assets.id` `ON DELETE CASCADE`, índice | Activo |
| `alert_type` | `String(20)` | | `percentage`, `price_min` o `price_max` |
| `threshold_value` | `Numeric(12, 4)` | | Porcentaje o precio umbral |
| `is_active` | `Boolean` | Default `true` | Activa o pausada |
| `last_triggered_at` | `DateTime(tz)` | Nullable | Hora de la vela del último disparo |
| `last_notified_price` | `Numeric(12, 4)` | Nullable | Precio de referencia de las alertas porcentuales |
| `created_at` | `DateTime(tz)` | Default `now()` | Piso temporal de evaluación (ver 5.4) |

#### `price_history`

| Columna | Tipo | Restricciones | Descripción |
|---|---|---|---|
| `id` | `BigInteger` | PK, autoincremental | Identificador de la lectura |
| `asset_id` | `Integer` | FK `assets.id` `ON DELETE CASCADE` | Activo |
| `price` | `Numeric(12, 4)` | | Cierre de la vela (o precio puntual en bonos) |
| `timestamp` | `DateTime(tz)` | Default `now()` | Inicio de la vela |

Restricción única `uq_price_history_asset_timestamp` sobre `(asset_id, timestamp)` (migración 0005). Reemplaza al índice simple anterior y sirve también como índice para las consultas del gráfico y del resumen semanal.

#### `alert_events` (historial de disparos, migración 0004)

| Columna | Tipo | Restricciones | Descripción |
|---|---|---|---|
| `id` | `BigInteger` | PK, autoincremental | Identificador del evento |
| `alert_id` | `Integer` | FK `alerts.id` `ON DELETE SET NULL`, índice, nullable | Alerta que disparó |
| `user_id` | `Integer` | FK `users.id` `ON DELETE CASCADE` | Usuario |
| `asset_id` | `Integer` | FK `assets.id` `ON DELETE CASCADE` | Activo |
| `alert_type` | `String(20)` | | Copia del tipo al momento del disparo |
| `threshold_value` | `Numeric(12, 4)` | | Copia del umbral |
| `price` | `Numeric(12, 4)` | | Precio alcanzado |
| `change_pct` | `Numeric(9, 4)` | Nullable | Variación con signo, solo en las porcentuales |
| `notified` | `Boolean` | | Si el aviso llegó por algún canal |
| `triggered_at` | `DateTime(tz)` | Default `now()` | Hora de la vela del disparo |

Índice `ix_alert_events_user_triggered` sobre `(user_id, triggered_at)`. Gracias al `ON DELETE SET NULL`, el historial sobrevive aunque se borre la alerta (por ejemplo, al quitar el activo de la cartera).

#### `weekly_summaries` (caché del resumen semanal, migración 0004)

| Columna | Tipo | Restricciones | Descripción |
|---|---|---|---|
| `id` | `Integer` | PK, autoincremental | Identificador |
| `user_id` | `Integer` | FK `users.id` `ON DELETE CASCADE` | Usuario |
| `week_start` | `Date` | | Lunes de la semana en hora argentina |
| `stats` | `JSONB` | | Estadísticas que se le mandaron a la IA |
| `content` | `Text` | | Texto generado por la IA |
| `model` | `String(100)` | | Modelo usado |
| `created_at` | `DateTime(tz)` | Default `now()` | Fecha de generación |

Restricción única `uq_weekly_summaries_user_week` sobre `(user_id, week_start)`.

## 5. Reglas de arquitectura (cumplimiento obligatorio)

### 5.1 Ingesta por activo único, nunca por usuario

El worker jamás itera usuario por usuario para consultar precios. Cada ciclo de `check_market_and_evaluate_alerts` (`app/worker/checker.py`) sigue este orden:

0. **Estado del mercado:** una sola petición a CompararFondos (`fetch_bonds_data`), que se reutiliza después para los precios de los bonos. Si `rueda.abierta` es `false`, o no se puede saber, se saltea el ciclo entero: no se consultan precios, no se guarda nada y no se evalúan alertas.
1. **Fase 1 (deduplicación):** conjunto único (`DISTINCT`) de tickers que están en alguna cartera o tienen alertas activas.
2. **Fase 2 (ingesta concurrente):** una sola consulta por ticker único con `get_multiple_series`: serie de velas de Yahoo para acciones y CEDEARs, y una lectura puntual de CompararFondos para los bonos.
3. **Fase 3 (persistencia y evaluación):** upsert de las velas en `price_history`, evaluación de las alertas activas sobre las velas cerradas pendientes y despacho de los avisos.

**No modificar el bloque de estado del mercado** del comienzo de `check_market_and_evaluate_alerts`: con el mercado cerrado o en estado desconocido, el ciclo no hace nada.

### 5.2 Concurrencia y control de tráfico

- Las consultas a varios activos se orquestan con `asyncio.gather`. Prohibidos los `for` secuenciales con `await` para llamadas de red externas.
- Las consultas a Yahoo se limitan con `asyncio.Semaphore(5)` (`market_series.py`).
- Toda llamada HTTP externa usa `httpx.AsyncClient` con un timeout explícito de **10 segundos como máximo**.
- **Única excepción:** las llamadas a Gemini (`app/services/ai.py`) usan un timeout de **60 segundos**, porque generar texto tarda más que consultar precios. La excepción vale solo para Gemini.
- Cada petición a Yahoo Finance lleva la cabecera `User-Agent: Mozilla/5.0 ...` (definida en `market.py`).

### 5.3 Persistencia y acceso a la base

- Toda interacción con PostgreSQL usa `AsyncSession` con asyncpg. Prohibidos la `Session` síncrona y los métodos del ORM sin `await`.
- Los endpoints obtienen la sesión con la dependencia `get_session`.
- El worker abre y cierra su propia sesión por ciclo con `async with async_session_maker() as session:`; no retiene sesiones ni conexiones entre ciclos.
- Las operaciones que tienen que ser atómicas se hacen con un solo `commit`. Por ejemplo, `DELETE /api/assets/me/{asset_id}` borra primero las alertas del usuario sobre ese activo y después la fila de `user_assets`, en la misma transacción.

### 5.4 Evaluación de alertas y prevención de spam

La evaluación es por velas (`app/worker/evaluation.py`, funciones puras). Cada alerta se evalúa sobre las velas cerradas que todavía no se evaluaron, en orden cronológico y con un máximo de un disparo por ciclo:

- **Precio mínimo (`price_min`):** dispara en la primera vela con `low <= umbral`; el precio informado es el `low`.
- **Precio máximo (`price_max`):** dispara en la primera vela con `high >= umbral`; el precio informado es el `high`.
- **Variación porcentual (`percentage`):** compara contra `last_notified_price` (la referencia). La suba se mide como `(high - ref) / ref * 100` y la baja como `(ref - low) / ref * 100`. Dispara en la primera vela donde alguna llegue al umbral (si llegan las dos, la mayor). Si la alerta todavía no tiene referencia, se ancla al `close` de la última vela sin disparar.
- Se ignoran las velas que empiezan antes de `created_at` de la alerta.

Mecanismo contra los avisos repetidos (no hay un cooldown por tiempo):

- **Umbral fijo (`price_min`, `price_max`):** un solo disparo. Al disparar, la alerta pasa a `is_active = false`; el usuario la reactiva desde la API.
- **Porcentual:** re-anclaje continuo. Al disparar, `last_notified_price` pasa a ser el precio alcanzado y la alerta sigue activa, así que solo vuelve a avisar ante una nueva variación del umbral.
- En cada disparo se guarda `last_triggered_at` con la hora de la vela y se agrega un `AlertEvent` en la misma transacción del ciclo.

Piso temporal al crear o reactivar: `app/api/alerts.py` pone en `created_at` la hora de la última lectura guardada del activo (y, en las porcentuales, toma ese precio como referencia inicial). Así la alerta no queda bloqueada por la demora de Yahoo frente a la hora actual. Lo mismo pasa al cambiar la condición de una alerta.

### 5.5 Notificaciones y manejo de errores

- El aviso se envía por Telegram (`sendMessage`, HTML) si el usuario tiene `telegram_chat_id`. Si no lo tiene, o si Telegram falla, se intenta por correo con Brevo.
- Errores como un `telegram_chat_id` inválido o un bot bloqueado (HTTP 400 o 403), timeouts o fallas de red se capturan con `try/except`, se loguean y devuelven `False`, sin interrumpir el worker ni los avisos de otros usuarios. El resultado queda en `alert_events.notified`.
- Si falta `TELEGRAM_BOT_TOKEN`, o alguna de `BREVO_API_KEY` y `EMAIL_FROM`, ese canal simplemente no envía.

### 5.6 Secretos y configuración

- Ninguna URL de base de datos, credencial de Google, token de Telegram ni clave de API se escribe fija en el código.
- Todas las variables se leen en `app/config.py` con `pydantic-settings`, desde el entorno y el `.env` (ver sección 8).

## 6. Resumen semanal analizado por IA

### 6.1 Tablas (migración 0004)

- **`alert_events`:** `app/worker/checker.py` agrega un evento en cada disparo, de umbral fijo o porcentual, dentro de la misma transacción del ciclo.
- **`weekly_summaries`:** caché del resumen por usuario y semana. Solo se guardan los resúmenes generados con éxito por la IA.

### 6.2 Flujo

- **Semana:** se presenta **de lunes a viernes** (los días de rueda): `week_end` es el viernes y el prompt dice "Semana del lunes X al viernes Y". La ventana de datos, en cambio, va del lunes 00:00 al lunes siguiente 00:00 en hora argentina (`America/Argentina/Buenos_Aires`); la clave de la caché sigue siendo `week_start` (el lunes).
- `app/services/weekly_summary.py` calcula las estadísticas con 3 consultas agregadas y arma el prompt con funciones puras, testeadas en `tests/test_weekly_summary.py`. A la IA se le mandan solo tickers, nombres de activos y números, nunca datos del usuario. La instrucción de sistema le pide no mencionar sábados ni domingos.
- `app/services/ai.py` llama a la API REST de Gemini con httpx. La clave viaja en el header `x-goog-api-key`, nunca en la URL. Timeout de 60 segundos (excepción a la regla 5.2).
- **API:** `GET` y `POST /api/summary/weekly?week_offset=0|1` (0 = semana actual, 1 = semana pasada). El `GET` devuelve el resumen guardado o 404. El `POST` regenera la semana actual solo si el guardado tiene más de 1 hora, y la pasada solo si se había generado antes de que la semana cerrara.
- Sin `GEMINI_API_KEY`, o si Gemini falla, se devuelven igual las estadísticas con `content = null` y un `error` legible.
- **Datos de prueba (solo desarrollo):** `python -m app.dev_fake_week <email> [--yes]`. Se niega a correr contra una base que no sea local.

## 7. Alertas y gráficos sobre velas intradiarias

### 7.1 Persistencia (migración 0005)

- La migración `0005` deduplica las lecturas previas de `price_history` (conserva el menor `id`), reemplaza el índice simple y crea el constraint único `uq_price_history_asset_timestamp`.
- En cada ciclo, el worker guarda el `close` de cada vela con `INSERT ... ON CONFLICT DO UPDATE`, en lotes de hasta 5000 filas. La vela en curso se actualiza solo si el precio cambió (`IS DISTINCT FROM`).

### 7.2 Ingesta y evaluación

- **`app/services/market_series.py`:** pide a Yahoo `/v8/finance/chart/{ticker}?interval=<YAHOO_INTERVAL>&range=1d` y obtiene `close`, `high` y `low` como `Decimal`. Los bonos llegan como lectura puntual de CompararFondos (`snapshot=True`, con `high = low = close`).
- **`app/worker/evaluation.py`:**
  - `candles_to_store`: velas a guardar desde la última guardada, inclusive.
  - `complete_candles`: excluye la vela en curso comparando el fin de cada vela contra `min(as_of, now)`, donde `as_of` es `meta.regularMarketTime`. Las lecturas puntuales de bonos cuentan como completas.
  - `select_candles_to_evaluate`: usa un cursor por activo que el worker guarda en memoria (`_cursors[asset_id]`, con `floor`, `evaluated_until` y `last_saved`) para evaluar cada vela cerrada una sola vez y no evaluar nunca velas anteriores al arranque del proceso.
  - `evaluate_alert`: aplica las reglas de la sección 5.4 sobre `high` y `low`, así detecta cruces de umbral aunque el precio haya vuelto dentro de la misma vela.

### 7.3 Ventana de gracia post-cierre (mitigación R1)

Yahoo Finance entrega las velas de BYMA con unos 20 minutos de demora y congela `meta.regularMarketTime` al cerrar la rueda. **Esta ventana se conserva tal cual:**

- Cuando `rueda.abierta` pasa de `true` a `false`, `run_checker_loop` abre una ventana de gracia de hasta `YAHOO_POST_CLOSE_MINUTES` minutos (`0` la desactiva).
- Durante la ventana se consulta solo Yahoo (acciones y CEDEARs, sin bonos), con la misma pausa entre pasadas.
- Si dos pasadas seguidas devuelven la misma firma (`as_of` y última vela de cada ticker), o vence el plazo, se fuerza el cierre de las velas pendientes (`session_closed=True`), se guardan y se evalúan por última vez.

### 7.4 Nota R4 (interacción con el resumen semanal)

El resumen semanal calcula el rango mínimo-máximo a partir de los cierres guardados en `price_history`, mientras que las alertas se disparan con los extremos de cada vela (`high` y `low`). Por eso un evento de `alert_events` puede informar un precio levemente por fuera del rango de cierres de la tabla semanal.

## 8. Variables de entorno

Las de la app se leen en `app/config.py`; las `POSTGRES_*` las usa solo `docker-compose.yml`. La plantilla es `.env.example`. En Docker Compose, `docker-compose.yml` pasa las variables al servicio `app` con los mismos valores por defecto (salvo `ACCESS_TOKEN_EXPIRE_MINUTES`, que no pasa y queda en su valor por defecto).

| Variable | Obligatoria | Por defecto | Descripción |
|---|---|---|---|
| `DATABASE_URL` | Sí | | URL `postgresql+asyncpg://...`. En Compose se arma sola apuntando al servicio `db` |
| `SECRET_KEY` | Sí | | Clave de firma de los JWT |
| `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB` | No | `postgres`, `postgres`, `monitor` | Credenciales del contenedor `db` (solo Compose) |
| `GOOGLE_CLIENT_ID` | No | vacío | Si está definido, se valida el `aud` del `id_token`; vacío loguea un warning al arrancar |
| `COOKIE_SECURE` | No | `false` | Atributo `Secure` de la cookie de sesión; `true` en producción con HTTPS |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | No | `1440` | Vigencia del JWT de sesión |
| `TELEGRAM_BOT_TOKEN` | No | vacío | Sin token no se envían mensajes por Telegram |
| `BREVO_API_KEY`, `EMAIL_FROM` | No | vacíos | Correo de respaldo; si falta alguno no se envían correos |
| `WORKER_INTERVAL_SECONDS` | No | `60` | Segundos entre ciclos del worker (y entre pasadas de la ventana de gracia) |
| `YAHOO_INTERVAL` | No | `1m` | Ancho de vela: `1m`, `2m`, `5m`, `15m`, `30m`, `60m` o `90m`. Vacío usa `1m` |
| `YAHOO_POST_CLOSE_MINUTES` | No | `15` | Duración máxima de la ventana de gracia; `0` la desactiva |
| `GEMINI_API_KEY` | No | vacío | Sin clave el resumen muestra solo las estadísticas |
| `GEMINI_MODEL` | No | `gemini-3.5-flash-lite` | Usar un nombre de modelo fijo, nunca un alias `-latest`. Vacío usa el valor por defecto |

Fuera de Docker, una variable numérica o booleana vacía en el `.env` (por ejemplo `YAHOO_POST_CLOSE_MINUTES=`) hace fallar el arranque: hay que dejarle un valor o borrar la línea. Las únicas que aceptan vacío y caen en su valor por defecto son `YAHOO_INTERVAL` y `GEMINI_MODEL`.
