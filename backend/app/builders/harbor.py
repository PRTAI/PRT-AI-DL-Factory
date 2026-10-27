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

import json
import os
import re
import shutil
import tomllib
from pathlib import Path

from fastapi import HTTPException

from ..config import settings
from ..models import DatasetKind, Job, VerifyConfig
from ..services import cc_bridge_service, harbor_compat, harbor_env_service, model_api_service, resources_service
from ..store import store
from .base import BuildError, BuildResult, kv_flags

_NO_MODEL_AGENTS = {"oracle", "nop"}


def resolve_agent_model(job: Job) -> model_api_service.ResolvedModel | None:
    """排队期间部署被停 / 密钥被删时，启动阶段给出可读的失败原因。"""
    try:
        return model_api_service.resolve_agent(job.verify, user_id=job.owner_id)
    except HTTPException as exc:
        raise BuildError(str(exc.detail)) from exc


def task_image_env() -> dict[str, str]:
    """平台题目镜像的保留数量；harbor_runtime 在每次新建镜像后据此清理旧镜像。"""
    return {"PRT_KEEP_TASK_IMAGES": str(store.system_settings().keep_recent_task_images)}


def _codex_config(source: str, base_url: str) -> str:
    """合并 Harbor 支持的 JSON/原生 TOML 来源，并明确选择 Responses。"""
    try:
        if source.strip().startswith("{"):
            native = json.loads(source)
        elif source.strip():
            with Path(source).expanduser().open("rb") as handle:
                native = tomllib.load(handle)
        else:
            native = {}
    except (OSError, ValueError) as exc:
        raise BuildError("Codex config 必须是有效 JSON 对象或可读取的 TOML 文件路径") from exc
    if not isinstance(native, dict) or not isinstance(native.get("model_providers", {}), dict):
        raise BuildError("Codex config 与 model_providers 必须是 JSON 对象")
    native["model_provider"] = "prt"
    native.setdefault("model_providers", {})["prt"] = {
        "name": "PRT Model API", "base_url": base_url,
        "env_key": "OPENAI_API_KEY", "wire_api": "responses",
        "requires_openai_auth": False, "supports_websockets": False,
    }
    native.setdefault("web_search", "disabled")
    # 0 表示按各服务的启动超时等待，避免默认 1 秒宽限遗漏尚未就绪的 MCP 工具。
    native.setdefault("mcp_optional_startup_grace_ms", 0)
    try:
        return json.dumps(native, ensure_ascii=False)
    except TypeError as exc:
        raise BuildError("Codex TOML 配置包含无法转换为 JSON 的值") from exc


def validate_secret_env(env: dict[str, str]) -> None:
    """Harbor 会盲替换密钥值；短值或 JSON 标量会破坏数字/布尔字段。"""
    for key, value in env.items():
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise BuildError("环境变量名称只能包含字母、数字和下划线，且不能以数字开头")
        if not value or not _looks_secret(key):
            continue
        try:
            literal = json.loads(value)
            json_placeholder = literal is None or isinstance(literal, (int, float, bool))
        except ValueError:
            json_placeholder = False
        if len(value.strip()) < 4 or json_placeholder:
            raise BuildError(
                f"环境变量 {key} 的密钥值过短或为数字/布尔占位值，会导致 Harbor 脱敏破坏结果文件。"
                "请填写真实密钥；不需要的变量请删除，不要用 1 等短值占位。"
            )


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


def resolve_harbor_python(binary: str) -> str:
    if settings.harbor_python:
        return settings.harbor_python
    try:
        first_line = Path(binary).read_text(encoding="utf-8").splitlines()[0]
        if first_line.startswith("#!") and "python" in first_line:
            import shlex
            parts = shlex.split(first_line[2:])
            if len(parts) == 1 and Path(parts[0]).is_file():
                return parts[0]
    except (OSError, UnicodeError, IndexError):
        pass
    candidate = Path(binary).parent / "python"
    if candidate.is_file():
        return str(candidate)
    raise BuildError("请配置 PRT_HARBOR_PYTHON 指向 Harbor 的 Python 解释器，以启用阶段超时和容器进程回收")


def build(job: Job, work_dir: Path, output_dir: Path) -> BuildResult:
    if job.harbor_rerun is not None:
        from .harbor_actions import build_inplace_rerun
        return build_inplace_rerun(job, work_dir, output_dir)
    if job.harbor_action is not None:
        from .harbor_actions import build_action
        return build_action(job, work_dir, output_dir)
    config: VerifyConfig = job.verify  # type: ignore[assignment]
    if config is None:
        raise BuildError("缺少评测配置")
    agent = config.agent_import_path.strip() or config.agent

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
    local_root = Path(dataset.root_path) if dataset is not None else Path(config.tasks_path) if config.tasks_path else None
    if config.batch_judge and not config.disable_verification:
        from ..services.harbor_judge_service import prepare_batched_tasks
        if local_root is None:
            raise BuildError("Batch Judge 需要本地任务集，请先导入 Registry 任务集")
        try:
            local_root, count = prepare_batched_tasks(local_root, work_dir / "batch-judge-tasks")
        except (OSError, ValueError) as exc:
            raise BuildError(f"无法启用 Batch Judge: {exc}") from exc
        command[command.index("--path") + 1] = str(local_root)
        notes.append(f"Batch Judge：{count} 份 Judge 配置已设置 mode=batched，原题包保持不变")
    if config.use_platform_image:
        from ..services import harbor_image_service
        if config.environment_type != "docker" or local_root is None or config.force_build:
            raise BuildError("平台镜像需要本地任务集、Docker 环境，并关闭强制重建")
        image = harbor_image_service.status()
        if not image["ready"]:
            raise BuildError("平台 Harbor 镜像尚未就绪，请管理员先构建")
        try:
            destination = work_dir / "platform-image-tasks"
            pinned = harbor_image_service.pin_image(image["image_id"]) if not destination.exists() else ""
            local_root = harbor_image_service.prepare_tasks(local_root, destination, pinned)
        except (OSError, ValueError) as exc:
            raise BuildError(str(exc)) from exc
        command[command.index("--path") + 1] = str(local_root)
        notes.append("使用平台 Harbor 镜像；题目层按内容首次构建一次并打标签，之后的 trial 与评测直接复用，不再 compose build；原数据集保持不变")
    if local_root is not None:
        try:
            adapted = harbor_compat.prepare_rewardkit_tasks(local_root)
        except (OSError, ValueError) as exc:
            raise BuildError(f"无法更新 reward-kit 兼容入口: {exc}") from exc
        if adapted:
            notes.append(f"已适配 {adapted} 个 reward-kit 评分入口：无挂载权限时保留独立副本隔离")

    # ---- agent / model ---------------------------------------------------
    command += ["--agent", agent]
    injected_env: dict[str, str] = {}
    injected_secrets: list[str] = []
    # 平台模型配置（提交人自己的密钥）或本地部署：--model 与密钥/base_url 按 Harbor 的标准环境变量注入
    resolved = resolve_agent_model(job)
    if resolved is not None:
        command += ["--model", resolved.model]
        injected_env = resolved.env
        injected_secrets = resolved.secrets
        notes.extend(resolved.notes)
    elif config.model:
        command += ["--model", config.model]
    elif agent not in _NO_MODEL_AGENTS:
        raise BuildError(f"agent {agent} 需要指定模型（--model）")

    # 思考模式：各 agent 接受的集合不一样，收敛到它支持的最近一档
    agent_kwargs = dict(config.agent_kwargs)
    if config.reasoning_effort and agent not in _NO_MODEL_AGENTS:
        effort, note = model_api_service.clamp_effort(
            config.reasoning_effort, resources_service.effort_levels_for(agent)
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
        judge = model_api_service.require_ready(config.judge_model_api_id, config.judge_api_protocol, user_id=job.owner_id)
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
    if agent == "codex":
        # Harbor 接受内联 JSON config，并写入每个 trial 的 CODEX_HOME/config.toml。
        # 自定义 provider 明确走 Responses，不能仅依赖 OPENAI_BASE_URL。
        agent_kwargs["config"] = _codex_config(
            agent_kwargs.get("config", ""),
            agent_env.get("OPENAI_BASE_URL") or agent_env.get("OPENAI_API_BASE") or "https://api.openai.com/v1",
        )
    for env_source in (agent_env, verifier_env, job.resources.env):
        validate_secret_env(env_source)
    agent_refs, verifier_refs, stage_env = stage_environment(agent_env, verifier_env)
    command += kv_flags("--agent-env", agent_refs)
    command += kv_flags("--agent-kwarg", agent_kwargs)
    command += kv_flags("--verifier-env", verifier_refs)
    command += kv_flags("--environment-kwarg", config.environment_kwargs)
    command += kv_flags("--verifier-kwarg", config.verifier_kwargs)

    if config.environment_cpus:
        command += ["--override-cpus", str(config.environment_cpus)]
    if config.environment_memory_mb:
        command += ["--override-memory-mb", str(config.environment_memory_mb)]
    if config.environment_gpus is not None:
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

    # Bare dotenv declarations acknowledge optional inputs without reparsing values.
    # Actual values travel only through the process environment.
    secrets_map = stage_env
    optional_references: set[str] = set()
    if local_root is not None:
        # [environment.env] / [verifier.environment.env] 的 ${VAR} 也从宿主机解析；没在 .env
        # 里声明的变量 Harbor 会停下来要确认，而子进程 stdin 是 /dev/null，会直接崩。
        detected = harbor_env_service.inspect_root(local_root)
        for field in [*detected["variables"], *detected["environment_variables"]]:
            for reference in field["source_variables"]:
                if reference in secrets_map:
                    continue
                inherited = job.resources.env.get(reference, os.environ.get(reference))
                if inherited is not None:
                    secrets_map[reference] = inherited
                elif not field["required"]:
                    optional_references.add(reference)
                # 必填又没有值：交给 Harbor 报「Missing Environment Variables」
        validate_secret_env(secrets_map)
    if secrets_map or optional_references:
        env_path = work_dir / ".env"
        work_dir.mkdir(parents=True, exist_ok=True)
        env_path.write_text(
            "".join(f"{key}\n" for key in sorted(secrets_map))
            # dotenv 的无值键只声明来源，不覆盖各题默认值；Harbor 视其为已显式确认。
            + "".join(f"{key}\n" for key in sorted(optional_references)), encoding="utf-8"
        )
        env_path.chmod(0o600)
        command += ["--env-file", str(env_path)]

    for key, value in config.extra_args.items():
        flag = key if key.startswith("-") else f"--{key.replace('_', '-')}"
        if value is True:
            command.append(flag)
        elif value is not False and value is not None and value != "":
            command += [flag, str(value)]

    # Secrets also go through the child's environment so agents that re-exec
    # still see them; they are masked everywhere the platform renders them.
    env = {"PYTHONUNBUFFERED": "1", **secrets_map, **task_image_env()}

    if agent in _NO_MODEL_AGENTS:
        notes.append(
            "oracle / nop 属于自检 agent：oracle 应当接近 1.0，nop 应当为 0，用来验证任务集本身是否可靠。"
        )

    secrets = collect_secrets(injected_env, config.agent_env, judge_env, config.verifier_env,
                              secrets_map, job.resources.env)
    secrets += [value for value in injected_secrets if value not in secrets]

    runtime_file = "harbor_runtime.py"
    command = [resolve_harbor_python(binary), "-u", str(work_dir / runtime_file), *command[1:]]
    notes.append("平台不设评测时限；Agent / Verifier 超时由 task.toml、Harbor 默认值及倍率配置决定")
    return BuildResult(
        command=command,
        cwd=str(work_dir),
        env=env,
        files={runtime_file: (settings.resources_dir / runtime_file).read_text(encoding="utf-8"),
               **cc_bridge_service.runtime_files(config), **harbor_compat.judge_meter_files(verifier_env)},
        output_dir=str(output_dir),
        notes=notes,
        secrets=secrets,
    )


def _looks_secret(key: str) -> bool:
    upper = key.upper()
    return bool(re.search(r"KEY|SECRET|TOKEN(?!S)|PASSWORD|CREDENTIAL|AUTH", upper))


def collect_secrets(*sources: dict[str, str]) -> list[str]:
    return list(dict.fromkeys(value for source in sources for key, value in source.items()
                              if value and _looks_secret(key)))


def stage_environment(
    agent: dict[str, str], verifier: dict[str, str], *, prefix: str = "PRT",
) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    # Host-side agent SDKs need the agent aliases. Verifier values never overwrite them.
    env = {**verifier, **agent}
    refs = []
    for stage, values in (("AGENT", agent), ("VERIFIER", verifier)):
        validate_secret_env(values)
        mapping = {}
        for key, value in values.items():
            name = f"{prefix}_{stage}_{key}"
            env[name] = value
            mapping[key] = "${" + name + "}"
        refs.append(mapping)
    return refs[0], refs[1], env
