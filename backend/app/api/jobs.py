"""Job submission, review, control and inspection."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Query
from fastapi.responses import FileResponse

from ..deps import AdminUser, CurrentUser
from ..models import Job, JobCreate, JobReviewRequest, JobStatus, JobType
from ..services import job_service
from ..services.scheduler import gpu_snapshot

router = APIRouter(prefix="/jobs", tags=["jobs"])


@router.get("", summary="任务列表")
def list_jobs(
    user: CurrentUser,
    mine_only: bool = Query(default=False, description="只看自己的任务"),
    status: list[JobStatus] | None = Query(default=None),
    type: list[JobType] | None = Query(default=None),
    project_id: str = Query(default="", description="只看某个项目的任务"),
    owner_id: str = Query(default="", description="只看某个负责人的任务"),
    q: str = "",
    limit: int = Query(default=100, le=500),
    offset: int = 0,
) -> dict[str, Any]:
    items, total = job_service.list_jobs(
        actor=user,
        mine_only=mine_only,
        statuses=status,
        types=type,
        project_id=project_id,
        owner_id=owner_id,
        query=q,
        limit=limit,
        offset=offset,
    )
    return {"items": items, "total": total, "offset": offset, "limit": limit}


@router.get("/overview", summary="任务统计")
def overview(user: CurrentUser, project_id: str = Query(default="")) -> dict[str, Any]:
    payload = job_service.stats_overview(user, project_id=project_id)
    payload["gpu"] = gpu_snapshot()
    return payload


@router.post("", response_model=Job, status_code=201, summary="提交任务")
def create_job(payload: JobCreate, user: CurrentUser) -> Job:
    return job_service.create_job(payload, owner=user)


@router.get("/{job_id}", response_model=Job, summary="任务详情")
def get_job(job_id: str, user: CurrentUser) -> Job:
    return job_service.get_job_for(job_id, user)


@router.get("/{job_id}/log", summary="任务日志")
def get_log(
    job_id: str,
    user: CurrentUser,
    offset: int = Query(default=0, ge=0, description="从该字节偏移继续读取"),
    tail_bytes: int | None = Query(default=None, le=5_000_000),
) -> dict[str, Any]:
    return job_service.job_log(job_id, actor=user, tail_bytes=tail_bytes, offset=offset)


@router.get("/{job_id}/metrics", summary="训练/评测曲线")
def get_metrics(job_id: str, user: CurrentUser) -> dict[str, Any]:
    return job_service.job_metrics(job_id, actor=user)


@router.get("/{job_id}/artifacts", summary="产物列表")
def get_artifacts(job_id: str, user: CurrentUser) -> list[dict[str, Any]]:
    return job_service.job_artifacts(job_id, actor=user)


@router.get("/{job_id}/artifacts/download", summary="下载单个产物文件")
def download_artifact(job_id: str, path: str, user: CurrentUser) -> FileResponse:
    target = job_service.artifact_file(job_id, path, actor=user)
    return FileResponse(target, filename=target.name, media_type="application/octet-stream")


@router.get("/{job_id}/trials", summary="评测 trial 明细")
def get_trials(job_id: str, user: CurrentUser, limit: int = Query(default=500, le=2000)) -> dict[str, Any]:
    return job_service.job_trials(job_id, actor=user, limit=limit)


@router.get("/{job_id}/command", summary="预览将要执行的命令")
def get_command(job_id: str, user: CurrentUser) -> dict[str, Any]:
    return job_service.preview_command(job_id, actor=user)


@router.post("/{job_id}/review", response_model=Job, summary="审核任务（管理员）")
def review(job_id: str, payload: JobReviewRequest, admin: AdminUser) -> Job:
    return job_service.review_job(
        job_id, approve=payload.approve, comment=payload.comment, reviewer=admin
    )


@router.post("/{job_id}/cancel", response_model=Job, summary="终止任务")
def cancel(job_id: str, user: CurrentUser, reason: str = Body(default="", embed=True)) -> Job:
    return job_service.cancel_job(job_id, actor=user, reason=reason)


@router.post("/{job_id}/resubmit", response_model=Job, status_code=201, summary="按相同配置重跑")
def resubmit(job_id: str, user: CurrentUser) -> Job:
    return job_service.resubmit_job(job_id, actor=user)


@router.get("/{job_id}/verify-suggestion", summary="从训练产物发起评测的建议")
def verify_suggestion(job_id: str, user: CurrentUser) -> dict[str, Any]:
    return job_service.verify_from_training(job_id, actor=user)


@router.delete("/{job_id}", status_code=204, summary="删除任务")
def delete_job(job_id: str, user: CurrentUser, purge_outputs: bool = False) -> None:
    job_service.delete_job(job_id, actor=user, purge_outputs=purge_outputs)
