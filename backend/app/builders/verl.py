"""verl command builder (GRPO / DAPO / PPO / RLOO / REINFORCE++ / ReMax).

verl is driven by Hydra, so a job becomes ``python -m verl.trainer.main_ppo``
plus a list of ``key=value`` overrides. Only keys that exist in the checked-out
verl config tree are emitted — anything the installed version cannot consume is
skipped with a note instead of producing a command that dies on startup.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..config import settings
from ..models import DatasetKind, Job, RLConfig, TrainStage
from ..services import dataset_service
from ..store import store
from .base import BuildError, BuildResult

# our algorithm name -> verl algorithm.adv_estimator
_ADV_ESTIMATOR = {
    TrainStage.GRPO: "grpo",
    TrainStage.DAPO: "grpo",
    TrainStage.PPO: "gae",
    TrainStage.RLOO: "rloo",
    TrainStage.REINFORCE_PLUS_PLUS: "reinforce_plus_plus",
    TrainStage.REMAX: "remax",
}


def _hydra_list(values: list[str]) -> str:
    return "[" + ",".join(json.dumps(v) for v in values) + "]"


def build(job: Job, work_dir: Path, output_dir: Path) -> BuildResult:
    config: RLConfig = job.rl  # type: ignore[assignment]
    if config is None:
        raise BuildError("缺少 RL 配置")

    root = settings.verl_root
    if not (root / "verl").is_dir():
        raise BuildError(f"verl 源码目录无效: {root}")

    estimator = _ADV_ESTIMATOR.get(config.algorithm)
    if estimator is None:
        raise BuildError(f"verl 不支持算法 {config.algorithm.value}，请改用 TRL")

    dataset = store.datasets.get(config.dataset_id)
    if dataset is None:
        raise BuildError(f"数据集不存在: {config.dataset_id}")
    if dataset.kind != DatasetKind.VERL_PARQUET:
        raise BuildError(
            f"verl 需要 parquet 数据集（当前为 {dataset.kind.value}）。"
            "Harbor 任务树请改用 TRL + Harbor 环境进行 agentic RL。"
        )

    meta = dataset_service.read_meta(dataset)
    train_files = meta.get("train_files") or []
    val_files = meta.get("val_files") or []
    if not train_files:
        raise BuildError("数据集中没有训练用 parquet 文件")

    if config.eval_dataset_id:
        eval_dataset = store.datasets.get(config.eval_dataset_id)
        if eval_dataset is None:
            raise BuildError(f"验证集不存在: {config.eval_dataset_id}")
        eval_meta = dataset_service.read_meta(eval_dataset)
        val_files = (eval_meta.get("train_files") or []) + (eval_meta.get("val_files") or [])
    if not val_files:
        val_files = train_files[:1]

    n_gpus = max(1, job.resources.n_gpus)
    notes: list[str] = []
    is_dapo = config.algorithm == TrainStage.DAPO

    overrides: list[str] = [
        f"algorithm.adv_estimator={estimator}",
        f"algorithm.use_kl_in_reward={config.use_kl_in_reward}",
        f"algorithm.norm_adv_by_std_in_grpo={config.norm_adv_by_std_in_grpo}",
        # data
        f"data.train_files={_hydra_list(train_files)}",
        f"data.val_files={_hydra_list(val_files)}",
        f"data.train_batch_size={config.train_batch_size}",
        f"data.max_prompt_length={config.max_prompt_length}",
        f"data.max_response_length={config.max_response_length}",
        f"data.prompt_key={config.prompt_key}",
        f"data.truncation={config.truncation}",
        f"data.filter_overlong_prompts={config.filter_overlong_prompts}",
        f"data.reward_fn_key={config.reward_fn_key}",
        # actor / model
        f"actor_rollout_ref.model.path={config.model_path}",
        "actor_rollout_ref.model.use_remove_padding=True",
        f"actor_rollout_ref.model.enable_gradient_checkpointing={config.gradient_checkpointing}",
        f"actor_rollout_ref.actor.optim.lr={config.learning_rate}",
        f"actor_rollout_ref.actor.optim.weight_decay={config.weight_decay}",
        f"actor_rollout_ref.actor.optim.lr_warmup_steps_ratio={config.warmup_ratio}",
        f"actor_rollout_ref.actor.ppo_mini_batch_size={config.mini_batch_size}",
        f"actor_rollout_ref.actor.ppo_micro_batch_size_per_gpu={config.micro_batch_size_per_gpu}",
        f"actor_rollout_ref.actor.use_dynamic_bsz={config.use_dynamic_bsz}",
        f"actor_rollout_ref.actor.entropy_coeff={config.entropy_coeff}",
        f"actor_rollout_ref.actor.loss_agg_mode={config.loss_agg_mode}",
        f"actor_rollout_ref.actor.use_kl_loss={config.use_kl_loss}",
        f"actor_rollout_ref.actor.kl_loss_coef={config.kl_loss_coef}",
        f"actor_rollout_ref.actor.strategy={config.strategy}",
        f"actor_rollout_ref.actor.fsdp_config.param_offload={config.param_offload}",
        f"actor_rollout_ref.actor.fsdp_config.optimizer_offload={config.optimizer_offload}",
        # rollout
        f"actor_rollout_ref.rollout.name={config.rollout.backend}",
        f"actor_rollout_ref.rollout.n={config.rollout.n}",
        f"actor_rollout_ref.rollout.temperature={config.rollout.temperature}",
        f"actor_rollout_ref.rollout.top_p={config.rollout.top_p}",
        f"actor_rollout_ref.rollout.gpu_memory_utilization={config.rollout.gpu_memory_utilization}",
        f"actor_rollout_ref.rollout.tensor_model_parallel_size={config.rollout.tensor_model_parallel_size}",
        f"actor_rollout_ref.rollout.max_num_batched_tokens={config.rollout.max_num_batched_tokens}",
        # trainer
        f"trainer.project_name={_slug(job.name)}",
        f"trainer.experiment_name={job.id}",
        f"trainer.logger={_hydra_list(config.logger)}",
        f"trainer.n_gpus_per_node={n_gpus}",
        "trainer.nnodes=1",
        f"trainer.save_freq={config.save_freq}",
        f"trainer.test_freq={config.test_freq}",
        f"trainer.total_epochs={config.total_epochs}",
        f"trainer.val_before_train={config.val_before_train}",
        f"trainer.default_local_dir={output_dir}",
    ]

    if config.total_training_steps:
        overrides.append(f"trainer.total_training_steps={config.total_training_steps}")

    if config.algorithm in {TrainStage.GRPO, TrainStage.DAPO}:
        overrides.append(f"actor_rollout_ref.actor.clip_ratio_low={config.clip_ratio_low}")
        overrides.append(f"actor_rollout_ref.actor.clip_ratio_high={config.clip_ratio_high}")
    else:
        overrides.append(f"actor_rollout_ref.actor.clip_ratio={config.clip_ratio}")

    if config.use_kl_loss:
        overrides.append("actor_rollout_ref.ref.fsdp_config.param_offload=True")

    if is_dapo or config.filter_groups_enable:
        overrides += [
            "algorithm.filter_groups.enable=True",
            f"algorithm.filter_groups.metric={config.filter_groups_metric}",
            f"algorithm.filter_groups.max_num_gen_batches={config.filter_groups_max_num_gen_batches}",
        ]
        notes.append("已启用动态采样（filter_groups）：全对/全错的组会被丢弃并重采。")

    if config.enable_overlong_buffer:
        notes.append(
            "当前 verl 版本未把 overlong_buffer 接入配置系统，超长回复软惩罚已跳过；"
            "如需该能力请改用自定义 reward 函数。"
        )

    if config.custom_reward_function_path:
        overrides.append(f"custom_reward_function.path={config.custom_reward_function_path}")
        if config.custom_reward_function_name:
            overrides.append(f"custom_reward_function.name={config.custom_reward_function_name}")

    for key, value in config.extra_args.items():
        overrides.append(f"{key}={value}")

    python_bin = settings.python_for("verl")
    env = {
        "PYTHONPATH": str(root),
        "VLLM_ATTENTION_BACKEND": "FLASH_ATTN",
        "TOKENIZERS_PARALLELISM": "false",
        "RAY_TMPDIR": str(work_dir / "ray_tmp"),
        "HYDRA_FULL_ERROR": "1",
    }

    notes.insert(0, f"adv_estimator={estimator}, rollout n={config.rollout.n}, {n_gpus} GPU")
    if config.rollout.tensor_model_parallel_size > n_gpus:
        raise BuildError(
            f"rollout 张量并行度 ({config.rollout.tensor_model_parallel_size}) 不能大于分配的 GPU 数 ({n_gpus})"
        )

    return BuildResult(
        command=[python_bin, "-m", "verl.trainer.main_ppo", *overrides],
        cwd=str(root),
        env=env,
        files={"verl_overrides.txt": "\n".join(overrides) + "\n"},
        output_dir=str(output_dir),
        notes=notes,
        mkdirs=[str(work_dir / "ray_tmp")],
    )


def _slug(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in value.strip())
    return cleaned.strip("-") or "prt-rl"
