"""项目：训练/评测任务的归属单位（类似 wandb / swanlab 的 project）。

任务必须属于某个项目；项目和任务都有负责人，但**列表与搜索对全员共享**——
团队里谁在跑什么应该是透明的，只有修改/归档才受负责人与管理员限制。
"""

from __future__ import annotations

import re
from datetime import datetime

from pydantic import Field, field_validator

from .common import Base, new_id, utcnow

# \w 在 re.UNICODE 下包含中日韩文字，所以「模型对齐」不会被清成空串——
# 否则所有纯中文项目名都会得到同一个 slug 而互相冲突。
_SLUG_CLEAN = re.compile(r"[^\w\-]+", re.UNICODE)


def slugify(value: str) -> str:
    slug = _SLUG_CLEAN.sub("-", value.strip().lower()).strip("-_")
    return slug or "project"


class Project(Base):
    id: str = Field(default_factory=lambda: new_id("prj"))
    name: str
    slug: str = ""
    description: str = ""
    tags: list[str] = Field(default_factory=list)

    owner_id: str
    owner_email: str = ""

    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    archived: bool = False

    @field_validator("slug", mode="before")
    @classmethod
    def _fill_slug(cls, value: str | None, info) -> str:
        if value:
            return slugify(value)
        name = (info.data or {}).get("name", "")
        return slugify(name)


class ProjectCreate(Base):
    name: str = Field(min_length=1, max_length=80)
    description: str = ""
    tags: list[str] = Field(default_factory=list)


class ProjectUpdate(Base):
    name: str | None = None
    description: str | None = None
    tags: list[str] | None = None
    archived: bool | None = None
    owner_id: str | None = None


class ProjectStats(Base):
    """项目卡片上展示的聚合数字。"""

    total_jobs: int = 0
    running: int = 0
    pending_review: int = 0
    succeeded: int = 0
    failed: int = 0
    last_activity_at: datetime | None = None
    members: list[str] = Field(default_factory=list)


class ProjectWithStats(Base):
    project: Project
    stats: ProjectStats
