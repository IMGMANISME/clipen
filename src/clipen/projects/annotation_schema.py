"""SQLite schema for user-authored material and transcript annotations."""

ANNOTATION_SCHEMA = """
CREATE TABLE material_annotations (
    version_id TEXT PRIMARY KEY REFERENCES asset_versions(version_id),
    revision INTEGER NOT NULL CHECK (revision >= 1),
    people_json TEXT NOT NULL,
    location TEXT,
    trip_day INTEGER CHECK (trip_day IS NULL OR trip_day BETWEEN 1 AND 365),
    day_part TEXT CHECK (
        day_part IS NULL OR day_part IN
        ('dawn', 'morning', 'noon', 'afternoon', 'evening', 'night', 'late_night')
    ),
    note TEXT NOT NULL,
    usage TEXT NOT NULL CHECK (
        usage IN ('normal', 'must_use', 'exclude', 'ambient_only')
    ),
    updated_at TEXT NOT NULL
);
CREATE TABLE transcript_annotations (
    version_id TEXT NOT NULL REFERENCES asset_versions(version_id),
    stream_index INTEGER NOT NULL CHECK (stream_index >= 0),
    source_revision_id TEXT NOT NULL REFERENCES transcription_revisions(revision_id),
    revision INTEGER NOT NULL CHECK (revision >= 1),
    confirmed INTEGER NOT NULL CHECK (confirmed IN (0, 1)),
    discarded INTEGER NOT NULL CHECK (discarded IN (0, 1)),
    corrections_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(version_id, stream_index)
);
CREATE TABLE annotation_command_receipts (
    request_id TEXT PRIMARY KEY,
    command_type TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX material_annotations_updated
    ON material_annotations(updated_at DESC);
"""
