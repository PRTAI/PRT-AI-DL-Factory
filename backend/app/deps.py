"""FastAPI dependencies: current user resolution and role gates."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, Query, Request, WebSocket, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .models import Role, User
from .security import decode_access_token
from .store import store

bearer_scheme = HTTPBearer(auto_error=False)


def _user_from_token(token: str | None) -> User | None:
    if not token:
        return None
    payload = decode_access_token(token)
    if not payload:
        return None
    user = store.users.get(payload.get("sub"))
    if user is None or not user.active:
        return None
    return user


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> User:
    user = _user_from_token(credentials.credentials if credentials else None)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="登录状态无效或已过期，请重新登录",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


async def get_current_admin(
    user: Annotated[User, Depends(get_current_user)],
) -> User:
    if user.role != Role.ADMIN:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="需要管理员权限")
    return user


async def get_ws_user(
    websocket: WebSocket,
    token: Annotated[str | None, Query()] = None,
) -> User | None:
    """WebSocket clients cannot set headers, so the token arrives as a query param."""
    if token is None:
        auth = websocket.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            token = auth[7:]
    return _user_from_token(token)


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else ""


CurrentUser = Annotated[User, Depends(get_current_user)]
AdminUser = Annotated[User, Depends(get_current_admin)]
