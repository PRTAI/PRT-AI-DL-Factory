"""Job lifecycle: submission, review gate, RBAC, cancellation, execution."""

from __future__ import annotations

import json
import time

import pytest

from .conftest import FAKE_MODEL_ID


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


def test_other_member_cannot_view_job(client, admin_headers, member_headers, dataset_id):
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
    assert client.get(f"/api/jobs/{job['id']}", headers=headers).status_code == 403
    # …but an admin can
    assert client.get(f"/api/jobs/{job['id']}", headers=admin_headers).status_code == 200


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
