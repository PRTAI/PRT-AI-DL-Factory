"""Job submission, review, control and inspection."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Query
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from ..deps import AdminUser, CurrentUser
from ..models import Job, JobCreate, JobReviewRequest, JobStatus, JobType
from ..services import billing_service, job_download_service, job_service
from ..models.job import JobPublic, JobRenameRequest
from ..models.job import HarborActionRequest, HarborTrialActionRequest, VerifyBatchCreate
from ..services import harbor_action_service
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
    pending = billing_service.worker.request(items)
    return {"items": [JobPublic.model_validate(item.model_dump()) for item in items], "total": total,
            "offset": offset, "limit": limit, "billing_pending": pending}


@router.get("/overview", summary="任务统计")
def overview(user: CurrentUser, project_id: str = Query(default="")) -> dict[str, Any]:
    payload = job_service.stats_overview(user, project_id=project_id)
    payload["gpu"] = gpu_snapshot()
    return payload


@router.post("", response_model=JobPublic, status_code=201, summary="提交任务")
def create_job(payload: JobCreate, user: CurrentUser) -> Job:
    return job_service.create_job(payload, owner=user)


@router.post("/verify-batch", response_model=list[JobPublic], status_code=201, summary="批量提交 Harbor 评测")
def create_verify_batch(payload: VerifyBatchCreate, user: CurrentUser) -> list[Job]:
    return job_service.create_verify_batch(payload, owner=user)


@router.post("/batch-resubmit", summary="批量按原配置重跑已结束任务")
def batch_resubmit(
    user: CurrentUser,
    job_ids: list[str] = Body(..., embed=True, min_length=1, max_length=200),
) -> dict[str, Any]:
    return job_service.batch_resubmit_jobs(job_ids, actor=user)


@router.post("/batch-export", summary="批量导出下载任务 ZIP")
def batch_export(
    user: CurrentUser,
    job_ids: list[str] = Body(..., embed=True, min_length=1, max_length=200),
) -> FileResponse:
    target, filename = job_download_service.prepare_batch_download(job_ids, actor=user)
    return FileResponse(
        target, filename=filename, media_type="application/zip",
        background=BackgroundTask(target.unlink, missing_ok=True),
    )


@router.get("/{job_id}", response_model=JobPublic, summary="任务详情")
def get_job(job_id: str, user: CurrentUser) -> Job:
    return job_service.get_job_for(job_id, user)


@router.patch("/{job_id}/name", response_model=JobPublic, summary="修改已结束任务的名称")
def rename_job(job_id: str, payload: JobRenameRequest, user: CurrentUser) -> Job:
    return job_service.rename_job(job_id, name=payload.name, actor=user)


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


@router.get("/{job_id}/export", summary="导出下载任务 ZIP（配置、日志、指标与产物）")
def export_job(job_id: str, user: CurrentUser) -> FileResponse:
    target, filename = job_download_service.prepare_download(job_id, actor=user)
    return FileResponse(
        target, filename=filename, media_type="application/zip",
        background=BackgroundTask(target.unlink, missing_ok=True),
    )


@router.get("/{job_id}/artifacts/download", summary="下载单个产物文件")
def download_artifact(job_id: str, path: str, user: CurrentUser) -> FileResponse:
    target = job_service.artifact_file(job_id, path, actor=user)
    return FileResponse(target, filename=target.name, media_type="application/octet-stream")


@router.get("/{job_id}/trials", summary="评测逐 trial 阶段（分页）")
def get_trials(
    job_id: str,
    user: CurrentUser,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=10, ge=1, le=100),
    phase: str = Query(default="", description="pending/setup_environment/running_agent/running_verifier/success/failed/running"),
    q: str = "",
) -> dict[str, Any]:
    return job_service.job_trials(job_id, actor=user, page=page, page_size=page_size, phase=phase, query=q)


@router.get("/{job_id}/trials/{trial_name}", summary="单个 trial 的详细日志")
def get_trial_detail(job_id: str, trial_name: str, user: CurrentUser) -> dict[str, Any]:
    return job_service.job_trial_detail(job_id, trial_name, actor=user)


@router.get("/{job_id}/command", summary="预览将要执行的命令")
def get_command(job_id: str, user: CurrentUser) -> dict[str, Any]:
    return job_service.preview_command(job_id, actor=user)


@router.post("/{job_id}/review", response_model=JobPublic, summary="审核任务（管理员）")
def review(job_id: str, payload: JobReviewRequest, admin: AdminUser) -> Job:
    return job_service.review_job(
        job_id, approve=payload.approve, comment=payload.comment, reviewer=admin
    )


@router.post("/{job_id}/cancel", response_model=JobPublic, summary="终止任务")
def cancel(job_id: str, user: CurrentUser, reason: str = Body(default="", embed=True)) -> Job:
    return job_service.cancel_job(job_id, actor=user, reason=reason)


@router.post("/{job_id}/resubmit", response_model=JobPublic, status_code=201, summary="按相同配置重跑")
def resubmit(job_id: str, user: CurrentUser) -> Job:
    return job_service.resubmit_job(job_id, actor=user)


@router.get("/{job_id}/harbor-actions", summary="检查 Harbor 恢复和重新判分所需的记录")
def harbor_actions(job_id: str, user: CurrentUser) -> dict[str, Any]:
    return harbor_action_service.inspect_job(job_id, actor=user)


@router.post("/{job_id}/harbor-actions", response_model=JobPublic, status_code=201, summary="恢复评测或重新判分")
def create_harbor_action(job_id: str, payload: HarborActionRequest, user: CurrentUser) -> Job:
    return harbor_action_service.create_action(job_id, payload, actor=user)


@router.post("/{job_id}/trial-actions", response_model=list[JobPublic], status_code=201,
             summary="对选中的 trial 批量重跑、评分或恢复")
def create_trial_actions(job_id: str, payload: HarborTrialActionRequest, user: CurrentUser) -> list[Job]:
    return harbor_action_service.create_trial_actions(job_id, payload, actor=user)


@router.get("/{job_id}/verify-suggestion", summary="从训练产物发起评测的建议")
def verify_suggestion(job_id: str, user: CurrentUser) -> dict[str, Any]:
    return job_service.verify_from_training(job_id, actor=user)


@router.delete("/{job_id}", status_code=204, summary="删除任务")
def delete_job(job_id: str, user: CurrentUser, purge_outputs: bool = False) -> None:
    job_service.delete_job(job_id, actor=user, purge_outputs=purge_outputs)
