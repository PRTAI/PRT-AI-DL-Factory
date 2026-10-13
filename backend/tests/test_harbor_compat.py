"""Reward Kit isolation stays independent when overlay mounts are unavailable."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType

import pytest

from backend.app.config import settings
from backend.app.services import harbor_compat


def _runner():
    spec = importlib.util.spec_from_file_location("prt_rewardkit_runner", settings.resources_dir / "rewardkit_runner.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def isolation_modules(monkeypatch):
    isolation = ModuleType("rewardkit.isolation")
    calls = []

    @contextmanager
    def original(path):
        calls.append(path)
        raise subprocess.CalledProcessError(32, ["mount", "-t", "overlay"])
        yield  # noqa: unreachable -- contextmanager fixture

    isolation.isolate = original
    reward = ModuleType("rewardkit.reward")
    reward.isolate = original
    package = ModuleType("rewardkit")
    package.__path__ = []
    package.isolation = isolation
    for name, module in (("rewardkit", package), ("rewardkit.isolation", isolation), ("rewardkit.reward", reward)):
        monkeypatch.setitem(sys.modules, name, module)
    return isolation, reward, calls


def test_unprivileged_judges_get_separate_copies_and_cleanup(tmp_path, monkeypatch, isolation_modules):
    isolation, reward, calls = isolation_modules
    source = tmp_path / "workspace"
    source.mkdir()
    (source / "answer.txt").write_text("original", encoding="utf-8")
    (source / "linked.txt").symlink_to(source / "answer.txt")
    module = _runner()
    monkeypatch.setattr(module, "_can_mount", lambda: False)
    module.install_fallback()
    with isolation.isolate(source) as first, reward.isolate(source) as second:
        assert first != second
        (first / "answer.txt").write_text("first judge", encoding="utf-8")
        (first / "linked.txt").write_text("linked write", encoding="utf-8")
        assert (second / "answer.txt").read_text() == "original"
        assert (source / "answer.txt").read_text() == "original"
    assert not first.parent.exists() and not second.parent.exists()
    assert calls == []  # 无挂载权限时直接使用副本，避免反复尝试 mount/apt。


def test_failed_mount_falls_back_once_and_judge_errors_propagate(tmp_path, monkeypatch, isolation_modules):
    isolation, _, calls = isolation_modules
    source = tmp_path / "workspace"
    source.mkdir()
    module = _runner()
    monkeypatch.setattr(module, "_can_mount", lambda: True)
    module.install_fallback()
    with isolation.isolate(source) as snapshot:
        assert snapshot.is_dir()
    with pytest.raises(OSError, match="judge failure"):
        with isolation.isolate(source) as failed:
            raise OSError("judge failure")
    assert calls == [source]
    assert not failed.parent.exists()


def test_rewardkit_task_adapter_preserves_criteria_and_is_idempotent(tmp_path):
    task = tmp_path / "task"
    tests = task / "tests"
    tests.mkdir(parents=True)
    (task / "task.toml").write_text('version = "1.0"\n', encoding="utf-8")
    judge = tests / "judge.toml"
    criteria = '[judge]\nisolated = true\n[[criterion]]\ndescription = "原评分标准"\n'
    judge.write_text(criteria, encoding="utf-8")
    script = tests / "test.sh"
    script.write_text('#!/bin/bash\n# python3 -m rewardkit example\nif ! python3 -m rewardkit /tests --workspace /app; then exit 1; fi\n', encoding="utf-8")
    script.chmod(0o755)
    assert harbor_compat.prepare_rewardkit_tasks(tmp_path) == 1
    assert "python3 /tests/_prt_rewardkit_runner.py /tests --workspace /app" in script.read_text()
    assert "# python3 -m rewardkit example" in script.read_text()
    assert script.stat().st_mode & 0o111
    assert (tests / harbor_compat.RUNNER_NAME).is_file()
    assert judge.read_text() == criteria
    assert harbor_compat.prepare_rewardkit_tasks(tmp_path) == 0


def test_harbor_upload_adapts_rewardkit_entrypoint(client, member_headers, make_zip):
    path = make_zip({
        "qa001/task.toml": 'version = "1.0"\n',
        "qa001/instruction.md": "# audit\n",
        "qa001/environment/Dockerfile": "FROM ubuntu:24.04\n",
        "qa001/tests/test.sh": "#!/bin/bash\npython3 -m rewardkit /tests --workspace /app\n",
        "qa001/tests/quality_a/judge.toml": "[judge]\nisolated = true\n",
    })
    with path.open("rb") as handle:
        response = client.post("/api/datasets", headers=member_headers,
            files={"file": (path.name, handle, "application/zip")},
            data={"name": "rewardkit-compat", "kind": "harbor_tasks"},
        )
    assert response.status_code == 201, response.text
    root = Path(response.json()["root_path"])
    assert (root / "tests" / harbor_compat.RUNNER_NAME).is_file()
    assert "独立工作区副本" in response.json()["stats"]["warnings"][0]


@pytest.mark.parametrize("shared", [False, True])
def test_multistep_rewardkit_upload_and_preview(client, member_headers, make_zip, shared):
    config = 'multi_step_reward_strategy = "final"\n[[steps]]\nname = "prepare"\n[[steps]]\nname = "analyze"\n'
    files = {"task/task.toml": config, "task/environment/Dockerfile": "FROM python:3.12-slim\n"}
    script = "#!/bin/bash\npython3 -m rewardkit /tests --workspace /app\n"
    rubric = '[judge]\nisolated = true\n[[criterion]]\ndescription = "检查结果"\n'
    if shared:
        files["task/tests/test.sh"] = script
    for name in ("prepare", "analyze"):
        files[f"task/steps/{name}/instruction.md"] = f"{name} instruction"
        files[f"task/steps/{name}/tests/rubrics.toml"] = rubric
        files[f"task/steps/{name}/solution/solve.sh"] = "#!/bin/bash\ntrue\n"
        if not shared:
            files[f"task/steps/{name}/tests/test.sh"] = script
    files["task/steps/analyze/workdir/setup.sh"] = "#!/bin/bash\ntrue\n"
    path = make_zip(files)
    with path.open("rb") as handle:
        response = client.post("/api/datasets", headers=member_headers,
            files={"file": (path.name, handle, "application/zip")},
            data={"name": "multi-rewardkit", "kind": "harbor_tasks", "rewardkit": "true"})
    assert response.status_code == 201, response.text
    body = response.json()
    root = Path(body["root_path"])
    assert body["rewardkit"] and body["stats"]["n_tasks"] == 1
    assert not any("没有" in warning for warning in body["stats"]["warnings"])
    assert (root / "task.toml").read_text() == config
    for name in ("prepare", "analyze"):
        assert (root / f"steps/{name}/tests/rubrics.toml").read_text() == rubric
        tests = root / "tests" if shared else root / f"steps/{name}/tests"
        assert (tests / harbor_compat.RUNNER_NAME).is_file()
    assert harbor_compat.prepare_rewardkit_tasks(root) == 0
    preview = client.get(f"/api/datasets/{body['id']}/preview", headers=member_headers).json()
    instruction = preview["tasks"][0]["instruction"]
    assert instruction.index("prepare instruction") < instruction.index("analyze instruction")


@pytest.mark.parametrize("config, expected", [
    ('[[steps]]\nname = "../escape"', "非法步骤"),
    ('[[steps]]\nname = "prepare"\n[[steps]]\nname = "PREPARE"', "重复步骤"),
    ('[[steps]]\nname = "missing"', "缺少 instruction"),
    ('', "声明执行顺序"),
])
def test_multistep_upload_rejects_invalid_structure(client, member_headers, make_zip, config, expected):
    path = make_zip({"task/task.toml": config, "task/environment/Dockerfile": "FROM python:3.12-slim",
                     "task/steps/prepare/instruction.md": "prepare"})
    with path.open("rb") as handle:
        response = client.post("/api/datasets", headers=member_headers,
            files={"file": (path.name, handle, "application/zip")},
            data={"name": "bad-steps", "kind": "harbor_tasks"})
    assert response.status_code == 400, response.text
    assert expected in response.json()["detail"]
