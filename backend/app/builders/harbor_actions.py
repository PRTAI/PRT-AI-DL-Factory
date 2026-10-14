"""Recovery commands use Harbor's dedicated CLI, not an oracle/nop run."""

from pathlib import Path

from ..config import settings
from ..models import Job
from ..services import model_api_service, resources_service
from ..services.harbor_action_service import snapshot_dir, verifier_path
from .base import BuildError, BuildResult, kv_flags
from .harbor import _looks_secret, resolve_harbor_bin, resolve_harbor_python, validate_secret_env


def build_action(job: Job, work_dir: Path, output_dir: Path) -> BuildResult:
    config = job.verify
    action = job.harbor_action
    binary = resolve_harbor_bin()
    source = snapshot_dir(job, work_dir, output_dir)
    agent_env: dict[str, str] = {}
    verifier_env: dict[str, str] = {}
    notes = ["派生任务使用原始记录副本，保留原任务的产物和评分记录"]
    if action.mode == "resume" and config.model_api_id:
        resolved = model_api_service.resolve(
            config.model_api_id, model_style=resources_service.model_style_for(config.agent),
            protocol=model_api_service.agent_protocol(config.agent, config.agent_api_protocol),
        )
        agent_env.update(resolved.env)
    if action.mode == "resume":
        agent_env.update(config.agent_env)
    if config.judge_model_api_id and not config.disable_verification:
        judge = model_api_service.require_ready(config.judge_model_api_id, config.judge_api_protocol)
        verifier_env.update({
            "JUDGE_API_KEY": judge.api_key.strip(), "JUDGE_BASE_URL": judge.base_url.strip(),
            "JUDGE_MODEL": judge.model_id, "JUDGE_PROVIDER": judge.provider,
            "JUDGE_API_PROTOCOL": config.judge_api_protocol,
        })
    verifier_env.update(config.verifier_env)
    verifier_env = {key: value for key, value in verifier_env.items() if value}
    if action.mode == "resume":
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
        argv = ["job", "regrade", str(source), "--task-path", str(task_path),
                "--job-name", job.id, "--jobs-dir", str(output_dir),
                "--n-concurrent", str(config.n_concurrent), "--env", config.environment_type]
        argv += kv_flags("--verifier-env", verifier_env)
        notes.append("仅运行独立 verifier；资源与超时取评分题包配置，不执行 Agent")
    env = {**agent_env, **verifier_env}
    validate_secret_env(env)
    secrets = [value for key, value in {**env, **job.resources.env}.items() if value and _looks_secret(key)]
    runtime = "harbor_runtime.py"
    return BuildResult(
        command=[resolve_harbor_python(binary), "-u", str(work_dir / runtime), *argv],
        cwd=str(work_dir), output_dir=str(output_dir), env={"PYTHONUNBUFFERED": "1", **env},
        files={runtime: (settings.resources_dir / runtime).read_text(encoding="utf-8")},
        notes=notes, secrets=secrets,
    )
