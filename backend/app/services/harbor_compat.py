"""Adapt Reward Kit entrypoints while keeping judge criteria and isolation enabled."""

from __future__ import annotations

import re
from pathlib import Path

from ..config import settings
from .harbor_task_layout import task_units

RUNNER_NAME = "_prt_rewardkit_runner.py"
_COMMAND = re.compile(r"\b(python(?:3(?:\.\d+)?)?)\s+-m\s+rewardkit\b")


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
            line if line.lstrip().startswith("#") else _COMMAND.sub(
                lambda match: f"{match[1]} /tests/{RUNNER_NAME}", line,
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
