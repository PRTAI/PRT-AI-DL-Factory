"""质检客户端的真实 HTTP 请求构造、错误响应和推理预算行为；不连接外部模型。"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from backend.app.models import ModelApiConfig
from backend.app.services import llm_client


def _mock_api(monkeypatch, data, *, status=200):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(status, json=data)

    client_class = httpx.AsyncClient
    monkeypatch.setattr(
        llm_client.httpx, "AsyncClient",
        lambda **kwargs: client_class(transport=httpx.MockTransport(handle), **kwargs),
    )
    return requests


def _chat(config):
    return asyncio.run(llm_client.chat(config, system="按规范质检", user="输出 JSON"))


@pytest.mark.parametrize("base,expected", [
    ("https://api.deepseek.com/anthropic", "https://api.deepseek.com/anthropic/v1/messages"),
    ("https://api.deepseek.com/anthropic/v1/", "https://api.deepseek.com/anthropic/v1/messages"),
    ("https://api.anthropic.com", "https://api.anthropic.com/v1/messages"),
    ("", "https://api.anthropic.com/v1/messages"),
])
def test_anthropic_request_matches_sdk_path(monkeypatch, base, expected):
    requests = _mock_api(monkeypatch, {
        "content": [{"type": "text", "text": '{"checks": []}'}],
        "stop_reason": "end_turn", "usage": {"input_tokens": 3, "output_tokens": 4},
    })
    config = ModelApiConfig(name="deepseek-flash", provider="anthropic", base_url=base, api_key="sk-test")
    text, usage = _chat(config)
    assert text == '{"checks": []}'
    assert usage == {"prompt_tokens": 3, "completion_tokens": 4}
    assert len(requests) == 1
    assert str(requests[0].url) == expected
    assert requests[0].headers["x-api-key"] == "sk-test"
    assert "api_key" not in json.loads(requests[0].content)


def test_openai_base_path_is_preserved(monkeypatch):
    requests = _mock_api(monkeypatch, {"choices": [{"message": {"content": "{}"}}]})
    config = ModelApiConfig(name="model", base_url="https://example.test/proxy/v1/", api_key="sk-test")
    assert _chat(config)[0] == "{}"
    assert str(requests[0].url) == "https://example.test/proxy/v1/chat/completions"


@pytest.mark.parametrize("provider,response", [
    ("anthropic", {
        "content": [{"type": "thinking", "thinking": "internal reasoning"}],
        "stop_reason": "max_tokens", "usage": {"input_tokens": 500, "output_tokens": 8192},
    }),
    ("openai", {
        "choices": [{"message": {"content": None, "reasoning_content": "internal reasoning"}, "finish_reason": "length"}],
        "usage": {"prompt_tokens": 500, "completion_tokens": 8192},
    }),
])
def test_reasoning_budget_exhaustion_does_not_retry_or_grade_thinking(monkeypatch, provider, response):
    requests = _mock_api(monkeypatch, response)
    config = ModelApiConfig(name="model", provider=provider, api_key="sk-test")
    with pytest.raises(llm_client.LLMError, match="token 上限") as caught:
        _chat(config)
    assert len(requests) == 1
    assert caught.value.diagnostics[0]["usage"]["completion_tokens"] == 8192
    assert "internal reasoning" not in str(caught.value) + json.dumps(caught.value.diagnostics)


def test_http_200_error_is_not_reported_as_empty_text(monkeypatch):
    requests = _mock_api(monkeypatch, {"error": {"message": "unsupported model sk-test"}})
    config = ModelApiConfig(name="model", provider="anthropic", api_key="sk-test")
    with pytest.raises(llm_client.LLMError, match="unsupported model") as caught:
        _chat(config)
    assert len(requests) == 1
    assert "sk-test" not in str(caught.value)


def test_wrong_protocol_response_has_structural_diagnostics(monkeypatch):
    requests = _mock_api(monkeypatch, {"choices": [{"message": {"content": "{}"}}]})
    config = ModelApiConfig(name="model", provider="anthropic", api_key="sk-test")
    with pytest.raises(llm_client.LLMError, match="缺少 content") as caught:
        _chat(config)
    assert len(requests) == 1
    assert caught.value.diagnostics[0]["response_keys"] == ["choices"]
