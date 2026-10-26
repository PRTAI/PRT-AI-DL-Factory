"""问题反馈接口：提交、查看；管理员启动托管修复、合并到 main。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from ..deps import AdminUser, CurrentUser
from ..models import Issue, IssueCreate
from ..services import issue_service

router = APIRouter(prefix="/issues", tags=["issues"])


@router.get("", response_model=list[Issue], summary="问题反馈列表（成员只看自己的）")
def list_issues(user: CurrentUser, status: str = Query(default="")) -> list[Issue]:
    return issue_service.list_issues(actor=user, status_filter=status)


@router.post("", response_model=Issue, status_code=201, summary="提交问题反馈")
def create_issue(payload: IssueCreate, user: CurrentUser) -> Issue:
    return issue_service.create_issue(payload, actor=user)


@router.get("/{issue_id}", response_model=Issue, summary="问题详情（含修复报告）")
def get_issue(issue_id: str, user: CurrentUser) -> Issue:
    return issue_service.get_issue(issue_id, actor=user)


@router.get("/{issue_id}/log", summary="托管修复日志")
def get_log(
    issue_id: str, user: CurrentUser, tail_bytes: int = Query(default=64_000, ge=1024, le=400_000),
) -> dict[str, Any]:
    return issue_service.agent_log(issue_id, actor=user, tail_bytes=tail_bytes)


@router.post("/{issue_id}/fix", response_model=Issue, summary="托管 Agent 修复（管理员）")
def start_fix(issue_id: str, user: AdminUser) -> Issue:
    return issue_service.start_fix(issue_id, actor=user)


@router.post("/{issue_id}/merge", response_model=Issue, summary="合并到 main、推送并重启（管理员）")
def merge_issue(issue_id: str, user: AdminUser) -> Issue:
    return issue_service.merge_issue(issue_id, actor=user)


@router.post("/{issue_id}/close", response_model=Issue, summary="关闭问题")
def close_issue(issue_id: str, user: CurrentUser) -> Issue:
    return issue_service.close_issue(issue_id, actor=user)
