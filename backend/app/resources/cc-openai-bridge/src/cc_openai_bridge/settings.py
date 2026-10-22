"""Deployment settings supplied only by the server operator."""

import os
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit


def valid_model(value):
    return (
        isinstance(value, str)
        and re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._:/-]{0,119}", value) is not None
        and "://" not in value
    )


@dataclass(frozen=True)
class Settings:
    upstream_base_url: str
    upstream_api_key: str = field(repr=False)
    bridge_api_key: str = field(repr=False)
    allowed_models: tuple[str, ...] = ("gpt-6-astra",)
    host: str = "127.0.0.1"
    port: int = 18876
    max_concurrency: int = 8
    upstream_timeout_seconds: int = 300
    max_request_bytes: int = 32 * 1024 * 1024

    def __post_init__(self):
        url = urlsplit(self.upstream_base_url)
        if (
            url.scheme not in {"http", "https"}
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            raise ValueError("UPSTREAM_BASE_URL must be an HTTP(S) API base without credentials")
        for name, value in (
            ("UPSTREAM_API_KEY", self.upstream_api_key),
            ("BRIDGE_API_KEY", self.bridge_api_key),
        ):
            if not value or value.startswith("replace-") or "\n" in value or "\r" in value:
                raise ValueError(name + " must be configured")
        if not self.allowed_models or any(not valid_model(m) for m in self.allowed_models):
            raise ValueError("BRIDGE_ALLOWED_MODELS requires valid, comma-separated model IDs")
        if not 1 <= self.port <= 65535 or not 1 <= self.max_concurrency <= 10000:
            raise ValueError("Invalid bridge port or concurrency")
        if self.upstream_timeout_seconds < 1 or self.max_request_bytes < 1:
            raise ValueError("Request limits must be positive")

    @classmethod
    def from_env(cls):
        return cls(
            upstream_base_url=os.environ.get("UPSTREAM_BASE_URL", "").rstrip("/"),
            upstream_api_key=os.environ.get("UPSTREAM_API_KEY", ""),
            bridge_api_key=os.environ.get("BRIDGE_API_KEY", ""),
            allowed_models=tuple(
                m.strip()
                for m in os.environ.get("BRIDGE_ALLOWED_MODELS", "gpt-6-astra").split(",")
                if m.strip()
            ),
            host=os.environ.get("BRIDGE_HOST", "127.0.0.1"),
            port=int(os.environ.get("BRIDGE_PORT", "18876")),
            max_concurrency=int(os.environ.get("BRIDGE_MAX_CONCURRENCY", "8")),
            upstream_timeout_seconds=int(os.environ.get("BRIDGE_UPSTREAM_TIMEOUT_SECONDS", "300")),
            max_request_bytes=int(
                os.environ.get("BRIDGE_MAX_REQUEST_BYTES", str(32 * 1024 * 1024))
            ),
        )
