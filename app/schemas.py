from datetime import datetime

from pydantic import BaseModel, ConfigDict, field_validator, model_validator


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


class AssetOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    ticker: str
    name: str
    asset_type: str
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
