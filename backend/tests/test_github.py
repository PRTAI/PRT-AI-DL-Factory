"""账号绑定 GitHub：Token 绑定、接口不回显 token、OAuth state 校验、合并用绑定身份署名。"""

from __future__ import annotations

import base64

import pytest

from backend.app.config import settings
from backend.app.models import GitHubAccount, IssueStatus
from backend.app.services import github_service, issue_service
from backend.app.store import store

from .conftest import user_id_of
from .test_issues import _admin, _git, _new_issue, _wait, repo  # noqa: F401 - repo 是 fixture

TOKEN = "ghp_secretsecretsecret1234"


@pytest.fixture
def fake_github(monkeypatch):
    def profile(token: str):
        if token != TOKEN:
            raise github_service.HTTPException(400, detail="GitHub 拒绝了这个 token（无效或已过期）")
        return {"id": 42, "login": "octo", "name": "Octo Cat", "avatar_url": "https://a/x.png"}, "repo, read:user"

    monkeypatch.setattr(github_service, "_fetch_profile", profile)
    monkeypatch.setattr(github_service, "_primary_email", lambda token, p: "octo@example.com")


def test_bind_token_masks_and_unbind(client, member_headers, fake_github):
    bad = client.put("/api/github/me/token", headers=member_headers, json={"token": "nope"})
    assert bad.status_code == 400

    bound = client.put("/api/github/me/token", headers=member_headers, json={"token": TOKEN})
    assert bound.status_code == 200, bound.text
    account = bound.json()["account"]
    assert account["login"] == "octo" and account["email"] == "octo@example.com"
    assert TOKEN not in bound.text and account["token_masked"].endswith("1234")
    assert TOKEN not in client.get("/api/github/me", headers=member_headers).text
    assert store.github_accounts.get(user_id_of("dev@prt.local")).token == TOKEN
    assert (settings.db_dir / "github_accounts.json").stat().st_mode & 0o077 == 0

    assert client.delete("/api/github/me", headers=member_headers).status_code == 204
    assert client.get("/api/github/me", headers=member_headers).json()["account"] is None


def test_oauth_requires_config_and_rejects_forged_state(client, member_headers, monkeypatch):
    monkeypatch.setattr(settings, "github_client_id", "")
    started = client.post("/api/github/oauth/start", headers=member_headers, params={"origin": "http://localhost:5153"})
    assert started.status_code == 400

    monkeypatch.setattr(settings, "github_client_id", "cid")
    monkeypatch.setattr(settings, "github_client_secret", "csecret")
    assert client.post("/api/github/oauth/start", headers=member_headers,
                       params={"origin": "javascript:alert(1)"}).status_code == 400
    started = client.post("/api/github/oauth/start", headers=member_headers, params={"origin": "http://localhost:5153"})
    assert started.status_code == 200
    url = started.json()["url"]
    assert url.startswith("https://github.com/login/oauth/authorize?") and "client_id=cid" in url
    assert "redirect_uri=http%3A%2F%2Flocalhost%3A5153%2Fapi%2Fgithub%2Foauth%2Fcallback" in url

    forged = client.get("/api/github/oauth/callback", params={"code": "x", "state": "forged"}, follow_redirects=False)
    assert forged.status_code == 303 and "github=error" in forged.headers["location"]
    assert forged.headers["location"].startswith("/settings?")


def test_push_env_only_for_https_github():
    account = GitHubAccount(id="u", github_id=1, login="octo", token=TOKEN)
    env = github_service.push_env(account, "https://github.com/PRTAI/PRT-AI-DL-Factory.git")
    header = env["GIT_CONFIG_VALUE_0"]
    assert base64.b64decode(header.split()[-1]).decode() == f"x-access-token:{TOKEN}"
    assert env["GIT_CONFIG_KEY_1"] == "credential.helper" and env["GIT_CONFIG_VALUE_1"] == ""
    assert github_service.push_env(account, "git@github.com:PRTAI/x.git") == {}
    assert github_service.push_env(account, "https://gitlab.com/a/b.git") == {}


def test_merge_uses_merger_github_identity(repo):  # noqa: F811
    admin = _admin()
    store.github_accounts.put(GitHubAccount(id=admin.id, github_id=7, login="admin-gh", name="Admin GH",
                                            email="admin-gh@example.com", token=TOKEN))
    try:
        issue = _new_issue("署名")
        issue_service.start_fix(issue.id, actor=admin)
        _wait(issue.id, IssueStatus.PENDING_MERGE)
        issue_service.merge_issue(issue.id, actor=admin)
        merged = _wait(issue.id, IssueStatus.MERGED, IssueStatus.PENDING_MERGE)
        assert merged.status == IssueStatus.MERGED, merged.error
        assert merged.merged_github == "admin-gh"
        author = _git(repo["work"], "log", "-1", "--format=%an <%ae>", "main").strip()
        assert author == "Admin GH <admin-gh@example.com>"
        # 测试里的远端是本地裸仓库，token 不会注入，并提示用了本机凭据
        assert "本机 git 凭据" in merged.error
        assert _git(repo["remote"], "rev-parse", "main").strip() == merged.merge_commit
        log = issue_service._log_path(issue.id).read_text(encoding="utf-8")
        assert TOKEN not in log and "admin-gh" in log
    finally:
        store.github_accounts.delete(admin.id)
