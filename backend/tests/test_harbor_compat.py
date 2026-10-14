"""Reward Kit isolation stays independent when overlay mounts are unavailable."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import tomllib
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


@pytest.mark.parametrize("agent", ["claude-code", "codex", "fx"])
@pytest.mark.parametrize("isolation", ["", "isolated = false\n", "isolated = true\n"])
def test_agent_isolation_preserves_rubric_and_is_idempotent(tmp_path, agent, isolation):
    tests = tmp_path / "tests"
    tests.mkdir()
    (tmp_path / "task.toml").write_text('version = "1.0"\n')
    rubric = tests / "quality.toml"
    content = (
        f'[judge]\njudge = "{agent}"\n{isolation}'
        'model = "example/model"\nmode = "individual"\ntimeout = 600\n'
        '[[judge.mcp_servers]]\nname = "data"\ntransport = "stdio"\n'
        'command = "python"\nargs = ["server.py"]\n'
        '[[criterion]]\nid = "q1"\ndescription = "检查结果\\n含中文及换行"\n'
        'weight = 0.75\nnegate = true\noptional = false\n'
        '[criterion.annotations]\nsource = "原始标准"\n'
        '[scoring]\naggregation = "weighted_mean"\n'
    )
    rubric.write_text(content, encoding="utf-8")
    expected = tomllib.loads(content)
    expected["judge"]["isolated"] = True
    assert harbor_compat.prepare_rewardkit_isolation(tmp_path) == (isolation != "isolated = true\n")
    assert tomllib.loads(rubric.read_text()) == expected
    first = rubric.read_bytes()
    assert harbor_compat.prepare_rewardkit_isolation(tmp_path) == 0
    assert rubric.read_bytes() == first
    if isolation == "isolated = true\n":
        assert rubric.read_text() == content


def test_isolation_leaves_llm_and_non_rewardkit_configs_unchanged(tmp_path):
    tests = tmp_path / "tests"
    tests.mkdir()
    (tmp_path / "task.toml").write_text("")
    configs = {
        "llm.toml": '[judge]\njudge = "openai/model"\nisolated = false\n[[criterion]]\ndescription = "test"\n',
        "default.toml": '[judge]\n[[criterion]]\ndescription = "default LLM"\n',
        "other.toml": '[judge]\njudge = "codex"\n',
        "reward.toml": '[[reward]]\nname = "total"\n',
    }
    for name, content in configs.items():
        (tests / name).write_text(content)
    assert harbor_compat.prepare_rewardkit_isolation(tmp_path) == 0
    for name, content in configs.items():
        assert (tests / name).read_text() == content


@pytest.mark.parametrize("prefix", ["", "if ! ", "exec ", "  "])
def test_rewardkit_cli_uses_isolation_fallback(tmp_path, prefix):
    tests = tmp_path / "tests"
    tests.mkdir()
    (tmp_path / "task.toml").write_text("")
    script = tests / "test.sh"
    untouched = '# rewardkit /tests\necho "rewardkit /tests"\n'
    script.write_text(untouched + prefix + "rewardkit /tests --workspace /app\n")
    assert harbor_compat.prepare_rewardkit_tasks(tmp_path) == 1
    assert script.read_text() == untouched + prefix + f"python3 /tests/{harbor_compat.RUNNER_NAME} /tests --workspace /app\n"
    assert harbor_compat.prepare_rewardkit_tasks(tmp_path) == 0


@pytest.mark.parametrize("multistep", [False, True])
def test_upload_and_replace_enable_agent_isolation(client, member_headers, make_zip, multistep):
    files = {
        "task/task.toml": '[[steps]]\nname = "one"\n[[steps]]\nname = "two"\n' if multistep else '',
        "task/environment/Dockerfile": "FROM python:3.12-slim\n",
        "task/tests/test.sh": "python3 -m rewardkit /tests --workspace /app\n",
    }
    units = ["task/steps/one", "task/steps/two"] if multistep else ["task"]
    for unit in units:
        files[f"{unit}/instruction.md"] = "Check the answer"
    rubrics = ["task/tests/quality_a/judge.toml"] + [f"{unit}/tests/quality_b/judge.toml" for unit in units]
    for rubric in rubrics:
        files[rubric] = '[judge]\njudge = "claude-code"\nisolated = false\n[[criterion]]\ndescription = "检查结果"\n'
    path = make_zip(files)
    with path.open("rb") as handle:
        response = client.post("/api/datasets", headers=member_headers,
            files={"file": (path.name, handle, "application/zip")},
            data={"name": "auto-isolation", "kind": "harbor_tasks"})
    assert response.status_code == 201, response.text
    dataset_id = response.json()["id"]
    for attempt in range(2):
        body = response.json()
        assert any(f"{len(rubrics)} 个 RewardKit Agent Judge" in warning for warning in body["stats"]["warnings"])
        root = Path(body["root_path"])
        for rubric in rubrics:
            assert tomllib.loads((root / rubric.removeprefix("task/")).read_text())["judge"]["isolated"] is True
        assert harbor_compat.prepare_rewardkit_isolation(root) == 0
        if attempt == 0:
            with path.open("rb") as handle:
                response = client.put(f"/api/datasets/{dataset_id}/file", headers=member_headers,
                    files={"file": (path.name, handle, "application/zip")})
            assert response.status_code == 200, response.text


@pytest.mark.parametrize("invalid", ["malformed", "symlink"])
def test_isolation_rejects_invalid_config_before_writing(tmp_path, invalid):
    tests = tmp_path / "tests"
    tests.mkdir()
    (tmp_path / "task.toml").write_text("")
    content = '[judge]\njudge = "codex"\n[[criterion]]\ndescription = "test"\n'
    valid = tests / "a.toml"
    valid.write_text(content)
    broken = tests / "z.toml"
    if invalid == "malformed":
        broken.write_text('[judge')
    else:
        broken.symlink_to(valid)
    with pytest.raises(ValueError, match="评分配置|符号链接"):
        harbor_compat.prepare_rewardkit_isolation(tmp_path)
    assert valid.read_text() == content
