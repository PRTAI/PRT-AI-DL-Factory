"""模型 API 配置的增删改查，以及「选了某个配置 → 该给 Harbor 注入什么」。

可见性：所有人都能看列表（提交评测要选），只有管理员能改。
平台不提供统一密钥：每个账号用自己在「个人设置 → 我的 API Key」里填的密钥
（``api_key_service``），所以 ``effective`` / ``resolve`` 都要带上是谁在用。
密钥只在两处出现：db 文件（0600）和子进程环境；接口返回永远是尾号。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from fastapi import HTTPException, status

from ..models import (
    EFFORT_ORDER,
    PROVIDERS,
    ModelApiConfig,
    ModelApiCreate,
    ModelApiPublic,
    ModelApiUpdate,
    User,
    utcnow,
)
from ..store import store
from . import api_key_service, audit_service, model_platform_service

# 首批要有的四个模型。provider 是「协议」：中转站基本都走 OpenAI 兼容，
# Claude 系走 Anthropic 协议（claude-code 只认 ANTHROPIC_*）。
# 密钥由每个账号自己填；没填之前提交页里灰着，不会让人拿去跑。
DEFAULT_MODELS: list[dict[str, Any]] = [
    {
        "name": "gpt-5.6-sol",
        "provider": "openai",
        "note": "OpenAI 系。走中转站的话把 base_url 换成中转站的 /v1",
    },
    {
        "name": "claude-opus-4-8",
        "provider": "anthropic",
        "note": "Anthropic 协议；claude-code agent 只认这一档的环境变量",
    },
    {
        "name": "claude-opus-5",
        "provider": "anthropic",
        "note": "Anthropic 协议；claude-code agent 只认这一档的环境变量",
    },
    {
        "name": "qwen3.8-max",
        "provider": "openai",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "note": "通义千问，默认走百炼的 OpenAI 兼容模式；用中转站就改 base_url",
    },
]


def seed_defaults() -> int:
    """初始化预置目录，并迁移旧配置；不复活用户已删除的模型。"""
    existing = {item.name for item in store.model_apis.all()}
    created = 0
    for spec in DEFAULT_MODELS if not existing and not store.model_platforms.count() else []:
        if spec["name"] in existing:
            continue
        store.model_apis.put(ModelApiConfig(created_by="system", updated_by="system", **spec))
        created += 1
    created += model_platform_service.seed_and_migrate()
    api_key_service.migrate_shared_keys()
    return created


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #
def list_public(user_id: str, *, include_disabled: bool = True) -> list[ModelApiPublic]:
    items = store.model_apis.all()
    if not include_disabled:
        items = [i for i in items if i.enabled]
    rows = [public(i, user_id) for i in items]
    rows.sort(key=lambda i: (not i.ready, i.vendor, i.name.lower()))
    return rows


def protocols(item: ModelApiConfig) -> list[str]:
    if item.supported_protocols or item.platform_id:
        return list(item.supported_protocols)
    return ["anthropic"] if item.provider == "anthropic" else ["openai", "responses"] if item.provider == "openai" else ["openai"] if item.provider in {"dashscope", "deepseek", "openrouter"} else []


def effective(item: ModelApiConfig, protocol: str | None = None, *, user_id: str | None = None) -> ModelApiConfig:
    """合并平台地址并填入 ``user_id`` 自己的密钥；不带 user_id 时密钥为空。"""
    resolved = item.model_copy(deep=True)
    resolved.api_key = api_key_service.key_for(item, user_id)
    if protocol and protocol not in protocols(item):
        raise HTTPException(400, f"模型「{item.name}」不支持所选接口 {protocol}")
    if item.platform_id:
        platform = model_platform_service.get(item.platform_id)
        selected = protocol or ("anthropic" if item.provider == "anthropic" else "openai")
        if not protocol and protocols(item) and selected not in protocols(item):
            selected = protocols(item)[0]
            resolved.provider = "anthropic" if selected == "anthropic" else "openai"
        if protocol and not platform.endpoints.get(selected):
            raise HTTPException(400, f"平台「{platform.name}」未配置所选接口地址 {protocol}")
        resolved.base_url = platform.endpoints.get(selected, item.base_url)
        resolved.enabled = item.enabled and platform.enabled
    if protocol:
        resolved.provider = "anthropic" if protocol == "anthropic" else "openai"
    elif item.platform_id and item.provider in {"dashscope", "deepseek", "openrouter"}:
        resolved.provider = "openai"
    return resolved


def public(item: ModelApiConfig, user_id: str | None = None) -> ModelApiPublic:
    """``has_api_key`` / ``ready`` 针对 ``user_id`` 这个账号。"""
    result = ModelApiPublic.of(effective(item, user_id=user_id))
    result.platform_id = item.platform_id
    result.supported_protocols = protocols(item)
    if item.platform_id:
        platform = model_platform_service.get(item.platform_id)
        result.platform_name, result.vendor = platform.name, platform.vendor
        result.vendor_label = model_platform_service.spec(platform.vendor)["label"]
        result.endpoints = dict(platform.endpoints)
        result.supported_protocols = [p for p in result.supported_protocols if result.endpoints.get(p)]
    else:
        result.platform_name = "自定义配置"
        result.endpoints = {p: item.base_url or str(PROVIDERS[item.provider]["default_base_url"]) for p in result.supported_protocols}
    return result


def agent_protocol(agent: str, requested: str = "") -> str | None:
    forced = "anthropic" if agent == "claude-code" else "responses" if agent == "codex" else None
    if forced and requested and requested != ("openai" if forced == "responses" else forced):
        raise HTTPException(400, f"{agent} 必须使用 {'OpenAI' if forced == 'responses' else 'Anthropic'} 接口")
    return forced or requested or None


def get(config_id: str) -> ModelApiConfig:
    item = store.model_apis.get(config_id)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="模型配置不存在")
    return item


def providers() -> list[dict[str, Any]]:
    """给前端下拉框：provider 标识、显示名、会注入的变量名、默认 base_url。"""
    return [
        {
            "id": key,
            "label": spec["label"],
            "api_key_envs": spec["api_key_envs"],
            "base_url_envs": spec["base_url_envs"],
            "default_base_url": spec["default_base_url"],
            "hint": spec["hint"],
        }
        for key, spec in PROVIDERS.items()
    ]


# --------------------------------------------------------------------------- #
# Writes (admin)
# --------------------------------------------------------------------------- #
def create(payload: ModelApiCreate, *, actor: User) -> ModelApiConfig:
    name = payload.name.strip()
    if not name:
        raise HTTPException(400, "模型名称不能为空")
    if store.model_apis.first(lambda i: i.name.lower() == name.lower() and (not payload.platform_id or i.platform_id == payload.platform_id)):
        raise HTTPException(status.HTTP_409_CONFLICT, detail=f"已存在同名模型配置：{name}")
    if payload.api_key.strip():
        raise HTTPException(400, model_platform_service.SHARED_KEY_GONE)
    supported = payload.supported_protocols
    if payload.platform_id:
        platform = model_platform_service.get(payload.platform_id)
        if payload.base_url or payload.extra_env:
            raise HTTPException(400, "平台模型的接口地址请在平台配置中修改")
        preset = next((p for p in model_platform_service.spec(platform.vendor)["models"] if p["model"] == (payload.model.strip() or name)), None)
        supported = supported if supported is not None else (preset["protocols"] if preset else list(platform.endpoints))
        if not supported:
            raise HTTPException(400, "请先为平台配置接口地址")
    item = ModelApiConfig(
        name=name,
        platform_id=payload.platform_id,
        supported_protocols=supported or [],
        provider=("openai" if "openai" in (supported or []) else "anthropic") if payload.platform_id else payload.provider,
        model=payload.model.strip(),
        model_is_server_id=bool(payload.platform_id),
        base_url=payload.base_url.strip(),
        extra_env={k.strip(): v for k, v in payload.extra_env.items() if k.strip()},
        note=payload.note,
        enabled=payload.enabled,
        supports_reasoning_effort=payload.supports_reasoning_effort,
        created_by=actor.email,
        updated_by=actor.email,
    )
    store.model_apis.put(item)
    audit_service.record(
        "model_api.create",
        actor=actor,
        target_type="model_api",
        target_id=item.id,
        summary=f"新增模型配置「{item.name}」({item.provider})",
        detail={"base_url": item.base_url},
    )
    return item


def update(config_id: str, payload: ModelApiUpdate, *, actor: User) -> ModelApiConfig:
    item = get(config_id)
    data = payload.model_dump(exclude_unset=True)
    if data.get("api_key"):
        raise HTTPException(400, model_platform_service.SHARED_KEY_GONE)
    if item.platform_id and any(data.get(key) is not None for key in ("base_url", "provider")):
        raise HTTPException(400, "该模型复用平台配置，请在平台中修改接口地址")
    if "name" in data and data["name"] is not None:
        new_name = data["name"].strip()
        if not new_name:
            raise HTTPException(400, "模型名称不能为空")
        clash = store.model_apis.first(
            lambda i: i.id != item.id and i.name.lower() == new_name.lower() and (not item.platform_id or i.platform_id == item.platform_id)
        )
        if clash:
            raise HTTPException(status.HTTP_409_CONFLICT, detail=f"已存在同名模型配置：{new_name}")

    def mutate(row: ModelApiConfig) -> None:
        for key in ("name", "model", "base_url", "note"):
            if data.get(key) is not None:
                setattr(row, key, data[key].strip())
        if data.get("provider") is not None:
            row.provider = data["provider"]
        if data.get("supported_protocols") is not None:
            row.supported_protocols = data["supported_protocols"]
        if row.platform_id and data.get("model") is not None:
            row.model_is_server_id = True
        if data.get("api_key") is not None:  # 只允许清空遗留的共享密钥
            row.api_key = ""
        if data.get("extra_env") is not None:
            row.extra_env = {k.strip(): v for k, v in data["extra_env"].items() if k.strip()}
        for key in ("enabled", "supports_reasoning_effort"):
            if data.get(key) is not None:
                setattr(row, key, data[key])
        row.updated_at = utcnow()
        row.updated_by = actor.email

    updated = store.model_apis.update(item.id, mutate)
    assert updated is not None
    audit_service.record(
        "model_api.update",
        actor=actor,
        target_type="model_api",
        target_id=item.id,
        summary=f"更新模型配置「{updated.name}」",
        detail={k: v for k, v in data.items() if k != "api_key"},
    )
    return updated


def delete(config_id: str, *, actor: User) -> None:
    item = get(config_id)
    store.model_apis.delete(item.id)
    if not item.platform_id:
        api_key_service.purge_target(item.id)
    audit_service.record(
        "model_api.delete",
        actor=actor,
        target_type="model_api",
        target_id=item.id,
        summary=f"删除模型配置「{item.name}」",
    )


# --------------------------------------------------------------------------- #
# Build-time resolution
# --------------------------------------------------------------------------- #
@dataclass
class ResolvedModel:
    model: str  # 发给 --model 的字符串
    env: dict[str, str] = field(default_factory=dict)  # 走 --agent-env 注入
    secrets: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def require_ready(config_id: str, protocol: str | None = None, *, user_id: str) -> ModelApiConfig:
    """取出模型配置并确认可用（已启用且 ``user_id`` 填了自己的密钥），否则抛 400。

    调用方需要裸字段（``model_id`` / ``provider`` / ``base_url``）时用它，
    不要走 ``resolve()`` —— 后者会顺带生成一整套标准环境变量。
    """
    original = get(config_id)
    item = effective(original, user_id=user_id)
    if not item.enabled:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=f"模型配置「{item.name}」已停用")
    if not item.api_key.strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=missing_key_message(original, user_id))
    return effective(original, protocol, user_id=user_id) if protocol else item


def missing_key_message(item: ModelApiConfig, user_id: str | None) -> str:
    platform = store.model_platforms.get(item.platform_id) if item.platform_id else None
    target = f"平台「{platform.name}」" if platform else f"模型配置「{item.name}」"
    user = store.users.get(user_id) if user_id else None
    who = f"账号 {user.email} " if user else ""
    return f"{who}还没有为{target}配置自己的 API Key，请到「个人设置 → 我的 API Key」填写"


def resolve(config_id: str, *, user_id: str, model_style: str, protocol: str | None = None) -> ResolvedModel:
    """把一条模型配置翻译成 Harbor 能吃的 ``--model`` + 标准环境变量。

    ``model_style``：
    * ``litellm`` —— terminus / mini-swe-agent 这类走 litellm 的 agent，模型名要带
      ``provider/`` 前缀才知道用哪家协议；
    * ``bare``    —— codex / claude-code 这类 CLI，模型名原样传给 CLI 自己的
      ``--model``，加了前缀反而会被当成模型名的一部分发给 API。
    """
    item = require_ready(config_id, protocol, user_id=user_id)

    spec = PROVIDERS[item.provider]
    key_envs: list[str] = list(spec["api_key_envs"])  # type: ignore[arg-type]
    url_envs: list[str] = list(spec["base_url_envs"])  # type: ignore[arg-type]

    env: dict[str, str] = {}
    # 同一个 provider 可能有多个别名键（GOOGLE_API_KEY / GEMINI_API_KEY），全给上——
    # 不同 agent 先查的键不一样，只给一个就会碰上「明明配了却说没密钥」。
    for name in key_envs:
        env[name] = item.api_key.strip()
    base_url = item.base_url.strip()
    if base_url:
        for name in url_envs:
            env[name] = base_url
    env.update(item.extra_env)

    model = item.model_id
    litellm_prefix = "openai" if item.provider == "dashscope" and base_url.endswith("/v1") else item.provider
    # 聚合平台的 anthropic/xxx 等是服务器模型标识，需保留在客户端协议前缀之后。
    needs_prefix = item.model_is_server_id or "/" not in model
    if model_style == "litellm" and needs_prefix:
        model = f"{litellm_prefix}/{model}"

    notes = [
        f"模型「{item.name}」→ --model {model}；已按 Harbor 标准变量注入 "
        + " / ".join(sorted(k for k in env if k in key_envs or k in url_envs))
    ]
    return ResolvedModel(model=model, env=env, secrets=[item.api_key.strip()], notes=notes)


def clamp_effort(requested: str, allowed: list[str]) -> tuple[str, str]:
    """把用户选的思考模式收敛到 agent 接受的集合里。返回 (值, 说明)。"""
    if not requested:
        return "", ""
    if not allowed:
        return "", f"该 agent 不支持思考模式参数，已忽略「{requested}」"
    if requested in allowed:
        return requested, ""
    # 往低档找最近的（宁可少想一点，也别让 CLI 因为不认识的取值直接退出）
    order = [e for e in EFFORT_ORDER if e in allowed]
    try:
        index = EFFORT_ORDER.index(requested)
    except ValueError:
        return "", f"未知的思考模式「{requested}」，已忽略"
    for candidate in reversed(EFFORT_ORDER[: index + 1]):
        if candidate in order:
            return candidate, f"该 agent 不支持思考模式「{requested}」，已改为最接近的「{candidate}」"
    fallback = order[0]
    return fallback, f"该 agent 不支持思考模式「{requested}」，已改为「{fallback}」"
