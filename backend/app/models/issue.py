"""问题反馈：用户提交问题，管理员可交给本机 Claude Code 在隔离副本里修复，再审批合并。

状态机：``open →（托管修复）fixing → pending_merge →（管理员合并）merging → merged``；
修复失败为 ``failed``（可再次修复），不处理直接 ``closed``。
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import Field

from .common import Base, new_id, utcnow


class IssueStatus(str, Enum):
    OPEN = "open"
    FIXING = "fixing"
    PENDING_MERGE = "pending_merge"  # 修复完成，申请合并到 main
    MERGING = "merging"
    MERGED = "merged"
    FAILED = "failed"
    CLOSED = "closed"

    @property
    def can_fix(self) -> bool:
        return self in {IssueStatus.OPEN, IssueStatus.FAILED, IssueStatus.PENDING_MERGE}


class Issue(Base):
    id: str = Field(default_factory=lambda: new_id("iss"))
    title: str
    description: str = ""
    page: str = ""  # 反馈时所在页面，帮助 Agent 定位

    reporter_id: str
    reporter_email: str = ""

    status: IssueStatus = IssueStatus.OPEN
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    # ---- 托管修复 ----
    fix_requested_by: str = ""
    fix_started_at: datetime | None = None
    fix_finished_at: datetime | None = None
    workspace: str = ""     # 仓库副本（不在平台运行目录里）
    base_commit: str = ""   # 复制时 main 的提交
    branch: str = ""
    head_commit: str = ""
    pid: int | None = None
    report: str = ""        # Agent 写的简短修复报告（Markdown）
    diff_stat: str = ""
    error: str = ""

    # ---- 合并 ----
    merged_by: str = ""
    merged_github: str = ""  # 合并时使用的 GitHub 账号；空 = 本机 git 身份
    merged_at: datetime | None = None
    merge_commit: str = ""
    restart_scheduled: bool = False


class IssueCreate(Base):
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=20_000)
    page: str = Field(default="", max_length=500)
