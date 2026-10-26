from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    false,
    func,
    true,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    google_id: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    name: Mapped[str | None] = mapped_column(String(255))
    telegram_chat_id: Mapped[str | None] = mapped_column(String(64))
    onboarding_completed: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Asset(Base):
    __tablename__ = "assets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(150))
    # 'stock' (acción), 'bond' (bono) o 'cedear'
    asset_type: Mapped[str] = mapped_column(String(20), index=True)
    currency: Mapped[str] = mapped_column(String(10), default="ARS", server_default="ARS")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())


class UserAsset(Base):
    __tablename__ = "user_assets"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    asset_id: Mapped[int] = mapped_column(ForeignKey("assets.id", ondelete="CASCADE"), primary_key=True)
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Alert(Base):
    __tablename__ = "alerts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    asset_id: Mapped[int] = mapped_column(ForeignKey("assets.id", ondelete="CASCADE"), index=True)
    # 'percentage', 'price_min' o 'price_max'
    alert_type: Mapped[str] = mapped_column(String(20))
    threshold_value: Mapped[Decimal] = mapped_column(Numeric(12, 4))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    last_triggered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_notified_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PriceHistory(Base):
    __tablename__ = "price_history"
    __table_args__ = (Index("ix_price_history_asset_timestamp", "asset_id", "timestamp"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    asset_id: Mapped[int] = mapped_column(ForeignKey("assets.id", ondelete="CASCADE"))
    price: Mapped[Decimal] = mapped_column(Numeric(12, 4))
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AlertEvent(Base):
    """Un disparo de alerta: copia los datos del momento para que el historial sobreviva a la alerta."""

    __tablename__ = "alert_events"
    __table_args__ = (Index("ix_alert_events_user_triggered", "user_id", "triggered_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    # SET NULL: si el usuario borra la alerta, el evento queda en el historial
    alert_id: Mapped[int | None] = mapped_column(ForeignKey("alerts.id", ondelete="SET NULL"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    asset_id: Mapped[int] = mapped_column(ForeignKey("assets.id", ondelete="CASCADE"))
    # Copias del tipo y el umbral de la alerta al momento del disparo
    alert_type: Mapped[str] = mapped_column(String(20))
    threshold_value: Mapped[Decimal] = mapped_column(Numeric(12, 4))
    price: Mapped[Decimal] = mapped_column(Numeric(12, 4))
    # Solo alertas porcentuales; con signo (+ subió, − bajó)
    change_pct: Mapped[Decimal | None] = mapped_column(Numeric(9, 4))
    # Resultado del aviso por Telegram o correo
    notified: Mapped[bool] = mapped_column(Boolean)
    triggered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class WeeklySummary(Base):
    """Resumen semanal de la cartera redactado por la IA (solo se guardan los exitosos)."""

    __tablename__ = "weekly_summaries"
    __table_args__ = (UniqueConstraint("user_id", "week_start", name="uq_weekly_summaries_user_week"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    # Lunes de la semana en hora argentina
    week_start: Mapped[date] = mapped_column(Date)
    # Estadísticas que se le mandaron a la IA
    stats: Mapped[dict] = mapped_column(JSONB)
    content: Mapped[str] = mapped_column(Text)
    model: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
