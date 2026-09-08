"""平台文档：读取 backend/app/resources/docs/ 下的 Markdown。

文档用轻量 front-matter 描述元信息，正文就是普通 Markdown（GFM 表格可用）：

    ---
    title: 快速开始
    category: 入门
    order: 1
    summary: 从登录到看到第一条 loss 曲线
    ---

按文件 mtime 缓存，改完 md 刷新页面就能看到，不需要重启服务。
"""

from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import Any

from fastapi import HTTPException, status

from ..config import settings

_LOCK = threading.RLock()
_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}

_FRONT_MATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.S)
_SLUG_OK = re.compile(r"^[a-z0-9][a-z0-9\-_]*$")


def docs_dir() -> Path:
    return settings.resources_dir / "docs"


def assets_dir() -> Path:
    return docs_dir() / "assets"


def _parse(path: Path) -> dict[str, Any]:
    """Split front-matter from body. Deliberately tiny — no YAML dependency."""
    raw = path.read_text(encoding="utf-8")
    meta: dict[str, Any] = {}
    body = raw
    match = _FRONT_MATTER.match(raw)
    if match:
        for line in match.group(1).splitlines():
            if ":" not in line:
                continue
            key, _, value = line.partition(":")
            meta[key.strip()] = value.strip()
        body = raw[match.end() :]

    # 文件名形如 01-quickstart.md -> slug=quickstart, order=1
    stem = path.stem
    order_prefix = re.match(r"^(\d+)[-_](.+)$", stem)
    slug = order_prefix.group(2) if order_prefix else stem
    default_order = int(order_prefix.group(1)) if order_prefix else 999

    heading = re.search(r"^#\s+(.+)$", body, re.M)
    return {
        "slug": slug,
        "title": meta.get("title") or (heading.group(1).strip() if heading else slug),
        "category": meta.get("category", "文档"),
        "summary": meta.get("summary", ""),
        "order": int(meta.get("order", default_order)),
        "updated_at": path.stat().st_mtime,
        "body": body.lstrip("\n"),
    }


def _load(path: Path) -> dict[str, Any]:
    key = str(path)
    mtime = path.stat().st_mtime
    with _LOCK:
        cached = _CACHE.get(key)
        if cached and cached[0] == mtime:
            return cached[1]
        parsed = _parse(path)
        _CACHE[key] = (mtime, parsed)
        return parsed


def list_docs() -> list[dict[str, Any]]:
    root = docs_dir()
    if not root.is_dir():
        return []
    docs = [_load(path) for path in sorted(root.glob("*.md"))]
    docs.sort(key=lambda d: (d["order"], d["title"]))
    return [{k: v for k, v in doc.items() if k != "body"} for doc in docs]


def get_doc(slug: str) -> dict[str, Any]:
    if not _SLUG_OK.match(slug):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="非法的文档标识")
    root = docs_dir()
    if not root.is_dir():
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="文档目录不存在")
    for path in sorted(root.glob("*.md")):
        doc = _load(path)
        if doc["slug"] == slug:
            return doc
    raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"未找到文档: {slug}")


def asset_path(name: str) -> Path:
    """Resolve a doc image, refusing anything outside the assets directory."""
    root = assets_dir().resolve()
    target = (root / name).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="非法的资源路径") from exc
    if not target.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="资源不存在")
    return target
