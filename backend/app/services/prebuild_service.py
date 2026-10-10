"""评测前「预构建容器」：挑一个 Harbor 任务，先把它的 docker 环境 build 一遍。

目的有两个：提前暴露 Dockerfile 写错/基础镜像拉不下来这类问题（否则要等评测排队、
拿到资源后才发现），以及预热 docker 的层缓存，让正式评测的构建更快。

和训练/评测任务不同，这里不走审核和调度队列、不占 GPU；记录只放在进程内存里，
日志落 ``data/prebuilds/<id>.log``。平台重启后记录丢失即可，子进程会随服务一起被回收，
所以不需要 ``reconcile()``。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import tomllib
from pathlib import Path
from typing import Any

from fastapi import HTTPException, status

from ..config import settings
from ..models import DatasetKind, User, new_id, utcnow
from . import archive, dataset_service
from .proc_env import with_interpreter_on_path

_LOCK = threading.Lock()
_BUILDS: dict[str, dict[str, Any]] = {}
_MAX_RECORDS = 50
_TAIL_BYTES = 64 * 1024


def _image_tag(task_name: str) -> str:
    slug = re.sub(r"[^a-z0-9_.-]+", "-", task_name.lower()).strip("-.") or "task"
    return f"prt-prebuild/{slug[:100]}:latest"


def _resolve_task_dir(dataset_id: str, task: str) -> Path:
    dataset = dataset_service.get_dataset(dataset_id)
    if dataset.kind != DatasetKind.HARBOR_TASKS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="只有 Harbor 任务树数据集才能预构建容器")
    base = settings.datasets_dir / dataset_id
    task_dir = (base / task).resolve()
    if not archive.is_within(base, task_dir) or not (task_dir / "task.toml").is_file():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=f"数据集中没有任务 {task}")
    return task_dir


def build_command(task_dir: Path, tag: str) -> list[str]:
    """按 Harbor docker 环境的三种形态决定怎么「预构建」。"""
    env_dir = task_dir / "environment"
    docker = shutil.which("docker") or "docker"
    compose = env_dir / "docker-compose.yaml"
    if compose.is_file():
        return [docker, "compose", "-f", str(compose), "build"]
    if (env_dir / "Dockerfile").is_file():
        return [docker, "build", "-t", tag, str(env_dir)]
    try:
        config = tomllib.loads((task_dir / "task.toml").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=f"task.toml 解析失败: {exc}") from exc
    image = (config.get("environment") or {}).get("docker_image")
    if image:
        return [docker, "pull", str(image)]
    raise HTTPException(
        status.HTTP_400_BAD_REQUEST,
        detail="该任务的 environment/ 下既没有 Dockerfile 也没有 docker-compose.yaml，task.toml 也没指定 docker_image",
    )


def start(dataset_id: str, task: str, *, actor: User) -> dict[str, Any]:
    task_dir = _resolve_task_dir(dataset_id, task)
    argv = build_command(task_dir, _image_tag(task_dir.name))
    build_id = new_id("pb")
    log_path = settings.prebuilds_dir / f"{build_id}.log"
    record: dict[str, Any] = {
        "id": build_id,
        "dataset_id": dataset_id,
        "task": task,
        "command": " ".join(argv),
        "status": "running",
        "return_code": None,
        "owner_id": actor.id,
        "created_at": utcnow().isoformat(),
        "finished_at": None,
        "log_path": str(log_path),
    }
    env = with_interpreter_on_path(dict(os.environ), argv[0])
    log = log_path.open("wb")
    try:
        proc = subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT, cwd=str(task_dir), env=env)
    except OSError as exc:
        log.close()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=f"启动 docker 失败: {exc}") from exc

    with _LOCK:
        _BUILDS[build_id] = record
        for stale in sorted(_BUILDS.values(), key=lambda r: r["created_at"])[:-_MAX_RECORDS]:
            _BUILDS.pop(stale["id"], None)

    def supervise() -> None:
        code = proc.wait()
        log.close()
        with _LOCK:
            record["return_code"] = code
            record["status"] = "succeeded" if code == 0 else "failed"
            record["finished_at"] = utcnow().isoformat()

    threading.Thread(target=supervise, name=f"prebuild-{build_id}", daemon=True).start()
    return _public(record)


def _public(record: dict[str, Any], *, with_log: bool = False) -> dict[str, Any]:
    out = {k: v for k, v in record.items() if k != "log_path"}
    if with_log:
        out["log"] = _tail(Path(record["log_path"]))
    return out


def _tail(path: Path) -> str:
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - _TAIL_BYTES))
            return handle.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def get(build_id: str, *, actor: User) -> dict[str, Any]:
    with _LOCK:
        record = _BUILDS.get(build_id)
    if record is None or (record["owner_id"] != actor.id and not actor.is_admin):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="预构建记录不存在（平台重启后会清空）")
    return _public(record, with_log=True)
