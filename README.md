# Monitor de Precios y Alertas del Mercado Argentino
**Grupo 11:** Ambort, Cavallo, Heinen

Aplicación web orientada al mercado financiero argentino para seguir cotizaciones de **acciones locales (BYMA)**, **bonos soberanos** y **CEDEARs**, visualizar su evolución histórica y recibir alertas automáticas por **Telegram** o **correo electrónico**.

---

## ¿Qué hace la aplicación?

1. **Ingreso y cartera de seguimiento:** El usuario inicia sesión con su cuenta de **Google (OAuth 2.0)**. En su primer ingreso pasa por un asistente de 3 pasos donde elige qué acciones locales, bonos soberanos y CEDEARs desea seguir, pudiendo agregar o quitar instrumentos en cualquier momento desde el panel.
2. **Monitoreo automático de cotizaciones:** Mientras el mercado está abierto, un proceso asíncrono en segundo plano consulta periódicamente las cotizaciones de todos los activos seguidos de forma concurrente:
   - **Acciones y CEDEARs:** obtiene las velas intradiarias minuto a minuto (precio de cierre, máximo y mínimo) desde **Yahoo Finance**.
   - **Bonos soberanos:** obtiene las cotizaciones desde **CompararFondos**.
   - Cuando cierra la rueda, el sistema mantiene una ventana de gracia para terminar de recibir las últimas cotizaciones del día que llegan con demora.
3. **Reglas de alerta y notificaciones:** Para cada activo de su cartera el usuario puede configurar tres tipos de alertas:
   - **Precio máximo (Take Profit):** notifica si la cotización alcanza o supera un valor techo (`$X`) y se desactiva automáticamente.
   - **Precio mínimo (Stop Loss):** notifica si la cotización toca o cae por debajo de un valor piso (`$X`) y se desactiva automáticamente.
   - **Variación porcentual:** notifica cada vez que el precio sube o baja un `X%` respecto de la última referencia y vuelve a anclarse en el nuevo valor.
   - Las condiciones se evalúan sobre los valores máximos y mínimos de cada minuto para detectar cruces de umbral aunque el precio haya revertido dentro del mismo minuto. Los avisos se envían por **Telegram** y, como respaldo, por **correo electrónico**.
4. **Gráficos e historial:** El panel principal permite filtrar los activos por categoría o moneda (`ARS` / `USD`) y consultar un gráfico interactivo con la evolución histórica de precios en distintos rangos de tiempo.
5. **Resumen semanal con IA:** Incluye una sección que calcula las estadísticas semanales de la cartera (variaciones, mínimos, máximos, mejor y peor activo, y alertas disparadas en la semana en curso o la semana anterior) y genera un análisis interpretativo redactado con inteligencia artificial (**Google Gemini**).
