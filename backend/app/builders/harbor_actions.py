"""Recovery commands use Harbor's dedicated CLI, not an oracle/nop run."""

import json
from pathlib import Path

from ..config import settings
from ..models import Job
from ..services import harbor_compat, model_api_service
from ..services.harbor_action_service import selected_config, snapshot_dir, verifier_path
from .base import BuildError, BuildResult, kv_flags
from .harbor import collect_secrets, resolve_agent_model, stage_environment, resolve_harbor_bin, resolve_harbor_python, validate_secret_env


def build_action(job: Job, work_dir: Path, output_dir: Path) -> BuildResult:
    config = job.verify
    action = job.harbor_action
    binary = resolve_harbor_bin()
    source = snapshot_dir(job, work_dir, output_dir)
    injected_secrets: list[str] = []
    agent_env: dict[str, str] = {}
    verifier_env: dict[str, str] = {}
    notes = ["派生任务使用原始记录副本，保留原任务的产物和评分记录"]
    resolved = resolve_agent_model(job) if action.mode in {"resume", "rerun"} else None
    if resolved is not None:
        agent_env.update(resolved.env)
        injected_secrets.extend(resolved.secrets)
    if action.mode in {"resume", "rerun"}:
        agent_env.update(config.agent_env)
    if config.judge_model_api_id and not config.disable_verification:
        judge = model_api_service.require_ready(config.judge_model_api_id, config.judge_api_protocol, user_id=job.owner_id)
        verifier_env.update({
            "JUDGE_API_KEY": judge.api_key.strip(), "JUDGE_BASE_URL": judge.base_url.strip(),
            "JUDGE_MODEL": judge.model_id, "JUDGE_PROVIDER": judge.provider,
            "JUDGE_API_PROTOCOL": config.judge_api_protocol,
        })
    verifier_env.update(config.verifier_env)
    verifier_env = {key: value for key, value in verifier_env.items() if value}
    agent_refs, verifier_refs, env = stage_environment(agent_env, verifier_env, prefix="PRT_SELECTED")
    if action.mode == "rerun":
        argv = ["run", "--config", str(work_dir / "selected-config.json")]
        notes.append("仅重新执行选中的一次 trial，沿用其 Agent、环境和超时配置")
    elif action.mode == "resume":
        argv = ["job", "resume", "--job-path", str(source)]
        for error in action.filter_error_types:
            argv += ["--filter-error-type", error]
        notes.append("保留已完成 trial；未完成及选定错误类型的 trial 从头执行，不恢复 Agent 会话或容器状态")
    else:
        task_path = verifier_path(config)
        if config.batch_judge:
            from ..services.harbor_judge_service import prepare_batched_tasks
            try:
                task_path, count = prepare_batched_tasks(task_path, work_dir / "batch-judge-tasks")
            except (OSError, ValueError) as exc:
                raise BuildError(f"无法启用 Batch Judge: {exc}") from exc
            notes.append(f"Batch Judge：{count} 份 Judge 配置已设置 mode=batched")
        # Same reward-kit entry adaptation as a normal run; a newly selected
        # grading set has never been through it.
        try:
            adapted = harbor_compat.prepare_rewardkit_tasks(task_path)
        except (OSError, ValueError) as exc:
            raise BuildError(f"无法更新 reward-kit 兼容入口: {exc}") from exc
        if adapted:
            notes.append(f"已适配 {adapted} 个 reward-kit 评分入口")
        argv = ["job", "regrade", str(source), "--task-path", str(task_path),
                "--job-name", job.id, "--jobs-dir", str(output_dir),
                "--n-concurrent", str(config.n_concurrent), "--env", config.environment_type]
        argv += kv_flags("--verifier-env", verifier_refs)
        custom_verifier = config.extra_args.get("verifier") or config.extra_args.get("--verifier")
        if custom_verifier:
            argv += ["--verifier", str(custom_verifier)]
        argv += kv_flags("--verifier-kwarg", config.verifier_kwargs)
        notes.append("仅运行独立 verifier；资源与超时取评分题包配置，不执行 Agent")
    extra_files = {}
    if action.mode == "resume" and not action.trial_name:
        extra_files["harbor-env-refs.json"] = json.dumps({"agent": agent_refs, "verifier": verifier_refs})
    if action.trial_name and action.mode in {"resume", "rerun"}:
        native = selected_config(job, output_dir)
        native["agents"][0].setdefault("env", {}).update(agent_refs)
        native["verifier"].setdefault("env", {}).update(verifier_refs)
        extra_files["selected-config.json"] = json.dumps(native, ensure_ascii=False)
        notes.append(f"源 trial：{action.trial_name}；本次仅执行 1 条数据")
    validate_secret_env(env)
    secrets = list(dict.fromkeys([*collect_secrets(agent_env, verifier_env, env, job.resources.env), *injected_secrets]))
    runtime = "harbor_runtime.py"
    return BuildResult(
        command=[resolve_harbor_python(binary), "-u", str(work_dir / runtime), *argv],
        cwd=str(work_dir), output_dir=str(output_dir), env={"PYTHONUNBUFFERED": "1", **env},
        files={runtime: (settings.resources_dir / runtime).read_text(encoding="utf-8"), **extra_files},
        notes=notes, secrets=secrets,
    )


def build_inplace_rerun(job: Job, work_dir: Path, output_dir: Path) -> BuildResult:
    from ..models.job import HarborAction
    from ..services.harbor_action_service import source_dir

    root = source_dir(job)
    operation = job.harbor_rerun
    trials, recorded, env, secrets = [], {}, {}, []
    for row in operation.selections:
        action = HarborAction(mode="rerun", source_job_id=job.id,
                              trial_name=row["trial_name"], selected_job_config=row["job_config"])
        built = build_action(job.model_copy(update={"harbor_action": action}), work_dir, output_dir)
        native = json.loads(built.files["selected-config.json"])
        trial = dict(row["trial_config"])
        trial.update(trials_dir=str(root), agent=native["agents"][0], verifier=native["verifier"],
                     source_trial=None)
        if not row["trial_name"].startswith("@planned:"):
            trial["trial_name"] = row["trial_name"]
        canonical = dict(row["trial_config"])
        canonical.update(trial_name=trial["trial_name"], source_trial=None)
        if row["trial_name"].startswith("@planned:"):
            canonical["trials_dir"] = str(root)
        recorded[trial["trial_name"]] = canonical
        trials.append(trial)
        env.update(built.env)
        secrets.extend(built.secrets)
    plan = {"root": str(root), "trials": trials, "recorded_configs": recorded,
            "backup": str(work_dir / "rerun-history" / operation.operation_id)}
    script = "harbor_inplace_rerun.py"
    return BuildResult(
        command=[resolve_harbor_python(resolve_harbor_bin()), "-u", str(work_dir / script),
                 str(work_dir / "inplace-rerun.json")],
        cwd=str(work_dir), output_dir=str(output_dir), env=env, secrets=secrets,
        files={script: (settings.resources_dir / script).read_text(encoding="utf-8"),
               "harbor_runtime.py": (settings.resources_dir / "harbor_runtime.py").read_text(encoding="utf-8"),
               "inplace-rerun.json": json.dumps(plan, ensure_ascii=False)},
        notes=[f"在原任务内重跑选中的 {len(trials)} 条数据，保留 trial 名称和未选中记录",
               "执行前备份选中记录；完成后重算原任务评分汇总"],
    )
