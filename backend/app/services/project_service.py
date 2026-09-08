"""项目：任务的归属单位。

可见性刻意做成**全员共享**——列表、搜索、任务详情所有人都能看，
团队里谁在跑什么应该透明。只有改名/归档/删除/转移负责人受限于负责人与管理员。
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, status

from ..models import (
    Job,
    JobStatus,
    Project,
    ProjectCreate,
    ProjectStats,
    ProjectUpdate,
    User,
    slugify,
    utcnow,
)
from ..store import store
from . import audit_service


def list_projects(*, query: str = "", include_archived: bool = False) -> list[Project]:
    def keep(project: Project) -> bool:
        if project.archived and not include_archived:
            return False
        if query:
            haystack = f"{project.name} {project.slug} {project.description} {project.owner_email} {' '.join(project.tags)}"
            if query.lower() not in haystack.lower():
                return False
        return True

    items = store.projects.find(keep)
    items.sort(key=lambda p: p.updated_at, reverse=True)
    return items


def get_project(project_id: str) -> Project:
    project = store.projects.get(project_id)
    if project is None:
        # 也允许用 slug 访问，方便直接分享链接
        project = store.projects.first(lambda p: p.slug == project_id)
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="项目不存在")
    return project


def stats_for(project_id: str) -> ProjectStats:
    jobs = store.jobs.find(lambda j: j.project_id == project_id)
    stats = ProjectStats(total_jobs=len(jobs))
    members: set[str] = set()
    for job in jobs:
        members.add(job.owner_email)
        if job.status == JobStatus.RUNNING:
            stats.running += 1
        elif job.status == JobStatus.PENDING_REVIEW:
            stats.pending_review += 1
        elif job.status == JobStatus.SUCCEEDED:
            stats.succeeded += 1
        elif job.status == JobStatus.FAILED:
            stats.failed += 1
        if stats.last_activity_at is None or job.updated_at > stats.last_activity_at:
            stats.last_activity_at = job.updated_at
    stats.members = sorted(m for m in members if m)
    return stats


def list_with_stats(*, query: str = "", include_archived: bool = False) -> list[dict[str, Any]]:
    return [
        {"project": project, "stats": stats_for(project.id)}
        for project in list_projects(query=query, include_archived=include_archived)
    ]


def create_project(payload: ProjectCreate, *, owner: User) -> Project:
    name = payload.name.strip()
    slug = slugify(name)
    if store.projects.first(lambda p: p.slug == slug and not p.archived):
        raise HTTPException(status.HTTP_409_CONFLICT, detail=f"已存在同名项目：{name}")

    project = Project(
        name=name,
        slug=slug,
        description=payload.description,
        tags=payload.tags,
        owner_id=owner.id,
        owner_email=owner.email,
    )
    store.projects.put(project)
    audit_service.record(
        "project.create",
        actor=owner,
        target_type="project",
        target_id=project.id,
        summary=f"创建项目「{project.name}」",
    )
    return project


def update_project(project_id: str, payload: ProjectUpdate, *, actor: User) -> Project:
    project = get_project(project_id)
    _assert_can_manage(project, actor)

    if payload.owner_id and payload.owner_id != project.owner_id:
        new_owner = store.users.get(payload.owner_id)
        if new_owner is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="新负责人不存在")

    def mutate(item: Project) -> None:
        data = payload.model_dump(exclude_unset=True, exclude_none=True)
        if "name" in data:
            item.name = data["name"].strip()
            item.slug = slugify(item.name)
        for key in ("description", "tags", "archived"):
            if key in data:
                setattr(item, key, data[key])
        if data.get("owner_id"):
            owner = store.users.get(data["owner_id"])
            if owner:
                item.owner_id = owner.id
                item.owner_email = owner.email
        item.updated_at = utcnow()

    updated = store.projects.update(project.id, mutate)
    assert updated is not None
    audit_service.record(
        "project.update",
        actor=actor,
        target_type="project",
        target_id=project.id,
        summary=f"更新项目「{updated.name}」",
        detail=payload.model_dump(exclude_unset=True),
    )
    return updated


def delete_project(project_id: str, *, actor: User) -> None:
    project = get_project(project_id)
    _assert_can_manage(project, actor)
    used = store.jobs.count(lambda j: j.project_id == project.id)
    if used:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=f"项目下还有 {used} 个任务，无法删除；可以改为归档",
        )
    store.projects.delete(project.id)
    audit_service.record(
        "project.delete",
        actor=actor,
        target_type="project",
        target_id=project.id,
        summary=f"删除项目「{project.name}」",
    )


def touch(project_id: str) -> None:
    """任务有动静时把项目的更新时间顶上去，列表按活跃度排序才有意义。"""
    if not project_id:
        return
    store.projects.update(project_id, lambda item: setattr(item, "updated_at", utcnow()))


def assert_usable(project_id: str) -> Project:
    """提交任务时校验项目可用。"""
    if not project_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="请先选择一个项目")
    project = get_project(project_id)
    if project.archived:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=f"项目「{project.name}」已归档，无法提交新任务")
    return project


def recent_jobs(project_id: str, limit: int = 20) -> list[Job]:
    jobs = store.jobs.find(lambda j: j.project_id == project_id)
    jobs.sort(key=lambda j: j.updated_at, reverse=True)
    return jobs[:limit]


DEFAULT_PROJECT_NAME = "默认项目"


def adopt_orphan_jobs() -> int:
    """把「项目」上线之前提交的任务收进一个默认项目。

    否则它们的 ``project_id`` 是空串，在任何项目视图里都看不到，
    只能在「全部项目」里出现——等于历史数据被藏起来了。
    """
    orphans = store.jobs.find(lambda j: not j.project_id)
    if not orphans:
        return 0

    project = store.projects.first(lambda p: p.name == DEFAULT_PROJECT_NAME)
    if project is None:
        # 归到最早那个任务的负责人名下，比挂在 admin 名下更贴近事实
        oldest = min(orphans, key=lambda j: j.created_at)
        project = Project(
            name=DEFAULT_PROJECT_NAME,
            slug=slugify(DEFAULT_PROJECT_NAME),
            description="「项目」功能上线前提交的历史任务",
            owner_id=oldest.owner_id,
            owner_email=oldest.owner_email,
        )
        store.projects.put(project)

    for job in orphans:
        store.jobs.update(job.id, lambda item: setattr(item, "project_id", project.id))
    return len(orphans)


def _assert_can_manage(project: Project, actor: User) -> None:
    if actor.is_admin or project.owner_id == actor.id:
        return
    raise HTTPException(status.HTTP_403_FORBIDDEN, detail="只有项目负责人或管理员可以修改该项目")
