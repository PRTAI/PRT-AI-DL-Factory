"""模型 API 配置：全员可读（提交评测要选），管理员可改。密钥只返回尾号。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from ..deps import AdminUser, CurrentUser
from ..models import ModelApiCreate, ModelApiPublic, ModelApiUpdate
from ..models import ModelPlatformCreate, ModelPlatformPublic, ModelPlatformUpdate
from ..services import model_api_service, model_platform_service
from ..store import store

router = APIRouter(prefix="/model-apis", tags=["model-apis"])


@router.get("", response_model=list[ModelApiPublic], summary="模型 API 配置列表")
def list_model_apis(_: CurrentUser) -> list[ModelApiPublic]:
    return model_api_service.list_public()


@router.get("/providers", summary="支持的厂家/协议与它们的标准环境变量")
def providers(_: CurrentUser) -> list[dict[str, Any]]:
    return model_api_service.providers()


@router.get("/platform-catalog")
def platform_catalog(_: CurrentUser) -> list[dict[str, Any]]:
    return model_platform_service.catalog()


@router.get("/platforms", response_model=list[ModelPlatformPublic])
def platforms(_: CurrentUser) -> list[ModelPlatformPublic]:
    return [model_platform_service.public(item) for item in store.model_platforms.all()]


@router.post("/platforms", response_model=ModelPlatformPublic, status_code=201)
def create_platform(payload: ModelPlatformCreate, admin: AdminUser) -> ModelPlatformPublic:
    return model_platform_service.public(model_platform_service.create(payload, actor=admin))


@router.patch("/platforms/{platform_id}", response_model=ModelPlatformPublic)
def update_platform(platform_id: str, payload: ModelPlatformUpdate, admin: AdminUser) -> ModelPlatformPublic:
    return model_platform_service.public(model_platform_service.update(platform_id, payload, actor=admin))


@router.delete("/platforms/{platform_id}", status_code=204)
def delete_platform(platform_id: str, admin: AdminUser) -> None:
    model_platform_service.delete(platform_id, actor=admin)


@router.post("", response_model=ModelApiPublic, status_code=201, summary="新增（管理员）")
def create(payload: ModelApiCreate, admin: AdminUser) -> ModelApiPublic:
    return model_api_service.public(model_api_service.create(payload, actor=admin))


@router.patch("/{config_id}", response_model=ModelApiPublic, summary="修改（管理员）")
def update(config_id: str, payload: ModelApiUpdate, admin: AdminUser) -> ModelApiPublic:
    return model_api_service.public(model_api_service.update(config_id, payload, actor=admin))


@router.delete("/{config_id}", status_code=204, summary="删除（管理员）")
def delete(config_id: str, admin: AdminUser) -> None:
    model_api_service.delete(config_id, actor=admin)
