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
    judge = ModelApiConfig(name="replacement-judge", provider="openai", model="new-model")
    store.model_apis.put(judge)
    client.put(f"/api/model-apis/my-keys/{judge.id}", headers=member_headers, json={"api_key": "new-test-secret"})
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


def _selected(client, headers, source, mode, names):
    return client.post(f"/api/jobs/{source.id}/trial-actions", headers=headers,
                       json={"mode": mode, "trial_names": names})


@pytest.mark.parametrize("mode, trial_name", [("resume", "demo__cancelled")])
def test_selected_attempt_runs_exactly_once(client, member_headers, recorded_job, mode, trial_name):
    from harbor.job import Job as NativeJob
    from harbor.models.job.config import JobConfig
    source, root = recorded_job
    before = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    response = _selected(client, member_headers, source, mode, [trial_name])
    assert response.status_code == 201, response.text
    row = response.json()[0]
    assert row["harbor_action"]["trial_name"] == trial_name
    assert "selected_job_config" not in row["harbor_action"]
    assert row["verify"]["n_attempts"] == 1
    child = store.jobs.get(row["id"])
    built, work, output = runner.prepare(child)
    config = JobConfig.model_validate_json((output / child.id / "config.json").read_text())
    assert config.n_attempts == 1
    assert config.datasets == [] and config.source_jobs == []
    native = asyncio.run(NativeJob.create(config))
    assert len(native._remaining_trial_configs) == 1
    assert native._remaining_trial_configs[0].task.path == Path(source.verify.tasks_path)
    assert not native._remaining_trial_configs[0].is_regrade
    native._close_logger_handlers()
    assert not (output / child.id / "demo__done").exists()
    assert not (output / child.id / "demo__cancelled").exists()
    assert "resume" in built.command if mode == "resume" else "run" in built.command
    for name, content in before.items():
        assert (root / name).read_bytes() == content


def test_selected_regrade_ignores_unselected_missing_artifacts(client, member_headers, recorded_job):
    from harbor.cli import jobs as native_cli
    from harbor.job import Job as NativeJob
    from unittest.mock import patch
    source, root = recorded_job
    (root / "demo__cancelled/artifacts/manifest.json").unlink()
    response = _selected(client, member_headers, source, "regrade", ["demo__done"])
    assert response.status_code == 201, response.text
    child = store.jobs.get(response.json()[0]["id"])
    built, work, output = runner.prepare(child)
    snapshot = work / "regrade-source"
    assert (snapshot / "demo__done/artifacts/manifest.json").is_file()
    assert not (snapshot / "demo__cancelled").exists()
    assert not (snapshot / "demo__unfinished").exists()
    seen = []

    async def fake_run(job):
        seen.append(job)
        from types import SimpleNamespace
        return SimpleNamespace(trial_results=[])

    with patch.object(NativeJob, "run", fake_run), patch.object(native_cli, "print_job_results_tables"), \
            patch("harbor.environments.docker.docker.DockerEnvironment.preflight"):
        native_cli.regrade(source=str(snapshot), task_paths=[Path(source.verify.tasks_path)],
                          jobs_dir=output, job_name=child.id)
    assert len(seen[0]._trial_configs) == 1
    assert seen[0]._trial_configs[0].is_regrade
    assert "--agent" not in built.command


def test_selected_batch_is_atomic_and_checks_permissions(client, admin_headers, member_headers, recorded_job):
    source, _ = recorded_job
    before = store.jobs.count()
    for mode, names in [("rerun", ["demo__done", "../outside"]),
                        ("resume", ["demo__cancelled", "demo__done"]),
                        ("rerun", ["demo__done", "demo__unfinished"])]:
        response = _selected(client, member_headers, source, mode, names)
        assert response.status_code == 400, response.text
        assert store.jobs.count() == before
    source.owner_id = "other-owner"
    store.jobs.put(source)
    assert _selected(client, member_headers, source, "rerun", ["demo__done"]).status_code == 403
    source.status = JobStatus.RUNNING
    store.jobs.put(source)
    assert _selected(client, admin_headers, source, "rerun", ["demo__done"]).status_code == 409


def test_selected_batch_reruns_original_job_once(client, member_headers, recorded_job):
    source, root = recorded_job
    count = store.jobs.count()
    before = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    response = _selected(client, member_headers, source, "rerun", ["demo__done", "demo__cancelled", "demo__done"])
    assert response.status_code == 201, response.text
    rows = response.json()
    assert len(rows) == 1 and rows[0]["id"] == source.id
    assert rows[0]["name"] == source.name
    assert rows[0]["status"] == "pending_review"
    assert rows[0]["parent_job_id"] == source.parent_job_id
    assert "harbor_rerun" not in rows[0]
    assert store.jobs.count() == count
    updated = store.jobs.get(source.id)
    assert len(updated.harbor_rerun.selections) == 2
    built, _, _ = runner.prepare(updated)
    assert "harbor_inplace_rerun.py" in built.command[2]
    plan = json.loads(built.files["inplace-rerun.json"])
    assert {row["trial_name"] for row in plan["trials"]} == {"demo__done", "demo__cancelled"}
    assert all(Path(row["trials_dir"]) == root.resolve() for row in plan["trials"])
    assert _selected(client, member_headers, source, "rerun", ["demo__done"]).status_code == 409
    for name, content in before.items():
        assert (root / name).read_bytes() == content


def test_selected_pending_uses_lock_identity_and_one_attempt(client, member_headers, recorded_job):
    from harbor.job import Job as NativeJob
    from harbor.models.job.config import JobConfig
    from backend.app.services import harbor_trials
    source, root = recorded_job
    original = asyncio.run(NativeJob.create(JobConfig.model_validate_json((root / "config.json").read_text())))
    original._init_job_lock()
    original._write_job_lock()
    original._close_logger_handlers()
    states = harbor_trials.scan(root, job_status=source.status.value)
    pending = [row for row in states if row.selection_id]
    assert len(pending) == 1
    response = _selected(client, member_headers, source, "resume", [pending[0].selection_id])
    assert response.status_code == 201, response.text
    child = store.jobs.get(response.json()[0]["id"])
    built, work, output = runner.prepare(child)
    config = JobConfig.model_validate_json((output / child.id / "config.json").read_text())
    assert config.n_attempts == 1 and len(config.tasks) == 1
    assert config.tasks[0].path == Path(source.verify.tasks_path)
    assert _selected(client, member_headers, source, "regrade", [pending[0].selection_id]).status_code == 400


def test_selected_credentials_stay_in_environment(client, member_headers, recorded_job):
    source, _ = recorded_job
    source.verify.agent_env = {"PRIVATE_API_KEY": "secret-for-selected-trial"}
    store.jobs.put(source)
    response = _selected(client, member_headers, source, "rerun", ["demo__done"])
    assert response.status_code == 201, response.text
    assert "secret-for-selected-trial" not in response.text
    child = store.jobs.get(response.json()[0]["id"])
    built, work, output = runner.prepare(child)
    assert "secret-for-selected-trial" in built.secrets
    assert "secret-for-selected-trial" not in built.files["inplace-rerun.json"]
    assert built.env["PRT_SELECTED_AGENT_PRIVATE_API_KEY"] == "secret-for-selected-trial"
    assert json.loads(built.files["inplace-rerun.json"])["trials"][0]["agent"]["env"]["PRIVATE_API_KEY"] == "${PRT_SELECTED_AGENT_PRIVATE_API_KEY}"


def test_inplace_native_execution_replaces_only_selected_result(client, member_headers, recorded_job, monkeypatch):
    from backend.app.resources import harbor_inplace_rerun as runtime
    from harbor.trial.trial import Trial
    from harbor.models.trial.result import TrialResult
    from harbor.models.job.result import JobResult

    source, root = recorded_job
    source.verify.agent_env = {"PRIVATE_API_KEY": "new-runtime-secret"}
    store.jobs.put(source)
    before = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    response = _selected(client, member_headers, source, "rerun", ["demo__done"])
    assert response.status_code == 201, response.text
    built, _, _ = runner.prepare(store.jobs.get(source.id))
    plan = json.loads(built.files["inplace-rerun.json"])
    seen = []
    record = json.loads(before["demo__done/result.json"])

    class FakeTrial:
        def __init__(self, config):
            self.config = config

        def add_hook(self, *args):
            return self

        async def run(self):
            seen.append(self.config)
            record["config"] = self.config.model_dump(mode="json")
            record["verifier_result"] = {"rewards": {"reward": 0.25}}
            result = TrialResult.model_validate(record)
            (root / self.config.trial_name / "result.json").write_text(result.model_dump_json())
            return result

    async def fake_create(config):
        return FakeTrial(config)

    monkeypatch.setattr(Trial, "create", fake_create)
    for key, value in built.env.items():
        monkeypatch.setenv(key, value)
    asyncio.run(runtime.run(plan))
    assert [trial.trial_name for trial in seen] == ["demo__done"]
    assert seen[0].trials_dir == root.resolve()
    from harbor.models.trial.config import TrialConfig
    assert TrialConfig.model_validate_json((root / "demo__done/config.json").read_text()) == TrialConfig.model_validate_json(before["demo__done/config.json"])
    for name, content in before.items():
        if not name.startswith("demo__done/"):
            assert (root / name).read_bytes() == content
    assert (root / "demo__unfinished").is_dir()
    assert (Path(plan["backup"]) / "demo__done/result.json").read_bytes() == before["demo__done/result.json"]
    aggregate = JobResult.model_validate_json((root / "result.json").read_text())
    assert aggregate.stats.n_completed_trials == 2
    assert aggregate.stats.n_errored_trials == 1
    assert aggregate.stats.evals["nop__adhoc"].reward_stats["reward"][0.25] == ["demo__done"]
    assert aggregate.finished_at is not None
    with pytest.raises(ValueError, match="already started"):
        asyncio.run(runtime.run(plan))


def test_inplace_rejects_active_source_dependencies(client, member_headers, recorded_job):
    source, _ = recorded_job
    child = _selected(client, member_headers, source, "regrade", ["demo__done"])
    assert child.status_code == 201
    response = _selected(client, member_headers, source, "rerun", ["demo__done"])
    assert response.status_code == 409
    assert store.jobs.get(source.id).status == JobStatus.CANCELLED


@pytest.mark.parametrize("cancelled", [False, True])
def test_inplace_interruption_keeps_backup_and_unselected_records(client, member_headers, recorded_job, monkeypatch, cancelled):
    from backend.app.resources import harbor_inplace_rerun as runtime
    from harbor.trial.queue import TrialQueue
    from harbor.models.job.result import JobResult

    source, root = recorded_job
    before = (root / "demo__done/result.json").read_bytes()
    other = (root / "demo__cancelled/result.json").read_bytes()
    response = _selected(client, member_headers, source, "rerun", ["demo__done"])
    assert response.status_code == 201
    built, _, _ = runner.prepare(store.jobs.get(source.id))
    plan = json.loads(built.files["inplace-rerun.json"])

    async def interrupt(self, config):
        if cancelled:
            asyncio.current_task().get_loop().call_soon(asyncio.current_task().cancel)
            await asyncio.sleep(0)
        raise RuntimeError("simulated launch failure")

    monkeypatch.setattr(TrialQueue, "_run_trial", interrupt)
    # TaskGroup treats an individually cancelled trial as completed; the runtime
    # must still avoid reporting the operation as successfully finished.
    with pytest.raises(asyncio.CancelledError if cancelled else ExceptionGroup):
        asyncio.run(runtime.run(plan))
    assert (Path(plan["backup"]) / "demo__done/result.json").read_bytes() == before
    assert (root / "demo__cancelled/result.json").read_bytes() == other
    assert not (root / "demo__done/result.json").exists()
    assert (root / "demo__unfinished").is_dir()
    assert JobResult.model_validate_json((root / "result.json").read_text()).finished_at is None


def test_whole_resume_preserves_separate_stage_keys_and_native_config_matching(client, member_headers, recorded_job, monkeypatch):
    from harbor.job import Job as NativeJob
    from harbor.models.job.config import JobConfig
    from harbor.utils.env import resolve_env_vars
    source, root = recorded_job
    source.verify.agent_env = {"OPENAI_API_KEY": "resume-agent-private"}
    source.verify.verifier_env = {"OPENAI_API_KEY": "resume-judge-private"}
    store.jobs.put(source)
    response = _submit(client, member_headers, source, "resume")
    assert response.status_code == 201, response.text
    child = store.jobs.get(response.json()["id"])
    built, _, output = runner.prepare(child)
    for key, value in built.env.items():
        monkeypatch.setenv(key, value)
    config = JobConfig.model_validate_json((output / child.id / "config.json").read_text())
    assert resolve_env_vars(config.agents[0].env)["OPENAI_API_KEY"] == "resume-agent-private"
    assert resolve_env_vars(config.verifier.env)["OPENAI_API_KEY"] == "resume-judge-private"
    native = asyncio.run(NativeJob.create(config))
    try:
        assert len(native._existing_trial_results) == 2
        assert len(native._remaining_trial_configs) == 1
        assert "resume-agent-private" not in built.masked_command_line()
        assert "resume-judge-private" not in built.masked_command_line()
    finally:
        native._close_logger_handlers()


def test_whole_resume_accepts_recorded_lock_with_stage_env(client, member_headers, recorded_job, monkeypatch):
    """A real run writes lock.json with the original env refs; resume swaps refs."""
    from harbor.cli import jobs as native_cli
    from harbor.job import Job as NativeJob
    from harbor.models.job.config import JobConfig
    from harbor.models.trial.config import TrialConfig
    from unittest.mock import patch

    source, root = recorded_job
    source.verify.agent_env = {"OPENAI_API_KEY": "resume-agent-private"}
    source.verify.verifier_env = {"JUDGE_API_KEY": "resume-judge-private"}
    store.jobs.put(source)
    # Mirror what the original platform run recorded: env as ${PRT_<STAGE>_*} refs.
    native = JobConfig.model_validate_json((root / "config.json").read_text())
    native.agents[0].env = {"OPENAI_API_KEY": "${PRT_AGENT_OPENAI_API_KEY}"}
    native.verifier.env = {"JUDGE_API_KEY": "${PRT_VERIFIER_JUDGE_API_KEY}"}
    (root / "config.json").write_text(native.model_dump_json())
    for name in ("demo__done", "demo__cancelled"):
        trial = TrialConfig.model_validate_json((root / name / "config.json").read_text())
        trial.agent, trial.verifier = native.agents[0], native.verifier
        (root / name / "config.json").write_text(trial.model_dump_json())
        record = json.loads((root / name / "result.json").read_text())
        record["config"] = json.loads(trial.model_dump_json())
        (root / name / "result.json").write_text(json.dumps(record))
    with monkeypatch.context() as original_env:
        original_env.setenv("PRT_AGENT_OPENAI_API_KEY", "old-run")
        original = asyncio.run(NativeJob.create(native))
    original._init_job_lock()
    original._write_job_lock()
    original._close_logger_handlers()

    response = _submit(client, member_headers, source)
    assert response.status_code == 201, response.text
    child = store.jobs.get(response.json()["id"])
    built, _, output = runner.prepare(child)
    target = output / child.id
    assert "resume-agent-private" not in (target / "lock.json").read_text()
    # Only what the platform injects; stale ${PRT_AGENT_*} refs would fail here.
    for key, value in built.env.items():
        monkeypatch.setenv(key, value)
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
    assert [result.trial_name for result in seen[0]._existing_trial_results] == ["demo__done"]


def test_codex_resume_waits_for_mcp_without_rewriting_historical_configs(client, member_headers, recorded_job, monkeypatch):
    from harbor.agents.factory import AgentFactory
    from harbor.agents.installed.codex import Codex
    from harbor.cli import jobs as native_cli
    from harbor.environments.docker.docker import DockerEnvironment
    from harbor.job import Job as NativeJob
    from harbor.models.job.config import JobConfig
    from harbor.models.trial.config import AgentConfig, TrialConfig
    from harbor.trial.trial import Trial
    from backend.app.resources import harbor_runtime
    from unittest.mock import patch

    source, root = recorded_job
    old_config = {
        "model_provider": "custom",
        "model_providers": {"custom": {"base_url": "http://model:8100/v1", "wire_api": "responses"}},
        "mcp_servers": {"world": {"url": "http://world:8000/mcp/"}},
    }
    source.verify.agent, source.verify.model = "codex", "gpt-4o"
    store.jobs.put(source)
    native = JobConfig.model_validate_json((root / "config.json").read_text())
    native.agents = [AgentConfig(name="codex", model_name="gpt-4o", kwargs={"config": old_config})]
    (root / "config.json").write_text(native.model_dump_json())
    for name in ("demo__done", "demo__cancelled"):
        trial = TrialConfig.model_validate_json((root / name / "config.json").read_text())
        trial.agent = native.agents[0]
        (root / name / "config.json").write_text(trial.model_dump_json())
        record = json.loads((root / name / "result.json").read_text())
        record["config"] = json.loads(trial.model_dump_json())
        (root / name / "result.json").write_text(json.dumps(record))
    original = asyncio.run(NativeJob.create(native))
    original._init_job_lock()
    original._write_job_lock()
    original._close_logger_handlers()
    before = {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}

    response = _submit(client, member_headers, source)
    assert response.status_code == 201, response.text
    child = store.jobs.get(response.json()["id"])
    _, _, output = runner.prepare(child)
    target = output / child.id
    # Restore every globally patched class method after exercising the real entry guard.
    for cls, names in ((Codex, ("_build_effective_config",)), (Trial, ("_emit",)),
                       (DockerEnvironment, ("exec", "_collect_buffered_output", "_collect_streamed_output"))):
        for name in names:
            if hasattr(cls, name):
                monkeypatch.setattr(cls, name, getattr(cls, name))
    harbor_runtime.install_guards()
    seen = []

    async def fake_run(job):
        job._init_job_lock()
        job._write_job_lock()
        seen.append(job)
        for trial in job._remaining_trial_configs:
            assert trial.agent.kwargs["config"] == old_config
            agent = AgentFactory.create_agent_from_config(trial.agent, logs_dir=target / "agent-check")
            effective = agent._build_effective_config()
            assert effective == {**old_config, "mcp_optional_startup_grace_ms": 0}
        return None

    with patch.object(NativeJob, "run", fake_run), patch.object(native_cli, "print_job_results_tables"), \
            patch.object(DockerEnvironment, "preflight"):
        native_cli.resume(job_path=target, filter_error_types=["CancelledError"])
    assert len(seen) == 1
    assert [result.trial_name for result in seen[0]._existing_trial_results] == ["demo__done"]
    assert len(seen[0]._remaining_trial_configs) == 2
    assert "mcp_optional_startup_grace_ms" not in (target / "config.json").read_text()
    assert "mcp_optional_startup_grace_ms" not in (target / "lock.json").read_text()
    assert "mcp_optional_startup_grace_ms" not in (target / "demo__done" / "result.json").read_text()
    for name, content in before.items():
        assert (root / name).read_bytes() == content


def test_regrade_adapts_rewardkit_entry_of_selected_grading_set(client, member_headers, recorded_job):
    from backend.app.services.harbor_compat import RUNNER_NAME
    source, _ = recorded_job
    script = Path(source.verify.tasks_path) / "tests" / "test.sh"
    script.write_text("#!/bin/bash\npython3 -m rewardkit /tests --workspace /app\n")
    response = _submit(client, member_headers, source, "regrade")
    assert response.status_code == 201, response.text
    built, _, _ = runner.prepare(store.jobs.get(response.json()["id"]))
    assert f"python3 /tests/{RUNNER_NAME}" in script.read_text()
    assert (script.parent / RUNNER_NAME).is_file()
    assert any("reward-kit" in note for note in built.notes)


def test_regrade_uses_recorded_task_snapshot(client, member_headers, recorded_job, tmp_path):
    """A replaced/deleted dataset must not break regrade: the source job's own
    task tree (platform-image or Batch Judge snapshot) is authoritative."""
    import shutil
    source, root = recorded_job
    recorded = tmp_path / "recorded" / Path(source.verify.tasks_path).name
    shutil.copytree(source.verify.tasks_path, recorded)
    native = json.loads((root / "config.json").read_text())
    native["tasks"][0]["path"] = str(recorded)
    (root / "config.json").write_text(json.dumps(native, ensure_ascii=False))
    source.verify.tasks_path = str(tmp_path / "deleted-upload")
    store.jobs.put(source)

    response = _submit(client, member_headers, source, "regrade")
    assert response.status_code == 201, response.text
    child = store.jobs.get(response.json()["id"])
    assert child.verify.tasks_path == str(recorded)
    built, _, _ = runner.prepare(child)
    assert built.command[built.command.index("--task-path") + 1] == str(recorded)


def test_recovery_never_requires_the_source_dataset(client, member_headers, recorded_job):
    """The original dataset may be gone after the run; resume/regrade still use
    the recorded configuration and task tree."""
    source, _ = recorded_job
    source.verify.dataset_id = "ds_removed"
    source.verify.tasks_path = ""
    store.jobs.put(source)
    for mode in ("resume", "regrade"):
        response = _submit(client, member_headers, source, mode)
        assert response.status_code == 201, (mode, response.text)
        child = store.jobs.get(response.json()["id"])
        assert child.verify.dataset_id == ("" if mode == "regrade" else "ds_removed")
        runner.prepare(child)


def test_whole_resume_rebuilds_stale_lock_after_task_change(client, member_headers, recorded_job):
    """Editing/re-importing the task pack changes its digest; Harbor rejects the
    old lock. Resume must drop the stale lock instead of failing the task."""
    from harbor.cli import jobs as native_cli
    from harbor.job import Job as NativeJob
    from harbor.models.job.config import JobConfig
    from unittest.mock import patch

    source, root = recorded_job
    original = asyncio.run(NativeJob.create(JobConfig.model_validate_json((root / "config.json").read_text())))
    original._init_job_lock()
    original._write_job_lock()
    original._close_logger_handlers()
    (Path(source.verify.tasks_path) / "instruction.md").write_text("Updated instructions")

    response = _submit(client, member_headers, source)
    assert response.status_code == 201, response.text
    child = store.jobs.get(response.json()["id"])
    assert child.harbor_action.lock_stale is True
    built, _, output = runner.prepare(child)
    target = output / child.id
    assert not (target / "lock.json").exists()
    seen = []

    async def fake_run(job):
        job._init_job_lock()
        job._write_job_lock()
        seen.append(job)
        return None

    with patch.object(NativeJob, "run", fake_run), patch.object(native_cli, "print_job_results_tables"), \
            patch("harbor.environments.docker.docker.DockerEnvironment.preflight"):
        native_cli.resume(job_path=target, filter_error_types=["CancelledError"])
    assert [result.trial_name for result in seen[0]._existing_trial_results] == ["demo__done"]
    assert (target / "lock.json").exists()
