"""Harbor evaluation command builder (Verify jobs).

Flag names are taken from the *checked-out* Harbor CLI
(``src/harbor/cli/jobs.py``), not from documentation — 0.22 renamed several of
them (``--task-name`` -> ``--include-task-name``, ``--sharegpt`` ->
``--export-sharegpt``) and dropped others. ``tests/test_config_keys.py`` parses
that file and fails the build if we ever emit a flag Harbor does not accept.

Mapping from the submission form:

* pass@k        -> ``--n-attempts / -k``
* 并发           -> ``--n-concurrent / -n``
* agent / model -> ``--agent / -a``, ``--model / -m``
* agent 环境变量 -> ``--agent-env``  (进入 agent 容器)
* judge 环境变量 -> ``--verifier-env`` (reward-kit judge 的 API Key)
* 沙箱后端       -> ``--env / -e``
"""

from __future__ import annotations

import shutil
from pathlib import Path

from ..config import settings
from ..models import DatasetKind, Job, VerifyConfig
from ..store import store
from .base import BuildError, BuildResult, kv_flags

_NO_MODEL_AGENTS = {"oracle", "nop"}


def resolve_harbor_bin() -> str:
    configured = settings.harbor_bin
    if configured and (shutil.which(configured) or Path(configured).is_file()):
        return shutil.which(configured) or configured
    raise BuildError(
        "未找到 harbor 命令。请在部署环境安装 Harbor（例如 `uv tool install harbor`）"
        "或在系统设置中配置 PRT_HARBOR_BIN 指向可执行文件。"
    )


def build(job: Job, work_dir: Path, output_dir: Path) -> BuildResult:
    config: VerifyConfig = job.verify  # type: ignore[assignment]
    if config is None:
        raise BuildError("缺少评测配置")

    binary = resolve_harbor_bin()
    notes: list[str] = []

    command: list[str] = [binary, "run", "--job-name", job.id, "--jobs-dir", str(output_dir)]

    # ---- task source -----------------------------------------------------
    sources = 0
    if config.dataset_id:
        dataset = store.datasets.get(config.dataset_id)
        if dataset is None:
            raise BuildError(f"数据集不存在: {config.dataset_id}")
        if dataset.kind != DatasetKind.HARBOR_TASKS:
            raise BuildError(f"评测需要 Harbor 任务树数据集（当前为 {dataset.kind.value}）")
        command += ["--path", dataset.root_path]
        notes.append(f"任务集: {dataset.name}（{dataset.stats.n_tasks} 个任务）")
        sources += 1
    if config.tasks_path:
        path = Path(config.tasks_path)
        if not path.is_dir():
            raise BuildError(f"任务目录不存在: {path}")
        command += ["--path", str(path)]
        sources += 1
    if config.registry_dataset:
        command += ["--dataset", config.registry_dataset]
        notes.append(f"registry 数据集: {config.registry_dataset}")
        sources += 1
    if sources == 0:
        raise BuildError("请选择要评测的 Harbor 任务集")
    if sources > 1:
        raise BuildError("同时指定了多个任务来源，请只保留一个")

    # ---- agent / model ---------------------------------------------------
    command += ["--agent", config.agent]
    if config.model:
        command += ["--model", config.model]
    elif config.agent not in _NO_MODEL_AGENTS:
        raise BuildError(f"agent {config.agent} 需要指定模型（--model）")
    if config.agent_import_path:
        command += ["--agent-import-path", config.agent_import_path]

    # ---- scale -----------------------------------------------------------
    command += ["--n-concurrent", str(config.n_concurrent)]
    if config.n_attempts > 1:
        command += ["--n-attempts", str(config.n_attempts)]
        notes.append(f"pass@k：每个任务尝试 {config.n_attempts} 次，结果会给出 pass@1..pass@{config.n_attempts}")
    if config.max_retries:
        command += ["--max-retries", str(config.max_retries)]
    command += ["--env", config.environment_type]

    # ---- task filters ----------------------------------------------------
    for pattern in config.task_names:
        command += ["--include-task-name", pattern]
    for pattern in config.exclude_task_names:
        command += ["--exclude-task-name", pattern]
    if config.n_tasks:
        command += ["--n-tasks", str(config.n_tasks)]

    # ---- kwargs / env ----------------------------------------------------
    command += kv_flags("--agent-env", config.agent_env)
    command += kv_flags("--agent-kwarg", config.agent_kwargs)
    command += kv_flags("--verifier-env", config.verifier_env)
    command += kv_flags("--environment-kwarg", config.environment_kwargs)
    command += kv_flags("--verifier-kwarg", config.verifier_kwargs)

    if config.environment_cpus:
        command += ["--override-cpus", str(config.environment_cpus)]
    if config.environment_memory_mb:
        command += ["--override-memory-mb", str(config.environment_memory_mb)]
    if config.environment_gpus:
        command += ["--override-gpus", str(config.environment_gpus)]

    # ---- timeouts / behaviour -------------------------------------------
    if config.timeout_multiplier != 1.0:
        command += ["--timeout-multiplier", str(config.timeout_multiplier)]
    if config.agent_timeout_multiplier:
        command += ["--agent-timeout-multiplier", str(config.agent_timeout_multiplier)]
    if config.verifier_timeout_multiplier:
        command += ["--verifier-timeout-multiplier", str(config.verifier_timeout_multiplier)]
    if config.disable_verification:
        command.append("--disable-verification")
        notes.append("已关闭 verifier：只跑 agent，不打分。")
    if config.force_build:
        command.append("--force-build")
        notes.append("强制重建任务镜像，首次运行会明显变慢。")
    if config.export_traces:
        command.append("--export-traces")
        if config.sharegpt:
            command.append("--export-sharegpt")
        notes.append("运行结束后会导出 ATIF 轨迹，可用于后续 SFT / 蒸馏。")

    # A .env file lets task.toml's ${VAR} interpolation resolve (that is how
    # reward-kit judge tasks pick up their API keys). Written 0600.
    secrets_map = {**config.agent_env, **config.verifier_env}
    if secrets_map:
        env_path = work_dir / ".env"
        work_dir.mkdir(parents=True, exist_ok=True)
        env_path.write_text(
            "".join(f"{key}={value}\n" for key, value in secrets_map.items() if key), encoding="utf-8"
        )
        env_path.chmod(0o600)
        command += ["--env-file", str(env_path)]

    for key, value in config.extra_args.items():
        flag = key if key.startswith("-") else f"--{key.replace('_', '-')}"
        if value is True:
            command.append(flag)
        elif value not in (False, None, ""):
            command += [flag, str(value)]

    # Secrets also go through the child's environment so agents that re-exec
    # still see them; they are masked everywhere the platform renders them.
    env = {"PYTHONUNBUFFERED": "1", **secrets_map}

    if config.agent in _NO_MODEL_AGENTS:
        notes.append(
            "oracle / nop 属于自检 agent：oracle 应当接近 1.0，nop 应当为 0，用来验证任务集本身是否可靠。"
        )

    secrets = [value for key, value in secrets_map.items() if value and _looks_secret(key)]

    return BuildResult(
        command=command,
        cwd=str(work_dir),
        env=env,
        files={},
        output_dir=str(output_dir),
        notes=notes,
        secrets=secrets,
    )


def _looks_secret(key: str) -> bool:
    upper = key.upper()
    return any(token in upper for token in ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL"))
