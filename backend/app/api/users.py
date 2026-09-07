"""Admin-only account management + per-user notifications."""

from __future__ import annotations

from fastapi import APIRouter, Query, Request

from ..deps import AdminUser, CurrentUser, client_ip
from ..models import (
    Notification,
    PasswordReset,
    UserCreate,
    UserPublic,
    UserUpdate,
)
from ..services import audit_service, user_service

router = APIRouter(tags=["users"])


@router.get("/users", response_model=list[UserPublic], summary="账号列表（管理员）")
def list_users(_: AdminUser) -> list[UserPublic]:
    return [UserPublic.of(u) for u in user_service.list_users()]


@router.post("/users", response_model=UserPublic, status_code=201, summary="创建账号（管理员）")
def create_user(payload: UserCreate, admin: AdminUser) -> UserPublic:
    return UserPublic.of(user_service.create_user(payload, actor=admin))


@router.patch("/users/{user_id}", response_model=UserPublic, summary="更新账号（管理员）")
def update_user(user_id: str, payload: UserUpdate, admin: AdminUser) -> UserPublic:
    return UserPublic.of(user_service.update_user(user_id, payload, actor=admin))


@router.post("/users/{user_id}/password", response_model=UserPublic, summary="重置密码（管理员）")
def reset_password(user_id: str, payload: PasswordReset, admin: AdminUser) -> UserPublic:
    updated = user_service.reset_password(
        user_id, payload.password, payload.must_change_password, actor=admin
    )
    return UserPublic.of(updated)


@router.delete("/users/{user_id}", status_code=204, summary="删除账号（管理员）")
def delete_user(user_id: str, admin: AdminUser) -> None:
    user_service.delete_user(user_id, actor=admin)


# --------------------------------------------------------------------------- #
# Notifications
# --------------------------------------------------------------------------- #
@router.get("/notifications", response_model=list[Notification], summary="我的通知")
def notifications(
    user: CurrentUser,
    unread_only: bool = Query(default=False),
    limit: int = Query(default=100, le=500),
) -> list[Notification]:
    return audit_service.list_for_user(user.id, unread_only=unread_only, limit=limit)


@router.post("/notifications/read", summary="标记通知已读")
def mark_read(user: CurrentUser, ids: list[str] | None = None) -> dict[str, int]:
    return {"updated": audit_service.mark_read(user.id, ids)}


# --------------------------------------------------------------------------- #
# Audit trail
# --------------------------------------------------------------------------- #
@router.get("/audit", summary="审计日志（管理员）")
def audit_log(
    admin: AdminUser,
    request: Request,
    limit: int = Query(default=200, le=1000),
    action_prefix: str = "",
    actor_id: str = "",
    target_id: str = "",
) -> list[dict]:
    del admin, request
    return audit_service.list_events(
        limit=limit,
        action_prefix=action_prefix or None,
        actor_id=actor_id or None,
        target_id=target_id or None,
    )


@router.get("/audit/mine", summary="我的操作记录")
def my_audit(user: CurrentUser, limit: int = Query(default=100, le=500)) -> list[dict]:
    return audit_service.list_events(limit=limit, actor_id=user.id)


__all__ = ["router", "client_ip"]
