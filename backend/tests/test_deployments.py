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
