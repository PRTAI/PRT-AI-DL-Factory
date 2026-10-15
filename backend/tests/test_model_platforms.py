"""个人密钥按平台复用、协议选择、旧数据迁移和真实 Harbor 参数兼容。"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from backend.app.builders import harbor
from backend.app.models import Job, JobType, ModelApiConfig, ModelPlatformConfig, ResourceSpec, User, UserApiKey, VerifyConfig
from backend.app.services import api_key_service, model_api_service, model_platform_service
from backend.app.store import store
from backend.app.store.json_store import JsonCollection

from .conftest import grant_key, user_id_of

KEY = "sk-platform-personal-abcdefghijklmnop"


def _platform(client, headers, vendor="deepseek", owner="u", **values):
    """建平台，并让 ``owner`` 这个账号在上面填好自己的密钥。"""
    response = client.post("/api/model-apis/platforms", headers=headers, json={"vendor": vendor, "name": "个人密钥测试", **values})
    assert response.status_code == 201, response.text
    grant_key(owner, response.json()["id"], KEY)
    return response.json()


def test_personal_key_model_creation_rotation_and_permissions(client, admin_headers, member_headers):
    shared = client.post("/api/model-apis/platforms", headers=admin_headers, json={"vendor": "deepseek", "api_key": "sk-shared"})
    assert shared.status_code == 400 and "个人设置" in shared.json()["detail"]
    p = _platform(client, admin_headers)
    assert "api_key" not in p and not p["has_api_key"]  # 管理员自己还没填
    response = client.post("/api/model-apis", headers=admin_headers, json={"platform_id": p["id"], "name": "my-custom-model"})
    assert response.status_code == 201, response.text
    model = response.json()
    assert not model["ready"] and set(model["supported_protocols"]) == {"openai", "anthropic", "responses"}
    assert store.model_apis.get(model["id"]).api_key == ""
    assert model_api_service.require_ready(model["id"], "anthropic", user_id="u").api_key == KEY

    # 成员在平台上填一次，整个平台下的模型都能用；只对自己生效
    put = client.put(f"/api/model-apis/my-keys/{p['id']}", headers=member_headers, json={"api_key": "sk-member-own-abcdefgh"})
    assert put.status_code == 200 and put.json()["kind"] == "platform" and "my-custom-model" in put.json()["models"]
    member = user_id_of("dev@prt.local")
    assert model_api_service.require_ready(model["id"], "anthropic", user_id=member).api_key == "sk-member-own-abcdefgh"
    presets = [m for m in model_api_service.list_public(member) if m.platform_id == p["id"]]
    assert len(presets) == 3 and all(m.ready and m.api_key_masked.endswith("efgh") for m in presets)
    platform = next(x for x in client.get("/api/model-apis/platforms", headers=member_headers).json() if x["id"] == p["id"])
    assert platform["has_api_key"] and "sk-member-own-abcdefgh" not in json.dumps(platform)
    with pytest.raises(HTTPException) as caught:
        model_api_service.require_ready(model["id"], "anthropic", user_id=user_id_of("admin@prt.local"))
    assert "admin@prt.local" in caught.value.detail and "我的 API Key" in caught.value.detail

    assert client.patch(f"/api/model-apis/platforms/{p['id']}", headers=member_headers, json={"name": "x"}).status_code == 403
    assert client.patch(f"/api/model-apis/platforms/{p['id']}", headers=admin_headers, json={"api_key": "sk-x"}).status_code == 400
    assert client.post("/api/model-apis/platforms", headers=member_headers, json={"vendor": "deepseek"}).status_code == 403
    assert client.patch(f"/api/model-apis/{model['id']}", headers=admin_headers, json={"api_key": "sk-private"}).status_code == 400
    assert client.delete(f"/api/model-apis/platforms/{p['id']}", headers=admin_headers).status_code == 409
    client.delete(f"/api/model-apis/my-keys/{p['id']}", headers=member_headers)
    assert all(not m.ready for m in model_api_service.list_public(member) if m.platform_id == p["id"])


def test_protocol_specific_endpoints_and_backend_rejection(client, admin_headers, member_headers, harbor_zip):
    p = _platform(client, admin_headers, "atlascloud")
    created = client.post("/api/model-apis", headers=admin_headers, json={"platform_id": p["id"], "name": "anthropic-only", "supported_protocols": ["anthropic"]}).json()
    resolved = model_api_service.resolve(created["id"], user_id="u", model_style="bare", protocol="anthropic")
    assert resolved.env["ANTHROPIC_BASE_URL"] == "https://api.atlascloud.ai"
    assert "OPENAI_API_KEY" not in resolved.env
    with pytest.raises(HTTPException) as caught:
        model_api_service.require_ready(created["id"], "openai", user_id="u")
    assert "不支持" in caught.value.detail
    with pytest.raises(HTTPException):
        model_api_service.agent_protocol("codex", "anthropic")
    with harbor_zip.open("rb") as handle:
        dataset = client.post("/api/datasets", headers=member_headers, files={"file": ("protocol-tasks.zip", handle, "application/zip")}, data={"name": "protocol-tasks", "kind": "harbor_tasks"}).json()
    from .conftest import project_id
    grant_key(user_id_of("dev@prt.local"), p["id"], KEY)
    for verify in (
        {"agent": "codex", "model_api_id": created["id"]},
        {"agent": "oracle", "judge_model_api_id": created["id"], "judge_api_protocol": "openai"},
    ):
        response = client.post("/api/jobs", headers=member_headers, json={"name": "incompatible-wire", "type": "verify", "project_id": project_id(), "resources": {"n_gpus": 0}, "verify": {"dataset_id": dataset["id"], **verify}})
        assert response.status_code == 400 and "不支持" in response.json()["detail"]


@pytest.mark.parametrize("agent,protocol", [("claude-code", "anthropic"), ("codex", "responses")])
def test_agent_and_judge_use_selected_wire_protocol(client, admin_headers, tmp_path, monkeypatch, agent, protocol):
    p = _platform(client, admin_headers)
    model = next(m for m in model_api_service.list_public("u") if m.platform_id == p["id"])
    root = tmp_path / "task"
    root.mkdir()
    monkeypatch.setattr(harbor, "resolve_harbor_bin", lambda: "harbor")
    job = Job(name="wire-test", type=JobType.VERIFY, framework="harbor", owner_id="u", owner_email="a@example.com", resources=ResourceSpec(n_gpus=0), verify=VerifyConfig(tasks_path=str(root), agent=agent, model_api_id=model.id, judge_model_api_id=model.id, judge_api_protocol="anthropic"))
    built = harbor.build(job, tmp_path / "work", tmp_path / "output")
    assert built.env["JUDGE_BASE_URL"] == "https://api.deepseek.com/anthropic"
    assert built.env["JUDGE_API_PROTOCOL"] == "anthropic"
    assert built.env["JUDGE_PROVIDER"] == "anthropic"
    assert built.env["JUDGE_API_KEY"] == built.env["ANTHROPIC_API_KEY" if agent == "claude-code" else "OPENAI_API_KEY"]
    assert built.env["ANTHROPIC_BASE_URL" if agent == "claude-code" else "OPENAI_BASE_URL"] == p["endpoints"][protocol]
    if agent == "codex":
        native = json.loads(next(part.removeprefix("config=") for part in built.command if part.startswith("config=")))
        assert native["model_provider"] == "prt"
        assert native["model_providers"]["prt"]["wire_api"] == "responses"
        assert native["model_providers"]["prt"]["base_url"] == p["endpoints"]["responses"]
        assert native["model_providers"]["prt"]["env_key"] == "OPENAI_API_KEY"
        assert "sk-platform" not in json.dumps(native)
    assert built.env["JUDGE_API_KEY"] == KEY
    assert KEY not in built.masked_command_line()


def _local_store(monkeypatch, tmp_path):
    local = SimpleNamespace(
        model_apis=JsonCollection(tmp_path / "models.json", ModelApiConfig),
        model_platforms=JsonCollection(tmp_path / "platforms.json", ModelPlatformConfig, mode=0o600),
        user_api_keys=JsonCollection(tmp_path / "keys.json", UserApiKey, mode=0o600),
        users=JsonCollection(tmp_path / "users.json", User),
    )
    for module in (model_api_service, model_platform_service, api_key_service):
        monkeypatch.setattr(module, "store", local)
    return local


def test_legacy_migration_groups_credentials_and_preserves_references(monkeypatch, tmp_path):
    local = _local_store(monkeypatch, tmp_path)
    admin = User(email="root@example.com", display_name="root", password_hash="x", role="admin")
    writer = User(email="writer@example.com", display_name="writer", password_hash="x", role="user")
    local.users.put_many([admin, writer])
    root = "https://llm-workspace.cn-beijing.maas.aliyuncs.com"
    originals = [ModelApiConfig(id=f"mdl-old-{n}", name=f"old-{n}", provider="anthropic", model=model, base_url=root + "/apps/anthropic", api_key=key, updated_by=by) for n, (model, key, by) in enumerate([("qwen3.8-max", "sk-shared-secret", "writer@example.com"), ("qwen3.8-flash", "sk-shared-secret", "writer@example.com"), ("qwen3.8-max", "sk-other-secret", "system")])]
    local.model_apis.put_many(originals)
    model_api_service.seed_defaults()
    a, b, c = [local.model_apis.get(i.id) for i in originals]
    assert a.platform_id == b.platform_id != c.platform_id
    assert a.api_key == b.api_key == c.api_key == ""
    # 共享密钥归到填写它的人（找不到人就归最早的管理员），平台上不再留存
    assert all(p.api_key == "" for p in local.model_platforms.all())
    assert model_api_service.require_ready(a.id, "anthropic", user_id=writer.id).base_url == root + "/apps/anthropic"
    assert model_api_service.require_ready(a.id, "responses", user_id=writer.id).base_url == root + "/compatible-mode/v1"
    assert model_api_service.require_ready(c.id, "anthropic", user_id=admin.id).api_key == "sk-other-secret"
    with pytest.raises(HTTPException):
        model_api_service.require_ready(c.id, "anthropic", user_id=writer.id)
    count = local.model_platforms.count()
    assert local.model_platforms.count(lambda p: p.vendor == "alibaba") == 2
    assert any(m.model_id == "qwen3.7-plus" and m.platform_id == a.platform_id for m in local.model_apis.all())
    model_api_service.seed_defaults()
    assert local.model_platforms.count() == count and local.user_api_keys.count() == 2
    assert local.model_apis.get(a.id).model_id == "qwen3.8-max"
    assert local.model_platforms.path.stat().st_mode & 0o777 == 0o600


def test_custom_endpoint_and_extra_env_survive_migration(monkeypatch, tmp_path):
    local = _local_store(monkeypatch, tmp_path)
    admin = User(email="root@example.com", display_name="root", password_hash="x", role="admin")
    local.users.put(admin)
    old = ModelApiConfig(name="ccmax", model="anthropic/claude-opus-5-ccmax", provider="anthropic", api_key="sk-original-key", base_url="https://relay.example/custom/messages", extra_env={"CUSTOM_OPTION": "enabled"}, enabled=False)
    local.model_apis.put(old)
    model_api_service.seed_defaults()
    migrated = local.model_apis.get(old.id)
    assert not migrated.enabled and migrated.extra_env == old.extra_env
    assert migrated.model_id == old.model_id
    assert model_api_service.effective(migrated, "anthropic").base_url == old.base_url
    assert model_api_service.effective(migrated, user_id=admin.id).api_key == old.api_key
    assert model_api_service.effective(migrated, user_id="someone-else").api_key == ""


def test_catalog_uses_ordinary_api_and_presets_stay_deleted(client, admin_headers):
    catalog = client.get("/api/model-apis/platform-catalog", headers=admin_headers).json()
    assert {p["id"] for p in catalog} == {"alibaba", "tencent", "volcengine", "deepseek", "atlascloud", "custom"}
    assert all("coding" not in url and "/plan" not in url for p in catalog for url in p["endpoints"].values())
    p = _platform(client, admin_headers)
    removed = next(m for m in model_api_service.list_public("u") if m.platform_id == p["id"])
    store.model_apis.delete(removed.id)
    model_api_service.seed_defaults()
    assert store.model_apis.get(removed.id) is None


def test_codex_preserves_native_settings_and_pins_responses(tmp_path):
    config = tmp_path / "codex.toml"
    config.write_text('model_provider = "old"\nmodel_reasoning_effort = "high"\n[model_providers.old]\nbase_url = "https://old.example"\nwire_api = "chat"\n[mcp_servers.tools]\ncommand = "echo"\n')
    merged = json.loads(harbor._codex_config(str(config), "https://api.atlascloud.ai/v1"))
    assert merged["model_provider"] == "prt"
    assert merged["model_providers"]["prt"]["wire_api"] == "responses"
    assert merged["model_reasoning_effort"] == "high"
    assert merged["mcp_servers"]["tools"]["command"] == "echo"


def test_litellm_keeps_server_namespace_and_export_masks_personal_key(client, admin_headers, tmp_path):
    p = _platform(client, admin_headers, "atlascloud")
    model = next(m for m in model_api_service.list_public("u") if m.platform_id == p["id"] and m.model_id == "deepseek-ai/deepseek-v3.2")
    resolved = model_api_service.resolve(model.id, user_id="u", model_style="litellm", protocol="openai")
    assert resolved.model == "openai/deepseek-ai/deepseek-v3.2"
    gpt = next(m for m in model_api_service.list_public("u") if m.platform_id == p["id"] and m.model_id == "openai/gpt-6.1-sol")
    assert model_api_service.resolve(gpt.id, user_id="u", model_style="litellm", protocol="openai").model == "openai/openai/gpt-6.1-sol"
    assert model_api_service.resolve(gpt.id, user_id="u", model_style="bare", protocol="openai").model == "openai/gpt-6.1-sol"
    from backend.app.services.job_download_service import _mask, _secrets_for
    job = Job(name="shared-export", type=JobType.VERIFY, framework="harbor", owner_id="u", verify=VerifyConfig(model_api_id=model.id))
    secrets = _secrets_for(job, {}, tmp_path)
    masked = _mask({"log": f"Authorization: {KEY}"}, secrets)
    assert KEY not in json.dumps(masked)
