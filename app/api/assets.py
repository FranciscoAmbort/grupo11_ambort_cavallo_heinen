from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.database import get_session
from app.models import Asset, PriceHistory, User, UserAsset
from app.schemas import AssetOut, PriceHistoryPoint, UserAssetAddRequest

router = APIRouter()

# Regla de negocio: no se soportan bonos.
VALID_ASSET_TYPES = ("stock", "cedear")


def _latest_price_subquery():
    """Subconsulta correlacionada con el último precio conocido de cada activo."""
    return (
        select(PriceHistory.price)
        .where(PriceHistory.asset_id == Asset.id)
        .order_by(PriceHistory.timestamp.desc())
        .limit(1)
        .correlate(Asset)
        .scalar_subquery()
    )


def _to_asset_out(asset: Asset, price) -> AssetOut:
    """Construye un AssetOut a partir del activo y su último precio (Decimal o None)."""
    return AssetOut(
        id=asset.id,
        ticker=asset.ticker,
        name=asset.name,
        asset_type=asset.asset_type,
        current_price=float(price) if price is not None else None,
    )


async def _get_user_assets(session: AsyncSession, user_id: int) -> list[AssetOut]:
    """Devuelve los activos de la cartera del usuario con su último precio, ordenados por ticker."""
    stmt = (
        select(Asset, _latest_price_subquery().label("current_price"))
        .join(UserAsset, UserAsset.asset_id == Asset.id)
        .where(UserAsset.user_id == user_id, Asset.asset_type.in_(VALID_ASSET_TYPES))
        .order_by(Asset.ticker.asc())
    )
    result = await session.execute(stmt)
    return [_to_asset_out(asset, price) for asset, price in result.all()]


@router.get("", response_model=list[AssetOut])
async def list_assets(
    asset_type: str | None = None,
    session: AsyncSession = Depends(get_session),
) -> list[AssetOut]:
    """Lista el catálogo de activos activos con su último precio, ordenado por ticker."""
    if asset_type is not None and asset_type not in VALID_ASSET_TYPES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"asset_type debe ser uno de: {', '.join(VALID_ASSET_TYPES)}",
        )

    types = (asset_type,) if asset_type else VALID_ASSET_TYPES
    stmt = (
        select(Asset, _latest_price_subquery().label("current_price"))
        .where(Asset.is_active.is_(True), Asset.asset_type.in_(types))
        .order_by(Asset.ticker.asc())
    )
    result = await session.execute(stmt)
    return [_to_asset_out(asset, price) for asset, price in result.all()]


@router.get("/me", response_model=list[AssetOut])
async def list_my_assets(
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[AssetOut]:
    """Lista los activos de la cartera del usuario autenticado."""
    return await _get_user_assets(session, current_user.id)


@router.post("/me", response_model=list[AssetOut])
async def add_my_assets(
    payload: UserAssetAddRequest,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[AssetOut]:
    """Agrega activos a la cartera de forma idempotente y completa el onboarding si estaba pendiente."""
    requested_ids = set(payload.asset_ids)

    if requested_ids:
        found_ids = set(
            (
                await session.scalars(
                    select(Asset.id).where(
                        Asset.id.in_(requested_ids),
                        Asset.is_active.is_(True),
                        Asset.asset_type.in_(VALID_ASSET_TYPES),
                    )
                )
            ).all()
        )
        missing = sorted(requested_ids - found_ids)
        if missing:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Activos inexistentes o no soportados: {missing}",
            )

        stmt = (
            insert(UserAsset)
            .values([{"user_id": current_user.id, "asset_id": asset_id} for asset_id in sorted(found_ids)])
            .on_conflict_do_nothing(index_elements=["user_id", "asset_id"])
        )
        await session.execute(stmt)

    # Un POST vacío equivale a "Omitir" en todos los pasos del onboarding.
    if not current_user.onboarding_completed:
        current_user.onboarding_completed = True

    await session.commit()
    return await _get_user_assets(session, current_user.id)


@router.delete("/me/{asset_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_my_asset(
    asset_id: int,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Quita un activo de la cartera del usuario (idempotente)."""
    await session.execute(
        delete(UserAsset).where(UserAsset.user_id == current_user.id, UserAsset.asset_id == asset_id)
    )
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{ticker}/history", response_model=list[PriceHistoryPoint])
async def get_asset_history(
    ticker: str,
    limit: int = Query(default=100, ge=1, le=1000),
    session: AsyncSession = Depends(get_session),
) -> list[PriceHistoryPoint]:
    """Devuelve las últimas `limit` cotizaciones del activo en orden cronológico ascendente."""
    asset_id = await session.scalar(
        select(Asset.id).where(Asset.ticker == ticker.upper(), Asset.asset_type.in_(VALID_ASSET_TYPES))
    )
    if asset_id is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Activo no encontrado")

    stmt = (
        select(PriceHistory.price, PriceHistory.timestamp)
        .where(PriceHistory.asset_id == asset_id)
        .order_by(PriceHistory.timestamp.desc())
        .limit(limit)
    )
    rows = (await session.execute(stmt)).all()
    return [PriceHistoryPoint(price=float(price), timestamp=ts) for price, ts in reversed(rows)]

