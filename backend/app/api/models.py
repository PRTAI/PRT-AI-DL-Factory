"""Model zoo endpoints: search local models, pull new ones from ModelScope."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from ..config import settings
from ..deps import AdminUser, CurrentUser
from ..models import ModelDownload, ModelDownloadRequest, ModelEntry
from ..services import model_service, resources_service

router = APIRouter(prefix="/models", tags=["models"])


@router.get("", response_model=list[ModelEntry], summary="搜索本地模型")
def list_models(
    _: CurrentUser,
    q: str = Query(default="", description="模糊搜索：厂家 / 模型名 / 架构"),
    framework: str = Query(default="", description="只看某框架支持的模型"),
    limit: int = Query(default=200, le=1000),
) -> list[ModelEntry]:
    return model_service.search_models(q, framework=framework, limit=limit)


@router.post("/refresh", response_model=list[ModelEntry], summary="重新扫描模型目录")
def refresh(_: CurrentUser) -> list[ModelEntry]:
    return model_service.scan_models(force=True)


@router.get("/compat", summary="框架-模型适配速查文档")
def compat(_: CurrentUser) -> dict[str, Any]:
    return resources_service.model_compat_doc()


@router.get("/root", summary="模型根目录信息")
def root(_: CurrentUser) -> dict[str, Any]:
    return {
        "models_root": str(settings.models_root),
        "exists": settings.models_root.is_dir(),
        "count": len(model_service.scan_models()),
    }


@router.get("/downloads", response_model=list[ModelDownload], summary="下载任务列表")
def downloads(_: CurrentUser, limit: int = Query(default=50, le=200)) -> list[ModelDownload]:
    return model_service.list_downloads(limit=limit)


@router.post("/downloads", response_model=ModelDownload, status_code=201, summary="从 ModelScope 下载模型")
def start_download(payload: ModelDownloadRequest, user: CurrentUser) -> ModelDownload:
    return model_service.start_download(payload.model_id, revision=payload.revision, actor=user)


@router.get("/downloads/{download_id}/log", summary="下载日志")
def download_log(download_id: str, _: CurrentUser) -> dict[str, str]:
    return {"content": model_service.download_log(download_id)}


@router.post("/downloads/{download_id}/cancel", response_model=ModelDownload, summary="取消下载")
def cancel_download(download_id: str, admin: AdminUser) -> ModelDownload:
    return model_service.cancel_download(download_id, actor=admin)


@router.get("/{model_id:path}", response_model=ModelEntry, summary="模型详情")
def get_model(model_id: str, _: CurrentUser) -> ModelEntry:
    from fastapi import HTTPException, status

    entry = model_service.get_model(model_id)
    if entry is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"未找到模型: {model_id}")
    return entry
