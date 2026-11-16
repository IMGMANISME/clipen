"""Stable encodings shared by Clipen's music and production records."""

from __future__ import annotations

import hashlib
import json


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()
