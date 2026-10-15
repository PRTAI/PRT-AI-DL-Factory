"""模型部署：把本地模型用 vLLM 拉起成 OpenAI / Responses / Anthropic 接口服务。

评测本地训练产物时需要一个可访问的推理端点，所以部署是平台的一等公民：
它像任务一样有进程、日志和 GPU 占用，但**没有终止态**——它是常驻服务，
由用户显式停止。
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import Field

from .common import Base, new_id, utcnow


class DeploymentStatus(str, Enum):
    STARTING = "starting"   # 进程已拉起，模型还在加载
    SERVING = "serving"     # /v1/models 可访问
    FAILED = "failed"
    STOPPED = "stopped"

    @property
    def is_active(self) -> bool:
        return self in {DeploymentStatus.STARTING, DeploymentStatus.SERVING}


class DeploymentSource(str, Enum):
    MODEL = "model"        # 模型仓库里的模型
    JOB_OUTPUT = "job"     # 某个训练任务的产物目录
    PATH = "path"          # 直接给绝对路径


# 对外接口形式。vLLM ≥0.18 在同一端口原生提供 /v1/chat/completions、/v1/responses
# 和 Anthropic /v1/messages；这里选的是评测时 agent 走哪一套（codex 只认 Responses，
# claude-code 只认 Anthropic），平台在就绪后会确认对应路由真的存在。
DeploymentApiProtocol = Literal["openai", "responses", "anthropic"]


class VLLMConfig(Base):
    """vLLM OpenAI 兼容服务的启动参数（只放常用的，其余走 extra_args）。"""

    tensor_parallel_size: int = Field(default=1, ge=1, le=16)
    pipeline_parallel_size: int = Field(default=1, ge=1, le=8)
    gpu_memory_utilization: float = Field(default=0.9, gt=0.0, le=1.0)
    max_model_len: int | None = None
    max_num_seqs: int = Field(default=256, ge=1)
    dtype: str = "auto"  # auto / bfloat16 / float16
    quantization: str = ""  # awq / gptq / fp8 …
    trust_remote_code: bool = True
    enable_prefix_caching: bool = True
    enforce_eager: bool = False
    api_key: str = ""  # 留空表示不校验
    chat_template: str = ""
    # agent 评测要靠工具调用：填了就加 --enable-auto-tool-choice --tool-call-parser
    tool_call_parser: str = ""
    extra_args: dict[str, Any] = Field(default_factory=dict)


class Deployment(Base):
    id: str = Field(default_factory=lambda: new_id("dep"))
    name: str
    note: str = ""

    source: DeploymentSource = DeploymentSource.MODEL
    model_id: str = ""       # 模型仓库 id
    job_id: str = ""         # 来源训练任务
    model_path: str = ""     # 最终解析出来的绝对路径
    served_model_name: str = ""  # 对外暴露的模型名（评测时填这个）

    port: int = Field(default=8100, ge=1024, le=65535)
    host: str = "0.0.0.0"
    engine: str = "vllm"
    api_protocol: DeploymentApiProtocol = "openai"
    config: VLLMConfig = Field(default_factory=VLLMConfig)

    owner_id: str
    owner_email: str = ""
    project_id: str = ""

    status: DeploymentStatus = DeploymentStatus.STARTING
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    started_at: datetime | None = None
    stopped_at: datetime | None = None
    ready_at: datetime | None = None

    gpu_ids: list[int] = Field(default_factory=list)
    pid: int | None = None
    pgid: int | None = None
    exit_code: int | None = None
    work_dir: str = ""
    log_file: str = ""
    command: str = ""
    error: str = ""

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"


class DeploymentCreate(Base):
    name: str = Field(min_length=1, max_length=80)
    note: str = ""
    source: DeploymentSource = DeploymentSource.MODEL
    model_id: str = ""
    job_id: str = ""
    model_path: str = ""
    served_model_name: str = ""
    port: int | None = None
    # 0 表示不参与 GPU 记账（CPU 推理，或本机没有可见显卡）
    n_gpus: int = Field(default=1, ge=0, le=16)
    project_id: str = ""
    api_protocol: DeploymentApiProtocol = "openai"
    config: VLLMConfig = Field(default_factory=VLLMConfig)


class ExportStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class ExportTask(Base):
    """把 LoRA adapter 合并回底座，导出成可直接部署/分发的完整权重。"""

    id: str = Field(default_factory=lambda: new_id("exp"))
    name: str
    job_id: str = ""          # 来源训练任务
    base_model_path: str = ""
    adapter_path: str = ""    # 空表示全参训练，直接复制/转存
    template: str = "default"
    export_dir: str = ""
    export_size_gb: int = Field(default=5, ge=1, le=50)
    export_device: str = "cpu"  # cpu / auto
    export_legacy_format: bool = False

    owner_id: str
    owner_email: str = ""
    status: ExportStatus = ExportStatus.QUEUED
    created_at: datetime = Field(default_factory=utcnow)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    pid: int | None = None
    pgid: int | None = None
    exit_code: int | None = None
    log_file: str = ""
    command: str = ""
    error: str = ""
    output_bytes: int = 0


class ExportCreate(Base):
    job_id: str
    name: str = ""
    # 留空表示用任务的输出根目录（最终 adapter）；也可以指定某个 checkpoint-* 子目录
    adapter_path: str = ""
    export_size_gb: int = Field(default=5, ge=1, le=50)
    export_device: str = "cpu"
    export_legacy_format: bool = False
