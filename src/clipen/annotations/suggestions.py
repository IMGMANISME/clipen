"""Conservative local suggestions derived from capture metadata."""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime

_FILENAME_TIME = re.compile(r"(?<!\d)(20\d{12})(?!\d)")


def annotation_suggestion(database: sqlite3.Connection, version_id: str) -> dict[str, object]:
    return annotation_suggestions(database).get(version_id, _empty())


def annotation_suggestions(
    database: sqlite3.Connection,
) -> dict[str, dict[str, object]]:
    rows = database.execute(
        "SELECT versions.version_id,versions.probe_json,assets.relative_path "
        "FROM current_inventory AS current "
        "JOIN inventory_items AS items ON items.run_id=current.run_id "
        "JOIN assets ON assets.asset_id=items.asset_id "
        "JOIN asset_versions AS versions ON versions.version_id=items.version_id "
        "WHERE current.slot=1 AND assets.role='source' AND items.kind='video' "
        "AND items.status='usable' ORDER BY assets.relative_path",
    ).fetchall()
    captures: dict[str, datetime] = {}
    locations: dict[str, str] = {}
    sources: dict[str, str] = {}
    for row in rows:
        probe = _probe(row["probe_json"])
        captured, source = _capture_time(row["relative_path"], probe)
        if captured is not None:
            captures[row["version_id"]] = captured
            sources[row["version_id"]] = source
        location = probe.get("location")
        if isinstance(location, str) and location.strip() and len(location) <= 160:
            locations[row["version_id"]] = location.strip()

    day_numbers = {
        capture_date: index
        for index, capture_date in enumerate(sorted({value.date() for value in captures.values()}), start=1)
    }
    result: dict[str, dict[str, object]] = {}
    for row in rows:
        version_id = row["version_id"]
        target = captures.get(version_id)
        suggestion = _empty()
        suggestion["location"] = locations.get(version_id)
        if target is not None:
            suggestion["trip_day"] = day_numbers[target.date()]
            suggestion["day_part"] = _day_part(target.hour)
            suggestion["capture_time"] = target.isoformat(timespec="seconds")
            suggestion["time_source"] = sources[version_id]
        result[version_id] = suggestion
    return result


def _empty() -> dict[str, object]:
    return {
        "trip_day": None,
        "day_part": None,
        "location": None,
        "capture_time": None,
        "time_source": None,
    }


def _probe(raw: object) -> dict[str, object]:
    if not isinstance(raw, str):
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _capture_time(relative_path: str, probe: dict[str, object]) -> tuple[datetime | None, str]:
    name = relative_path.rsplit("/", 1)[-1]
    match = _FILENAME_TIME.search(name)
    if match is not None:
        try:
            return datetime.strptime(match.group(1), "%Y%m%d%H%M%S"), "filename"
        except ValueError:
            pass
    raw = probe.get("capture_time")
    if not isinstance(raw, str):
        return None, ""
    try:
        captured = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None, ""
    # Camera metadata represents the wall-clock time at the recording location.
    # Converting it to the workstation timezone can move travel footage into a
    # different day or day part, so preserve the recorded calendar values.
    return captured.replace(tzinfo=None), "metadata"


def _day_part(hour: int) -> str:
    if 4 <= hour < 6:
        return "dawn"
    if 6 <= hour < 12:
        return "morning"
    if 12 <= hour < 14:
        return "noon"
    if 14 <= hour < 18:
        return "afternoon"
    if 18 <= hour < 20:
        return "evening"
    if 20 <= hour:
        return "night"
    return "late_night"
