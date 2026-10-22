"""样例审核接口：提交、历史、逐题结论、下载报告与原始数据。"""

from __future__ import annotations

from typing import Any, Literal
from urllib.parse import quote

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile, status
from fastapi.responses import FileResponse

from ..deps import CurrentUser
from ..models import ReasoningEffort, ReviewCreate, ReviewTask
from ..services import review_service

router = APIRouter(prefix="/reviews", tags=["reviews"])

DOCX_MEDIA = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


@router.get("", response_model=list[ReviewTask], summary="我的审核历史")
def list_reviews(
    user: CurrentUser,
    mine_only: bool = Query(default=True, description="管理员置 false 可看全部"),
    project_id: str = Query(default=""),
) -> list[ReviewTask]:
    return review_service.list_reviews(actor=user, mine_only=mine_only, project_id=project_id)


@router.post("", response_model=ReviewTask, status_code=201, summary="提交样例审核")
async def create_review(
    user: CurrentUser,
    archive: UploadFile = File(..., description="样例压缩包（zip / tar.gz）"),
    docs: list[UploadFile] = File(..., description="需求文档（质检规范），docx / pdf / md / txt，可多份"),
    name: str = Form(..., description="批次名，进报告标题"),
    supplier: str = Form(default=""),
    spec_title: str = Form(default="", description="依据规范名，进「一、说明」"),
    prompt: str = Form(default="", description="自定义提示词"),
    model_api_id: str = Form(...),
    project_id: str = Form(..., min_length=1, description="质检所属项目"),
    agent: Literal["claude-code", "codex"] = Form(default="claude-code", description="审核 Agent harness"),
    reasoning_effort: ReasoningEffort = Form(default=""),
) -> ReviewTask:
    if not archive.filename:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="缺少压缩包文件名")
    uploaded = [(d.filename or "", d.file) for d in docs if d.filename]
    if not uploaded:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="请至少上传一份需求文档")
    payload = ReviewCreate(
        name=name,
        supplier=supplier,
        spec_title=spec_title,
        prompt=prompt,
        model_api_id=model_api_id,
        project_id=project_id,
        agent=agent,
        reasoning_effort=reasoning_effort,
    )
    return review_service.create_review(
        payload,
        archive_name=archive.filename,
        archive_stream=archive.file,
        docs=uploaded,
        owner=user,
    )


@router.get("/{review_id}", response_model=ReviewTask, summary="审核详情")
def get_review(review_id: str, user: CurrentUser) -> ReviewTask:
    return review_service.get_review(review_id, actor=user)


@router.get("/{review_id}/findings", summary="逐题结论")
def get_findings(review_id: str, user: CurrentUser) -> list[dict[str, Any]]:
    return review_service.findings_of(review_id, actor=user)


@router.get("/{review_id}/log", summary="审核 Agent 执行日志")
def get_agent_log(
    review_id: str, user: CurrentUser, tail_bytes: int = Query(default=64_000, ge=1024, le=200_000),
) -> dict[str, Any]:
    return review_service.agent_log(review_id, actor=user, tail_bytes=tail_bytes)


@router.get("/{review_id}/report.docx", summary="下载返修报告")
def download_report(review_id: str, user: CurrentUser) -> FileResponse:
    item = review_service.get_review(review_id, actor=user)
    target = review_service.report_file(review_id, actor=user)
    return _attachment(target, f"返修报告_{item.supplier or item.name}_{item.name}.docx", DOCX_MEDIA)


@router.get("/{review_id}/archive", summary="下载原始样例压缩包")
def download_archive(review_id: str, user: CurrentUser) -> FileResponse:
    target = review_service.archive_file(review_id, actor=user)
    return _attachment(target, target.name, "application/octet-stream")


@router.post("/{review_id}/cancel", response_model=ReviewTask, summary="取消审核")
def cancel_review(review_id: str, user: CurrentUser) -> ReviewTask:
    return review_service.cancel_review(review_id, actor=user)


@router.delete("/{review_id}", status_code=204, summary="删除审核记录")
def delete_review(
    review_id: str,
    user: CurrentUser,
    purge_files: bool = Query(default=False, description="同时清掉工作目录里的原始数据"),
) -> None:
    review_service.delete_review(review_id, actor=user, purge_files=purge_files)


def _attachment(target, filename: str, media_type: str) -> FileResponse:
    """文件名里有中文，必须走 RFC 5987 的 filename*，否则浏览器存成乱码。"""
    safe = filename.replace('"', "")
    return FileResponse(
        target,
        media_type=media_type,
        headers={
            "content-disposition": f"attachment; filename*=UTF-8''{quote(safe)}",
        },
    )
