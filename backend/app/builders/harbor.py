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

import os
import shutil
from pathlib import Path

from ..config import settings
from ..models import DatasetKind, Job, VerifyConfig
from ..services import model_api_service, resources_service
from ..store import store
from .base import BuildError, BuildResult, kv_flags

_NO_MODEL_AGENTS = {"oracle", "nop"}


def resolve_harbor_bin() -> str:
    configured = (settings.harbor_bin or "harbor").strip()
    found = shutil.which(configured)
    if found:
        return found
    if Path(configured).is_file():
        return configured
    # 裸名不在 PATH 上：Harbor CLI 装在它自己的 conda 环境里（平台跑在
    # prt-ai-dl-factory 环境，PATH 上没有它）。按框架解释器的同一套约定，
    # 用 PRT_HARBOR_PYTHON 推断同目录下的 harbor，省得每台机器都配绝对路径。
    sibling = _sibling_of_harbor_python(configured)
    if sibling:
        return sibling
    raise BuildError(
        f"未找到 harbor 命令（{configured!r}）。请把 PRT_HARBOR_BIN 配成绝对路径"
        "（例如 /home/user/.conda/envs/harbor/bin/harbor），或设置 PRT_HARBOR_PYTHON "
        "指向 harbor 环境的解释器，平台会自动使用同目录下的 harbor。"
    )


def _sibling_of_harbor_python(name: str) -> str | None:
    """``<PRT_HARBOR_PYTHON 所在目录>/<name>`` —— 若存在且可执行则返回。"""
    python = (settings.harbor_python or "").strip()
    if not python or os.sep not in python:
        return None
    candidate = Path(python).resolve().parent / name
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate)
    return None


def build(job: Job, work_dir: Path, output_dir: Path) -> BuildResult:
    config: VerifyConfig = job.verify  # type: ignore[assignment]
    if config is None:
        raise BuildError("缺少评测配置")

    binary = resolve_harbor_bin()
    notes: list[str] = []

    command: list[str] = [binary, "run", "--job-name", job.id, "--jobs-dir", str(output_dir)]

    # ---- task source -----------------------------------------------------
    sources = 0
    dataset = None
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
    injected_env: dict[str, str] = {}
    injected_secrets: list[str] = []
    if config.model_api_id:
        # 平台维护的模型配置：--model 与密钥/base_url 都由后端按 Harbor 的标准环境变量注入
        resolved = model_api_service.resolve(
            config.model_api_id, model_style=resources_service.model_style_for(config.agent)
        )
        command += ["--model", resolved.model]
        injected_env = resolved.env
        injected_secrets = resolved.secrets
        notes.extend(resolved.notes)
    elif config.model:
        command += ["--model", config.model]
    elif config.agent not in _NO_MODEL_AGENTS:
        raise BuildError(f"agent {config.agent} 需要指定模型（--model）")
    if config.agent_import_path:
        command += ["--agent-import-path", config.agent_import_path]

    # 思考模式：各 agent 接受的集合不一样，收敛到它支持的最近一档
    agent_kwargs = dict(config.agent_kwargs)
    if config.reasoning_effort and config.agent not in _NO_MODEL_AGENTS:
        effort, note = model_api_service.clamp_effort(
            config.reasoning_effort, resources_service.effort_levels_for(config.agent)
        )
        if effort:
            agent_kwargs.setdefault("reasoning_effort", effort)
            notes.append(f"思考模式 reasoning_effort={effort}")
        if note:
            notes.append(note)

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

    # ---- reward-kit judge ------------------------------------------------
    # reward-kit 套件的 tests/test.sh 从 JUDGE_* 读 judge 配置。平台只注入这 5 个，
    # 值为空的跳过（不发默认值），标准 provider 变量由任务侧的 test.sh 自行映射。
    judge_env: dict[str, str] = {}
    if dataset is not None and dataset.rewardkit and not config.disable_verification and not config.judge_model_api_id:
        raise BuildError("该任务集是 reward-kit 格式，请选择 judge 模型（用于注入 JUDGE_* 环境变量）")
    if config.judge_model_api_id and not config.disable_verification:
        judge = model_api_service.require_ready(config.judge_model_api_id)
        judge_env = {
            "JUDGE_API_KEY": judge.api_key.strip(),
            "JUDGE_BASE_URL": judge.base_url.strip(),
            "JUDGE_MODEL": judge.model_id,  # 裸模型名，不带 litellm provider/ 前缀
            "JUDGE_PROVIDER": judge.provider,
            "JUDGE_API_PROTOCOL": config.judge_api_protocol,
        }
        judge_env = {key: value for key, value in judge_env.items() if value}
        notes.append(f"judge 模型「{judge.name}」→ JUDGE_MODEL={judge.model_id}，协议 {config.judge_api_protocol}")

    # ---- kwargs / env ----------------------------------------------------
    # 用户手填的 agent_env 优先于模型配置注入的（万一要临时覆盖 base_url）；
    # verifier_env 同理，手填优先于 judge 模型注入的 JUDGE_*。
    agent_env = {**injected_env, **config.agent_env}
    verifier_env = {**judge_env, **config.verifier_env}
    command += kv_flags("--agent-env", agent_env)
    command += kv_flags("--agent-kwarg", agent_kwargs)
    command += kv_flags("--verifier-env", verifier_env)
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
    if config.verbose_log:
        # rich 进度条在非终端里不输出；--debug 让 Harbor 把每个 trial 的安装命令、
        # agent 命令与失败原因打进日志。
        command.append("--debug")
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
    secrets_map = {**agent_env, **verifier_env}
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
    secrets += [value for value in injected_secrets if value not in secrets]

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
