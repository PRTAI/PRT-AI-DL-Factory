"""Dataset upload / registry endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile, status

from ..deps import CurrentUser
from ..models import Dataset, DatasetKind, DatasetUpdate
from ..services import dataset_service, resources_service

router = APIRouter(prefix="/datasets", tags=["datasets"])


@router.get("/specs", summary="支持的数据集格式说明")
def specs() -> dict[str, Any]:
    return resources_service.dataset_specs()


@router.get("", response_model=list[Dataset], summary="数据集列表")
def list_datasets(
    user: CurrentUser,
    kind: DatasetKind | None = Query(default=None),
    mine_only: bool = Query(default=False),
) -> list[Dataset]:
    return dataset_service.list_datasets(kind=kind, owner_id=user.id if mine_only else None)


@router.post("", response_model=Dataset, status_code=201, summary="上传数据集压缩包")
async def upload_dataset(
    user: CurrentUser,
    file: UploadFile = File(..., description="zip / tar.gz 压缩包"),
    name: str = Form(...),
    kind: DatasetKind = Form(...),
    description: str = Form(default=""),
    tags: str = Form(default=""),
) -> Dataset:
    if not file.filename:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="缺少文件名")
    tag_list = [t.strip() for t in tags.split(",") if t.strip()]
    return dataset_service.create_from_upload(
        name=name,
        kind=kind,
        description=description,
        tags=tag_list,
        filename=file.filename,
        stream=file.file,
        owner=user,
    )


@router.get("/{dataset_id}", response_model=Dataset, summary="数据集详情")
def get_dataset(dataset_id: str, _: CurrentUser) -> Dataset:
    return dataset_service.get_dataset(dataset_id)


@router.get("/{dataset_id}/preview", summary="数据集样例预览")
def preview(dataset_id: str, _: CurrentUser, limit: int = Query(default=5, le=20)) -> dict[str, Any]:
    return dataset_service.preview(dataset_id, limit=limit)


@router.patch("/{dataset_id}", response_model=Dataset, summary="更新数据集信息")
def update_dataset(dataset_id: str, payload: DatasetUpdate, user: CurrentUser) -> Dataset:
    return dataset_service.update_dataset(dataset_id, payload, actor=user)


@router.delete("/{dataset_id}", status_code=204, summary="删除数据集")
def delete_dataset(dataset_id: str, user: CurrentUser) -> None:
    dataset_service.delete_dataset(dataset_id, actor=user)
