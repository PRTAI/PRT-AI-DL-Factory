"""Queue policy: decide what runs, on which GPUs, and keep job state honest.

A single background loop (started from the FastAPI lifespan) does three things
every tick:

1. **Reconcile** — running jobs whose process disappeared (platform restart,
   OOM-killer) are marked failed instead of hanging at "running" forever.
2. **Refresh** — parse each active job's logs into progress + metric curves.
3. **Dispatch** — start queued jobs while concurrency and GPU budgets allow.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
from pathlib import Path

from ..builders import BuildError
from ..config import settings
from ..models import Job, JobStatus, utcnow
from ..store import store
from . import audit_service, metrics_parser, runner

logger = logging.getLogger("prt.scheduler")

_DISPATCH_LOCK = threading.RLock()


class Scheduler:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self.total_gpus: int = 0

    # ------------------------------------------------------------- lifecycle
    async def start(self) -> None:
        self.total_gpus = runner.gpu_count()
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self._loop(), name="prt-scheduler")
        logger.info("scheduler started (%d GPU visible)", self.total_gpus)

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.to_thread(self.tick)
            except Exception:  # pragma: no cover - the loop must survive anything
                logger.exception("scheduler tick failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=settings.scheduler_interval_seconds)
            except asyncio.TimeoutError:
                continue

    # ------------------------------------------------------------------ tick
    def tick(self) -> None:
        self._reconcile()
        self._refresh_metrics()
        self.dispatch()

    def _reconcile(self) -> None:
        for job in store.jobs.find(lambda j: j.status == JobStatus.RUNNING):
            if runner.is_running(job):
                continue
            # Give the supervisor thread a moment to record the exit itself.
            current = store.jobs.get(job.id)
            if current is None or current.status != JobStatus.RUNNING:
                continue

            def mutate(item: Job) -> None:
                item.status = JobStatus.CANCELLED if item.runtime.cancel_requested else JobStatus.FAILED
                item.runtime.finished_at = utcnow()
                if not item.runtime.cancel_requested:
                    item.runtime.error = (
                        item.runtime.error
                        or "进程已不存在（可能在平台重启期间退出），请查看日志确认。"
                    )
                    item.progress.message = "进程丢失"
                item.updated_at = utcnow()

            updated = store.jobs.update(job.id, mutate)
            if updated:
                logger.warning("job %s marked %s after process disappeared", job.id, updated.status.value)
                audit_service.notify(
                    updated.owner_id,
                    f"任务 {updated.name} 异常结束",
                    body=updated.runtime.error,
                    level="error",
                    link=f"/jobs/{updated.id}",
                )

    def _refresh_metrics(self) -> None:
        for job in store.jobs.find(lambda j: j.status.is_active):
            refresh_metrics(job)

    # -------------------------------------------------------------- dispatch
    def busy_gpu_ids(self, *, include_deployments: bool = True) -> set[int]:
        busy: set[int] = set()
        for job in store.jobs.find(
            lambda j: j.status in {JobStatus.RUNNING, JobStatus.PREPARING}
        ):
            busy.update(job.resources.gpu_ids)
        if include_deployments:
            # 常驻的 vLLM 服务同样占着显存，不能把卡再分给训练。
            # 延迟导入：deployment_service 反过来要问 scheduler 要总卡数。
            from . import deployment_service

            busy.update(deployment_service.busy_gpu_ids())
        return busy

    def free_gpu_ids(self) -> list[int]:
        busy = self.busy_gpu_ids()
        return [index for index in range(self.total_gpus) if index not in busy]

    def running_count(self) -> int:
        return store.jobs.count(lambda j: j.status in {JobStatus.RUNNING, JobStatus.PREPARING})

    def dispatch(self) -> None:
        with _DISPATCH_LOCK:
            system = store.system_settings()
            max_concurrent = system.max_concurrent_jobs or settings.max_concurrent_jobs
            queued = store.jobs.find(lambda j: j.status == JobStatus.QUEUED)
            if not queued:
                return
            queued.sort(key=lambda j: (-j.resources.priority, j.queued_at or j.created_at))

            for job in queued:
                if self.running_count() >= max_concurrent:
                    return
                if not self._user_slot_available(job, system.max_concurrent_jobs):
                    continue
                free = self.free_gpu_ids()
                needed = job.resources.n_gpus
                if needed > self.total_gpus and self.total_gpus > 0:
                    self._fail(job, f"申请 {needed} 张 GPU，但本机只有 {self.total_gpus} 张")
                    continue
                if needed > len(free):
                    continue  # wait for a GPU to free up
                self._start(job, free[:needed])

    def _user_slot_available(self, job: Job, default_limit: int) -> bool:
        owner = store.users.get(job.owner_id)
        limit = (owner.max_concurrent_jobs if owner else None) or 0
        if limit <= 0:
            return True
        running = store.jobs.count(
            lambda j: j.owner_id == job.owner_id and j.status in {JobStatus.RUNNING, JobStatus.PREPARING}
        )
        del default_limit
        return running < limit

    def _start(self, job: Job, gpu_ids: list[int]) -> None:
        def claim(item: Job) -> None:
            item.status = JobStatus.PREPARING
            item.resources.gpu_ids = gpu_ids
            item.progress.message = "准备运行环境…"
            item.updated_at = utcnow()

        claimed = store.jobs.update(job.id, claim)
        if claimed is None:
            return
        try:
            runner.launch(claimed)
            logger.info("job %s launched on GPU %s", job.id, gpu_ids or "cpu")
        except BuildError as exc:
            self._fail(claimed, str(exc))
        except Exception as exc:  # pragma: no cover
            logger.exception("job %s failed to launch", job.id)
            self._fail(claimed, f"{type(exc).__name__}: {exc}")

    def _fail(self, job: Job, message: str) -> None:
        def mutate(item: Job) -> None:
            item.status = JobStatus.FAILED
            item.runtime.error = message
            item.runtime.finished_at = utcnow()
            item.resources.gpu_ids = []
            item.progress.message = "启动失败"
            item.updated_at = utcnow()

        updated = store.jobs.update(job.id, mutate)
        if updated:
            audit_service.notify(
                updated.owner_id,
                f"任务 {updated.name} 启动失败",
                body=message,
                level="error",
                link=f"/jobs/{updated.id}",
            )
            audit_service.record(
                "job.launch_failed",
                target_type="job",
                target_id=updated.id,
                summary=f"任务启动失败: {message}",
            )


scheduler = Scheduler()


# --------------------------------------------------------------------------- #
# Helpers usable from request handlers
# --------------------------------------------------------------------------- #
def refresh_metrics(job: Job) -> Job:
    parsed = metrics_parser.parse_job(job)
    if not parsed.series and parsed.progress.percent == 0 and parsed.progress.current_step == 0:
        if parsed.progress.total_trials == 0:
            return job
    work_dir = Path(job.runtime.work_dir or runner.work_dir_for(job))
    metrics_parser.write_series(work_dir, parsed)

    def mutate(item: Job) -> None:
        previous = item.progress
        item.metrics = parsed.summary
        item.progress = parsed.progress
        if not item.progress.message:
            item.progress.message = previous.message
        if item.status.is_terminal:
            # Adopt the fresher counters (steps / trials) but keep the terminal
            # verdict the supervisor wrote — a finished job stays at 100%.
            item.progress.message = previous.message
            item.progress.percent = 100.0 if item.status == JobStatus.SUCCEEDED else previous.percent
        item.updated_at = utcnow()

    return store.jobs.update(job.id, mutate) or job


def gpu_snapshot() -> dict:
    from . import deployment_service

    busy = scheduler.busy_gpu_ids()
    assignments: dict[int, str] = {}
    for job in store.jobs.find(lambda j: j.status in {JobStatus.RUNNING, JobStatus.PREPARING}):
        for gpu_id in job.resources.gpu_ids:
            assignments[gpu_id] = job.id
    for deployment in deployment_service.list_deployments(active_only=True):
        for gpu_id in deployment.gpu_ids:
            assignments[gpu_id] = deployment.id
    return {
        "total": scheduler.total_gpus,
        "busy": sorted(busy),
        "free": scheduler.free_gpu_ids(),
        "assignments": assignments,
    }
