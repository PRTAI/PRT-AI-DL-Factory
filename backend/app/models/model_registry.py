"""Local model zoo entries plus ModelScope download tracking."""

from __future__ import annotations

from datetime import datetime

from pydantic import Field

from .common import Base, DownloadStatus, new_id, utcnow


class ModelEntry(Base):
    """A model directory discovered under ``settings.models_root``."""

    id: str  # "<vendor>/<name>" — stable, human readable
    vendor: str
    name: str
    path: str
    size_bytes: int = 0
    model_type: str = ""
    architectures: list[str] = Field(default_factory=list)
    num_parameters_b: float | None = None
    hidden_size: int | None = None
    num_layers: int | None = None
    max_position_embeddings: int | None = None
    torch_dtype: str = ""
    has_tokenizer: bool = False
    is_multimodal: bool = False
    modified_at: datetime | None = None
    # Framework hints resolved from resources/framework_support.json
    llamafactory_template: str | None = None
    supported_frameworks: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class ModelDownload(Base):
    id: str = Field(default_factory=lambda: new_id("dl"))
    model_id: str  # "{vendor}/{name}" on ModelScope
    source: str = "modelscope"
    revision: str = ""
    target_path: str = ""
    status: DownloadStatus = DownloadStatus.QUEUED
    requested_by: str = ""
    requested_by_email: str = ""
    created_at: datetime = Field(default_factory=utcnow)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    pid: int | None = None
    pgid: int | None = None
    exit_code: int | None = None
    log_file: str = ""
    error: str = ""
    downloaded_bytes: int = 0


class ModelDownloadRequest(Base):
    model_id: str = Field(description="ModelScope id, e.g. Qwen/Qwen3-8B")
    revision: str = ""
    source: str = "modelscope"
