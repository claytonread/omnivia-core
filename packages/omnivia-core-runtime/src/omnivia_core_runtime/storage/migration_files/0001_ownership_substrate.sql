-- Generation-1 ownership and migration substrate (T-0629B).
--
-- The minimum set of reserved tables the normal fenced migration path needs
-- before it can run at all. A pristine or frozen Phase 0 database contains none
-- of them, so this cannot itself be applied by the fenced path without circular
-- authority; it is applied once, in a single atomic bootstrap transaction, under
-- the lifetime storage lock and the sole exclusive SQLite connection.
--
-- Every table is prefixed `omnivia_` and reserved. No product data lives here.

-- Authoritative workspace state, including the current fencing generation.
-- Exactly one row, pinned by a CHECK so a second row cannot be inserted: two
-- rows would mean two competing generations for one workspace.
CREATE TABLE IF NOT EXISTS omnivia_workspace_state (
    singleton          INTEGER PRIMARY KEY CHECK (singleton = 1),
    workspace_id       TEXT    NOT NULL,
    fencing_generation INTEGER NOT NULL CHECK (fencing_generation >= 1),
    workspace_format_version TEXT NOT NULL,
    baseline_state     TEXT    NOT NULL,
    created_at         TEXT    NOT NULL,
    updated_at         TEXT    NOT NULL
);

-- Ordered, checksum-pinned schema migrations. This ledger is authoritative;
-- PRAGMA user_version mirrors its highest version for diagnostics only.
CREATE TABLE IF NOT EXISTS omnivia_schema_migrations (
    version     INTEGER PRIMARY KEY,
    name        TEXT    NOT NULL,
    checksum    TEXT    NOT NULL,
    applied_at  TEXT    NOT NULL,
    applied_by_service_instance TEXT,
    fencing_generation INTEGER
);

-- Durable record of every migration attempt, including failures. Recorded before
-- the attempt so an interrupted migration is discoverable on restart rather than
-- inferred from a half-applied schema.
CREATE TABLE IF NOT EXISTS omnivia_migration_attempts (
    attempt_id  TEXT    PRIMARY KEY,
    version     INTEGER NOT NULL,
    outcome     TEXT    NOT NULL,
    started_at  TEXT    NOT NULL,
    finished_at TEXT,
    detail      TEXT
);

-- Workspace open events, for takeover diagnostics.
CREATE TABLE IF NOT EXISTS omnivia_workspace_open_events (
    event_id            TEXT    PRIMARY KEY,
    opened_at           TEXT    NOT NULL,
    open_mode           TEXT    NOT NULL,
    service_instance_id TEXT,
    fencing_generation  INTEGER
);

-- The current workspace service lease. Single row for the same reason as
-- workspace_state: one workspace has one authoritative owner.
CREATE TABLE IF NOT EXISTS omnivia_workspace_lease (
    singleton           INTEGER PRIMARY KEY CHECK (singleton = 1),
    workspace_id        TEXT    NOT NULL,
    service_instance_id TEXT    NOT NULL,
    installation_id     TEXT    NOT NULL,
    fencing_generation  INTEGER NOT NULL,
    os_principal        TEXT,
    process_pid         INTEGER,
    process_start       TEXT,
    boot_id             TEXT,
    endpoint            TEXT,
    lock_mechanism      TEXT,
    heartbeat_monotonic REAL,
    heartbeat_at        TEXT,
    acquired_at         TEXT    NOT NULL,
    lifecycle           TEXT    NOT NULL,
    takeover_predecessor TEXT,
    shutdown_state      TEXT
);

-- Projection ledger: which derived indexes exist and whether they are rebuildable.
CREATE TABLE IF NOT EXISTS omnivia_projection_ledger (
    projection_id TEXT    PRIMARY KEY,
    version       TEXT    NOT NULL,
    rebuildable   INTEGER NOT NULL CHECK (rebuildable IN (0, 1)),
    state         TEXT    NOT NULL,
    updated_at    TEXT    NOT NULL,
    fencing_generation INTEGER
);

-- Durable jobs, recovered before writable readiness is published.
CREATE TABLE IF NOT EXISTS omnivia_durable_jobs (
    job_id      TEXT    PRIMARY KEY,
    job_type    TEXT    NOT NULL,
    state       TEXT    NOT NULL,
    payload_json TEXT,
    created_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL,
    fencing_generation INTEGER,
    claimed_by_service_instance TEXT
);

CREATE INDEX IF NOT EXISTS omnivia_idx_durable_jobs_state
    ON omnivia_durable_jobs (state);
CREATE INDEX IF NOT EXISTS omnivia_idx_migration_attempts_version
    ON omnivia_migration_attempts (version, outcome);
CREATE INDEX IF NOT EXISTS omnivia_idx_open_events_opened_at
    ON omnivia_workspace_open_events (opened_at);
