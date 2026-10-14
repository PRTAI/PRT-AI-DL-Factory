"""Renaming a completed job preserves execution evidence and enforces ownership."""

import pytest

from backend.app.models import Framework, Job, JobStatus, JobType, VerifyConfig, utcnow
from backend.app.services import audit_service
from backend.app.services.scheduler import scheduler
from backend.app.store import store
from .conftest import project_id


@pytest.fixture
def finished_job(client, member_headers, tmp_path, monkeypatch):
    monkeypatch.setattr(scheduler, "tick", lambda: None)
    owner = client.get("/api/auth/me", headers=member_headers).json()
    job = Job(
        name="原评测任务", type=JobType.VERIFY, framework=Framework.HARBOR,
        status=JobStatus.SUCCEEDED, owner_id=owner["id"], owner_email=owner["email"],
        project_id=project_id(), verify=VerifyConfig(agent_env={"API_KEY": "rename-test-secret"}),
    )
    job.runtime.output_dir = str(tmp_path)
    job.runtime.finished_at = utcnow()
    job.metrics.resolved_rate = 0.75
    (tmp_path / "reward.txt").write_text("0.75")
    store.jobs.put(job)
    yield job
    store.jobs.delete(job.id)


@pytest.mark.parametrize("state", ["succeeded", "failed", "cancelled", "rejected"])
def test_rename_finished_job_preserves_evidence(client, member_headers, finished_job, state, tmp_path):
    finished_job.status = JobStatus(state)
    store.jobs.put(finished_job)
    before = finished_job.model_dump()
    response = client.patch(
        f"/api/jobs/{finished_job.id}/name", headers=member_headers, json={"name": "  新任务名称  "},
    )
    assert response.status_code == 200, response.text
    assert response.json()["name"] == "新任务名称"
    assert "rename-test-secret" not in response.text
    after = store.jobs.get(finished_job.id).model_dump()
    assert after.pop("name") == "新任务名称"
    assert after.pop("updated_at") >= before.pop("updated_at")
    before.pop("name")
    assert after == before
    assert (tmp_path / "reward.txt").read_text() == "0.75"
    assert client.get(f"/api/jobs/{finished_job.id}", headers=member_headers).json()["name"] == "新任务名称"
    matches = client.get("/api/jobs", headers=member_headers, params={"q": "新任务名称"}).json()
    assert any(item["id"] == finished_job.id for item in matches["items"])
    event = audit_service.list_events(action_prefix="job.rename", target_id=finished_job.id)[0]
    assert event["detail"] == {"old_name": "原评测任务", "new_name": "新任务名称"}


def test_admin_can_rename_another_users_job(client, admin_headers, finished_job):
    response = client.patch(
        f"/api/jobs/{finished_job.id}/name", headers=admin_headers, json={"name": "管理员改名"},
    )
    assert response.status_code == 200
    assert response.json()["owner_id"] == finished_job.owner_id


def test_non_owner_cannot_rename(client, admin_headers, member_headers, finished_job):
    admin = client.get("/api/auth/me", headers=admin_headers).json()
    finished_job.owner_id = admin["id"]
    store.jobs.put(finished_job)
    response = client.patch(
        f"/api/jobs/{finished_job.id}/name", headers=member_headers, json={"name": "越权修改"},
    )
    assert response.status_code == 403
    assert store.jobs.get(finished_job.id).name == "原评测任务"


@pytest.mark.parametrize("state", ["draft", "pending_review", "queued", "preparing", "running"])
def test_unfinished_job_cannot_be_renamed(client, member_headers, finished_job, state):
    finished_job.status = JobStatus(state)
    store.jobs.put(finished_job)
    response = client.patch(
        f"/api/jobs/{finished_job.id}/name", headers=member_headers, json={"name": "未结束"},
    )
    assert response.status_code == 409
    assert store.jobs.get(finished_job.id).name == "原评测任务"


@pytest.mark.parametrize("payload", [
    {}, {"name": ""}, {"name": " \n\t "}, {"name": "名" * 121}, {"name": None},
    {"name": "新名称", "status": "queued"},
])
def test_rename_rejects_invalid_payload(client, member_headers, finished_job, payload):
    response = client.patch(f"/api/jobs/{finished_job.id}/name", headers=member_headers, json=payload)
    assert response.status_code == 422
    assert store.jobs.get(finished_job.id).name == "原评测任务"


def test_rename_unknown_job(client, member_headers):
    response = client.patch("/api/jobs/job_missing/name", headers=member_headers, json={"name": "新名称"})
    assert response.status_code == 404
