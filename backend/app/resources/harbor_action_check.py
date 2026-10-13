"""Read-only recovery checks, run with the configured Harbor interpreter."""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path


def inspect_source(source: Path) -> dict:
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
    for trial in sorted(source.iterdir()):
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


def check_regrade(source: Path, task_path: Path) -> None:
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
    for trial in sorted(source.iterdir()):
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
        result = inspect_source(source)
        if request.get("task_path"):
            if result["regrade_error"]:
                raise ValueError(result["regrade_error"])
            check_regrade(source, Path(request["task_path"]))
        print(json.dumps({"ok": True, **result}, ensure_ascii=False))
    except ValueError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
    except Exception:
        print(json.dumps({"ok": False, "error": "无法读取 Harbor 记录，请检查框架版本及目录权限"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
