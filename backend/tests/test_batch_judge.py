"""Per-run batch mode must preserve rubric semantics and shared task sources."""

import tomllib
import pytest

from backend.app.services.harbor_judge_service import prepare_batched_tasks


RUBRIC = '''[judge]
judge = "claude-code"
model = "test-model"
mode = "individual"
isolated = true
files = ["/app/answer.md"]

[scoring]
aggregation = "weighted-mean"

[[criterion]]
name = "check_1"
description = "检查真实来源"
weight = 2.5
negate = true
'''


@pytest.mark.parametrize("multistep", [False, True])
def test_batch_snapshot_preserves_rubric_and_source(tmp_path, multistep):
    root = tmp_path / "source"
    root.mkdir()
    config = '[[steps]]\nname = "first"\n[[steps]]\nname = "second"\n' if multistep else ''
    (root / "task.toml").write_text(config)
    units = [root]
    if multistep:
        units += [root / "steps" / name for name in ("first", "second")]
    for unit in units:
        tests = unit / "tests" / "quality"
        tests.mkdir(parents=True)
        (unit / "instruction.md").write_text("Evaluate the answer")
        (tests / "rubrics.toml").write_text(RUBRIC)
        (unit / "tests" / "reward.toml").write_text('[scoring]\naggregation="all-pass"\n')
        script = unit / "tests" / "test.sh"
        script.write_text("#!/bin/bash\npython3 -m rewardkit /tests\n")
        script.chmod(0o755)
    (root / "input_files").mkdir()
    (root / "input_files" / "judge.toml").write_text(RUBRIC)
    before = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    target, count = prepare_batched_tasks(root, tmp_path / "job" / "batch-judge-tasks")
    assert count == len(units)
    expected = tomllib.loads(RUBRIC)
    expected["judge"]["mode"] = "batched"
    for unit in units:
        copied = target / unit.relative_to(root)
        assert tomllib.loads((copied / "tests/quality/rubrics.toml").read_text()) == expected
        assert (copied / "tests/test.sh").stat().st_mode & 0o111
    for relative, content in before.items():
        assert (root / relative).read_bytes() == content
        if relative.name != "rubrics.toml":
            assert (target / relative).read_bytes() == content
    # Repeated previews use the same snapshot, independent of source changes.
    (root / "tests/quality/rubrics.toml").write_text(RUBRIC.replace("test-model", "changed"))
    assert prepare_batched_tasks(root, target) == (target, count)
    assert tomllib.loads((target / "tests/quality/rubrics.toml").read_text()) == expected


@pytest.mark.parametrize("content, message", [
    ('[judge]\nmode="individual"\n[[criterion]]\nfiles=["/app/a.txt"]\n', "逐评分项 files"),
    ('[judge]\nmode = [', "无法解析"),
])
def test_batch_rejects_unsafe_conversion_without_partial_snapshot(tmp_path, content, message):
    root = tmp_path / "source"
    (root / "tests").mkdir(parents=True)
    (root / "task.toml").touch()
    (root / "tests/judge.toml").write_text(content)
    destination = tmp_path / "job/tasks"
    with pytest.raises(ValueError, match=message):
        prepare_batched_tasks(root, destination)
    assert not destination.exists()
    assert not destination.with_name("tasks.copying").exists()
    assert (root / "tests/judge.toml").read_text() == content


def test_batch_rejects_symlinks(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    (root / "task.toml").touch()
    (root / "link").symlink_to(tmp_path)
    with pytest.raises(ValueError, match="符号链接"):
        prepare_batched_tasks(root, tmp_path / "job/tasks")


def test_batch_adds_mode_when_omitted(tmp_path):
    root = tmp_path / "source"
    (root / "tests").mkdir(parents=True)
    (root / "task.toml").touch()
    (root / "tests/judge.toml").write_text(RUBRIC.replace('mode = "individual"\n', ''))
    destination, _ = prepare_batched_tasks(root, tmp_path / "job/tasks")
    assert tomllib.loads((destination / "tests/judge.toml").read_text())["judge"]["mode"] == "batched"


def test_batch_option_is_saved_and_registry_rejected(client, member_headers, tmp_path):
    from .conftest import project_id

    root = tmp_path / "task"
    (root / "tests").mkdir(parents=True)
    (root / "task.toml").touch()
    (root / "tests/judge.toml").write_text(RUBRIC)
    payload = {"name": "batch-api", "type": "verify", "project_id": project_id(),
               "resources": {"n_gpus": 0},
               "verify": {"agent": "nop", "tasks_path": str(root), "batch_judge": True}}
    response = client.post("/api/jobs", headers=member_headers, json=payload)
    assert response.status_code == 201, response.text
    assert response.json()["verify"]["batch_judge"] is True
    assert (root / "tests/judge.toml").read_text() == RUBRIC
    payload["verify"].update(tasks_path="", registry_dataset="terminal-bench@2.0")
    response = client.post("/api/jobs", headers=member_headers, json=payload)
    assert response.status_code == 400 and "Batch Judge" in response.text
