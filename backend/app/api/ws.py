"""WebSocket streams: live system telemetry and live job logs."""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, status

from ..config import settings
from ..deps import get_ws_user
from ..services import monitor
from ..services.job_service import get_job
from ..services.scheduler import gpu_snapshot, refresh_metrics
from ..store import store

router = APIRouter(tags=["stream"])


@router.websocket("/ws/system")
async def system_stream(websocket: WebSocket) -> None:
    user = await get_ws_user(websocket)
    if user is None:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    await websocket.accept()
    try:
        await websocket.send_json(
            {"type": "history", "points": monitor.compact_history(180), "gpu": gpu_snapshot()}
        )
        while True:
            await asyncio.sleep(settings.monitor_interval_seconds)
            await websocket.send_json(
                {"type": "sample", "metrics": monitor.latest(), "gpu": gpu_snapshot()}
            )
    except (WebSocketDisconnect, RuntimeError):
        return
    except Exception:  # pragma: no cover
        with contextlib.suppress(RuntimeError):
            await websocket.close()


@router.websocket("/ws/jobs/{job_id}/log")
async def job_log_stream(websocket: WebSocket, job_id: str) -> None:
    user = await get_ws_user(websocket)
    if user is None:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    job = store.jobs.get(job_id)
    if job is None or not (user.is_admin or job.owner_id == user.id):
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await websocket.accept()
    log_path = Path(job.runtime.log_file or "")
    offset = 0
    if log_path.is_file():
        size = log_path.stat().st_size
        offset = max(0, size - settings.log_tail_bytes)

    try:
        while True:
            current = store.jobs.get(job_id)
            if current is None:
                break
            path = Path(current.runtime.log_file or "")
            if path.is_file():
                size = path.stat().st_size
                if size < offset:  # log rotated / job restarted
                    offset = 0
                if size > offset:
                    with path.open("rb") as handle:
                        handle.seek(offset)
                        chunk = handle.read(min(size - offset, 512_000))
                    offset += len(chunk)
                    await websocket.send_json(
                        {"type": "log", "content": chunk.decode("utf-8", errors="replace"), "offset": offset}
                    )
            if current.status.is_active:
                current = refresh_metrics(current)
            await websocket.send_json(
                {
                    "type": "status",
                    "status": current.status.value,
                    "progress": current.progress.model_dump(mode="json"),
                    "metrics": current.metrics.model_dump(mode="json"),
                }
            )
            if current.status.is_terminal:
                await asyncio.sleep(1.0)
                # one last drain so the tail of the log is never lost
                path = Path(current.runtime.log_file or "")
                if path.is_file() and path.stat().st_size > offset:
                    with path.open("rb") as handle:
                        handle.seek(offset)
                        chunk = handle.read()
                    await websocket.send_json(
                        {"type": "log", "content": chunk.decode("utf-8", errors="replace"), "offset": offset + len(chunk)}
                    )
                await websocket.send_json({"type": "end", "status": current.status.value})
                break
            await asyncio.sleep(1.5)
    except (WebSocketDisconnect, RuntimeError):
        return
    except Exception:  # pragma: no cover
        with contextlib.suppress(RuntimeError):
            await websocket.close()


__all__ = ["router", "get_job"]
