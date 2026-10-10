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
