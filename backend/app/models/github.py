"""账号绑定的 GitHub 身份：合并问题修复时用操作人自己的 GitHub 账号署名并推送。"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from .common import Base, utcnow


class GitHubAccount(Base):
    id: str  # = 平台账号 id，一个账号只绑一个 GitHub
    github_id: int
    login: str
    name: str = ""
    email: str = ""        # 提交署名用；拿不到公开邮箱时用 GitHub 的 noreply 地址
    avatar_url: str = ""
    token: str = ""        # 明文只存在 db 文件里（0600），接口永不返回
    scopes: str = ""
    source: Literal["oauth", "token"] = "token"
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class GitHubAccountPublic(Base):
    login: str
    name: str = ""
    email: str = ""
    avatar_url: str = ""
    scopes: str = ""
    source: Literal["oauth", "token"] = "token"
    token_masked: str = ""
    created_at: datetime | None = None
    updated_at: datetime | None = None


class GitHubBinding(Base):
    account: GitHubAccountPublic | None = None
    oauth_enabled: bool = False


class GitHubTokenSet(Base):
    token: str = Field(min_length=1, max_length=512)
