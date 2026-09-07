#!/usr/bin/env python3
"""Offline admin CLI — useful when nobody can log in any more.

    python scripts/manage.py list-users
    python scripts/manage.py create-user ops@team.com --role admin
    python scripts/manage.py reset-password ops@team.com --password 'new-pass-1'
    python scripts/manage.py unlock-admin
    python scripts/manage.py gc --days 30 --dry-run

Run it with the same environment (PRT_DATA_ROOT …) as the server.
"""

from __future__ import annotations

import argparse
import secrets
import shutil
import sys
from datetime import timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from backend.app.config import settings  # noqa: E402
from backend.app.models import Role, UserCreate, utcnow  # noqa: E402
from backend.app.services import user_service  # noqa: E402
from backend.app.store import store  # noqa: E402


def _random_password() -> str:
    alphabet = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(16))


def cmd_list_users(_: argparse.Namespace) -> int:
    users = user_service.list_users()
    if not users:
        print("（没有账号）")
        return 0
    width = max(len(u.email) for u in users)
    print(f"{'EMAIL'.ljust(width)}  ROLE   ACTIVE  LAST LOGIN")
    for user in users:
        last = user.last_login_at.strftime("%Y-%m-%d %H:%M") if user.last_login_at else "-"
        print(f"{user.email.ljust(width)}  {user.role.value:5s}  {str(user.active):6s}  {last}")
    return 0


def cmd_create_user(args: argparse.Namespace) -> int:
    password = args.password or _random_password()
    user = user_service.create_user(
        UserCreate(
            email=args.email,
            display_name=args.name or "",
            password=password,
            role=Role(args.role),
            must_change_password=not args.no_force_change,
        ),
        actor=None,
    )
    print(f"✔ 已创建 {user.email} ({user.role.value})")
    print(f"  初始密码: {password}")
    return 0


def cmd_reset_password(args: argparse.Namespace) -> int:
    user = user_service.get_by_email(args.email)
    if user is None:
        print(f"✗ 找不到账号 {args.email}", file=sys.stderr)
        return 1
    password = args.password or _random_password()
    from backend.app.security import hash_password

    def mutate(target) -> None:
        target.password_hash = hash_password(password)
        target.must_change_password = not args.no_force_change
        target.active = True
        target.updated_at = utcnow()

    store.users.update(user.id, mutate)
    print(f"✔ 已重置 {user.email} 的密码")
    print(f"  新密码: {password}")
    return 0


def cmd_unlock_admin(args: argparse.Namespace) -> int:
    """Re-enable (or create) an administrator when everyone is locked out."""
    admins = [u for u in store.users.all() if u.role == Role.ADMIN]
    if not admins:
        return cmd_create_user(
            argparse.Namespace(
                email=args.email or settings.bootstrap_admin_email,
                name="平台管理员",
                password=None,
                role="admin",
                no_force_change=False,
            )
        )
    target = next((u for u in admins if u.email == args.email), admins[0])
    return cmd_reset_password(
        argparse.Namespace(email=target.email, password=None, no_force_change=False)
    )


def cmd_gc(args: argparse.Namespace) -> int:
    """Delete work dirs of finished jobs older than N days (outputs are kept)."""
    cutoff = utcnow() - timedelta(days=args.days)
    removed = 0
    freed = 0
    for job in store.jobs.all():
        if not job.status.is_terminal:
            continue
        finished = job.runtime.finished_at or job.updated_at
        if finished > cutoff:
            continue
        work_dir = Path(job.runtime.work_dir or (settings.jobs_dir / job.id))
        if not work_dir.is_dir():
            continue
        size = sum(p.stat().st_size for p in work_dir.rglob("*") if p.is_file())
        print(f"{'[dry-run] ' if args.dry_run else ''}删除 {work_dir} ({size / 1e6:.1f} MB)")
        if not args.dry_run:
            shutil.rmtree(work_dir, ignore_errors=True)
        removed += 1
        freed += size
    print(f"共 {removed} 个任务工作目录，{freed / 1e9:.2f} GB")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list-users", help="列出所有账号").set_defaults(func=cmd_list_users)

    create = sub.add_parser("create-user", help="创建账号")
    create.add_argument("email")
    create.add_argument("--name", default="")
    create.add_argument("--password", default=None, help="留空则随机生成")
    create.add_argument("--role", choices=["admin", "user"], default="user")
    create.add_argument("--no-force-change", action="store_true", help="不要求首次登录改密")
    create.set_defaults(func=cmd_create_user)

    reset = sub.add_parser("reset-password", help="重置某个账号的密码")
    reset.add_argument("email")
    reset.add_argument("--password", default=None)
    reset.add_argument("--no-force-change", action="store_true")
    reset.set_defaults(func=cmd_reset_password)

    unlock = sub.add_parser("unlock-admin", help="管理员全部失联时恢复访问")
    unlock.add_argument("--email", default=None)
    unlock.set_defaults(func=cmd_unlock_admin)

    gc = sub.add_parser("gc", help="清理已结束任务的工作目录（保留产物）")
    gc.add_argument("--days", type=int, default=30)
    gc.add_argument("--dry-run", action="store_true")
    gc.set_defaults(func=cmd_gc)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
