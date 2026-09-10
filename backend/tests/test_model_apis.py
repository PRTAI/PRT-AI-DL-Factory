"""模型 API 配置：CRUD 权限、密钥打码、注入到 Harbor 的标准环境变量、思考模式收敛。

环境变量名与各 agent 的 reasoning_effort 取值都去解析本机 Harbor 源码核对，
而不是相信我们自己抄的表。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from backend.app.config import settings
from backend.app.models import PROVIDERS
from backend.app.services import resources_service


def _harbor_pkg() -> Path | None:
    for root in (settings.harbor_root / "src" / "harbor", Path(settings.harbor_bin).resolve().parents[1] / "lib"):
        if (root / "agents" / "model_connection.py").is_file():
            return root
    # site-packages 里的安装
    import glob

    for hit in glob.glob(str(Path(settings.harbor_bin).resolve().parents[1] / "lib" / "python3*" / "site-packages" / "harbor")):
        return Path(hit)
    return None


# ----------------------------------------------------------------------------- #
def test_defaults_seeded_and_visible_to_members(client, member_headers):
    rows = client.get("/api/model-apis", headers=member_headers).json()
    names = {row["name"] for row in rows}
    assert {"gpt-5.6-sol", "claude-opus-4-8", "claude-opus-5", "qwen3.8-max"} <= names
    for row in rows:
        # 没填密钥之前不可用，且接口里永远没有明文密钥字段
        assert "api_key" not in row
        if row["name"] in {"gpt-5.6-sol", "claude-opus-4-8", "claude-opus-5", "qwen3.8-max"} and not row["has_api_key"]:
            assert row["ready"] is False
    providers = client.get("/api/model-apis/providers", headers=member_headers).json()
    assert {p["id"] for p in providers} >= {"openai", "anthropic", "dashscope"}


def test_only_admin_can_write(client, admin_headers, member_headers):
    payload = {"name": "member-try", "provider": "openai", "api_key": "sk-x"}
    assert client.post("/api/model-apis", headers=member_headers, json=payload).status_code == 403

    created = client.post(
        "/api/model-apis",
        headers=admin_headers,
        json={"name": "relay-gpt", "provider": "openai", "base_url": "https://relay.example/v1",
              "api_key": "sk-relay-abcdefghijklmnop", "note": "中转站"},
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["api_key_masked"] == "sk-…mnop"
    assert body["has_api_key"] and body["ready"]
    assert body["env_names"] == ["OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_API_BASE"]

    # 编辑时没动 api_key（None）→ 保留；传 "" → 清空
    kept = client.patch(f"/api/model-apis/{body['id']}", headers=admin_headers, json={"note": "改备注"}).json()
    assert kept["has_api_key"] is True
    cleared = client.patch(f"/api/model-apis/{body['id']}", headers=admin_headers, json={"api_key": ""}).json()
    assert cleared["has_api_key"] is False and cleared["ready"] is False

    assert client.patch(f"/api/model-apis/{body['id']}", headers=member_headers, json={"note": "x"}).status_code == 403
    assert client.delete(f"/api/model-apis/{body['id']}", headers=member_headers).status_code == 403
    assert client.delete(f"/api/model-apis/{body['id']}", headers=admin_headers).status_code == 204

    dup = client.post("/api/model-apis", headers=admin_headers, json={"name": "GPT-5.6-SOL", "provider": "openai"})
    assert dup.status_code == 409


# ----------------------------------------------------------------------------- #
def test_provider_env_names_match_harbor_source():
    """我们表里的变量名必须真的出现在 Harbor 的 PROVIDERS 表里。"""
    pkg = _harbor_pkg()
    if pkg is None:
        pytest.skip("Harbor 源码不可用")
    tree = ast.parse((pkg / "agents" / "model_connection.py").read_text(encoding="utf-8"))
    harbor: dict[str, list[str]] = {}
    for node in ast.walk(tree):
        targets = node.targets if isinstance(node, ast.Assign) else ([node.target] if isinstance(node, ast.AnnAssign) else [])
        if targets and any(isinstance(t, ast.Name) and t.id == "PROVIDERS" for t in targets) and node.value is not None:
            for key, value in zip(node.value.keys, node.value.values):  # type: ignore[attr-defined]
                strings = [c.value for c in ast.walk(value) if isinstance(c, ast.Constant) and isinstance(c.value, str)]
                harbor[key.value] = strings  # type: ignore[union-attr]
    assert harbor, "没解析出 Harbor 的 PROVIDERS 表"
    for provider, spec in PROVIDERS.items():
        assert provider in harbor, f"Harbor 没有 provider {provider}"
        for name in spec["api_key_envs"]:  # type: ignore[union-attr]
            assert name in harbor[provider], f"{provider}: Harbor 不读 {name}"
        # base_url 变量：Harbor 没写的 provider 会按 <PROVIDER>_BASE_URL 兜底，也算合法
        for name in spec["base_url_envs"]:  # type: ignore[union-attr]
            assert name in harbor[provider] or name == f"{provider.upper()}_BASE_URL", f"{provider}: {name}"


def test_effort_levels_match_agent_sources():
    """下拉框里能选的档位，必须是对应 agent 源码里 choices 的子集。"""
    pkg = _harbor_pkg()
    if pkg is None:
        pytest.skip("Harbor 源码不可用")
    installed = pkg / "agents" / "installed"
    sources = {
        "claude-code": installed / "claude_code.py",
        "copilot-cli": installed / "copilot_cli.py",
        "cursor-cli": installed / "cursor_cli.py",
        "gemini-cli": installed / "gemini_cli.py",
        "grok-build": installed / "grok_build.py",
        "terminus-2": pkg / "agents" / "terminus_2" / "terminus_2.py",
    }
    for agent, path in sources.items():
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        # 三种写法：choices=[...]、frozenset((...))、Literal[...]
        found = set(re.findall(r'"(none|minimal|low|medium|high|xhigh|max|ultracode|default)"', text))
        ours = set(resources_service.effort_levels_for(agent))
        assert ours, agent
        assert ours <= found, f"{agent}: 平台提供了源码里没有的档位 {sorted(ours - found)}"


def test_clamp_effort():
    from backend.app.services.model_api_service import clamp_effort

    assert clamp_effort("", ["low"]) == ("", "")
    assert clamp_effort("high", ["low", "medium", "high"])[0] == "high"
    value, note = clamp_effort("max", ["low", "medium", "high"])
    assert value == "high" and "max" in note
    value, note = clamp_effort("max", [])
    assert value == "" and "不支持" in note
    value, _ = clamp_effort("xhigh", ["low", "medium", "high", "xhigh"])
    assert value == "xhigh"


# ----------------------------------------------------------------------------- #
def test_builder_injects_standard_env_from_model_config(client, admin_headers, member_headers, harbor_zip, tmp_path, monkeypatch):
    """选了模型配置 → --model + 标准环境变量 + 密钥打码；思考模式按 agent 收敛。"""
    from backend.app.builders import harbor as harbor_builder
    from backend.app.models import Framework, Job, JobType, ResourceSpec, VerifyConfig

    with open(harbor_zip, "rb") as handle:
        dataset_id = client.post(
            "/api/datasets", headers=member_headers,
            files={"file": ("harbor.zip", handle, "application/zip")},
            data={"name": "mdl-tasks", "kind": "harbor_tasks"},
        ).json()["id"]

    cfg = client.post(
        "/api/model-apis", headers=admin_headers,
        json={"name": "claude-test", "provider": "anthropic", "base_url": "https://relay.example/anthropic",
              "api_key": "sk-ant-secret-1234567890"},
    ).json()
    binary = tmp_path / "harbor"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(harbor_builder, "resolve_harbor_bin", lambda: str(binary))

    def build(agent: str, effort: str = "max") -> tuple[list[str], object]:
        job = Job(
            name="mdl", type=JobType.VERIFY, framework=Framework.HARBOR, owner_id="usr_x",
            resources=ResourceSpec(n_gpus=0),
            verify=VerifyConfig(dataset_id=dataset_id, agent=agent, model_api_id=cfg["id"], reasoning_effort=effort),
        )
        result = harbor_builder.build(job, tmp_path / f"w-{agent}", tmp_path / "o")
        return result.command, result

    command, result = build("claude-code")
    assert command[command.index("--model") + 1] == "claude-test"  # CLI agent：裸模型名
    assert "ANTHROPIC_API_KEY=sk-ant-secret-1234567890" in command
    assert "ANTHROPIC_BASE_URL=https://relay.example/anthropic" in command
    assert "reasoning_effort=max" in command
    assert "--debug" in command
    assert "sk-ant-secret-1234567890" not in result.masked_command_line()
    assert result.masked_env()["ANTHROPIC_API_KEY"] == "***"

    command, _ = build("terminus")
    assert command[command.index("--model") + 1] == "anthropic/claude-test"  # litellm agent：带前缀

    command, result = build("gemini-cli", "max")
    assert "reasoning_effort=high" in command  # gemini 没有 max，收敛到 high
    assert any("max" in n and "high" in n for n in result.notes)

    command, result = build("oracle", "max")
    assert not any(part.startswith("reasoning_effort=") for part in command)


def test_submit_rejects_model_config_without_key(client, member_headers, harbor_zip):
    with open(harbor_zip, "rb") as handle:
        dataset_id = client.post(
            "/api/datasets", headers=member_headers,
            files={"file": ("harbor2.zip", handle, "application/zip")},
            data={"name": "mdl-tasks-2", "kind": "harbor_tasks"},
        ).json()["id"]
    rows = client.get("/api/model-apis", headers=member_headers).json()
    empty = next(r for r in rows if not r["has_api_key"])
    from .conftest import project_id

    response = client.post(
        "/api/jobs", headers=member_headers,
        json={"name": "no-key", "type": "verify", "project_id": project_id(), "resources": {"n_gpus": 0},
              "verify": {"dataset_id": dataset_id, "agent": "codex", "model_api_id": empty["id"]}},
    )
    assert response.status_code == 400
    assert "API Key" in response.json()["detail"]


def test_presets_endpoint_is_gone_but_algorithms_stay(client, member_headers):
    assert client.get("/api/catalog/presets", headers=member_headers).status_code == 404
    algorithms = client.get("/api/catalog/algorithms", headers=member_headers).json()
    assert any(a["id"] == "grpo" for a in algorithms)
    agents = client.get("/api/catalog/harbor-agents", headers=member_headers).json()
    claude = next(a for a in agents if a["name"] == "claude-code")
    assert claude["effort_levels"] == ["low", "medium", "high", "xhigh", "max"]
    assert claude["model_style"] == "bare"
