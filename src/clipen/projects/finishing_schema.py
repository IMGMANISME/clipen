"""Structured finishing instructions and recoverable local operations."""

FINISHING_SCHEMA = """
CREATE TABLE finishing_library (
    role TEXT PRIMARY KEY CHECK(role IN ('music','sfx')),
    directory_json TEXT NOT NULL,
    assets_json TEXT,
    issues_json TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE finishing_runs (
    run_id TEXT PRIMARY KEY,
    request_id TEXT UNIQUE NOT NULL,
    request_hash TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('inspect','plan','render','produce')),
    state TEXT NOT NULL CHECK(state IN ('queued','running','cancelling','succeeded','failed','cancelled','interrupted')),
    phase TEXT NOT NULL,
    progress REAL NOT NULL DEFAULT 0 CHECK(progress BETWEEN 0 AND 1),
    context_json TEXT NOT NULL,
    result_json TEXT,
    usage_json TEXT,
    error_code TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT
);
CREATE TABLE finishing_draft (
    slot INTEGER PRIMARY KEY CHECK(slot=1),
    revision INTEGER NOT NULL,
    context_json TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE INDEX finishing_runs_created ON finishing_runs(created_at DESC);
"""

FINISHING_RUNS_SCHEMA = FINISHING_SCHEMA[
    FINISHING_SCHEMA.index("CREATE TABLE finishing_runs") : FINISHING_SCHEMA.index("CREATE TABLE finishing_draft")
]
