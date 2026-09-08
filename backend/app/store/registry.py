"""Single place where every collection is wired up.

Point these at a SQL-backed implementation later and nothing else changes.
"""

from __future__ import annotations

from functools import lru_cache

from ..config import settings
from ..models import (
    AuditEvent,
    Dataset,
    Deployment,
    ExportTask,
    Job,
    ModelDownload,
    Notification,
    Project,
    SystemSettings,
    User,
)
from .json_store import JsonCollection, JsonDocument, JsonlLog


class Store:
    def __init__(self) -> None:
        db = settings.db_dir
        self.users: JsonCollection[User] = JsonCollection(db / "users.json", User)
        self.jobs: JsonCollection[Job] = JsonCollection(db / "jobs.json", Job)
        self.datasets: JsonCollection[Dataset] = JsonCollection(db / "datasets.json", Dataset)
        self.downloads: JsonCollection[ModelDownload] = JsonCollection(
            db / "downloads.json", ModelDownload
        )
        self.notifications: JsonCollection[Notification] = JsonCollection(
            db / "notifications.json", Notification
        )
        self.projects: JsonCollection[Project] = JsonCollection(db / "projects.json", Project)
        self.deployments: JsonCollection[Deployment] = JsonCollection(db / "deployments.json", Deployment)
        self.exports: JsonCollection[ExportTask] = JsonCollection(db / "exports.json", ExportTask)
        self.settings_doc: JsonDocument[SystemSettings] = JsonDocument(
            db / "settings.json", SystemSettings
        )
        self.audit = JsonlLog(settings.audit_dir / "audit.jsonl")

    # Convenience wrappers used all over the services layer.
    def audit_log(self, event: AuditEvent) -> None:
        self.audit.append(event)

    def system_settings(self) -> SystemSettings:
        current = self.settings_doc.read()
        return current


@lru_cache(maxsize=1)
def get_store() -> Store:
    return Store()


store = get_store()
