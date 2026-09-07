"""Shared primitives: id generation, timestamps, enums used across domains."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    """Short, sortable-ish, human-greppable identifier (e.g. ``job_9f2c1a0b``)."""
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class Base(BaseModel):
    model_config = ConfigDict(
        populate_by_name=True,
        use_enum_values=False,
        ser_json_timedelta="float",
    )


class Role(str, Enum):
    ADMIN = "admin"
    USER = "user"


class JobType(str, Enum):
    SFT = "sft"
    RL = "rl"
    VERIFY = "verify"


class Framework(str, Enum):
    LLAMAFACTORY = "llamafactory"
    VERL = "verl"
    TRL = "trl"
    HARBOR = "harbor"


class TrainStage(str, Enum):
    """Post-training algorithm requested by the user."""

    SFT = "sft"
    DPO = "dpo"
    KTO = "kto"
    ORPO = "orpo"
    SIMPO = "simpo"
    PPO = "ppo"
    GRPO = "grpo"
    DAPO = "dapo"
    GSPO = "gspo"
    RLOO = "rloo"
    REINFORCE_PLUS_PLUS = "reinforce_plus_plus"
    REMAX = "remax"


class JobStatus(str, Enum):
    DRAFT = "draft"
    PENDING_REVIEW = "pending_review"
    REJECTED = "rejected"
    QUEUED = "queued"
    PREPARING = "preparing"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self in {
            JobStatus.SUCCEEDED,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
            JobStatus.REJECTED,
        }

    @property
    def is_active(self) -> bool:
        return self in {JobStatus.QUEUED, JobStatus.PREPARING, JobStatus.RUNNING}


class DatasetKind(str, Enum):
    """Upload flavours the platform knows how to validate and register."""

    SFT_ALPACA = "sft_alpaca"
    SFT_SHAREGPT = "sft_sharegpt"
    DPO_PAIRS = "dpo_pairs"
    KTO_PAIRS = "kto_pairs"
    HARBOR_TASKS = "harbor_tasks"
    VERL_PARQUET = "verl_parquet"
    RAW = "raw"


class DatasetStatus(str, Enum):
    UPLOADING = "uploading"
    EXTRACTING = "extracting"
    VALIDATING = "validating"
    READY = "ready"
    INVALID = "invalid"
    DELETED = "deleted"


class DownloadStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Page(Base):
    """Envelope for paginated list endpoints."""

    items: list[Any]
    total: int
    page: int
    page_size: int
