"""File-backed persistence.

The team asked for "files now, MySQL later", so every collection is a plain
JSON document guarded by an in-process ``RLock`` (fast path) and a ``filelock``
(cross-process safety, e.g. a CLI script poking the same data directory).
Writes are atomic: temp file + ``os.replace``.

Swapping in MySQL later means providing another class with the same surface as
:class:`JsonCollection` and rebinding it in ``store/registry.py`` — no service
code has to change.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, Generic, TypeVar

from filelock import FileLock
from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


def _json_default(value: Any) -> Any:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"not JSON serialisable: {type(value)!r}")


class JsonCollection(Generic[T]):
    """A list of pydantic records persisted as a single JSON array."""

    def __init__(self, path: Path, model: type[T], id_field: str = "id") -> None:
        self.path = path
        self.model = model
        self.id_field = id_field
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._file_lock = FileLock(str(self.path) + ".lock", timeout=30)
        self._cache: list[T] | None = None
        self._cache_sig: tuple[float, int] | None = None

    # ----------------------------------------------------------- internals --
    def _signature(self) -> tuple[float, int] | None:
        try:
            stat = self.path.stat()
        except FileNotFoundError:
            return None
        return (stat.st_mtime_ns / 1e9, stat.st_size)

    def _load(self) -> list[T]:
        sig = self._signature()
        if self._cache is not None and sig == self._cache_sig:
            return self._cache
        if sig is None:
            self._cache, self._cache_sig = [], None
            return self._cache
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8") or "[]")
        except json.JSONDecodeError:
            # Corrupt file: keep it around for forensics, start clean.
            self.path.rename(self.path.with_suffix(self.path.suffix + ".corrupt"))
            raw = []
        items: list[T] = []
        for row in raw:
            try:
                items.append(self.model.model_validate(row))
            except Exception:  # pragma: no cover - tolerate schema drift
                continue
        self._cache, self._cache_sig = items, self._signature()
        return items

    def _dump(self, items: Iterable[T]) -> None:
        payload = [item.model_dump(mode="json") for item in items]
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2, default=_json_default)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        self._cache = list(items)
        self._cache_sig = self._signature()

    def _key(self, item: T) -> Any:
        return getattr(item, self.id_field)

    # -------------------------------------------------------------- public --
    def all(self) -> list[T]:
        with self._lock:
            return list(self._load())

    def get(self, key: Any) -> T | None:
        with self._lock:
            for item in self._load():
                if self._key(item) == key:
                    return item
        return None

    def find(self, predicate: Callable[[T], bool]) -> list[T]:
        with self._lock:
            return [item for item in self._load() if predicate(item)]

    def first(self, predicate: Callable[[T], bool]) -> T | None:
        with self._lock:
            for item in self._load():
                if predicate(item):
                    return item
        return None

    def count(self, predicate: Callable[[T], bool] | None = None) -> int:
        with self._lock:
            items = self._load()
            return len(items) if predicate is None else sum(1 for i in items if predicate(i))

    def put(self, item: T) -> T:
        """Insert or replace by id."""
        with self._lock, self._file_lock:
            items = list(self._load())
            key = self._key(item)
            for index, existing in enumerate(items):
                if self._key(existing) == key:
                    items[index] = item
                    break
            else:
                items.append(item)
            self._dump(items)
        return item

    def put_many(self, new_items: Iterable[T]) -> None:
        with self._lock, self._file_lock:
            items = list(self._load())
            index_by_key = {self._key(i): n for n, i in enumerate(items)}
            for item in new_items:
                key = self._key(item)
                if key in index_by_key:
                    items[index_by_key[key]] = item
                else:
                    index_by_key[key] = len(items)
                    items.append(item)
            self._dump(items)

    def replace_all(self, new_items: Iterable[T]) -> None:
        with self._lock, self._file_lock:
            self._dump(list(new_items))

    def update(self, key: Any, mutate: Callable[[T], None]) -> T | None:
        """Read-modify-write a single record while holding the write lock."""
        with self._lock, self._file_lock:
            items = list(self._load())
            for index, existing in enumerate(items):
                if self._key(existing) == key:
                    updated = existing.model_copy(deep=True)
                    mutate(updated)
                    items[index] = updated
                    self._dump(items)
                    return updated
        return None

    def delete(self, key: Any) -> bool:
        with self._lock, self._file_lock:
            items = list(self._load())
            remaining = [i for i in items if self._key(i) != key]
            if len(remaining) == len(items):
                return False
            self._dump(remaining)
            return True


class JsonDocument(Generic[T]):
    """A single JSON object (used for system settings)."""

    def __init__(self, path: Path, model: type[T]) -> None:
        self.path = path
        self.model = model
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._file_lock = FileLock(str(self.path) + ".lock", timeout=30)

    def read(self) -> T:
        with self._lock:
            if not self.path.exists():
                return self.model()
            try:
                return self.model.model_validate_json(self.path.read_text(encoding="utf-8"))
            except Exception:
                return self.model()

    def write(self, value: T) -> T:
        with self._lock, self._file_lock:
            fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(value.model_dump_json(indent=2))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
        return value

    def update(self, mutate: Callable[[T], None]) -> T:
        with self._lock, self._file_lock:
            current = self.read()
            mutate(current)
            return self.write(current)


class JsonlLog:
    """Append-only event log (audit trail)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def append(self, record: BaseModel) -> None:
        line = json.dumps(record.model_dump(mode="json"), ensure_ascii=False, default=_json_default)
        with self._lock:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")

    def tail(self, limit: int = 200, predicate: Callable[[dict[str, Any]], bool] | None = None) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        with self._lock:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        out: list[dict[str, Any]] = []
        for line in reversed(lines):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if predicate and not predicate(row):
                continue
            out.append(row)
            if len(out) >= limit:
                break
        return out
