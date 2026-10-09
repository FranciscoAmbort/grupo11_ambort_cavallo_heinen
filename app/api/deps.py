from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_session
from app.models import User

JWT_ALGORITHM = "HS256"
ACCESS_TOKEN_COOKIE = "access_token"


def create_access_token(user_id: int, expires_minutes: int | None = None) -> str:
    """Genera un JWT firmado (HS256) cuyo `sub` es el id del usuario."""
    minutes = expires_minutes if expires_minutes is not None else settings.access_token_expire_minutes
    now = datetime.now(timezone.utc)
    payload = {"sub": str(user_id), "iat": now, "exp": now + timedelta(minutes=minutes)}
    return jwt.encode(payload, settings.secret_key, algorithm=JWT_ALGORITHM)


def decode_access_token(token: str) -> dict:
    """Decodifica y valida firma y vigencia del JWT. Lanza `jwt.PyJWTError` si es inválido."""
    return jwt.decode(
        token,
        settings.secret_key,
        algorithms=[JWT_ALGORITHM],
        options={"require": ["exp", "sub"]},
    )


def _extract_token(request: Request) -> str | None:
    """Obtiene el token de la cookie `access_token` o, si falta, del header Bearer."""
    token = request.cookies.get(ACCESS_TOKEN_COOKIE)
    if token:
        return token
    authorization = request.headers.get("Authorization", "")
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() == "bearer" and value.strip():
        return value.strip()
    return None


async def get_current_user(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> User:
    """Resuelve el usuario autenticado a partir del JWT; responde 401 si algo falla."""
    unauthorized = HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="No autenticado")

    token = _extract_token(request)
    if not token:
        raise unauthorized

    try:
        payload = decode_access_token(token)
        user_id = int(payload["sub"])
    except (jwt.PyJWTError, ValueError, KeyError) as exc:
        raise unauthorized from exc

    user = await session.scalar(select(User).where(User.id == user_id))
    if user is None:
        raise unauthorized
    return user

