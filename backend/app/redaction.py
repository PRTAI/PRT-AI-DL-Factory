"""Non-mutating redaction shared by public responses and job exports."""

import re
from typing import Any

SECRET_KEY = re.compile(
    r"api[-_]?key|password|passwd|secret|credential|authorization|"
    r"(?:^|[-_])(?:access|auth|bearer|refresh)?[-_]?token$|(?:^|[-_])key$", re.I,
)


def secret_values(value: Any) -> set[str]:
    secrets: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            if SECRET_KEY.search(key) and isinstance(child, str) and child:
                secrets.add(child)
            else:
                secrets.update(secret_values(child))
    elif isinstance(value, list):
        for child in value:
            secrets.update(secret_values(child))
    return secrets


def mask_values(value: Any, secrets: list[str]) -> Any:
    if isinstance(value, dict):
        return {key: mask_values(child, secrets) for key, child in value.items()}
    if isinstance(value, list):
        return [mask_values(child, secrets) for child in value]
    if isinstance(value, str):
        for secret in secrets:
            value = value.replace(secret, "***")
    return value


def public_payload(payload: dict[str, Any]) -> dict[str, Any]:
    # Short placeholders must not corrupt unrelated names, IDs or timestamps.
    secrets = sorted((s for s in secret_values(payload) if len(s) >= 4), key=len, reverse=True)

    def redact(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: "***" if SECRET_KEY.search(key) and isinstance(child, str) and child
                    else redact(child) for key, child in value.items()}
        if isinstance(value, list):
            return [redact(child) for child in value]
        return mask_values(value, secrets)

    return redact(payload)


def has_secret_placeholders(value: Any) -> bool:
    if isinstance(value, dict):
        return any((bool(SECRET_KEY.search(key)) and child == "***") or has_secret_placeholders(child)
                   for key, child in value.items())
    if isinstance(value, list):
        return any(has_secret_placeholders(child) for child in value)
    return False


def restore_secret_placeholders(value: Any, original: Any) -> Any:
    """Only call after checking control permission on the source job."""
    if isinstance(value, dict):
        source = original if isinstance(original, dict) else {}
        result = {}
        for key, child in value.items():
            if SECRET_KEY.search(key) and child == "***":
                if not isinstance(source.get(key), str) or not source[key]:
                    raise ValueError("原任务中没有对应密钥，请重新填写")
                result[key] = source[key]
            else:
                result[key] = restore_secret_placeholders(child, source.get(key))
        return result
    if isinstance(value, list):
        source = original if isinstance(original, list) else []
        return [restore_secret_placeholders(child, source[index] if index < len(source) else None)
                for index, child in enumerate(value)]
    return value
