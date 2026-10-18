-- Engineering repository identity (SPEC-CORE-ENGMEM-001, plan PR-C; spec §6).
--
-- Additive only; this is the file the reservation 0047 was held for. The
-- families:
--
--   omnivia_engineering_repositories  one stable logical repository identity per
--                                     workspace. Display names are labels:
--                                     two unrelated repositories may share a
--                                     basename, so nothing here unique-ifies a
--                                     name, and label-only resolution is
--                                     ambiguous by construction (§6.2).
--   omnivia_engineering_checkouts     installation-local materialisations of
--                                     one logical repository. Rows are
--                                     installation state: they are never
--                                     exported as authority, and moving a
--                                     checkout re-points the mapping under
--                                     audit without changing the logical id.
--   omnivia_engineering_snapshots     one immutable captured source state per
--                                     repository: kind, manifest digest, base
--                                     commit when one exists, and an explicit
--                                     capture status. A dirty working-tree
--                                     snapshot is never asserted to be its
--                                     base commit, and a snapshot row is
--                                     append-only once written (§6.3).
--
-- No capture producer exists yet: rows are written through the storage module
-- by whoever records a capture, and every continuity reference to a repository
-- or snapshot is validated against these tables fail-closed.

CREATE TABLE IF NOT EXISTS omnivia_engineering_repositories (
    workspace_id   TEXT    NOT NULL,
    repository_id  TEXT    NOT NULL,
    display_name   TEXT    NOT NULL,
    provider_hint  TEXT,
    registered_at_us INTEGER NOT NULL,
    audit_ref      TEXT    NOT NULL,

    PRIMARY KEY (workspace_id, repository_id),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(repository_id) = 'text' AND length(repository_id) BETWEEN 1 AND 128
           AND repository_id GLOB '[A-Za-z0-9]*'
           AND repository_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(repository_id, char(0)) = 0),
    CHECK (typeof(display_name) = 'text' AND length(display_name) BETWEEN 1 AND 256
           AND instr(display_name, char(0)) = 0),
    CHECK (provider_hint IS NULL OR (typeof(provider_hint) = 'text'
           AND length(provider_hint) BETWEEN 1 AND 256)),
    CHECK (typeof(registered_at_us) = 'integer' AND registered_at_us > 0),
    CHECK (typeof(audit_ref) = 'text' AND length(audit_ref) BETWEEN 1 AND 128
           AND audit_ref GLOB '[A-Za-z0-9]*'
           AND audit_ref NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(audit_ref, char(0)) = 0),

    FOREIGN KEY (audit_ref, workspace_id)
        REFERENCES omnivia_application_audit_events (audit_ref, workspace_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_engineering_checkouts (
    workspace_id     TEXT    NOT NULL,
    checkout_id      TEXT    NOT NULL,
    repository_id    TEXT    NOT NULL,
    installation_id  TEXT    NOT NULL,
    checkout_hint    TEXT    NOT NULL,
    registered_at_us INTEGER NOT NULL,
    last_seen_at_us  INTEGER NOT NULL,
    audit_ref        TEXT    NOT NULL,

    PRIMARY KEY (workspace_id, checkout_id),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(checkout_id) = 'text' AND length(checkout_id) BETWEEN 1 AND 128
           AND checkout_id GLOB '[A-Za-z0-9]*'
           AND checkout_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(checkout_id, char(0)) = 0),
    CHECK (typeof(repository_id) = 'text' AND length(repository_id) BETWEEN 1 AND 128
           AND repository_id GLOB '[A-Za-z0-9]*'
           AND repository_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(repository_id, char(0)) = 0),
    CHECK (typeof(installation_id) = 'text'
           AND length(installation_id) BETWEEN 1 AND 128),
    CHECK (typeof(checkout_hint) = 'text' AND length(checkout_hint) BETWEEN 1 AND 512
           AND instr(checkout_hint, char(0)) = 0),
    CHECK (typeof(registered_at_us) = 'integer' AND registered_at_us > 0),
    CHECK (typeof(last_seen_at_us) = 'integer' AND last_seen_at_us > 0),
    CHECK (typeof(audit_ref) = 'text' AND length(audit_ref) BETWEEN 1 AND 128
           AND audit_ref GLOB '[A-Za-z0-9]*'
           AND audit_ref NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(audit_ref, char(0)) = 0),

    UNIQUE (workspace_id, installation_id, checkout_hint),

    FOREIGN KEY (workspace_id, repository_id)
        REFERENCES omnivia_engineering_repositories (workspace_id, repository_id),
    FOREIGN KEY (audit_ref, workspace_id)
        REFERENCES omnivia_application_audit_events (audit_ref, workspace_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_engineering_snapshots (
    workspace_id     TEXT    NOT NULL,
    snapshot_id      TEXT    NOT NULL,
    repository_id    TEXT    NOT NULL,
    snapshot_kind    TEXT    NOT NULL,
    manifest_digest  TEXT    NOT NULL,
    base_commit      TEXT,
    capture_status   TEXT    NOT NULL,
    captured_at_us   INTEGER NOT NULL,
    audit_ref        TEXT    NOT NULL,

    PRIMARY KEY (workspace_id, snapshot_id),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(snapshot_id) = 'text' AND length(snapshot_id) BETWEEN 1 AND 128
           AND snapshot_id GLOB '[A-Za-z0-9]*'
           AND snapshot_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(snapshot_id, char(0)) = 0),
    CHECK (typeof(repository_id) = 'text' AND length(repository_id) BETWEEN 1 AND 128
           AND repository_id GLOB '[A-Za-z0-9]*'
           AND repository_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(repository_id, char(0)) = 0),
    CHECK (snapshot_kind IN ('git_commit', 'working_tree', 'source_archive')),
    CHECK (typeof(manifest_digest) = 'text' AND length(manifest_digest) = 71
           AND substr(manifest_digest, 1, 7) = 'sha256:'
           AND substr(manifest_digest, 8) NOT GLOB '*[^0-9a-f]*'),
    CHECK (base_commit IS NULL OR (typeof(base_commit) = 'text'
           AND length(base_commit) BETWEEN 1 AND 128
           AND instr(base_commit, char(0)) = 0)),
    CHECK (capture_status IN ('complete', 'incomplete', 'pending')),
    CHECK (typeof(captured_at_us) = 'integer' AND captured_at_us > 0),
    CHECK (typeof(audit_ref) = 'text' AND length(audit_ref) BETWEEN 1 AND 128
           AND audit_ref GLOB '[A-Za-z0-9]*'
           AND audit_ref NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(audit_ref, char(0)) = 0),
    CHECK ((snapshot_kind = 'git_commit') = (base_commit IS NOT NULL)),

    FOREIGN KEY (workspace_id, repository_id)
        REFERENCES omnivia_engineering_repositories (workspace_id, repository_id),
    FOREIGN KEY (audit_ref, workspace_id)
        REFERENCES omnivia_application_audit_events (audit_ref, workspace_id)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS omnivia_idx_engineering_repositories_name
    ON omnivia_engineering_repositories (workspace_id, display_name);
CREATE INDEX IF NOT EXISTS omnivia_idx_engineering_checkouts_repo
    ON omnivia_engineering_checkouts (workspace_id, repository_id, installation_id);
CREATE INDEX IF NOT EXISTS omnivia_idx_engineering_snapshots_repo
    ON omnivia_engineering_snapshots (workspace_id, repository_id, captured_at_us);

CREATE TRIGGER IF NOT EXISTS omnivia_guard_omnivia_engineering_repositories_insert
BEFORE INSERT ON omnivia_engineering_repositories
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_engineering_repositories')
    WHERE omnivia_service_writer() IS NOT 1
       OR NOT EXISTS (
            SELECT 1 FROM omnivia_mutation_guard g
            JOIN omnivia_workspace_state s ON s.singleton = 1
            JOIN omnivia_workspace_lease l ON l.singleton = 1
            WHERE g.singleton = 1 AND g.fencing_generation = s.fencing_generation
              AND g.workspace_id = s.workspace_id
              AND l.fencing_generation = g.fencing_generation
              AND l.workspace_id = g.workspace_id
              AND l.service_instance_id = g.service_instance_id
              AND l.lifecycle IN ('acquiring', 'held', 'draining'))
       OR NEW.workspace_id IS NOT (
            SELECT workspace_id FROM omnivia_workspace_state WHERE singleton = 1);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_omnivia_engineering_repositories_update
BEFORE UPDATE ON omnivia_engineering_repositories
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_engineering_repositories is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_omnivia_engineering_repositories_delete
BEFORE DELETE ON omnivia_engineering_repositories
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_engineering_repositories is append-only; DELETE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_omnivia_engineering_checkouts_insert
BEFORE INSERT ON omnivia_engineering_checkouts
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_engineering_checkouts')
    WHERE omnivia_service_writer() IS NOT 1
       OR NOT EXISTS (
            SELECT 1 FROM omnivia_mutation_guard g
            JOIN omnivia_workspace_state s ON s.singleton = 1
            JOIN omnivia_workspace_lease l ON l.singleton = 1
            WHERE g.singleton = 1 AND g.fencing_generation = s.fencing_generation
              AND g.workspace_id = s.workspace_id
              AND l.fencing_generation = g.fencing_generation
              AND l.workspace_id = g.workspace_id
              AND l.service_instance_id = g.service_instance_id
              AND l.lifecycle IN ('acquiring', 'held', 'draining'))
       OR NEW.workspace_id IS NOT (
            SELECT workspace_id FROM omnivia_workspace_state WHERE singleton = 1);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_omnivia_engineering_checkouts_update
BEFORE UPDATE ON omnivia_engineering_checkouts
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded UPDATE on omnivia_engineering_checkouts')
    WHERE omnivia_service_writer() IS NOT 1
       OR NOT EXISTS (
            SELECT 1 FROM omnivia_mutation_guard g
            JOIN omnivia_workspace_state s ON s.singleton = 1
            JOIN omnivia_workspace_lease l ON l.singleton = 1
            WHERE g.singleton = 1 AND g.fencing_generation = s.fencing_generation
              AND g.workspace_id = s.workspace_id
              AND l.fencing_generation = g.fencing_generation
              AND l.workspace_id = g.workspace_id
              AND l.service_instance_id = g.service_instance_id
              AND l.lifecycle IN ('acquiring', 'held', 'draining'))
       OR NEW.workspace_id IS NOT (
            SELECT workspace_id FROM omnivia_workspace_state WHERE singleton = 1);
    SELECT RAISE(ABORT, 'omnivia: engineering checkout identity is immutable; only the repository mapping and last-seen time may move, audited')
    WHERE NEW.checkout_id IS NOT OLD.checkout_id
       OR NEW.installation_id IS NOT OLD.installation_id
       OR NEW.checkout_hint IS NOT OLD.checkout_hint
       OR NEW.registered_at_us IS NOT OLD.registered_at_us
       OR NEW.last_seen_at_us < OLD.last_seen_at_us;
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_omnivia_engineering_checkouts_delete
BEFORE DELETE ON omnivia_engineering_checkouts
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_engineering_checkouts is append-only; DELETE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_omnivia_engineering_snapshots_insert
BEFORE INSERT ON omnivia_engineering_snapshots
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_engineering_snapshots')
    WHERE omnivia_service_writer() IS NOT 1
       OR NOT EXISTS (
            SELECT 1 FROM omnivia_mutation_guard g
            JOIN omnivia_workspace_state s ON s.singleton = 1
            JOIN omnivia_workspace_lease l ON l.singleton = 1
            WHERE g.singleton = 1 AND g.fencing_generation = s.fencing_generation
              AND g.workspace_id = s.workspace_id
              AND l.fencing_generation = g.fencing_generation
              AND l.workspace_id = g.workspace_id
              AND l.service_instance_id = g.service_instance_id
              AND l.lifecycle IN ('acquiring', 'held', 'draining'))
       OR NEW.workspace_id IS NOT (
            SELECT workspace_id FROM omnivia_workspace_state WHERE singleton = 1);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_omnivia_engineering_snapshots_update
BEFORE UPDATE ON omnivia_engineering_snapshots
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_engineering_snapshots is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_omnivia_engineering_snapshots_delete
BEFORE DELETE ON omnivia_engineering_snapshots
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_engineering_snapshots is append-only; DELETE is never permitted');
END;
