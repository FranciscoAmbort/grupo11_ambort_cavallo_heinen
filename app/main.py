import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, status
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import router as auth_router
from app.config import settings
from app.database import engine, get_session
from app.worker.checker import run_checker_loop

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Maneja el arranque y apagado de la app; lanza el worker en segundo plano y cierra conexiones."""
    # Arrancar tarea en segundo plano
    worker_task = asyncio.create_task(run_checker_loop(interval_seconds=settings.worker_interval_seconds))
    logger.info("Worker de monitoreo lanzado en segundo plano.")

    yield

    # Shutdown limpio
    logger.info("Cancelando worker de monitoreo...")
    worker_task.cancel()
    try:
        await worker_task
    except asyncio.CancelledError:
        logger.info("Worker de monitoreo cancelado correctamente.")
    except Exception as exc:
        logger.warning("Excepción durante la cancelación del worker: %s", exc)

    await engine.dispose()
    logger.info("Conexiones de base de datos cerradas.")


app = FastAPI(title=settings.app_name, lifespan=lifespan)

app.include_router(auth_router, prefix="/api/auth", tags=["auth"])


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
