"""样例审核阶段 ①：解压后把「事实」采集成 manifest.json，全程不过模型。

分工是刻意的：**能用代码确定的事情绝不交给模型猜**。文件在不在、权限位是多少、
task.toml 里某个字段是不是空字符串，都在这里落成结构化证据；模型只在证据上做
「这算不算符合规范」的判断。这样报告里的每一条都能回到一个可复查的事实。
"""

from __future__ import annotations

import json
import re
import subprocess
import tomllib
import zipfile
from pathlib import Path
from typing import Any

from ..config import settings
from . import archive

# 目录里出现任一标志文件就算一道题（与 Harbor 的任务发现口径一致）
TASK_MARKERS = ("task.toml", "instruction.md", "metadata.json")
# 送进证据的关键文本：模式 -> 用途标签
TEXT_GLOBS = (
    "task.toml",
    "metadata.json",
    "instruction.md",
    "solution/solve.*",
    "tests/test.*",
    "environment/Dockerfile",
    "environment/docker-compose.yaml",
    "environment/docker-compose.yml",
)
MAX_FILES_PER_TASK = 300
DOC_SUFFIXES = (".docx", ".pdf", ".md", ".markdown", ".txt")


# --------------------------------------------------------------------------- #
# 需求文档 → 纯文本
# --------------------------------------------------------------------------- #
def read_document_text(path: Path, max_chars: int | None = None) -> str:
    """把需求文档读成纯文本。docx / pdf / md / txt，不引入新依赖。"""
    limit = max_chars if max_chars is not None else settings.review_max_doc_chars
    suffix = path.suffix.lower()
    try:
        if suffix == ".docx":
            text = _docx_text(path)
        elif suffix == ".pdf":
            text = _pdf_text(path)
        else:
            text = path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:  # 单份文档读不出来不该让整次审核挂掉
        return f"[无法解析 {path.name}：{type(exc).__name__}: {exc}]"
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) > limit:
        text = text[:limit] + f"\n…（文档过长，已截断，原文 {len(text)} 字）"
    return text


def _docx_text(path: Path) -> str:
    """docx 就是个 zip，正文在 word/document.xml；按段落取文字即可。"""
    with zipfile.ZipFile(path) as zf:
        names = [
            "word/document.xml",
            *sorted(n for n in zf.namelist() if re.fullmatch(r"word/(header|footer)\d*\.xml", n)),
        ]
        chunks = []
        for name in names:
            try:
                chunks.append(zf.read(name).decode("utf-8", errors="replace"))
            except KeyError:
                continue
    xml = "\n".join(chunks)
    # 段落/换行/单元格边界先变成分隔符，再剥标签
    xml = re.sub(r"</w:p>|<w:br\s*/>|<w:tab\s*/>", "\n", xml)
    xml = re.sub(r"</w:tc>", " | ", xml)
    xml = re.sub(r"</w:tr>", "\n", xml)
    xml = re.sub(r"<[^>]+>", "", xml)
    return _unescape(xml)


def _unescape(text: str) -> str:
    for src, dst in (("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'), ("&apos;", "'"), ("&amp;", "&")):
        text = text.replace(src, dst)
    return text


def _pdf_text(path: Path) -> str:
    """pdftotext 是系统自带的（平台其他地方也在用），不为了 pdf 加 python 依赖。"""
    proc = subprocess.run(  # noqa: S603 - 固定 argv，不过 shell
        ["/usr/bin/pdftotext", "-layout", str(path), "-"],
        capture_output=True,
        timeout=120,
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or b"").decode("utf-8", "replace")[:200] or "pdftotext 失败")
    return proc.stdout.decode("utf-8", errors="replace")


# --------------------------------------------------------------------------- #
# 题目发现 + 证据采集
# --------------------------------------------------------------------------- #
def collect(extracted: archive.ExtractResult) -> dict[str, Any]:
    """扫描解压结果，产出 manifest（题目清单 + 每题证据）。"""
    root = extracted.root
    notes = list(extracted.notes)
    task_dirs = _discover_tasks(root)

    if len(task_dirs) > settings.review_max_tasks:
        notes.append(
            f"压缩包里有 {len(task_dirs)} 道题，超过单次上限 {settings.review_max_tasks}，"
            f"只审核前 {settings.review_max_tasks} 道（按路径排序）"
        )
        task_dirs = task_dirs[: settings.review_max_tasks]

    tasks = [_task_evidence(root, d, extracted.modes) for d in task_dirs]
    seen: dict[str, int] = {}
    for task in tasks:  # 题号重复时带上路径，保证报告里能区分
        tid = task["task_id"]
        seen[tid] = seen.get(tid, 0) + 1
    for task in tasks:
        if seen[task["task_id"]] > 1:
            task["task_id_duplicated"] = True

    duplicated = sorted(tid for tid, n in seen.items() if n > 1)
    if duplicated:
        notes.append(f"{len(duplicated)} 个题号在包里出现多次，例如 {duplicated[:3]}")

    return {
        "root": str(root),
        "n_tasks": len(tasks),
        "inner_archives": extracted.inner_archives,
        "notes": notes,
        "tasks": tasks,
    }


def _discover_tasks(root: Path) -> list[Path]:
    found: set[Path] = set()
    for marker in TASK_MARKERS:
        for hit in root.rglob(marker):
            if hit.is_file():
                found.add(hit.parent)
    # 一道题的目录里不该再套另一道题；父目录只是分组目录，剔掉
    tasks = {d for d in found if not any(other != d and other.is_relative_to(d) for other in found)}
    return sorted(tasks, key=lambda p: p.as_posix())


def _task_evidence(root: Path, task_dir: Path, modes: dict[str, int]) -> dict[str, Any]:
    rel_dir = task_dir.relative_to(root).as_posix()
    files: list[dict[str, Any]] = []
    truncated = False
    for path in sorted(task_dir.rglob("*"), key=lambda p: p.as_posix()):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if len(files) >= MAX_FILES_PER_TASK:
            truncated = True
            break
        recorded = modes.get(rel)
        files.append(
            {
                "path": path.relative_to(task_dir).as_posix(),
                "size": path.stat().st_size,
                # 压缩包里记录的权限位；None = 包里没记（Windows 打的包）
                "mode": f"{recorded:04o}" if recorded else None,
                "executable": bool(recorded and recorded & 0o111),
            }
        )

    names = {f["path"] for f in files}
    dirs = {p.split("/", 1)[0] for p in names if "/" in p}
    evidence: dict[str, Any] = {
        "task_id": _task_id(task_dir, files),
        "rel_path": rel_dir,
        "n_files": len(files),
        "files_truncated": truncated,
        "files": files,
        "present": {
            "task.toml": "task.toml" in names,
            "instruction.md": "instruction.md" in names,
            "metadata.json": "metadata.json" in names,
            "environment/": "environment" in dirs,
            "tests/": "tests" in dirs,
            "solution/": "solution" in dirs,
        },
        "metadata": {},
        "metadata_errors": [],
        "texts": {},
    }

    for key, value in _read_metadata(task_dir, evidence["metadata_errors"]).items():
        evidence["metadata"][key] = value
    evidence["texts"] = _read_texts(task_dir)
    return evidence


def _task_id(task_dir: Path, files: list[dict[str, Any]]) -> str:
    """题号：优先 task.toml / metadata.json 里的 task_id，否则用目录名。"""
    for name in ("task.toml", "metadata.json"):
        path = task_dir / name
        if not path.is_file():
            continue
        try:
            data = _load_structured(path)
        except Exception:
            continue
        for key in ("task_id", "id", "taskId"):
            value = data.get(key) if isinstance(data, dict) else None
            if isinstance(value, str) and value.strip():
                return value.strip()
    return task_dir.name


def _load_structured(path: Path) -> Any:
    if path.suffix == ".toml":
        with path.open("rb") as handle:
            return tomllib.load(handle)
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _read_metadata(task_dir: Path, errors: list[str]) -> dict[str, Any]:
    """把 task.toml / metadata.json 解析成一棵扁平的事实树。

    值原样保留（包括空字符串与 null）——「必填字段是不是非空字符串」这类判定要的
    就是原值，任何归一化都会把问题抹掉。
    """
    merged: dict[str, Any] = {}
    for name in ("task.toml", "metadata.json"):
        path = task_dir / name
        if not path.is_file():
            continue
        try:
            data = _load_structured(path)
        except Exception as exc:
            errors.append(f"{name} 无法解析：{type(exc).__name__}: {exc}")
            continue
        if isinstance(data, dict):
            merged[name] = data
        else:
            errors.append(f"{name} 顶层不是对象（实际是 {type(data).__name__}）")
    return merged


def _read_texts(task_dir: Path) -> dict[str, str]:
    limit = settings.review_evidence_bytes
    texts: dict[str, str] = {}
    for pattern in TEXT_GLOBS:
        for path in sorted(task_dir.glob(pattern)):
            if not path.is_file():
                continue
            rel = path.relative_to(task_dir).as_posix()
            try:
                body = path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                texts[rel] = f"[读取失败：{exc}]"
                continue
            if len(body) > limit:
                body = body[:limit] + f"\n…（已截断，原文 {len(body)} 字）"
            texts[rel] = body
    return texts


def write_manifest(manifest: dict[str, Any], path: Path) -> None:
    path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
