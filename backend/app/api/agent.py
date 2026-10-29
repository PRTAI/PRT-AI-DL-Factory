"""内置 Agent：对话、消息、附件、模型与技能清单。

所有路由都要求登录；对话只能被创建者看到和操作（服务层按 owner_id 隔离）。
"""

from __future__ import annotations

from fastapi import APIRouter, File, UploadFile

from ..deps import CurrentUser
from ..models import (
    AgentConversationCreate,
    AgentConversationPublic,
    AgentConversationUpdate,
    AgentMessage,
    AgentModelOption,
    AgentSkill,
    AgentTool,
    AgentTurnRequest,
)
from ..services import agent_service

router = APIRouter(prefix="/agent", tags=["agent"])


@router.get("/conversations", response_model=list[AgentConversationPublic], summary="我的 Agent 对话")
def list_conversations(user: CurrentUser) -> list[AgentConversationPublic]:
    return agent_service.list_conversations(user)


@router.post(
    "/conversations", response_model=AgentConversationPublic, status_code=201, summary="新建对话"
)
def create_conversation(
    user: CurrentUser, payload: AgentConversationCreate | None = None
) -> AgentConversationPublic:
    return agent_service.create_conversation(payload or AgentConversationCreate(), actor=user)


@router.get(
    "/conversations/{conversation_id}",
    response_model=AgentConversationPublic,
    summary="对话详情",
)
def get_conversation(conversation_id: str, user: CurrentUser) -> AgentConversationPublic:
    return agent_service.get_conversation(conversation_id, actor=user)


@router.patch(
    "/conversations/{conversation_id}",
    response_model=AgentConversationPublic,
    summary="改名或切换模型",
)
def update_conversation(
    conversation_id: str, payload: AgentConversationUpdate, user: CurrentUser
) -> AgentConversationPublic:
    return agent_service.update_conversation(conversation_id, payload, actor=user)


@router.delete("/conversations/{conversation_id}", status_code=204, summary="删除对话")
def delete_conversation(conversation_id: str, user: CurrentUser) -> None:
    agent_service.delete_conversation(conversation_id, actor=user)


@router.get(
    "/conversations/{conversation_id}/messages",
    response_model=list[AgentMessage],
    summary="对话消息",
)
def list_messages(conversation_id: str, user: CurrentUser) -> list[AgentMessage]:
    return agent_service.list_messages(conversation_id, actor=user)


@router.post(
    "/conversations/{conversation_id}/messages",
    response_model=AgentConversationPublic,
    summary="发送消息（后台启动 Agent）",
)
def send_message(
    conversation_id: str, payload: AgentTurnRequest, user: CurrentUser
) -> AgentConversationPublic:
    return agent_service.send_message(conversation_id, payload.content, actor=user)


@router.post(
    "/conversations/{conversation_id}/stop",
    response_model=AgentConversationPublic,
    summary="停止本轮生成",
)
def stop_conversation(conversation_id: str, user: CurrentUser) -> AgentConversationPublic:
    return agent_service.stop_conversation(conversation_id, actor=user)


@router.post(
    "/conversations/{conversation_id}/attachments",
    response_model=AgentConversationPublic,
    summary="上传附件给 Agent",
)
def upload_attachment(
    conversation_id: str, user: CurrentUser, file: UploadFile = File(...)
) -> AgentConversationPublic:
    return agent_service.upload_attachment(conversation_id, file, actor=user)


@router.delete(
    "/conversations/{conversation_id}/attachments/{attachment_id}",
    response_model=AgentConversationPublic,
    summary="删除附件",
)
def delete_attachment(
    conversation_id: str, attachment_id: str, user: CurrentUser
) -> AgentConversationPublic:
    return agent_service.delete_attachment(conversation_id, attachment_id, actor=user)


@router.get("/models", response_model=list[AgentModelOption], summary="Agent 可用模型")
def list_models(user: CurrentUser) -> list[AgentModelOption]:
    return agent_service.list_models(user)


@router.get("/skills", response_model=list[AgentSkill], summary="Agent 可用的平台 Skill")
def list_skills(_: CurrentUser) -> list[AgentSkill]:
    return agent_service.skill_catalog()


@router.get("/tools", response_model=list[AgentTool], summary="Agent 可用的平台工具")
def list_tools(_: CurrentUser) -> list[AgentTool]:
    return agent_service.tool_catalog()
