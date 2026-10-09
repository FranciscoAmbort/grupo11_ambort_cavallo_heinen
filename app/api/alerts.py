from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.database import get_session
from app.models import Alert, Asset, User
from app.schemas import AlertCreateRequest, AlertOut

router = APIRouter()

# El worker (app/worker/checker.py) evalúa los tipos internos del modelo; la API expone otros nombres.
API_TO_DB_TYPE = {"ABOVE": "price_max", "BELOW": "price_min", "PCT_CHANGE": "percentage"}
DB_TO_API_TYPE = {db: api for api, db in API_TO_DB_TYPE.items()}


def _to_alert_out(alert: Alert, ticker: str) -> AlertOut:
    """Convierte una Alert del modelo al esquema público, separando precio objetivo y porcentaje."""
    api_type = DB_TO_API_TYPE.get(alert.alert_type, alert.alert_type)
    value = float(alert.threshold_value)
    is_pct = api_type == "PCT_CHANGE"
    return AlertOut(
        id=alert.id,
        asset_id=alert.asset_id,
        ticker=ticker,
        alert_type=api_type,
        target_price=None if is_pct else value,
        threshold_percent=value if is_pct else None,
        is_active=alert.is_active,
        last_triggered_at=alert.last_triggered_at,
        created_at=alert.created_at,
    )


@router.get("", response_model=list[AlertOut])
async def list_alerts(
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[AlertOut]:
    """Lista todas las alertas del usuario (activas e inactivas) con el ticker del activo."""
    stmt = (
        select(Alert, Asset.ticker)
        .join(Asset, Asset.id == Alert.asset_id)
        .where(Alert.user_id == current_user.id)
        .order_by(Alert.created_at.desc(), Alert.id.desc())
    )
    result = await session.execute(stmt)
    return [_to_alert_out(alert, ticker) for alert, ticker in result.all()]


@router.post("", response_model=AlertOut, status_code=status.HTTP_201_CREATED)
async def create_alert(
    payload: AlertCreateRequest,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> AlertOut:
    """Crea una alerta activa para el usuario sobre un activo existente."""
    ticker = await session.scalar(select(Asset.ticker).where(Asset.id == payload.asset_id))
    if ticker is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Activo no encontrado")

    value = payload.threshold_percent if payload.alert_type == "PCT_CHANGE" else payload.target_price
    alert = Alert(
        user_id=current_user.id,
        asset_id=payload.asset_id,
        alert_type=API_TO_DB_TYPE[payload.alert_type],
        threshold_value=Decimal(str(value)),
        is_active=True,
    )
    session.add(alert)
    await session.commit()
    await session.refresh(alert)
    return _to_alert_out(alert, ticker)


@router.delete("/{alert_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_alert(
    alert_id: int,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Elimina una alerta del usuario; responde 404 si no existe o pertenece a otro usuario."""
    result = await session.execute(
        delete(Alert).where(Alert.id == alert_id, Alert.user_id == current_user.id).returning(Alert.id)
    )
    if result.scalar_one_or_none() is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Alerta no encontrada")
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)

