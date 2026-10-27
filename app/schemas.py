import re
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator, model_validator


class PublicConfigOut(BaseModel):
    google_client_id: str


class GoogleAuthRequest(BaseModel):
    id_token: str


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    email: str
    name: str | None
    telegram_chat_id: str | None
    onboarding_completed: bool
    created_at: datetime


class TelegramUpdateRequest(BaseModel):
    # None o vacío desvincula Telegram
    telegram_chat_id: str | None = None

    @field_validator("telegram_chat_id")
    @classmethod
    def _validate_chat_id(cls, value: str | None) -> str | None:
        """Convierte vacío en None y exige que el Chat ID sea numérico."""
        if value is None or not value.strip():
            return None
        value = value.strip()
        if not re.fullmatch(r"-?\d{5,20}", value):
            raise ValueError("El Chat ID de Telegram es un número, por ejemplo 123456789")
        return value


ALERT_TYPES = ("ABOVE", "BELOW", "PCT_CHANGE")
# La columna threshold_value es Numeric(12, 4): admite hasta 8 cifras enteras
MAX_THRESHOLD_VALUE = 100_000_000
ASSET_TYPES = ("stock", "cedear", "bond")


class AssetOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    ticker: str
    name: str
    asset_type: str
    currency: str = "ARS"
    current_price: float | None = None


class UserAssetAddRequest(BaseModel):
    asset_ids: list[int]


class MarketCandidateOut(BaseModel):
    """Activo del mercado que todavía no está en el catálogo (resultado de la búsqueda)."""

    ticker: str
    name: str
    # "bond" en los bonos; None en Yahoo, donde el usuario elige acción o CEDEAR
    asset_type: str | None
    # Solo en los bonos: la búsqueda de Yahoo no trae la moneda
    currency: str | None
    # Yahoo: "cedear" o "stock" según el nombre, para preseleccionar el tipo
    type_hint: str | None


class CustomAssetRequest(BaseModel):
    # Texto libre: el endpoint lo valida y responde 400 si el formato no es válido
    ticker: str
    asset_type: str


class PriceHistoryPoint(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    price: float
    timestamp: datetime


class AlertCreateRequest(BaseModel):
    asset_id: int
    alert_type: str
    target_price: float | None = None
    threshold_percent: float | None = None

    @field_validator("alert_type")
    @classmethod
    def _normalize_alert_type(cls, value: str) -> str:
        """Normaliza a mayúsculas y rechaza tipos desconocidos."""
        normalized = value.strip().upper()
        if normalized not in ALERT_TYPES:
            raise ValueError(f"alert_type debe ser uno de: {', '.join(ALERT_TYPES)}")
        return normalized

    @model_validator(mode="after")
    def _validate_thresholds(self) -> "AlertCreateRequest":
        """ABOVE/BELOW exigen target_price > 0; PCT_CHANGE exige threshold_percent > 0."""
        if self.alert_type in ("ABOVE", "BELOW"):
            if self.target_price is None or self.target_price <= 0:
                raise ValueError("target_price es obligatorio y debe ser mayor a 0 para alertas ABOVE/BELOW")
        elif self.threshold_percent is None or self.threshold_percent <= 0:
            raise ValueError("threshold_percent es obligatorio y debe ser mayor a 0 para alertas PCT_CHANGE")
        value = self.threshold_percent if self.alert_type == "PCT_CHANGE" else self.target_price
        if value >= MAX_THRESHOLD_VALUE:
            raise ValueError(f"El valor debe ser menor a {MAX_THRESHOLD_VALUE:,.0f}")
        return self


class AlertUpdateRequest(BaseModel):
    """Campos opcionales para modificar una alerta: pausar/reactivar o cambiar su condición."""

    is_active: bool | None = None
    alert_type: str | None = None
    target_price: float | None = None
    threshold_percent: float | None = None

    @field_validator("alert_type")
    @classmethod
    def _normalize_alert_type(cls, value: str | None) -> str | None:
        """Normaliza a mayúsculas y rechaza tipos desconocidos."""
        if value is None:
            return None
        normalized = value.strip().upper()
        if normalized not in ALERT_TYPES:
            raise ValueError(f"alert_type debe ser uno de: {', '.join(ALERT_TYPES)}")
        return normalized

    @model_validator(mode="after")
    def _validate_values(self) -> "AlertUpdateRequest":
        """Exige al menos un campo y que los valores numéricos estén dentro del rango permitido."""
        if all(v is None for v in (self.is_active, self.alert_type, self.target_price, self.threshold_percent)):
            raise ValueError("No se indicó ningún cambio")
        for name in ("target_price", "threshold_percent"):
            value = getattr(self, name)
            if value is not None and not 0 < value < MAX_THRESHOLD_VALUE:
                raise ValueError(f"{name} debe ser mayor a 0 y menor a {MAX_THRESHOLD_VALUE:,.0f}")
        return self


class AlertOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    asset_id: int
    ticker: str
    alert_type: str
    target_price: float | None
    threshold_percent: float | None
    is_active: bool
    last_triggered_at: datetime | None
    created_at: datetime


class WeeklySummaryOut(BaseModel):
    """Resumen semanal: estadísticas de la cartera y texto de la IA (null si no está disponible)."""

    model_config = ConfigDict(from_attributes=True)

    week_start: date
    week_end: date
    stats: dict[str, Any]
    content: str | None
    error: str | None = None
    model: str | None
    created_at: datetime
    from_cache: bool
