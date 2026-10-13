"""Read Harbor's declared step order without importing its runtime environment."""

from __future__ import annotations

import re
import tomllib
import unicodedata
from pathlib import Path


def task_units(task_file: Path) -> list[Path]:
    """Return the single task or declared step directories; never infer step order."""
    try:
        config = tomllib.loads(task_file.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"{task_file.parent.name}/task.toml 无法解析") from exc
    steps = config.get("steps")
    if not steps:
        if (task_file.parent / "steps").is_dir():
            raise ValueError(f"{task_file.parent.name}: 存在 steps/，请在 task.toml 中用 [[steps]] 声明执行顺序")
        return [task_file.parent]
    if not isinstance(steps, list):
        raise ValueError("task.toml 的 steps 必须使用 [[steps]] 数组声明")
    result = []
    seen = set()
    for step in steps:
        name = step.get("name") if isinstance(step, dict) else None
        if (not isinstance(name, str) or not name.strip() or name in {".", ".."}
                or name.endswith((".", " ")) or len(name.encode("utf-8")) > 255
                or re.search(r'[<>:"/\\|?*\x00-\x1f\x7f]', name)
                or re.fullmatch(r"CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|(?:COM|LPT)[1-9¹²³]", name.partition(".")[0].rstrip(), re.I)):
            raise ValueError("task.toml 包含非法步骤名，必须为可移植的单层目录名")
        key = unicodedata.normalize("NFC", name).casefold()
        if key in seen:
            raise ValueError(f"task.toml 包含重复步骤名: {name}")
        seen.add(key)
        directory = task_file.parent / "steps" / name
        if not directory.resolve().is_relative_to(task_file.parent.resolve()) or directory.is_symlink():
            raise ValueError(f"步骤目录不允许指向任务外部: {name}")
        result.append(directory)
    return result
