"""直接调用模型 API 的最小客户端（样例审核用）。

评测任务是把密钥注进子进程的环境变量交给 Harbor 去调；审核不起子进程，需要在平台
进程内自己发请求，所以有这个模块。只覆盖两种协议：

- openai 兼容（openai / dashscope / openrouter / deepseek / xai / moonshot / google 中转）
  → ``POST {base_url}/chat/completions``
- anthropic → ``POST {base_url}/messages``

密钥只出现在请求头里：留档、日志、接口返回都拿不到（见 ``redacted_request``）。
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

import httpx

from ..models.model_api import PROVIDERS, ModelApiConfig

ANTHROPIC_VERSION = "2023-06-01"
RETRY_STATUSES = {408, 409, 425, 429, 500, 502, 503, 504}


class LLMError(RuntimeError):
    """调用失败（网络、鉴权、限流重试耗尽）。消息可以直接给用户看，不含密钥。"""

    def __init__(self, message: str, *, diagnostics: list[dict[str, Any]] | None = None) -> None:
        super().__init__(message)
        self.diagnostics = diagnostics or []


def base_url_for(config: ModelApiConfig) -> str:
    spec = PROVIDERS.get(config.provider, {})
    url = config.base_url.strip() or str(spec.get("default_base_url", ""))
    url = url.rstrip("/")
    # Anthropic SDK 在 base_url 后追加 /v1/messages；平台用 HTTP 直调，
    # 必须补齐同样的版本路径（例如 DeepSeek 的 /anthropic 根地址）。
    if url and config.provider == "anthropic" and not url.endswith("/v1"):
        url += "/v1"
    return url


def _headers(config: ModelApiConfig) -> dict[str, str]:
    key = config.api_key.strip()
    if config.provider == "anthropic":
        return {
            "x-api-key": key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        }
    return {"authorization": f"Bearer {key}", "content-type": "application/json"}


def redacted_request(config: ModelApiConfig, payload: dict[str, Any]) -> dict[str, Any]:
    """留档用：请求体原样，请求头只留键名。"""
    return {
        "provider": config.provider,
        "base_url": base_url_for(config),
        "header_keys": sorted(_headers(config)),
        "payload": payload,
    }


def build_payload(
    config: ModelApiConfig,
    *,
    system: str,
    user: str,
    max_tokens: int,
    temperature: float,
) -> tuple[str, dict[str, Any]]:
    """返回 (url 后缀, 请求体)。"""
    if config.provider == "anthropic":
        return "/messages", {
            "model": config.model_id,
            "system": system,
            "messages": [{"role": "user", "content": user}],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
    return "/chat/completions", {
        "model": config.model_id,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }


def extract_text(provider: str, data: dict[str, Any]) -> str:
    if provider == "anthropic":
        blocks = data.get("content") or []
        if isinstance(blocks, str):
            return blocks.strip()
        parts = [b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text"]
        return "".join(parts).strip()
    choices = data.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, list):  # 有些中转站返回 content blocks
        content = "".join(
            b.get("text", "") for b in content if isinstance(b, dict)
        )
    return (content or "").strip()


def response_summary(provider: str, data: dict[str, Any]) -> dict[str, Any]:
    """只保留响应结构与用量，失败时也能判断是否耗尽预算；不将思考内容当作结论。"""
    if provider == "anthropic":
        blocks = data.get("content") or []
        types = [str(block.get("type", "unknown")) for block in blocks if isinstance(block, dict)]
        stop = data.get("stop_reason")
    else:
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        types = [key for key in ("content", "reasoning_content", "tool_calls") if message.get(key)]
        stop = choice.get("finish_reason")
    return {
        "response_keys": sorted(data),
        "stop_reason": stop,
        "content_types": types,
        "usage": extract_usage(provider, data),
    }


def extract_usage(provider: str, data: dict[str, Any]) -> dict[str, int]:
    usage = data.get("usage") or {}
    if provider == "anthropic":
        return {
            "prompt_tokens": int(usage.get("input_tokens") or 0),
            "completion_tokens": int(usage.get("output_tokens") or 0),
        }
    return {
        "prompt_tokens": int(usage.get("prompt_tokens") or 0),
        "completion_tokens": int(usage.get("completion_tokens") or 0),
    }


async def chat(
    config: ModelApiConfig,
    *,
    system: str,
    user: str,
    max_tokens: int = 8192,
    temperature: float = 0.0,
    timeout: float = 300.0,
    attempts: int = 3,
) -> tuple[str, dict[str, int]]:
    """发一次对话，返回 (文本, usage)。失败抛 ``LLMError``。"""
    if not config.api_key.strip():
        raise LLMError(f"模型 API「{config.name}」没有配置密钥")
    base = base_url_for(config)
    if not base:
        raise LLMError(f"模型 API「{config.name}」没有 base_url，且 provider 没有默认地址")

    suffix, payload = build_payload(
        config, system=system, user=user, max_tokens=max_tokens, temperature=temperature
    )
    last = ""
    diagnostics: list[dict[str, Any]] = []
    for attempt in range(1, attempts + 1):
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(base + suffix, json=payload, headers=_headers(config))
            if resp.status_code >= 400:
                last = f"HTTP {resp.status_code}: {resp.text.replace(config.api_key.strip(), '[REDACTED]')[:400]}"
                if resp.status_code in RETRY_STATUSES and attempt < attempts:
                    await asyncio.sleep(min(2 ** attempt, 16))
                    continue
                raise LLMError(last, diagnostics=diagnostics)
            data = resp.json()
            if not isinstance(data, dict):
                raise LLMError("模型接口响应不是 JSON 对象", diagnostics=diagnostics)
            if data.get("error"):
                error = str(data["error"]).replace(config.api_key.strip(), "[REDACTED]")[:400]
                raise LLMError(f"模型接口返回错误：{error}", diagnostics=diagnostics)
            summary = response_summary(config.provider, data)
            diagnostics.append(summary)
            expected = "content" if config.provider == "anthropic" else "choices"
            if expected not in data:
                raise LLMError(
                    f"模型接口响应缺少 {expected} 字段，请检查接口地址与协议",
                    diagnostics=diagnostics,
                )
        except LLMError:
            raise
        except Exception as exc:  # 网络错误 / 非 JSON 响应
            last = f"{type(exc).__name__}: {exc}"
            if attempt < attempts:
                await asyncio.sleep(min(2 ** attempt, 16))
                continue
            raise LLMError(f"调用模型 API 失败：{last}", diagnostics=diagnostics) from exc

        text = extract_text(config.provider, data)
        if not text:
            last = "模型返回了空内容"
            stop = summary["stop_reason"]
            if stop in {"max_tokens", "length"}:
                raise LLMError(
                    f"模型输出达到 token 上限（{stop}），但未返回正文；请提高输出预算或关闭该模型的思考模式",
                    diagnostics=diagnostics,
                )
            last += f"（stop_reason={stop or '未提供'}；内容类型={','.join(summary['content_types']) or '无'}）"
            if attempt < attempts:
                await asyncio.sleep(1.0)
                continue
            raise LLMError(last, diagnostics=diagnostics)
        return text, extract_usage(config.provider, data)

    raise LLMError(f"调用模型 API 失败：{last}", diagnostics=diagnostics)


# --------------------------------------------------------------------------- #
# JSON 抽取
# --------------------------------------------------------------------------- #
_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def extract_json(text: str) -> Any:
    """从模型输出里取出第一个 JSON 对象/数组。解析不出来抛 ``ValueError``。

    模型很爱在 JSON 前后加解释、用 ``` 围栏包起来，所以按「去围栏 → 直接解析 →
    截取第一段括号平衡的子串」三步来。
    """
    candidates: list[str] = []
    stripped = text.strip()
    if stripped:
        candidates.append(stripped)
    for match in _FENCE.finditer(text):
        body = match.group(1).strip()
        if body:
            candidates.insert(0, body)

    for candidate in candidates:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass
        sliced = _first_balanced(candidate)
        if sliced:
            try:
                return json.loads(sliced)
            except json.JSONDecodeError:
                continue
    raise ValueError("模型输出里没有可解析的 JSON")


def _first_balanced(text: str) -> str:
    """截取第一个括号平衡的 JSON 片段（忽略字符串里的括号）。"""
    starts = [i for i in (text.find("{"), text.find("[")) if i >= 0]
    if not starts:
        return ""
    start = min(starts)
    opening = text[start]
    closing = "}" if opening == "{" else "]"
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == opening:
            depth += 1
        elif char == closing:
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return ""
