"""Harbor 官方注册表里的数据集（terminal-bench、aider-polyglot、aime…）。

数据源是 Harbor 自己读的那份 ``registry.json``（``harbor.constants.DEFAULT_REGISTRY_URL``），
我们只取名字 / 版本 / 描述 / 任务数给提交页做下拉。落一份到 ``data/cache/``：

* 断网时用缓存，别让整个提交页因为拉不到 GitHub 而卡住；
* 6 小时以内不重复拉，注册表几天才动一次。

选了注册表数据集之后 Harbor 运行时要 ``git clone`` 任务仓库 —— 那一步同样需要网络，
提交页里要提醒。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from ..config import settings

# 和 harbor/constants.py 里的 DEFAULT_REGISTRY_URL 保持一致（有测试核对）
REGISTRY_URL = "https://raw.githubusercontent.com/laude-institute/harbor/main/registry.json"
CACHE_TTL_SECONDS = 6 * 3600
# registry.json 有 13 MB，GitHub 首字节有时要 20 多秒；给足
FETCH_TIMEOUT_SECONDS = 120

_LOCK = threading.RLock()


def cache_path() -> Path:
    return settings.data_root / "cache" / "harbor_registry.json"


def _summarize(raw: Any) -> list[dict[str, Any]]:
    """把注册表原文压成下拉框需要的字段。任务列表本身不要，太大。"""
    rows: list[dict[str, Any]] = []
    entries = raw.get("datasets", raw) if isinstance(raw, dict) else raw
    if not isinstance(entries, list):
        return rows
    for entry in entries:
        if not isinstance(entry, dict) or not entry.get("name"):
            continue
        version = str(entry.get("version") or "").strip()
        rows.append(
            {
                "name": str(entry["name"]),
                "version": version,
                "ref": f"{entry['name']}@{version}" if version else str(entry["name"]),
                "description": str(entry.get("description") or "").strip(),
                "task_count": len(entry.get("tasks") or []),
            }
        )
    rows.sort(key=lambda r: (r["name"].lower(), r["version"]))
    return rows


def _read_cache() -> dict[str, Any] | None:
    path = cache_path()
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _write_cache(payload: dict[str, Any]) -> None:
    path = cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _fetch() -> list[dict[str, Any]]:
    """拉 registry.json。

    优先用 curl：registry.json 有 13 MB，实测 urllib 拿到响应头后正文会一直 trickle
    到超时，而 curl（HTTP/2）几秒就完；没有 curl 再回落到 urllib。
    """
    if shutil.which("curl"):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as handle:
            target = Path(handle.name)
        try:
            completed = subprocess.run(  # noqa: S603 - 固定参数
                ["curl", "-sSL", "--retry", "2", "--retry-all-errors", "--max-time", str(FETCH_TIMEOUT_SECONDS),
                 "-A", "prt-ai-dl-factory", "-o", str(target), REGISTRY_URL],
                capture_output=True, text=True, timeout=FETCH_TIMEOUT_SECONDS * 3 + 10, check=False,
            )
            if completed.returncode != 0:
                raise OSError(completed.stderr.strip() or f"curl exit {completed.returncode}")
            raw = json.loads(target.read_text(encoding="utf-8"))
        finally:
            target.unlink(missing_ok=True)
        return _summarize(raw)

    request = urllib.request.Request(REGISTRY_URL, headers={"User-Agent": "prt-ai-dl-factory"})
    with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT_SECONDS) as response:  # noqa: S310
        raw = json.loads(response.read().decode("utf-8"))
    return _summarize(raw)


_REFRESHING = False
_LAST_ERROR = ""


def _refresh_in_background() -> None:
    """起一个线程去拉；同一时刻只跑一个。提交页永远不该被这 13 MB 卡住。"""
    global _REFRESHING
    with _LOCK:
        if _REFRESHING:
            return
        _REFRESHING = True

    def worker() -> None:
        global _REFRESHING, _LAST_ERROR
        try:
            datasets = _fetch()
            _write_cache({"datasets": datasets, "fetched_at": time.time(), "url": REGISTRY_URL})
            _LAST_ERROR = ""
        except Exception as exc:  # noqa: BLE001 - 只记录，不能炸
            _LAST_ERROR = f"{type(exc).__name__}: {exc}"
        finally:
            with _LOCK:
                _REFRESHING = False

    threading.Thread(target=worker, name="harbor-registry-refresh", daemon=True).start()


def list_datasets(*, force_refresh: bool = False, wait: bool = False) -> dict[str, Any]:
    """返回 ``{datasets, fetched_at, source, error, refreshing}``。

    ``source``：``network``（本次拉到的）/ ``cache`` / ``loading``（后台正在拉，先给空或旧数据）
    / ``none``（拉失败且没缓存）。默认**不阻塞**：缓存过期或被要求刷新时在后台拉，
    调用方拿到的是当前手里最好的一份；``wait=True`` 只给测试和脚本用。
    """
    cached = _read_cache()
    now = time.time()
    fresh = bool(cached) and now - float(cached.get("fetched_at") or 0) < CACHE_TTL_SECONDS

    if wait and (force_refresh or not fresh):
        try:
            datasets = _fetch()
        except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError, subprocess.SubprocessError) as exc:
            if cached:
                return {**cached, "source": "cache", "error": f"刷新失败，使用缓存：{exc}", "refreshing": False}
            return {"datasets": [], "fetched_at": None, "source": "none", "error": f"无法访问注册表：{exc}", "refreshing": False}
        payload = {"datasets": datasets, "fetched_at": time.time(), "url": REGISTRY_URL}
        try:
            _write_cache(payload)
        except OSError:
            pass
        return {**payload, "source": "network", "error": "", "refreshing": False}

    if force_refresh or not fresh:
        _refresh_in_background()

    if cached:
        return {**cached, "source": "cache" if fresh else "loading", "error": _LAST_ERROR, "refreshing": _REFRESHING}
    return {
        "datasets": [],
        "fetched_at": None,
        "source": "loading" if _REFRESHING else "none",
        "error": _LAST_ERROR if not _REFRESHING else "",
        "refreshing": _REFRESHING,
    }
