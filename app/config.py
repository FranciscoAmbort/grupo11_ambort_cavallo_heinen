from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "Monitor de Precios y Alertas"
    # Sin valor por defecto: debe venir del entorno o del .env
    database_url: str

    # Token del bot de Telegram para el despacho de alertas push.
    # Valor vacío por defecto: la app arranca aunque no esté configurado;
    # el servicio telegram.py verifica que no esté vacío antes de enviar.
    telegram_bot_token: str = ""

    # Intervalo de chequeo del background worker en segundos
    worker_interval_seconds: int = 60

    # Clave de firma de los JWT de sesión (HS256). Sin valor por defecto.
    secret_key: str
    # Vigencia del JWT de sesión (por defecto 24 horas)
    access_token_expire_minutes: int = 60 * 24
    # Client ID de Google OAuth; si está definido, se valida el "aud" del id_token.
    google_client_id: str = ""



@lru_cache
def get_settings() -> Settings:
    """Lee la configuración del entorno una sola vez y la reutiliza."""
    return Settings()


settings = get_settings()
