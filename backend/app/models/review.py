"""样例审核：上传一批样例 + 需求文档，按文档拆出的质检项逐题静态验收。

审核与训练/评测一样必须归属项目，但不占 GPU、不进调度队列，也不需要管理员放行。

每次审核独享一个 16 位 uuid 的工作目录（``data/reviews/<workspace>/``），原始压缩包、
需求文档、解压出来的样例树、逐项结论和最终 docx 都留在里面，用户可以回看和下载。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import Field, model_validator

from .common import Base, utcnow
from .model_api import ReasoningEffort


def new_review_id() -> str:
    """``rev_`` + 16 位 hex —— 后半段同时是工作目录名，方便从目录反查记录。"""
    return f"rev_{uuid.uuid4().hex[:16]}"


class ReviewStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self in {ReviewStatus.SUCCEEDED, ReviewStatus.FAILED, ReviewStatus.CANCELLED}


class ReviewStage(str, Enum):
    """四个阶段，详情页按这个显示进度。"""

    PENDING = "pending"
    COLLECTING = "collecting"   # ① 解压 + 采证（纯代码）
    PLANNING = "planning"       # ② 主 agent 拆质检项
    CHECKING = "checking"       # ③ 每个质检项一个 subagent
    REPORTING = "reporting"     # ④ 汇总 + 生成 docx
    DONE = "done"


class ReviewVerdict(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    NEEDS_REVIEW = "needs_review"


class CheckStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    DROPPED = "dropped"  # quote 无法在规范文档里定位，已丢弃（防编造闸门）


class ReviewCheck(Base):
    """一个质检项 = 一个 subagent 的职责范围。全部字段都来自需求文档。"""

    code: str  # 文档自己的编号（F08 / M02…），没有才生成 C01…
    title: str
    source: str = ""        # 出处，例如 "§8.2.2"
    requirement: str = ""   # 写进报告的「要求」一行
    quote: str = ""         # 规范原文逐字摘录，平台会回查文档做校验
    how_to_judge: str = ""  # 给 subagent 的判定说明
    # 这一项需要哪些证据视图（见 review_service.EVIDENCE_VIEWS）；只发需要的，省 token
    views: list[str] = Field(default_factory=list)
    status: CheckStatus = CheckStatus.PENDING
    drop_reason: str = ""
    n_fail: int = 0
    n_pass: int = 0
    n_needs_review: int = 0
    error: str = ""


class ReviewFinding(Base):
    """某个质检项对某一道题的结论。"""

    check_code: str
    task_id: str        # 题号
    rel_path: str = ""  # 源数据相对位置（含 zip 的 `!/`）
    verdict: ReviewVerdict = ReviewVerdict.PASS
    problems: list[str] = Field(default_factory=list)


class ReviewStats(Base):
    n_tasks: int = 0
    n_pass: int = 0
    n_fail: int = 0
    n_needs_review: int = 0
    n_checks: int = 0
    n_checks_done: int = 0
    n_findings: int = 0

    @property
    def pass_rate(self) -> float:
        """通过 / 受检。needs_review 既不算通过也不算不合格，所以不能用「1 - 不合格率」。"""
        return (self.n_pass / self.n_tasks) if self.n_tasks else 0.0


class ReviewTask(Base):
    id: str = Field(default_factory=new_review_id)
    workspace: str = ""  # 16 位目录名，默认取 id 的后半段

    name: str                    # 批次名，进报告标题
    supplier: str = ""           # 供应商，进报告副标题
    spec_title: str = ""         # 依据规范名，进「一、说明」
    prompt: str = ""             # 用户自定义提示词
    model_api_id: str = ""
    model_label: str = ""        # 提交时的模型显示名（配置可能后来被改名/删除）
    agent: Literal["", "claude-code", "codex"] = ""  # 空值仅用于读取历史直接 LLM 记录
    reasoning_effort: ReasoningEffort = ""

    owner_id: str
    owner_email: str = ""
    project_id: str = ""

    status: ReviewStatus = ReviewStatus.QUEUED
    stage: ReviewStage = ReviewStage.PENDING
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    started_at: datetime | None = None
    finished_at: datetime | None = None

    work_dir: str = ""
    archive_name: str = ""
    archive_bytes: int = 0
    doc_names: list[str] = Field(default_factory=list)
    report_path: str = ""

    stats: ReviewStats = Field(default_factory=ReviewStats)
    checks: list[ReviewCheck] = Field(default_factory=list)
    # 平台替用户做的取舍（丢弃的质检项、被忽略的返回、截断说明…），详情页原样展示
    notes: list[str] = Field(default_factory=list)
    error: str = ""
    usage: dict[str, Any] = Field(default_factory=dict)  # tokens / 调用次数

    @model_validator(mode="after")
    def _default_workspace(self) -> "ReviewTask":
        if not self.workspace:
            self.workspace = self.id.removeprefix("rev_")
        return self


class ReviewCreate(Base):
    """multipart 表单里的非文件字段。"""

    name: str = Field(min_length=1, max_length=120)
    supplier: str = Field(default="", max_length=120)
    spec_title: str = Field(default="", max_length=200)
    prompt: str = Field(default="", max_length=20_000)
    model_api_id: str = ""
    project_id: str = Field(min_length=1)
    agent: Literal["claude-code", "codex"] = "claude-code"
    reasoning_effort: ReasoningEffort = ""
