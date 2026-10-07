import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, status
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import engine, get_session

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Maneja el arranque y apagado de la app; al apagar cierra las conexiones a la base."""
    yield
    await engine.dispose()


app = FastAPI(title=settings.app_name, lifespan=lifespan)


@app.get("/api/health", tags=["health"])
async def health(session: AsyncSession = Depends(get_session)) -> dict[str, bool | str]:
    """Verifica que la app esté viva y que pueda consultar PostgreSQL."""
    try:
        await session.execute(text("SELECT 1"))
    except (SQLAlchemyError, OSError) as exc:
        logger.exception("Fallo el chequeo de conexión a PostgreSQL")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"status": "error", "database": False},
        ) from exc
    return {"status": "ok", "database": True}
