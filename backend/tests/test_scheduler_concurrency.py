"""Regression: dispatch must honour the concurrency limit the settings page shows.

An unsaved ``settings.json`` still yields ``SystemSettings`` field defaults
(``max_concurrent_jobs=2``). The settings page falls back to
``PRT_MAX_CONCURRENT_JOBS`` for exactly that case, so dispatch has to resolve
the same value — otherwise a deployment configured with 8000 in ``.env`` shows
8000 in the UI while the queue only starts two jobs at a time.
"""

from backend.app.config import settings
from backend.app.models import Framework, Job, JobStatus, JobType, SystemSettings, VerifyConfig
from backend.app.models.job import ResourceSpec
from backend.app.services import scheduler as scheduler_module


class _FakeJobs:
    """Isolated job table so queued fixtures from other tests cannot leak in."""

    def __init__(self, jobs: list[Job]) -> None:
        self.items = jobs

    def find(self, predicate):
        return [job for job in self.items if predicate(job)]

    def count(self, predicate=None):
        if predicate is None:
            return len(self.items)
        return sum(1 for job in self.items if predicate(job))

    def get(self, job_id: str):
        return next((job for job in self.items if job.id == job_id), None)

    def update(self, job_id: str, mutate):
        for index, job in enumerate(self.items):
            if job.id == job_id:
                updated = job.model_copy(deep=True)
                mutate(updated)
                self.items[index] = updated
                return updated
        return None


def _queued(count: int) -> list[Job]:
    return [
        Job(
            name=f"评测 {index}",
            type=JobType.VERIFY,
            framework=Framework.HARBOR,
            status=JobStatus.QUEUED,
            owner_id="usr_never",
            resources=ResourceSpec(n_gpus=0),
            verify=VerifyConfig(tasks_path="/tmp/tasks"),
        )
        for index in range(count)
    ]


def _dispatch(monkeypatch, jobs: list[Job], *, saved: SystemSettings | None = None) -> list[str]:
    # Stop the background loop from dispatching behind our back.
    monkeypatch.setattr(scheduler_module.Scheduler, "tick", lambda self: None)
    monkeypatch.setattr(scheduler_module.store, "jobs", _FakeJobs(jobs))
    monkeypatch.setattr(
        scheduler_module.store, "system_settings", lambda: saved or SystemSettings()
    )
    launches: list[str] = []
    monkeypatch.setattr(scheduler_module.runner, "launch", lambda job: launches.append(job.id))
    scheduler_module.scheduler.dispatch()
    return launches


def test_env_max_concurrent_jobs_is_used_when_settings_were_never_saved(monkeypatch):
    monkeypatch.setattr(settings, "max_concurrent_jobs", 8000)
    assert len(_dispatch(monkeypatch, _queued(3))) == 3


def test_admin_saved_max_concurrent_jobs_overrides_env(monkeypatch):
    monkeypatch.setattr(settings, "max_concurrent_jobs", 8000)
    saved = SystemSettings(max_concurrent_jobs=1, updated_by="admin@prt.local")
    assert len(_dispatch(monkeypatch, _queued(3), saved=saved)) == 1


def test_effective_settings_fall_back_to_env(monkeypatch):
    monkeypatch.setattr(settings, "max_concurrent_jobs", 7)
    monkeypatch.setattr(settings, "require_approval", False)
    monkeypatch.setattr(scheduler_module.store, "system_settings", lambda: SystemSettings())
    resolved = scheduler_module.effective_system_settings()
    assert resolved.max_concurrent_jobs == 7
    assert resolved.require_approval is False


def test_effective_settings_keep_admin_saved_values(monkeypatch):
    monkeypatch.setattr(settings, "max_concurrent_jobs", 8000)
    saved = SystemSettings(max_concurrent_jobs=3, require_approval=True, updated_by="admin@prt.local")
    monkeypatch.setattr(
        scheduler_module.store, "system_settings", lambda: saved.model_copy(deep=True)
    )
    resolved = scheduler_module.effective_system_settings()
    assert resolved.max_concurrent_jobs == 3
    assert resolved.require_approval is True
