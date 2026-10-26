"""问题反馈 + 托管修复：用临时 git 仓库、裸远端和假 claude 脚本走完整流程。"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

from backend.app.config import settings
from backend.app.models import IssueCreate, IssueStatus
from backend.app.services import issue_service
from backend.app.store import store

FAKE_AGENT = """#!{python}
import pathlib, subprocess, sys
prompt = sys.argv[sys.argv.index("-p") + 1]
mode = pathlib.Path({mode!r}).read_text().strip()
print('{{"type":"system","subtype":"init"}}', flush=True)
if mode == "fix":
    pathlib.Path("app.py").write_text("def add(a, b):\\n    return a + b\\n")
    pathlib.Path("test_app.py").write_text("from app import add\\n\\ndef test_add():\\n    assert add(1, 2) == 3\\n")
    subprocess.run(["git", "add", "app.py", "test_app.py"], check=True)
    subprocess.run(["git", "commit", "-qm", "fix: add"], check=True)
    pathlib.Path("PRT_FIX_REPORT.md").write_text("## 问题定位\\napp.add 用了减法\\n")
elif mode == "switch":
    subprocess.run(["git", "checkout", "-q", "main"], check=True)
print('{{"type":"result","result":"' + ("prompt-ok" if "定位" in prompt else "bad") + '"}}', flush=True)
"""


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path, monkeypatch):
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(remote))
    work = tmp_path / "platform"
    work.mkdir()
    _git(work, "init", "-q", "-b", "main")
    _git(work, "config", "user.email", "t@prt.local")
    _git(work, "config", "user.name", "t")
    (work / "app.py").write_text("def add(a, b):\n    return a - b\n")
    _git(work, "add", "app.py")
    _git(work, "commit", "-qm", "init")
    _git(work, "remote", "add", "origin", str(remote))
    _git(work, "push", "-q", "origin", "main")

    mode = tmp_path / "mode"
    mode.write_text("fix")
    agent = tmp_path / "bin" / "claude"
    agent.parent.mkdir()
    agent.write_text(FAKE_AGENT.format(python=sys.executable, mode=str(mode)))
    agent.chmod(0o755)

    monkeypatch.setattr(settings, "issue_repo_root", work)
    monkeypatch.setattr(settings, "issue_workspace_root", tmp_path / "workspaces")
    monkeypatch.setattr(settings, "issue_agent_bin", str(agent))
    monkeypatch.setattr(settings, "issue_auto_restart", False)
    return {"work": work, "remote": remote, "mode": mode}


def _admin():
    return store.users.first(lambda u: u.is_admin)


def _wait(issue_id: str, *states: IssueStatus, timeout: float = 30) -> object:
    deadline = time.time() + timeout
    while time.time() < deadline:
        item = store.issues.get(issue_id)
        if item.status in states:
            return item
        time.sleep(0.1)
    raise AssertionError(f"issue stayed {store.issues.get(issue_id).status}")


def _new_issue(title: str = "加法结果错误"):
    return issue_service.create_issue(IssueCreate(title=title, description="1+2 显示 -1", page="/jobs"), actor=_admin())


def test_fix_then_merge_pushes_main_and_keeps_platform_clean(repo):
    issue = _new_issue()
    started = issue_service.start_fix(issue.id, actor=_admin())
    assert started.status == IssueStatus.FIXING and started.branch.startswith("fix/issue-")

    fixed = _wait(issue.id, IssueStatus.PENDING_MERGE, IssueStatus.FAILED)
    assert fixed.status == IssueStatus.PENDING_MERGE, fixed.error
    assert "app.add 用了减法" in fixed.report
    assert "app.py" in fixed.diff_stat
    workspace = Path(fixed.workspace)
    assert not workspace.is_relative_to(repo["work"])
    # 修复只发生在副本里：平台目录还是旧代码
    assert "a - b" in (repo["work"] / "app.py").read_text()
    assert any("申请合并" in n.title for n in store.notifications.find(lambda n: n.link == f"/issues/{issue.id}"))

    issue_service.merge_issue(issue.id, actor=_admin())
    merged = _wait(issue.id, IssueStatus.MERGED, IssueStatus.PENDING_MERGE)
    assert merged.status == IssueStatus.MERGED, merged.error
    assert "a + b" in (repo["work"] / "app.py").read_text()
    assert _git(repo["remote"], "rev-parse", "main").strip() == merged.merge_commit
    assert not workspace.exists()


def test_agent_without_commit_fails_with_report_fallback(repo):
    repo["mode"].write_text("noop")
    issue = _new_issue("无从下手")
    issue_service.start_fix(issue.id, actor=_admin())
    failed = _wait(issue.id, IssueStatus.FAILED, IssueStatus.PENDING_MERGE)
    assert failed.status == IssueStatus.FAILED
    assert "没有在修复分支上提交" in failed.error
    assert failed.report == "prompt-ok"  # 没写报告文件时取 stream-json 的最终结果
    # 失败后可以再次托管修复
    repo["mode"].write_text("fix")
    issue_service.start_fix(issue.id, actor=_admin())
    assert _wait(issue.id, IssueStatus.PENDING_MERGE, IssueStatus.FAILED).status == IssueStatus.PENDING_MERGE


def test_agent_leaving_fix_branch_is_rejected(repo):
    repo["mode"].write_text("switch")
    issue = _new_issue("切分支")
    issue_service.start_fix(issue.id, actor=_admin())
    failed = _wait(issue.id, IssueStatus.FAILED, IssueStatus.PENDING_MERGE)
    assert "切离了修复分支" in failed.error


def test_merge_refuses_dirty_platform_checkout(repo):
    issue = _new_issue("脏目录")
    issue_service.start_fix(issue.id, actor=_admin())
    _wait(issue.id, IssueStatus.PENDING_MERGE)
    (repo["work"] / "app.py").write_text("local edit\n")
    with pytest.raises(Exception) as exc:
        issue_service.merge_issue(issue.id, actor=_admin())
    assert "未提交的改动" in str(getattr(exc.value, "detail", exc.value))
    assert store.issues.get(issue.id).status == IssueStatus.PENDING_MERGE


def test_prompt_requires_locate_fix_test_and_forbids_push():
    issue = _new_issue("提示词")
    issue.branch, issue.base_commit = "fix/issue-x", "abc1234567"
    prompt = issue_service.build_prompt(issue)
    for needle in ("定位", "修复", "测试", "禁止 `git push`", issue.branch, "PRT_FIX_REPORT.md", "1+2 显示 -1"):
        assert needle in prompt


def test_issue_api_permissions(client, admin_headers, member_headers):
    created = client.post("/api/issues", headers=member_headers, json={"title": "按钮没反应", "description": "点了没用"})
    assert created.status_code == 201, created.text
    issue_id = created.json()["id"]
    assert created.json()["status"] == "open"
    assert any(i["id"] == issue_id for i in client.get("/api/issues", headers=member_headers).json())
    assert client.get(f"/api/issues/{issue_id}", headers=admin_headers).status_code == 200
    # 托管修复与合并只给管理员
    assert client.post(f"/api/issues/{issue_id}/fix", headers=member_headers).status_code == 403
    assert client.post(f"/api/issues/{issue_id}/merge", headers=member_headers).status_code == 403
    assert client.post(f"/api/issues/{issue_id}/merge", headers=admin_headers).status_code == 409
    closed = client.post(f"/api/issues/{issue_id}/close", headers=member_headers)
    assert closed.json()["status"] == "closed"


def test_reconcile_finalizes_fix_that_finished_while_platform_was_down(repo):
    issue = _new_issue("重启期间完成")
    issue_service.start_fix(issue.id, actor=_admin())
    done = _wait(issue.id, IssueStatus.PENDING_MERGE)
    # 模拟：Agent 已经提交并退出，但平台在收尾前重启
    store.issues.update(issue.id, lambda i: setattr(i, "status", IssueStatus.FIXING))
    issue_service.reconcile()
    assert store.issues.get(issue.id).status == IssueStatus.PENDING_MERGE
    assert store.issues.get(issue.id).head_commit == done.head_commit
