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
from ..models.job import HarborAction, HarborActionRequest
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


def _check(source: Path, task_path: Path | None = None) -> dict:
    from ..builders.harbor import resolve_harbor_bin, resolve_harbor_python
    binary = resolve_harbor_bin()
    try:
        result = subprocess.run(
            [resolve_harbor_python(binary), str(settings.resources_dir / "harbor_action_check.py")],
            input=json.dumps({"source": str(source), "task_path": str(task_path) if task_path else None}),
            capture_output=True, text=True, timeout=45,
        )
        data = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise BuildError("Harbor 恢复检查失败，请检查 Harbor 解释器配置和记录目录") from exc
    if result.returncode or not data.get("ok"):
        raise BuildError(data.get("error", "Harbor 恢复检查失败"))
    return data


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
        request.dataset_id, request.judge_model_api_id, request.judge_api_protocol,
    )):
        raise HTTPException(400, "恢复必须沿用原任务配置；如需更换任务集或 Judge，请使用重新判分")
    if request.mode == "regrade":
        config.disable_verification = False
        config.use_platform_image = False
        if request.dataset_id is not None:
            config.dataset_id = request.dataset_id
            config.tasks_path = ""
            config.registry_dataset = ""
        if request.judge_model_api_id is not None:
            config.judge_model_api_id = request.judge_model_api_id
            # An explicitly selected judge must win over old manual overrides.
            config.verifier_env = {k: v for k, v in config.verifier_env.items() if not k.startswith("JUDGE_")}
        if request.judge_api_protocol is not None:
            config.judge_api_protocol = request.judge_api_protocol
    try:
        _check(source_dir(source), verifier_path(config) if request.mode == "regrade" else None)
    except BuildError as exc:
        raise HTTPException(400, str(exc)) from exc
    action = HarborAction(mode=request.mode, source_job_id=source.id, filter_error_types=filters)
    label = "中断恢复" if action.mode == "resume" else "重新判分"
    payload = JobCreate(
        name=f"{source.name[:108]} ({label})", note=source.note, type=JobType.VERIFY,
        project_id=source.project_id, tags=source.tags, parent_job_id=source.id,
        resources=source.resources.model_copy(update={"gpu_ids": []}), verify=config,
    )
    return job_service.create_job(payload, owner=actor, harbor_action=action)


def snapshot_dir(job: Job, work_dir: Path, output_dir: Path) -> Path:
    return output_dir / job.id if job.harbor_action.mode == "resume" else work_dir / "regrade-source"


def prepare_snapshot(job: Job, work_dir: Path, output_dir: Path) -> None:
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
        shutil.copytree(original, staging)
        if action.mode == "resume":
            config_path = staging / "config.json"
            config = json.loads(config_path.read_text())
            config.update(job_name=job.id, jobs_dir=str(output_dir))
            config_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
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
