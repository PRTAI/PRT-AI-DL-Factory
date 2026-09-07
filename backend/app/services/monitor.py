"""System telemetry sampler: CPU / memory / GPU / disk / network.

Keeps a fixed-size ring buffer in memory (default ~30 min at 2 s resolution) so
the dashboard can render Task-Manager style rolling charts without a TSDB.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
import threading
import time
from collections import deque
from typing import Any

import psutil

from ..config import settings

logger = logging.getLogger("prt.monitor")

_LOCK = threading.RLock()
_HISTORY: deque[dict[str, Any]] = deque(maxlen=settings.monitor_history_points)
_NVML_READY: bool | None = None
_GPU_HANDLES: list[Any] = []
_LAST_NET: tuple[float, int, int] | None = None
_LAST_DISK: tuple[float, int, int] | None = None


def _init_nvml() -> bool:
    global _NVML_READY, _GPU_HANDLES
    if _NVML_READY is not None:
        return _NVML_READY
    try:
        import pynvml

        pynvml.nvmlInit()
        count = pynvml.nvmlDeviceGetCount()
        _GPU_HANDLES = [pynvml.nvmlDeviceGetHandleByIndex(i) for i in range(count)]
        _NVML_READY = True
    except Exception as exc:  # pragma: no cover - depends on the host
        logger.info("NVML unavailable (%s); GPU metrics disabled", exc)
        _GPU_HANDLES = []
        _NVML_READY = False
    return _NVML_READY


def gpu_samples() -> list[dict[str, Any]]:
    if not _init_nvml():
        return []
    import pynvml

    out: list[dict[str, Any]] = []
    for index, handle in enumerate(_GPU_HANDLES):
        entry: dict[str, Any] = {"index": index}
        try:
            entry["name"] = pynvml.nvmlDeviceGetName(handle)
            if isinstance(entry["name"], bytes):
                entry["name"] = entry["name"].decode()
        except Exception:
            entry["name"] = f"GPU {index}"
        for key, getter in (
            ("utilization", lambda h: pynvml.nvmlDeviceGetUtilizationRates(h).gpu),
            ("memory_utilization", lambda h: pynvml.nvmlDeviceGetUtilizationRates(h).memory),
            ("temperature", lambda h: pynvml.nvmlDeviceGetTemperature(h, pynvml.NVML_TEMPERATURE_GPU)),
        ):
            try:
                entry[key] = getter(handle)
            except Exception:
                entry[key] = None
        try:
            mem = pynvml.nvmlDeviceGetMemoryInfo(handle)
            entry["memory_used"] = int(mem.used)
            entry["memory_total"] = int(mem.total)
            entry["memory_percent"] = round(mem.used / mem.total * 100, 1) if mem.total else 0.0
        except Exception:
            entry["memory_used"] = entry["memory_total"] = 0
            entry["memory_percent"] = 0.0
        try:
            entry["power_watts"] = round(pynvml.nvmlDeviceGetPowerUsage(handle) / 1000, 1)
            entry["power_limit_watts"] = round(
                pynvml.nvmlDeviceGetEnforcedPowerLimit(handle) / 1000, 1
            )
        except Exception:
            entry["power_watts"] = entry["power_limit_watts"] = None
        try:
            procs = pynvml.nvmlDeviceGetComputeRunningProcesses(handle)
            entry["processes"] = [
                {"pid": int(p.pid), "memory": int(p.usedGpuMemory or 0)} for p in procs
            ]
        except Exception:
            entry["processes"] = []
        out.append(entry)
    return out


def sample() -> dict[str, Any]:
    global _LAST_NET, _LAST_DISK
    now = time.time()
    virtual = psutil.virtual_memory()
    swap = psutil.swap_memory()
    per_cpu = psutil.cpu_percent(percpu=True)
    load1, load5, load15 = psutil.getloadavg()

    net = psutil.net_io_counters()
    net_rate = {"sent_bps": 0.0, "recv_bps": 0.0}
    if _LAST_NET is not None:
        elapsed = max(now - _LAST_NET[0], 1e-6)
        net_rate = {
            "sent_bps": max(0.0, (net.bytes_sent - _LAST_NET[1]) / elapsed),
            "recv_bps": max(0.0, (net.bytes_recv - _LAST_NET[2]) / elapsed),
        }
    _LAST_NET = (now, net.bytes_sent, net.bytes_recv)

    disk_io = psutil.disk_io_counters()
    disk_rate = {"read_bps": 0.0, "write_bps": 0.0}
    if disk_io is not None:
        if _LAST_DISK is not None:
            elapsed = max(now - _LAST_DISK[0], 1e-6)
            disk_rate = {
                "read_bps": max(0.0, (disk_io.read_bytes - _LAST_DISK[1]) / elapsed),
                "write_bps": max(0.0, (disk_io.write_bytes - _LAST_DISK[2]) / elapsed),
            }
        _LAST_DISK = (now, disk_io.read_bytes, disk_io.write_bytes)

    usage = shutil.disk_usage(str(settings.data_root))
    gpus = gpu_samples()

    return {
        "at": now,
        "cpu": {
            "percent": round(sum(per_cpu) / len(per_cpu), 1) if per_cpu else 0.0,
            "per_core": [round(v, 1) for v in per_cpu],
            "count": psutil.cpu_count(logical=True) or 0,
            "physical": psutil.cpu_count(logical=False) or 0,
            "load": [round(load1, 2), round(load5, 2), round(load15, 2)],
        },
        "memory": {
            "percent": virtual.percent,
            "used": int(virtual.used),
            "total": int(virtual.total),
            "available": int(virtual.available),
            "swap_percent": swap.percent,
        },
        "disk": {
            "percent": round(usage.used / usage.total * 100, 1) if usage.total else 0.0,
            "used": int(usage.used),
            "total": int(usage.total),
            "free": int(usage.free),
            **{k: round(v, 1) for k, v in disk_rate.items()},
        },
        "network": {k: round(v, 1) for k, v in net_rate.items()},
        "gpu": {
            "count": len(gpus),
            "devices": gpus,
            "avg_utilization": round(
                sum(g.get("utilization") or 0 for g in gpus) / len(gpus), 1
            )
            if gpus
            else 0.0,
            "avg_memory_percent": round(
                sum(g.get("memory_percent") or 0 for g in gpus) / len(gpus), 1
            )
            if gpus
            else 0.0,
        },
    }


def latest() -> dict[str, Any]:
    with _LOCK:
        if _HISTORY:
            return _HISTORY[-1]
    return sample()


def history(limit: int = 300) -> list[dict[str, Any]]:
    with _LOCK:
        items = list(_HISTORY)
    return items[-limit:]


def compact_history(limit: int = 300) -> list[dict[str, Any]]:
    """Slim payload for charts: only the scalars the dashboard plots."""
    out = []
    for item in history(limit):
        out.append(
            {
                "at": item["at"],
                "cpu": item["cpu"]["percent"],
                "memory": item["memory"]["percent"],
                "gpu": item["gpu"]["avg_utilization"],
                "gpu_memory": item["gpu"]["avg_memory_percent"],
                "net_recv": item["network"]["recv_bps"],
                "net_sent": item["network"]["sent_bps"],
                "disk_read": item["disk"].get("read_bps", 0.0),
                "disk_write": item["disk"].get("write_bps", 0.0),
                "gpus": [
                    {"index": g["index"], "utilization": g.get("utilization") or 0, "memory": g.get("memory_percent") or 0}
                    for g in item["gpu"]["devices"]
                ],
            }
        )
    return out


class Monitor:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stop: asyncio.Event | None = None

    async def start(self) -> None:
        psutil.cpu_percent(percpu=True)  # prime the delta counters
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self._loop(), name="prt-monitor")

    async def stop(self) -> None:
        if self._stop:
            self._stop.set()
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _loop(self) -> None:
        assert self._stop is not None
        while not self._stop.is_set():
            try:
                snapshot = await asyncio.to_thread(sample)
                with _LOCK:
                    _HISTORY.append(snapshot)
            except Exception:  # pragma: no cover
                logger.exception("monitor sample failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=settings.monitor_interval_seconds)
            except asyncio.TimeoutError:
                continue


monitor = Monitor()
