from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.database import get_session
from app.models import User
from app.schemas import WeeklySummaryOut
from app.services.weekly_summary import generate_weekly_summary, get_cached_summary

router = APIRouter()

# 0 = semana en curso (parcial), 1 = semana pasada
WeekOffset = Annotated[int, Query(ge=0, le=1, description="0 = esta semana, 1 = semana pasada")]


@router.get("/weekly", response_model=WeeklySummaryOut)
async def get_weekly_summary(
    week_offset: WeekOffset = 0,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> WeeklySummaryOut:
    """Devuelve el resumen guardado de la semana; 404 si todavía no se generó (o quedó desactualizado)."""
    summary = await get_cached_summary(session, current_user.id, week_offset)
    if summary is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Todavía no hay un resumen para esta semana",
        )
    return WeeklySummaryOut.model_validate(summary)


@router.post("/weekly", response_model=WeeklySummaryOut)
async def create_weekly_summary(
    week_offset: WeekOffset = 0,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> WeeklySummaryOut:
    """
    Genera el resumen de la semana con la IA y lo guarda.

    Si hay uno guardado que sigue vigente (semana pasada ya cerrada, o semana actual con
    menos de 1 hora) lo devuelve sin llamar a la IA. Si la IA no está disponible devuelve
    igual las estadísticas, con content=null y un mensaje en error.
    """
    summary = await generate_weekly_summary(session, current_user.id, week_offset)
    return WeeklySummaryOut.model_validate(summary)
