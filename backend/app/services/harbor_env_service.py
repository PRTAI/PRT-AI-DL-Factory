"""Inspect task-defined verifier variables without exposing embedded credentials."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from ..models import DatasetKind
from . import archive, dataset_service

_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_TEMPLATE = re.compile(r"\$\{([^}:]+)(?::-(.*))?\}")
_SECRET = re.compile(r"KEY|SECRET|TOKEN(?!S)|PASSWORD|CREDENTIAL|AUTH", re.IGNORECASE)


def inspect_source(*, dataset_id: str = "", tasks_path: str = "") -> dict[str, Any]:
    if bool(dataset_id) == bool(tasks_path):
        raise HTTPException(400, "请指定一个任务集或服务器任务目录")
    if dataset_id:
        dataset = dataset_service.get_dataset(dataset_id)
        if dataset.kind != DatasetKind.HARBOR_TASKS:
            raise HTTPException(400, "只能检测 Harbor 任务集的 verifier 环境变量")
        root = Path(dataset.root_path)
    else:
        root = Path(tasks_path).expanduser()
    if not root.is_dir():
        raise HTTPException(400, "任务目录不存在")
    return inspect_root(root)


def inspect_root(root: Path) -> dict[str, Any]:
    fields: dict[str, dict[str, Any]] = {}
    defaults: dict[str, set[str | None]] = {}
    references: dict[str, set[str | None]] = {}
    warnings: list[str] = []
    n_tasks = 0
    for path in sorted(root.rglob("task.toml")):
        if path.is_symlink() or not archive.is_within(root, path):
            continue
        task = path.parent.relative_to(root).as_posix()
        try:
            if path.stat().st_size > 1_000_000:
                raise ValueError("task.toml 超过 1 MB")
            data = tomllib.loads(path.read_text(encoding="utf-8-sig"))
            env = (data.get("verifier") or {}).get("env") or {}
            if not isinstance(env, dict):
                raise ValueError("[verifier.env] 必须是键值表")
        except (OSError, UnicodeError, ValueError, AttributeError):
            warnings.append(f"{task}/task.toml 无法解析，请检查配置文件")
            continue
        n_tasks += 1
        for key, value in env.items():
            if not _KEY.fullmatch(key) or not isinstance(value, str):
                warnings.append(f"{task}/task.toml 含不支持的环境变量声明")
                continue
            template = _TEMPLATE.fullmatch(value)
            reference = template[1] if template else None
            if reference and not _KEY.fullmatch(reference):
                warnings.append(f"{task}/task.toml 含不支持的环境变量引用")
                continue
            default = template[2] if template else value
            secret = bool(_SECRET.search(key) or (reference and _SECRET.search(reference)))
            field = fields.setdefault(key, {
                "key": key, "required": False, "secret": False, "tasks": [], "has_task_value": False,
            })
            field["required"] |= template is not None and default is None
            field["secret"] |= secret
            field["has_task_value"] |= bool(default)
            field["tasks"].append(task)
            defaults.setdefault(key, set()).add(default)
            references.setdefault(key, set()).add(reference)
    for key, field in fields.items():
        choices = defaults[key]
        default = next(iter(choices)) if len(choices) == 1 else None
        # 返回空默认值，但绝不把 task.toml 中的真实密钥或密钥默认值送到浏览器。
        field["default"] = None if field["secret"] and default else default
        field["conflicting_defaults"] = len(choices) > 1
        field["source_variable"] = next(iter(references[key])) if len(references[key]) == 1 else None
        field["source_variables"] = sorted(reference for reference in references[key] if reference)
    return {"variables": [fields[key] for key in sorted(fields)], "n_tasks": n_tasks, "warnings": warnings}
