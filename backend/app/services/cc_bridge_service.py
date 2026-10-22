"""Wire the supplied CC OpenAI bridge into each trial's own container."""

import re
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import HTTPException

from ..config import settings
from .model_api_service import ResolvedModel

PREFIX = "PRT_CC_BRIDGE_"


def resolve(config, resolved: ResolvedModel | None) -> ResolvedModel:
    if any(name.startswith(PREFIX) for name in config.agent_env):
        raise HTTPException(400, "PRT_CC_BRIDGE_* 为平台保留变量，请使用 OpenAI 模型配置")
    env = {**(resolved.env if resolved else {}), **config.agent_env}
    model = resolved.model if resolved else config.model.strip()
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._:/-]{0,119}", model) or "://" in model:
        raise HTTPException(400, "Bridge 需要有效的上游模型 ID")
    key = env.get("OPENAI_API_KEY", "").strip()
    url = (env.get("OPENAI_BASE_URL") or env.get("OPENAI_API_BASE") or "https://api.openai.com/v1").rstrip("/")
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise HTTPException(400, "Bridge 上游必须是无内嵌密钥的 HTTP(S) API 根地址")
    if not key:
        raise HTTPException(400, "请配置上游的个人 API Key，手动配置时填写 OPENAI_API_KEY")
    env.update({PREFIX + "ENABLED": "1", PREFIX + "UPSTREAM_API_KEY": key,
                PREFIX + "UPSTREAM_BASE_URL": url,
                "ANTHROPIC_API_KEY": "prt-managed-bridge",
                "ANTHROPIC_BASE_URL": "http://127.0.0.1:18876"})
    env.setdefault("ENABLE_TOOL_SEARCH", "false")
    notes = [*(resolved.notes if resolved else []),
             "Claude Code OpenAI Bridge：容器内独立进程，仅监听回环地址；上游使用 Responses，工具仍由 Claude Code 执行"]
    return ResolvedModel(model=model, env=env, secrets=list(dict.fromkeys([*(resolved.secrets if resolved else []), key])), notes=notes)


def runtime_files(config) -> dict[str, str]:
    if not config.use_openai_bridge:
        return {}
    root = settings.resources_dir
    result = {"prt_cc_bridge_runtime.py": (root / "cc_bridge_runtime.py").read_text(encoding="utf-8")}
    bundle = root / "cc-openai-bridge"
    for path in sorted(bundle.rglob("*")):
        if path.is_file() and not path.is_symlink() and "__pycache__" not in path.parts:
            result[str(Path("cc-openai-bridge") / path.relative_to(bundle))] = path.read_text(encoding="utf-8")
    return result
