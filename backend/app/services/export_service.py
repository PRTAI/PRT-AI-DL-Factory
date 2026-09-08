"""Checkpoint 合并导出：把 LoRA adapter 合并回底座，得到完整可部署的权重。

用的是 LLaMA-Factory 自己的 ``llamafactory.cli export``——它会正确处理 tokenizer、
生成配置和分片，比自己写 `peft.merge_and_unload` 稳妥。参数名以本机检出的
``ExportArguments`` dataclass 为准（见 test_config_keys）。

导出是几分钟量级的纯 CPU 活（``export_device: cpu``），所以不占 GPU、不进调度队列，
直接起进程 + 后台线程收尾。
"""

from __future__ import annotations

import os
import shlex
import signal
import subprocess
import threading
from pathlib import Path

import yaml
from fastapi import HTTPException, status

from ..config import settings
from ..models import (
    ExportCreate,
    ExportStatus,
    ExportTask,
    Job,
    JobStatus,
    JobType,
    User,
    utcnow,
)
from ..store import store
from . import audit_service, proc_env

_PROCESSES: dict[str, subprocess.Popen] = {}
_LOCK = threading.RLock()


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #
def list_exports(*, job_id: str = "") -> list[ExportTask]:
    items = store.exports.find(lambda e: not job_id or e.job_id == job_id)
    items.sort(key=lambda e: e.created_at, reverse=True)
    return items


def get_export(export_id: str) -> ExportTask:
    item = store.exports.get(export_id)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="导出任务不存在")
    return item


def export_log(export_id: str, *, tail_bytes: int | None = None) -> dict:
    item = get_export(export_id)
    path = Path(item.log_file or "")
    if not path.is_file():
        return {"content": "", "size": 0, "eof": item.status != ExportStatus.RUNNING}
    size = path.stat().st_size
    limit = tail_bytes or settings.log_tail_bytes
    with path.open("rb") as handle:
        if size > limit:
            handle.seek(size - limit)
            handle.readline()
        data = handle.read()
    return {
        "content": data.decode("utf-8", errors="replace"),
        "size": size,
        "eof": item.status != ExportStatus.RUNNING,
    }


def exportable_checkpoints(job_id: str) -> dict:
    """训练任务能导出哪些 checkpoint —— 前端下拉框的数据源。"""
    job = _job_or_404(job_id)
    reason = _why_not_exportable(job)
    output_dir = Path(job.runtime.output_dir or "")
    checkpoints: list[dict] = []
    if output_dir.is_dir():
        if (output_dir / "adapter_config.json").is_file():
            checkpoints.append({"path": str(output_dir), "label": "最终 adapter", "step": None})
        for path in sorted(
            output_dir.glob("checkpoint-*"), key=lambda p: _step_of(p), reverse=True
        ):
            if (path / "adapter_config.json").is_file():
                checkpoints.append(
                    {"path": str(path), "label": path.name, "step": _step_of(path)}
                )
    return {
        "job_id": job_id,
        "exportable": reason == "",
        "reason": reason,
        "base_model_path": (job.sft.model_path if job.sft else ""),
        "template": (job.sft.template if job.sft else "default"),
        "checkpoints": checkpoints,
    }


def _step_of(path: Path) -> int:
    try:
        return int(path.name.rsplit("-", 1)[1])
    except (IndexError, ValueError):
        return 0


def _why_not_exportable(job: Job) -> str:
    if job.type != JobType.SFT or job.sft is None:
        return "只有 LLaMA-Factory 训练任务支持合并导出"
    if job.sft.finetuning_type == "full":
        return "全参微调的产物本身就是完整权重，直接部署即可，无需合并"
    if job.status not in {JobStatus.SUCCEEDED, JobStatus.CANCELLED, JobStatus.FAILED}:
        return "任务还在运行，请等结束后再导出"
    output_dir = Path(job.runtime.output_dir or "")
    if not output_dir.is_dir():
        return "任务没有产物目录"
    if not (output_dir / "adapter_config.json").is_file() and not any(
        (p / "adapter_config.json").is_file() for p in output_dir.glob("checkpoint-*")
    ):
        return "产物目录里没有找到 adapter，无法合并"
    return ""


# --------------------------------------------------------------------------- #
# Create / run
# --------------------------------------------------------------------------- #
def create_export(payload: ExportCreate, *, owner: User) -> ExportTask:
    job = _job_or_404(payload.job_id)
    reason = _why_not_exportable(job)
    if reason:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=reason)
    assert job.sft is not None

    adapter_path = _resolve_adapter(job, payload.adapter_path)
    task = ExportTask(
        name=payload.name.strip() or f"{job.name}-merged",
        job_id=job.id,
        base_model_path=job.sft.model_path,
        adapter_path=adapter_path,
        template=job.sft.template or "default",
        export_size_gb=payload.export_size_gb,
        export_device=payload.export_device,
        export_legacy_format=payload.export_legacy_format,
        owner_id=owner.id,
        owner_email=owner.email,
    )
    task.export_dir = str(settings.exports_dir / task.id / "model")
    store.exports.put(task)

    try:
        task = _launch(task)
    except Exception as exc:
        _mark(task.id, ExportStatus.FAILED, error=f"{type(exc).__name__}: {exc}")
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=f"导出启动失败：{exc}") from exc

    audit_service.record(
        "export.create",
        actor=owner,
        target_type="export",
        target_id=task.id,
        summary=f"合并导出「{job.name}」的 checkpoint",
        detail={"adapter": adapter_path, "export_dir": task.export_dir},
    )
    return task


def _resolve_adapter(job: Job, requested: str) -> str:
    output_dir = Path(job.runtime.output_dir or "").resolve()
    if not requested:
        if (output_dir / "adapter_config.json").is_file():
            return str(output_dir)
        candidates = sorted(
            (p for p in output_dir.glob("checkpoint-*") if (p / "adapter_config.json").is_file()),
            key=_step_of,
        )
        if not candidates:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="没有可用的 adapter")
        return str(candidates[-1])

    target = Path(requested).resolve()
    try:
        target.relative_to(output_dir)
    except ValueError as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail="只能导出该任务自己的产物目录"
        ) from exc
    if not (target / "adapter_config.json").is_file():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=f"{target} 不是一个 adapter 目录")
    return str(target)


def build_config(task: ExportTask) -> dict:
    """``llamafactory-cli export`` 的 YAML。键名来自 ExportArguments dataclass。"""
    return {
        "model_name_or_path": task.base_model_path,
        "adapter_name_or_path": task.adapter_path,
        "template": task.template,
        "trust_remote_code": True,
        "finetuning_type": "lora",
        "export_dir": task.export_dir,
        "export_size": task.export_size_gb,
        "export_device": task.export_device,
        "export_legacy_format": task.export_legacy_format,
    }


def _launch(task: ExportTask) -> ExportTask:
    root = settings.llamafactory_root
    if not (root / "src" / "llamafactory").is_dir():
        raise RuntimeError(f"LLaMA-Factory 源码目录无效: {root}")

    work_dir = settings.exports_dir / task.id
    work_dir.mkdir(parents=True, exist_ok=True)
    (work_dir / "export_config.yaml").write_text(
        yaml.safe_dump(build_config(task), allow_unicode=True, sort_keys=False), encoding="utf-8"
    )

    command = [
        settings.python_for("llamafactory"),
        "-m",
        "llamafactory.cli",
        "export",
        "export_config.yaml",
    ]
    env = os.environ.copy()
    env.update(
        {
            "PYTHONPATH": str(root / "src"),
            "PYTHONUNBUFFERED": "1",
            "DISABLE_VERSION_CHECK": "1",
            "TOKENIZERS_PARALLELISM": "false",
        }
    )
    proc_env.with_interpreter_on_path(env, command[0])
    # export_device=cpu 时不该占用显卡，否则会和训练任务抢显存。
    if task.export_device == "cpu":
        env["CUDA_VISIBLE_DEVICES"] = ""

    log_path = work_dir / "stdout.log"
    log_handle = log_path.open("ab", buffering=0)
    log_handle.write(
        (
            f"=== PRT AI DL Factory — 合并导出 ===\n"
            f"export : {task.id} / {task.name}\n"
            f"base   : {task.base_model_path}\n"
            f"adapter: {task.adapter_path}\n"
            f"target : {task.export_dir}\n"
            f"{'=' * 70}\n\n"
        ).encode("utf-8")
    )

    process = subprocess.Popen(  # noqa: S603 - argv 固定，不过 shell
        command,
        cwd=str(work_dir),
        env=env,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    with _LOCK:
        _PROCESSES[task.id] = process

    def mutate(item: ExportTask) -> None:
        item.status = ExportStatus.RUNNING
        item.started_at = utcnow()
        item.pid = process.pid
        item.pgid = os.getpgid(process.pid)
        item.log_file = str(log_path)
        item.command = " ".join(shlex.quote(part) for part in command)

    updated = store.exports.update(task.id, mutate) or task
    threading.Thread(target=_supervise, args=(task.id, process, log_handle), daemon=True).start()
    return updated


def _supervise(export_id: str, process: subprocess.Popen, log_handle) -> None:
    exit_code = process.wait()
    try:
        log_handle.write(f"\n\n=== 导出结束，退出码 {exit_code} ===\n".encode("utf-8"))
        log_handle.close()
    except (OSError, ValueError):
        pass
    with _LOCK:
        _PROCESSES.pop(export_id, None)

    item = store.exports.get(export_id)
    if item is None:
        return
    ok = exit_code == 0 and Path(item.export_dir).is_dir()

    def mutate(entry: ExportTask) -> None:
        entry.exit_code = exit_code
        entry.finished_at = utcnow()
        if ok:
            entry.status = ExportStatus.SUCCEEDED
            entry.output_bytes = _dir_size(Path(entry.export_dir))
        else:
            entry.status = ExportStatus.FAILED
            entry.error = _tail(Path(entry.log_file))

    updated = store.exports.update(export_id, mutate)
    if updated is None:
        return
    audit_service.notify(
        updated.owner_id,
        f"合并导出 {updated.name} {'完成' if ok else '失败'}",
        body=updated.export_dir if ok else updated.error[:400],
        level="success" if ok else "error",
        link="/deployments",
    )


def _dir_size(path: Path) -> int:
    total = 0
    for child in path.rglob("*"):
        try:
            if child.is_file():
                total += child.stat().st_size
        except OSError:
            continue
    return total


def _tail(path: Path, lines: int = 40) -> str:
    if not path.is_file():
        return ""
    size = path.stat().st_size
    with path.open("rb") as handle:
        if size > 64_000:
            handle.seek(size - 64_000)
        text = handle.read().decode("utf-8", errors="replace")
    return "\n".join([line for line in text.splitlines() if line.strip()][-lines:])


def _mark(export_id: str, new_status: ExportStatus, *, error: str = "") -> None:
    def mutate(entry: ExportTask) -> None:
        entry.status = new_status
        entry.error = error
        entry.finished_at = utcnow()

    store.exports.update(export_id, mutate)


# --------------------------------------------------------------------------- #
# Control
# --------------------------------------------------------------------------- #
def cancel_export(export_id: str, *, actor: User) -> ExportTask:
    item = get_export(export_id)
    _assert_can_control(item, actor)
    if item.status != ExportStatus.RUNNING:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="该导出不在运行中")
    try:
        if item.pgid:
            os.killpg(item.pgid, signal.SIGTERM)
        elif item.pid:
            os.kill(item.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    _mark(export_id, ExportStatus.FAILED, error="已取消")
    return get_export(export_id)


def delete_export(export_id: str, *, actor: User, purge_files: bool = False) -> None:
    item = get_export(export_id)
    _assert_can_control(item, actor)
    if item.status == ExportStatus.RUNNING:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="请先取消再删除")
    if purge_files:
        import shutil

        shutil.rmtree(settings.exports_dir / item.id, ignore_errors=True)
    store.exports.delete(export_id)
    audit_service.record(
        "export.delete",
        actor=actor,
        target_type="export",
        target_id=export_id,
        summary=f"删除导出「{item.name}」",
        detail={"purge_files": purge_files},
    )


def reconcile() -> None:
    """平台重启后把没人看管的导出任务标成失败。"""
    import psutil

    for item in store.exports.find(lambda e: e.status == ExportStatus.RUNNING):
        alive = False
        if item.pid:
            try:
                proc = psutil.Process(item.pid)
                alive = proc.status() != psutil.STATUS_ZOMBIE and not (
                    item.started_at and proc.create_time() < item.started_at.timestamp() - 5
                )
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                alive = False
        if not alive:
            _mark(item.id, ExportStatus.FAILED, error="进程已不存在（可能在平台重启期间退出）")


def _job_or_404(job_id: str) -> Job:
    job = store.jobs.get(job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="任务不存在")
    return job


def _assert_can_control(item: ExportTask, actor: User) -> None:
    if actor.is_admin or item.owner_id == actor.id:
        return
    raise HTTPException(status.HTTP_403_FORBIDDEN, detail="只有导出的发起人或管理员可以操作")
