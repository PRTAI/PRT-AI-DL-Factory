"""Batch submission validates all rows before publishing any jobs."""

import pytest

from backend.app.services import job_service
from backend.app.store import store


def payload(project_id):
    return {
        "common": {
            "name": "金融评测", "type": "verify", "project_id": project_id,
            "tags": ["batch"], "note": "同一数据集",
            "resources": {"n_gpus": 0},
            "verify": {
                "tasks_path": "/tmp/harbor-tests", "n_attempts": 2,
                "agent_env": {"OLD_API_KEY": "must-not-leak"},
                "agent_kwargs": {"old": "discard"},
            },
        },
        "agents": [
            {"agent": "oracle"},
            {"agent": "claude-code", "model": "claude/claude-opus-5",
             "reasoning_effort": "low", "agent_env": {"ANTHROPIC_API_KEY": "sk-private-claude"}},
            {"agent": "codex", "model": "openai/gpt-test", "agent_kwargs": {"max_turns": "5"}},
        ],
    }


def test_batch_creates_separate_jobs(client, member_headers, default_project):
    body = payload(default_project)
    response = client.post("/api/jobs/verify-batch", headers=member_headers, json=body)
    assert response.status_code == 201, response.text
    jobs = response.json()
    assert [j["name"] for j in jobs] == ["金融评测-oracle", "金融评测-claude-opus-5", "金融评测-gpt-test"]
    assert len({j["id"] for j in jobs}) == 3
    assert "sk-private-claude" not in response.text
    for job in jobs:
        assert job["status"] == "pending_review"
        assert job["project_id"] == default_project
        assert job["tags"] == ["batch"]
        assert job["verify"]["n_attempts"] == 2
        assert "OLD_API_KEY" not in job["verify"]["agent_env"]
    saved = [store.jobs.get(j["id"]) for j in jobs]
    assert saved[0].verify.agent_env == {}
    assert saved[1].verify.agent_env == {"ANTHROPIC_API_KEY": "sk-private-claude"}
    assert saved[1].verify.reasoning_effort == "low"
    assert saved[2].verify.agent_kwargs == {"max_turns": "5"}
    assert saved[1].verify.agent_kwargs == {}


@pytest.mark.parametrize("kind", ["missing_model", "bad_api", "name_too_long", "masked_secret"])
def test_invalid_row_creates_nothing(client, member_headers, default_project, kind, monkeypatch):
    body = payload(default_project)
    if kind == "missing_model":
        body["agents"][-1]["model"] = ""
    elif kind == "bad_api":
        body["agents"][-1]["model_api_id"] = "does-not-exist"
    elif kind == "name_too_long":
        body["agents"][-1]["model"] = "x" * 120
    else:
        body["agents"][-1]["agent_env"] = {"OPENAI_API_KEY": "***"}
    before = store.jobs.count()
    dispatches = []
    monkeypatch.setattr(job_service.scheduler, "dispatch", lambda: dispatches.append(True))
    response = client.post("/api/jobs/verify-batch", headers=member_headers, json=body)
    assert response.status_code in {400, 404}, response.text
    assert "第 3 项" in response.json()["detail"]
    assert store.jobs.count() == before
    assert not dispatches


def test_duplicate_suffixes_and_oracle_ignore_model(client, member_headers, default_project):
    body = payload(default_project)
    body["agents"] = [
        {"agent": "oracle", "model": "stale", "model_api_id": "stale"},
        {"agent": "oracle"}, {"agent": "codex", "model": "provider/oracle-2"},
    ]
    response = client.post("/api/jobs/verify-batch", headers=member_headers, json=body)
    assert response.status_code == 201, response.text
    assert [j["name"] for j in response.json()] == ["金融评测-oracle", "金融评测-oracle-2", "金融评测-oracle-2-2"]
    assert response.json()[0]["verify"]["model_api_id"] == ""


def test_platform_model_uses_actual_model_id(client, admin_headers, member_headers, default_project):
    model = client.post("/api/model-apis", headers=admin_headers, json={
        "name": "batch-friendly-display-name", "provider": "anthropic",
        "model": "claude/claude-opus-5", "api_key": "sk-batch-test-only",
    })
    assert model.status_code == 201, model.text
    body = payload(default_project)
    body["agents"] = [{"agent": "claude-code", "model_api_id": model.json()["id"]}]
    response = client.post("/api/jobs/verify-batch", headers=member_headers, json=body)
    assert response.status_code == 201, response.text
    assert response.json()[0]["name"] == "金融评测-claude-opus-5"
    assert "sk-batch-test-only" not in response.text


def test_batch_dispatches_once_after_all_jobs_saved(client, member_headers, default_project, monkeypatch):
    system = store.system_settings().model_copy(deep=True)
    system.require_approval = False
    system.updated_by = "test"
    monkeypatch.setattr(store, "system_settings", lambda: system)
    before = store.jobs.count()
    counts = []
    monkeypatch.setattr(job_service.scheduler, "dispatch", lambda: counts.append(store.jobs.count()))
    response = client.post("/api/jobs/verify-batch", headers=member_headers, json=payload(default_project))
    assert response.status_code == 201, response.text
    assert counts == [before + 3]
    assert all(j["status"] == "queued" and not j["review"]["required"] for j in response.json())


@pytest.mark.parametrize("agents", [[], [{"agent": "oracle"}] * 33, [{"agent": "oracle", "dataset_id": "override"}]])
def test_batch_size_and_override_limits(client, member_headers, default_project, agents):
    body = payload(default_project)
    body["agents"] = agents
    before = store.jobs.count()
    assert client.post("/api/jobs/verify-batch", headers=member_headers, json=body).status_code == 422
    assert store.jobs.count() == before


def test_batch_rejects_training_and_requires_auth(client, member_headers, default_project):
    body = payload(default_project)
    assert client.post("/api/jobs/verify-batch", json=body).status_code == 401
    body["common"]["type"] = "sft"
    assert client.post("/api/jobs/verify-batch", headers=member_headers, json=body).status_code == 422
