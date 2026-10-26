"""Queue policy: decide what runs, on which GPUs, and keep job state honest.

A single background loop (started from the FastAPI lifespan) does three things
every tick:

1. **Reconcile** — running jobs whose process disappeared (platform restart,
   OOM-killer) are settled by the exit code the wrapper left on disk, or marked
   failed when there is none, instead of hanging at "running" forever.
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
from ..models import Job, JobStatus, SystemSettings, utcnow
from ..store import store
from . import audit_service, metrics_parser, runner
from .resource_lock import RESOURCE_LOCK

logger = logging.getLogger("prt.scheduler")

_DISPATCH_LOCK = threading.RLock()
_PREPARING: set[str] = set()


def effective_system_settings() -> SystemSettings:
    """Resolve settings the way the settings page already shows them.

    ``SystemSettings()`` doubles as the API response model, so when no admin has
    ever saved ``settings.json`` the document still yields the model defaults
    (``max_concurrent_jobs=2``). The settings page deliberately falls back to
    ``PRT_REQUIRE_APPROVAL`` / ``PRT_MAX_CONCURRENT_JOBS`` in that case; the
    scheduler must read the same values, otherwise the UI can display 8000 while
    the queue still only dispatches two jobs at a time.
    """
    current = store.system_settings()
    if not current.updated_by:
        current.require_approval = settings.require_approval
        current.max_concurrent_jobs = settings.max_concurrent_jobs
    return current


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
        for job in store.jobs.find(lambda j: j.status in {JobStatus.RUNNING, JobStatus.PREPARING}):
            def mutate(item: Job) -> None:
                exit_code = runner.read_exit_code(item) if item.status == JobStatus.RUNNING else None
                if exit_code is not None:
                    # 平台重启期间跑完的任务：按包装脚本落盘的退出码如实结算
                    runner.apply_exit(item, exit_code)
                    item.resources.gpu_ids = []
                    return
                item.status = JobStatus.CANCELLED if item.runtime.cancel_requested else JobStatus.FAILED
                item.runtime.finished_at = utcnow()
                item.resources.gpu_ids = []
                if not item.runtime.cancel_requested:
                    item.runtime.error = (
                        item.runtime.error
                        or "进程已不存在（可能在平台重启期间退出），请查看日志确认。"
                    )
                    item.progress.message = "进程丢失"
                item.updated_at = utcnow()

            with runner.LIFECYCLE_LOCK:
                current = store.jobs.get(job.id)
                if current is None or current.status not in {JobStatus.RUNNING, JobStatus.PREPARING}:
                    continue
                if job.id in _PREPARING or runner.has_supervisor(job.id) or runner.is_running(current):
                    continue
                updated = store.jobs.update(job.id, mutate)
            if updated:
                logger.warning("job %s marked %s after process disappeared", job.id, updated.status.value)
                if updated.runtime.exit_code is not None:
                    runner.announce_exit(updated, updated.runtime.exit_code)
                    continue
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
            launches: list[Job] = []
            with RESOURCE_LOCK:
                system = effective_system_settings()
                max_concurrent = system.max_concurrent_jobs or settings.max_concurrent_jobs
                queued = store.jobs.find(lambda j: j.status == JobStatus.QUEUED)
                if not queued:
                    return
                queued.sort(key=lambda j: (-j.resources.priority, j.queued_at or j.created_at))

                for job in queued:
                    if self.running_count() >= max_concurrent:
                        break
                    if not self._user_slot_available(job, system.max_concurrent_jobs):
                        continue
                    free = self.free_gpu_ids()
                    needed = job.resources.n_gpus
                    if needed > self.total_gpus and self.total_gpus > 0:
                        self._fail(job, f"申请 {needed} 张 GPU，但本机只有 {self.total_gpus} 张")
                        continue
                    if needed > len(free):
                        continue  # wait for a GPU to free up
                    claimed = self._claim(job, free[:needed])
                    if claimed is not None:
                        launches.append(claimed)
            for claimed in launches:
                self._launch(claimed)

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

    def _claim(self, job: Job, gpu_ids: list[int]) -> Job | None:
        # Called while RESOURCE_LOCK is held, before any slow preparation.
        with runner.LIFECYCLE_LOCK:
            current = store.jobs.get(job.id)
            if current is None or current.status != JobStatus.QUEUED or current.runtime.cancel_requested:
                return None

            def claim(item: Job) -> None:
                item.status = JobStatus.PREPARING
                item.resources.gpu_ids = gpu_ids
                item.progress.message = "准备运行环境…"
                item.updated_at = utcnow()

            claimed = store.jobs.update(job.id, claim)
            if claimed is not None:
                _PREPARING.add(job.id)
            return claimed

    def _launch(self, claimed: Job) -> None:
        try:
            runner.launch(claimed)
            logger.info("job %s launch handled", claimed.id)
        except BuildError as exc:
            self._fail(claimed, str(exc))
        except Exception as exc:  # pragma: no cover
            logger.exception("job %s failed to launch", claimed.id)
            self._fail(claimed, f"{type(exc).__name__}: {exc}")
        finally:
            with runner.LIFECYCLE_LOCK:
                _PREPARING.discard(claimed.id)

    def _fail(self, job: Job, message: str) -> None:
        def mutate(item: Job) -> None:
            if item.runtime.cancel_requested or item.status.is_terminal:
                return
            item.status = JobStatus.FAILED
            item.runtime.error = message
            item.runtime.finished_at = utcnow()
            item.resources.gpu_ids = []
            item.progress.message = "启动失败"
            item.updated_at = utcnow()

        updated = store.jobs.update(job.id, mutate)
        if updated and updated.status == JobStatus.FAILED:
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
    from . import billing_service
    billing_service.worker.request([job])
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
