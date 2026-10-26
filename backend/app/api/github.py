"""个人账号绑定 GitHub：OAuth 授权或 Personal Access Token。"""

from __future__ import annotations

from urllib.parse import urlencode, urlparse

from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import RedirectResponse

from ..deps import CurrentUser
from ..models import GitHubBinding, GitHubTokenSet
from ..services import github_service

router = APIRouter(prefix="/github", tags=["github"])

CALLBACK_PATH = "/api/github/oauth/callback"


@router.get("/me", response_model=GitHubBinding, summary="我绑定的 GitHub 账号")
def my_binding(user: CurrentUser) -> GitHubBinding:
    return github_service.binding(user)


@router.put("/me/token", response_model=GitHubBinding, summary="用 Personal Access Token 绑定")
def bind_token(payload: GitHubTokenSet, user: CurrentUser) -> GitHubBinding:
    return github_service.bind_token(user, payload.token)


@router.delete("/me", status_code=204, summary="解绑 GitHub")
def unbind(user: CurrentUser) -> None:
    github_service.unbind(user)


@router.post("/oauth/start", summary="开始 GitHub OAuth 授权，返回跳转地址")
def oauth_start(user: CurrentUser, origin: str = Query(min_length=1, max_length=300)) -> dict[str, str]:
    parsed = urlparse(origin)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.path not in {"", "/"}:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="origin 不合法")
    redirect_uri = f"{parsed.scheme}://{parsed.netloc}{CALLBACK_PATH}"
    return {"url": github_service.oauth_start(user, redirect_uri)}


@router.get("/oauth/callback", include_in_schema=False)
def oauth_callback(state: str = "", code: str = "", error: str = "", error_description: str = "") -> RedirectResponse:
    # 浏览器从 GitHub 跳回来，带不了 token；身份由签名的 state 确定。回到同源的个人设置页。
    target = "/settings"
    try:
        if error:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=error_description or error)
        if not code or not state:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="缺少授权参数")
        github_service.oauth_callback(code, state)
        query = {"github": "ok"}
    except HTTPException as exc:
        query = {"github": "error", "message": str(exc.detail)[:200]}
    return RedirectResponse(f"{target}?{urlencode(query)}", status_code=status.HTTP_303_SEE_OTHER)
