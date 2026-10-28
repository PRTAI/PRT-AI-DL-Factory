"""平台内置 Agent：每个账号自己的对话、消息与附件。

对话之间完全隔离：所有查询都按 ``owner_id`` 过滤，管理员也看不到别人的对话。
Agent 的「大脑」是本机 Claude Code，运行在平台仓库之外的独立工作目录里；
平台凭据按对话用户签发短期 token，通过 skill 的 HTTP 客户端操作平台。
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import Field

from .common import Base, new_id, utcnow


class AgentStatus(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    FAILED = "failed"


class AgentAttachment(Base):
    id: str = Field(default_factory=lambda: new_id("att"))
    name: str
    path: str  # 工作目录内的绝对路径，只用于本机 Agent 读取
    size_bytes: int = 0
    content_type: str = ""
    created_at: datetime = Field(default_factory=utcnow)


class AgentConversation(Base):
    id: str = Field(default_factory=lambda: new_id("conv"))
    owner_id: str
    owner_email: str = ""
    title: str = "新对话"
    model_api_id: str = ""
    session_id: str = ""  # Claude Code 会话 ID，用于 --resume
    workdir: str = ""
    status: AgentStatus = AgentStatus.IDLE
    last_error: str = ""
    message_count: int = 0
    attachments: list[AgentAttachment] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    @property
    def is_running(self) -> bool:
        return self.status == AgentStatus.RUNNING


class AgentMessage(Base):
    id: str = Field(default_factory=lambda: new_id("msg"))
    conversation_id: str
    role: Literal["user", "assistant", "tool"] = "assistant"
    # text：正常对话；tool_call / tool_result：Agent 调用的工具；error：失败信息
    kind: Literal["text", "tool_call", "tool_result", "error"] = "text"
    content: str = ""
    name: str = ""  # 工具名
    created_at: datetime = Field(default_factory=utcnow)


class AgentConversationPublic(Base):
    id: str
    title: str
    model_api_id: str
    model_name: str = ""
    status: AgentStatus
    last_error: str = ""
    message_count: int
    attachments: list[AgentAttachment]
    created_at: datetime
    updated_at: datetime


class AgentConversationCreate(Base):
    title: str = Field(default="新对话", max_length=100)
    model_api_id: str = ""


class AgentConversationUpdate(Base):
    title: str | None = Field(default=None, min_length=1, max_length=100)
    model_api_id: str | None = None


class AgentTurnRequest(Base):
    content: str = Field(min_length=1, max_length=40_000)


class AgentModelOption(Base):
    id: str
    name: str
    provider: str
    model_id: str
    ready: bool
    note: str = ""


class AgentSkill(Base):
    name: str
    description: str
    updated_at: datetime | None = None


class AgentTool(Base):
    name: str
    description: str
