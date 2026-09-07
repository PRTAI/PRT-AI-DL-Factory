"""Local model zoo: discovery, search, framework hints and ModelScope pulls."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import HTTPException, status

from ..config import settings
from ..models import DownloadStatus, ModelDownload, ModelEntry, User, utcnow
from ..store import store
from . import audit_service
from .resources_service import framework_support

_SCAN_LOCK = threading.RLock()
_CACHE: list[ModelEntry] = []
_CACHE_AT: float = 0.0
_CACHE_TTL = 60.0  # seconds

_WEIGHT_SUFFIXES = (".safetensors", ".bin", ".pt", ".pth", ".gguf")
_BYTES_PER_PARAM = {"bfloat16": 2, "float16": 2, "float32": 4, "int8": 1, "float8_e4m3fn": 1}


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #
def _looks_like_model_dir(path: Path) -> bool:
    if (path / "config.json").is_file():
        return True
    try:
        for entry in os.scandir(path):
            if entry.is_file() and entry.name.endswith(_WEIGHT_SUFFIXES):
                return True
    except OSError:
        return False
    return False


def _dir_size(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path, followlinks=False):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                continue
    return total


def _param_estimate(path: Path, config: dict[str, Any], size_bytes: int) -> float | None:
    index = path / "model.safetensors.index.json"
    total = None
    if index.is_file():
        try:
            total = json.loads(index.read_text(encoding="utf-8")).get("metadata", {}).get("total_size")
        except (json.JSONDecodeError, OSError):
            total = None
    if total is None:
        weights = 0
        try:
            for entry in os.scandir(path):
                if entry.is_file() and entry.name.endswith(_WEIGHT_SUFFIXES):
                    weights += entry.stat().st_size
        except OSError:
            pass
        total = weights or size_bytes
    if not total:
        return None
    dtype = str(config.get("torch_dtype") or "bfloat16").lower()
    per_param = _BYTES_PER_PARAM.get(dtype, 2)
    return round(total / per_param / 1e9, 2)


def _read_config(path: Path) -> dict[str, Any]:
    config_path = path / "config.json"
    if not config_path.is_file():
        return {}
    try:
        return json.loads(config_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _text_config(config: dict[str, Any]) -> dict[str, Any]:
    """VLM configs nest the LM hyper-parameters under ``text_config``."""
    nested = config.get("text_config")
    return nested if isinstance(nested, dict) else config


def _build_entry(vendor: str, path: Path) -> ModelEntry:
    config = _read_config(path)
    text_config = _text_config(config)
    size_bytes = _dir_size(path)
    architectures = [str(a) for a in (config.get("architectures") or [])]
    is_multimodal = bool(
        config.get("vision_config")
        or config.get("audio_config")
        or any("Vision" in a or "VL" in a or "Omni" in a for a in architectures)
    )
    entry = ModelEntry(
        id=f"{vendor}/{path.name}" if vendor else path.name,
        vendor=vendor,
        name=path.name,
        path=str(path),
        size_bytes=size_bytes,
        model_type=str(config.get("model_type") or ""),
        architectures=architectures,
        num_parameters_b=_param_estimate(path, config, size_bytes),
        hidden_size=text_config.get("hidden_size"),
        num_layers=text_config.get("num_hidden_layers"),
        max_position_embeddings=text_config.get("max_position_embeddings"),
        torch_dtype=str(config.get("torch_dtype") or ""),
        has_tokenizer=(path / "tokenizer_config.json").is_file() or (path / "tokenizer.json").is_file(),
        is_multimodal=is_multimodal,
        modified_at=datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc),
    )
    _annotate_framework_support(entry)
    return entry


def _annotate_framework_support(entry: ModelEntry) -> None:
    """Attach LlamaFactory template + framework compatibility hints."""
    support = framework_support()
    lf = support.get("llamafactory", {})
    lookup_by_path: dict[str, dict] = {}
    lookup_by_name: dict[str, dict] = {}
    for model in lf.get("models", []):
        for key in (model.get("hf"), model.get("modelscope"), model.get("openmind")):
            if key:
                lookup_by_path[key.lower()] = model
        lookup_by_name[model["name"].lower()] = model

    match = lookup_by_path.get(entry.id.lower()) or lookup_by_name.get(entry.name.lower())
    if match is None:
        # fall back to a prefix match on the directory name (e.g. local renames)
        for name, model in lookup_by_name.items():
            if entry.name.lower().startswith(name) or name.startswith(entry.name.lower()):
                match = model
                break

    frameworks: list[str] = []
    notes: list[str] = []
    if match and match.get("template"):
        entry.llamafactory_template = match["template"]
        frameworks.append("llamafactory")
    elif match:
        entry.llamafactory_template = "default"
        frameworks.append("llamafactory")
        notes.append("LLaMA-Factory 已收录该模型，但未定义对话模板（base 模型通常使用 default）")
    else:
        notes.append("未在 LLaMA-Factory 模型表中匹配到，SFT 时需手动指定 template")

    causal = any(a.endswith("ForCausalLM") or a.endswith("ForConditionalGeneration") for a in entry.architectures)
    if causal:
        frameworks.extend(["verl", "trl"])
    else:
        notes.append("未检测到 CausalLM 架构，verl / TRL 的 RL 训练可能不适用")

    if entry.is_multimodal:
        notes.append("多模态模型：verl 需使用 VL 专用配置，TRL 需使用 VLM 版脚本")

    verl_patches = support.get("verl", {}).get("transformers_patches", [])
    if entry.model_type and entry.model_type in verl_patches:
        notes.append(f"verl 为 {entry.model_type} 提供了专用加速补丁")

    entry.supported_frameworks = sorted(set(frameworks))
    entry.notes = notes


def scan_models(force: bool = False) -> list[ModelEntry]:
    global _CACHE, _CACHE_AT
    with _SCAN_LOCK:
        if not force and _CACHE and (time.time() - _CACHE_AT) < _CACHE_TTL:
            return _CACHE
        root = settings.models_root
        entries: list[ModelEntry] = []
        if root.is_dir():
            for vendor_dir in sorted(root.iterdir()):
                if not vendor_dir.is_dir() or vendor_dir.name.startswith("."):
                    continue
                if _looks_like_model_dir(vendor_dir):
                    entries.append(_build_entry("", vendor_dir))
                    continue
                for model_dir in sorted(vendor_dir.iterdir()):
                    if not model_dir.is_dir() or model_dir.name.startswith("."):
                        continue
                    if _looks_like_model_dir(model_dir):
                        entries.append(_build_entry(vendor_dir.name, model_dir))
        entries.sort(key=lambda m: m.id.lower())
        _CACHE, _CACHE_AT = entries, time.time()
        return entries


def search_models(query: str = "", *, framework: str = "", limit: int = 200) -> list[ModelEntry]:
    items = scan_models()
    tokens = [t for t in query.lower().split() if t]
    if tokens:
        items = [
            m
            for m in items
            if all(
                t in m.id.lower() or t in m.model_type.lower() or any(t in a.lower() for a in m.architectures)
                for t in tokens
            )
        ]
    if framework:
        items = [m for m in items if framework in m.supported_frameworks]
    return items[:limit]


def get_model(model_id: str) -> ModelEntry | None:
    for item in scan_models():
        if item.id == model_id:
            return item
    return None


def resolve_model_path(model_id: str, explicit_path: str = "") -> str:
    """Accept either a registry id or an absolute path; validate it exists."""
    if explicit_path:
        path = Path(explicit_path)
        if not path.is_dir():
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=f"模型路径不存在: {explicit_path}")
        return str(path)
    entry = get_model(model_id)
    if entry is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"未找到模型: {model_id}")
    return entry.path


# --------------------------------------------------------------------------- #
# ModelScope downloads
# --------------------------------------------------------------------------- #
def _modelscope_bin() -> str:
    configured = settings.modelscope_bin
    if configured and (shutil.which(configured) or Path(configured).is_file()):
        return configured
    for candidate in (
        "/opt/miniconda3/envs/modelscope/bin/modelscope",
        shutil.which("modelscope"),
    ):
        if candidate and Path(candidate).is_file():
            return candidate
    raise HTTPException(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="未找到 modelscope 命令，请在系统设置中配置 PRT_MODELSCOPE_BIN",
    )


def start_download(model_id: str, *, revision: str = "", actor: User) -> ModelDownload:
    model_id = model_id.strip().strip("/")
    if "/" not in model_id or len(model_id.split("/")) != 2:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="模型 ID 需形如 {厂家}/{模型名}")
    if any(part in {"", ".", ".."} for part in model_id.split("/")):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="非法的模型 ID")

    running = store.downloads.first(
        lambda d: d.model_id == model_id and d.status in {DownloadStatus.QUEUED, DownloadStatus.RUNNING}
    )
    if running is not None:
        return running

    target = settings.models_root / model_id
    if target.is_dir() and any(target.iterdir()):
        raise HTTPException(status.HTTP_409_CONFLICT, detail=f"模型已存在: {target}")

    binary = _modelscope_bin()
    record = ModelDownload(
        model_id=model_id,
        revision=revision,
        target_path=str(target),
        requested_by=actor.id,
        requested_by_email=actor.email,
        status=DownloadStatus.QUEUED,
    )
    log_file = settings.downloads_dir / f"{record.id}.log"
    record.log_file = str(log_file)
    store.downloads.put(record)

    thread = threading.Thread(
        target=_run_download, args=(record.id, binary, model_id, revision, target, log_file), daemon=True
    )
    thread.start()

    audit_service.record(
        "model.download",
        actor=actor,
        target_type="model",
        target_id=model_id,
        summary=f"下载模型 {model_id}",
    )
    return record


def _run_download(
    download_id: str, binary: str, model_id: str, revision: str, target: Path, log_file: Path
) -> None:
    command = [binary, "download", "--model", model_id, "--local_dir", str(target)]
    if revision:
        command += ["--revision", revision]
    target.mkdir(parents=True, exist_ok=True)

    def mark(status_value: DownloadStatus, **fields: Any) -> None:
        def mutate(record: ModelDownload) -> None:
            record.status = status_value
            for key, value in fields.items():
                setattr(record, key, value)

        store.downloads.update(download_id, mutate)

    try:
        with log_file.open("w", encoding="utf-8") as handle:
            handle.write(f"$ {' '.join(command)}\n\n")
            handle.flush()
            process = subprocess.Popen(
                command,
                stdout=handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                cwd=str(settings.models_root),
            )
            mark(
                DownloadStatus.RUNNING,
                pid=process.pid,
                pgid=os.getpgid(process.pid),
                started_at=utcnow(),
            )
            exit_code = process.wait()
    except Exception as exc:  # pragma: no cover - environment dependent
        mark(DownloadStatus.FAILED, finished_at=utcnow(), error=str(exc))
        return

    size = _dir_size(target) if target.is_dir() else 0
    if exit_code == 0:
        mark(
            DownloadStatus.SUCCEEDED,
            finished_at=utcnow(),
            exit_code=exit_code,
            downloaded_bytes=size,
        )
        scan_models(force=True)
        record = store.downloads.get(download_id)
        if record:
            audit_service.notify(
                record.requested_by,
                f"模型下载完成: {model_id}",
                body=f"已保存到 {target}",
                level="success",
                link="/models",
            )
    else:
        mark(
            DownloadStatus.FAILED,
            finished_at=utcnow(),
            exit_code=exit_code,
            error=f"modelscope 退出码 {exit_code}",
            downloaded_bytes=size,
        )
        record = store.downloads.get(download_id)
        if record:
            audit_service.notify(
                record.requested_by,
                f"模型下载失败: {model_id}",
                body=f"退出码 {exit_code}，请查看下载日志",
                level="error",
                link="/models",
            )


def cancel_download(download_id: str, *, actor: User) -> ModelDownload:
    record = store.downloads.get(download_id)
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="下载任务不存在")
    if record.status not in {DownloadStatus.QUEUED, DownloadStatus.RUNNING}:
        return record
    if record.pgid:
        try:
            os.killpg(record.pgid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass

    def mutate(item: ModelDownload) -> None:
        item.status = DownloadStatus.CANCELLED
        item.finished_at = utcnow()

    updated = store.downloads.update(download_id, mutate)
    audit_service.record(
        "model.download_cancel",
        actor=actor,
        target_type="model",
        target_id=record.model_id,
        summary=f"取消下载 {record.model_id}",
    )
    return updated or record


def list_downloads(limit: int = 50) -> list[ModelDownload]:
    items = store.downloads.all()
    items.sort(key=lambda d: d.created_at, reverse=True)
    return items[:limit]


def download_log(download_id: str, tail_bytes: int = 100_000) -> str:
    record = store.downloads.get(download_id)
    if record is None or not record.log_file:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="下载日志不存在")
    path = Path(record.log_file)
    if not path.is_file():
        return ""
    size = path.stat().st_size
    with path.open("rb") as handle:
        if size > tail_bytes:
            handle.seek(size - tail_bytes)
        return handle.read().decode("utf-8", errors="replace")
