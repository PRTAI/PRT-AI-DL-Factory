"""模型 API 配置：全员可读（提交评测要选），管理员可改；密钥每个账号自己配，只返回尾号。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from ..deps import AdminUser, CurrentUser
from ..models import ModelApiCreate, ModelApiPublic, ModelApiUpdate
from ..models import ModelPlatformCreate, ModelPlatformPublic, ModelPlatformUpdate, UserApiKeyPublic, UserApiKeySet
from ..services import api_key_service, model_api_service, model_platform_service
from ..store import store

router = APIRouter(prefix="/model-apis", tags=["model-apis"])


@router.get("", response_model=list[ModelApiPublic], summary="模型 API 配置列表")
def list_model_apis(user: CurrentUser) -> list[ModelApiPublic]:
    return model_api_service.list_public(user.id)


@router.get("/providers", summary="支持的厂家/协议与它们的标准环境变量")
def providers(_: CurrentUser) -> list[dict[str, Any]]:
    return model_api_service.providers()


@router.get("/platform-catalog")
def platform_catalog(_: CurrentUser) -> list[dict[str, Any]]:
    return model_platform_service.catalog()


@router.get("/platforms", response_model=list[ModelPlatformPublic])
def platforms(user: CurrentUser) -> list[ModelPlatformPublic]:
    return [model_platform_service.public(item, user.id) for item in store.model_platforms.all()]


@router.get("/my-keys", response_model=list[UserApiKeyPublic], summary="我的 API Key（按平台）")
def my_keys(user: CurrentUser) -> list[UserApiKeyPublic]:
    return api_key_service.list_mine(user)


@router.put("/my-keys/{target_id}", response_model=UserApiKeyPublic, summary="设置我在某平台的 API Key")
def set_my_key(target_id: str, payload: UserApiKeySet, user: CurrentUser) -> UserApiKeyPublic:
    return api_key_service.set_key(user, target_id, payload.api_key)


@router.delete("/my-keys/{target_id}", status_code=204, summary="清除我在某平台的 API Key")
def delete_my_key(target_id: str, user: CurrentUser) -> None:
    api_key_service.delete_key(user, target_id)


@router.post("/platforms", response_model=ModelPlatformPublic, status_code=201)
def create_platform(payload: ModelPlatformCreate, admin: AdminUser) -> ModelPlatformPublic:
    return model_platform_service.public(model_platform_service.create(payload, actor=admin), admin.id)


@router.patch("/platforms/{platform_id}", response_model=ModelPlatformPublic)
def update_platform(platform_id: str, payload: ModelPlatformUpdate, admin: AdminUser) -> ModelPlatformPublic:
    return model_platform_service.public(model_platform_service.update(platform_id, payload, actor=admin), admin.id)


@router.delete("/platforms/{platform_id}", status_code=204)
def delete_platform(platform_id: str, admin: AdminUser) -> None:
    model_platform_service.delete(platform_id, actor=admin)


@router.post("", response_model=ModelApiPublic, status_code=201, summary="新增（管理员）")
def create(payload: ModelApiCreate, admin: AdminUser) -> ModelApiPublic:
    return model_api_service.public(model_api_service.create(payload, actor=admin), admin.id)


@router.patch("/{config_id}", response_model=ModelApiPublic, summary="修改（管理员）")
def update(config_id: str, payload: ModelApiUpdate, admin: AdminUser) -> ModelApiPublic:
    return model_api_service.public(model_api_service.update(config_id, payload, actor=admin), admin.id)


@router.delete("/{config_id}", status_code=204, summary="删除（管理员）")
def delete(config_id: str, admin: AdminUser) -> None:
    model_api_service.delete(config_id, actor=admin)
