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



@lru_cache
def get_settings() -> Settings:
    """Lee la configuración del entorno una sola vez y la reutiliza."""
    return Settings()


settings = get_settings()
