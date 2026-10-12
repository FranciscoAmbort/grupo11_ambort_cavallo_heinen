from datetime import datetime

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


class TelegramUpdateRequest(BaseModel):
    telegram_chat_id: str


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
