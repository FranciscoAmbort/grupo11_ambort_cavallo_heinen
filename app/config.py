from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "Monitor de Precios y Alertas"
    # Sin valor por defecto: debe venir del entorno o del .env
    database_url: str


@lru_cache
def get_settings() -> Settings:
    """Lee la configuración del entorno una sola vez y la reutiliza."""
    return Settings()


settings = get_settings()
