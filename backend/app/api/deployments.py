"""模型部署（vLLM）与 checkpoint 合并导出。

两者放在一个路由里：合并导出的产物几乎总是紧接着被部署，前端也是同一个页面。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from ..deps import CurrentUser
from ..models import Deployment, DeploymentCreate, ExportCreate, ExportTask
from ..services import deployment_service, export_service

router = APIRouter(prefix="/deployments", tags=["deployments"])


# ------------------------------------------------------------------ 部署 ---
@router.get("", response_model=list[Deployment], summary="部署列表（全员可见）")
def list_deployments(
    user: CurrentUser,
    active_only: bool = False,
    project_id: str = Query(default=""),
) -> list[Deployment]:
    del user
    return deployment_service.list_deployments(active_only=active_only, project_id=project_id)


@router.get("/endpoints", summary="可用推理端点（评测表单用）")
def endpoints(user: CurrentUser) -> list[dict[str, Any]]:
    del user
    return deployment_service.endpoints()


@router.post("", response_model=Deployment, status_code=201, summary="部署模型")
def create_deployment(payload: DeploymentCreate, user: CurrentUser) -> Deployment:
    return deployment_service.create_deployment(payload, owner=user)


@router.get("/{deployment_id}", response_model=Deployment, summary="部署详情")
def get_deployment(deployment_id: str, user: CurrentUser) -> Deployment:
    del user
    return deployment_service.get_deployment(deployment_id)


@router.get("/{deployment_id}/log", summary="部署日志")
def deployment_log(
    deployment_id: str,
    user: CurrentUser,
    offset: int = Query(default=0, ge=0),
    tail_bytes: int | None = Query(default=None, le=5_000_000),
) -> dict[str, Any]:
    del user
    return deployment_service.deployment_log(
        deployment_id, tail_bytes=tail_bytes, offset=offset
    )


@router.post("/{deployment_id}/stop", response_model=Deployment, summary="停止部署")
def stop_deployment(deployment_id: str, user: CurrentUser) -> Deployment:
    return deployment_service.stop_deployment(deployment_id, actor=user)


@router.delete("/{deployment_id}", status_code=204, summary="删除部署记录")
def delete_deployment(deployment_id: str, user: CurrentUser) -> None:
    deployment_service.delete_deployment(deployment_id, actor=user)


# ------------------------------------------------------------ 合并导出 ---
exports_router = APIRouter(prefix="/exports", tags=["exports"])


@exports_router.get("", response_model=list[ExportTask], summary="导出任务列表")
def list_exports(user: CurrentUser, job_id: str = Query(default="")) -> list[ExportTask]:
    del user
    return export_service.list_exports(job_id=job_id)


@exports_router.get("/candidates/{job_id}", summary="某个训练任务可导出的 checkpoint")
def candidates(job_id: str, user: CurrentUser) -> dict[str, Any]:
    del user
    return export_service.exportable_checkpoints(job_id)


@exports_router.post("", response_model=ExportTask, status_code=201, summary="发起合并导出")
def create_export(payload: ExportCreate, user: CurrentUser) -> ExportTask:
    return export_service.create_export(payload, owner=user)


@exports_router.get("/{export_id}", response_model=ExportTask, summary="导出详情")
def get_export(export_id: str, user: CurrentUser) -> ExportTask:
    del user
    return export_service.get_export(export_id)


@exports_router.get("/{export_id}/log", summary="导出日志")
def export_log(
    export_id: str, user: CurrentUser, tail_bytes: int | None = Query(default=None, le=5_000_000)
) -> dict[str, Any]:
    del user
    return export_service.export_log(export_id, tail_bytes=tail_bytes)


@exports_router.post("/{export_id}/cancel", response_model=ExportTask, summary="取消导出")
def cancel_export(export_id: str, user: CurrentUser) -> ExportTask:
    return export_service.cancel_export(export_id, actor=user)


@exports_router.delete("/{export_id}", status_code=204, summary="删除导出记录")
def delete_export(export_id: str, user: CurrentUser, purge_files: bool = False) -> None:
    export_service.delete_export(export_id, actor=user, purge_files=purge_files)
