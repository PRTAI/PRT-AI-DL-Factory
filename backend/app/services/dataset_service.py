"""Dataset ingestion: zip upload -> safe extract -> format validation -> registry.

Every accepted archive lands in ``data/datasets/<dataset_id>/`` and a
``dataset_info.json`` (LLaMA-Factory schema) is written next to it for the
formats that LLaMA-Factory can consume, so job builders never have to re-derive
column mappings.
"""

from __future__ import annotations

import json
import shutil
import tarfile
import zipfile
from pathlib import Path
from typing import Any, BinaryIO

from fastapi import HTTPException, status

from ..config import settings
from ..models import (
    Dataset,
    DatasetFileInfo,
    DatasetKind,
    DatasetStats,
    DatasetStatus,
    DatasetUpdate,
    User,
    new_id,
    utcnow,
)
from ..store import store
from . import audit_service

CHUNK = 4 * 1024 * 1024
DATA_SUFFIXES = (".json", ".jsonl")
MAX_SAMPLE_ROWS = 3
MAX_PREVIEW_CHARS = 600


# --------------------------------------------------------------------------- #
# Archive handling
# --------------------------------------------------------------------------- #
def _is_within(base: Path, target: Path) -> bool:
    try:
        target.resolve().relative_to(base.resolve())
        return True
    except ValueError:
        return False


def _safe_extract(archive: Path, dest: Path) -> None:
    """Extract zip/tar defending against path traversal and symlink escapes."""
    dest.mkdir(parents=True, exist_ok=True)
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as zf:
            for member in zf.infolist():
                name = member.filename
                if name.startswith("/") or ".." in Path(name).parts:
                    raise HTTPException(
                        status.HTTP_400_BAD_REQUEST, detail=f"压缩包包含非法路径: {name}"
                    )
                target = dest / name
                if not _is_within(dest, target):
                    raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=f"压缩包越界: {name}")
            zf.extractall(dest)
        return

    if tarfile.is_tarfile(archive):
        with tarfile.open(archive) as tf:
            for member in tf.getmembers():
                if member.issym() or member.islnk():
                    raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="压缩包包含符号链接，已拒绝")
                target = dest / member.name
                if member.name.startswith("/") or ".." in Path(member.name).parts or not _is_within(dest, target):
                    raise HTTPException(
                        status.HTTP_400_BAD_REQUEST, detail=f"压缩包包含非法路径: {member.name}"
                    )
            tf.extractall(dest)  # noqa: S202 - members validated above
        return

    raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="仅支持 .zip / .tar / .tar.gz 压缩包")


def _flatten_single_root(root: Path) -> None:
    """``foo.zip`` that contains a single ``foo/`` directory is unwrapped."""
    entries = [p for p in root.iterdir() if p.name not in {"__MACOSX", ".DS_Store"}]
    if len(entries) == 1 and entries[0].is_dir():
        inner = entries[0]
        for child in list(inner.iterdir()):
            shutil.move(str(child), str(root / child.name))
        inner.rmdir()


# --------------------------------------------------------------------------- #
# Validation per kind
# --------------------------------------------------------------------------- #
def _iter_records(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8-sig", errors="strict")
    if path.suffix == ".jsonl":
        rows = []
        for line in text.splitlines():
            line = line.strip()
            if line:
                rows.append(json.loads(line))
        return rows
    data = json.loads(text)
    if isinstance(data, dict):
        for key in ("data", "records", "instances", "train"):
            if isinstance(data.get(key), list):
                return data[key]
        return [data]
    if not isinstance(data, list):
        raise ValueError("JSON 顶层必须是数组")
    return data


def _truncate(value: Any) -> Any:
    if isinstance(value, str) and len(value) > MAX_PREVIEW_CHARS:
        return value[:MAX_PREVIEW_CHARS] + " …"
    if isinstance(value, list):
        return [_truncate(v) for v in value[:6]]
    if isinstance(value, dict):
        return {k: _truncate(v) for k, v in list(value.items())[:12]}
    return value


_REQUIRED_ANY: dict[DatasetKind, list[list[str]]] = {
    # each inner list is one acceptable field combination
    DatasetKind.SFT_ALPACA: [["instruction", "output"], ["messages"], ["conversations"]],
    DatasetKind.SFT_SHAREGPT: [["conversations"], ["messages"]],
    DatasetKind.DPO_PAIRS: [["chosen", "rejected"]],
    DatasetKind.KTO_PAIRS: [["messages", "label"], ["instruction", "output", "label"]],
}


def _validate_json_dataset(root: Path, kind: DatasetKind) -> tuple[DatasetStats, list[DatasetFileInfo], dict[str, Any]]:
    files = sorted(
        p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in DATA_SUFFIXES and p.name != "dataset_info.json"
    )
    if not files:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="压缩包内没有找到 .json / .jsonl 数据文件")

    stats = DatasetStats()
    infos: list[DatasetFileInfo] = []
    lf_entries: dict[str, Any] = {}
    columns: set[str] = set()
    accepted = _REQUIRED_ANY.get(kind, [])

    for path in files:
        rel = path.relative_to(root).as_posix()
        try:
            records = _iter_records(path)
        except (json.JSONDecodeError, ValueError, UnicodeDecodeError) as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=f"{rel} 解析失败: {exc}") from exc
        if not records:
            stats.warnings.append(f"{rel} 为空，已跳过")
            continue
        if not isinstance(records[0], dict):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=f"{rel} 的元素必须是对象")

        keys = set(records[0].keys())
        columns |= keys
        if accepted and not any(all(field in keys for field in combo) for combo in accepted):
            expected = " 或 ".join("+".join(combo) for combo in accepted)
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail=f"{rel} 缺少必需字段（需要 {expected}），实际字段: {sorted(keys)}",
            )

        role = "eval" if any(tag in rel.lower() for tag in ("eval", "valid", "test", "dev")) else "train"
        infos.append(
            DatasetFileInfo(path=rel, size_bytes=path.stat().st_size, records=len(records), role=role)
        )
        stats.n_records += len(records)
        stats.total_bytes += path.stat().st_size
        if not stats.sample:
            stats.sample = [_truncate(r) for r in records[:MAX_SAMPLE_ROWS]]

        lf_entries[_entry_name(rel)] = _lf_entry(str(path.resolve()), kind, keys)

    stats.n_files = len(infos)
    stats.columns = sorted(columns)
    return stats, infos, lf_entries


def _entry_name(rel_path: str) -> str:
    return Path(rel_path).stem.replace(" ", "_")


def _lf_entry(abs_path: str, kind: DatasetKind, keys: set[str]) -> dict[str, Any]:
    """Build the LLaMA-Factory ``dataset_info.json`` entry for one file."""
    entry: dict[str, Any] = {"file_name": abs_path}
    sharegpt_style = "conversations" in keys or "messages" in keys

    if kind in {DatasetKind.SFT_SHAREGPT} or (kind == DatasetKind.SFT_ALPACA and sharegpt_style):
        entry["formatting"] = "sharegpt"
        columns = {"messages": "conversations" if "conversations" in keys else "messages"}
        if "system" in keys:
            columns["system"] = "system"
        if "tools" in keys:
            columns["tools"] = "tools"
        entry["columns"] = columns
    elif kind == DatasetKind.DPO_PAIRS:
        entry["ranking"] = True
        if sharegpt_style:
            entry["formatting"] = "sharegpt"
            entry["columns"] = {
                "messages": "conversations" if "conversations" in keys else "messages",
                "chosen": "chosen",
                "rejected": "rejected",
            }
        else:
            entry["columns"] = {
                "prompt": "instruction" if "instruction" in keys else "prompt",
                "query": "input" if "input" in keys else "",
                "chosen": "chosen",
                "rejected": "rejected",
            }
            entry["columns"] = {k: v for k, v in entry["columns"].items() if v}
    elif kind == DatasetKind.KTO_PAIRS:
        entry["formatting"] = "sharegpt" if sharegpt_style else "alpaca"
        columns = {"kto_tag": "label"}
        if sharegpt_style:
            columns["messages"] = "conversations" if "conversations" in keys else "messages"
        else:
            columns["prompt"] = "instruction"
            columns["response"] = "output"
        entry["columns"] = columns
    else:  # alpaca
        columns = {}
        for source, target in (("instruction", "prompt"), ("input", "query"), ("output", "response"), ("system", "system"), ("history", "history")):
            if source in keys:
                columns[target] = source
        if columns:
            entry["columns"] = columns
    return entry


def _validate_harbor_tasks(root: Path) -> tuple[DatasetStats, list[DatasetFileInfo], dict[str, Any]]:
    task_files = sorted(root.rglob("task.toml"))
    if not task_files:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="未找到任何 task.toml，请确认是 Harbor 任务树（tasks/<task>/task.toml）",
        )
    stats = DatasetStats(n_tasks=len(task_files))
    infos: list[DatasetFileInfo] = []
    missing_instruction: list[str] = []
    missing_tests: list[str] = []
    missing_solution: list[str] = []

    for task_file in task_files:
        task_dir = task_file.parent
        rel = task_dir.relative_to(root).as_posix()
        if not (task_dir / "instruction.md").is_file():
            missing_instruction.append(rel)
        if not any((task_dir / "tests").glob("test.*")):
            missing_tests.append(rel)
        if not any((task_dir / "solution").glob("solve.*")):
            missing_solution.append(rel)
        size = sum(p.stat().st_size for p in task_dir.rglob("*") if p.is_file())
        stats.total_bytes += size
        if len(infos) < 500:  # keep the record list bounded for huge suites
            infos.append(DatasetFileInfo(path=rel, size_bytes=size, role="tasks"))

    if missing_instruction:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=f"{len(missing_instruction)} 个任务缺少 instruction.md，例如: {missing_instruction[:3]}",
        )
    if missing_tests:
        stats.warnings.append(
            f"{len(missing_tests)} 个任务没有 tests/test.*，评测时必须使用 --disable-verification 或补齐测试"
        )
    if missing_solution:
        stats.warnings.append(
            f"{len(missing_solution)} 个任务没有 solution/solve.*，oracle agent 无法自检这些任务"
        )

    stats.n_files = len(task_files)
    # Harbor's own root: prefer a `tasks/` subdir when present.
    tasks_root = root / "tasks" if (root / "tasks").is_dir() else root
    return stats, infos, {"_harbor_tasks_root": str(tasks_root.resolve())}


def _validate_parquet(root: Path) -> tuple[DatasetStats, list[DatasetFileInfo], dict[str, Any]]:
    files = sorted(root.rglob("*.parquet"))
    if not files:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="未找到 .parquet 文件")
    stats = DatasetStats(n_files=len(files))
    infos: list[DatasetFileInfo] = []
    train_files: list[str] = []
    val_files: list[str] = []

    try:  # optional: richer stats when pyarrow is installed
        import pyarrow.parquet as pq  # type: ignore
    except ImportError:
        pq = None
        stats.warnings.append("未安装 pyarrow，跳过行数与列名校验")

    for path in files:
        rel = path.relative_to(root).as_posix()
        rows = None
        if pq is not None:
            try:
                meta = pq.ParquetFile(path)
                rows = meta.metadata.num_rows
                stats.columns = sorted(set(stats.columns) | set(meta.schema.names))
            except Exception as exc:  # pragma: no cover
                stats.warnings.append(f"{rel} 读取失败: {exc}")
        role = "eval" if any(tag in rel.lower() for tag in ("test", "val", "eval")) else "train"
        (val_files if role == "eval" else train_files).append(str(path.resolve()))
        stats.total_bytes += path.stat().st_size
        if rows:
            stats.n_records += rows
        infos.append(DatasetFileInfo(path=rel, size_bytes=path.stat().st_size, records=rows, role=role))

    if not train_files:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="未找到训练用 parquet（文件名不含 test/val/eval）")
    return stats, infos, {"_verl_train_files": train_files, "_verl_val_files": val_files}


def _validate_raw(root: Path) -> tuple[DatasetStats, list[DatasetFileInfo], dict[str, Any]]:
    stats = DatasetStats()
    infos: list[DatasetFileInfo] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        size = path.stat().st_size
        stats.total_bytes += size
        stats.n_files += 1
        if len(infos) < 300:
            infos.append(DatasetFileInfo(path=path.relative_to(root).as_posix(), size_bytes=size))
    return stats, infos, {}


VALIDATORS = {
    DatasetKind.SFT_ALPACA: lambda root: _validate_json_dataset(root, DatasetKind.SFT_ALPACA),
    DatasetKind.SFT_SHAREGPT: lambda root: _validate_json_dataset(root, DatasetKind.SFT_SHAREGPT),
    DatasetKind.DPO_PAIRS: lambda root: _validate_json_dataset(root, DatasetKind.DPO_PAIRS),
    DatasetKind.KTO_PAIRS: lambda root: _validate_json_dataset(root, DatasetKind.KTO_PAIRS),
    DatasetKind.HARBOR_TASKS: _validate_harbor_tasks,
    DatasetKind.VERL_PARQUET: _validate_parquet,
    DatasetKind.RAW: _validate_raw,
}


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def create_from_upload(
    *,
    name: str,
    kind: DatasetKind,
    description: str,
    tags: list[str],
    filename: str,
    stream: BinaryIO,
    owner: User,
) -> Dataset:
    dataset_id = new_id("ds")
    archive_path = settings.uploads_dir / f"{dataset_id}{_archive_suffix(filename)}"
    root = settings.datasets_dir / dataset_id

    total = 0
    limit = settings.max_upload_mb * 1024 * 1024
    with archive_path.open("wb") as handle:
        while True:
            chunk = stream.read(CHUNK)
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                handle.close()
                archive_path.unlink(missing_ok=True)
                raise HTTPException(
                    status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    detail=f"压缩包超过 {settings.max_upload_mb} MB 上限",
                )
            handle.write(chunk)

    if total == 0:
        archive_path.unlink(missing_ok=True)
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="上传内容为空")

    dataset = Dataset(
        id=dataset_id,
        name=name.strip() or dataset_id,
        kind=kind,
        description=description,
        tags=tags,
        owner_id=owner.id,
        owner_email=owner.email,
        status=DatasetStatus.EXTRACTING,
        root_path=str(root),
        archive_name=filename,
        archive_bytes=total,
    )
    store.datasets.put(dataset)

    try:
        _safe_extract(archive_path, root)
        _flatten_single_root(root)
        dataset.status = DatasetStatus.VALIDATING
        store.datasets.put(dataset)

        stats, files, extra = VALIDATORS[kind](root)
        dataset.stats = stats
        dataset.files = files
        dataset.status = DatasetStatus.READY
        dataset.updated_at = utcnow()

        lf_entries = {k: v for k, v in extra.items() if not k.startswith("_")}
        if lf_entries:
            _write_dataset_info(root, lf_entries)
            dataset.lf_dataset_names = sorted(lf_entries)
        if "_harbor_tasks_root" in extra:
            dataset.root_path = extra["_harbor_tasks_root"]
        if "_verl_train_files" in extra:
            _write_meta(root, {"train_files": extra["_verl_train_files"], "val_files": extra["_verl_val_files"]})
    except HTTPException as exc:
        dataset.status = DatasetStatus.INVALID
        dataset.error = str(exc.detail)
        store.datasets.put(dataset)
        raise
    except Exception as exc:  # pragma: no cover
        dataset.status = DatasetStatus.INVALID
        dataset.error = f"{type(exc).__name__}: {exc}"
        store.datasets.put(dataset)
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=dataset.error) from exc
    finally:
        archive_path.unlink(missing_ok=True)

    store.datasets.put(dataset)
    audit_service.record(
        "dataset.create",
        actor=owner,
        target_type="dataset",
        target_id=dataset.id,
        summary=f"上传数据集 {dataset.name} ({kind.value})",
        detail={"records": dataset.stats.n_records, "tasks": dataset.stats.n_tasks},
    )
    return dataset


def _archive_suffix(filename: str) -> str:
    lowered = filename.lower()
    for suffix in (".tar.gz", ".tgz", ".tar", ".zip"):
        if lowered.endswith(suffix):
            return suffix
    return ".zip"


def _write_dataset_info(root: Path, entries: dict[str, Any]) -> None:
    (root / "dataset_info.json").write_text(
        json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _write_meta(root: Path, meta: dict[str, Any]) -> None:
    (root / ".prt_meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


def read_meta(dataset: Dataset) -> dict[str, Any]:
    path = Path(dataset.root_path) / ".prt_meta.json"
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def lf_entries(dataset: Dataset) -> dict[str, Any]:
    path = Path(dataset.root_path) / "dataset_info.json"
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def list_datasets(*, kind: DatasetKind | None = None, owner_id: str | None = None) -> list[Dataset]:
    items = store.datasets.find(lambda d: d.status != DatasetStatus.DELETED)
    if kind:
        items = [d for d in items if d.kind == kind]
    if owner_id:
        items = [d for d in items if d.owner_id == owner_id]
    items.sort(key=lambda d: d.created_at, reverse=True)
    return items


def get_dataset(dataset_id: str) -> Dataset:
    dataset = store.datasets.get(dataset_id)
    if dataset is None or dataset.status == DatasetStatus.DELETED:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="数据集不存在")
    return dataset


def update_dataset(dataset_id: str, payload: DatasetUpdate, *, actor: User) -> Dataset:
    dataset = get_dataset(dataset_id)
    if dataset.owner_id != actor.id and not actor.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="只能修改自己上传的数据集")

    def mutate(item: Dataset) -> None:
        data = payload.model_dump(exclude_unset=True, exclude_none=True)
        for key, value in data.items():
            setattr(item, key, value)
        item.updated_at = utcnow()

    updated = store.datasets.update(dataset_id, mutate)
    assert updated is not None
    audit_service.record(
        "dataset.update", actor=actor, target_type="dataset", target_id=dataset_id, summary=f"更新数据集 {updated.name}"
    )
    return updated


def delete_dataset(dataset_id: str, *, actor: User, purge: bool = True) -> None:
    dataset = get_dataset(dataset_id)
    if dataset.owner_id != actor.id and not actor.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="只能删除自己上传的数据集")

    in_use = store.jobs.find(
        lambda j: j.status.is_active
        and (
            (j.sft and dataset_id in (j.sft.dataset_ids + j.sft.eval_dataset_ids))
            or (j.rl and dataset_id in {j.rl.dataset_id, j.rl.eval_dataset_id})
            or (j.verify and j.verify.dataset_id == dataset_id)
        )
    )
    if in_use:
        raise HTTPException(
            status.HTTP_409_CONFLICT, detail=f"数据集正被 {len(in_use)} 个进行中的任务使用，无法删除"
        )

    if purge:
        base = settings.datasets_dir / dataset_id
        if base.is_dir():
            shutil.rmtree(base, ignore_errors=True)
    store.datasets.delete(dataset_id)
    audit_service.record(
        "dataset.delete", actor=actor, target_type="dataset", target_id=dataset_id, summary=f"删除数据集 {dataset.name}"
    )


def preview(dataset_id: str, limit: int = 5) -> dict[str, Any]:
    dataset = get_dataset(dataset_id)
    root = Path(dataset.root_path)
    if dataset.kind == DatasetKind.HARBOR_TASKS:
        tasks = sorted(root.rglob("task.toml"))[:limit]
        items = []
        for task in tasks:
            instruction = task.parent / "instruction.md"
            items.append(
                {
                    "task": task.parent.name,
                    "task_toml": task.read_text(encoding="utf-8", errors="replace")[:2000],
                    "instruction": instruction.read_text(encoding="utf-8", errors="replace")[:2000]
                    if instruction.is_file()
                    else "",
                }
            )
        return {"kind": dataset.kind.value, "tasks": items}

    files = [f for f in dataset.files if f.role in {"train", "data"}]
    if not files:
        return {"kind": dataset.kind.value, "records": dataset.stats.sample}
    target = root / files[0].path
    if target.suffix.lower() not in DATA_SUFFIXES or not target.is_file():
        return {"kind": dataset.kind.value, "records": dataset.stats.sample}
    try:
        records = _iter_records(target)[:limit]
    except Exception:
        records = dataset.stats.sample
    return {"kind": dataset.kind.value, "file": files[0].path, "records": [_truncate(r) for r in records]}
