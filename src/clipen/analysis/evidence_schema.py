"""Persistent evidence checkpoints and explicit observation reservations."""
EVIDENCE_SCHEMA = """
CREATE TABLE evidence_runs (
    run_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL UNIQUE,
    scope_id TEXT NOT NULL,
    inventory_run_id TEXT NOT NULL,
    project_revision INTEGER NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('running','succeeded','failed','cancelled','interrupted')),
    scope_json TEXT NOT NULL,
    total_count INTEGER NOT NULL,
    progress_json TEXT NOT NULL DEFAULT '{}',
    error_code TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE evidence_results (
    run_id TEXT NOT NULL REFERENCES evidence_runs(run_id),
    ordinal INTEGER NOT NULL,
    item_key TEXT NOT NULL,
    result_json TEXT NOT NULL,
    has_issue INTEGER NOT NULL,
    PRIMARY KEY(run_id, ordinal),
    UNIQUE(run_id, item_key)
);
CREATE TABLE evidence_images (
    run_id TEXT NOT NULL REFERENCES evidence_runs(run_id),
    artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id),
    PRIMARY KEY(run_id, artifact_id)
);
CREATE TABLE evidence_cache (
    item_key TEXT NOT NULL,
    recipe TEXT NOT NULL,
    ffmpeg TEXT NOT NULL,
    result_json TEXT NOT NULL,
    PRIMARY KEY(item_key, recipe, ffmpeg)
);
"""

OBSERVATION_SCHEMA = """
CREATE TABLE evidence_observations (
    observation_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES evidence_runs(run_id),
    item_key TEXT NOT NULL,
    request_id TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL CHECK (state IN ('running','succeeded','failed','cancelled','interrupted')),
    result_json TEXT,
    usage_json TEXT,
    error_code TEXT,
    UNIQUE(run_id, item_key)
);
"""
