"""内置 Agent：对话隔离、改名/删除、附件、Claude Code 轮次与 Skill 清单。"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

from backend.app.config import settings
from backend.app.models import AgentConversation, AgentStatus
from backend.app.store import store

FAKE_AGENT = """#!{python}
import json, os, sys
args = sys.argv[1:]
prompt = args[args.index("-p") + 1]
session = os.environ.get("PRT_PLATFORM_SESSION", "")
assert session and os.path.exists(session), "平台没有注入会话文件"
assert os.environ.get("ANTHROPIC_API_KEY") == "test-key", "平台没有注入模型密钥"
assert "Bash(git:*)" in " ".join(args), "Agent 没有禁用 git"
assert os.environ.get("PRT_PLATFORM_URL"), "平台没有注入平台地址"
with open(os.environ["FAKE_ARGV_LOG"], "a", encoding="utf-8") as handle:
    handle.write(json.dumps(args, ensure_ascii=False) + "\\n")
sid = "11111111-2222-3333-4444-555555555555"
print(json.dumps({{"type": "system", "subtype": "init", "session_id": sid}}))
reply = "收到：" + prompt.split("## 用户消息")[-1].strip()
print(json.dumps({{"type": "assistant", "message": {{"content": [{{"type": "text", "text": reply}}]}}}}))
print(json.dumps({{"type": "assistant", "message": {{"content": [
    {{"type": "tool_use", "name": "platform_api", "input": {{"path": "/projects"}}}}]}}}}))
print(json.dumps({{"type": "user", "message": {{"content": [
    {{"type": "tool_result", "content": [{{"type": "text", "text": "{{\\"items\\": []}}"}}]}}]}}}}))
print(json.dumps({{"type": "result", "subtype": "success", "result": "", "session_id": sid}}))
"""


@pytest.fixture
def fake_agent(tmp_path, monkeypatch):
    agent = tmp_path / "bin" / "claude"
    agent.parent.mkdir()
    agent.write_text(FAKE_AGENT.format(python=sys.executable))
    agent.chmod(0o755)
    log = tmp_path / "argv.log"
    monkeypatch.setattr(settings, "agent_bin", str(agent))
    monkeypatch.setattr(settings, "agent_workspace_root", tmp_path / "workspaces")
    monkeypatch.setenv("FAKE_ARGV_LOG", str(log))
    return {"agent": agent, "log": log}


def _anthropic_model():
    from backend.app.services import model_api_service

    item = next(
        (i for i in store.model_apis.all() if model_api_service.protocols(i) == ["anthropic"]),
        None,
    )
    assert item is not None, "测试环境没有 Anthropic 模型配置"
    return item


def _grant(model, key: str = "test-key") -> None:
    from backend.app.services import api_key_service
    from backend.tests.conftest import grant_key, user_id_of

    grant_key(user_id_of("dev@prt.local"), api_key_service.target_of(model), key)


def _wait(conversation_id: str, timeout: float = 20) -> AgentConversation:
    deadline = time.time() + timeout
    while time.time() < deadline:
        item = store.agent_conversations.get(conversation_id)
        if item is not None and item.status != AgentStatus.RUNNING:
            return item
        time.sleep(0.05)
    raise AssertionError("agent conversation stayed running")


def _create(client, headers, model_api_id: str = "", title: str = "测试对话") -> dict:
    response = client.post(
        "/api/agent/conversations", headers=headers,
        json={"title": title, "model_api_id": model_api_id},
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_conversation_crud_and_isolation(client, member_headers, admin_headers, fake_agent):
    created = _create(client, member_headers)
    conversation_id = created["id"]
    assert created["status"] == "idle"

    mine = client.get("/api/agent/conversations", headers=member_headers)
    assert [row["id"] for row in mine.json()] == [conversation_id]
    # 其他账号（包括管理员）看不到，也拿不到详情
    assert client.get("/api/agent/conversations", headers=admin_headers).json() == []
    assert client.get(f"/api/agent/conversations/{conversation_id}", headers=admin_headers).status_code == 404
    assert client.delete(f"/api/agent/conversations/{conversation_id}", headers=admin_headers).status_code == 404

    renamed = client.patch(
        f"/api/agent/conversations/{conversation_id}", headers=member_headers,
        json={"title": "  改个名字  "},
    )
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["title"] == "改个名字"

    assert client.delete(f"/api/agent/conversations/{conversation_id}", headers=member_headers).status_code == 204
    assert client.get(f"/api/agent/conversations/{conversation_id}", headers=member_headers).status_code == 404
    assert client.get(f"/api/agent/conversations/{conversation_id}/messages", headers=member_headers).status_code == 404


def test_send_message_runs_agent_and_resumes_session(client, member_headers, fake_agent):
    model = _anthropic_model()
    _grant(model)
    created = _create(client, member_headers, model.id, title="新对话")

    response = client.post(
        f"/api/agent/conversations/{created['id']}/messages", headers=member_headers,
        json={"content": "帮我看看有哪些项目"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "running"
    item = _wait(created["id"])
    assert item.status == AgentStatus.IDLE, item.last_error
    assert item.title.startswith("帮我看看有哪些项目")
    assert item.session_id == "11111111-2222-3333-4444-555555555555"

    messages = client.get(
        f"/api/agent/conversations/{created['id']}/messages", headers=member_headers
    ).json()
    roles = [(row["role"], row["kind"]) for row in messages]
    assert ("user", "text") in roles
    assert ("assistant", "text") in roles
    assert ("tool", "tool_call") in roles
    assert ("tool", "tool_result") in roles
    assert any("帮我看看有哪些项目" in row["content"] for row in messages if row["role"] == "assistant")

    # 第二轮沿用 Claude Code 的会话，不再从头开始
    second = client.post(
        f"/api/agent/conversations/{created['id']}/messages", headers=member_headers,
        json={"content": "继续"},
    )
    assert second.status_code == 200, second.text
    _wait(created["id"])
    calls = [json.loads(line) for line in fake_agent["log"].read_text().splitlines() if line]
    assert len(calls) == 2
    assert "--resume" in calls[1]
    assert calls[1][calls[1].index("--resume") + 1] == item.session_id


def test_send_without_key_rejected_before_writing(client, member_headers, fake_agent):
    model = _anthropic_model()
    created = _create(client, member_headers, model.id)
    # 清掉可能来自其他用例的密钥后发送
    from backend.app.models import UserApiKey
    from backend.app.services import api_key_service
    from backend.tests.conftest import user_id_of

    store.user_api_keys.delete(UserApiKey.key_id(user_id_of("dev@prt.local"), api_key_service.target_of(model)))
    response = client.post(
        f"/api/agent/conversations/{created['id']}/messages", headers=member_headers,
        json={"content": "你好"},
    )
    assert response.status_code == 400
    assert "API Key" in response.json()["detail"]
    assert client.get(
        f"/api/agent/conversations/{created['id']}/messages", headers=member_headers
    ).json() == []
    assert store.agent_conversations.get(created["id"]).status == AgentStatus.IDLE


def test_attachment_upload_and_delete(client, member_headers, fake_agent, tmp_path):
    created = _create(client, member_headers)
    response = client.post(
        f"/api/agent/conversations/{created['id']}/attachments", headers=member_headers,
        files={"file": ("notes.txt", b"hello agent", "text/plain")},
    )
    assert response.status_code == 200, response.text
    attachments = response.json()["attachments"]
    assert len(attachments) == 1
    assert attachments[0]["name"] == "notes.txt"
    path = Path(attachments[0]["path"])
    assert path.is_file() and path.read_text() == "hello agent"

    removed = client.delete(
        f"/api/agent/conversations/{created['id']}/attachments/{attachments[0]['id']}",
        headers=member_headers,
    )
    assert removed.status_code == 200
    assert removed.json()["attachments"] == []
    assert not path.exists()


def test_models_skills_and_tools_are_user_scoped(client, member_headers):
    model = _anthropic_model()
    _grant(model)
    models = client.get("/api/agent/models", headers=member_headers)
    assert models.status_code == 200
    rows = models.json()
    assert any(row["id"] == model.id and row["ready"] for row in rows)
    # 只支持 OpenAI 协议的模型不能作为 Claude Code 核心的模型
    from backend.app.services import model_api_service

    openai_only = next(
        (i for i in store.model_apis.all() if model_api_service.protocols(i) == ["openai"]), None
    )
    if openai_only is not None:
        assert all(row["id"] != openai_only.id for row in rows)

    skills = client.get("/api/agent/skills", headers=member_headers).json()
    assert any(row["name"] == "prt-platform" for row in skills)
    tools = client.get("/api/agent/tools", headers=member_headers).json()
    assert {row["name"] for row in tools} >= {"platform_api", "datasets", "jobs"}


def test_reconcile_marks_running_conversations_failed(client, member_headers, fake_agent):
    from backend.app.services import agent_service

    created = _create(client, member_headers)
    item = store.agent_conversations.get(created["id"])
    store.agent_conversations.put(item.model_copy(update={"status": AgentStatus.RUNNING}))
    assert agent_service.reconcile() >= 1
    assert store.agent_conversations.get(created["id"]).status == AgentStatus.FAILED
