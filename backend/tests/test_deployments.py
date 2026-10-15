"""模型部署（vLLM）与 checkpoint 合并导出。

真起 vLLM 需要显卡和几分钟，所以这里把 argv 换成一个假的 HTTP 服务：
生命周期、端口分配、就绪探测、GPU 记账都是平台自己的逻辑，值得被测。
"""

from __future__ import annotations

import json
import time

import pytest
import yaml

from .conftest import FAKE_MODEL_ID, project_id
from .test_builders import harbor_dataset  # noqa: F401 - fixture

FAKE_SERVER = r"""
import json, sys
from http.server import BaseHTTPRequestHandler, HTTPServer

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/v1/models":
            self.send_response(404); self.end_headers(); return
        body = json.dumps({"object": "list", "data": [{"id": sys.argv[2]}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass

print("fake vllm listening", flush=True)
HTTPServer(("127.0.0.1", int(sys.argv[1])), Handler).serve_forever()
"""


@pytest.fixture
def fake_vllm(tmp_path, monkeypatch):
    """把 build_command 换成一个只实现 /v1/models 的假服务。"""
    from backend.app.services import deployment_service

    script = tmp_path / "fake_vllm.py"
    script.write_text(FAKE_SERVER, encoding="utf-8")

    def build(deployment):
        return (
            ["python", str(script), str(deployment.port), deployment.served_model_name],
            {},
        )

    monkeypatch.setattr(deployment_service, "build_command", build)
    return script


def _wait_status(client, headers, deployment_id, wanted, timeout=30):
    deadline = time.time() + timeout
    status = ""
    while time.time() < deadline:
        status = client.get(f"/api/deployments/{deployment_id}", headers=headers).json()["status"]
        if status in wanted:
            return status
        time.sleep(0.3)
    return status


def test_deployment_reaches_serving_and_stops(client, member_headers, fake_vllm):
    created = client.post(
        "/api/deployments",
        headers=member_headers,
        json={
            "name": "pytest-vllm",
            "source": "model",
            "model_id": FAKE_MODEL_ID,
            "served_model_name": "qwen3-test",
            "n_gpus": 1,
            "project_id": project_id(),
        },
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["status"] == "starting"
    assert body["port"] >= 8100
    assert body["model_path"].endswith(FAKE_MODEL_ID)

    assert _wait_status(client, member_headers, body["id"], {"serving", "failed"}) == "serving"

    listed = client.get("/api/deployments/endpoints", headers=member_headers).json()
    entry = next(row for row in listed if row["id"] == body["id"])
    assert entry["base_url"] == f"http://127.0.0.1:{body['port']}/v1"
    assert entry["model"] == "qwen3-test"

    # Harbor 的 agent 跑在容器里，容器内的 127.0.0.1 是容器自己 ——
    # 给评测用的地址必须是宿主机地址，不能是回环。
    assert entry["sandbox_base_url"].endswith(f":{body['port']}/v1")
    assert "127.0.0.1" not in entry["sandbox_base_url"] or entry["sandbox_base_url"] == entry["base_url"]

    log = client.get(f"/api/deployments/{body['id']}/log", headers=member_headers).json()
    assert "fake vllm listening" in log["content"]

    # 端口被占着，同一个端口不能再部署
    conflict = client.post(
        "/api/deployments",
        headers=member_headers,
        json={"name": "conflict", "model_id": FAKE_MODEL_ID, "port": body["port"]},
    )
    assert conflict.status_code == 409

    stopped = client.post(f"/api/deployments/{body['id']}/stop", headers=member_headers)
    assert stopped.status_code == 200
    assert _wait_status(client, member_headers, body["id"], {"stopped", "failed"}) == "stopped"
    # 停掉之后 GPU 记账要还回去
    assert client.get(f"/api/deployments/{body['id']}", headers=member_headers).json()["gpu_ids"] == []
    assert client.delete(f"/api/deployments/{body['id']}", headers=member_headers).status_code == 204


def test_running_deployment_cannot_be_deleted(client, member_headers, fake_vllm):
    body = client.post(
        "/api/deployments",
        headers=member_headers,
        json={"name": "pytest-del-guard", "model_id": FAKE_MODEL_ID, "n_gpus": 0},
    ).json()
    assert client.delete(f"/api/deployments/{body['id']}", headers=member_headers).status_code == 409
    client.post(f"/api/deployments/{body['id']}/stop", headers=member_headers)
    _wait_status(client, member_headers, body["id"], {"stopped", "failed"})


def test_only_owner_or_admin_can_stop(client, admin_headers, member_headers, fake_vllm):
    body = client.post(
        "/api/deployments",
        headers=member_headers,
        json={"name": "pytest-rbac", "model_id": FAKE_MODEL_ID, "n_gpus": 0},
    ).json()

    client.post(
        "/api/users",
        headers=admin_headers,
        json={
            "email": "dep-stranger@prt.local",
            "password": "dep-stranger-1",
            "role": "user",
            "must_change_password": False,
        },
    )
    token = client.post(
        "/api/auth/login", json={"email": "dep-stranger@prt.local", "password": "dep-stranger-1"}
    ).json()["access_token"]
    stranger = {"Authorization": f"Bearer {token}"}

    # 部署对全员可见（评测要用别人起的端点），但只有负责人/管理员能停
    assert client.get(f"/api/deployments/{body['id']}", headers=stranger).status_code == 200
    assert client.post(f"/api/deployments/{body['id']}/stop", headers=stranger).status_code == 403
    assert client.post(f"/api/deployments/{body['id']}/stop", headers=admin_headers).status_code == 200


def test_vllm_command_uses_real_flags():
    """--served-model-name / --tensor-parallel-size 等都以 vLLM 的 argv 为准。"""
    from backend.app.models import Deployment, VLLMConfig
    from backend.app.services import deployment_service

    deployment = Deployment(
        name="cmd",
        model_path="/models/qwen",
        served_model_name="qwen",
        port=8123,
        owner_id="usr_x",
        gpu_ids=[2, 3],
        config=VLLMConfig(
            tensor_parallel_size=2,
            max_model_len=8192,
            quantization="fp8",
            enforce_eager=True,
            api_key="sk-secret",
            extra_args={"swap_space": 8, "disable_log_requests": True},
        ),
    )
    command, env = deployment_service.build_command(deployment)
    joined = " ".join(command)
    assert "vllm.entrypoints.openai.api_server" in joined
    for flag in (
        "--model /models/qwen",
        "--served-model-name qwen",
        "--port 8123",
        "--tensor-parallel-size 2",
        "--max-model-len 8192",
        "--quantization fp8",
        "--enforce-eager",
        "--trust-remote-code",
        "--enable-prefix-caching",
        "--swap-space 8",
        "--disable-log-requests",
    ):
        assert flag in joined, flag
    assert env["CUDA_VISIBLE_DEVICES"] == "2,3"


def test_lora_adapter_directory_refused_for_direct_deploy(
    client, member_headers, admin_headers, dataset_id_for_projects, monkeypatch
):
    """LoRA adapter 不能直接给 vLLM，必须先合并导出。"""
    from pathlib import Path

    from backend.app.builders.base import BuildResult
    from backend.app.services import runner

    def fake_build(job, work_dir, output_dir):
        del job
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)
        (out / "adapter_config.json").write_text(
            json.dumps({"peft_type": "LORA", "r": 8}), encoding="utf-8"
        )
        (out / "adapter_model.safetensors").write_bytes(b"\x00")
        return BuildResult(command=["bash", "-c", "true"], cwd=str(work_dir), output_dir=str(output_dir))

    monkeypatch.setattr(runner, "build_job", fake_build)

    job = client.post(
        "/api/jobs",
        headers=member_headers,
        json={
            "name": "lora-for-deploy",
            "type": "sft",
            "project_id": project_id(),
            "resources": {"n_gpus": 0},
            "sft": {
                "stage": "sft",
                "model_id": FAKE_MODEL_ID,
                "model_path": "",
                "dataset_ids": [dataset_id_for_projects],
                "finetuning_type": "lora",
            },
        },
    ).json()
    client.post(f"/api/jobs/{job['id']}/review", headers=admin_headers, json={"approve": True})

    deadline = time.time() + 30
    while time.time() < deadline:
        if client.get(f"/api/jobs/{job['id']}", headers=member_headers).json()["status"] in {
            "succeeded",
            "failed",
        }:
            break
        time.sleep(0.3)

    refused = client.post(
        "/api/deployments",
        headers=member_headers,
        json={"name": "adapter-deploy", "source": "job", "job_id": job["id"], "n_gpus": 0},
    )
    assert refused.status_code == 400
    assert "合并导出" in refused.json()["detail"]

    # 但它应该出现在可导出的 checkpoint 列表里
    candidates = client.get(f"/api/exports/candidates/{job['id']}", headers=member_headers).json()
    assert candidates["exportable"] is True
    assert candidates["checkpoints"], candidates
    assert candidates["base_model_path"].endswith(FAKE_MODEL_ID)


def test_export_merges_adapter(
    client, member_headers, admin_headers, dataset_id_for_projects, monkeypatch
):
    """合并导出走 llamafactory.cli export，配置键以 ExportArguments 为准。"""
    from pathlib import Path

    from backend.app.builders.base import BuildResult
    from backend.app.services import export_service, runner

    def fake_build(job, work_dir, output_dir):
        del job
        out = Path(output_dir)
        (out / "checkpoint-10").mkdir(parents=True, exist_ok=True)
        (out / "checkpoint-10" / "adapter_config.json").write_text("{}", encoding="utf-8")
        (out / "adapter_config.json").write_text("{}", encoding="utf-8")
        return BuildResult(command=["bash", "-c", "true"], cwd=str(work_dir), output_dir=str(output_dir))

    monkeypatch.setattr(runner, "build_job", fake_build)

    job = client.post(
        "/api/jobs",
        headers=member_headers,
        json={
            "name": "lora-for-export",
            "type": "sft",
            "project_id": project_id(),
            "resources": {"n_gpus": 0},
            "sft": {
                "stage": "sft",
                "model_id": FAKE_MODEL_ID,
                "model_path": "",
                "dataset_ids": [dataset_id_for_projects],
                "finetuning_type": "lora",
            },
        },
    ).json()
    client.post(f"/api/jobs/{job['id']}/review", headers=admin_headers, json={"approve": True})
    deadline = time.time() + 30
    while time.time() < deadline:
        if client.get(f"/api/jobs/{job['id']}", headers=member_headers).json()["status"] in {
            "succeeded",
            "failed",
        }:
            break
        time.sleep(0.3)

    # 假的 export 进程：只创建目标目录并写一个文件
    def fake_launch(task):
        Path(task.export_dir).mkdir(parents=True, exist_ok=True)
        (Path(task.export_dir) / "model.safetensors").write_bytes(b"merged")
        work_dir = Path(task.export_dir).parent
        (work_dir / "export_config.yaml").write_text(
            yaml.safe_dump(export_service.build_config(task)), encoding="utf-8"
        )
        from backend.app.models import ExportStatus
        from backend.app.store import store

        def mutate(item):
            item.status = ExportStatus.SUCCEEDED
            item.output_bytes = 6

        return store.exports.update(task.id, mutate) or task

    monkeypatch.setattr(export_service, "_launch", fake_launch)

    created = client.post("/api/exports", headers=member_headers, json={"job_id": job["id"]})
    assert created.status_code == 201, created.text
    task = created.json()
    assert task["status"] == "succeeded"
    assert task["adapter_path"].endswith(job["id"])  # 默认取输出根目录的最终 adapter
    assert Path(task["export_dir"], "model.safetensors").read_bytes() == b"merged"

    cfg = yaml.safe_load(Path(task["export_dir"]).parent.joinpath("export_config.yaml").read_text())
    assert cfg["adapter_name_or_path"] == task["adapter_path"]
    assert cfg["export_dir"] == task["export_dir"]
    assert cfg["export_size"] == 5
    assert cfg["finetuning_type"] == "lora"
    assert cfg["template"]

    # 指定某个 checkpoint 也可以
    checkpoint = next(
        c["path"]
        for c in client.get(f"/api/exports/candidates/{job['id']}", headers=member_headers).json()[
            "checkpoints"
        ]
        if c["label"] == "checkpoint-10"
    )
    specific = client.post(
        "/api/exports",
        headers=member_headers,
        json={"job_id": job["id"], "adapter_path": checkpoint, "export_device": "auto"},
    ).json()
    assert specific["adapter_path"] == checkpoint
    assert specific["export_device"] == "auto"

    # 越权路径要被拒
    escaped = client.post(
        "/api/exports",
        headers=member_headers,
        json={"job_id": job["id"], "adapter_path": "/etc"},
    )
    assert escaped.status_code == 400

    assert len(client.get(f"/api/exports?job_id={job['id']}", headers=member_headers).json()) == 2


def test_full_finetune_is_not_exportable(client, member_headers, dataset_id_for_projects):
    job = client.post(
        "/api/jobs",
        headers=member_headers,
        json={
            "name": "full-ft",
            "type": "sft",
            "project_id": project_id(),
            "resources": {"n_gpus": 0},
            "sft": {
                "stage": "sft",
                "model_id": FAKE_MODEL_ID,
                "model_path": "",
                "dataset_ids": [dataset_id_for_projects],
                "finetuning_type": "full",
            },
        },
    ).json()
    body = client.get(f"/api/exports/candidates/{job['id']}", headers=member_headers).json()
    assert body["exportable"] is False
    assert "全参" in body["reason"]
    assert client.post("/api/exports", headers=member_headers, json={"job_id": job["id"]}).status_code == 400


# --------------------------------------------------------------------------- #
# 接口形式（OpenAI Chat / Responses / Anthropic）与评测接入
# --------------------------------------------------------------------------- #
ROUTE_SERVER = r"""
import json, sys
from http.server import BaseHTTPRequestHandler, HTTPServer

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/v1/models":
            body = {"object": "list", "data": [{"id": sys.argv[2]}]}
        elif self.path == "/openapi.json":
            body = {"paths": {route: {} for route in sys.argv[3].split(",") if route}}
        else:
            self.send_response(404); self.end_headers(); return
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass

HTTPServer(("127.0.0.1", int(sys.argv[1])), Handler).serve_forever()
"""


@pytest.fixture
def route_vllm(tmp_path, monkeypatch):
    """假服务的 /openapi.json 只列出 ``routes`` 里的路由，模拟新旧版本 vLLM。"""
    from backend.app.services import deployment_service

    script = tmp_path / "route_vllm.py"
    script.write_text(ROUTE_SERVER, encoding="utf-8")
    routes = {"value": "/v1/chat/completions"}

    def build(deployment):
        return ["python", str(script), str(deployment.port), deployment.served_model_name, routes["value"]], {}

    monkeypatch.setattr(deployment_service, "build_command", build)
    return routes


def _anthropic_payload(name):
    return {"name": name, "model_id": FAKE_MODEL_ID, "n_gpus": 0, "served_model_name": "local-claude",
            "api_protocol": "anthropic", "config": {"tool_call_parser": "hermes"}}


def test_anthropic_deployment_requires_tool_parser(client, member_headers):
    payload = _anthropic_payload("pytest-no-parser")
    payload["config"] = {}
    response = client.post("/api/deployments", headers=member_headers, json=payload)
    assert response.status_code == 400
    assert "tool_call_parser" in response.json()["detail"]


def test_missing_protocol_route_fails_and_frees_gpu(client, member_headers, route_vllm):
    """老版本 vLLM 没有 /v1/messages：不能标成 serving，要判失败并提示升级。"""
    body = client.post("/api/deployments", headers=member_headers, json=_anthropic_payload("pytest-old-vllm")).json()
    assert _wait_status(client, member_headers, body["id"], {"serving", "failed"}) == "failed"
    # 平台发 SIGTERM 收尾后，supervisor 不能把它改写成「用户停止」
    time.sleep(1.5)
    detail = client.get(f"/api/deployments/{body['id']}", headers=member_headers).json()
    assert detail["status"] == "failed"
    assert "/v1/messages" in detail["error"] and "0.18" in detail["error"]
    assert detail["gpu_ids"] == []


def test_protocol_route_present_reaches_serving(client, member_headers, route_vllm):
    route_vllm["value"] = "/v1/chat/completions,/v1/messages"
    body = client.post("/api/deployments", headers=member_headers, json=_anthropic_payload("pytest-new-vllm")).json()
    try:
        assert _wait_status(client, member_headers, body["id"], {"serving", "failed"}) == "serving"
        entry = next(row for row in client.get("/api/deployments/endpoints", headers=member_headers).json()
                     if row["id"] == body["id"])
        assert entry["api_protocol"] == "anthropic"
        assert entry["supported_protocols"] == ["anthropic"]
        # Anthropic 客户端自己拼 /v1/messages，地址不带 /v1
        assert entry["sandbox_endpoints"]["anthropic"].endswith(f":{body['port']}")
    finally:
        client.post(f"/api/deployments/{body['id']}/stop", headers=member_headers)
        _wait_status(client, member_headers, body["id"], {"stopped", "failed"})


def test_vllm_command_protocol_flags():
    import os

    from backend.app.config import settings
    from backend.app.models import Deployment, VLLMConfig
    from backend.app.services import deployment_service

    def command_for(protocol, api_key):
        deployment = Deployment(name="cmd", model_path="/models/qwen", served_model_name="qwen", port=8124,
                                owner_id="usr_x", api_protocol=protocol,
                                config=VLLMConfig(tool_call_parser="hermes", api_key=api_key))
        return deployment_service.build_command(deployment)

    command, env = command_for("anthropic", "sk-local-secret")
    joined = " ".join(command)
    assert "--enable-auto-tool-choice --tool-call-parser hermes" in joined
    assert "--middleware prt_vllm_auth.AnthropicApiKeyMiddleware" in joined
    # 中间件文件写在部署工作目录里，vLLM 进程要能 import 到
    assert env["PYTHONPATH"].split(os.pathsep)[0].startswith(str(settings.deployments_dir))
    # 不需要鉴权或不是 Anthropic 接口时不挂中间件
    assert "--middleware" not in command_for("anthropic", "")[0]
    assert "--middleware" not in command_for("responses", "sk-local-secret")[0]


def test_anthropic_api_key_middleware_maps_header():
    """claude-code 用 x-api-key 传密钥，vLLM 只认 Bearer，中间件负责转换。"""
    import asyncio

    from backend.app.services import deployment_service

    namespace: dict = {}
    exec(deployment_service.AUTH_MIDDLEWARE, namespace)
    seen = []

    async def app(scope, receive, send):
        seen.append(dict(scope["headers"]))

    middleware = namespace["AnthropicApiKeyMiddleware"](app)
    asyncio.run(middleware({"type": "http", "headers": [(b"x-api-key", b"sk-1")]}, None, None))
    asyncio.run(middleware({"type": "http", "headers": [(b"authorization", b"Bearer sk-2"), (b"x-api-key", b"sk-1")]}, None, None))
    assert seen[0][b"authorization"] == b"Bearer sk-1"
    assert seen[1][b"authorization"] == b"Bearer sk-2"


@pytest.fixture
def serving_deployment():
    from backend.app.models import Deployment, DeploymentStatus, VLLMConfig
    from backend.app.store import store

    created = []

    def make(protocol, *, api_key="", served="local-model", status=DeploymentStatus.SERVING):
        item = Deployment(name=f"dep-{protocol}", model_path="/models/x", served_model_name=served, port=8177,
                          owner_id="usr_x", api_protocol=protocol, status=status,
                          config=VLLMConfig(api_key=api_key, tool_call_parser="hermes"))
        store.deployments.put(item)
        created.append(item.id)
        return item

    yield make
    for deployment_id in created:
        store.deployments.delete(deployment_id)


def test_resolve_local_deployment_per_agent(serving_deployment):
    from fastapi import HTTPException

    from backend.app.models import DeploymentStatus, VerifyConfig
    from backend.app.services import deployment_service, model_api_service

    host = deployment_service.host_address()
    anthropic = serving_deployment("anthropic", api_key="sk-deploy-secret")
    resolved = model_api_service.resolve_agent(VerifyConfig(agent="claude-code", deployment_id=anthropic.id), user_id="usr_x")
    assert resolved.model == "local-model"  # claude-code 走 bare 模型名
    assert resolved.env["ANTHROPIC_BASE_URL"] == f"http://{host}:8177"
    assert resolved.env["ANTHROPIC_API_KEY"] == "sk-deploy-secret"
    assert resolved.env["ANTHROPIC_SMALL_FAST_MODEL"] == "local-model"
    assert resolved.secrets == ["sk-deploy-secret"]

    responses = serving_deployment("responses")
    resolved = model_api_service.resolve_agent(VerifyConfig(agent="codex", deployment_id=responses.id), user_id="usr_x")
    assert resolved.model == "local-model"
    assert resolved.env["OPENAI_BASE_URL"] == f"http://{host}:8177/v1"
    assert resolved.env["OPENAI_API_KEY"] == deployment_service.NO_AUTH_PLACEHOLDER
    assert resolved.secrets == []
    # Responses 部署同端口仍有 Chat 接口，litellm 系 agent 也能用
    resolved = model_api_service.resolve_agent(VerifyConfig(agent="terminus-2", deployment_id=responses.id), user_id="usr_x")
    assert resolved.model == "openai/local-model"

    chat = serving_deployment("openai")
    for agent in ("codex", "claude-code"):
        with pytest.raises(HTTPException, match="重新部署"):
            model_api_service.resolve_agent(VerifyConfig(agent=agent, deployment_id=chat.id), user_id="usr_x")
    with pytest.raises(HTTPException, match="重新部署"):
        model_api_service.resolve_agent(VerifyConfig(agent="terminus-2", agent_api_protocol="anthropic", deployment_id=chat.id), user_id="usr_x")
    starting = serving_deployment("openai", status=DeploymentStatus.STARTING)
    with pytest.raises(HTTPException, match="不在服务中"):
        model_api_service.resolve_agent(VerifyConfig(agent="terminus-2", deployment_id=starting.id), user_id="usr_x")
    slashed = serving_deployment("responses", served="Qwen/Qwen3")
    with pytest.raises(HTTPException, match="codex"):
        model_api_service.resolve_agent(VerifyConfig(agent="codex", deployment_id=slashed.id), user_id="usr_x")
    with pytest.raises(HTTPException, match="只能选一个"):
        model_api_service.resolve_agent(VerifyConfig(agent="codex", deployment_id=responses.id, model_api_id="x"), user_id="usr_x")


def test_harbor_build_uses_local_deployment(tmp_path, monkeypatch, serving_deployment, harbor_dataset):
    from backend.app.builders import harbor
    from backend.app.models import Framework, Job, JobType, ResourceSpec, VerifyConfig

    from .test_builders import _fake_harbor_bin

    _fake_harbor_bin(monkeypatch, tmp_path)
    deployment = serving_deployment("responses", api_key="sk-deploy-secret")
    job = Job(name="local", owner_id="usr_x", type=JobType.VERIFY, framework=Framework.HARBOR,
              resources=ResourceSpec(n_gpus=0),
              verify=VerifyConfig(dataset_id=harbor_dataset.id, agent="codex", deployment_id=deployment.id))
    built = harbor.build(job, tmp_path / "work", tmp_path / "out")
    assert built.command[built.command.index("--model") + 1] == "local-model"
    codex_config = json.loads(next(v.split("=", 1)[1] for v in built.command if v.startswith("config=")))
    assert codex_config["model_providers"]["prt"]["base_url"].endswith(":8177/v1")
    assert built.env["PRT_AGENT_OPENAI_API_KEY"] == "sk-deploy-secret"
    assert "sk-deploy-secret" in built.secrets
    assert "sk-deploy-secret" not in built.masked_command_line()
    # 排队期间部署被停：启动阶段给出可读的 BuildError，而不是 HTTPException
    from backend.app.builders.base import BuildError
    from backend.app.models import DeploymentStatus
    from backend.app.store import store

    store.deployments.update(deployment.id, lambda item: setattr(item, "status", DeploymentStatus.STOPPED))
    with pytest.raises(BuildError, match="不在服务中"):
        harbor.build(job, tmp_path / "work", tmp_path / "out")


def test_verify_submission_checks_local_deployment(client, member_headers, default_project, serving_deployment):
    from backend.app.models import DeploymentStatus
    from backend.app.store import store

    deployment = serving_deployment("anthropic")
    body = {"name": "local-eval", "type": "verify", "project_id": default_project, "resources": {"n_gpus": 0},
            "verify": {"tasks_path": "/tmp/harbor-tests", "agent": "claude-code", "deployment_id": deployment.id}}
    response = client.post("/api/jobs", headers=member_headers, json=body)
    assert response.status_code == 201, response.text
    assert response.json()["verify"]["model"] == "local-model"
    store.deployments.update(deployment.id, lambda item: setattr(item, "status", DeploymentStatus.STOPPED))
    response = client.post("/api/jobs", headers=member_headers, json=body)
    assert response.status_code == 400
    assert "不在服务中" in response.json()["detail"]
