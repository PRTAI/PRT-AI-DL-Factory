"""项目：任务的归属单位。列表/搜索全员共享，编辑归负责人与管理员。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from ..deps import CurrentUser
from ..models import (
    Job,
    JobType,
    Project,
    ProjectCreate,
    ProjectUpdate,
    ProjectWithStats,
    TeamMember,
)
from ..services import project_service

router = APIRouter(prefix="/projects", tags=["projects"])


@router.get("", response_model=list[ProjectWithStats], summary="项目列表（全员可见）")
def list_projects(
    user: CurrentUser,
    q: str = Query(default="", description="项目名/描述/标签，以及项目内的任务名/负责人"),
    owner_id: str = Query(default="", description="只看某个负责人参与的项目"),
    job_type: JobType | None = Query(default=None, description="只看含有该类型任务的项目"),
    include_archived: bool = False,
) -> list[dict[str, Any]]:
    del user  # 登录即可查看，团队内共享
    return project_service.list_with_stats(
        query=q, owner_id=owner_id, job_type=job_type, include_archived=include_archived
    )


@router.get("/members", response_model=list[TeamMember], summary="团队成员（筛选用）")
def members(user: CurrentUser) -> list[dict[str, Any]]:
    del user
    return project_service.members()


@router.post("", response_model=Project, status_code=201, summary="创建项目")
def create_project(payload: ProjectCreate, user: CurrentUser) -> Project:
    return project_service.create_project(payload, owner=user)


@router.get("/{project_id}", response_model=ProjectWithStats, summary="项目详情")
def get_project(project_id: str, user: CurrentUser) -> dict[str, Any]:
    del user
    project = project_service.get_project(project_id)
    return {"project": project, "stats": project_service.stats_for(project.id)}


@router.get("/{project_id}/jobs", response_model=list[Job], summary="项目下的最近任务")
def project_jobs(project_id: str, user: CurrentUser, limit: int = Query(default=20, le=200)) -> list[Job]:
    del user
    project = project_service.get_project(project_id)
    return project_service.recent_jobs(project.id, limit=limit)


@router.patch("/{project_id}", response_model=Project, summary="更新项目（负责人或管理员）")
def update_project(project_id: str, payload: ProjectUpdate, user: CurrentUser) -> Project:
    return project_service.update_project(project_id, payload, actor=user)


@router.delete("/{project_id}", status_code=204, summary="删除项目（需先清空任务）")
def delete_project(project_id: str, user: CurrentUser) -> None:
    project_service.delete_project(project_id, actor=user)
