"""Adapt Reward Kit entrypoints while keeping judge criteria and isolation enabled."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import toml

from ..config import settings
from .harbor_task_layout import task_units

RUNNER_NAME = "_prt_rewardkit_runner.py"
_COMMAND = re.compile(r"\b(python(?:3(?:\.\d+)?)?)\s+-m\s+rewardkit\b")
_CLI_COMMAND = re.compile(r"^(\s*(?:(?:if|elif|then|do|exec)\s+)?(?:!\s+)?)rewardkit(?=\s|$)")
# RewardKit's built-in Agent Judges; provider/model names are LLM Judges.
_AGENT_JUDGES = {"claude-code", "codex", "fx"}


def prepare_rewardkit_isolation(root: Path) -> int:
    """Default declared Agent Judges to isolation, including single-agent rubrics.

    Inspect configuration only: never import user-supplied scoring Python. Prepare
    every patch before writing so a malformed rubric cannot leave partial edits.
    """
    patches: dict[Path, str] = {}
    for task in sorted(root.rglob("task.toml")):
        for unit in dict.fromkeys([task.parent, *task_units(task)]):
            for path in sorted((unit / "tests").rglob("*.toml")):
                relative = path.relative_to(root)
                if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
                    raise ValueError(f"Judge 配置不能是符号链接: {relative}")
                try:
                    config = tomllib.loads(path.read_text(encoding="utf-8-sig"))
                except (OSError, ValueError) as exc:
                    raise ValueError(f"无法解析评分配置: {relative}") from exc
                judge = config.get("judge")
                if not isinstance(judge, dict) or not isinstance(config.get("criterion"), list):
                    continue
                if judge.get("judge") not in _AGENT_JUDGES or judge.get("isolated") is True:
                    continue
                judge["isolated"] = True
                patched = toml.dumps(config)
                if tomllib.loads(patched) != config:
                    raise ValueError(f"无法无损更新评分配置: {relative}")
                patches[path] = patched
    for path, content in patches.items():
        path.write_text(content, encoding="utf-8")
    return len(patches)


def prepare_rewardkit_tasks(root: Path) -> int:
    runner = (settings.resources_dir / "rewardkit_runner.py").read_text(encoding="utf-8")
    changed = 0
    scripts = []
    for task in sorted(root.rglob("task.toml")):
        units = task_units(task)
        scripts.extend(unit / "tests" / "test.sh" for unit in dict.fromkeys([task.parent, *units]))
    for script in scripts:
        if not script.is_file() or script.is_symlink():
            continue
        content = script.read_text(encoding="utf-8")
        patched = "".join(
            line if line.lstrip().startswith("#") else _CLI_COMMAND.sub(
                lambda match: f"{match[1]}python3 /tests/{RUNNER_NAME}",
                _COMMAND.sub(lambda match: f"{match[1]} /tests/{RUNNER_NAME}", line),
            )
            for line in content.splitlines(keepends=True)
        )
        if patched == content and RUNNER_NAME not in content:
            continue
        helper = script.parent / RUNNER_NAME
        if helper.is_symlink():
            raise ValueError(f"reward-kit 兼容入口不能是符号链接: {helper}")
        if not helper.is_file() or helper.read_text(encoding="utf-8") != runner:
            helper.write_text(runner, encoding="utf-8")
            changed += 1
        if patched != content:
            script.write_text(patched, encoding="utf-8")
    return changed
