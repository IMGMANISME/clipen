"""Whether the Mac runs on battery: on battery, closing the lid sleeps it even while the AI works."""

from __future__ import annotations

import re
import subprocess
import sys

_SOURCE = re.compile(r"Now drawing from '([^']+)'")
_PERCENT = re.compile(r"(\d{1,3})%")


def power_status() -> dict:
    """`{"on_battery": bool | None, "percent": int | None}`; None when it cannot be told."""
    if sys.platform != "darwin":
        return {"on_battery": None, "percent": None}
    try:
        output = subprocess.run(
            ["/usr/bin/pmset", "-g", "batt"], capture_output=True, text=True, timeout=3, check=False
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return {"on_battery": None, "percent": None}
    source = _SOURCE.search(output)
    percent = _PERCENT.search(output)
    return {
        "on_battery": None if source is None else source.group(1) == "Battery Power",
        "percent": int(percent.group(1)) if percent else None,
    }
