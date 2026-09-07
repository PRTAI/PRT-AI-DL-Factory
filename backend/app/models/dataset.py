"""Datasets uploaded as zip archives and registered into the platform."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field

from .common import Base, DatasetKind, DatasetStatus, new_id, utcnow


class DatasetFileInfo(Base):
    path: str  # relative to the dataset root
    size_bytes: int
    records: int | None = None
    role: str = "data"  # data | tasks | meta


class DatasetStats(Base):
    n_records: int = 0
    n_files: int = 0
    n_tasks: int = 0  # harbor task trees
    total_bytes: int = 0
    columns: list[str] = Field(default_factory=list)
    sample: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class Dataset(Base):
    id: str = Field(default_factory=lambda: new_id("ds"))
    name: str
    kind: DatasetKind
    description: str = ""
    status: DatasetStatus = DatasetStatus.UPLOADING
    owner_id: str
    owner_email: str = ""
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    root_path: str = ""  # absolute path of the extracted dataset
    archive_name: str = ""
    archive_bytes: int = 0

    stats: DatasetStats = Field(default_factory=DatasetStats)
    files: list[DatasetFileInfo] = Field(default_factory=list)
    error: str = ""

    # LlamaFactory needs a `dataset_info.json` entry name per split; recorded so
    # job builders can reference the dataset without re-deriving it.
    lf_dataset_names: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)


class DatasetCreate(Base):
    name: str
    kind: DatasetKind
    description: str = ""
    tags: list[str] = Field(default_factory=list)


class DatasetUpdate(Base):
    name: str | None = None
    description: str | None = None
    tags: list[str] | None = None
