"""账号绑定 GitHub。

* 配了 OAuth App（``PRT_GITHUB_CLIENT_ID`` / ``PRT_GITHUB_CLIENT_SECRET``）时走网页授权；
  没配时可以在个人设置里粘贴 Personal Access Token，平台调 ``/user`` 校验后保存。
* 合并问题修复时，``issue_service`` 用操作人绑定的账号给合并提交署名，并用其 token 推送
  （token 经环境变量里的 git 配置传给子进程，不出现在命令行参数和日志里）。
"""

from __future__ import annotations

import base64
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode, urlparse

import httpx
import jwt
from fastapi import HTTPException, status

from ..config import settings
from ..models import GitHubAccount, GitHubAccountPublic, GitHubBinding, User, utcnow
from ..store import store
from . import audit_service

_STATE_AUDIENCE = "github-oauth"
_STATE_TTL = timedelta(minutes=10)


def _client() -> httpx.Client:
    return httpx.Client(timeout=15, headers={"Accept": "application/vnd.github+json", "User-Agent": settings.app_name})


def mask_token(token: str) -> str:
    token = token.strip()
    return f"{token[:4]}…{token[-4:]}" if len(token) > 12 else ("…" + token[-2:] if token else "")


def oauth_enabled() -> bool:
    return bool(settings.github_client_id.strip() and settings.github_client_secret.strip())


def account_of(user_id: str) -> GitHubAccount | None:
    row = store.github_accounts.get(user_id)
    return row if row and row.token else None


def binding(user: User) -> GitHubBinding:
    row = account_of(user.id)
    public = None
    if row is not None:
        public = GitHubAccountPublic(**row.model_dump(exclude={"id", "github_id", "token"}), token_masked=mask_token(row.token))
    return GitHubBinding(account=public, oauth_enabled=oauth_enabled())


# --------------------------------------------------------------------------- #
# 绑定 / 解绑
# --------------------------------------------------------------------------- #
def bind_token(user: User, token: str) -> GitHubBinding:
    _save(user, token.strip(), source="token")
    return binding(user)


def unbind(user: User) -> None:
    row = store.github_accounts.get(user.id)
    if row is None:
        return
    store.github_accounts.delete(user.id)
    audit_service.record("github.unbind", actor=user, target_type="user", target_id=user.id,
                         summary=f"解绑 GitHub {row.login}")


def purge_user(user_id: str) -> None:
    store.github_accounts.delete(user_id)


def oauth_start(user: User, redirect_uri: str) -> str:
    if not oauth_enabled():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="管理员还没有配置 GitHub OAuth App，请改用 Personal Access Token 绑定")
    now = datetime.now(timezone.utc)
    state = jwt.encode(
        {"sub": user.id, "aud": _STATE_AUDIENCE, "nonce": secrets.token_urlsafe(8),
         "redirect_uri": redirect_uri, "exp": int((now + _STATE_TTL).timestamp())},
        settings.secret_key, algorithm=settings.jwt_algorithm,
    )
    query = urlencode({"client_id": settings.github_client_id, "redirect_uri": redirect_uri,
                       "scope": settings.github_oauth_scopes, "state": state, "allow_signup": "false"})
    return f"{settings.github_web_url.rstrip('/')}/login/oauth/authorize?{query}"


def oauth_callback(code: str, state: str) -> User:
    """用 state 里签名的平台账号完成绑定；回调不带登录态，身份只认 state。"""
    try:
        claims = jwt.decode(state, settings.secret_key, algorithms=[settings.jwt_algorithm],
                            audience=_STATE_AUDIENCE, options={"require": ["exp", "sub"]})
    except jwt.PyJWTError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="授权已过期或无效，请重新绑定") from exc
    user = store.users.get(claims["sub"])
    if user is None or not user.active:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="平台账号不存在或已停用")
    if not oauth_enabled():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="GitHub OAuth 未配置")
    with _client() as client:
        resp = client.post(
            f"{settings.github_web_url.rstrip('/')}/login/oauth/access_token",
            data={"client_id": settings.github_client_id, "client_secret": settings.github_client_secret,
                  "code": code, "redirect_uri": claims.get("redirect_uri", "")},
            headers={"Accept": "application/json"},
        )
    data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
    token = str(data.get("access_token") or "")
    if resp.status_code >= 400 or not token:
        detail = data.get("error_description") or data.get("error") or f"HTTP {resp.status_code}"
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=f"GitHub 授权失败：{detail}")
    _save(user, token, source="oauth")
    return user


def _save(user: User, token: str, *, source: str) -> GitHubAccount:
    profile, scopes = _fetch_profile(token)
    previous = store.github_accounts.get(user.id)
    row = GitHubAccount(
        id=user.id, github_id=int(profile["id"]), login=str(profile["login"]), name=str(profile.get("name") or ""),
        email=_primary_email(token, profile), avatar_url=str(profile.get("avatar_url") or ""),
        token=token, scopes=scopes, source=source,  # type: ignore[arg-type]
        created_at=previous.created_at if previous else utcnow(),
    )
    store.github_accounts.put(row)
    audit_service.record("github.bind", actor=user, target_type="user", target_id=user.id,
                         summary=f"绑定 GitHub {row.login}（{'OAuth' if source == 'oauth' else 'Token'}）")
    return row


def _fetch_profile(token: str) -> tuple[dict, str]:
    try:
        with _client() as client:
            resp = client.get(f"{settings.github_api_url.rstrip('/')}/user", headers={"Authorization": f"Bearer {token}"})
    except httpx.HTTPError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=f"连接 GitHub 失败：{exc}") from exc
    if resp.status_code == 401:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="GitHub 拒绝了这个 token（无效或已过期）")
    if resp.status_code >= 400:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=f"GitHub 返回 HTTP {resp.status_code}")
    return resp.json(), resp.headers.get("x-oauth-scopes", "")


def _primary_email(token: str, profile: dict) -> str:
    try:
        with _client() as client:
            resp = client.get(f"{settings.github_api_url.rstrip('/')}/user/emails", headers={"Authorization": f"Bearer {token}"})
        if resp.status_code == 200:
            for item in resp.json():
                if item.get("primary") and item.get("verified"):
                    return str(item["email"])
    except (httpx.HTTPError, ValueError, TypeError):
        pass
    if profile.get("email"):
        return str(profile["email"])
    return f"{profile['id']}+{profile['login']}@users.noreply.github.com"


# --------------------------------------------------------------------------- #
# 给 git 子进程用
# --------------------------------------------------------------------------- #
def git_identity(account: GitHubAccount) -> tuple[str, str]:
    return account.name or account.login, account.email


def push_env(account: GitHubAccount, remote_url: str) -> dict[str, str]:
    """只对 https 的 GitHub 远端注入 token；ssh 或其他主机返回空，沿用本机凭据。"""
    parsed = urlparse(remote_url)
    host = urlparse(settings.github_web_url).hostname
    if parsed.scheme != "https" or parsed.hostname != host:
        return {}
    basic = base64.b64encode(f"x-access-token:{account.token}".encode()).decode()
    return {
        "GIT_CONFIG_COUNT": "2",
        "GIT_CONFIG_KEY_0": f"http.https://{host}/.extraheader",
        "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}",
        # 不让本机 credential helper 抢先给出别人的凭据
        "GIT_CONFIG_KEY_1": "credential.helper",
        "GIT_CONFIG_VALUE_1": "",
    }
