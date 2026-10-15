"""Account lifecycle. Registration is admin-driven: there is no self sign-up."""

from __future__ import annotations

from fastapi import HTTPException, status

from ..config import settings
from ..models import Role, User, UserCreate, UserUpdate, utcnow
from ..security import hash_password, verify_password
from ..store import store
from . import api_key_service, audit_service


def normalise_email(email: str) -> str:
    return email.strip().lower()


def get_by_email(email: str) -> User | None:
    target = normalise_email(email)
    return store.users.first(lambda u: u.email.lower() == target)


def list_users() -> list[User]:
    users = store.users.all()
    users.sort(key=lambda u: (u.role != Role.ADMIN, u.created_at))
    return users


def create_user(payload: UserCreate, *, actor: User | None = None) -> User:
    email = normalise_email(str(payload.email))
    if get_by_email(email) is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=f"邮箱 {email} 已存在")
    user = User(
        email=email,
        display_name=payload.display_name.strip() or email.split("@")[0],
        role=payload.role,
        password_hash=hash_password(payload.password),
        must_change_password=payload.must_change_password,
        created_by=actor.id if actor else "system",
        max_concurrent_jobs=payload.max_concurrent_jobs,
        max_gpus_per_job=payload.max_gpus_per_job,
        note=payload.note,
    )
    store.users.put(user)
    audit_service.record(
        "user.create",
        actor=actor,
        target_type="user",
        target_id=user.id,
        summary=f"创建账号 {user.email} ({user.role.value})",
    )
    audit_service.notify(
        user.id,
        "欢迎使用 PRT AI DL Factory",
        body="账号由管理员创建，请尽快修改初始密码。",
        level="info",
        link="/settings",
    )
    return user


def update_user(user_id: str, payload: UserUpdate, *, actor: User) -> User:
    target = store.users.get(user_id)
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="账号不存在")
    if target.role == Role.ADMIN and payload.role == Role.USER and _admin_count() <= 1:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="至少需要保留一个管理员")
    if target.id == actor.id and payload.active is False:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="不能停用自己的账号")

    def mutate(user: User) -> None:
        data = payload.model_dump(exclude_unset=True, exclude_none=True)
        for key, value in data.items():
            setattr(user, key, value)
        user.updated_at = utcnow()

    updated = store.users.update(user_id, mutate)
    assert updated is not None
    audit_service.record(
        "user.update",
        actor=actor,
        target_type="user",
        target_id=user_id,
        summary=f"更新账号 {updated.email}",
        detail=payload.model_dump(exclude_unset=True),
    )
    return updated


def reset_password(user_id: str, new_password: str, must_change: bool, *, actor: User) -> User:
    target = store.users.get(user_id)
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="账号不存在")

    def mutate(user: User) -> None:
        user.password_hash = hash_password(new_password)
        user.must_change_password = must_change
        user.updated_at = utcnow()

    updated = store.users.update(user_id, mutate)
    assert updated is not None
    audit_service.record(
        "user.reset_password",
        actor=actor,
        target_type="user",
        target_id=user_id,
        summary=f"重置 {updated.email} 的密码",
    )
    audit_service.notify(user_id, "密码已被管理员重置", level="warning", link="/settings")
    return updated


def change_own_password(user: User, current_password: str, new_password: str) -> User:
    if not verify_password(current_password, user.password_hash):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="当前密码不正确")
    if current_password == new_password:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="新密码不能与当前密码相同")

    def mutate(target: User) -> None:
        target.password_hash = hash_password(new_password)
        target.must_change_password = False
        target.updated_at = utcnow()

    updated = store.users.update(user.id, mutate)
    assert updated is not None
    audit_service.record(
        "user.change_password", actor=user, target_type="user", target_id=user.id, summary="修改自身密码"
    )
    return updated


def delete_user(user_id: str, *, actor: User) -> None:
    target = store.users.get(user_id)
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="账号不存在")
    if target.id == actor.id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="不能删除自己的账号")
    if target.role == Role.ADMIN and _admin_count() <= 1:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="至少需要保留一个管理员")
    active_jobs = store.jobs.count(lambda j: j.owner_id == user_id and j.status.is_active)
    if active_jobs:
        raise HTTPException(
            status.HTTP_409_CONFLICT, detail=f"该用户还有 {active_jobs} 个进行中的任务，无法删除"
        )
    store.users.delete(user_id)
    api_key_service.purge_user(user_id)
    audit_service.record(
        "user.delete",
        actor=actor,
        target_type="user",
        target_id=user_id,
        summary=f"删除账号 {target.email}",
    )


def touch_login(user: User) -> User:
    def mutate(target: User) -> None:
        target.last_login_at = utcnow()

    updated = store.users.update(user.id, mutate)
    return updated or user


def _admin_count() -> int:
    return store.users.count(lambda u: u.role == Role.ADMIN and u.active)


def ensure_bootstrap_admin() -> User | None:
    """Create the first administrator on a fresh data directory."""
    if store.users.count() > 0:
        return None
    payload = UserCreate(
        email=settings.bootstrap_admin_email,
        display_name="平台管理员",
        password=settings.bootstrap_admin_password,
        role=Role.ADMIN,
        must_change_password=True,
        note="首次启动自动创建，请立即修改密码",
    )
    return create_user(payload, actor=None)
