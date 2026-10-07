\# 1\. Visión del Proyecto y Alcance (Overview)

\#\# 1.1 Propósito General  
Desarrollar una aplicación web orientada al mercado financiero argentino para el seguimiento, monitoreo y alerta automática sobre cotizaciones de activos (acciones locales de BYMA, bonos soberanos y CEDEARs).  
El sistema resuelve un problema de I/O masivo: delega en un proceso en segundo plano la consulta concurrente de precios en internet y la evaluación de reglas fijadas por los usuarios, notificando en tiempo real vía Telegram.

\#\# 1.2 Flujo Principal de Usuario  
1\. El usuario ingresa a la plataforma e inicia sesión utilizando Google OAuth (sin necesidad de registrar contraseñas).  
2\. Primer ingreso (Onboarding guiado por pasos):  
   \- Al registrarse por primera vez, la aplicación guía al usuario a través de un catálogo categorizado en 3 pasos consecutivos:  
     a. Acciones Locales (BYMA): el usuario selecciona las que desea seguir o presiona "Omitir".  
     b. Bonos Soberanos: selecciona los títulos de interés o presiona "Omitir".  
     c. CEDEARs: selecciona las empresas internacionales o presiona "Omitir".  
   \- Al finalizar (o tras omitir los pasos deseados), los activos seleccionados quedan guardados automáticamente en su cartera de seguimiento.

3\. El usuario puede posteriormente agregar o quitar activos de su lista de seguimiento.  
4\. Para cada activo seguido, el usuario puede definir reglas de alerta personalizadas:  
   \- Alerta por variación porcentual (ej. notificar si sube o baja más del X% respecto al último cierre/medición).  
   \- Alerta por precio mínimo / Stop Loss (ej. notificar si el precio cae por debajo de \$X).  
   \- Alerta por precio máximo / Take Profit (ej. notificar si el precio supera los \$X).  
5\. El usuario vincula su identificador o chat de Telegram en su perfil para recibir las notificaciones push.  
6\. El usuario dispone de un panel visual donde puede seleccionar cualquier activo de su cartera y consultar un gráfico interactivo con la evolución histórica de su cotización.

\#\# 1.3 Alcance Técnico (Enfoque en los contenidos de la materia)  
\- \*\*Monitoreo I/O-Bound:\*\* Ingesta de cotizaciones desde los endpoints HTTP públicos de \*\*Yahoo Finance\*\* (\`/v8/finance/chart/{ticker}\`) mediante llamadas asíncronas concurrentes (no bloqueantes).  
\- \*\*Procesamiento en Background:\*\* Worker periódico que consulta precios, persiste históricos en PostgreSQL y evalúa las reglas de los usuarios sin congelar la API web.  
\- \*\*Notificación Externa:\*\* Despacho asíncrono de alertas hacia la API de Telegram.

\#\# 1.4 Fuera de Alcance (Cosas que el agente NO debe implementar)  
\- Ejecución de órdenes de compra/venta o integración con brokers reales.  
\- Manejo de dinero ficticio, billeteras o balances de inversión.  
\- Soporte para WebSockets de alta frecuencia (el monitoreo es por polling periódico en intervalos de minutos).  
\- Registro tradicional con usuario y contraseña (se delega enteramente en Google OAuth).  
\- Scraping con navegadores pesados (Selenium, Playwright) o integración con APIs pagas de brokers (las consultas se limitan a los endpoints REST de Yahoo Finance).

\# 2\. Stack Tecnológico Mandatorio

\#\# 2.1 Backend y Runtime  
\- \*\*Lenguaje & Versión:\*\* Python 3.11+ (aprovecha mejoras de rendimiento en el intérprete y soporte nativo de \`asyncio\`).  
\- \*\*Framework Web:\*\* FastAPI (endpoints asíncronos nativos con \`async def\`, validación automática mediante Pydantic y documentación OpenAPI/Swagger interactiva).  
\- \*\*Servidor ASGI:\*\* Uvicorn (gestor de procesos asíncronos ligero y de alto rendimiento).  
\- \*\*Cliente HTTP (Scraping/Ingesta):\*\* \`httpx\` con \`httpx.AsyncClient\` (soporte completo de peticiones asíncronas no bloqueantes y reutilización de conexiones; queda prohibido el uso de \`requests\`).  
\- \*\*Concurrencia & Control de Flujo:\*\* Biblioteca estándar \`asyncio\` (\`asyncio.gather\` para llamadas en simultáneo y \`asyncio.Semaphore\` para limitar tasa de concurrencia).

\#\# 2.2 Base de Datos y Persistencia  
\- \*\*Motor de Base de Datos:\*\* PostgreSQL 15+ (ejecutado dentro del entorno de contenedores).  
\- \*\*ORM / Capa de Datos:\*\* SQLAlchemy 2.0 (modo asíncrono).  
\- \*\*Driver de Conexión:\*\* \`asyncpg\` (driver nativo de PostgreSQL para Python, extremadamente rápido y 100% asíncrono no bloqueante).  
\- \*\*Gestión de Migraciones:\*\* Alembic (para versionado y control de cambios en el esquema de la base de datos).

\#\# 2.3 Procesamiento en Background y Notificaciones  
\- \*\*Worker de Monitoreo:\*\* Corrutina nativa en segundo plano orquestada con \`asyncio.create\_task\` dentro del ciclo de vida de la aplicación (\`lifespan\` de FastAPI), con pausas controladas mediante \`asyncio.sleep\`.  
\- \*\*Canal de Alertas:\*\* Telegram Bot API (consumida de forma asíncrona mediante llamadas HTTP directas con \`httpx\` hacia \`https\://api.telegram.org/bot\<TOKEN\>/sendMessage\`).

\#\# 2.4 Autenticación y Seguridad  
\- \*\*Proveedor de Identidad:\*\* Google OAuth 2.0 (Google Identity Services / Token Verification).  
\- \*\*Librería de Verificación:\*\* \`google-auth\` (verificación asíncrona/ligera del \`id\_token\` emitido por Google) o \`authlib\`.  
\- \*\*Manejo de Sesión Interna:\*\* Emisión de JWT (JSON Web Tokens) firmados con algoritmo HS256 (\`PyJWT\`).  
\- \*\*Transporte de Sesión:\*\* Almacenado en \*\*Cookie \`HttpOnly\`\*\* con atributo \`SameSite=Lax\` para que el navegador lo envíe automáticamente en cada petición, protegiendo la sesión contra robo por XSS (soporte secundario opcional para encabezado \`Authorization: Bearer \<token\>\` para pruebas directas en \`/docs\`).

\#\# 2.5 Frontend / Interfaz de Usuario  
\- \*\*Arquitectura:\*\* Single Page Application (SPA) ligera o renderizado liviano.  
\- \*\*Tecnologías UI:\*\* HTML5 semántico, Tailwind CSS (o Bootstrap 5 para agilidad de prototipado) y JavaScript nativo (Fetch API) o React/Vite.  
\- \*\*Gráficos Interactivos:\*\* Chart.js (ligero, intuitivo de integrar y suficiente para gráficos de cotización temporal con escala de fechas).

\#\# 2.6 Infraestructura y Despliegue  
\- \*\*Entorno Local:\*\* Docker & Docker Compose (orquestación multi-contenedor para servicio web \`app\` y base de datos \`db\`).  
\- \*\*Gestión de Secretos:\*\* Variables de entorno mediante archivo \`.env\` gestionado con \`pydantic-settings\` (nunca versionar secretos en el repositorio).  
\- \*\*Despliegue Cloud (PaaS):\*\* Render / Fly.io / Railway (servicios gratuitos/accesibles para desplegar contenedores y base de datos PostgreSQL gestionada).

\# 3\. Estructura de Directorios y Arquitectura del Proyecto

\#\# 3.1 Árbol de Archivos  
.  
├── docker-compose.yml  
├── Dockerfile  
├── requirements.txt  
├── .env.example  
├── .gitignore  
├── README.md  
├── AGENTS.md  
└── app/  
    ├── \_\_init\_\_.py  
    ├── main.py  
    ├── config.py  
    ├── database.py  
    ├── models.py  
    ├── schemas.py  
    │  
    ├── api/  
    │   ├── \_\_init\_\_.py  
    │   ├── deps.py  
    │   ├── auth.py  
    │   ├── assets.py  
    │   └── alerts.py  
    │  
    ├── services/  
    │   ├── \_\_init\_\_.py  
    │   ├── market.py  
    │   └── telegram.py  
    │  
    ├── worker/  
    │   ├── \_\_init\_\_.py  
    │   └── checker.py  
    │  
    └── static/  
        ├── index.html  
        ├── styles.css  
        └── app.js

\# 4\. Esquema de Datos y Modelos (PostgreSQL)

\#\# 4.1 Principios de Diseño  
\- Esquema relacional normalizado sobre \*\*PostgreSQL 15+\*\* administrado con \*\*SQLAlchemy 2.0 (modo asíncrono)\*\*.  
\- Todos los campos temporales (\`DateTime\`) deben persistirse en UTC (\`timezone=True\`).  
\- Índices explícitos en campos de búsqueda frecuente y claves foráneas para optimizar el rendimiento en consultas concurrentes.  
\- Regla de negocio en alertas: soporte de marca temporal de disparo (\`last\_triggered\_at\`) para evitar envíos repetitivos (mecanismo de enfriamiento / cooldown).

\---

\#\# 4.2 Definición de Tablas y Atributos

\#\#\# 1\. \`users\` (Usuarios)  
Registra la identidad validada vía Google OAuth y los datos de destino de alertas.

| Columna | Tipo | Restricciones / Índices | Descripción |  
|---|---|---|---|  
| \`id\` | \`Integer\` | Primary Key, Autoincrement | Identificador único interno |  
| \`email\` | \`String(255)\` | Unique, Not Null, Index | Correo electrónico provisto por Google |  
| \`google\_id\` | \`String(255)\` | Unique, Not Null, Index | Identificador único (\`sub\`) emitido por Google |  
| \`name\` | \`String(255)\` | Nullable | Nombre visible del usuario |  
| \`telegram\_chat\_id\` | \`String(64)\` | Nullable | ID del chat de Telegram para notificaciones push |  
| \`onboarding\_completed\` | \`Boolean\` | Not Null, Default: \`False\` | Bandera para controlar el flujo de 3 pasos inicial |  
| \`created\_at\` | \`DateTime(timezone=True)\` | Not Null, Default: UTC now | Fecha de registro en el sistema |

\---

\#\#\# 2\. \`assets\` (Catálogo de Instrumentos Financieros)  
Contiene los activos disponibles para seguimiento categorizados por tipo.

| Columna | Tipo | Restricciones / Índices | Descripción |  
|---|---|---|---|  
| \`id\` | \`Integer\` | Primary Key, Autoincrement | Identificador único del activo |  
| \`ticker\` | \`String(20)\` | Unique, Not Null, Index | Símbolo exacto de consulta (ej. \`GGAL.BA\`, \`AL30.BA\`, \`AAPL.BA\`) |  
| \`name\` | \`String(150)\` | Not Null | Nombre descriptivo del instrumento |  
| \`asset\_type\` | \`String(20)\` | Not Null, Index | Categoría: \`'stock'\` (Acción), \`'bond'\` (Bono), \`'cedear'\` (CEDEAR) |  
| \`is\_active\` | \`Boolean\` | Not Null, Default: \`True\` | Estado operativo del activo en el sistema |

\---

\#\#\# 3\. \`user\_assets\` (Cartera de Seguimiento)  
Tabla intermedia que modela la relación Muchos a Muchos entre usuarios y activos seguidos.

| Columna | Tipo | Restricciones / Índices | Descripción |  
|---|---|---|---|  
| \`user\_id\` | \`Integer\` | ForeignKey(\`users.id\`, ondelete="CASCADE"), PK compuesta | Identificador del usuario |  
| \`asset\_id\` | \`Integer\` | ForeignKey(\`assets.id\`, ondelete="CASCADE"), PK compuesta | Identificador del activo seguido |  
| \`added\_at\` | \`DateTime(timezone=True)\` | Not Null, Default: UTC now | Fecha de incorporación a la cartera |

\*Restricción:\* Clave primaria compuesta \`(user\_id, asset\_id)\` para garantizar unicidad.

\---

\#\#\# 4\. \`alerts\` (Reglas de Alerta Configuradas)  
Reglas de notificación asociadas a un activo y a un usuario.

| Columna | Tipo | Restricciones / Índices | Descripción |  
|---|---|---|---|  
| \`id\` | \`Integer\` | Primary Key, Autoincrement | Identificador único de la alerta |  
| \`user\_id\` | \`Integer\` | ForeignKey(\`users.id\`, ondelete="CASCADE"), Index, Not Null | Usuario dueño de la alerta |  
| \`asset\_id\` | \`Integer\` | ForeignKey(\`assets.id\`, ondelete="CASCADE"), Index, Not Null | Activo sobre el cual aplica la regla |  
| \`alert\_type\` | \`String(20)\` | Not Null | Tipo de condición: \`'percentage'\`, \`'price\_min'\`, \`'price\_max'\` |  
| \`threshold\_value\` | \`Numeric(12, 4)\` | Not Null | Valor umbral (% de variación o precio objetivo/stop) |  
| \`is\_active\` | \`Boolean\` | Not Null, Default: \`True\` | Estado de la alerta |  
| \`last\_triggered\_at\` | \`DateTime(timezone=True)\` | Nullable | Marca temporal del último disparo (para control de spam/cooldown) |  
| \`created\_at\` | \`DateTime(timezone=True)\` | Not Null, Default: UTC now | Fecha de creación de la regla |

\---

\#\#\# 5\. \`price\_history\` (Registro Histórico de Cotizaciones)  
Almacena las mediciones periódicas recolectadas por el worker para cálculos de variación y generación de gráficos.

| Columna | Tipo | Restricciones / Índices | Descripción |  
|---|---|---|---|  
| \`id\` | \`BigInteger\` | Primary Key, Autoincrement | Identificador secuencial de lectura |  
| \`asset\_id\` | \`Integer\` | ForeignKey(\`assets.id\`, ondelete="CASCADE"), Not Null | Activo cotizado |  
| \`price\` | \`Numeric(12, 4)\` | Not Null | Precio registrado en la medición |  
| \`timestamp\` | \`DateTime(timezone=True)\` | Not Null, Default: UTC now | Momento exacto de la captura |

\*Índice compuesto mandatorio:\* \`Index("ix\_price\_history\_asset\_timestamp", "asset\_id", "timestamp")\` para garantizar respuestas inmediatas en las consultas temporales del gráfico.

\# 5\. Reglas de Oro de Arquitectura y Gotchas (Directivas para Agentes)

Estas reglas son de cumplimiento estricto. Cualquier implementación generada por los agentes debe respetar estas directrices de diseño y rendimiento.

\---

\#\# 5.1 Desacoplamiento e Ingesta Eficiente de Cotizaciones (Anti-Duplicación)  
1\. \*\*Consulta por Activo Único (Nunca por Usuario):\*\*  
   \- El worker periódico \*\*jamás\*\* debe iterar usuario por usuario para consultar precios en Yahoo Finance.  
   \- \*\*Flujo en 3 Fases obligatorio:\*\*  
     \- \*\*Fase A (Agrupación):\*\* Consultar en PostgreSQL el conjunto único (\`DISTINCT\`) de \`tickers\` que pertenezcan a carteras activas o tengan alertas pendientes.  
     \- \*\*Fase B (Ingesta Concurrente):\*\* Consultar Yahoo Finance exactamente \*\*una sola vez por ticker único\*\* en cada ciclo del worker y persistir los nuevos precios en \`price\_history\`.  
     \- \*\*Fase C (Evaluación Local):\*\* Con el mapa de precios actualizados en memoria (\`{ticker: precio}\`), evaluar en la base de datos las condiciones de las alertas de todos los usuarios y despachar los avisos pertinentes.

\---

\#\# 5.2 Concurrencia y Control de Tráfico I/O  
1\. \*\*Llamadas Concurrentes No Bloqueantes:\*\*  
   \- La ingesta de múltiples activos en \`services/market.py\` debe orquestarse mediante \`asyncio.gather(\*tasks)\`.  
   \- Se prohíbe el uso de bucles \`for\` secuenciales con \`await\` para llamadas de red externas.  
2\. \*\*Control de Saturación (Semáforos y Timeouts):\*\*  
   \- Toda llamada HTTP externa (\`httpx.AsyncClient\`) debe incluir un \`timeout\` explícito de máximo 10 segundos.  
   \- Enviar obligatoriamente la cabecera \`User-Agent: Mozilla/5.0...\` en cada petición dirigida a los endpoints de Yahoo Finance (\`/v8/finance/chart/{ticker}\`).

\---

\#\# 5.3 Persistencia y Acceso a Base de Datos  
1\. \*\*Driver 100% Asíncrono:\*\*  
   \- Toda interacción con PostgreSQL debe realizarse mediante \`AsyncSession\` con el driver \`asyncpg\`.  
   \- Queda prohibido el uso de métodos síncronos del ORM (como \`session.commit()\` sin \`await\` o \`Session\` tradicional).  
2\. \*\*Gestión de Sesiones en el Worker:\*\*  
   \- El background worker debe instanciar y cerrar su propia \`AsyncSession\` utilizando un context manager (\`async with async\_session\_maker() as session:\`) por cada ciclo de ejecución, evitando retener sesiones o conexiones abiertas de forma indefinida.

\---

\#\# 5.4 Evaluación de Alertas y Prevención de Spam (Cooldown)  
1\. \*\*Fórmulas de Evaluación:\*\*  
   \- \*\*Variación Porcentual:\*\*    
     \$\$\\text{Variación} \= \\left\\vert{} \\frac{\\text{Precio Actual} \- \\text{Último Precio Registrado}}{\\text{Último Precio Registrado}} \\right\\vert{} \\times 100\$\$    
     Disparar si Variación \>= threshold\_value.  
   \- \*\*Precio Mínimo (Stop Loss):\*\* Disparar si Precio Actual \<= threshold\_value.  
   \- \*\*Precio Máximo (Take Profit):\*\* Disparar si Precio Actual \>= threshold\_value.  
2\. \*\*Mecanismo de Enfriamiento (Cooldown):\*\*  
   \- Para no saturar al usuario con mensajes repetitivos en cada pasada del worker cuando un precio se mantiene fuera de rango:  
     \- No volver a disparar una alerta si su campo \`last\_triggered\_at\` tiene una antigüedad menor al tiempo de enfriamiento configurado (ej. 30 o 60 minutos).  
     \- Al dispararse la alerta, actualizar de inmediato el campo \`last\_triggered\_at \= datetime.now(timezone.utc)\`.

\---

\#\# 5.5 Integración con Telegram y Manejo de Errores  
1\. \*\*Despacho Ligero:\*\*  
   \- Los mensajes se envían mediante peticiones HTTP asíncronas con \`httpx\` al endpoint \`sendMessage\` de Telegram Bot API.  
\- Si un usuario tiene un \`telegram\_chat\_id\` inválido o bloqueó al bot (errores HTTP 400 o 403), el sistema debe capturar la excepción (\`try/except\`) e ignorar ese envío puntual para no interrumpir la ejecución del worker ni las alertas del resto de los usuarios.

\---

\#\# 5.6 Manejo de Secretos y Configuración  
1\. \*\*Configuración Centralizada:\*\*  
   \- Ninguna URL de base de datos, credencial de Google OAuth o token de Telegram debe escribirse fija en el código fuente.  
   \- Todas las variables deben leerse a través de \`app/config.py\` utilizando \`pydantic-settings\` contra el entorno del sistema y el archivo \`.env\`.  