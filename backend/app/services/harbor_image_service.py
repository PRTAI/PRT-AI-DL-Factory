"""Manage the shared evaluation image and isolated, lightweight task contexts."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any

from fastapi import HTTPException
from filelock import FileLock

from ..config import settings

IMAGE = "prt-ai-factory-harbor-eval-base-image:latest"
_LOCK = threading.Lock()
_BUILD: dict[str, Any] = {"status": "idle"}


def recipe() -> str:
    return (settings.resources_dir / "harbor-eval.Dockerfile").read_text(encoding="utf-8")


def status() -> dict[str, Any]:
    with _LOCK:
        result = dict(_BUILD)
    result.update(image=IMAGE, ready=False, image_id="", error="")
    try:
        proc = subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True, text=True, timeout=10)
        if proc.returncode == 0:
            image = json.loads(proc.stdout)[0]
            result.update(ready=True, image_id=image["Id"], created=image.get("Created", ""))
        else:
            result["error"] = proc.stderr.strip()[-2000:]
    except (OSError, subprocess.TimeoutExpired, ValueError, KeyError) as exc:
        result["error"] = str(exc)
    log = settings.prebuilds_dir / "harbor-platform-image.log"
    if log.exists():
        with log.open("rb") as handle:
            handle.seek(max(0, log.stat().st_size - 32768))
            result["log"] = handle.read().decode("utf-8", errors="replace")
    else:
        result["log"] = ""
    return result


def start() -> dict[str, Any]:
    dockerfile = recipe().encode()
    with _LOCK:
        if _BUILD["status"] == "running":
            raise HTTPException(409, "平台镜像正在构建")
        settings.prebuilds_dir.mkdir(parents=True, exist_ok=True)
        log = (settings.prebuilds_dir / "harbor-platform-image.log").open("wb")
        try:
            # Dockerfile on stdin gives Docker an empty context: no repository data or secrets.
            proc = subprocess.Popen(["docker", "build", "-t", IMAGE, "-"], stdin=subprocess.PIPE, stdout=log, stderr=subprocess.STDOUT)
        except OSError as exc:
            log.close()
            raise HTTPException(400, f"无法启动 Docker: {exc}") from exc
        _BUILD.update(status="running", return_code=None)

    def supervise() -> None:
        try:
            proc.communicate(dockerfile)
            code = proc.returncode
        except Exception:
            proc.kill()
            proc.wait()
            code = -1
        finally:
            log.close()
        with _LOCK:
            _BUILD.update(status="succeeded" if code == 0 else "failed", return_code=code)

    threading.Thread(target=supervise, daemon=True, name="harbor-platform-image").start()
    return {"status": "running", "image": IMAGE}


def pin_image(image_id: str) -> str:
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", image_id):
        raise ValueError("Docker 返回了无效的镜像 ID")
    tag = IMAGE.removesuffix(":latest") + ":sha-" + image_id.split(":", 1)[1]
    try:
        result = subprocess.run(["docker", "tag", image_id, tag], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError(f"固定评测镜像版本失败: {exc}") from exc
    if result.returncode:
        raise ValueError(f"固定评测镜像版本失败: {result.stderr[-2000:]}")
    return tag


def lightweight_dockerfile(source: str) -> str:
    """Replace the dependency prefix of the user's shared task template.

    Only the documented template (first local COPY is requirements/input_files)
    is eligible. Preserve all subsequent task-specific commands verbatim.
    """
    lines = source.splitlines(keepends=True)
    instructions: list[tuple[int, str]] = []
    pending = ""
    start_line = 0
    for index, line in enumerate(lines):
        if not pending and (not line.strip() or line.lstrip().startswith("#")):
            continue
        if not pending:
            start_line = index
        pending += line.strip().removesuffix("\\") + " "
        if line.rstrip().endswith("\\"):
            continue
        instructions.append((start_line, pending.strip()))
        pending = ""
    if sum(text.upper().startswith("FROM ") for _, text in instructions) != 1:
        raise ValueError("平台镜像模式仅支持单阶段 Dockerfile")
    for index, text in instructions:
        command = text.split()[0].upper()
        if command in {"COPY", "ADD"}:
            if not re.fullmatch(r"COPY\s+(requirements\.txt\s+/tmp/requirements\.txt|input_files/?\s+/app(?:/input_files)?/?)", text, re.I):
                raise ValueError("平台镜像仅支持首个 COPY 为 requirements.txt 或 input_files 的标准任务模板")
            return "FROM {image}\n" + "".join(lines[index:])
        if command not in {"FROM", "RUN", "WORKDIR"}:
            raise ValueError(f"公共依赖段含 {command}，请使用原任务镜像模式")
    raise ValueError("未找到标准任务的 COPY requirements.txt / input_files 指令")


def prepare_tasks(root: Path, destination: Path, image_id: str) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(destination.parent / ".platform-image.lock")):
        return _prepare_tasks(root, destination, image_id)


def _prepare_tasks(root: Path, destination: Path, image_id: str) -> Path:
    root = root.resolve()
    if destination.resolve().is_relative_to(root):
        raise ValueError("任务运行目录不能位于源任务目录内")
    if destination.exists():
        return destination
    tasks = sorted(root.rglob("task.toml"))
    if not tasks:
        raise ValueError("任务目录中没有 task.toml")
    transformed: dict[Path, str] = {}
    for task in tasks:
        env = task.parent / "environment"
        if (env / "docker-compose.yaml").exists():
            raise ValueError(f"{task.parent.name}: Compose 任务不支持平台镜像")
        dockerfile = env / "Dockerfile"
        if not dockerfile.is_file():
            raise ValueError(f"{task.parent.name}: 缺少 Dockerfile")
        transformed[dockerfile.relative_to(root)] = lightweight_dockerfile(dockerfile.read_text(encoding="utf-8")).replace("{image}", image_id, 1)
    if any(path.is_symlink() for path in root.rglob("*")):
        raise ValueError("平台镜像模式暂不支持含符号链接的任务目录")
    import toml
    try:
        shutil.copytree(root, destination)
        for relative, content in transformed.items():
            (destination / relative).write_text(content, encoding="utf-8")
        for task in tasks:
            copied = destination / task.relative_to(root)
            config = toml.loads(copied.read_text(encoding="utf-8"))
            config.setdefault("environment", {}).pop("docker_image", None)
            copied.write_text(toml.dumps(config), encoding="utf-8")
    except Exception:
        shutil.rmtree(destination, ignore_errors=True)
        raise
    return destination
