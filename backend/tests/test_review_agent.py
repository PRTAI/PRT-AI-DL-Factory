"""Agent 真实进程生命周期与 Harbor 原生配置契约；不访问外部模型。"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import psutil
import pytest

from backend.app.builders import review as builder
from backend.app.builders.base import BuildResult
from backend.app.config import settings
from backend.app.models import ModelApiConfig, ReviewTask
from backend.app.services import review_agent_service as runner
from backend.app.store import store
from .conftest import grant_key, project_id, user_id_of


@pytest.fixture
def review_item(monkeypatch):
    owner = user_id_of("admin@prt.local")
    config = ModelApiConfig(name="review-agent-test", model="deepseek-flash[1m]", provider="anthropic", base_url="https://api.deepseek.com/anthropic")
    store.model_apis.put(config)
    grant_key(owner, config.id, "sk-private-review-test")
    item = ReviewTask(name="Agent 核查", agent="claude-code", model_api_id=config.id, owner_id=owner, project_id=project_id())
    base = settings.reviews_dir / item.workspace
    (base / "docs").mkdir(parents=True)
    (base / "extracted").mkdir()
    (base / "docs" / "规范.md").write_text("原始规范", encoding="utf-8")
    (base / "manifest.json").write_text('{"tasks": []}')
    monkeypatch.setattr(settings, "harbor_python", sys.executable)
    return item, base


@pytest.mark.parametrize("agent,protocol,key", [
    ("claude-code", "anthropic", "ANTHROPIC_API_KEY"), ("codex", "responses", "OPENAI_API_KEY"),
])
def test_agent_build_round_trips_through_native_harbor(review_item, agent, protocol, key):
    item, base = review_item
    item.agent = agent
    config = store.model_apis.get(item.model_api_id)
    if agent == "codex":
        config.provider = "openai"
        config.base_url = "https://example.test/v1"
        store.model_apis.put(config)
    call = base / "agents" / "review-test-config"
    result = builder.build(item, call, base, system="审核规则", user="读取文件并输出 JSON")
    assert "sk-private-review-test" not in result.command_line
    assert "sk-private-review-test" not in json.dumps(result.files)
    assert "sk-private-review-test" not in json.dumps(result.masked_env())
    for name, body in result.files.items():
        path = Path(result.cwd) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    proc = subprocess.run([*result.command, "--print-config"], env={**os.environ, **result.env}, cwd=result.cwd, capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    native = json.loads(proc.stdout[proc.stdout.index("{"):])
    assert native["agents"][0]["name"] == agent
    assert native["agents"][0]["model_name"] == "deepseek-flash[1m]"
    assert key in native["agents"][0]["env"]
    assert native["verifier"]["disable"] is True
    assert native["environment"]["type"] == "docker"
    assert native["environment"]["override_gpus"] == 0
    mounts = native["environment"]["mounts"]
    assert {m["target"] for m in mounts} == {"/review/docs", "/review/data", "/review/manifest.json", "/review/request.md"}
    assert all(m["read_only"] for m in mounts)
    assert not any("docker.sock" in m["source"] or m["source"] == str(settings.data_root) for m in mounts)
    from harbor.models.task.task import Task
    task = Task(call / "task", disable_verification=True)
    assert task.config.agent.timeout_sec == settings.review_agent_timeout_seconds
    assert task.config.environment.docker_image == settings.review_agent_image
    assert "/logs/agent/review-result.json" in task.instruction


def _native_result(call: Path, *, exception=None, text='{"checks": []}'):
    trial = call / "runs" / call.name / "task__trial"
    (trial / "agent").mkdir(parents=True)
    (trial / "result.json").write_text(json.dumps({"exception_info": exception, "agent_result": {"n_input_tokens": 10, "n_output_tokens": 20}}))
    (trial / "agent" / "review-result.json").write_text(text)
    return trial


def test_native_failure_cannot_be_hidden_by_existing_output(tmp_path):
    _native_result(tmp_path, exception={"exception_type": "AgentTimeoutError", "exception_message": "timed out"})
    with pytest.raises(runner.ReviewAgentError, match="AgentTimeoutError"):
        runner.read_result(tmp_path)


def test_result_symlink_cannot_read_host_files(tmp_path):
    call = tmp_path / "call"
    trial = _native_result(call)
    result = trial / "agent" / "review-result.json"
    result.unlink()
    other = tmp_path / "private.json"
    other.write_text('{"private": true}')
    result.symlink_to(other)
    with pytest.raises(runner.ReviewAgentError, match="越界"):
        runner.read_result(call)


def test_agent_process_output_and_secret_redaction(monkeypatch, review_item):
    item, base = review_item

    def fake_build(entry, call, source, **kwargs):
        _native_result(call)
        return BuildResult(command=[sys.executable, "-u", "-c", "print('tool read completed sk-private-review-test')"], cwd=str(call), output_dir=str(call / "runs"), secrets=["sk-private-review-test"])

    monkeypatch.setattr(builder, "build", fake_build)
    text, usage = asyncio.run(runner.run(item, system="system", user="user", tag="plan", attempt=1))
    assert json.loads(text) == {"checks": []}
    assert usage == {"prompt_tokens": 10, "completion_tokens": 20}
    log = runner.log_tail(item, 64000)
    assert "tool read completed ***" in log
    assert "sk-private-review-test" not in log
    assert len(list((base / "agents").glob("review-*/process.json"))) == 1


def test_cancel_stops_the_agent_process(monkeypatch, review_item):
    item, base = review_item

    def fake_build(entry, call, source, **kwargs):
        return BuildResult(command=[sys.executable, "-u", "-c", "import time; print('ready', flush=True); time.sleep(60)"], cwd=str(call), output_dir=str(call / "runs"))

    monkeypatch.setattr(builder, "build", fake_build)

    async def scenario():
        task = asyncio.create_task(runner.run(item, system="system", user="user", tag="plan", attempt=1))
        for _ in range(1000):
            if "ready" in runner.log_tail(item, 64000):
                break
            await asyncio.sleep(0.01)
        else:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            pytest.fail("test process did not start")
        metadata = json.loads(next((base / "agents").glob("review-*/process.json")).read_text())
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not psutil.pid_exists(metadata["pid"])

    asyncio.run(scenario())


@pytest.mark.parametrize("mismatch", ["creation_time", "command", "process_group", "none"])
def test_restart_cleanup_checks_process_identity(monkeypatch, review_item, mismatch):
    item, base = review_item
    call = base / "agents" / "review-restart-test"
    call.mkdir(parents=True)
    (call / "process.json").write_text(json.dumps({"pid": 12345, "create_time": 100.0}))

    class Process:
        pid = 12345

        def create_time(self):
            return 101.0 if mismatch == "creation_time" else 100.0

        def cmdline(self):
            return [sys.executable, "unrelated.py" if mismatch == "command" else str(call / "work" / "harbor_runtime.py")]

        def wait(self, timeout):
            return 0

    signals = []
    monkeypatch.setattr(runner.psutil, "Process", lambda _: Process())
    monkeypatch.setattr(runner.os, "getpgid", lambda _: 777 if mismatch == "process_group" else 12345)
    monkeypatch.setattr(runner.os, "killpg", lambda pid, sig: signals.append((pid, sig)))
    asyncio.run(runner.reap_orphans(item))
    assert len(signals) == (1 if mismatch == "none" else 0)
