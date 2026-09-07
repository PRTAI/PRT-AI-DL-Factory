"""Audit trail and in-app notifications."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field

from .common import Base, new_id, utcnow


class AuditEvent(Base):
    id: str = Field(default_factory=lambda: new_id("evt"))
    at: datetime = Field(default_factory=utcnow)
    actor_id: str = "system"
    actor_email: str = "system"
    action: str  # e.g. "job.approve", "user.create", "dataset.upload"
    target_type: str = ""
    target_id: str = ""
    summary: str = ""
    ip: str = ""
    detail: dict[str, Any] = Field(default_factory=dict)


class Notification(Base):
    id: str = Field(default_factory=lambda: new_id("ntf"))
    at: datetime = Field(default_factory=utcnow)
    user_id: str  # "*" broadcasts to every admin
    level: str = "info"  # info | success | warning | error
    title: str
    body: str = ""
    link: str = ""
    read: bool = False


class SystemSettings(Base):
    """Admin-tunable knobs persisted in the DB (override the env defaults)."""

    require_approval: bool = True
    max_concurrent_jobs: int = 2
    default_max_gpus_per_job: int = 8
    allow_self_service_verify: bool = False
    banner: str = ""
    updated_at: datetime = Field(default_factory=utcnow)
    updated_by: str = ""
