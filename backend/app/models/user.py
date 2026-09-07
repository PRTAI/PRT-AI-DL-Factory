"""User accounts. Admin-provisioned; email is the login handle."""

from __future__ import annotations

from datetime import datetime

from pydantic import Field

from .common import Base, Role, new_id, utcnow
from .fields import EmailAddress


class User(Base):
    id: str = Field(default_factory=lambda: new_id("usr"))
    email: EmailAddress
    display_name: str
    role: Role = Role.USER
    password_hash: str
    active: bool = True
    must_change_password: bool = False
    created_at: datetime = Field(default_factory=utcnow)
    created_by: str | None = None
    updated_at: datetime = Field(default_factory=utcnow)
    last_login_at: datetime | None = None
    # Per-user guardrails; ``None`` means "inherit the system default".
    max_concurrent_jobs: int | None = None
    max_gpus_per_job: int | None = None
    note: str = ""

    @property
    def is_admin(self) -> bool:
        return self.role == Role.ADMIN


class UserPublic(Base):
    id: str
    email: EmailAddress
    display_name: str
    role: Role
    active: bool
    must_change_password: bool
    created_at: datetime
    last_login_at: datetime | None = None
    max_concurrent_jobs: int | None = None
    max_gpus_per_job: int | None = None
    note: str = ""

    @classmethod
    def of(cls, user: User) -> "UserPublic":
        return cls(**user.model_dump(exclude={"password_hash", "created_by", "updated_at"}))


class UserCreate(Base):
    email: EmailAddress
    display_name: str = ""
    password: str = Field(min_length=8, max_length=128)
    role: Role = Role.USER
    must_change_password: bool = True
    max_concurrent_jobs: int | None = None
    max_gpus_per_job: int | None = None
    note: str = ""


class UserUpdate(Base):
    display_name: str | None = None
    role: Role | None = None
    active: bool | None = None
    max_concurrent_jobs: int | None = None
    max_gpus_per_job: int | None = None
    note: str | None = None


class PasswordReset(Base):
    password: str = Field(min_length=8, max_length=128)
    must_change_password: bool = True


class PasswordChange(Base):
    current_password: str
    new_password: str = Field(min_length=8, max_length=128)


class LoginRequest(Base):
    email: EmailAddress
    password: str


class TokenResponse(Base):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user: UserPublic
