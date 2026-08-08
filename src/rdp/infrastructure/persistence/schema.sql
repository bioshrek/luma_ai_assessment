-- The catalog. One SQLite file; the stage machine lives here, outside the process, which is
-- what makes `kill -9` recoverable.
--
-- schema version 1 (M1). `episode_state`, leases and staleness detection arrive in M2.

CREATE TABLE IF NOT EXISTS sources (
    source_id     TEXT PRIMARY KEY,
    kind          TEXT NOT NULL,
    uri           TEXT NOT NULL,
    revision      TEXT NOT NULL,
    embodiment    TEXT NOT NULL,
    license       TEXT,
    notes         TEXT,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS episodes (
    episode_uid       TEXT PRIMARY KEY,
    source_id         TEXT NOT NULL,
    upstream_id       TEXT NOT NULL,
    -- Idempotency key is (source_id, upstream_id, content_hash): the first two identify the
    -- episode, the third decides whether what we hold is still what upstream holds.
    content_hash      TEXT,
    status            TEXT NOT NULL,
    embodiment        TEXT,
    task              TEXT,
    action_level      TEXT,
    action_space      TEXT,
    action_dim        INTEGER,
    physical_dim      INTEGER,
    state_space       TEXT,
    state_dim         INTEGER,
    n_frames          INTEGER,
    fps_nominal       REAL,
    fps_effective     REAL,
    duration_s        REAL,
    capabilities_json TEXT,
    action_spec_json  TEXT,
    state_spec_json   TEXT,
    camera_json       TEXT,
    provenance_json   TEXT,
    boundary_json     TEXT,
    raw_extra_json    TEXT,
    raw_columns_json  TEXT,
    stats_json        TEXT,
    frames_path       TEXT,
    qc_verdict        TEXT NOT NULL DEFAULT 'PENDING',
    last_error        TEXT,
    schema_version    TEXT,
    adapter_version   TEXT,
    first_seen_run    TEXT,
    last_update_run   TEXT,
    updated_at        TEXT,
    UNIQUE (source_id, upstream_id)
);

CREATE INDEX IF NOT EXISTS idx_episodes_status ON episodes (status);
CREATE INDEX IF NOT EXISTS idx_episodes_source ON episodes (source_id);
CREATE INDEX IF NOT EXISTS idx_episodes_verdict ON episodes (qc_verdict);

CREATE TABLE IF NOT EXISTS qc_results (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    episode_uid     TEXT NOT NULL,
    rule_id         TEXT NOT NULL,
    verdict         TEXT NOT NULL,
    metrics_json    TEXT NOT NULL,
    reason          TEXT,
    run_id          TEXT NOT NULL,
    ruleset_version TEXT,
    created_at      TEXT NOT NULL,
    -- Re-running QC within one run replaces the row rather than appending a duplicate.
    UNIQUE (episode_uid, rule_id, run_id)
);

CREATE INDEX IF NOT EXISTS idx_qc_episode ON qc_results (episode_uid);
CREATE INDEX IF NOT EXISTS idx_qc_run ON qc_results (run_id);

CREATE TABLE IF NOT EXISTS runs (
    run_id      TEXT PRIMARY KEY,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT NOT NULL,
    args_json   TEXT,
    stats_json  TEXT
);

CREATE TABLE IF NOT EXISTS exports (
    export_id     TEXT PRIMARY KEY,
    run_id        TEXT,
    strategy      TEXT NOT NULL,
    budget_frames INTEGER NOT NULL,
    n_episodes    INTEGER NOT NULL,
    n_frames      INTEGER NOT NULL,
    path          TEXT NOT NULL,
    created_at    TEXT NOT NULL
);
