"""System dashboard: telemetry, toolchain health, admin settings."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from ..config import settings
from ..deps import AdminUser, CurrentUser
from ..models import JobStatus, SystemSettings, utcnow
from ..models.job import JobPublic
from ..models.billing import PricingSettings
from ..services import billing_service, monitor, resources_service, audit_service
from ..services.scheduler import gpu_snapshot, scheduler
from ..store import store

router = APIRouter(prefix="/system", tags=["system"])


@router.get("/metrics", summary="当前系统指标")
def metrics(_: CurrentUser) -> dict[str, Any]:
    return monitor.latest()


@router.get("/metrics/history", summary="系统指标历史（折线图用）")
def metrics_history(_: CurrentUser, limit: int = Query(default=300, le=2000)) -> dict[str, Any]:
    return {
        "points": monitor.compact_history(limit),
        "interval_seconds": settings.monitor_interval_seconds,
    }


@router.get("/dashboard", summary="数据大屏聚合数据")
def dashboard(user: CurrentUser) -> dict[str, Any]:
    jobs = store.jobs.all()
    visible = jobs if user.is_admin else [j for j in jobs if j.owner_id == user.id]
    billing = billing_service.summary(visible, actor=user)
    visible = [store.jobs.get(job.id) or job for job in visible]
    active = [j for j in visible if j.status.is_active]
    active.sort(key=lambda j: (j.status != JobStatus.RUNNING, j.created_at))

    recent = sorted(visible, key=lambda j: j.updated_at, reverse=True)[:12]
    return {
        "at": utcnow().isoformat(),
        "metrics": monitor.latest(),
        "history": monitor.compact_history(180),
        "gpu": gpu_snapshot(),
        "active_jobs": [JobPublic.model_validate(job.model_dump()) for job in active[:20]],
        "recent_jobs": [JobPublic.model_validate(job.model_dump()) for job in recent],
        "billing": billing,
        "counters": {
            "running": sum(1 for j in visible if j.status == JobStatus.RUNNING),
            "queued": sum(1 for j in visible if j.status == JobStatus.QUEUED),
            "pending_review": sum(1 for j in visible if j.status == JobStatus.PENDING_REVIEW),
            "succeeded": sum(1 for j in visible if j.status == JobStatus.SUCCEEDED),
            "failed": sum(1 for j in visible if j.status == JobStatus.FAILED),
            "total": len(visible),
            "users": store.users.count() if user.is_admin else None,
            "datasets": store.datasets.count(),
        },
    }


@router.get("/toolchain", summary="框架/工具链可用性")
def toolchain(_: CurrentUser) -> list[dict[str, Any]]:
    return resources_service.toolchain_status()


@router.get("/settings", response_model=SystemSettings, summary="系统设置")
def read_settings(_: CurrentUser) -> SystemSettings:
    current = store.settings_doc.read()
    if not current.updated_by:
        current.require_approval = settings.require_approval
        current.max_concurrent_jobs = settings.max_concurrent_jobs
    return current


@router.get("/pricing", response_model=PricingSettings, summary="模型单价、折扣和人民币汇率")
def read_pricing(_: CurrentUser) -> PricingSettings:
    return billing_service.pricing()


@router.put("/pricing", response_model=PricingSettings, summary="更新价格设置（仅影响新任务）")
def update_pricing(payload: PricingSettings, admin: AdminUser) -> PricingSettings:
    updated = billing_service.save_pricing(payload, admin)
    audit_service.record("billing.pricing.update", actor=admin, target_type="system", target_id="pricing",
                         summary="更新模型价格、折扣和人民币汇率（保留现有任务价格快照）")
    return updated


@router.put("/settings", response_model=SystemSettings, summary="更新系统设置（管理员）")
def update_settings(payload: SystemSettings, admin: AdminUser) -> SystemSettings:
    def mutate(current: SystemSettings) -> None:
        current.require_approval = payload.require_approval
        current.max_concurrent_jobs = payload.max_concurrent_jobs
        current.default_max_gpus_per_job = payload.default_max_gpus_per_job
        current.allow_self_service_verify = payload.allow_self_service_verify
        current.banner = payload.banner
        current.updated_at = utcnow()
        current.updated_by = admin.email

    updated = store.settings_doc.update(mutate)
    scheduler.dispatch()
    return updated


@router.get("/info", summary="平台基础信息")
def info(_: CurrentUser) -> dict[str, Any]:
    return {
        "app_name": settings.app_name,
        "version": settings.app_version,
        "models_root": str(settings.models_root),
        "data_root": str(settings.data_root),
        "gpu_total": scheduler.total_gpus,
        "monitor_interval_seconds": settings.monitor_interval_seconds,
        "max_upload_mb": settings.max_upload_mb,
    }


@router.get("/harbor-image", summary="平台 Harbor 镜像状态")
def harbor_image_status(_: CurrentUser) -> dict[str, Any]:
    from ..services import harbor_image_service
    return harbor_image_service.status()


@router.get("/harbor-image/recipe", summary="平台 Harbor 镜像构建配方")
def harbor_image_recipe(_: AdminUser) -> dict[str, str]:
    from ..services import harbor_image_service
    return {"dockerfile": harbor_image_service.recipe()}


@router.post("/harbor-image/build", summary="构建平台 Harbor 镜像")
def build_harbor_image(_: AdminUser) -> dict[str, Any]:
    from ..services import harbor_image_service
    return harbor_image_service.start()
