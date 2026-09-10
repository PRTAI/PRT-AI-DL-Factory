"""模型 API 配置的增删改查，以及「选了某个配置 → 该给 Harbor 注入什么」。

可见性：所有人都能看列表（提交评测要选），只有管理员能改。
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
from . import audit_service

# 首批要有的四个模型。provider 是「协议」：中转站基本都走 OpenAI 兼容，
# Claude 系走 Anthropic 协议（claude-code 只认 ANTHROPIC_*）。
# 密钥留空 —— 管理员在平台管理页填；没密钥之前提交页里灰着，不会让人拿去跑。
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
    """首次启动时把四个默认模型放进去（已存在同名的不动）。"""
    existing = {item.name for item in store.model_apis.all()}
    created = 0
    for spec in DEFAULT_MODELS:
        if spec["name"] in existing:
            continue
        store.model_apis.put(ModelApiConfig(created_by="system", updated_by="system", **spec))
        created += 1
    return created


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #
def list_public(*, include_disabled: bool = True) -> list[ModelApiPublic]:
    items = store.model_apis.all()
    if not include_disabled:
        items = [i for i in items if i.enabled]
    items.sort(key=lambda i: (not i.ready, i.provider, i.name.lower()))
    return [ModelApiPublic.of(i) for i in items]


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
    if store.model_apis.first(lambda i: i.name.lower() == name.lower()):
        raise HTTPException(status.HTTP_409_CONFLICT, detail=f"已存在同名模型配置：{name}")
    item = ModelApiConfig(
        name=name,
        provider=payload.provider,
        model=payload.model.strip(),
        base_url=payload.base_url.strip(),
        api_key=payload.api_key.strip(),
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
        detail={"has_api_key": bool(item.api_key), "base_url": item.base_url},
    )
    return item


def update(config_id: str, payload: ModelApiUpdate, *, actor: User) -> ModelApiConfig:
    item = get(config_id)
    data = payload.model_dump(exclude_unset=True)
    if "name" in data and data["name"] is not None:
        new_name = data["name"].strip()
        clash = store.model_apis.first(
            lambda i: i.id != item.id and i.name.lower() == new_name.lower()
        )
        if clash:
            raise HTTPException(status.HTTP_409_CONFLICT, detail=f"已存在同名模型配置：{new_name}")

    def mutate(row: ModelApiConfig) -> None:
        for key in ("name", "model", "base_url", "note"):
            if data.get(key) is not None:
                setattr(row, key, data[key].strip())
        if data.get("provider") is not None:
            row.provider = data["provider"]
        # api_key: None=不改，""=清空，其他=覆盖 —— 编辑表单不回显密钥，
        # 所以「没动这一栏」必须表示保留原值。
        if data.get("api_key") is not None:
            row.api_key = data["api_key"].strip()
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
        # 审计里绝不能带密钥本身，只记「改了没」
        detail={k: ("***" if k == "api_key" else v) for k, v in data.items()},
    )
    return updated


def delete(config_id: str, *, actor: User) -> None:
    item = get(config_id)
    store.model_apis.delete(item.id)
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


def resolve(config_id: str, *, model_style: str) -> ResolvedModel:
    """把一条模型配置翻译成 Harbor 能吃的 ``--model`` + 标准环境变量。

    ``model_style``：
    * ``litellm`` —— terminus / mini-swe-agent 这类走 litellm 的 agent，模型名要带
      ``provider/`` 前缀才知道用哪家协议；
    * ``bare``    —— codex / claude-code 这类 CLI，模型名原样传给 CLI 自己的
      ``--model``，加了前缀反而会被当成模型名的一部分发给 API。
    """
    item = get(config_id)
    if not item.enabled:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=f"模型配置「{item.name}」已停用")
    if not item.api_key.strip():
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=f"模型配置「{item.name}」还没有填 API Key，请管理员在「平台管理 → 模型 API」里补上",
        )

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
    if model_style == "litellm" and "/" not in model:
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
