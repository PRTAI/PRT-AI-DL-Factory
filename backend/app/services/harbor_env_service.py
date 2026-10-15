"""Inspect task timeouts and verifier variables without exposing credentials."""

from __future__ import annotations

import json
import math
import re
import subprocess
import tomllib
from functools import lru_cache
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
            raise HTTPException(400, "只能检测 Harbor 任务集的环境变量与超时配置")
        root = Path(dataset.root_path)
    else:
        root = Path(tasks_path).expanduser()
    if not root.is_dir():
        raise HTTPException(400, "任务目录不存在")
    defaults = timeout_defaults()
    result = inspect_root(root, defaults=defaults)
    result["timeout_defaults"] = defaults
    return result


def timeout_defaults() -> dict[str, Any]:
    from ..builders.harbor import resolve_harbor_bin, resolve_harbor_python
    try:
        python = resolve_harbor_python(resolve_harbor_bin())
    except ValueError:
        return {"available": False}
    return _timeout_defaults(python)


@lru_cache(maxsize=4)
def _timeout_defaults(python: str) -> dict[str, Any]:
    code = (
        "import json; from importlib.metadata import version; "
        "import harbor.models.task.config as config; c=config.TaskConfig(); "
        "s=config.StepConfig(name='timeout-probe') if hasattr(config,'StepConfig') else None; "
        "print(json.dumps(dict(available=True,version=version('harbor'),"
        "agent=c.agent.timeout_sec,verifier=c.verifier.timeout_sec,"
        "steps=dict(agent=s.agent.timeout_sec,verifier=s.verifier.timeout_sec) if s else None)))"
    )
    try:
        result = subprocess.run([python, "-c", code], capture_output=True, text=True, timeout=10, check=True)
        value = json.loads(result.stdout)
        if not isinstance(value, dict) or not all(key in value for key in ("agent", "verifier", "version")):
            raise ValueError("invalid defaults")
        return value
    except (OSError, subprocess.SubprocessError, ValueError):
        return {"available": False}


def _task_timeouts(
    data: dict[str, Any], task: str, warnings: list[str], defaults: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    def phase_value(phase: str, step: dict[str, Any] | None) -> dict[str, Any]:
        section = data.get(phase, {})
        step_section = (step or {}).get(phase, {})
        if not isinstance(section, dict) or not isinstance(step_section, dict):
            warnings.append(f"{task}: [{phase}] 超时配置格式错误")
            return {"seconds": None, "source": "invalid"}
        if "timeout_sec" in step_section:
            value, source = step_section["timeout_sec"], "step"
        else:
            if step is not None:
                step_defaults = (defaults or {}).get("steps")
                if not isinstance(step_defaults, dict) or phase not in step_defaults:
                    return {"seconds": None, "source": "unavailable"}
                # Harbor falls back to the task only when the step's model value is None.
                if step_defaults[phase] is not None:
                    return {"seconds": step_defaults[phase], "source": "step_default"}
            if "timeout_sec" in section:
                value, source = section["timeout_sec"], "task"
            else:
                return {"seconds": None, "source": "harbor_default"}
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            warnings.append(f"{task}: {phase}.timeout_sec 必须为正数")
            return {"seconds": None, "source": "invalid"}
        return {"seconds": value, "source": source}

    steps = data.get("steps") or [None]
    if not isinstance(steps, list) or any(step is not None and not isinstance(step, dict) for step in steps):
        warnings.append(f"{task}: steps 格式错误")
        steps = [None]
    return [{"task": task, "step": str(step.get("name", index + 1)) if step else "",
             "agent": phase_value("agent", step), "verifier": phase_value("verifier", step)}
            for index, step in enumerate(steps)]


def _env_table(section: Any, label: str, task: str, warnings: list[str]) -> dict[str, Any]:
    if section is None:
        return {}
    if not isinstance(section, dict):
        warnings.append(f"{task}: [{label}] 必须是表")
        return {}
    env = section.get("env") or {}
    if not isinstance(env, dict):
        warnings.append(f"{task}: [{label}.env] 必须是键值表")
        return {}
    return env


def _verifier_tables(verifier: Any, label: str, task: str, warnings: list[str]) -> dict[str, Any]:
    """[verifier.env] 进 verifier 执行环境，[verifier.environment.env] 进独立 verifier 容器；
    两者的 ${VAR} 都从宿主机解析，表单上统一按 --verifier-env 填。"""
    if verifier is None:
        return {}
    if not isinstance(verifier, dict):
        warnings.append(f"{task}: [{label}] 必须是表")
        return {}
    return {**_env_table(verifier.get("environment"), f"{label}.environment", task, warnings),
            **_env_table(verifier, label, task, warnings)}


class _Collector:
    def __init__(self) -> None:
        self.fields: dict[str, dict[str, Any]] = {}
        self.defaults: dict[str, set[str | None]] = {}
        self.references: dict[str, set[str | None]] = {}

    def add(self, scope: str, env: dict[str, Any], task: str, warnings: list[str]) -> None:
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
            field = self.fields.setdefault(key, {
                "key": key, "required": False, "secret": False, "tasks": [], "has_task_value": False,
            })
            field["required"] |= template is not None and default is None
            field["secret"] |= secret
            field["has_task_value"] |= bool(default)
            if scope not in field["tasks"]:
                field["tasks"].append(scope)
            self.defaults.setdefault(key, set()).add(default)
            self.references.setdefault(key, set()).add(reference)

    def result(self) -> list[dict[str, Any]]:
        for key, field in self.fields.items():
            choices = self.defaults[key]
            default = next(iter(choices)) if len(choices) == 1 else None
            # 返回空默认值，但绝不把 task.toml 中的真实密钥或密钥默认值送到浏览器。
            field["default"] = None if field["secret"] and default else default
            field["conflicting_defaults"] = len(choices) > 1
            field["source_variable"] = next(iter(self.references[key])) if len(self.references[key]) == 1 else None
            field["source_variables"] = sorted(reference for reference in self.references[key] if reference)
        return [self.fields[key] for key in sorted(self.fields)]


def inspect_root(root: Path, *, defaults: dict[str, Any] | None = None) -> dict[str, Any]:
    """``variables``：verifier 用的变量；``environment_variables``：任务容器
    （[environment.env]，oracle 时还有 [solution.env]）从宿主机读取的变量。"""
    verifier_vars, environment_vars = _Collector(), _Collector()
    warnings: list[str] = []
    n_tasks = 0
    task_timeouts: list[dict[str, Any]] = []
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
        task_timeouts.extend(_task_timeouts(data, task, warnings, defaults))
        verifier_env = _verifier_tables(data.get("verifier"), "verifier", task, warnings)
        steps = data.get("steps")
        if isinstance(steps, list) and steps:
            for step in steps:
                if not isinstance(step, dict):
                    continue
                step_env = _verifier_tables(step.get("verifier"), "steps.verifier", task, warnings)
                verifier_vars.add(f"{task}/steps/{step.get('name', '?')}", {**verifier_env, **step_env}, task, warnings)
        else:
            verifier_vars.add(task, verifier_env, task, warnings)
        environment_vars.add(task, _env_table(data.get("environment"), "environment", task, warnings), task, warnings)
        environment_vars.add(f"{task}/solution", _env_table(data.get("solution"), "solution", task, warnings), task, warnings)
    return {
        "variables": verifier_vars.result(),
        "environment_variables": environment_vars.result(),
        "n_tasks": n_tasks,
        "warnings": warnings,
        "task_timeouts": task_timeouts,
    }
