"""Small validation primitives shared by independent domain modules."""

from __future__ import annotations

import re

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")


def require_identifier(value: object, field: str) -> None:
    """Require a bounded identifier that is safe in logs and JSON payloads."""

    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"{field} must be a non-empty bounded identifier")
