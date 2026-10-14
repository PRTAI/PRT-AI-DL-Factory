"""Apply per-run Judge settings to an independent Harbor task snapshot."""

from __future__ import annotations

import os
import shutil
import tomllib
from pathlib import Path

import toml
from filelock import FileLock

from .harbor_task_layout import task_units


def batch_judge_patches(root: Path) -> dict[Path, str]:
    patches: dict[Path, str] = {}
    tasks = sorted(root.rglob("task.toml"))
    if not tasks:
        raise ValueError("Batch Judge 需要包含 task.toml 的本地任务集")
    for task in tasks:
        for unit in dict.fromkeys([task.parent, *task_units(task)]):
            for path in sorted((unit / "tests").rglob("*.toml")):
                relative = path.relative_to(root)
                if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
                    raise ValueError(f"Judge 配置不能是符号链接: {relative}")
                try:
                    config = tomllib.loads(path.read_text(encoding="utf-8-sig"))
                except (OSError, ValueError) as exc:
                    raise ValueError(f"无法解析评分配置: {relative}") from exc
                judge = config.get("judge")
                if not isinstance(judge, dict):
                    continue
                if any(isinstance(item, dict) and item.get("files") for item in config.get("criterion", [])):
                    raise ValueError(f"{relative}: Batch Judge 不支持逐评分项 files，请先合并到 [judge].files")
                judge["mode"] = "batched"
                patches[relative] = toml.dumps(config)
    return patches


def prepare_batched_tasks(root: Path, destination: Path) -> tuple[Path, int]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(destination.parent / ".batch-judge.lock")):
        if destination.is_symlink():
            raise ValueError("Batch Judge 运行目录不能是符号链接")
        if destination.exists():
            return destination, len(batch_judge_patches(destination))
        if destination.resolve().is_relative_to(root.resolve()):
            raise ValueError("Batch Judge 运行目录不能位于源任务集内")
        for directory, dirs, files in os.walk(root, followlinks=False):
            for name in dirs + files:
                path = Path(directory) / name
                if path.is_symlink() or not (path.is_dir() or path.is_file()):
                    raise ValueError("Batch Judge 暂不支持含符号链接或特殊文件的任务集")
        patches = batch_judge_patches(root)
        staging = destination.with_name(destination.name + ".copying")
        try:
            if staging.exists():
                shutil.rmtree(staging)
            shutil.copytree(root, staging)
            for relative, content in patches.items():
                (staging / relative).write_text(content, encoding="utf-8")
            staging.rename(destination)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
        return destination, len(patches)
