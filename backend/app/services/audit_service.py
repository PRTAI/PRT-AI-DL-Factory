"""Audit trail + in-app notification helpers."""

from __future__ import annotations

from typing import Any

from ..models import AuditEvent, Notification, Role, User
from ..store import store

MAX_NOTIFICATIONS = 2000


def record(
    action: str,
    *,
    actor: User | None = None,
    target_type: str = "",
    target_id: str = "",
    summary: str = "",
    ip: str = "",
    detail: dict[str, Any] | None = None,
) -> AuditEvent:
    event = AuditEvent(
        actor_id=actor.id if actor else "system",
        actor_email=actor.email if actor else "system",
        action=action,
        target_type=target_type,
        target_id=target_id,
        summary=summary,
        ip=ip,
        detail=detail or {},
    )
    store.audit_log(event)
    return event


def list_events(
    limit: int = 200,
    *,
    actor_id: str | None = None,
    action_prefix: str | None = None,
    target_id: str | None = None,
) -> list[dict[str, Any]]:
    def keep(row: dict[str, Any]) -> bool:
        if actor_id and row.get("actor_id") != actor_id:
            return False
        if action_prefix and not str(row.get("action", "")).startswith(action_prefix):
            return False
        if target_id and row.get("target_id") != target_id:
            return False
        return True

    return store.audit.tail(limit=limit, predicate=keep)


def notify(
    user_id: str,
    title: str,
    *,
    body: str = "",
    level: str = "info",
    link: str = "",
) -> Notification:
    notification = Notification(user_id=user_id, title=title, body=body, level=level, link=link)
    store.notifications.put(notification)
    _trim()
    return notification


def notify_admins(title: str, *, body: str = "", level: str = "info", link: str = "") -> None:
    for admin in store.users.find(lambda u: u.role == Role.ADMIN and u.active):
        notify(admin.id, title, body=body, level=level, link=link)


def list_for_user(user_id: str, *, unread_only: bool = False, limit: int = 100) -> list[Notification]:
    items = store.notifications.find(lambda n: n.user_id == user_id)
    if unread_only:
        items = [n for n in items if not n.read]
    items.sort(key=lambda n: n.at, reverse=True)
    return items[:limit]


def mark_read(user_id: str, notification_ids: list[str] | None = None) -> int:
    targets = store.notifications.find(
        lambda n: n.user_id == user_id
        and not n.read
        and (notification_ids is None or n.id in notification_ids)
    )
    for item in targets:
        item.read = True
    if targets:
        store.notifications.put_many(targets)
    return len(targets)


def _trim() -> None:
    items = store.notifications.all()
    if len(items) <= MAX_NOTIFICATIONS:
        return
    items.sort(key=lambda n: n.at, reverse=True)
    store.notifications.replace_all(items[:MAX_NOTIFICATIONS])
