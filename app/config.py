from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Nombre fijo (no un alias -latest, que puede cambiar de modelo sin aviso)
DEFAULT_GEMINI_MODEL = "gemini-3.5-flash-lite"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "Monitor de Precios y Alertas"
    # Sin valor por defecto: debe venir del entorno o del .env
    database_url: str

    # Token del bot de Telegram para el despacho de alertas push.
    # Valor vacío por defecto: la app arranca aunque no esté configurado;
    # el servicio telegram.py verifica que no esté vacío antes de enviar.
    telegram_bot_token: str = ""

    # Correo de respaldo (API de Brevo) para usuarios sin Telegram o cuando Telegram falla.
    # Vacíos por defecto: si faltan, simplemente no se envían correos.
    brevo_api_key: str = ""
    # Dirección remitente; tiene que estar verificada en la cuenta de Brevo.
    email_from: str = ""

    # Intervalo de chequeo del background worker en segundos
    worker_interval_seconds: int = 60

    # Clave de firma de los JWT de sesión (HS256). Sin valor por defecto.
    secret_key: str
    # Vigencia del JWT de sesión (por defecto 24 horas)
    access_token_expire_minutes: int = 60 * 24
    # Client ID de Google OAuth; si está definido, se valida el "aud" del id_token.
    google_client_id: str = ""

    # Resumen semanal con IA (Gemini). Sin clave la app funciona igual y el resumen
    # muestra solo las estadísticas.
    gemini_api_key: str = ""
    gemini_model: str = DEFAULT_GEMINI_MODEL

    @field_validator("gemini_model")
    @classmethod
    def _default_gemini_model(cls, value: str) -> str:
        """Un GEMINI_MODEL vacío en el .env no debe pisar el modelo por defecto."""
        return value.strip() or DEFAULT_GEMINI_MODEL


@lru_cache
def get_settings() -> Settings:
    """Lee la configuración del entorno una sola vez y la reutiliza."""
    return Settings()


settings = get_settings()
