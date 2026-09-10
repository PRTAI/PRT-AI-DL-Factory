"""Job domain model: SFT / RL training and Harbor verification runs.

A job moves through ``pending_review -> queued -> running -> succeeded|failed``.
The per-type configuration blocks are validated up-front so the command
builders never have to guess at missing fields.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field, model_validator

from .common import (
    Base,
    Framework,
    JobStatus,
    JobType,
    TrainStage,
    new_id,
    utcnow,
)

# --------------------------------------------------------------------------- #
# Resources
# --------------------------------------------------------------------------- #


class ResourceSpec(Base):
    n_gpus: int = Field(default=1, ge=0, le=64)
    gpu_ids: list[int] = Field(default_factory=list)  # assigned by the scheduler
    cpu_cores: int | None = None
    memory_gb: int | None = None
    priority: int = Field(default=0, ge=-10, le=10)
    env: dict[str, str] = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# SFT (LlamaFactory)
# --------------------------------------------------------------------------- #


class LoraSpec(Base):
    rank: int = 8
    alpha: int = 16
    dropout: float = 0.05
    target: str = "all"
    create_new_adapter: bool = False


class SFTConfig(Base):
    """LlamaFactory driven supervised / preference fine-tuning."""

    framework: Literal[Framework.LLAMAFACTORY] = Framework.LLAMAFACTORY
    stage: TrainStage = TrainStage.SFT

    model_path: str
    model_id: str = ""
    template: str = "default"
    trust_remote_code: bool = True

    dataset_ids: list[str] = Field(default_factory=list)
    eval_dataset_ids: list[str] = Field(default_factory=list)
    val_size: float = Field(default=0.0, ge=0.0, lt=1.0)

    finetuning_type: Literal["lora", "full", "freeze"] = "lora"
    lora: LoraSpec = Field(default_factory=LoraSpec)
    freeze_trainable_layers: int = 2

    # core hyper-parameters
    cutoff_len: int = 2048
    max_samples: int | None = None
    learning_rate: float = 1.0e-4
    num_train_epochs: float = 3.0
    max_steps: int | None = None
    per_device_train_batch_size: int = 1
    gradient_accumulation_steps: int = 8
    lr_scheduler_type: str = "cosine"
    warmup_ratio: float = 0.1
    weight_decay: float = 0.0
    max_grad_norm: float = 1.0
    logging_steps: int = 10
    save_steps: int = 500
    eval_steps: int | None = None
    save_only_model: bool = False
    resume_from_checkpoint: str | None = None

    # precision / memory
    compute_type: Literal["bf16", "fp16", "fp32", "pure_bf16"] = "bf16"
    quantization_bit: int | None = None
    # 取值对应 LLaMA-Factory 的 AttentionFunction 枚举：auto 交给框架自己探测，
    # fa2/fa3 需要装 flash-attn（fa3 只在 Hopper 及更新的卡上可用），
    # sdpa 是 PyTorch 自带的高效实现，没装 flash-attn 也能提速。
    flash_attn: Literal["auto", "disabled", "sdpa", "fa2", "fa3"] = "auto"
    gradient_checkpointing: bool = True
    deepspeed_stage: Literal["none", "zero2", "zero3", "zero3_offload"] = "none"
    liger_kernel: bool = False

    # preference-tuning knobs (dpo / kto / orpo / simpo)
    pref_beta: float = 0.1
    pref_loss: Literal["sigmoid", "orpo", "simpo", "hinge", "ipo", "kto_pair"] = "sigmoid"
    pref_ftx: float = 0.0

    # reporting
    report_to: Literal["none", "wandb", "tensorboard", "swanlab", "mlflow"] = "none"
    plot_loss: bool = True

    extra_args: dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# RL (verl / trl)
# --------------------------------------------------------------------------- #


class RolloutSpec(Base):
    backend: Literal["vllm", "sglang", "hf"] = "vllm"
    n: int = Field(default=8, ge=1, description="rollouts (group size) per prompt")
    temperature: float = 1.0
    top_p: float = 1.0
    top_k: int = -1
    gpu_memory_utilization: float = 0.6
    tensor_model_parallel_size: int = 1
    max_num_batched_tokens: int = 8192


class HarborEnvSpec(Base):
    """Wiring for Harbor-task-backed RL (TRL ``HarborSpec`` / verl agent loop)."""

    enabled: bool = False
    harness: str = "bash"  # bash | jupyter | terminal_notes | pkg.mod:Class
    environment_type: str = "docker"  # docker | e2b | daytona | modal | ...
    num_tasks: int | None = 64
    max_tool_calling_iterations: int = 25
    include_metadata: bool = True


class RLConfig(Base):
    framework: Literal[Framework.VERL, Framework.TRL] = Framework.VERL
    algorithm: TrainStage = TrainStage.GRPO

    model_path: str
    model_id: str = ""
    dataset_id: str = ""
    eval_dataset_id: str = ""

    # data shaping
    max_prompt_length: int = 1024
    max_response_length: int = 2048
    train_batch_size: int = 64
    mini_batch_size: int = 32
    micro_batch_size_per_gpu: int = 1
    prompt_key: str = "prompt"
    truncation: Literal["error", "left", "right", "middle"] = "error"
    filter_overlong_prompts: bool = True

    # optimisation
    learning_rate: float = 1.0e-6
    total_epochs: int = 1
    total_training_steps: int | None = None
    warmup_ratio: float = 0.0
    weight_decay: float = 0.01
    clip_ratio: float = 0.2
    clip_ratio_low: float = 0.2
    clip_ratio_high: float = 0.28
    loss_agg_mode: Literal["token-mean", "seq-mean-token-sum", "seq-mean-token-mean"] = "token-mean"
    entropy_coeff: float = 0.0
    use_kl_loss: bool = False
    kl_loss_coef: float = 0.001
    use_kl_in_reward: bool = False
    norm_adv_by_std_in_grpo: bool = True

    # DAPO specifics
    filter_groups_enable: bool = False
    filter_groups_metric: str = "acc"
    filter_groups_max_num_gen_batches: int = 10
    enable_overlong_buffer: bool = False
    overlong_buffer_len: int = 512
    overlong_penalty_factor: float = 1.0

    rollout: RolloutSpec = Field(default_factory=RolloutSpec)
    harbor: HarborEnvSpec = Field(default_factory=HarborEnvSpec)

    # engine / parallelism
    # auto（默认）：装了 flash-attn 就用 flash_attention_2，否则回落到 sdpa ——
    # 直接把 flash_attention_2 当默认值会在没装 flash-attn 的机器上，
    # 白排队几分钟才在加载模型时报错。default 表示平台不下发这个参数。
    attn_implementation: Literal["auto", "flash_attention_2", "sdpa", "eager", "default"] = "auto"
    strategy: Literal["fsdp", "fsdp2", "megatron"] = "fsdp2"
    param_offload: bool = False
    optimizer_offload: bool = False
    use_dynamic_bsz: bool = True
    gradient_checkpointing: bool = True

    # reward
    reward_fn_key: str = "data_source"
    custom_reward_function_path: str = ""
    custom_reward_function_name: str = ""

    save_freq: int = 20
    test_freq: int = 20
    val_before_train: bool = False
    logger: list[str] = Field(default_factory=lambda: ["console"])

    extra_args: dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Verify (Harbor)
# --------------------------------------------------------------------------- #


class VerifyConfig(Base):
    framework: Literal[Framework.HARBOR] = Framework.HARBOR

    dataset_id: str = ""
    tasks_path: str = ""  # absolute path override (admin / advanced)
    registry_dataset: str = ""  # e.g. "terminal-bench@2.0"

    agent: str = "oracle"
    # 二选一：选平台维护的模型配置（推荐，密钥与 base_url 由后端按 Harbor 的标准
    # 环境变量注入），或者手填 litellm 风格的模型名 + 自己在 agent_env 里给密钥。
    model_api_id: str = ""
    model: str = ""
    # 思考模式：留空表示不下发，交给 agent 自己的默认值。各 agent 接受的集合不同，
    # builder 会收敛到该 agent 支持的最近一档并在 notes 里说明。
    reasoning_effort: Literal["", "low", "medium", "high", "xhigh", "max"] = ""
    agent_import_path: str = ""

    n_concurrent: int = Field(default=4, ge=1, le=256)
    n_attempts: int = Field(default=1, ge=1, le=64, description="pass@k -> harbor -k")
    max_retries: int = Field(default=0, ge=0, le=10)
    environment_type: str = "docker"

    task_names: list[str] = Field(default_factory=list)  # --include-task-name
    exclude_task_names: list[str] = Field(default_factory=list)  # --exclude-task-name
    n_tasks: int | None = None

    agent_env: dict[str, str] = Field(default_factory=dict)  # --agent-env
    agent_kwargs: dict[str, str] = Field(default_factory=dict)  # --agent-kwarg
    verifier_env: dict[str, str] = Field(default_factory=dict)  # --verifier-env
    environment_kwargs: dict[str, str] = Field(default_factory=dict)  # --environment-kwarg
    verifier_kwargs: dict[str, str] = Field(default_factory=dict)  # --verifier-kwarg

    # Emitted as --override-cpus / --override-memory-mb / --override-gpus
    environment_cpus: int | None = None
    environment_memory_mb: int | None = None
    environment_gpus: int | None = None

    timeout_multiplier: float = 1.0
    agent_timeout_multiplier: float | None = None
    verifier_timeout_multiplier: float | None = None

    disable_verification: bool = False
    force_build: bool = False  # --force-build
    export_traces: bool = False
    sharegpt: bool = False  # --export-sharegpt
    # Harbor 的进度条是 rich Live，在非终端里一个字都不会打；--debug 把它的控制台
    # handler 调到 DEBUG，每个 trial 装了什么、跑了什么命令、为什么失败才会进日志。
    verbose_log: bool = True

    extra_args: dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Runtime / review / metrics
# --------------------------------------------------------------------------- #


class JobReview(Base):
    required: bool = True
    reviewer_id: str | None = None
    reviewer_email: str | None = None
    reviewed_at: datetime | None = None
    comment: str = ""


class JobRuntime(Base):
    work_dir: str = ""
    output_dir: str = ""
    log_file: str = ""
    script_path: str = ""
    command: str = ""
    pid: int | None = None
    pgid: int | None = None
    exit_code: int | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    cancel_requested: bool = False
    host: str = ""
    error: str = ""

    @property
    def duration_seconds(self) -> float | None:
        if not self.started_at:
            return None
        end = self.finished_at or utcnow()
        return (end - self.started_at).total_seconds()


class JobProgress(Base):
    percent: float = 0.0
    current_step: int = 0
    total_steps: int = 0
    current_epoch: float = 0.0
    total_epochs: float = 0.0
    eta_seconds: float | None = None
    throughput: float | None = None
    message: str = ""
    # verification-specific
    completed_trials: int = 0
    total_trials: int = 0
    # 各阶段的 trial 数：pending / setup_environment / running_agent / running_verifier / success / failed
    trial_phases: dict[str, int] = Field(default_factory=dict)


class JobMetricsSummary(Base):
    """Small, DB-resident rollup. Full curves live in ``<job>/metrics.json``."""

    last_loss: float | None = None
    best_loss: float | None = None
    last_reward: float | None = None
    best_reward: float | None = None
    learning_rate: float | None = None
    grad_norm: float | None = None
    accuracy: float | None = None
    pass_at_k: dict[str, float] = Field(default_factory=dict)
    n_trials: int | None = None
    n_errors: int | None = None
    resolved_rate: float | None = None
    cost_usd: float | None = None
    tokens: dict[str, int] = Field(default_factory=dict)
    updated_at: datetime | None = None


# --------------------------------------------------------------------------- #
# Job
# --------------------------------------------------------------------------- #


class Job(Base):
    id: str = Field(default_factory=lambda: new_id("job"))
    name: str
    note: str = ""
    type: JobType
    framework: Framework
    status: JobStatus = JobStatus.PENDING_REVIEW

    owner_id: str
    owner_email: str = ""
    project_id: str = ""  # 任务必须归属一个项目
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    queued_at: datetime | None = None
    tags: list[str] = Field(default_factory=list)

    resources: ResourceSpec = Field(default_factory=ResourceSpec)
    review: JobReview = Field(default_factory=JobReview)
    runtime: JobRuntime = Field(default_factory=JobRuntime)
    progress: JobProgress = Field(default_factory=JobProgress)
    metrics: JobMetricsSummary = Field(default_factory=JobMetricsSummary)

    sft: SFTConfig | None = None
    rl: RLConfig | None = None
    verify: VerifyConfig | None = None

    # Chained lineage: a verify job spawned from a finished training run.
    parent_job_id: str | None = None

    @model_validator(mode="after")
    def _check_config_present(self) -> "Job":
        expected = {
            JobType.SFT: self.sft,
            JobType.RL: self.rl,
            JobType.VERIFY: self.verify,
        }[self.type]
        if expected is None:
            raise ValueError(f"job of type {self.type.value} requires its config block")
        return self

    @property
    def config_block(self) -> SFTConfig | RLConfig | VerifyConfig:
        return self.sft or self.rl or self.verify  # type: ignore[return-value]


class JobCreate(Base):
    name: str = Field(min_length=1, max_length=120)
    note: str = ""
    type: JobType
    project_id: str = ""
    tags: list[str] = Field(default_factory=list)
    resources: ResourceSpec = Field(default_factory=ResourceSpec)
    sft: SFTConfig | None = None
    rl: RLConfig | None = None
    verify: VerifyConfig | None = None
    parent_job_id: str | None = None


class JobReviewRequest(Base):
    approve: bool
    comment: str = ""


class JobActionResponse(Base):
    ok: bool = True
    job: Job
    message: str = ""


class JobFilter(Base):
    status: list[JobStatus] | None = None
    type: list[JobType] | None = None
    owner_id: str | None = None
    query: str | None = None
