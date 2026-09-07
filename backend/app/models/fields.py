"""Custom scalar types.

``EmailAddress`` deliberately does *not* use ``pydantic.EmailStr``: that rejects
reserved/internal TLDs such as ``@prt.local`` or ``@corp.internal``, which is
exactly what an on-prem team platform uses. We validate the shape and normalise
the case, and leave deliverability to the admin who creates the account.
"""

from __future__ import annotations

import re
from typing import Annotated

from pydantic import AfterValidator

_EMAIL_RE = re.compile(
    r"^[A-Za-z0-9!#$%&'*+/=?^_`{|}~.-]+@[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?"
    r"(\.[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?)+$"
)


def _validate_email(value: str) -> str:
    value = value.strip().lower()
    if len(value) > 254 or not _EMAIL_RE.match(value):
        raise ValueError("邮箱格式不正确")
    local, _, domain = value.partition("@")
    if not local or local.startswith(".") or local.endswith(".") or ".." in local:
        raise ValueError("邮箱格式不正确")
    if len(domain) > 253:
        raise ValueError("邮箱格式不正确")
    return value


EmailAddress = Annotated[str, AfterValidator(_validate_email)]
