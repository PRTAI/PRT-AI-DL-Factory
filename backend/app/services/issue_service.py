"""问题反馈与托管修复。

* 任何成员都能提交问题；只有管理员能启动托管修复和合并。
* 修复：把仓库 **clone 到仓库之外的独立目录**（``issue_workspace_root/<issue_id>``），
  建 ``fix/issue-<id>`` 分支，再用本机 Claude Code（``claude -p``）在副本里定位、修复、
  补测试并提交。正在运行的平台目录不会被改动。
* Agent 不允许 push；修复完成后状态变为 ``pending_merge``（申请合并到 main）并通知。
* 合并：管理员确认后，平台在运行目录里合并分支到 main、推送远端、按需重建前端，再重启自身。

修复子进程独立成会话，平台重启时不会被带走；``reconcile()`` 只要发现它还活着就继续等它结束，
结束后的收尾只看副本里的 git 状态与报告文件，不依赖退出码。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from fastapi import HTTPException, status

from ..config import settings
from ..models import Issue, IssueCreate, IssueStatus, User, utcnow
from ..store import store
from . import audit_service, github_service

logger = logging.getLogger("prt.issues")

REPORT_FILE = "PRT_FIX_REPORT.md"
MAX_REPORT_CHARS = 20_000
_GIT_IDENTITY = ("PRT Agent", "agent@prt.local")
_lock = threading.Lock()
_watching: set[str] = set()
_merging = threading.Lock()

FIX_PROMPT = """你是 PRT AI DL Factory 平台的维护工程师，正在一个专用的仓库副本里修复用户反馈的问题。

## 工作环境
- 当前目录是平台仓库的独立副本，已经切到分支 `{branch}`（基于 `{main}` 的 {base}）。只在这个目录里工作。
- 正在运行的平台在另一个目录，不要访问、修改或重启它，也不要读取任何 `data/`、`.env`、密钥文件。
- 禁止 `git push`、修改 remote、切换到 `{main}` 或改写已有提交历史；推送和合并由平台在管理员确认后完成。
- 不要安装或升级依赖（pip/npm install 等）。前端 `frontend/node_modules` 是指向原仓库的链接，只读使用。
- 先读 `CLAUDE.md`，遵守里面的分层、存储、前端设计令牌等约定。

## 要修复的问题
- 编号：{issue_id}
- 标题：{title}
- 反馈页面：{page}
- 描述：
{description}

## 步骤
1. **定位**：根据描述（页面、报错、操作步骤）在代码中搜索相关的接口、服务和前端组件，
   确认问题的根因。先读代码再下结论；描述不足以确定时，选择最合理的理解并在报告中写明假设。
2. **修复**：做能解决根因的最小改动，风格与周围代码保持一致，不顺手重构无关代码。
3. **测试**：在 `backend/tests/` 下新增或补充几个测试用例覆盖这个问题（修复前应失败、修复后通过），
   涉及前端时补 `frontend/tests/` 或至少保证构建通过。然后运行：
   - 后端：`python -m pytest backend/tests -p no:cacheprovider -q`（PATH 与 PYTHONPATH 已指向平台解释器和本副本，直接用 `python`）
     （与本问题相关的测试必须全部通过；因缺少本机训练框架而失败的环境性用例可以忽略，但要在报告里说明）
   - 前端（改了前端时）：`cd frontend && npm run build && npm test`
4. **提交**：`git add` 具体文件后在 `{branch}` 上提交，提交信息用中文，格式 `fix: <简述>（{issue_id}）`。
5. **报告**：在仓库根目录写 `{report}`（已被 git 忽略，不要提交），用中文 Markdown，控制在 30 行内：
   - 问题定位：根因在哪个文件/函数，为什么出错
   - 修复内容：改了什么
   - 测试：新增的用例名称，以及实际运行的命令和结果（通过/失败数量）
   - 风险与未完成事项（没有就写「无」）

如果无法修复（找不到问题、需要用户补充信息、或需要改动外部系统），不要提交猜测性的改动，
只写报告说明原因和需要的信息。
"""


# --------------------------------------------------------------------------- #
# 查询与创建
# --------------------------------------------------------------------------- #
def list_issues(*, actor: User, status_filter: str = "") -> list[Issue]:
    def keep(item: Issue) -> bool:
        if status_filter and item.status.value != status_filter:
            return False
        return actor.is_admin or item.reporter_id == actor.id

    return sorted(store.issues.find(keep), key=lambda i: i.created_at, reverse=True)


def get_issue(issue_id: str, *, actor: User) -> Issue:
    item = store.issues.get(issue_id)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="问题不存在")
    if not (actor.is_admin or item.reporter_id == actor.id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="只有反馈人或管理员可以查看")
    return item


def create_issue(payload: IssueCreate, *, actor: User) -> Issue:
    item = Issue(
        title=payload.title.strip(),
        description=payload.description.strip(),
        page=payload.page.strip(),
        reporter_id=actor.id,
        reporter_email=actor.email,
    )
    store.issues.put(item)
    audit_service.record("issue.create", actor=actor, target_type="issue", target_id=item.id, summary=item.title)
    audit_service.notify_admins("收到新的问题反馈", body=f"{actor.email}：{item.title}", link=_link(item))
    return item


def close_issue(issue_id: str, *, actor: User) -> Issue:
    item = get_issue(issue_id, actor=actor)
    if item.status in {IssueStatus.FIXING, IssueStatus.MERGING}:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="修复或合并进行中，不能关闭")
    if item.status == IssueStatus.MERGED:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="已合并的问题不能关闭")
    _remove_workspace(item)
    updated = _update(item.id, status=IssueStatus.CLOSED)
    audit_service.record("issue.close", actor=actor, target_type="issue", target_id=item.id, summary=item.title)
    return updated


def agent_log(issue_id: str, *, actor: User, tail_bytes: int = 64_000) -> dict[str, object]:
    item = get_issue(issue_id, actor=actor)
    path = _log_path(item.id)
    text = ""
    if path.is_file():
        with path.open("rb") as handle:
            handle.seek(max(0, path.stat().st_size - tail_bytes))
            text = handle.read().decode("utf-8", errors="replace")
    return {"text": text, "running": item.status in {IssueStatus.FIXING, IssueStatus.MERGING}}


# --------------------------------------------------------------------------- #
# 托管修复
# --------------------------------------------------------------------------- #
def start_fix(issue_id: str, *, actor: User) -> Issue:
    item = get_issue(issue_id, actor=actor)
    agent = shutil.which(settings.issue_agent_bin) or (
        settings.issue_agent_bin if Path(settings.issue_agent_bin).is_file() else None
    )
    if agent is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=f"本机未找到 Claude Code（{settings.issue_agent_bin}），请配置 PRT_ISSUE_AGENT_BIN")
    repo = settings.issue_repo_root.resolve()
    if not (repo / ".git").exists():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=f"{repo} 不是 git 仓库")
    if _workspace_root().is_relative_to(repo):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="PRT_ISSUE_WORKSPACE_ROOT 必须在平台仓库之外")
    with _lock:
        current = store.issues.get(item.id)
        if current is None or not current.status.can_fix:
            raise HTTPException(status.HTTP_409_CONFLICT, detail="当前状态不能启动托管修复")
        _remove_workspace(current)
        item = _update(
            item.id, status=IssueStatus.FIXING, fix_requested_by=actor.id, fix_started_at=utcnow(),
            fix_finished_at=None, report="", diff_stat="", error="", head_commit="", pid=None,
            branch=f"fix/issue-{item.id.removeprefix('iss_')}",
        )
    audit_service.record("issue.fix", actor=actor, target_type="issue", target_id=item.id, summary=item.title)
    threading.Thread(target=_run_fix, args=(item.id, agent), daemon=True, name=f"issue-fix-{item.id}").start()
    return item


def _run_fix(issue_id: str, agent: str) -> None:
    log_path = _log_path(issue_id)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        item = _prepare_workspace(issue_id)
        command = _agent_command(item, agent)
        with log_path.open("ab") as log:
            process = subprocess.Popen(  # noqa: S603 - argv 由本模块构造，不过 shell
                command, cwd=item.workspace, env=_agent_env(item.workspace),stdin=subprocess.DEVNULL,
                stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
            )
        _update(issue_id, pid=process.pid)
        try:
            process.wait(timeout=settings.issue_agent_timeout_seconds)
        except subprocess.TimeoutExpired:
            _kill_group(process.pid)
            process.wait()
            _finalize(issue_id, timed_out=True)
            return
        _finalize(issue_id)
    except Exception as exc:  # 任何异常都要让记录离开 fixing，否则按钮永远是灰的
        logger.exception("issue %s fix failed", issue_id)
        _fail(issue_id, f"托管修复启动失败：{exc}")


def _prepare_workspace(issue_id: str) -> Issue:
    item = store.issues.get(issue_id)
    assert item is not None
    repo = settings.issue_repo_root.resolve()
    main = settings.issue_main_branch
    workspace = _workspace_root() / item.id
    workspace.parent.mkdir(parents=True, exist_ok=True)
    _log(issue_id, f"复制仓库 {repo}（{main}）到 {workspace}")
    _git(workspace.parent, "clone", "--quiet", "--branch", main, str(repo), str(workspace))
    _git(workspace, "checkout", "--quiet", "-b", item.branch)
    if not _git(workspace, "config", "user.email", check=False).strip():
        _git(workspace, "config", "user.name", _GIT_IDENTITY[0])
        _git(workspace, "config", "user.email", _GIT_IDENTITY[1])
    with (workspace / ".git" / "info" / "exclude").open("a", encoding="utf-8") as handle:
        handle.write(f"\n{REPORT_FILE}\nfrontend/node_modules\n")
    modules = repo / "frontend" / "node_modules"
    if modules.is_dir() and (workspace / "frontend").is_dir():
        (workspace / "frontend" / "node_modules").symlink_to(modules, target_is_directory=True)
    base = _git(workspace, "rev-parse", "HEAD").strip()
    _log(issue_id, f"已在分支 {item.branch} 上就绪（基线 {base[:10]}），启动 Claude Code")
    return _update(issue_id, workspace=str(workspace), base_commit=base)


def build_prompt(item: Issue) -> str:
    return FIX_PROMPT.format(
        branch=item.branch, main=settings.issue_main_branch, base=item.base_commit[:10] or "HEAD",
        issue_id=item.id, title=item.title, page=item.page or "（未提供）",
        description=item.description or "（未提供，仅有标题）", report=REPORT_FILE,
    )


def _agent_command(item: Issue, agent: str) -> list[str]:
    command = [
        agent, "-p", build_prompt(item),
        "--output-format", "stream-json", "--verbose",
        "--permission-mode", settings.issue_agent_permission_mode,
    ]
    if settings.issue_agent_allowed_tools.strip():
        command += ["--allowedTools", settings.issue_agent_allowed_tools]
    if settings.issue_agent_disallowed_tools.strip():
        command += ["--disallowedTools", settings.issue_agent_disallowed_tools]
    if settings.issue_agent_model.strip():
        command += ["--model", settings.issue_agent_model.strip()]
    return command


def _agent_env(workspace: str) -> dict[str, str]:
    from .proc_env import with_interpreter_on_path

    # 平台自己的 PRT_* 配置（数据目录、密钥等）不能漏进副本：测试会按 conftest 用临时目录
    env = {key: value for key, value in os.environ.items() if not key.startswith("PRT_")}
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONPATH"] = workspace
    return with_interpreter_on_path(env, sys.executable)


def _finalize(issue_id: str, *, timed_out: bool = False) -> Issue | None:
    """只依据副本里的 git 提交和报告收尾——平台重启后也能用同一套判断。"""
    item = store.issues.get(issue_id)
    if item is None or item.status != IssueStatus.FIXING:
        return item
    workspace = Path(item.workspace) if item.workspace else None
    report = ""
    if workspace is not None and (workspace / REPORT_FILE).is_file():
        report = (workspace / REPORT_FILE).read_text(encoding="utf-8", errors="replace")[:MAX_REPORT_CHARS].strip()
    if not report:
        report = _final_message(issue_id)
    commits, head, diff_stat = "", "", ""
    if workspace is not None and workspace.is_dir() and item.base_commit:
        try:
            current = _git(workspace, "rev-parse", "--abbrev-ref", "HEAD").strip()
            head = _git(workspace, "rev-parse", "HEAD").strip()
            commits = _git(workspace, "log", "--oneline", f"{item.base_commit}..{item.branch}").strip()
            diff_stat = _git(workspace, "diff", "--stat", f"{item.base_commit}..{item.branch}").strip()
            if current != item.branch:
                return _fail(issue_id, f"Agent 切离了修复分支（当前 {current}）", report=report)
        except RuntimeError as exc:
            return _fail(issue_id, f"读取修复分支失败：{exc}", report=report)
    if timed_out:
        return _fail(issue_id, f"Claude Code 超过 {settings.issue_agent_timeout_seconds:.0f} 秒未完成，已终止", report=report)
    if not commits:
        return _fail(issue_id, "Agent 没有在修复分支上提交任何改动，详见报告与日志", report=report)
    updated = _update(
        issue_id, status=IssueStatus.PENDING_MERGE, fix_finished_at=utcnow(), pid=None,
        report=report or "（Agent 未写报告）", head_commit=head, diff_stat=diff_stat[-4000:],
    )
    for user_id in {updated.fix_requested_by, updated.reporter_id} - {""}:
        audit_service.notify(user_id, "问题已修复，申请合并到 main", body=updated.title, level="success", link=_link(updated))
    return updated


def _final_message(issue_id: str) -> str:
    """没写报告时，退而取 stream-json 最后一条 result 文本。"""
    path = _log_path(issue_id)
    if not path.is_file():
        return ""
    result = ""
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("type") == "result" and isinstance(event.get("result"), str):
            result = event["result"]
    return result[:MAX_REPORT_CHARS].strip()


# --------------------------------------------------------------------------- #
# 合并、推送、重启
# --------------------------------------------------------------------------- #
def merge_issue(issue_id: str, *, actor: User) -> Issue:
    item = get_issue(issue_id, actor=actor)
    if item.status != IssueStatus.PENDING_MERGE:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="只有「申请合并」状态的问题可以合并")
    workspace = Path(item.workspace)
    if not workspace.is_dir():
        raise HTTPException(status.HTTP_409_CONFLICT, detail="修复副本已不存在，请重新托管修复")
    repo = settings.issue_repo_root.resolve()
    try:
        branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
        dirty = _git(repo, "status", "--porcelain", "--untracked-files=no").strip()
    except RuntimeError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    if branch != settings.issue_main_branch:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=f"平台目录当前在 {branch} 分支，请切回 {settings.issue_main_branch} 后再合并")
    if dirty:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="平台目录有未提交的改动，请先提交或暂存后再合并")
    if not _merging.acquire(blocking=False):
        raise HTTPException(status.HTTP_409_CONFLICT, detail="已有问题正在合并，请稍后")
    try:
        item = _update(item.id, status=IssueStatus.MERGING, error="", merged_by=actor.id)
    except Exception:
        _merging.release()
        raise
    audit_service.record("issue.merge", actor=actor, target_type="issue", target_id=item.id,
                         summary=f"{item.title} → {settings.issue_main_branch}")
    threading.Thread(target=_run_merge, args=(item.id,), daemon=True, name=f"issue-merge-{item.id}").start()
    return item


def _run_merge(issue_id: str) -> None:
    try:
        item = store.issues.get(issue_id)
        assert item is not None
        repo = settings.issue_repo_root.resolve()
        main = settings.issue_main_branch
        _log(issue_id, f"合并 {item.branch} 到 {main}")
        account = github_service.account_of(item.merged_by)
        identity: list[str] = []
        if account is not None:
            name, email = github_service.git_identity(account)
            identity = ["-c", f"user.name={name}", "-c", f"user.email={email}"]
            _log(issue_id, f"使用 {item.merged_by} 绑定的 GitHub 账号 {account.login} 署名并推送")
        _git(repo, "fetch", "--quiet", item.workspace, f"+{item.branch}:{item.branch}")
        try:
            _git(repo, *identity, "merge", "--no-ff", "--no-edit", "-m", f"Merge {item.branch}: {item.title}（{item.id}）", item.branch)
        except RuntimeError as exc:
            _git(repo, "merge", "--abort", check=False)
            _update(issue_id, status=IssueStatus.PENDING_MERGE, error=f"合并冲突，已放弃本次合并：{exc}")
            return
        merge_commit = _git(repo, "rev-parse", "HEAD").strip()
        warnings: list[str] = []
        _log(issue_id, f"推送 {main} 到 {settings.issue_push_remote}")
        push_env: dict[str, str] = {}
        if account is not None:
            remote_url = _git(repo, "remote", "get-url", "--push", settings.issue_push_remote, check=False).strip()
            push_env = github_service.push_env(account, remote_url)
            if not push_env:
                warnings.append(f"远端 {remote_url or settings.issue_push_remote} 不是 https 的 GitHub 地址，已用本机 git 凭据推送")
        try:
            _git(repo, "-c", "http.version=HTTP/1.1", "push", settings.issue_push_remote, main, timeout=300, env=push_env)
        except RuntimeError as exc:
            warnings.append(f"推送失败，请手动推送：{exc}")
        changed = _git(repo, "diff", "--name-only", f"{item.base_commit}..{merge_commit}", check=False)
        if any(line.startswith("frontend/") for line in changed.splitlines()):
            warnings.extend(_build_frontend(issue_id, repo))
        restart = settings.issue_auto_restart
        updated = _update(
            issue_id, status=IssueStatus.MERGED, merged_at=utcnow(), merge_commit=merge_commit,
            merged_github=account.login if account else "",
            error="；".join(warnings), restart_scheduled=restart,
        )
        _remove_workspace(updated)
        for user_id in {updated.merged_by, updated.reporter_id} - {""}:
            audit_service.notify(user_id, f"问题已合并到 {main}" + ("，平台即将重启" if restart else ""),
                                 body=updated.title, level="warning" if warnings else "success", link=_link(updated))
        if restart:
            _log(issue_id, "合并完成，平台即将重启")
            threading.Timer(2.0, restart_platform).start()
    except Exception as exc:
        logger.exception("issue %s merge failed", issue_id)
        _update(issue_id, status=IssueStatus.PENDING_MERGE, error=f"合并失败：{exc}")
    finally:
        _merging.release()


def _build_frontend(issue_id: str, repo: Path) -> list[str]:
    npm = shutil.which("npm")
    if npm is None:
        return ["本机没有 npm，前端改动未重新构建"]
    _log(issue_id, "前端有改动，重新构建 frontend/dist")
    try:
        result = subprocess.run([npm, "run", "build"], cwd=repo / "frontend", capture_output=True, text=True, timeout=900)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return [f"前端构建失败：{exc}"]
    _log(issue_id, (result.stdout + result.stderr)[-4000:])
    return [] if result.returncode == 0 else ["前端构建失败，详见日志"]


def restart_platform() -> None:
    """重新拉起当前进程。调度器对账会接管仍在运行的训练/评测子进程。"""
    import multiprocessing

    if settings.issue_restart_command.strip():
        subprocess.Popen(settings.issue_restart_command, shell=True, start_new_session=True)  # noqa: S602 - 管理员配置
        return
    if multiprocessing.parent_process() is not None:
        logger.info("running under the uvicorn reloader; code changes reload automatically")
        return
    logger.warning("restarting platform after issue merge")
    os.execv(sys.executable, [sys.executable, *sys.orig_argv[1:]])


# --------------------------------------------------------------------------- #
# 重启对账
# --------------------------------------------------------------------------- #
def reconcile() -> None:
    for item in store.issues.find(lambda i: i.status in {IssueStatus.FIXING, IssueStatus.MERGING}):
        if item.status == IssueStatus.MERGING:
            repo = settings.issue_repo_root.resolve()
            merged = bool(item.head_commit) and not _git(
                repo, "merge-base", "--is-ancestor", item.head_commit, settings.issue_main_branch, check=False, rc=True)
            if merged:
                _update(item.id, status=IssueStatus.MERGED, merged_at=item.merged_at or utcnow(), restart_scheduled=False)
            else:
                _update(item.id, status=IssueStatus.PENDING_MERGE, error="合并过程中平台重启，请重新合并")
        elif _alive(item.pid):
            _watch(item.id, item.pid)  # type: ignore[arg-type]
        elif item.workspace:
            _finalize(item.id)
        else:
            _fail(item.id, "平台重启时托管修复尚未开始，请重新启动")


def _watch(issue_id: str, pid: int) -> None:
    if issue_id in _watching:
        return
    _watching.add(issue_id)

    def loop() -> None:
        deadline = (store.issues.get(issue_id).fix_started_at or utcnow()).timestamp() + settings.issue_agent_timeout_seconds
        while _alive(pid):
            if time.time() > deadline:
                _kill_group(pid)
                _finalize(issue_id, timed_out=True)
                break
            time.sleep(5)
        else:
            _finalize(issue_id)
        _watching.discard(issue_id)

    threading.Thread(target=loop, daemon=True, name=f"issue-watch-{issue_id}").start()


def _alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        import psutil

        process = psutil.Process(pid)
        name = Path(settings.issue_agent_bin).name
        return process.status() != psutil.STATUS_ZOMBIE and any(name in part for part in process.cmdline()[:2])
    except Exception:
        return False


def _kill_group(pid: int) -> None:
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pid, sig)
        except (ProcessLookupError, PermissionError):
            return
        time.sleep(3)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _git(cwd: Path, *args: str, check: bool = True, rc: bool = False, timeout: float = 120,
         env: dict[str, str] | None = None):
    full_env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", **(env or {})}
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout, env=full_env)
    if rc:
        return result.returncode
    if check and result.returncode:
        command = next((a for a in args if not a.startswith("-") and "=" not in a), args[0])
        raise RuntimeError(f"git {command} 失败：{(result.stderr or result.stdout).strip()[-1500:]}")
    return result.stdout


def _workspace_root() -> Path:
    return settings.issue_workspace_root.expanduser().resolve()


def _remove_workspace(item: Issue) -> None:
    if not item.workspace:
        return
    path = Path(item.workspace).resolve()
    # 只删我们自己建的副本目录
    if path.parent == _workspace_root() and path.name == item.id:
        shutil.rmtree(path, ignore_errors=True)


def _log_path(issue_id: str) -> Path:
    return settings.issues_dir / f"{issue_id}.log"


def _log(issue_id: str, text: str) -> None:
    path = _log_path(issue_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"[PRT {time.strftime('%Y-%m-%d %H:%M:%S')}] {text}\n")


def _link(item: Issue) -> str:
    return f"/issues/{item.id}"


def _update(issue_id: str, **changes) -> Issue:
    def mutate(item: Issue) -> None:
        for key, value in changes.items():
            setattr(item, key, value)
        item.updated_at = utcnow()

    updated = store.issues.update(issue_id, mutate)
    if updated is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="问题不存在")
    return updated


def _fail(issue_id: str, error: str, *, report: str = "") -> Issue:
    _log(issue_id, error)
    item = _update(issue_id, status=IssueStatus.FAILED, error=error, fix_finished_at=utcnow(), pid=None,
                   **({"report": report} if report else {}))
    if item.fix_requested_by:
        audit_service.notify(item.fix_requested_by, "托管修复未完成", body=f"{item.title}：{error}", level="error", link=_link(item))
    return item
