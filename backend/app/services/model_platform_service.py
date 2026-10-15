"""平台配置（接口地址）、官方接入目录与旧模型配置迁移。

平台不再保存可用的共享密钥：密钥由每个账号自己填（见 ``api_key_service``）。
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import HTTPException

from ..models import (
    PROVIDERS, ModelApiConfig, ModelPlatformConfig, ModelPlatformCreate,
    ModelPlatformPublic, ModelPlatformUpdate, User, utcnow,
)
from ..store import store
from . import api_key_service, audit_service


@lru_cache(maxsize=1)
def catalog() -> list[dict[str, Any]]:
    return json.loads((Path(__file__).parents[1] / "resources/model_platforms.json").read_text())


def spec(vendor: str) -> dict[str, Any]:
    return next(item for item in catalog() if item["id"] == vendor)


SHARED_KEY_GONE = "平台不再配置统一的 API Key，请每个账号在「个人设置 → 我的 API Key」中填写自己的密钥"


def public(item: ModelPlatformConfig, user_id: str | None = None) -> ModelPlatformPublic:
    """``has_api_key`` / ``api_key_masked`` 指的是 ``user_id`` 这个账号自己的密钥。"""
    key = api_key_service.get_key(user_id, item.id)
    return ModelPlatformPublic(
        **item.model_dump(exclude={"api_key", "created_at", "created_by", "presets_seeded"}),
        has_api_key=bool(key), api_key_masked=api_key_service.mask_key(key),
    )


def get(platform_id: str) -> ModelPlatformConfig:
    item = store.model_platforms.get(platform_id)
    if item is None:
        raise HTTPException(404, "平台配置不存在")
    return item


def _endpoints(values: dict[str, str]) -> dict[str, str]:
    result = {key: value.strip().rstrip("/") for key, value in values.items() if value.strip()}
    for value in result.values():
        parsed = urlsplit(value)
        if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise HTTPException(400, "接口地址必须是 HTTP(S) Base URL，不能包含密钥、查询参数或片段")
    return result


def create(payload: ModelPlatformCreate, *, actor: User) -> ModelPlatformConfig:
    if payload.access_mode != "standard":
        raise HTTPException(400, "此处使用普通 API；专属地址请在高级设置中填写")
    if payload.api_key.strip():
        raise HTTPException(400, SHARED_KEY_GONE)
    item = ModelPlatformConfig(
        vendor=payload.vendor, name=payload.name.strip() or spec(payload.vendor)["label"],
        enabled=payload.enabled,
        endpoints=_endpoints(payload.endpoints if payload.endpoints is not None else spec(payload.vendor)["endpoints"]),
        created_by=actor.email, updated_by=actor.email,
    )
    store.model_platforms.put(item)
    seed_models(item)
    audit_service.record("model_platform.create", actor=actor, target_type="model_platform", target_id=item.id, summary=f"新增平台「{item.name}」")
    return item


def update(platform_id: str, payload: ModelPlatformUpdate, *, actor: User) -> ModelPlatformConfig:
    item = get(platform_id).model_copy(deep=True)
    if payload.api_key:
        raise HTTPException(400, SHARED_KEY_GONE)
    if payload.name is not None:
        if not payload.name.strip():
            raise HTTPException(400, "平台名称不能为空")
        item.name = payload.name.strip()
    if payload.endpoints is not None:
        item.endpoints = _endpoints(payload.endpoints)
    if payload.enabled is not None:
        item.enabled = payload.enabled
    item.updated_at, item.updated_by = utcnow(), actor.email
    store.model_platforms.put(item)
    audit_service.record("model_platform.update", actor=actor, target_type="model_platform", target_id=item.id, summary=f"更新平台「{item.name}」")
    return item


def delete(platform_id: str, *, actor: User) -> None:
    item = get(platform_id)
    if store.model_apis.first(lambda row: row.platform_id == item.id):
        raise HTTPException(409, "请先删除该平台下的模型，或停用平台")
    store.model_platforms.delete(item.id)
    api_key_service.purge_target(item.id)
    audit_service.record("model_platform.delete", actor=actor, target_type="model_platform", target_id=item.id, summary=f"删除平台「{item.name}」")


def seed_models(platform: ModelPlatformConfig) -> int:
    existing = {row.model_id for row in store.model_apis.all() if row.platform_id == platform.id}
    created = 0
    for preset in spec(platform.vendor)["models"]:
        if preset["model"] in existing:
            continue
        protocols = [p for p in preset["protocols"] if p in platform.endpoints]
        store.model_apis.put(ModelApiConfig(
            name=preset["model"], model=preset["model"], platform_id=platform.id,
            model_is_server_id=True,
            provider="openai" if "openai" in protocols else "anthropic",
            supported_protocols=protocols, created_by="system", updated_by="system",
        ))
        created += 1
    platform = platform.model_copy(deep=True)
    platform.presets_seeded = True
    store.model_platforms.put(platform)
    return created


def _legacy_endpoints(item: ModelApiConfig) -> tuple[str, dict[str, str]]:
    """只为文档确认的官方路径推导另一个协议；中转/非标准路径原样保留。"""
    url = item.base_url.strip().rstrip("/")
    parsed = urlsplit(url)
    root = f"{parsed.scheme}://{parsed.netloc}"
    host, path = parsed.hostname or "", parsed.path
    vendor = "custom"
    endpoints = {"anthropic" if item.provider == "anthropic" else "openai": url}
    if host.endswith(".aliyuncs.com") and path in {"/apps/anthropic", "/compatible-mode/v1"}:
        vendor = "alibaba"
        endpoints = {"openai": root + "/compatible-mode/v1", "responses": root + "/compatible-mode/v1", "anthropic": root + "/apps/anthropic"}
    elif host == "api.deepseek.com" and path in {"", "/v1", "/anthropic"}:
        vendor, endpoints = "deepseek", dict(spec("deepseek")["endpoints"])
    elif host == "api.atlascloud.ai" and path in {"", "/v1"}:
        vendor, endpoints = "atlascloud", dict(spec("atlascloud")["endpoints"])
    elif host == "tokenhub.tencentmaas.com" and path in {"", "/v1"}:
        vendor, endpoints = "tencent", dict(spec("tencent")["endpoints"])
    elif host == "ark.cn-beijing.volces.com" and path in {"/api/v3", "/api/compatible"}:
        vendor, endpoints = "volcengine", dict(spec("volcengine")["endpoints"])
    if not url:
        endpoints = {"anthropic" if item.provider == "anthropic" else "openai": str(PROVIDERS[item.provider]["default_base_url"])}
        if item.provider == "anthropic":
            endpoints["anthropic"] = "https://api.anthropic.com"
    # 已有配置的 URL（含 /v1、地区、专属业务空间）始终保留。
    if url:
        endpoints["anthropic" if item.provider == "anthropic" else "openai"] = url
    return vendor, endpoints


def seed_and_migrate() -> int:
    created = 0
    for old in store.model_apis.all():
        if old.platform_id:
            continue
        vendor, endpoints = _legacy_endpoints(old)
        if old.provider == "openai" and vendor == "custom":
            endpoints["responses"] = endpoints["openai"]
        platform = store.model_platforms.first(lambda row: row.vendor == vendor and row.api_key == old.api_key and row.endpoints == endpoints)
        if platform is None:
            platform = ModelPlatformConfig(
                vendor=vendor, name=spec(vendor)["label"] + (f" · {urlsplit(next(iter(endpoints.values()))).hostname or old.provider}" if vendor == "custom" else ""),
                endpoints=endpoints, api_key=old.api_key, created_by=old.created_by, updated_by=old.updated_by,
            )
            store.model_platforms.put(platform)
        migrated = old.model_copy(deep=True)
        migrated.platform_id, migrated.api_key = platform.id, ""
        preset = next((p for p in spec(vendor)["models"] if p["model"] == old.model_id), None)
        migrated.model_is_server_id = bool(preset) or vendor in {"atlascloud", "tencent"}
        if not migrated.supported_protocols:
            migrated.supported_protocols = preset["protocols"] if preset else (["anthropic"] if old.provider == "anthropic" else ["openai", "responses"] if old.provider == "openai" else ["openai"] if old.provider in {"dashscope", "deepseek", "openrouter"} else [])
        # 旧 OpenAI 兼容配置此前已用于 Codex；保留 Responses 入口。
        if "responses" in migrated.supported_protocols and "responses" not in platform.endpoints:
            platform = platform.model_copy(deep=True)
            platform.endpoints["responses"] = platform.endpoints["openai"]
            store.model_platforms.put(platform)
        store.model_apis.put(migrated)
    for entry in catalog():
        if entry["id"] == "custom":
            continue
        platforms = store.model_platforms.find(lambda row: row.vendor == entry["id"])
        if not platforms:
            platform = ModelPlatformConfig(vendor=entry["id"], name=entry["label"], endpoints=entry["endpoints"], created_by="system", updated_by="system")
            store.model_platforms.put(platform)
            platforms = [platform]
        for platform in platforms:
            if platform.presets_seeded:
                continue
            # 首次从旧配置迁移的官方账号补全预置模型；后续不复活用户删除的模型。
            created += seed_models(platform)
    return created
