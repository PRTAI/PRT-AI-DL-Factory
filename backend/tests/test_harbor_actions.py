"""Recovery must preserve source evidence and native Harbor reconciliation."""

import asyncio
import json
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from backend.app.builders import harbor as builder
from backend.app.builders.base import BuildError
from backend.app.config import settings
from backend.app.models import Framework, Job, JobStatus, JobType, ResourceSpec, VerifyConfig
from backend.app.models.job import HarborAction
from backend.app.services import harbor_action_service as service, runner
from backend.app.store import store
from .conftest import project_id


@pytest.fixture
def recorded_job(tmp_path, client, member_headers, monkeypatch):
    pytest.importorskip("harbor", reason="需要 Harbor 原生模型以验证恢复兼容性")
    from harbor.models.job.config import JobConfig
    from harbor.models.trial.config import AgentConfig, TaskConfig, TrialConfig
    from harbor.models.trial.result import TrialResult

    monkeypatch.setattr(settings, "harbor_python", sys.executable)
    monkeypatch.setattr(builder, "resolve_harbor_bin", lambda: sys.executable)
    owner = client.get("/api/auth/me", headers=member_headers).json()
    task = tmp_path / "demo"
    (task / "environment").mkdir(parents=True)
    (task / "tests").mkdir()
    (task / "task.toml").write_text('version = "1.0"\n[verifier]\nenvironment_mode = "separate"\n')
    (task / "instruction.md").write_text("Write an answer")
    (task / "environment" / "Dockerfile").write_text("FROM ubuntu:24.04\n")
    (task / "tests" / "test.sh").write_text("echo 1 > /logs/verifier/reward.txt\n")
    source = Job(name="recovery source", type=JobType.VERIFY, framework=Framework.HARBOR,
                 owner_id=owner["id"], owner_email=owner["email"], project_id=project_id(),
                 status=JobStatus.CANCELLED, resources=ResourceSpec(n_gpus=0),
                 verify=VerifyConfig(tasks_path=str(task), agent="nop"))
    store.jobs.put(source)
    root = runner.output_dir_for(source) / source.id
    root.mkdir(parents=True)
    task_config = TaskConfig(path=task)
    native = JobConfig(job_name=source.id, jobs_dir=root.parent, n_attempts=3,
                       tasks=[task_config], agents=[AgentConfig(name="nop")])
    (root / "config.json").write_text(native.model_dump_json())
    for name, error in (("demo__done", None), ("demo__cancelled", "CancelledError")):
        trial = root / name
        (trial / "artifacts").mkdir(parents=True)
        config = TrialConfig(task=task_config, trial_name=name, trials_dir=root, agent=native.agents[0])
        result = TrialResult(
            task_name="demo", trial_name=name, trial_uri=trial.as_uri(),
            task_id={"path": str(task)}, task_checksum="test", config=config,
            agent_info={"name": "nop", "version": "1"},
            exception_info={"exception_type": error, "exception_message": "stopped",
                            "exception_traceback": "", "occurred_at": "2026-10-13T00:00:00Z"} if error else None,
        )
        (trial / "config.json").write_text(config.model_dump_json())
        (trial / "result.json").write_text(result.model_dump_json())
        (trial / "artifacts" / "logs" / "artifacts").mkdir(parents=True)
        (trial / "artifacts" / "manifest.json").write_text(json.dumps([{
            "source": "/logs/artifacts", "destination": "artifacts/logs/artifacts",
            "type": "directory", "status": "ok", "exclude": [],
        }]))
        (trial / "artifacts" / "logs" / "artifacts" / "answer.txt").write_text("original answer")
    (root / "demo__unfinished").mkdir()
    return source, root


def _submit(client, headers, source, mode="resume", **kwargs):
    return client.post(f"/api/jobs/{source.id}/harbor-actions", headers=headers,
                       json={"mode": mode, **kwargs})


def test_recovery_submission_approval_and_preview(client, member_headers, recorded_job):
    source, root = recorded_job
    info = client.get(f"/api/jobs/{source.id}/harbor-actions", headers=member_headers)
    assert info.status_code == 200, info.text
    assert info.json()["error_types"] == {"CancelledError": 1}
    assert info.json()["unfinished_trials"] == 1
    response = _submit(client, member_headers, source)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "pending_review"
    assert body["parent_job_id"] == source.id
    assert body["harbor_action"]["mode"] == "resume"
    child = store.jobs.get(body["id"])
    preview = client.get(f"/api/jobs/{child.id}/command", headers=member_headers)
    assert preview.status_code == 200, preview.text
    assert "resume" in preview.text
    assert not (runner.output_dir_for(child) / child.id).exists()
    assert (root / "demo__cancelled" / "result.json").exists()
    assert client.delete(f"/api/jobs/{source.id}?purge_outputs=true", headers=member_headers).status_code == 409


def test_native_resume_preserves_completed_trials_and_source(client, member_headers, recorded_job):
    from harbor.job import Job as NativeJob
    from harbor.models.job.config import JobConfig

    source, root = recorded_job
    before = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    # A real Harbor lock ensures relocation does not disable replay integrity checks.
    original = asyncio.run(NativeJob.create(JobConfig.model_validate_json((root / "config.json").read_text())))
    original._init_job_lock()
    original._write_job_lock()
    response = _submit(client, member_headers, source)
    assert response.status_code == 201, response.text
    child = store.jobs.get(response.json()["id"])
    _, work, output = runner.prepare(child)
    target = output / child.id
    assert (target / "lock.json").exists()
    # Exercise Harbor's actual resume CLI including selected-error removal,
    # while replacing only job.run so no container/model is started.
    from harbor.cli import jobs as native_cli
    from unittest.mock import patch
    seen = []

    async def fake_run(job):
        job._init_job_lock()
        job._write_job_lock()
        seen.append(job)
        return None

    with patch.object(NativeJob, "run", fake_run), patch.object(native_cli, "print_job_results_tables"), \
            patch("harbor.environments.docker.docker.DockerEnvironment.preflight"):
        native_cli.resume(job_path=target, filter_error_types=["CancelledError"])
    assert len(seen) == 1
    assert len(seen[0]._existing_trial_results) == 1
    assert seen[0]._existing_trial_results[0].trial_name == "demo__done"
    assert len(seen[0]._remaining_trial_configs) == 2
    assert not (target / "demo__cancelled").exists()
    for name, content in before.items():
        # Native Job.create removes unfinished folders, but never edits result records.
        assert (root / name).read_bytes() == content
    assert (root / "demo__cancelled").exists()


def test_native_regrade_never_runs_agent(client, member_headers, recorded_job):
    from harbor.cli import jobs as native_cli
    from harbor.job import Job as NativeJob
    from unittest.mock import patch

    source, root = recorded_job
    response = _submit(client, member_headers, source, "regrade")
    assert response.status_code == 201, response.text
    child = store.jobs.get(response.json()["id"])
    result, work, output = runner.prepare(child)
    assert result.command[3:5] == ["job", "regrade"]
    assert "--agent" not in result.command
    seen = []

    async def fake_run(job):
        seen.append(job)
        from types import SimpleNamespace
        return SimpleNamespace(trial_results=[])

    with patch.object(NativeJob, "run", fake_run), patch.object(native_cli, "print_job_results_tables"), \
            patch("harbor.environments.docker.docker.DockerEnvironment.preflight"):
        native_cli.regrade(source=str(work / "regrade-source"), task_paths=[Path(source.verify.tasks_path)],
                           jobs_dir=output, job_name=child.id)
    assert len(seen[0]._trial_configs) == 2
    assert all(config.is_regrade for config in seen[0]._trial_configs)
    assert all(config.source_trial.path.is_relative_to(work.resolve()) for config in seen[0]._trial_configs)
    assert (root / "demo__done" / "result.json").exists()


def test_recovery_access_and_state_checks(client, admin_headers, member_headers, recorded_job):
    source, _ = recorded_job
    source.owner_id = "another-owner"
    store.jobs.put(source)
    assert _submit(client, member_headers, source).status_code == 403
    source.status = JobStatus.RUNNING
    store.jobs.put(source)
    assert _submit(client, admin_headers, source).status_code == 409
    source.status = JobStatus.CANCELLED
    store.jobs.put(source)
    assert _submit(client, admin_headers, source, filter_error_types=["--bad"]).status_code == 400
    assert _submit(client, admin_headers, source, dataset_id="new").status_code == 400


def test_regrade_missing_manifest_and_shared_verifier_rejected(client, member_headers, recorded_job):
    source, root = recorded_job
    manifest = root / "demo__done" / "artifacts" / "manifest.json"
    manifest.unlink()
    response = _submit(client, member_headers, source, "regrade")
    assert response.status_code == 400 and "manifest.json" in response.text
    manifest.write_text("[]")
    (Path(source.verify.tasks_path) / "task.toml").write_text('version = "1.0"\n[verifier]\nenvironment_mode = "shared"\n')
    response = _submit(client, member_headers, source, "regrade")
    assert response.status_code == 400 and "separate" in response.text


def test_snapshot_refuses_links_and_cleans_partial_copy(tmp_path, recorded_job):
    source, root = recorded_job
    (root / "escape").symlink_to(tmp_path)
    child = source.model_copy(update={"id": f"job_{uuid4().hex[:12]}",
        "harbor_action": HarborAction(mode="resume", source_job_id=source.id)})
    work, output = tmp_path / "work", tmp_path / "out"
    output.mkdir()
    with pytest.raises(BuildError, match="符号链接"):
        service.prepare_snapshot(child, work, output)
    assert not (output / child.id).exists()
    assert not list(output.glob("*.copying"))


def test_partial_result_retried_only_in_snapshot(client, member_headers, recorded_job):
    source, root = recorded_job
    partial = root / "demo__unfinished" / "result.json"
    partial.write_text('{"config":')
    response = _submit(client, member_headers, source)
    assert response.status_code == 201, response.text
    child = store.jobs.get(response.json()["id"])
    runner.prepare(child)
    assert not (runner.output_dir_for(child) / child.id / "demo__unfinished").exists()
    assert partial.read_text() == '{"config":'


def test_regrade_missing_artifact_bytes_rejected(client, member_headers, recorded_job):
    import shutil
    source, root = recorded_job
    shutil.rmtree(root / "demo__done" / "artifacts" / "logs")
    response = _submit(client, member_headers, source, "regrade")
    assert response.status_code == 400 and "产物记录" in response.text


def test_regrade_can_replace_judge_and_resubmit_stays_regrade(client, member_headers, recorded_job):
    from backend.app.models import ModelApiConfig
    source, _ = recorded_job
    source.verify.model_api_id = "deleted-agent-model"
    source.verify.verifier_env = {"JUDGE_MODEL": "old-override", "JUDGE_API_KEY": "old-secret"}
    store.jobs.put(source)
    judge = ModelApiConfig(name="replacement-judge", provider="openai", model="new-model", api_key="new-test-secret")
    store.model_apis.put(judge)
    response = _submit(client, member_headers, source, "regrade", judge_model_api_id=judge.id, judge_api_protocol="openai")
    assert response.status_code == 201, response.text
    assert "new-test-secret" not in response.text
    child = store.jobs.get(response.json()["id"])
    built = builder.build(child, runner.work_dir_for(child), runner.output_dir_for(child))
    assert built.env["JUDGE_MODEL"] == "new-model"
    assert built.env["JUDGE_API_KEY"] == "new-test-secret"
    assert "new-test-secret" not in built.masked_command_line()
    child.status = JobStatus.FAILED
    store.jobs.put(child)
    repeated = client.post(f"/api/jobs/{child.id}/resubmit", headers=member_headers)
    assert repeated.status_code == 201, repeated.text
    assert repeated.json()["harbor_action"]["mode"] == "regrade"


def test_regrade_can_enable_batch_without_changing_resume_config(client, member_headers, recorded_job):
    import tomllib
    source, _ = recorded_job
    judge = Path(source.verify.tasks_path) / "tests/judge.toml"
    judge.write_text('[judge]\nmode="individual"\n')
    response = _submit(client, member_headers, source, "regrade", batch_judge=True)
    assert response.status_code == 201, response.text
    assert response.json()["verify"]["batch_judge"] is True
    child = store.jobs.get(response.json()["id"])
    built, _, _ = runner.prepare(child)
    path = Path(built.command[built.command.index("--task-path") + 1])
    assert tomllib.loads((path / "tests/judge.toml").read_text())["judge"]["mode"] == "batched"
    assert tomllib.loads(judge.read_text())["judge"]["mode"] == "individual"
    assert not store.jobs.get(source.id).verify.batch_judge
    assert _submit(client, member_headers, source, "resume", batch_judge=True).status_code == 400


def test_scheduler_leaves_exited_managed_process_to_supervisor(tmp_path):
    from backend.app.services.scheduler import Scheduler

    class ExitedProcess:
        def poll(self):
            return 0

        def wait(self):
            return 0

    job = Job(name="fast resume", type=JobType.VERIFY, framework=Framework.HARBOR,
              owner_id="test", status=JobStatus.RUNNING, resources=ResourceSpec(n_gpus=0),
              verify=VerifyConfig(agent="nop"))
    store.jobs.put(job)
    process = ExitedProcess()
    runner._PROCESSES[job.id] = process
    try:
        assert not runner.is_running(job)
        Scheduler()._reconcile()
        assert store.jobs.get(job.id).status == JobStatus.RUNNING
        runner._supervise(job.id, process, (tmp_path / "log").open("wb"))
        assert store.jobs.get(job.id).status == JobStatus.SUCCEEDED
        assert not runner.has_supervisor(job.id)
    finally:
        runner._PROCESSES.pop(job.id, None)
        store.jobs.delete(job.id)
