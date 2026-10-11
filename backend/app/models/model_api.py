"""模型 API 配置：厂家 / 中转站的模型端点，评测时按 Harbor 的标准环境变量注入。

Harbor 的 agent 不吃「平台自己发明的」变量名——每个 provider 读哪些环境变量写在
``harbor/agents/model_connection.py`` 的 ``PROVIDERS`` 表里（anthropic 读
``ANTHROPIC_API_KEY`` / ``ANTHROPIC_BASE_URL``，openai 读 ``OPENAI_API_KEY`` /
``OPENAI_BASE_URL``……）。这里的 ``PROVIDERS`` 是它的镜像，
``tests/test_model_apis.py`` 会去解析 Harbor 源码确认没有写错。

配置对全员可见（大家都要选模型），但只有管理员能增删改；API Key 只进
``BuildResult.secrets`` 和子进程环境，接口返回时永远是打码后的尾号。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from .common import Base, new_id, utcnow

# provider 标识 -> (API Key 变量名, Base URL 变量名们, 说明)
# 变量名以 Harbor 的 PROVIDERS 表为准，顺序也保持一致（第一个是首选键）。
PROVIDERS: dict[str, dict[str, object]] = {
    "openai": {
        "label": "OpenAI / OpenAI 兼容",
        "api_key_envs": ["OPENAI_API_KEY"],
        "base_url_envs": ["OPENAI_BASE_URL", "OPENAI_API_BASE"],
        "default_base_url": "https://api.openai.com/v1",
        "hint": "中转站（如 cf.api.fan）大多走这一档：填它的 /v1 地址就行",
    },
    "anthropic": {
        "label": "Anthropic",
        "api_key_envs": ["ANTHROPIC_API_KEY"],
        "base_url_envs": ["ANTHROPIC_BASE_URL"],
        "default_base_url": "https://api.anthropic.com/v1",
        "hint": "claude-code 走 Anthropic 协议；中转站需要暴露 /v1/messages",
    },
    "dashscope": {
        "label": "阿里云百炼 (DashScope)",
        "api_key_envs": ["DASHSCOPE_API_KEY"],
        "base_url_envs": ["DASHSCOPE_BASE_URL"],
        "default_base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "hint": "通义千问原生入口；用 OpenAI 兼容模式的话选上面的 openai 更通用",
    },
    "google": {
        "label": "Google Gemini",
        "api_key_envs": ["GOOGLE_API_KEY", "GEMINI_API_KEY"],
        "base_url_envs": ["GOOGLE_BASE_URL"],
        "default_base_url": "https://generativelanguage.googleapis.com",
        "hint": "",
    },
    "openrouter": {
        "label": "OpenRouter",
        "api_key_envs": ["OPENROUTER_API_KEY"],
        "base_url_envs": ["OPENROUTER_BASE_URL"],
        "default_base_url": "https://openrouter.ai/api/v1",
        "hint": "",
    },
    "deepseek": {
        "label": "DeepSeek",
        "api_key_envs": ["DEEPSEEK_API_KEY"],
        "base_url_envs": ["DEEPSEEK_BASE_URL"],
        "default_base_url": "https://api.deepseek.com",
        "hint": "",
    },
    "xai": {
        "label": "xAI",
        "api_key_envs": ["XAI_API_KEY"],
        "base_url_envs": ["XAI_BASE_URL"],
        "default_base_url": "https://api.x.ai/v1",
        "hint": "",
    },
    "moonshot": {
        "label": "Moonshot / Kimi",
        "api_key_envs": ["MOONSHOT_API_KEY"],
        "base_url_envs": ["MOONSHOT_BASE_URL"],
        "default_base_url": "https://api.moonshot.cn/v1",
        "hint": "",
    },
}

ProviderName = Literal[
    "openai", "anthropic", "dashscope", "google", "openrouter", "deepseek", "xai", "moonshot"
]
ApiProtocol = Literal["openai", "anthropic", "responses"]
PlatformVendor = Literal["alibaba", "tencent", "volcengine", "deepseek", "atlascloud", "custom"]


class ModelPlatformConfig(Base):
    id: str = Field(default_factory=lambda: new_id("plt"))
    vendor: PlatformVendor
    name: str
    access_mode: str = "standard"
    api_key: str = ""
    endpoints: dict[ApiProtocol, str] = Field(default_factory=dict)
    enabled: bool = True
    presets_seeded: bool = False
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    created_by: str = ""
    updated_by: str = ""


class ModelPlatformPublic(Base):
    id: str
    vendor: PlatformVendor
    name: str
    access_mode: str
    endpoints: dict[ApiProtocol, str]
    enabled: bool
    has_api_key: bool
    api_key_masked: str
    updated_at: datetime
    updated_by: str


class ModelPlatformCreate(Base):
    vendor: PlatformVendor
    name: str = Field(default="", max_length=100)
    access_mode: str = "standard"
    api_key: str = ""
    endpoints: dict[ApiProtocol, str] | None = None
    enabled: bool = True


class ModelPlatformUpdate(Base):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    api_key: str | None = None
    endpoints: dict[ApiProtocol, str] | None = None
    enabled: bool | None = None

# 思考模式。Harbor 各 agent 接受的集合不一样，builder 会按 agent 收敛（见 resources_service）。
ReasoningEffort = Literal["", "low", "medium", "high", "xhigh", "max"]
EFFORT_ORDER: list[str] = ["low", "medium", "high", "xhigh", "max"]


class ModelApiConfig(Base):
    id: str = Field(default_factory=lambda: new_id("mdl"))
    name: str  # 平台里显示的名字，也是默认的模型标识，例如 gpt-5.6-sol
    provider: ProviderName = "openai"
    platform_id: str = ""
    supported_protocols: list[ApiProtocol] = Field(default_factory=list)
    # 真正发给 agent 的模型标识；留空用 name。litellm 系 agent 会自动加 provider/ 前缀。
    model: str = ""
    # 新平台模型填写服务器标识；旧配置可能已经包含 LiteLLM 客户端前缀。
    model_is_server_id: bool = False
    base_url: str = ""
    api_key: str = ""  # 明文只存在 db 文件里（文件权限 0600），接口永不返回
    extra_env: dict[str, str] = Field(default_factory=dict)  # 额外要注入的变量
    note: str = ""
    enabled: bool = True
    supports_reasoning_effort: bool = True
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    created_by: str = ""
    updated_by: str = ""

    @property
    def model_id(self) -> str:
        return (self.model or self.name).strip()

    @property
    def ready(self) -> bool:
        """有密钥才算可用；没密钥的配置在提交页里灰掉并说明原因。"""
        return self.enabled and bool(self.api_key.strip())


class ModelApiPublic(Base):
    """接口返回用：密钥只给尾号。"""

    id: str
    name: str
    provider: ProviderName
    model: str
    model_id: str
    base_url: str
    api_key_masked: str
    has_api_key: bool
    ready: bool
    extra_env_keys: list[str]
    note: str
    enabled: bool
    supports_reasoning_effort: bool
    created_at: datetime
    updated_at: datetime
    created_by: str
    updated_by: str
    # 会注入哪些标准环境变量（给用户看，明白平台替他做了什么）
    env_names: list[str]
    platform_id: str = ""
    platform_name: str = ""
    vendor: PlatformVendor = "custom"
    vendor_label: str = "自定义平台"
    supported_protocols: list[ApiProtocol] = Field(default_factory=list)
    endpoints: dict[ApiProtocol, str] = Field(default_factory=dict)

    @classmethod
    def of(cls, item: ModelApiConfig) -> "ModelApiPublic":
        spec = PROVIDERS[item.provider]
        key = item.api_key.strip()
        masked = ""
        if key:
            masked = f"{key[:3]}…{key[-4:]}" if len(key) > 10 else "…" + key[-2:]
        env_names = list(spec["api_key_envs"])  # type: ignore[arg-type]
        if item.base_url.strip():
            env_names += list(spec["base_url_envs"])  # type: ignore[arg-type]
        env_names += sorted(item.extra_env)
        return cls(
            id=item.id,
            name=item.name,
            provider=item.provider,
            model=item.model,
            model_id=item.model_id,
            base_url=item.base_url,
            api_key_masked=masked,
            has_api_key=bool(key),
            ready=item.ready,
            extra_env_keys=sorted(item.extra_env),
            note=item.note,
            enabled=item.enabled,
            supports_reasoning_effort=item.supports_reasoning_effort,
            created_at=item.created_at,
            updated_at=item.updated_at,
            created_by=item.created_by,
            updated_by=item.updated_by,
            env_names=env_names,
        )


class ModelApiCreate(Base):
    name: str = Field(min_length=1, max_length=256)
    provider: ProviderName = "openai"
    platform_id: str = ""
    supported_protocols: list[ApiProtocol] | None = None
    model: str = ""
    base_url: str = ""
    api_key: str = ""
    extra_env: dict[str, str] = Field(default_factory=dict)
    note: str = ""
    enabled: bool = True
    supports_reasoning_effort: bool = True


class ModelApiUpdate(Base):
    name: str | None = None
    provider: ProviderName | None = None
    supported_protocols: list[ApiProtocol] | None = None
    model: str | None = None
    base_url: str | None = None
    # None = 不改；"" = 清空；其他 = 覆盖
    api_key: str | None = None
    extra_env: dict[str, str] | None = None
    note: str | None = None
    enabled: bool | None = None
    supports_reasoning_effort: bool | None = None
