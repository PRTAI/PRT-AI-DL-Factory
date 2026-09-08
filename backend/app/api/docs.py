"""平台文档接口：目录、正文与插图。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from fastapi.responses import FileResponse

from ..deps import CurrentUser
from ..services import docs_service

# 注意：/api/docs 被 FastAPI 的 Swagger UI 占用，平台文档走 /api/handbook
router = APIRouter(prefix="/handbook", tags=["handbook"])


@router.get("", summary="文档目录")
def list_docs(_: CurrentUser) -> list[dict[str, Any]]:
    return docs_service.list_docs()


@router.get("/assets/{name}", summary="文档插图")
def doc_asset(name: str, _: CurrentUser) -> FileResponse:
    return FileResponse(docs_service.asset_path(name))


@router.get("/{slug}", summary="文档正文（Markdown）")
def get_doc(slug: str, _: CurrentUser) -> dict[str, Any]:
    return docs_service.get_doc(slug)
