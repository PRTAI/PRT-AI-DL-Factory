"""Export a job's configuration, working files and outputs as a temporary ZIP."""

from __future__ import annotations

import json
import os
import re
import tempfile
import zipfile
from pathlib import Path
from typing import Any, BinaryIO

from ..config import settings
from ..models import Job, User, utcnow
from ..redaction import SECRET_KEY as _SECRET_KEY, secret_values as _secret_values, mask_values as _mask
from ..store import store
from . import audit_service, job_service, runner

_BINARY_SUFFIXES = {".bin", ".pt", ".pth", ".ckpt", ".safetensors", ".zip", ".gz", ".png", ".jpg", ".jpeg", ".pdf", ".docx", ".xlsx", ".parquet", ".npy", ".npz"}
_PRIVATE_NAMES = {"secrets.json", "credentials.json", "auth.json"}
_CHUNK = 1024 * 1024


def _secrets_for(job: Job, payload: dict[str, Any], work: Path) -> list[str]:
    secrets = _secret_values(payload)
    if job.verify:
        for model_id in (job.verify.model_api_id, job.verify.judge_model_api_id):
            model = store.model_apis.get(model_id) if model_id else None
            if model and model.api_key:
                secrets.add(model.api_key)
            if model:
                secrets.update(_secret_values(model.extra_env))
                platform = store.model_platforms.get(model.platform_id) if model.platform_id else None
                if platform and platform.api_key:
                    secrets.add(platform.api_key)
    env_file = work / ".env"
    if not work.is_symlink() and env_file.is_file() and not env_file.is_symlink():
        # 保留运行时旧密钥的脱敏能力，即使模型配置里的密钥已经轮换。
        for line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
            key, separator, value = line.partition("=")
            if separator and _SECRET_KEY.search(key) and value:
                secrets.add(value)
    return sorted(secrets, key=len, reverse=True)


def _copy_masked(source: BinaryIO, target: BinaryIO, secrets: list[str]) -> None:
    """Bounded-memory replacement, including secrets spanning chunk boundaries."""
    if not secrets:
        while chunk := source.read(_CHUNK):
            target.write(chunk)
        return
    encoded = sorted({
        value.encode("utf-8")
        for secret in secrets
        for value in (secret, json.dumps(secret, ensure_ascii=False)[1:-1], json.dumps(secret)[1:-1])
    }, key=len, reverse=True)
    matcher = re.compile(b"|".join(re.escape(secret) for secret in encoded))
    overlap = max(map(len, encoded)) - 1
    pending = b""
    while chunk := source.read(_CHUNK):
        pending += chunk
        cut = max(0, len(pending) - overlap)
        for match in matcher.finditer(pending):
            if match.start() >= cut:
                break
            cut = max(cut, match.end())
        target.write(matcher.sub(b"***", pending[:cut]))
        pending = pending[cut:]
    target.write(matcher.sub(b"***", pending))


def prepare_download(job_id: str, *, actor: User) -> tuple[Path, str]:
    job = job_service.get_job_for(job_id, actor)
    work = runner.work_dir_for(job)
    output = runner.output_dir_for(job)
    payload = job.model_dump(mode="json")
    secrets = _secrets_for(job, payload, work)
    project = store.projects.get(job.project_id)
    payload["project_name"] = project.name if project else ""
    payload["exported_at"] = utcnow().isoformat()
    fd, name = tempfile.mkstemp(prefix="prt-job-export-", suffix=".zip", dir=settings.downloads_dir)
    os.close(fd)
    target = Path(name)
    try:
        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
            archive.writestr("job.json", json.dumps(_mask(payload, secrets), ensure_ascii=False, indent=2))
            for prefix, root in (("work", work), ("outputs", output)):
                if root.is_symlink() or not root.is_dir():
                    continue
                for path in sorted(root.rglob("*")):
                    relative = path.relative_to(root)
                    if (
                        path.is_symlink()
                        or any(part.startswith(".") for part in relative.parts)
                        or path.name.lower() in _PRIVATE_NAMES
                        or path.suffix.lower() in {".env", ".pem", ".key"}
                        or not path.is_file()
                    ):
                        continue
                    destination = f"{prefix}/{relative.as_posix()}"
                    try:
                        if path.suffix.lower() in _BINARY_SUFFIXES:
                            archive.write(path, destination, compress_type=zipfile.ZIP_STORED)
                        else:
                            with path.open("rb") as source, archive.open(destination, "w", force_zip64=True) as entry:
                                _copy_masked(source, entry, secrets)
                    except FileNotFoundError:
                        # 运行中的任务可能清理旧 checkpoint，导出当前仍存在的内容。
                        continue
        audit_service.record(
            "job.download", actor=actor, target_type="job", target_id=job.id,
            summary=f"导出下载任务「{job.name}」",
        )
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    label = _mask(f"{project.name + '_' if project else ''}{job.name}", secrets)
    label = re.sub(r"[^\w .-]", "_", label)[:100].strip(" .")
    return target, f"{label or 'job'}_{job.id}.zip"
