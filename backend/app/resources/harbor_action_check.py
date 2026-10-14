"""Read-only recovery checks, run with the configured Harbor interpreter."""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path


def selected_directories(source: Path, trial_names: list[str] | None) -> list[Path]:
    if trial_names is None:
        return sorted(source.iterdir())
    paths = []
    for name in trial_names:
        if name.startswith("@planned:"):
            continue
        if not name or name in {".", ".."} or "/" in name or "\\" in name:
            raise ValueError("无效的 trial 标识")
        path = source / name
        if not path.is_dir() or path.is_symlink():
            raise ValueError(f"选中的记录不存在：{name}")
        paths.append(path)
    return paths


def selected_job_configs(source: Path, trial_names: list[str]) -> list[dict]:
    """Each selected attempt becomes one native job, without tasks x attempts expansion."""
    from harbor.models.job.config import JobConfig
    from harbor.models.trial.config import TrialConfig

    original = JobConfig.model_validate_json((source / "config.json").read_text())
    root = original.model_dump(mode="json")
    rows = []
    for name in trial_names:
        if name.startswith("@planned:"):
            try:
                index = int(name.split(":", 1)[1])
                if index < 0:
                    raise ValueError()
                block = json.loads((source / "lock.json").read_text())["trials"][index]
                task = block["task"]
                task_config = {key: task[key] for key in ("path", "git_url", "git_commit_id") if task.get(key)}
                if task.get("type") == "package":
                    task_config = {"name": task["name"], "ref": task.get("version")}
                block = {**root, **block, "task": task_config,
                         "extra_instructions": root.get("extra_instructions", [])}
                trial = TrialConfig.model_validate(block)
            except (ValueError, KeyError, IndexError, OSError, TypeError):
                raise ValueError(f"无法恢复待开始记录的配置：{name}") from None
        else:
            directory = selected_directories(source, [name])[0]
            try:
                if (directory / "config.json").is_file():
                    trial = TrialConfig.model_validate_json((directory / "config.json").read_text())
                else:
                    trial = TrialConfig.model_validate(json.loads((directory / "result.json").read_text())["config"])
            except Exception:
                raise ValueError(f"{name} 缺少有效的 trial 配置，不能自动重跑") from None
        data = dict(root)
        # Preserve per-attempt overrides, including timeouts, environment and instructions.
        for key, value in trial.model_dump(mode="json").items():
            if key in JobConfig.model_fields and key not in {"job_id"}:
                data[key] = value
        data.update(tasks=[trial.task.model_dump(mode="json")], datasets=[],
                    agents=[trial.agent.model_dump(mode="json")], n_attempts=1, source_jobs=[])
        native = JobConfig.model_validate(data)
        rows.append({"trial_name": name, "job_config": native.model_dump(mode="json")})
    return rows


def inspect_source(source: Path, trial_names: list[str] | None = None) -> dict:
    from harbor.cli import jobs
    from harbor.models.job.config import JobConfig
    from harbor.models.trial.result import TrialResult

    if not callable(getattr(jobs, "resume", None)):
        raise ValueError("当前 Harbor 未提供 job resume，请升级 Harbor")
    try:
        JobConfig.model_validate_json((source / "config.json").read_text())
    except Exception:
        raise ValueError("原任务 config.json 缺失或与当前 Harbor 版本不兼容") from None
    recorded = 0
    unfinished = 0
    errors: dict[str, int] = {}
    regrade_error = ""
    if not callable(getattr(jobs, "regrade", None)):
        regrade_error = "当前 Harbor 未提供 job regrade，请升级 Harbor"
    for trial in selected_directories(source, trial_names):
        if not trial.is_dir() or trial.name.startswith("."):
            continue
        try:
            result = TrialResult.model_validate_json((trial / "result.json").read_text())
        except Exception:
            unfinished += 1
            continue
        recorded += 1
        if result.exception_info:
            kind = result.exception_info.exception_type
            errors[kind] = errors.get(kind, 0) + 1
        if not regrade_error:
            from harbor.trial.regrade import read_artifact_manifest
            try:
                if result.step_results:
                    for step in result.step_results:
                        read_artifact_manifest(trial, step_name=step.step_name)
                else:
                    read_artifact_manifest(trial)
            except Exception:
                regrade_error = f"{trial.name} 缺少有效的 artifacts/manifest.json，只有产物文件不能直接重新判分"
    if not recorded and not regrade_error:
        regrade_error = "未找到有效的 trial result.json，只有产物文件不能直接重新判分"
    return {"recorded_trials": recorded, "unfinished_trials": unfinished,
            "error_types": errors, "regrade_error": regrade_error}


def check_regrade(source: Path, task_path: Path, trial_names: list[str] | None = None) -> None:
    from harbor.models.trial.config import SourceTrialConfig, TaskConfig, TrialConfig
    from harbor.models.trial.result import TrialResult
    from harbor.trial.regrade import RegradeError, check_task_regradable, expand_task_path, local_task_name
    from harbor.trial.trial import Trial

    tasks = {}
    for path in expand_task_path(task_path):
        name = local_task_name(path)
        if name in tasks:
            raise ValueError(f"评分任务集存在同名任务：{name}")
        tasks[name] = path
    for trial in selected_directories(source, trial_names):
        if not trial.is_dir():
            continue
        try:
            result = TrialResult.model_validate_json((trial / "result.json").read_text())
        except Exception:
            continue
        target = tasks.get(result.task_name)
        if target is None:
            raise ValueError(f"评分任务集未覆盖原任务：{result.task_name}")
        try:
            reason = check_task_regradable(target)
        except Exception:
            raise ValueError(f"评分任务 {result.task_name} 的 task.toml 无效") from None
        if reason:
            raise ValueError(f"任务 {result.task_name} 的评分器不能独立运行。需在 task.toml 将 verifier.environment_mode 配为 separate，并让评分脚本从 /logs/artifacts 读取产物；多步骤任务的每一步均需满足。")
        # Use Harbor's own replay validator for input coverage and multi-step
        # ordering. Creation only prepares local metadata; never call run/setup.
        # All generated files live in an automatically removed temporary dir.
        with tempfile.TemporaryDirectory(prefix="prt-regrade-check-") as scratch:
            probe = None
            try:
                config = TrialConfig(
                    task=TaskConfig(path=target), trials_dir=Path(scratch),
                    agent=result.config.agent, artifacts=result.config.artifacts,
                    source_trial=SourceTrialConfig(action="regrade", type="local", path=trial),
                )
                probe = asyncio.run(Trial.create(config))
                probe._validate_regradable()
            except RegradeError as exc:
                raise ValueError(f"{trial.name} 的产物记录不能用于评分：{exc}") from None
            except Exception:
                raise ValueError(f"无法验证 {trial.name} 的评分输入，请检查题包、产物和 Harbor 版本") from None
            finally:
                if probe is not None:
                    probe._close_logger_handler()


def main() -> None:
    try:
        request = json.load(sys.stdin)
        source = Path(request["source"])
        names = request.get("trial_names")
        result = inspect_source(source, names)
        if names is not None:
            result["selected"] = selected_job_configs(source, names)
        if request.get("task_path"):
            if result["regrade_error"]:
                raise ValueError(result["regrade_error"])
            check_regrade(source, Path(request["task_path"]), names)
        print(json.dumps({"ok": True, **result}, ensure_ascii=False))
    except ValueError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
    except Exception:
        print(json.dumps({"ok": False, "error": "无法读取 Harbor 记录，请检查框架版本及目录权限"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
