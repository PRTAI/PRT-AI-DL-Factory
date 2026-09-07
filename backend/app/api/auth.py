"""Login / session endpoints. Accounts are provisioned by admins only."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status

from ..deps import CurrentUser
from ..models import LoginRequest, PasswordChange, TokenResponse, UserPublic
from ..security import create_access_token, verify_password
from ..services import audit_service, user_service
from ..deps import client_ip

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=TokenResponse, summary="邮箱密码登录")
def login(payload: LoginRequest, request: Request) -> TokenResponse:
    user = user_service.get_by_email(str(payload.email))
    if user is None or not verify_password(payload.password, user.password_hash):
        audit_service.record(
            "auth.login_failed",
            target_type="user",
            target_id=str(payload.email),
            summary=f"登录失败: {payload.email}",
            ip=client_ip(request),
        )
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="邮箱或密码不正确")
    if not user.active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="账号已停用，请联系管理员")

    token, expires_in = create_access_token(user.id, role=user.role.value, email=user.email)
    user = user_service.touch_login(user)
    audit_service.record(
        "auth.login",
        actor=user,
        target_type="user",
        target_id=user.id,
        summary=f"{user.email} 登录",
        ip=client_ip(request),
    )
    return TokenResponse(access_token=token, expires_in=expires_in, user=UserPublic.of(user))


@router.get("/me", response_model=UserPublic, summary="当前登录用户")
def me(user: CurrentUser) -> UserPublic:
    return UserPublic.of(user)


@router.post("/change-password", response_model=UserPublic, summary="修改自己的密码")
def change_password(payload: PasswordChange, user: CurrentUser) -> UserPublic:
    updated = user_service.change_own_password(user, payload.current_password, payload.new_password)
    return UserPublic.of(updated)


@router.post("/logout", summary="登出（前端丢弃 token 即可）")
def logout(user: CurrentUser, request: Request) -> dict[str, bool]:
    audit_service.record(
        "auth.logout", actor=user, target_type="user", target_id=user.id, summary=f"{user.email} 登出", ip=client_ip(request)
    )
    return {"ok": True}
