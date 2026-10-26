"""Platform-owned Harbor recovery sources and immutable source snapshots."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path

from fastapi import HTTPException

from ..builders.base import BuildError
from ..config import settings
from ..models import DatasetKind, Job, JobCreate, JobType, User, VerifyConfig
from ..models.job import HarborAction, HarborActionRequest, HarborTrialActionRequest, HarborRerun
from ..store import store
from . import metrics_parser

SOURCE_LOCK = threading.RLock()


def source_dir(job: Job) -> Path:
    from .runner import output_dir_for
    root = output_dir_for(job).resolve()
    source = metrics_parser.harbor_job_dir(root, job.id)
    if source is None or source.name.endswith(".copying") or not (source / "config.json").is_file():
        raise BuildError("未找到 Harbor 原始任务 config.json，无法恢复或重新判分")
    if not source.resolve().is_relative_to(root):
        raise BuildError("Harbor 记录目录超出该任务的产物目录")
    if source.is_symlink() or output_dir_for(job).is_symlink():
        raise BuildError("Harbor 记录目录不能是符号链接")
    _assert_regular_tree(source)
    return source


def _assert_regular_tree(source: Path) -> None:
    for root, dirs, files in os.walk(source, followlinks=False):
        for name in dirs + files:
            path = Path(root) / name
            if path.is_symlink() or not (path.is_dir() or path.is_file()):
                raise BuildError("原始记录包含符号链接或特殊文件，无法安全复制")


def _source(job_id: str, actor: User) -> Job:
    from . import job_service
    job = job_service.get_job(job_id)
    job_service._assert_can_control(job, actor)
    if job.type != JobType.VERIFY or not job.verify:
        raise HTTPException(400, "仅 Harbor 评测任务支持此操作")
    if not job.status.is_terminal:
        raise HTTPException(409, "请先等待任务结束或终止任务，再恢复或重新判分")
    return job


def _check(source: Path, task_path: Path | None = None, trial_names: list[str] | None = None,
           *, check_lock: bool = False) -> dict:
    from ..builders.harbor import resolve_harbor_bin, resolve_harbor_python
    binary = resolve_harbor_bin()
    try:
        result = subprocess.run(
            [resolve_harbor_python(binary), str(settings.resources_dir / "harbor_action_check.py")],
            input=json.dumps({"source": str(source), "task_path": str(task_path) if task_path else None,
                              "trial_names": trial_names, "check_lock": check_lock}),
            capture_output=True, text=True, timeout=45,
        )
        data = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise BuildError("Harbor 恢复检查失败，请检查 Harbor 解释器配置和记录目录") from exc
    if result.returncode or not data.get("ok"):
        raise BuildError(data.get("error", "Harbor 恢复检查失败"))
    return data


def recorded_task_root(source: Job) -> Path | None:
    """The task tree the source job actually ran with.

    Platform-image and Batch Judge runs execute a job-local task snapshot, and
    Harbor records it in ``config.json``. Reusing that snapshot keeps trial task
    names/content identical even when the original upload was a single task
    directory or the dataset has since been replaced.
    """
    try:
        config = json.loads((source_dir(source) / "config.json").read_text(encoding="utf-8"))
    except (BuildError, OSError, ValueError):
        return None
    entries = (config.get("tasks") or []) + (config.get("datasets") or [])
    paths = [Path(entry["path"]) for entry in entries
             if isinstance(entry, dict) and entry.get("path")]
    unique = list(dict.fromkeys(paths))
    if len(unique) != 1 or not unique[0].is_dir():
        return None
    return unique[0]


def verifier_path(config: VerifyConfig) -> Path:
    if config.dataset_id:
        dataset = store.datasets.get(config.dataset_id)
        if dataset is None or dataset.kind != DatasetKind.HARBOR_TASKS or dataset.status.value != "ready":
            raise BuildError("请选择已就绪的 Harbor 评分任务集")
        path = Path(dataset.root_path)
    elif config.tasks_path:
        path = Path(config.tasks_path)
    else:
        raise BuildError("请先导入评分任务集；重新判分需要可检查的本地任务目录")
    if not path.is_dir():
        raise BuildError("评分任务目录已不存在")
    return path


def inspect_job(job_id: str, *, actor: User) -> dict:
    source = _source(job_id, actor)
    try:
        return _check(source_dir(source))
    except BuildError as exc:
        raise HTTPException(400, str(exc)) from exc


def create_action(job_id: str, request: HarborActionRequest, *, actor: User) -> Job:
    # Serialize the dependency registration with deletion of its source.
    with SOURCE_LOCK:
        return _create_action(job_id, request, actor=actor)


def _create_action(job_id: str, request: HarborActionRequest, *, actor: User) -> Job:
    from . import job_service
    source = _source(job_id, actor)
    config = source.verify.model_copy(deep=True)
    filters = list(dict.fromkeys(request.filter_error_types))
    if not filters or any(not re.fullmatch(r"[A-Za-z_][\w.]{0,150}", value) for value in filters):
        raise HTTPException(400, "请至少选择一个有效的错误类型（默认 CancelledError）")
    if request.mode == "resume" and any(value is not None for value in (
        request.dataset_id, request.judge_model_api_id, request.judge_api_protocol, request.batch_judge,
    )):
        raise HTTPException(400, "恢复必须沿用原任务配置；如需更换任务集或 Judge，请使用重新判分")
    if request.mode == "regrade":
        config.disable_verification = False
        config.use_platform_image = False
        if request.batch_judge is not None:
            config.batch_judge = request.batch_judge
        if request.dataset_id is not None:
            config.dataset_id = request.dataset_id
            config.tasks_path = ""
            config.registry_dataset = ""
        else:
            # Reuse the exact task tree the source ran with (platform-image /
            # Batch Judge snapshots included). A single-task dataset would
            # otherwise be re-resolved under a different task name, and a
            # replaced or deleted dataset would make regrade impossible.
            recorded = recorded_task_root(source)
            if recorded is not None:
                config.dataset_id = ""
                config.registry_dataset = ""
                config.tasks_path = str(recorded)
        if request.judge_model_api_id is not None:
            config.judge_model_api_id = request.judge_model_api_id
            # An explicitly selected judge must win over old manual overrides.
            config.verifier_env = {k: v for k, v in config.verifier_env.items() if not k.startswith("JUDGE_")}
        if request.judge_api_protocol is not None:
            config.judge_api_protocol = request.judge_api_protocol
    try:
        checked = _check(source_dir(source), verifier_path(config) if request.mode == "regrade" else None,
                         check_lock=request.mode == "resume")
    except BuildError as exc:
        raise HTTPException(400, str(exc)) from exc
    action = HarborAction(mode=request.mode, source_job_id=source.id, filter_error_types=filters,
                          lock_stale=bool(checked.get("lock_stale")) and request.mode == "resume")
    label = "中断恢复" if action.mode == "resume" else "重新判分"
    payload = JobCreate(
        name=f"{source.name[:108]} ({label})", note=source.note, type=JobType.VERIFY,
        project_id=source.project_id, tags=source.tags, parent_job_id=source.id,
        resources=source.resources.model_copy(update={"gpu_ids": []}), verify=config,
    )
    return job_service.create_job(payload, owner=actor, harbor_action=action)


def create_trial_actions(job_id: str, request: HarborTrialActionRequest, *, actor: User) -> list[Job]:
    from . import harbor_trials, job_service

    with SOURCE_LOCK:
        source = _source(job_id, actor)
        names = list(dict.fromkeys(request.trial_names))
        try:
            root = source_dir(source)
            states = {row.selection_id or row.trial_name: row for row in
                      harbor_trials.scan(root, job_status=source.status.value)}
            for name in names:
                state = states.get(name)
                if state is None:
                    raise BuildError(f"选中的记录不存在或已变化：{name}")
                if request.mode == "resume" and state.phase == "success":
                    raise BuildError(f"{state.trial_name} 已完成，请使用重新运行或直接评分")
                if request.mode == "regrade" and not state.path:
                    raise BuildError(f"{state.trial_name} 尚未执行，没有可评分的产物")
            regrade_root = recorded_task_root(source) if request.mode == "regrade" else None
            checked = _check(root, (regrade_root or verifier_path(source.verify))
                             if request.mode == "regrade" else None, names)
        except BuildError as exc:
            raise HTTPException(400, str(exc)) from exc
        if request.mode == "rerun":
            return [_rerun_in_place(source, checked["selected"], actor)]
        prepared = []
        label = {"rerun": "重跑", "regrade": "评分", "resume": "恢复"}[request.mode]
        for row in checked["selected"]:
            config = source.verify.model_copy(deep=True)
            native = row["job_config"]
            config.n_attempts = 1
            config.n_tasks = None
            config.task_names = [states[row["trial_name"]].task_name]
            config.exclude_task_names = []
            if request.mode == "regrade":
                config.disable_verification = False
                config.use_platform_image = False
                if regrade_root is not None:
                    config.dataset_id = ""
                    config.registry_dataset = ""
                    config.tasks_path = str(regrade_root)
            action = HarborAction(mode=request.mode, source_job_id=source.id,
                                  trial_name=row["trial_name"], selected_job_config=native)
            payload = JobCreate(
                name=f"{source.name[:50]} ({label}) {row['trial_name'][:60]}",
                note=source.note, type=JobType.VERIFY, project_id=source.project_id,
                tags=source.tags, parent_job_id=source.id,
                resources=source.resources.model_copy(deep=True, update={"gpu_ids": []}), verify=config,
            )
            prepared.append(job_service._prepare_job(payload, owner=actor, harbor_action=action))
        jobs = [job for job, _ in prepared]
        store.jobs.put_many(jobs)
        for job, project_name in prepared:
            job_service._announce_created_job(job, owner=actor, project_name=project_name)
        if any(job.status.value == "queued" for job in jobs):
            job_service.scheduler.dispatch()
        return jobs


def _rerun_in_place(source: Job, selections: list[dict], actor: User) -> Job:
    from ..models import JobRuntime, JobProgress, utcnow
    from . import audit_service, job_service, runner

    # A queued descendant must not snapshot records while they are being replaced.
    if store.jobs.find(lambda item: item.harbor_action is not None
                       and item.harbor_action.source_job_id == source.id
                       and not item.status.is_terminal):
        raise HTTPException(409, "还有待执行的恢复或评分任务引用原记录，请等待结束或先取消")
    payload = JobCreate(name=source.name, note=source.note, type=source.type,
                        project_id=source.project_id, tags=source.tags,
                        resources=source.resources.model_copy(deep=True, update={"gpu_ids": []}),
                        verify=source.verify.model_copy(deep=True))
    validated, _ = job_service._prepare_job(
        payload, owner=actor, harbor_action=HarborAction(mode="rerun", source_job_id=source.id))
    with runner.LIFECYCLE_LOCK:
        if runner.has_supervisor(source.id):
            raise HTTPException(409, "原任务仍在收尾，请稍后重试")

        def mutate(item: Job) -> None:
            item.harbor_rerun = HarborRerun(selections=selections)
            item.status = validated.status
            item.review = validated.review
            item.queued_at = validated.queued_at
            item.runtime = JobRuntime()
            item.progress = JobProgress(message=f"等待原地重跑 {len(selections)} 条数据")
            item.resources.gpu_ids = []
            item.updated_at = utcnow()

        updated = store.jobs.update(source.id, mutate)
    audit_service.record("job.rerun_trials", actor=actor, target_type="job", target_id=source.id,
                         summary=f"原地重跑「{source.name}」的 {len(selections)} 条数据",
                         detail={"trial_names": [row["trial_name"] for row in selections]})
    if updated.status.value == "queued":
        job_service.scheduler.dispatch()
    return updated


def selected_config(job: Job, output_dir: Path) -> dict:
    """A new native plan contains exactly one selected attempt and no inherited results."""
    import copy
    config = copy.deepcopy(job.harbor_action.selected_job_config)
    config.update(job_name=job.id, jobs_dir=str(output_dir), source_jobs=[], datasets=[], n_attempts=1)
    return config


def snapshot_dir(job: Job, work_dir: Path, output_dir: Path) -> Path:
    return output_dir / job.id if job.harbor_action.mode in {"resume", "rerun"} else work_dir / "regrade-source"


def prepare_snapshot(job: Job, work_dir: Path, output_dir: Path, *, selected_native: dict | None = None, environment_refs: dict | None = None) -> None:
    """Only called for execution, never from command preview. Atomic publication."""
    action = job.harbor_action
    source = store.jobs.get(action.source_job_id)
    if source is None or not source.status.is_terminal:
        raise BuildError("原任务不存在或仍在运行")
    original = source_dir(source)
    destination = snapshot_dir(job, work_dir, output_dir)
    if destination.exists():
        return
    staging = destination.with_name(destination.name + ".copying")
    try:
        if staging.exists():
            shutil.rmtree(staging)
        if action.trial_name:
            staging.mkdir(parents=True)
            if action.mode == "regrade":
                # Copy only the selected attempt, never siblings or aggregate results.
                trial = original / action.trial_name
                if not trial.is_dir() or trial.parent != original:
                    raise BuildError("选中的 trial 记录已不存在")
                shutil.copytree(trial, staging / trial.name)
                shutil.copy2(original / "config.json", staging / "config.json")
            else:
                native = selected_native or selected_config(job, output_dir)
                (staging / "config.json").write_text(json.dumps(native, ensure_ascii=False), encoding="utf-8")
        else:
            shutil.copytree(original, staging)
        if action.mode == "resume" and not action.trial_name:
            config_path = staging / "config.json"
            config = json.loads(config_path.read_text())
            config.update(job_name=job.id, jobs_dir=str(output_dir))
            if environment_refs:
                for agent in config.get("agents", []):
                    agent.setdefault("env", {}).update(environment_refs["agent"])
                config.setdefault("verifier", {}).setdefault("env", {}).update(environment_refs["verifier"])
            config_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
            # Harbor compares the recorded lock.json (agent/verifier env included)
            # with the resolved one before running; it must carry the same refs.
            if environment_refs:
                _rebase_lock(staging / "lock.json", environment_refs)
            if action.lock_stale:
                # Harbor refuses to resume when the recorded task digest no
                # longer matches; a rebuilt lock keeps completed trials while
                # letting unfinished/failed ones run against the current tree.
                (staging / "lock.json").unlink(missing_ok=True)
            # Existing TrialConfig equality includes trials_dir. Rebase every
            # recorded trial, so completed work is recognized in the new job.
            for trial in staging.iterdir():
                if not trial.is_dir():
                    continue
                result_path = trial / "result.json"
                try:
                    record = json.loads(result_path.read_text())
                    block = record["config"]
                    block["trials_dir"] = str(destination)
                    if environment_refs:
                        for stage in ("agent", "verifier"):
                            block.setdefault(stage, {}).setdefault("env", {}).update(environment_refs[stage])
                except (OSError, ValueError, KeyError, TypeError):
                    # Abrupt termination may leave a partial JSON file. Retry
                    # that trial from scratch; all evidence remains in source.
                    shutil.rmtree(trial)
                    continue
                record["trial_uri"] = (destination / trial.name).as_uri()
                result_path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
                (trial / "config.json").write_text(json.dumps(block, ensure_ascii=False), encoding="utf-8")
            # Harbor recomputes the aggregate from retained trial results.
            # A new platform run must not inherit a stale aggregate/Hub UUID.
            (staging / "result.json").unlink(missing_ok=True)
        staging.rename(destination)
    except BuildError:
        raise
    except (OSError, ValueError) as exc:
        raise BuildError("无法复制 Harbor 原始记录，请检查磁盘空间和文件权限") from exc
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def _rebase_lock(path: Path, environment_refs: dict) -> None:
    if not path.is_file():
        return
    try:
        lock = json.loads(path.read_text())
        for trial in lock["trials"]:
            for stage in ("agent", "verifier"):
                trial.setdefault(stage, {}).setdefault("env", {}).update(environment_refs[stage])
    except (ValueError, KeyError, TypeError, AttributeError):
        # Harbor refuses to overwrite an unparsable lock; let it rebuild one.
        path.unlink()
        return
    path.write_text(json.dumps(lock, ensure_ascii=False), encoding="utf-8")
