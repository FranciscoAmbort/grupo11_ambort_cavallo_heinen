import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import ACCESS_TOKEN_COOKIE, create_access_token, get_current_user
from app.config import settings
from app.database import get_session
from app.models import User
from app.schemas import GoogleAuthRequest, TelegramUpdateRequest, UserOut

logger = logging.getLogger(__name__)

router = APIRouter()

GOOGLE_TOKENINFO_URL = "https://oauth2.googleapis.com/tokeninfo"


async def _verify_google_id_token(id_token: str) -> dict:
    """Valida el id_token contra el endpoint tokeninfo de Google y devuelve sus claims."""
    invalid = HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token de Google inválido")
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(GOOGLE_TOKENINFO_URL, params={"id_token": id_token})
    except httpx.HTTPError as exc:
        logger.warning("No se pudo contactar a Google para validar el id_token: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="No se pudo validar el token con Google",
        ) from exc

    if response.status_code != 200:
        raise invalid

    claims = response.json()
    if not claims.get("sub") or not claims.get("email"):
        raise invalid
    if str(claims.get("email_verified", "")).lower() != "true":
        raise invalid
    if settings.google_client_id and claims.get("aud") != settings.google_client_id:
        raise invalid
    return claims


@router.post("/google", response_model=UserOut)
async def login_with_google(
    payload: GoogleAuthRequest,
    response: Response,
    session: AsyncSession = Depends(get_session),
) -> User:
    """Inicia sesión con un id_token de Google; crea el usuario si es la primera vez."""
    claims = await _verify_google_id_token(payload.id_token)
    google_id: str = claims["sub"]
    email: str = claims["email"]
    name: str | None = claims.get("name")

    user = await session.scalar(select(User).where(User.google_id == google_id))
    if user is None:
        user = await session.scalar(select(User).where(User.email == email))

    if user is None:
        user = User(email=email, google_id=google_id, name=name, onboarding_completed=False)
        session.add(user)
        try:
            await session.commit()
        except IntegrityError:
            # Carrera: otro request creó al mismo usuario entre el SELECT y el INSERT.
            await session.rollback()
            user = await session.scalar(select(User).where(User.google_id == google_id))
            if user is None:
                raise
        else:
            await session.refresh(user)

    token = create_access_token(user.id)
    response.set_cookie(
        key=ACCESS_TOKEN_COOKIE,
        value=token,
        httponly=True,
        samesite="lax",
        max_age=settings.access_token_expire_minutes * 60,
    )
    return user


@router.get("/me", response_model=UserOut)
async def read_me(current_user: User = Depends(get_current_user)) -> User:
    """Devuelve los datos del usuario autenticado."""
    return current_user


@router.patch("/telegram", response_model=UserOut)
async def update_telegram(
    payload: TelegramUpdateRequest,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> User:
    """Actualiza el telegram_chat_id del usuario autenticado."""
    current_user.telegram_chat_id = payload.telegram_chat_id
    session.add(current_user)
    await session.commit()
    await session.refresh(current_user)
    return current_user


@router.post("/logout")
async def logout(response: Response) -> dict[str, str]:
    """Cierra la sesión eliminando la cookie access_token."""
    response.delete_cookie(ACCESS_TOKEN_COOKIE)
    return {"detail": "Sesión cerrada"}

