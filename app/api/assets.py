from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.config import settings
from app.database import get_session
from app.models import Alert, Asset, PriceHistory, User, UserAsset
from app.schemas import (
    AssetOut,
    CustomAssetRequest,
    MarketCandidateOut,
    PriceHistoryPoint,
    UserAssetAddRequest,
)
from app.services import catalog

router = APIRouter()

# Activos soportados: acciones BYMA, CEDEARs y bonos soberanos
VALID_ASSET_TYPES = ("stock", "cedear", "bond")

MAX_ASSETS_MESSAGE = "Llegaste al máximo de activos que podés seguir"
CATALOG_FULL_MESSAGE = "El catálogo llegó a su máximo de activos"
# Resultados del mercado que devuelve una búsqueda
SEARCH_RESULTS_LIMIT = 20
# Clave del advisory lock de PostgreSQL que serializa las altas al catálogo
CATALOG_LOCK_KEY = 1128354892


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
        currency=getattr(asset, "currency", "ARS") or "ARS",
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


async def _followed_ids(session: AsyncSession, user_id: int) -> set[int]:
    """Ids de los activos que ya sigue el usuario."""
    return set((await session.scalars(select(UserAsset.asset_id).where(UserAsset.user_id == user_id))).all())


async def _follow_assets(session: AsyncSession, user: User, asset_ids: set[int]) -> None:
    """
    Agrega los activos a la cartera de forma idempotente, aplica MAX_ASSETS_PER_USER (409) y
    completa el onboarding si estaba pendiente. Los activos que ya sigue no cuentan como nuevos.
    No hace commit.
    """
    followed = await _followed_ids(session, user.id)
    new_ids = asset_ids - followed
    if new_ids and len(followed) + len(new_ids) > settings.max_assets_per_user:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=MAX_ASSETS_MESSAGE)

    if new_ids:
        stmt = (
            insert(UserAsset)
            .values([{"user_id": user.id, "asset_id": asset_id} for asset_id in sorted(new_ids)])
            .on_conflict_do_nothing(index_elements=["user_id", "asset_id"])
        )
        await session.execute(stmt)

    # Un POST vacío equivale a "Omitir" en todos los pasos del onboarding.
    if not user.onboarding_completed:
        user.onboarding_completed = True


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


@router.get("/search", response_model=list[MarketCandidateOut])
async def search_market_assets(
    q: str = Query(min_length=1, max_length=40),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[MarketCandidateOut]:
    """
    Busca por ticker o nombre activos del mercado (bonos soberanos y acciones o CEDEARs de
    Buenos Aires) que todavía no están en el catálogo. Si Yahoo falla devuelve igual los bonos.
    """
    if not catalog.search_limiter.allow(current_user.id):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Demasiadas búsquedas: probá de nuevo en un minuto",
        )
    query = q.strip()
    if not query:
        return []

    candidates = await catalog.search_market(query)
    # Se consultan también las bases de las variantes (GGAL.BA para GGALD.BA) para ocultarlas
    lookup = {candidate.ticker for candidate in candidates} | catalog.variant_bases(candidates)
    known: set[str] = set()
    if lookup:
        known = set((await session.scalars(select(Asset.ticker).where(Asset.ticker.in_(lookup)))).all())

    visible = catalog.drop_known_and_variants(candidates, known)
    ranked = catalog.rank_and_limit(visible, query, SEARCH_RESULTS_LIMIT)
    return [MarketCandidateOut(**asdict(candidate)) for candidate in ranked]


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
    """
    Agrega activos a la cartera de forma idempotente y completa el onboarding si estaba pendiente.
    Responde 409 si se supera MAX_ASSETS_PER_USER (los activos ya seguidos no cuentan como nuevos).
    """
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

    await _follow_assets(session, current_user, requested_ids)
    await session.commit()
    return await _get_user_assets(session, current_user.id)


@router.post("/custom", response_model=list[AssetOut])
async def add_custom_asset(
    payload: CustomAssetRequest,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[AssetOut]:
    """
    Sigue un activo del mercado: si no está en el catálogo lo valida contra su proveedor
    (CompararFondos para bonos, Yahoo para acciones y CEDEARs) y lo agrega; si ya está, lo
    reutiliza tal cual. Responde 400 por formato inválido, 404 si el proveedor no lo conoce,
    409 por los topes y 503 si el proveedor no responde.
    """
    if payload.asset_type not in VALID_ASSET_TYPES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Tipo de activo inválido")
    ticker = catalog.normalize_ticker(payload.ticker, payload.asset_type)
    if ticker is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Formato de ticker inválido")

    asset = await session.scalar(select(Asset).where(Asset.ticker == ticker))
    if asset is not None and not asset.is_active:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Ese activo está deshabilitado")

    if asset is not None:
        # Ya está en el catálogo: se reutiliza con su tipo y su moneda, sin consultar al proveedor
        asset_id = asset.id
    else:
        # Tope del usuario antes de consultar al proveedor (se vuelve a aplicar al seguirlo)
        if len(await _followed_ids(session, current_user.id)) >= settings.max_assets_per_user:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=MAX_ASSETS_MESSAGE)

        if payload.asset_type == "bond":
            result = await catalog.validate_bond(ticker)
        else:
            result = await catalog.validate_yahoo(ticker)
        if result.status == "unavailable":
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="El proveedor de precios no responde: probá de nuevo más tarde",
            )
        if result.status == "not_found":
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="El proveedor no conoce ese ticker")

        # Serializa las altas hasta el commit para que dos pedidos simultáneos no pasen el tope
        await session.execute(select(func.pg_advisory_xact_lock(CATALOG_LOCK_KEY)))
        catalog_size = await session.scalar(select(func.count()).select_from(Asset))
        if catalog_size >= settings.max_catalog_assets:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=CATALOG_FULL_MESSAGE)

        asset_id = await session.scalar(
            insert(Asset)
            .values(
                ticker=ticker,
                name=result.name,
                asset_type=payload.asset_type,
                currency=result.currency,
                is_active=True,
            )
            .on_conflict_do_nothing(index_elements=["ticker"])
            .returning(Asset.id)
        )
        if asset_id is None:
            # Otro pedido lo dio de alta entre el SELECT y el INSERT
            asset_id = await session.scalar(select(Asset.id).where(Asset.ticker == ticker))

    await _follow_assets(session, current_user, {asset_id})
    await session.commit()
    return await _get_user_assets(session, current_user.id)


@router.delete("/me/{asset_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_my_asset(
    asset_id: int,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Quita un activo de la cartera del usuario junto con sus alertas sobre ese activo (idempotente).

    Los AlertEvent se conservan: su alert_id queda en NULL por el ON DELETE SET NULL.
    """
    await session.execute(
        delete(Alert).where(Alert.user_id == current_user.id, Alert.asset_id == asset_id)
    )
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

