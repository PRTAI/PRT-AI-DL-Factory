"""Job archive download: contents, authentication, redaction and cleanup."""

from __future__ import annotations

import io
import json
import zipfile
from urllib.parse import unquote

import pytest

from backend.app.config import settings
from backend.app.models import JobStatus, ModelApiConfig
from backend.app.services import job_download_service, runner
from backend.app.store import store

from .conftest import FAKE_MODEL_ID, grant_key, project_id, user_id_of


@pytest.fixture
def export_job(client, member_headers, dataset_id_for_projects):
    response = client.post("/api/jobs", headers=member_headers, json={
        "name": "容量审计 / 导出", "type": "sft", "project_id": project_id(),
        "resources": {"n_gpus": 0},
        "sft": {"stage": "sft", "model_id": FAKE_MODEL_ID, "model_path": "", "dataset_ids": [dataset_id_for_projects]},
    })
    assert response.status_code == 201, response.text
    job = store.jobs.get(response.json()["id"])
    work = runner.work_dir_for(job)
    output = runner.output_dir_for(job)
    work.mkdir(parents=True)
    output.mkdir(parents=True)
    (work / "stdout.log").write_text("任务完成\n", encoding="utf-8")
    (work / "run.sh").write_text("#!/bin/bash\necho done\n", encoding="utf-8")
    (work / "metrics.json").write_text('{"series": {}}', encoding="utf-8")
    (output / "trial-1").mkdir()
    (output / "trial-1" / "result.json").write_text('{"reward": 1.0}', encoding="utf-8")
    return job


def test_download_job_zip_has_configuration_logs_and_nested_outputs(client, member_headers, export_job):
    before = set(settings.downloads_dir.glob("prt-job-export-*.zip"))
    response = client.get(f"/api/jobs/{export_job.id}/export", headers=member_headers)
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/zip"
    filename = unquote(response.headers["content-disposition"].split("filename*=utf-8''", 1)[1])
    assert "容量审计" in filename and filename.endswith(f"{export_job.id}.zip")
    assert "/" not in filename
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert set(archive.namelist()) == {
            "job.json", "work/stdout.log", "work/run.sh", "work/metrics.json", "outputs/trial-1/result.json",
        }
        metadata = json.loads(archive.read("job.json"))
        assert metadata["id"] == export_job.id
        assert metadata["project_name"] and metadata["exported_at"]
        assert archive.read("work/stdout.log").decode() == "任务完成\n"
        assert json.loads(archive.read("outputs/trial-1/result.json"))["reward"] == 1.0
    assert set(settings.downloads_dir.glob("prt-job-export-*.zip")) == before


def test_download_job_requires_login_and_is_team_visible(client, admin_headers, member_headers, export_job):
    assert client.get(f"/api/jobs/{export_job.id}/export").status_code == 401
    assert client.get("/api/jobs/job_missing/export", headers=member_headers).status_code == 404
    assert client.get(f"/api/jobs/{export_job.id}/export", headers=admin_headers).status_code == 200


def test_download_unstarted_job_still_exports_metadata(client, member_headers, export_job):
    import shutil

    shutil.rmtree(runner.work_dir_for(export_job))
    shutil.rmtree(runner.output_dir_for(export_job))
    response = client.get(f"/api/jobs/{export_job.id}/export", headers=member_headers)
    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert archive.namelist() == ["job.json"]
        assert json.loads(archive.read("job.json"))["status"] == "pending_review"


@pytest.mark.parametrize("secret", ["sk-export+secret-ABC", 'password-"quoted"', "中文密码\n换行"])
def test_export_redacts_secrets_across_chunks_without_changing_saved_job(client, member_headers, export_job, monkeypatch, secret):
    work = runner.work_dir_for(export_job)
    output = runner.output_dir_for(export_job)
    export_job.resources.env["API_KEY"] = secret
    export_job.name = f"导出测试 {secret}"
    export_job.note = f"备注中的密钥 {secret}"
    store.jobs.put(export_job)
    (work / "stdout.log").write_text(f"prefix-{secret}-suffix", encoding="utf-8")
    (output / "trial-1" / "config.json").write_text(json.dumps({"api_key": secret}), encoding="utf-8")
    (work / ".env").write_text(f"API_KEY={secret}\n", encoding="utf-8")
    (output / "credentials.json").write_text(f'{{"key": "{secret}"}}', encoding="utf-8")
    monkeypatch.setattr(job_download_service, "_CHUNK", 8)
    response = client.get(f"/api/jobs/{export_job.id}/export", headers=member_headers)
    assert response.status_code == 200, response.text
    assert secret not in unquote(response.headers["content-disposition"])
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert "work/.env" not in archive.namelist()
        assert "outputs/credentials.json" not in archive.namelist()
        for name in archive.namelist():
            assert secret.encode() not in archive.read(name)
        assert json.loads(archive.read("outputs/trial-1/config.json"))["api_key"] == "***"
        assert archive.read("work/stdout.log") == b"prefix-***-suffix"
        assert json.loads(archive.read("job.json"))["resources"]["env"]["API_KEY"] == "***"
    assert store.jobs.get(export_job.id).resources.env["API_KEY"] == secret
    assert (work / "stdout.log").read_text() == f"prefix-{secret}-suffix"


def test_export_redacts_old_runtime_env_key(client, member_headers, export_job):
    work = runner.work_dir_for(export_job)
    (work / ".env").write_text("JUDGE_API_KEY=old-key-from-runtime\n", encoding="utf-8")
    (work / "stdout.log").write_text("judge: old-key-from-runtime", encoding="utf-8")
    response = client.get(f"/api/jobs/{export_job.id}/export", headers=member_headers)
    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert archive.read("work/stdout.log") == b"judge: ***"


def test_export_redacts_injected_model_key(client, member_headers, tmp_path):
    key = "injected-model-api-key"
    extra_token = "injected-extra-env-token"
    model = ModelApiConfig(name="export-test-model", extra_env={"ACCESS_TOKEN": extra_token})
    store.model_apis.put(model)
    grant_key(user_id_of("dev@prt.local"), model.id, key)
    response = client.post("/api/jobs", headers=member_headers, json={
        "name": "model-key-export", "type": "verify", "project_id": project_id(),
        "resources": {"n_gpus": 0},
        "verify": {"agent": "oracle", "tasks_path": str(tmp_path), "model_api_id": model.id},
    })
    assert response.status_code == 201, response.text
    job = store.jobs.get(response.json()["id"])
    output = runner.output_dir_for(job)
    output.mkdir(parents=True)
    (output / "config.json").write_text(json.dumps({"agent_env": {"OPENAI_API_KEY": key, "ACCESS_TOKEN": extra_token}}), encoding="utf-8")
    downloaded = client.get(f"/api/jobs/{job.id}/export", headers=member_headers)
    assert downloaded.status_code == 200
    with zipfile.ZipFile(io.BytesIO(downloaded.content)) as archive:
        assert key.encode() not in archive.read("outputs/config.json")
        assert extra_token.encode() not in archive.read("outputs/config.json")


def test_export_preserves_binary_files_and_skips_symlinks(client, member_headers, export_job, tmp_path):
    output = runner.output_dir_for(export_job)
    binary = bytes(range(256)) * 20
    (output / "adapter.safetensors").write_bytes(binary)
    outside = tmp_path / "outside.txt"
    outside.write_text("outside data", encoding="utf-8")
    (output / "linked.txt").symlink_to(outside)
    (output / "linked-dir").symlink_to(tmp_path, target_is_directory=True)
    response = client.get(f"/api/jobs/{export_job.id}/export", headers=member_headers)
    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert archive.read("outputs/adapter.safetensors") == binary
        assert not any("linked" in name for name in archive.namelist())


def test_export_packaging_failure_removes_temporary_file(client, member_headers, export_job, monkeypatch):
    actor = store.users.get(export_job.owner_id)
    before = set(settings.downloads_dir.glob("prt-job-export-*.zip"))

    def fail(*args, **kwargs):
        raise OSError("模拟打包失败")

    monkeypatch.setattr(zipfile.ZipFile, "writestr", fail)
    with pytest.raises(OSError, match="模拟打包失败"):
        job_download_service.prepare_download(export_job.id, actor=actor)
    assert set(settings.downloads_dir.glob("prt-job-export-*.zip")) == before


def test_batch_export_separates_same_named_jobs_and_deduplicates_ids(client, member_headers, export_job):
    other = export_job.model_copy(deep=True)
    other.id = f"{export_job.id}_other"
    other.owner_id = "another-member"
    other.status = JobStatus.RUNNING
    store.jobs.put(other)
    work = runner.work_dir_for(other)
    work.mkdir(parents=True)
    (work / "stdout.log").write_text("仍在运行", encoding="utf-8")
    before = set(settings.downloads_dir.glob("prt-job-export-*.zip"))
    response = client.post("/api/jobs/batch-export", headers=member_headers, json={
        "job_ids": [export_job.id, other.id, export_job.id],
    })
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/zip"
    assert "jobs_2.zip" in response.headers["content-disposition"]
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        names = archive.namelist()
        assert len(names) == len(set(names))
        assert {name.split("/")[0] for name in names} == {export_job.id, other.id}
        assert archive.read(f"{export_job.id}/work/stdout.log").decode() == "任务完成\n"
        assert archive.read(f"{other.id}/work/stdout.log").decode() == "仍在运行"
        assert json.loads(archive.read(f"{other.id}/job.json"))["status"] == "running"
        assert json.loads(archive.read(f"{export_job.id}/outputs/trial-1/result.json"))["reward"] == 1.0
    assert set(settings.downloads_dir.glob("prt-job-export-*.zip")) == before


@pytest.mark.parametrize("ids", [[], ["job_missing"], ["job_missing"] * 201])
def test_batch_export_validates_selection(client, member_headers, ids):
    before = set(settings.downloads_dir.glob("prt-job-export-*.zip"))
    response = client.post("/api/jobs/batch-export", headers=member_headers, json={"job_ids": ids})
    assert response.status_code == (404 if len(ids) == 1 else 422)
    assert set(settings.downloads_dir.glob("prt-job-export-*.zip")) == before


def test_batch_export_requires_login_and_validates_every_job_before_packaging(client, member_headers, export_job, monkeypatch):
    assert client.post("/api/jobs/batch-export", json={"job_ids": [export_job.id]}).status_code == 401
    calls = []
    monkeypatch.setattr(job_download_service, "_write_job", lambda *args, **kwargs: calls.append(args))
    response = client.post("/api/jobs/batch-export", headers=member_headers, json={
        "job_ids": [export_job.id, "job_missing"],
    })
    assert response.status_code == 404
    assert calls == []


def test_batch_export_uses_redaction_and_preserves_binary_files(client, member_headers, export_job):
    secret = "sk-batch-export-secret"
    export_job.resources.env["API_KEY"] = secret
    store.jobs.put(export_job)
    work = runner.work_dir_for(export_job)
    (work / ".env").write_text(f"API_KEY={secret}")
    (work / "stdout.log").write_text(f"key={secret}")
    binary = bytes(range(256))
    (runner.output_dir_for(export_job) / "adapter.safetensors").write_bytes(binary)
    response = client.post("/api/jobs/batch-export", headers=member_headers, json={"job_ids": [export_job.id]})
    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert f"{export_job.id}/work/.env" not in archive.namelist()
        assert archive.read(f"{export_job.id}/work/stdout.log") == b"key=***"
        assert json.loads(archive.read(f"{export_job.id}/job.json"))["resources"]["env"]["API_KEY"] == "***"
        assert archive.read(f"{export_job.id}/outputs/adapter.safetensors") == binary
    assert (work / "stdout.log").read_text() == f"key={secret}"


def test_batch_export_failure_cleans_partial_archive(export_job, monkeypatch):
    actor = store.users.get(export_job.owner_id)
    before = set(settings.downloads_dir.glob("prt-job-export-*.zip"))
    original = job_download_service._write_job
    def fail_after_writing(*args, **kwargs):
        original(*args, **kwargs)
        raise OSError("模拟批量打包失败")
    monkeypatch.setattr(job_download_service, "_write_job", fail_after_writing)
    with pytest.raises(OSError, match="模拟批量打包失败"):
        job_download_service.prepare_batch_download([export_job.id], actor=actor)
    assert set(settings.downloads_dir.glob("prt-job-export-*.zip")) == before
