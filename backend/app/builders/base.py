"""Shared plumbing for turning a validated job config into a runnable command."""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class BuildResult:
    """Everything the runner needs to execute a job."""

    command: list[str]
    cwd: str
    env: dict[str, str] = field(default_factory=dict)
    # relative path inside the job work dir -> file content
    files: dict[str, str] = field(default_factory=dict)
    output_dir: str = ""
    notes: list[str] = field(default_factory=list)
    # Extra directories to create before launching.
    mkdirs: list[str] = field(default_factory=list)
    # Literal secret values (API keys …). Never rendered into run.sh, the stored
    # command line, or the UI — replaced by ``***``.
    secrets: list[str] = field(default_factory=list)

    @property
    def command_line(self) -> str:
        return " ".join(shlex.quote(part) for part in self.command)

    def masked_command_line(self) -> str:
        parts = []
        for part in self.command:
            for secret in self.secrets:
                if secret and secret in part:
                    part = part.replace(secret, "***")
            parts.append(shlex.quote(part))
        return " ".join(parts)

    def masked_env(self) -> dict[str, str]:
        return {
            key: ("***" if any(s and s == value for s in self.secrets) else value)
            for key, value in self.env.items()
        }


class BuildError(ValueError):
    """Raised when a job configuration cannot produce a valid command."""


def flag(name: str, value: object) -> list[str]:
    return [name, str(value)]


def kv_flags(name: str, mapping: dict[str, str]) -> list[str]:
    out: list[str] = []
    for key, value in mapping.items():
        if key:
            out += [name, f"{key}={value}"]
    return out


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def resolve_attn_implementation(requested: str, python_bin: str) -> tuple[str, str]:
    """把 RL 的 ``attn_implementation`` 落到具体取值，返回 ``(取值, 说明)``。

    ``auto`` 需要看目标环境里到底有没有 flash-attn：没装却发
    ``flash_attention_2``，transformers 会在加载模型时才抛
    ``FlashAttention2 has been toggled on, but it cannot be used``，
    白排队几分钟才失败。``""`` 表示不下发这个参数。
    """
    from ..services import resources_service

    if requested == "default":
        return "", ""
    if requested != "auto":
        if requested == "flash_attention_2" and not resources_service.has_flash_attn(python_bin):
            return requested, "⚠ 该环境里没有 flash-attn，flash_attention_2 会在加载模型时失败；建议改用 sdpa"
        return requested, f"attention 实现: {requested}"
    if resources_service.has_flash_attn(python_bin):
        return "flash_attention_2", "attention 实现: flash_attention_2（auto 探测到 flash-attn）"
    return "sdpa", "attention 实现: sdpa（auto 未探测到 flash-attn，已回落）"
