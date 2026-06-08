from fastapi import Depends, HTTPException, status, Request, Cookie
from typing import Optional
from auth.jwt_utils import verify_access_token

async def get_current_user(
    access_token: Optional[str] = Cookie(None),
    authorization: Optional[str] = None
) -> dict:
    """
    Dependency для получения текущего авторизованного пользователя.
    Поддерживает как cookies, так и Authorization header.
    """
    token = None
    
    # Приоритет 1: Cookie (для браузеров)
    if access_token:
        token = access_token
    # Приоритет 2: Authorization header (для мобильных приложений)
    elif authorization:
        try:
            scheme, token = authorization.split(" ")
            if scheme.lower() != "bearer":
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Invalid authorization scheme"
                )
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid authorization header"
            )
    
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated"
        )
    
    payload = verify_access_token(token)
    
    if not payload:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token"
        )
    
    return {
        "tg_id": int(payload["sub"]),
        "username": payload.get("username")
    }

async def get_optional_user(
    access_token: Optional[str] = Cookie(None),
    authorization: Optional[str] = None
) -> dict | None:
    """
    Dependency для опциональной авторизации.
    Возвращает None если пользователь не авторизован.
    """
    try:
        return await get_current_user(access_token, authorization)
    except HTTPException:
        return None