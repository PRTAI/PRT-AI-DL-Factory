"""内置 Agent：对话 CRUD、附件、以及以本机 Claude Code 为核心的对话轮次。

设计要点（与反馈需求一一对应）：

* **对话归账号所有**：列表、详情、改名、删除、发消息都按 ``owner_id`` 过滤，
  管理员也看不到别人的对话。
* **不用再登录**：每轮启动子进程前，按对话用户签发一个短期 JWT，写到工作目录里
  的会话文件，并通过 ``PRT_PLATFORM_SESSION`` / ``PRT_PLATFORM_URL`` 交给
  Skill 的 ``prt_api.py``；Agent 操作平台时用的就是这位用户的权限。
* **拥有平台 Skill + 工具**：把 ``skills/prt-platform`` 复制到工作目录的
  ``.claude/skills``，Claude Code 会自动发现；系统提示里说明如何调用。
* **改不了平台代码**：工作目录在平台仓库之外，子进程的工具白名单只有
  读写/检索和 ``python``（跑 Skill 客户端），``git`` / ``npm`` / ``pip`` 全部禁用。
* **对话可以改名、删除**；附件落在工作目录的 ``uploads/``，Agent 可以直接读。
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
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import HTTPException, UploadFile, status

from ..config import settings
from ..models import (
    AgentAttachment,
    AgentConversation,
    AgentConversationCreate,
    AgentConversationPublic,
    AgentConversationUpdate,
    AgentMessage,
    AgentModelOption,
    AgentSkill,
    AgentStatus,
    AgentTool,
    User,
    utcnow,
)
from ..store import store
from . import api_key_service, audit_service, model_api_service

logger = logging.getLogger("prt.agent")

SKILL_NAME = "prt-platform"
MAX_TOOL_TEXT = 8_000
MAX_TOOL_INPUT = 4_000

_lock = threading.RLock()
_processes: dict[str, subprocess.Popen[str]] = {}
_cancelled: set[str] = set()

TOOLS: list[tuple[str, str]] = [
    ("platform_api", "调用 PRT 平台 HTTP API（项目、任务、数据集、部署、费用等），凭据已由平台注入"),
    ("datasets", "查询与上传数据集；上传的压缩包会由平台校验并注册"),
    ("jobs", "提交 SFT / RL / Harbor 评测任务，查看日志、指标与 trial 明细"),
    ("models", "查询模型仓库、个人模型 API Key 与本地部署，必要时发起部署"),
    ("billing", "查询任务费用与项目/人员/模型花费汇总"),
    ("files", "读取用户在对话里上传的附件（文本、代码、压缩包清单）"),
]

SYSTEM_PROMPT = """你是 PRT AI DL Factory 平台内置的 Agent 助手，帮助用户操作平台、排查任务、处理数据。

## 运行环境（平台已经配置好，不要向用户索要平台密码）
- 工作目录：{workdir}
- 平台地址：{platform_url}
- 平台凭据：环境变量 `PRT_PLATFORM_SESSION` 指向一个已登录的会话文件（当前用户：{email}）。
- 平台 Skill：`{skill_dir}`，客户端脚本：`{skill_script}`。
- 用法示例（在 Bash 工具里执行）：
  `python "{skill_script}" request GET /auth/me`
  `python "{skill_script}" schema --search jobs`
  `python "{skill_script}" request GET /jobs --query status=running`

## 边界
- 只在这个工作目录里读写；**不要**访问、修改或提交平台源码仓库，不要执行 git / npm / pip。
- 平台写操作（创建/上传/提交/部署/删除）按用户本轮明确要求执行；破坏性操作先向用户确认。
- 评测数据时用 Skill 按 `/jobs` 的 schema 提交 Harbor 评测；缺少数据集、模型或 GPU 时如实说明。
- 回复用中文，简洁说明你做了什么、资源 ID 与当前状态。{attachments}"""


# --------------------------------------------------------------------------- #
# 查询
# --------------------------------------------------------------------------- #
def _get(conversation_id: str, actor: User) -> AgentConversation:
    item = store.agent_conversations.get(conversation_id)
    # 对别人返回 404 而不是 403：不暴露「这个对话存在」本身
    if item is None or item.owner_id != actor.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="对话不存在")
    return item


def _model_name(model_api_id: str) -> str:
    item = store.model_apis.get(model_api_id)
    return item.name if item else ""


def public(item: AgentConversation) -> AgentConversationPublic:
    return AgentConversationPublic(
        id=item.id,
        title=item.title,
        model_api_id=item.model_api_id,
        model_name=_model_name(item.model_api_id),
        status=item.status,
        last_error=item.last_error,
        message_count=item.message_count,
        attachments=list(item.attachments),
        created_at=item.created_at,
        updated_at=item.updated_at,
    )


def list_conversations(actor: User) -> list[AgentConversationPublic]:
    items = store.agent_conversations.find(lambda i: i.owner_id == actor.id)
    items.sort(key=lambda i: i.updated_at, reverse=True)
    return [public(item) for item in items]


def get_conversation(conversation_id: str, *, actor: User) -> AgentConversationPublic:
    return public(_get(conversation_id, actor))


def list_messages(conversation_id: str, *, actor: User) -> list[AgentMessage]:
    _get(conversation_id, actor)
    items = store.agent_messages.find(lambda m: m.conversation_id == conversation_id)
    items.sort(key=lambda m: m.created_at)
    return items


# --------------------------------------------------------------------------- #
# 模型 / Skill / 工具
# --------------------------------------------------------------------------- #
def _anthropic(item) -> bool:
    return item.provider == "anthropic" or "anthropic" in model_api_service.protocols(item)


def list_models(actor: User) -> list[AgentModelOption]:
    """平台已有模型中，能被 Claude Code 核心直接使用的那一档。"""
    rows: list[AgentModelOption] = []
    for item in store.model_apis.all():
        if not item.enabled or not _anthropic(item):
            continue
        rows.append(
            AgentModelOption(
                id=item.id,
                name=item.name,
                provider=item.provider,
                model_id=item.model_id,
                ready=bool(api_key_service.key_for(item, actor.id).strip()),
                note=item.note,
            )
        )
    rows.sort(key=lambda r: (not r.ready, r.name.lower()))
    return rows


def default_model_id(actor: User) -> str:
    ready = [row for row in list_models(actor) if row.ready]
    return ready[0].id if ready else ""


def _validate_model(model_api_id: str, actor: User) -> str:
    if not model_api_id:
        return ""
    item = store.model_apis.get(model_api_id)
    if item is None or not item.enabled:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="模型配置不存在或已停用")
    if not _anthropic(item):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=f"内置 Agent 使用 Claude Code 核心，只支持 Anthropic 协议模型；「{item.name}」不支持",
        )
    return item.id


def resolve_model(model_api_id: str, actor: User) -> model_api_service.ResolvedModel:
    item = store.model_apis.get(model_api_id)
    if item is None or not item.enabled:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="请先为对话选择模型")
    if not _anthropic(item):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=f"内置 Agent 使用 Claude Code 核心，只支持 Anthropic 协议模型；「{item.name}」不支持",
        )
    return model_api_service.resolve(model_api_id, user_id=actor.id, model_style="bare", protocol="anthropic")


def _skill_path() -> Path:
    return settings.skill_root / SKILL_NAME


def skill_catalog() -> list[AgentSkill]:
    rows: list[AgentSkill] = []
    root = settings.skill_root
    if not root.is_dir():
        return rows
    for directory in sorted(root.iterdir()):
        manifest = directory / "SKILL.md"
        if not directory.is_dir() or not manifest.is_file():
            continue
        name, description = directory.name, ""
        try:
            text = manifest.read_text(encoding="utf-8", errors="replace")
            for line in text.splitlines():
                if line.startswith("name:"):
                    name = line.split(":", 1)[1].strip().strip('"') or name
                elif line.startswith("description:"):
                    description = line.split(":", 1)[1].strip().strip('"')
        except OSError:
            continue
        rows.append(AgentSkill(name=name, description=description, updated_at=_mtime(manifest)))
    return rows


def tool_catalog() -> list[AgentTool]:
    return [AgentTool(name=name, description=description) for name, description in TOOLS]


def _mtime(path: Path) -> datetime | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return None


# --------------------------------------------------------------------------- #
# 创建 / 改名 / 删除
# --------------------------------------------------------------------------- #
def create_conversation(payload: AgentConversationCreate, *, actor: User) -> AgentConversationPublic:
    mine = store.agent_conversations.count(lambda i: i.owner_id == actor.id)
    if mine >= settings.agent_max_conversations:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="对话数量已达上限，请先删除一些历史对话")
    model_api_id = _validate_model(payload.model_api_id, actor) or default_model_id(actor)
    item = AgentConversation(
        owner_id=actor.id,
        owner_email=actor.email,
        title=(payload.title.strip() or "新对话"),
        model_api_id=model_api_id,
        # 会话 ID 只认 Claude Code 自己上报的（见 _handle_line）。平台预先编一个 UUID 的话，
        # 首轮在 Claude 起来之前就失败（找不到 CLI、缺 Skill……）时，之后每一轮都会
        # --resume 一个不存在的会话，这个对话就再也发不出消息了。
        session_id="",
    )
    item.workdir = str(_ensure_workdir(item))
    store.agent_conversations.put(item)
    audit_service.record(
        "agent.create", actor=actor, target_type="agent_conversation", target_id=item.id,
        summary=item.title,
    )
    return public(item)


def update_conversation(
    conversation_id: str, payload: AgentConversationUpdate, *, actor: User
) -> AgentConversationPublic:
    item = _get(conversation_id, actor)
    changes: dict[str, Any] = {}
    if payload.title is not None:
        title = payload.title.strip()
        if not title:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="对话名称不能为空")
        changes["title"] = title[:100]
    if payload.model_api_id is not None:
        changes["model_api_id"] = _validate_model(payload.model_api_id, actor)
    if not changes:
        return public(item)
    changes["updated_at"] = utcnow()
    updated = item.model_copy(update=changes)
    store.agent_conversations.put(updated)
    audit_service.record(
        "agent.update", actor=actor, target_type="agent_conversation", target_id=item.id,
        summary=changes.get("title", item.title),
    )
    return public(updated)


def delete_conversation(conversation_id: str, *, actor: User) -> None:
    item = _get(conversation_id, actor)
    _kill(conversation_id)
    for message in store.agent_messages.find(lambda m: m.conversation_id == item.id):
        store.agent_messages.delete(message.id)
    store.agent_conversations.delete(item.id)
    workdir = _safe_workdir(item)
    if workdir is not None:
        shutil.rmtree(workdir, ignore_errors=True)
    audit_service.record(
        "agent.delete", actor=actor, target_type="agent_conversation", target_id=item.id,
        summary=item.title,
    )


# --------------------------------------------------------------------------- #
# 附件
# --------------------------------------------------------------------------- #
def upload_attachment(
    conversation_id: str, upload: UploadFile, *, actor: User
) -> AgentConversationPublic:
    item = _get(conversation_id, actor)
    filename = Path(upload.filename or "").name
    if not filename or filename in {".", ".."}:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="缺少文件名")
    workdir = _ensure_workdir(item)
    uploads = workdir / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)
    target = uploads / f"{uuid.uuid4().hex[:8]}_{filename}"
    limit = settings.agent_max_upload_mb * 1024 * 1024
    size = 0
    try:
        with target.open("wb") as handle:
            while chunk := upload.file.read(1024 * 1024):
                size += len(chunk)
                if limit and size > limit:
                    raise HTTPException(
                        status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        detail=f"附件超过 {settings.agent_max_upload_mb} MB 上限",
                    )
                handle.write(chunk)
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    attachment = AgentAttachment(
        name=filename,
        path=str(target),
        size_bytes=size,
        content_type=upload.content_type or "application/octet-stream",
    )
    updated = item.model_copy(update={
        "attachments": [*item.attachments, attachment],
        "updated_at": utcnow(),
    })
    store.agent_conversations.put(updated)
    audit_service.record(
        "agent.attachment", actor=actor, target_type="agent_conversation", target_id=item.id,
        summary=filename,
    )
    return public(updated)


def delete_attachment(conversation_id: str, attachment_id: str, *, actor: User) -> AgentConversationPublic:
    item = _get(conversation_id, actor)
    attachment = next((a for a in item.attachments if a.id == attachment_id), None)
    if attachment is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="附件不存在")
    workdir = _safe_workdir(item)
    path = Path(attachment.path)
    if workdir is not None and path.resolve().is_relative_to(workdir):
        path.unlink(missing_ok=True)
    updated = item.model_copy(update={
        "attachments": [a for a in item.attachments if a.id != attachment_id],
        "updated_at": utcnow(),
    })
    store.agent_conversations.put(updated)
    return public(updated)


# --------------------------------------------------------------------------- #
# 发消息
# --------------------------------------------------------------------------- #
def send_message(conversation_id: str, content: str, *, actor: User) -> AgentConversationPublic:
    text = content.strip()
    if not text:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="消息不能为空")
    with _lock:
        item = _get(conversation_id, actor)
        if item.status == AgentStatus.RUNNING:
            raise HTTPException(status.HTTP_409_CONFLICT, detail="上一轮对话还在运行，请稍候或先停止")
        if not item.model_api_id:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="请先为对话选择模型")
        # 先解析模型，确保密钥缺失等问题在写入用户消息之前就返回
        resolved = resolve_model(item.model_api_id, actor)
        _append_message(item.id, role="user", kind="text", content=text)
        count = item.message_count + 1
        changes: dict[str, Any] = {
            "status": AgentStatus.RUNNING,
            "last_error": "",
            "message_count": count,
            "updated_at": utcnow(),
        }
        if count == 1 and item.title in {"", "新对话", "未命名对话"}:
            changes["title"] = _auto_title(text)
        updated = item.model_copy(update=changes)
        store.agent_conversations.put(updated)
        _cancelled.discard(item.id)

    threading.Thread(
        target=_run_turn,
        args=(item.id, text, resolved),
        daemon=True,
        name=f"agent-{item.id}",
    ).start()
    return public(store.agent_conversations.get(item.id) or updated)


def stop_conversation(conversation_id: str, *, actor: User) -> AgentConversationPublic:
    item = _get(conversation_id, actor)
    _cancelled.add(item.id)
    _kill(item.id)
    if item.status == AgentStatus.RUNNING:
        updated = item.model_copy(update={
            "status": AgentStatus.FAILED,
            "last_error": "已停止本轮生成",
            "updated_at": utcnow(),
        })
        store.agent_conversations.put(updated)
        return public(updated)
    return public(item)


def _auto_title(text: str) -> str:
    line = " ".join(text.split())
    return line[:40] + ("…" if len(line) > 40 else "")


def _append_message(
    conversation_id: str,
    *,
    role: str,
    kind: str,
    content: str,
    name: str = "",
) -> AgentMessage:
    message = AgentMessage(
        conversation_id=conversation_id,
        role=role,  # type: ignore[arg-type]
        kind=kind,  # type: ignore[arg-type]
        content=content[:MAX_TOOL_TEXT] if kind != "text" else content,
        name=name,
    )
    with _lock:
        store.agent_messages.put(message)
        item = store.agent_conversations.get(conversation_id)
        if item is not None:
            store.agent_conversations.put(item.model_copy(update={
                "message_count": item.message_count + 1,
                "updated_at": utcnow(),
            }))
    return message


# --------------------------------------------------------------------------- #
# 子进程轮次
# --------------------------------------------------------------------------- #
def _run_turn(conversation_id: str, text: str, resolved: model_api_service.ResolvedModel) -> None:
    item = store.agent_conversations.get(conversation_id)
    user = store.users.get(item.owner_id) if item else None
    if item is None or user is None:
        _finish(conversation_id, status=AgentStatus.FAILED, error="对话或用户已不存在")
        return
    workdir = _ensure_workdir(item)
    try:
        skill_script = _install_skill(workdir)
        session_path = _write_session(workdir, user)
        prompt = _build_prompt(item, text, workdir, session_path, skill_script)
        command = _command(item, prompt, resolved)
        env = _agent_env(workdir, resolved, session_path, skill_script)
        log_path = workdir / "agent.log"
        state: dict[str, Any] = {"result": None, "last_text": "", "tool": ""}
        timed_out = threading.Event()
        with _lock:
            process = subprocess.Popen(  # noqa: S603 - argv 由本模块构造，不经 shell
                command,
                cwd=workdir,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                start_new_session=True,
            )
            _processes[conversation_id] = process
        def _timeout() -> None:
            timed_out.set()
            _kill(conversation_id)

        timer = threading.Timer(settings.agent_timeout_seconds, _timeout)
        timer.daemon = True
        timer.start()
        with log_path.open("ab") as log:
            log.write((f"\n===== {utcnow().isoformat()} =====\n").encode())
            log.flush()
            try:
                assert process.stdout is not None
                for line in process.stdout:
                    log.write(line.encode("utf-8", "replace"))
                    log.flush()
                    _handle_line(conversation_id, line, state)
                process.wait()
            finally:
                timer.cancel()
                if process.poll() is None:
                    _kill(conversation_id)
        if timed_out.is_set():
            _finish(conversation_id, status=AgentStatus.FAILED,
                    error=f"Agent 超过 {settings.agent_timeout_seconds:.0f} 秒未完成，已终止")
        elif conversation_id in _cancelled:
            _finish(conversation_id, status=AgentStatus.FAILED, error="已停止本轮生成")
        elif state.get("error"):
            _finish(conversation_id, status=AgentStatus.FAILED, error=str(state["error"]))
        elif state["result"] is None and process.returncode != 0:
            tail = _tail(log_path)
            _finish(conversation_id, status=AgentStatus.FAILED,
                    error=f"Agent 进程异常退出（{process.returncode}）{tail}")
        else:
            result = state["result"]
            if isinstance(result, str) and result.strip() and result.strip() != state["last_text"].strip():
                _append_message(conversation_id, role="assistant", kind="text", content=result.strip())
            _finish(conversation_id, status=AgentStatus.IDLE)
    except HTTPException as exc:
        _finish(conversation_id, status=AgentStatus.FAILED, error=str(exc.detail))
    except FileNotFoundError:
        _finish(conversation_id, status=AgentStatus.FAILED,
                error=f"本机未找到 Claude Code（{settings.agent_bin}），请配置 PRT_AGENT_BIN")
    except Exception as exc:  # 任何异常都不能让对话永远停在 running
        logger.exception("agent conversation %s failed", conversation_id)
        _finish(conversation_id, status=AgentStatus.FAILED, error=f"Agent 运行失败：{exc}")
    finally:
        with _lock:
            _processes.pop(conversation_id, None)
            _cancelled.discard(conversation_id)
        # 兜底：正常情况下 _finish 已经改过状态，这里只处理极端的半途异常
        current = store.agent_conversations.get(conversation_id)
        if current is not None and current.status == AgentStatus.RUNNING:
            _finish(conversation_id, status=AgentStatus.FAILED, error="Agent 进程已结束但没有返回结果")


def _finish(conversation_id: str, *, status: AgentStatus, error: str = "") -> None:
    with _lock:
        item = store.agent_conversations.get(conversation_id)
        if item is None:
            return
        store.agent_conversations.put(item.model_copy(update={
            "status": status,
            "last_error": error[:2000],
            "updated_at": utcnow(),
        }))
    if error:
        _append_message(conversation_id, role="assistant", kind="error", content=error)


def _handle_line(conversation_id: str, line: str, state: dict[str, Any]) -> None:
    line = line.strip()
    if not line.startswith("{"):
        return
    try:
        event = json.loads(line)
    except ValueError:
        return
    if not isinstance(event, dict):
        return
    session_id = event.get("session_id")
    if isinstance(session_id, str) and session_id:
        _update_session(conversation_id, session_id)
    kind = event.get("type")
    if kind == "assistant":
        message = event.get("message") or {}
        _handle_blocks(conversation_id, message.get("content"), state, assistant=True)
    elif kind == "user":
        message = event.get("message") or {}
        _handle_blocks(conversation_id, message.get("content"), state, assistant=False)
    elif kind == "result":
        result = event.get("result")
        if event.get("is_error"):
            state["result"] = ""
            state["error"] = str(result or event.get("subtype") or "Agent 返回错误")
        elif isinstance(result, str):
            state["result"] = result
        session_id = event.get("session_id")
        if isinstance(session_id, str) and session_id:
            _update_session(conversation_id, session_id)
        state["result"] = state["result"] if state["result"] is not None else ""


def _handle_blocks(
    conversation_id: str, blocks: Any, state: dict[str, Any], *, assistant: bool
) -> None:
    if isinstance(blocks, str):
        blocks = [{"type": "text", "text": blocks}]
    if not isinstance(blocks, list):
        return
    for block in blocks:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "text" and assistant:
            text = str(block.get("text") or "").strip()
            if text:
                state["last_text"] = text
                _append_message(conversation_id, role="assistant", kind="text", content=text)
        elif block_type == "tool_use" and assistant:
            name = str(block.get("name") or "tool")
            state["tool"] = name
            try:
                payload = json.dumps(block.get("input") or {}, ensure_ascii=False)
            except (TypeError, ValueError):
                payload = str(block.get("input") or "")
            _append_message(
                conversation_id, role="tool", kind="tool_call",
                content=payload[:MAX_TOOL_INPUT], name=name,
            )
        elif block_type == "tool_result" and not assistant:
            _append_message(
                conversation_id, role="tool", kind="tool_result",
                content=_flatten(block.get("content")), name=state.get("tool", "tool"),
            )


def _flatten(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or ""))
            else:
                parts.append(str(item))
        return "\n".join(part for part in parts if part)
    return str(value or "")


def _update_session(conversation_id: str, session_id: str) -> None:
    with _lock:
        item = store.agent_conversations.get(conversation_id)
        if item is not None and item.session_id != session_id:
            store.agent_conversations.put(item.model_copy(update={"session_id": session_id}))


def _command(item: AgentConversation, prompt: str, resolved: model_api_service.ResolvedModel) -> list[str]:
    agent = shutil.which(settings.agent_bin) or (
        settings.agent_bin if Path(settings.agent_bin).is_file() else settings.agent_bin
    )
    command = [
        agent,
        "-p", prompt,
        "--output-format", "stream-json",
        "--verbose",
        "--model", resolved.model,
        "--permission-mode", settings.agent_permission_mode,
    ]
    if item.session_id:
        command += ["--resume", item.session_id]
    if settings.agent_allowed_tools.strip():
        command += ["--allowedTools", settings.agent_allowed_tools]
    if settings.agent_disallowed_tools.strip():
        command += ["--disallowedTools", settings.agent_disallowed_tools]
    return command


def _agent_env(
    workdir: Path,
    resolved: model_api_service.ResolvedModel,
    session_path: Path,
    skill_script: Path,
) -> dict[str, str]:
    from .proc_env import with_interpreter_on_path

    # 平台自己的 PRT_* 配置（含密钥、数据目录）不能漏进 Agent 子进程
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("PRT_") and not key.startswith("ANTHROPIC_")
    }
    env.update(resolved.env)
    env.update({
        "PYTHONUNBUFFERED": "1",
        "PRT_PLATFORM_URL": settings.agent_api_url,
        "PRT_PLATFORM_SESSION": str(session_path),
        "PRT_API": str(skill_script),
    })
    return with_interpreter_on_path(env, sys.executable)


def _build_prompt(
    item: AgentConversation,
    text: str,
    workdir: Path,
    session_path: Path,
    skill_script: Path,
) -> str:
    attachments = ""
    if item.attachments:
        lines = [f"\n- {a.name}（{a.size_bytes} 字节）：{a.path}" for a in item.attachments]
        attachments = "\n\n## 用户上传的附件（可用 Read 工具读取）" + "".join(lines)
    system = SYSTEM_PROMPT.format(
        workdir=workdir,
        platform_url=settings.agent_api_url,
        email=item.owner_email,
        skill_dir=workdir / ".claude" / "skills" / SKILL_NAME,
        skill_script=skill_script,
        attachments=attachments,
    )
    return f"{system}\n\n## 用户消息\n{text}"


def _install_skill(workdir: Path) -> Path:
    """把仓库里的 Skill 复制进工作目录，确保 Agent 用的是当前版本。"""
    source = _skill_path()
    target = workdir / ".claude" / "skills" / SKILL_NAME
    if not source.is_dir():
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"缺少平台 Skill：{source}")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        source, target, dirs_exist_ok=True,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    script = target / "scripts" / "prt_api.py"
    if not script.is_file():
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, detail="平台 Skill 缺少 scripts/prt_api.py")
    return script


def _write_session(workdir: Path, user: User) -> Path:
    from ..security import create_access_token

    ttl = max(5, min(settings.access_token_ttl_minutes, 120))
    token, expires_in = create_access_token(
        user.id, role=user.role.value, email=user.email, ttl_minutes=ttl
    )
    path = workdir / ".prt-session.json"
    payload = {
        "base_url": settings.agent_api_url,
        "token": token,
        "expires_at": time.time() + expires_in,
        "user": {"id": user.id, "email": user.email, "role": user.role.value},
    }
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False)
    return path


def _workspace_root() -> Path:
    # 默认值 ``~/prt-agent-workspaces/agent`` 不经过 settings 的 validator，必须自己展开 ``~``，
    # 否则会在平台进程的 cwd（也就是平台仓库）里建出一个名叫 ``~`` 的目录。
    return settings.agent_workspace_root.expanduser().resolve()


def _ensure_workdir(item: AgentConversation) -> Path:
    path = Path(item.workdir).expanduser() if item.workdir else _workspace_root() / item.id
    path = path.resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def _safe_workdir(item: AgentConversation) -> Path | None:
    if not item.workdir:
        return None
    root = _workspace_root()
    path = Path(item.workdir).expanduser().resolve()
    return path if path.is_relative_to(root) else None


def _kill(conversation_id: str) -> None:
    with _lock:
        process = _processes.get(conversation_id)
    if process is None or process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def _tail(path: Path, limit: int = 1200) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    lines = [line for line in text.splitlines() if line.strip() and not line.startswith("{")]
    return "：" + "\n".join(lines[-6:])[-limit:] if lines else ""


def reconcile() -> int:
    """平台重启后，内存里的子进程早已不在，把仍在 running 的对话标记失败。"""
    count = 0
    for item in store.agent_conversations.find(lambda i: i.status == AgentStatus.RUNNING):
        store.agent_conversations.put(item.model_copy(update={
            "status": AgentStatus.FAILED,
            "last_error": "平台重启，上一轮对话已中断",
            "updated_at": utcnow(),
        }))
        count += 1
    return count


__all__ = [
    "list_conversations",
    "get_conversation",
    "list_messages",
    "create_conversation",
    "update_conversation",
    "delete_conversation",
    "send_message",
    "stop_conversation",
    "upload_attachment",
    "delete_attachment",
    "list_models",
    "skill_catalog",
    "tool_catalog",
    "reconcile",
]
