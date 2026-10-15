"""每个账号自己的模型 API Key。

平台（管理员）只维护「有哪些平台、接口地址、模型」，不再提供统一的密钥；
谁提交任务 / 样例审核，就用谁在「个人设置 → 我的 API Key」里填的密钥。
密钥按平台配置一份（同平台下的模型共用），未挂平台的旧模型配置按模型配置一份。
"""

from __future__ import annotations

from fastapi import HTTPException, status

from ..models import ModelApiConfig, ModelPlatformConfig, User, UserApiKey, UserApiKeyPublic, utcnow
from ..store import store
from . import audit_service


def mask_key(key: str) -> str:
    key = key.strip()
    return (f"{key[:3]}…{key[-4:]}" if len(key) > 10 else "…" + key[-2:]) if key else ""


def target_of(item: ModelApiConfig) -> str:
    return item.platform_id or item.id


def get_key(user_id: str | None, target_id: str) -> str:
    if not user_id or not target_id:
        return ""
    row = store.user_api_keys.get(UserApiKey.key_id(user_id, target_id))
    return row.api_key.strip() if row else ""


def key_for(item: ModelApiConfig, user_id: str | None) -> str:
    return get_key(user_id, target_of(item))


def keys_of_user(user_id: str) -> dict[str, UserApiKey]:
    return {row.target_id: row for row in store.user_api_keys.find(lambda r: r.user_id == user_id)}


def _target_name(target_id: str) -> str:
    platform = store.model_platforms.get(target_id)
    if platform is not None:
        return platform.name
    model = store.model_apis.get(target_id)
    if model is not None and not model.platform_id:
        return model.name
    raise HTTPException(status.HTTP_404_NOT_FOUND, detail="平台或模型配置不存在")


def list_mine(user: User) -> list[UserApiKeyPublic]:
    # 延迟导入：model_platform_service 反过来要用这里的 key_for/mask_key
    from . import model_platform_service

    mine = keys_of_user(user.id)
    models = store.model_apis.all()
    rows: list[UserApiKeyPublic] = []
    for platform in store.model_platforms.all():
        row = mine.get(platform.id)
        rows.append(UserApiKeyPublic(
            target_id=platform.id, kind="platform", name=platform.name, vendor=platform.vendor,
            vendor_label=model_platform_service.spec(platform.vendor)["label"],
            endpoints=dict(platform.endpoints),
            models=sorted(m.name for m in models if m.platform_id == platform.id and m.enabled),
            enabled=platform.enabled, has_api_key=bool(row and row.api_key.strip()),
            api_key_masked=mask_key(row.api_key) if row else "", updated_at=row.updated_at if row else None,
        ))
    for model in models:
        if model.platform_id:
            continue
        row = mine.get(model.id)
        rows.append(UserApiKeyPublic(
            target_id=model.id, kind="model", name=model.name, vendor_label="自定义配置",
            models=[model.name], enabled=model.enabled, has_api_key=bool(row and row.api_key.strip()),
            api_key_masked=mask_key(row.api_key) if row else "", updated_at=row.updated_at if row else None,
        ))
    # 已配置的排前面，其次是启用的
    rows.sort(key=lambda r: (not r.has_api_key, not r.enabled, r.vendor_label, r.name.lower()))
    return rows


def set_key(user: User, target_id: str, api_key: str) -> UserApiKeyPublic:
    name = _target_name(target_id)
    key = api_key.strip()
    if not key:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="API Key 不能为空")
    key_id = UserApiKey.key_id(user.id, target_id)
    existing = store.user_api_keys.get(key_id)
    row = existing.model_copy(deep=True) if existing else UserApiKey(id=key_id, user_id=user.id, target_id=target_id)
    row.api_key, row.updated_at = key, utcnow()
    store.user_api_keys.put(row)
    audit_service.record(
        "api_key.set", actor=user, target_type="api_key", target_id=target_id,
        summary=f"{'更新' if existing else '配置'}了「{name}」的个人 API Key",
    )
    return next(r for r in list_mine(user) if r.target_id == target_id)


def delete_key(user: User, target_id: str) -> None:
    if store.user_api_keys.delete(UserApiKey.key_id(user.id, target_id)):
        audit_service.record(
            "api_key.delete", actor=user, target_type="api_key", target_id=target_id,
            summary="清除了个人 API Key",
        )


def purge_target(target_id: str) -> None:
    """平台 / 模型配置被删除时，顺带删掉所有人在它上面的密钥。"""
    for row in store.user_api_keys.find(lambda r: r.target_id == target_id):
        store.user_api_keys.delete(row.id)


def purge_user(user_id: str) -> None:
    for row in store.user_api_keys.find(lambda r: r.user_id == user_id):
        store.user_api_keys.delete(row.id)


def migrate_shared_keys() -> int:
    """把旧版的平台共享密钥转成「填写它的那个账号」的个人密钥，然后从平台上清掉。

    找不到对应账号（例如 system 预置）时归到最早的管理员名下，不直接丢弃；
    之后其他账号必须自己填密钥才能使用。
    """
    users = store.users.all()
    by_email = {u.email.lower(): u for u in users}
    admins = sorted((u for u in users if u.is_admin), key=lambda u: u.created_at)
    moved = 0
    sources = [*store.model_platforms.all(), *(m for m in store.model_apis.all() if not m.platform_id)]
    for item in sources:
        key = item.api_key.strip()
        if not key:
            continue
        owner = by_email.get(item.updated_by.lower()) or by_email.get(item.created_by.lower()) or (admins[0] if admins else None)
        if owner is not None and not get_key(owner.id, item.id):
            store.user_api_keys.put(UserApiKey(id=UserApiKey.key_id(owner.id, item.id), user_id=owner.id, target_id=item.id, api_key=key))
            moved += 1
        cleared = item.model_copy(deep=True)
        cleared.api_key = ""
        (store.model_platforms if isinstance(item, ModelPlatformConfig) else store.model_apis).put(cleared)
        audit_service.record(
            "api_key.migrate", target_type="api_key", target_id=item.id,
            summary=f"「{item.name}」的共享 API Key 已转为 {owner.email if owner else '（无账号）'} 的个人密钥",
        )
    return moved
