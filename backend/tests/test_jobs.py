"""Job lifecycle: submission, review gate, RBAC, cancellation, execution."""

from __future__ import annotations

import json
import time

import pytest

from .conftest import FAKE_MODEL_ID, project_id


def _upload_alpaca(client, headers, make_zip, name="job-data"):
    records = [{"instruction": f"q{i}", "input": "", "output": f"a{i}"} for i in range(8)]
    path = make_zip({"train.json": json.dumps(records)}, f"{name}.zip")
    with open(path, "rb") as handle:
        response = client.post(
            "/api/datasets",
            headers=headers,
            files={"file": (path.name, handle, "application/zip")},
            data={"name": name, "kind": "sft_alpaca"},
        )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _sft_payload(dataset_id: str, **overrides):
    payload = {
        "name": "pytest-sft",
        "note": "由测试创建",
        "type": "sft",
        "project_id": project_id(),
        "resources": {"n_gpus": 0},
        "sft": {
            "stage": "sft",
            "model_id": FAKE_MODEL_ID,
            "model_path": "",
            "template": "auto",
            "dataset_ids": [dataset_id],
            "finetuning_type": "lora",
            "num_train_epochs": 1.0,
        },
    }
    payload.update(overrides)
    return payload


@pytest.fixture(scope="module")
def dataset_id(client, member_headers, tmp_path_factory):
    tmp = tmp_path_factory.mktemp("ds")

    def make_zip(files, name):
        import zipfile

        target = tmp / name
        with zipfile.ZipFile(target, "w") as zf:
            for rel, content in files.items():
                zf.writestr(rel, content)
        return target

    return _upload_alpaca(client, member_headers, make_zip)


def test_submitted_job_waits_for_review(client, member_headers, dataset_id):
    response = client.post("/api/jobs", headers=member_headers, json=_sft_payload(dataset_id))
    assert response.status_code == 201, response.text
    job = response.json()
    assert job["status"] == "pending_review"
    assert job["framework"] == "llamafactory"
    # template resolved from the model registry
    assert job["sft"]["template"] == "qwen3_nothink"
    assert job["sft"]["model_path"].endswith(FAKE_MODEL_ID)


def test_member_cannot_review_own_job(client, member_headers, dataset_id):
    job = client.post("/api/jobs", headers=member_headers, json=_sft_payload(dataset_id)).json()
    response = client.post(
        f"/api/jobs/{job['id']}/review", headers=member_headers, json={"approve": True}
    )
    assert response.status_code == 403


def test_admin_reject_moves_to_rejected(client, admin_headers, member_headers, dataset_id):
    job = client.post("/api/jobs", headers=member_headers, json=_sft_payload(dataset_id)).json()
    response = client.post(
        f"/api/jobs/{job['id']}/review",
        headers=admin_headers,
        json={"approve": False, "comment": "batch size 太大"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "rejected"
    assert body["review"]["comment"] == "batch size 太大"


def test_jobs_are_shared_but_only_owner_can_control(
    client, admin_headers, member_headers, dataset_id
):
    """Jobs are team-visible (like a wandb project); writes stay with owner+admin."""
    job = client.post("/api/jobs", headers=member_headers, json=_sft_payload(dataset_id)).json()
    client.post(
        "/api/users",
        headers=admin_headers,
        json={"email": "outsider@prt.local", "password": "outsider-pass-1", "role": "user",
              "must_change_password": False},
    )
    login = client.post(
        "/api/auth/login", json={"email": "outsider@prt.local", "password": "outsider-pass-1"}
    ).json()
    headers = {"Authorization": f"Bearer {login['access_token']}"}
    assert client.get(f"/api/jobs/{job['id']}", headers=headers).status_code == 200
    assert client.get(f"/api/jobs/{job['id']}", headers=admin_headers).status_code == 200
    assert (
        client.post(
            f"/api/jobs/{job['id']}/cancel", headers=headers, json={"reason": "not mine"}
        ).status_code
        == 403
    )
    assert client.delete(f"/api/jobs/{job['id']}", headers=headers).status_code == 403


def test_job_requires_a_project(client, member_headers, dataset_id):
    payload = _sft_payload(dataset_id)
    payload["project_id"] = ""
    response = client.post("/api/jobs", headers=member_headers, json=payload)
    assert response.status_code == 400
    assert "项目" in response.json()["detail"]

    payload["project_id"] = "prj_does_not_exist"
    assert client.post("/api/jobs", headers=member_headers, json=payload).status_code == 404


def test_unknown_model_rejected(client, member_headers, dataset_id):
    payload = _sft_payload(dataset_id)
    payload["sft"]["model_id"] = "Nope/Missing-7B"
    response = client.post("/api/jobs", headers=member_headers, json=payload)
    assert response.status_code == 404


def test_wrong_dataset_kind_rejected(client, member_headers, harbor_zip):
    with open(harbor_zip, "rb") as handle:
        harbor_id = client.post(
            "/api/datasets",
            headers=member_headers,
            files={"file": ("harbor.zip", handle, "application/zip")},
            data={"name": "harbor-for-sft", "kind": "harbor_tasks"},
        ).json()["id"]
    response = client.post("/api/jobs", headers=member_headers, json=_sft_payload(harbor_id))
    assert response.status_code == 400
    assert "sft_alpaca" in response.json()["detail"]


def test_cancel_queued_job(client, admin_headers, member_headers, dataset_id):
    job = client.post("/api/jobs", headers=member_headers, json=_sft_payload(dataset_id)).json()
    cancelled = client.post(f"/api/jobs/{job['id']}/cancel", headers=member_headers, json={"reason": "手滑"})
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    del admin_headers


def test_resubmit_clones_config(client, member_headers, dataset_id):
    job = client.post("/api/jobs", headers=member_headers, json=_sft_payload(dataset_id)).json()
    clone = client.post(f"/api/jobs/{job['id']}/resubmit", headers=member_headers)
    assert clone.status_code == 201
    body = clone.json()
    assert body["parent_job_id"] == job["id"]
    assert body["sft"]["dataset_ids"] == [dataset_id]
    assert body["status"] == "pending_review"


def test_gpu_request_over_capacity_rejected(client, member_headers, dataset_id):
    payload = _sft_payload(dataset_id)
    payload["resources"] = {"n_gpus": 64}
    response = client.post("/api/jobs", headers=member_headers, json=payload)
    assert response.status_code == 400
    assert "GPU" in response.json()["detail"]

    payload["resources"] = {"n_gpus": 4096}  # beyond the schema bound
    assert client.post("/api/jobs", headers=member_headers, json=payload).status_code == 422


def test_approved_job_runs_to_completion(client, admin_headers, member_headers, dataset_id, monkeypatch):
    """End-to-end through the scheduler with a stubbed command."""
    from backend.app.builders.base import BuildResult
    from backend.app.services import runner

    def fake_build(job, work_dir, output_dir):
        del job
        return BuildResult(
            command=["bash", "-c", "echo 'pytest fake training'; sleep 0.3; echo done"],
            cwd=str(work_dir),
            env={"PRT_TEST": "1"},
            files={"train_config.yaml": "stage: sft\n"},
            output_dir=str(output_dir),
            notes=["stubbed"],
        )

    monkeypatch.setattr(runner, "build_job", fake_build)

    job = client.post("/api/jobs", headers=member_headers, json=_sft_payload(dataset_id)).json()
    approved = client.post(
        f"/api/jobs/{job['id']}/review", headers=admin_headers, json={"approve": True}
    ).json()
    assert approved["status"] == "queued"

    deadline = time.time() + 30
    status = approved["status"]
    while time.time() < deadline:
        status = client.get(f"/api/jobs/{job['id']}", headers=member_headers).json()["status"]
        if status in {"succeeded", "failed", "cancelled"}:
            break
        time.sleep(0.4)
    assert status == "succeeded", f"job ended as {status}"

    log = client.get(f"/api/jobs/{job['id']}/log", headers=member_headers).json()
    assert "pytest fake training" in log["content"]
    assert "退出码 0" in log["content"]

    detail = client.get(f"/api/jobs/{job['id']}", headers=member_headers).json()
    assert detail["runtime"]["exit_code"] == 0
    assert detail["runtime"]["work_dir"]


def test_cancel_running_job(client, admin_headers, member_headers, dataset_id, monkeypatch):
    from backend.app.builders.base import BuildResult
    from backend.app.services import runner

    def fake_build(job, work_dir, output_dir):
        del job
        return BuildResult(
            command=["bash", "-c", "echo starting; sleep 120"],
            cwd=str(work_dir),
            output_dir=str(output_dir),
        )

    monkeypatch.setattr(runner, "build_job", fake_build)

    job = client.post("/api/jobs", headers=member_headers, json=_sft_payload(dataset_id)).json()
    client.post(f"/api/jobs/{job['id']}/review", headers=admin_headers, json={"approve": True})

    deadline = time.time() + 30
    while time.time() < deadline:
        if client.get(f"/api/jobs/{job['id']}", headers=member_headers).json()["status"] == "running":
            break
        time.sleep(0.3)

    response = client.post(
        f"/api/jobs/{job['id']}/cancel", headers=admin_headers, json={"reason": "管理员终止"}
    )
    assert response.status_code == 200

    deadline = time.time() + 30
    status = ""
    while time.time() < deadline:
        status = client.get(f"/api/jobs/{job['id']}", headers=member_headers).json()["status"]
        if status in {"cancelled", "failed", "succeeded"}:
            break
        time.sleep(0.3)
    assert status == "cancelled"


def test_overview_counts(client, admin_headers):
    body = client.get("/api/jobs/overview", headers=admin_headers).json()
    assert body["total"] > 0
    assert "gpu" in body


def test_artifact_download_rejects_traversal(client, admin_headers, member_headers, dataset_id, monkeypatch):
    """Artifacts are served from the job's output dir only."""
    from pathlib import Path

    from backend.app.builders.base import BuildResult
    from backend.app.services import runner

    def fake_build(job, work_dir, output_dir):
        del job
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        (Path(output_dir) / "adapter.txt").write_text("weights", encoding="utf-8")
        return BuildResult(command=["bash", "-c", "true"], cwd=str(work_dir), output_dir=str(output_dir))

    monkeypatch.setattr(runner, "build_job", fake_build)

    job = client.post("/api/jobs", headers=member_headers, json=_sft_payload(dataset_id)).json()
    client.post(f"/api/jobs/{job['id']}/review", headers=admin_headers, json={"approve": True})

    deadline = time.time() + 30
    while time.time() < deadline:
        if client.get(f"/api/jobs/{job['id']}", headers=member_headers).json()["status"] in {
            "succeeded",
            "failed",
        }:
            break
        time.sleep(0.3)

    ok = client.get(f"/api/jobs/{job['id']}/artifacts/download?path=adapter.txt", headers=member_headers)
    assert ok.status_code == 200
    assert ok.content == b"weights"

    escaped = client.get(
        f"/api/jobs/{job['id']}/artifacts/download?path=../../db/users.json", headers=member_headers
    )
    assert escaped.status_code == 400

    missing = client.get(f"/api/jobs/{job['id']}/artifacts/download?path=nope.bin", headers=member_headers)
    assert missing.status_code == 404


def test_trials_endpoint_rejects_training_jobs(client, member_headers, dataset_id):
    job = client.post("/api/jobs", headers=member_headers, json=_sft_payload(dataset_id)).json()
    response = client.get(f"/api/jobs/{job['id']}/trials", headers=member_headers)
    assert response.status_code == 400


def test_scrubbed_chinese_trials_api_uses_terminal_job_and_summary(client, member_headers, harbor_zip):
    from pathlib import Path
    from urllib.parse import quote

    from backend.app.models import JobStatus, utcnow
    from backend.app.services import metrics_parser, runner
    from backend.app.store import store

    with harbor_zip.open("rb") as stream:
        uploaded = client.post("/api/datasets", headers=member_headers,
                               files={"file": (harbor_zip.name, stream, "application/zip")},
                               data={"name": "unicode-trial-suite", "kind": "harbor_tasks"})
    assert uploaded.status_code == 201, uploaded.text
    created = client.post("/api/jobs", headers=member_headers, json={
        "name": "nop中文trial", "type": "verify", "project_id": project_id(),
        "resources": {"n_gpus": 0}, "verify": {"agent": "nop", "dataset_id": uploaded.json()["id"]},
    })
    assert created.status_code == 201, created.text
    job = store.jobs.get(created.json()["id"])
    job.status = JobStatus.SUCCEEDED
    job.runtime.finished_at = utcnow()
    job.runtime.output_dir = str(runner.output_dir_for(job))
    store.jobs.put(job)
    job_dir = Path(job.runtime.output_dir) / job.id
    job_dir.mkdir(parents=True)
    names = [f"PRT-医疗-{index:03d}-20261003-1__abc{index}" for index in range(1, 4)]
    for name in names:
        trial = job_dir / name
        (trial / "verifier").mkdir(parents=True)
        (trial / "config.json").write_text('{"attempt": [REDACTED]}')
        (trial / "result.json").write_text('{"timeout_multiplier": [REDACTED].0}')
        (trial / "verifier" / "test-stdout.txt").write_text("verification finished")
    (job_dir / "result.json").write_text(json.dumps({
        "stats": {"n_completed_trials": 3, "evals": {"nop__adhoc": {
            "reward_stats": {"reward": {"0.0": names}},
        }}},
    }))  # 没有 Harbor finished_at，必须使用 DB 的终态及结束时间。

    listed = client.get(f"/api/jobs/{job.id}/trials", headers=member_headers)
    assert listed.status_code == 200, listed.text
    assert listed.json()["counts"]["success"] == 3
    assert listed.json()["counts"]["running_verifier"] == 0
    for name in names:
        response = client.get(f"/api/jobs/{job.id}/trials/{quote(name, safe='')}", headers=member_headers)
        assert response.status_code == 200, response.text
        state = response.json()["trial"]
        assert state["phase"] == "success" and state["trial_name"] == name
        assert state["finished_at"] == job.runtime.finished_at.isoformat()
        assert state["reward"] == 0.0 and state["passed"] is False
        assert response.json()["verifier_logs"][0]["content"] == "verification finished"
    parsed = metrics_parser.parse_job(job)
    assert parsed.progress.completed_trials == parsed.progress.total_trials == 3
    assert parsed.progress.percent == 100
    assert parsed.progress.trial_phases["running_verifier"] == 0
    assert parsed.summary.resolved_rate == 0.0 and parsed.summary.n_errors == 0


@pytest.mark.parametrize("source", ["agent_env", "verifier_env"])
def test_verification_submission_rejects_short_secret_placeholder(client, member_headers, harbor_zip, source):
    with harbor_zip.open("rb") as stream:
        uploaded = client.post("/api/datasets", headers=member_headers,
                               files={"file": (harbor_zip.name, stream, "application/zip")},
                               data={"name": "secret-check", "kind": "harbor_tasks"})
    response = client.post("/api/jobs", headers=member_headers, json={
        "name": "bad-key", "type": "verify", "project_id": project_id(), "resources": {"n_gpus": 0},
        "verify": {"agent": "nop", "dataset_id": uploaded.json()["id"], source: {"EVAL_API_KEY": "1"}},
    })
    assert response.status_code == 400, response.text
    assert "EVAL_API_KEY" in response.json()["detail"] and "脱敏" in response.json()["detail"]


def test_absurd_learning_rate_is_rejected(client, member_headers, dataset_id):
    """真机回归：学习率被数字框改成 1.000001 提交了进去。"""
    payload = _sft_payload(dataset_id)
    payload["sft"]["learning_rate"] = 1.000001
    response = client.post("/api/jobs", headers=member_headers, json=payload)
    assert response.status_code == 400
    assert "学习率" in response.json()["detail"] and "1e-6" in response.json()["detail"]

    payload["sft"]["learning_rate"] = 0
    assert client.post("/api/jobs", headers=member_headers, json=payload).status_code == 400

    payload["sft"]["learning_rate"] = 1e-4
    assert client.post("/api/jobs", headers=member_headers, json=payload).status_code == 201


def test_resubmit_repairs_bad_learning_rate(client, admin_headers, member_headers, dataset_id):
    """真机回归：旧任务存着 lr=1.000001，重跑不能永远被拦在校验上。"""
    from backend.app.store import store

    job = client.post("/api/jobs", headers=member_headers, json=_sft_payload(dataset_id)).json()
    # 绕过校验直接把库里的值改坏，模拟校验上线前提交的老任务
    store.jobs.update(job["id"], lambda item: setattr(item.sft, "learning_rate", 1.000001))

    clone = client.post(f"/api/jobs/{job['id']}/resubmit", headers=member_headers)
    assert clone.status_code == 201, clone.text
    body = clone.json()
    assert body["sft"]["learning_rate"] == 1e-4
    assert "自动修正" in body["note"] and "1.000001" in body["note"]
    assert body["name"] == "pytest-sft (重跑)"

    again = client.post(f"/api/jobs/{body['id']}/resubmit", headers=member_headers).json()
    assert again["name"] == "pytest-sft (重跑)"  # 不叠加
    del admin_headers
