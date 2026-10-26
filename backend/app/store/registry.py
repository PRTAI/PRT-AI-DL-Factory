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
    Issue,
    Job,
    ModelApiConfig,
    ModelPlatformConfig,
    ModelDownload,
    Notification,
    Project,
    ReviewTask,
    SystemSettings,
    User,
    UserApiKey,
)
from .json_store import JsonCollection, JsonDocument, JsonlLog
from ..models.billing import BillingArchive, PricingSettings, UsageCharge


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
        self.reviews: JsonCollection[ReviewTask] = JsonCollection(db / "reviews.json", ReviewTask)
        self.issues: JsonCollection[Issue] = JsonCollection(db / "issues.json", Issue)
        # 里面有明文 API Key：文件是 0600（见 json_store 的写入），接口层永远只返回尾号
        self.model_apis: JsonCollection[ModelApiConfig] = JsonCollection(
            db / "model_apis.json", ModelApiConfig, mode=0o600
        )
        self.model_platforms: JsonCollection[ModelPlatformConfig] = JsonCollection(
            db / "model_platforms.json", ModelPlatformConfig, mode=0o600
        )
        # 每个账号自己的模型 API Key，同样是明文 + 0600
        self.user_api_keys: JsonCollection[UserApiKey] = JsonCollection(
            db / "user_api_keys.json", UserApiKey, mode=0o600
        )
        self.settings_doc: JsonDocument[SystemSettings] = JsonDocument(
            db / "settings.json", SystemSettings
        )
        self.pricing_doc = JsonDocument(db / "pricing.json", PricingSettings)
        self.usage_charges = JsonCollection(db / "usage_charges.json", UsageCharge)
        self.billing_archives = JsonCollection(db / "billing_archives.json", BillingArchive)
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
