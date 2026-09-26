-- Engineering source coverage (SPEC-CORE-ENGMEM-001, plan P0-04; spec §6.3, §15).
--
-- Additive only; allocation 0050 (Engineering Memory, predecessor 0049). The
-- families:
--
--   omnivia_engineering_source_streams     one trusted source stream: the principal
--                                          that owns it, the logical repository it
--                                          is bound to, the newest announced
--                                          sequence and the contiguous covered
--                                          sequence (the coverage barrier). Identity
--                                          never changes; both sequences only
--                                          advance, and every covered sequence is
--                                          backed by a present event.
--   omnivia_engineering_source_events      one immutable source event per (stream,
--                                          sequence): the snapshot it recorded, its
--                                          predecessor link and the canonical
--                                          manifest body whose digest the 0047
--                                          snapshot row carries. Append-only. A
--                                          stored neighbour must agree with the
--                                          predecessor link, so the stored chain is
--                                          always consistent and coverage never
--                                          crosses a gap or a broken link.
--   omnivia_engineering_dependency_sets    the dependency coverage metadata of one
--                                          exact record version: the baseline
--                                          source (repository, stream, snapshot),
--                                          the producer and its version, and
--                                          whether the producer declares its
--                                          dependency list complete. Append-only;
--                                          the individual dependencies stay in
--                                          0049's dependency table, which gains the
--                                          expected whole-file digest here, and the
--                                          set row seals them: no dependency row is
--                                          added for a version once its set exists.
--
-- Coverage follows the producer's sequence and predecessor chain, never capture
-- time. Nothing here writes governed records, governance state or assessments:
-- applicability is evaluated from these rows at read time and never persisted.

CREATE TABLE IF NOT EXISTS omnivia_engineering_source_streams (
    workspace_id       TEXT    NOT NULL,
    stream_id          TEXT    NOT NULL,
    repository_id      TEXT    NOT NULL,
    principal_id       TEXT    NOT NULL,
    announced_sequence INTEGER NOT NULL,
    covered_sequence   INTEGER NOT NULL,
    registered_at_us   INTEGER NOT NULL,
    updated_at_us      INTEGER NOT NULL,
    audit_ref          TEXT    NOT NULL,

    PRIMARY KEY (workspace_id, stream_id),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(stream_id) = 'text' AND length(stream_id) BETWEEN 1 AND 128
           AND stream_id GLOB '[A-Za-z0-9]*'
           AND stream_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(stream_id, char(0)) = 0),
    CHECK (typeof(repository_id) = 'text' AND length(repository_id) BETWEEN 1 AND 128
           AND repository_id GLOB '[A-Za-z0-9]*'
           AND repository_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(repository_id, char(0)) = 0),
    CHECK (typeof(principal_id) = 'text'
           AND length(principal_id) BETWEEN 1 AND 128),
    CHECK (typeof(announced_sequence) = 'integer'
           AND announced_sequence BETWEEN 1 AND 2147483647),
    CHECK (typeof(covered_sequence) = 'integer' AND covered_sequence >= 0
           AND covered_sequence <= announced_sequence),
    CHECK (typeof(registered_at_us) = 'integer' AND registered_at_us > 0),
    CHECK (typeof(updated_at_us) = 'integer' AND updated_at_us >= registered_at_us),
    CHECK (typeof(audit_ref) = 'text' AND length(audit_ref) BETWEEN 1 AND 128
           AND audit_ref GLOB '[A-Za-z0-9]*'
           AND audit_ref NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(audit_ref, char(0)) = 0),

    FOREIGN KEY (workspace_id, repository_id)
        REFERENCES omnivia_engineering_repositories (workspace_id, repository_id),
    FOREIGN KEY (audit_ref, workspace_id)
        REFERENCES omnivia_application_audit_events (audit_ref, workspace_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_engineering_source_events (
    workspace_id            TEXT    NOT NULL,
    stream_id               TEXT    NOT NULL,
    sequence                INTEGER NOT NULL,
    snapshot_id             TEXT    NOT NULL,
    predecessor_sequence    INTEGER,
    predecessor_snapshot_id TEXT,
    manifest_json           TEXT    NOT NULL,
    manifest_digest         TEXT    NOT NULL,
    manifest_entry_count    INTEGER NOT NULL,
    event_digest            TEXT    NOT NULL,
    recorded_at_us          INTEGER NOT NULL,
    audit_ref               TEXT    NOT NULL,

    PRIMARY KEY (workspace_id, stream_id, sequence),
    UNIQUE (workspace_id, snapshot_id),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(stream_id) = 'text' AND length(stream_id) BETWEEN 1 AND 128
           AND stream_id GLOB '[A-Za-z0-9]*'
           AND stream_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(stream_id, char(0)) = 0),
    CHECK (typeof(sequence) = 'integer' AND sequence BETWEEN 1 AND 2147483647),
    CHECK (typeof(snapshot_id) = 'text' AND length(snapshot_id) BETWEEN 1 AND 128
           AND snapshot_id GLOB '[A-Za-z0-9]*'
           AND snapshot_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(snapshot_id, char(0)) = 0),
    CHECK ((sequence = 1) = (predecessor_sequence IS NULL)),
    CHECK ((predecessor_sequence IS NULL) = (predecessor_snapshot_id IS NULL)),
    CHECK (predecessor_sequence IS NULL
           OR (typeof(predecessor_sequence) = 'integer'
               AND predecessor_sequence = sequence - 1)),
    CHECK (predecessor_snapshot_id IS NULL
           OR (typeof(predecessor_snapshot_id) = 'text'
               AND length(predecessor_snapshot_id) BETWEEN 1 AND 128
               AND predecessor_snapshot_id GLOB '[A-Za-z0-9]*'
               AND predecessor_snapshot_id NOT GLOB '*[^A-Za-z0-9._:-]*'
               AND instr(predecessor_snapshot_id, char(0)) = 0)),
    CHECK (typeof(manifest_json) = 'text'
           AND length(CAST(manifest_json AS BLOB)) BETWEEN 2 AND 65536
           AND json_valid(manifest_json) = 1
           AND json_type(manifest_json) = 'object'),
    CHECK (typeof(manifest_digest) = 'text' AND length(manifest_digest) = 71
           AND substr(manifest_digest, 1, 7) = 'sha256:'
           AND substr(manifest_digest, 8) NOT GLOB '*[^0-9a-f]*'),
    CHECK (typeof(manifest_entry_count) = 'integer'
           AND manifest_entry_count BETWEEN 0 AND 256),
    CHECK (typeof(event_digest) = 'text' AND length(event_digest) = 71
           AND substr(event_digest, 1, 7) = 'sha256:'
           AND substr(event_digest, 8) NOT GLOB '*[^0-9a-f]*'),
    CHECK (typeof(recorded_at_us) = 'integer' AND recorded_at_us > 0),
    CHECK (typeof(audit_ref) = 'text' AND length(audit_ref) BETWEEN 1 AND 128
           AND audit_ref GLOB '[A-Za-z0-9]*'
           AND audit_ref NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(audit_ref, char(0)) = 0),

    FOREIGN KEY (workspace_id, stream_id)
        REFERENCES omnivia_engineering_source_streams (workspace_id, stream_id),
    FOREIGN KEY (workspace_id, snapshot_id)
        REFERENCES omnivia_engineering_snapshots (workspace_id, snapshot_id),
    FOREIGN KEY (audit_ref, workspace_id)
        REFERENCES omnivia_application_audit_events (audit_ref, workspace_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_engineering_dependency_sets (
    workspace_id     TEXT    NOT NULL,
    record_id        TEXT    NOT NULL,
    version          TEXT    NOT NULL,
    repository_id    TEXT    NOT NULL,
    stream_id        TEXT    NOT NULL,
    snapshot_id      TEXT    NOT NULL,
    producer         TEXT    NOT NULL,
    producer_version TEXT    NOT NULL,
    coverage         TEXT    NOT NULL,
    dependency_count INTEGER NOT NULL,
    recorded_at_us   INTEGER NOT NULL,
    audit_ref        TEXT    NOT NULL,

    PRIMARY KEY (workspace_id, record_id, version),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(record_id) = 'text' AND length(record_id) BETWEEN 1 AND 128
           AND record_id GLOB '[A-Za-z0-9]*'
           AND record_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(record_id, char(0)) = 0),
    CHECK (typeof(version) = 'text' AND length(version) BETWEEN 1 AND 128
           AND version GLOB '[A-Za-z0-9]*'
           AND version NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(version, char(0)) = 0),
    CHECK (typeof(repository_id) = 'text' AND length(repository_id) BETWEEN 1 AND 128
           AND repository_id GLOB '[A-Za-z0-9]*'
           AND repository_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(repository_id, char(0)) = 0),
    CHECK (typeof(stream_id) = 'text' AND length(stream_id) BETWEEN 1 AND 128
           AND stream_id GLOB '[A-Za-z0-9]*'
           AND stream_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(stream_id, char(0)) = 0),
    CHECK (typeof(snapshot_id) = 'text' AND length(snapshot_id) BETWEEN 1 AND 128
           AND snapshot_id GLOB '[A-Za-z0-9]*'
           AND snapshot_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(snapshot_id, char(0)) = 0),
    CHECK (typeof(producer) = 'text' AND length(producer) BETWEEN 1 AND 128
           AND instr(producer, char(0)) = 0),
    CHECK (typeof(producer_version) = 'text' AND length(producer_version) BETWEEN 1 AND 64
           AND instr(producer_version, char(0)) = 0),
    CHECK (coverage IN ('complete', 'partial')),
    CHECK (typeof(dependency_count) = 'integer' AND dependency_count BETWEEN 0 AND 64),
    CHECK (typeof(recorded_at_us) = 'integer' AND recorded_at_us > 0),
    CHECK (typeof(audit_ref) = 'text' AND length(audit_ref) BETWEEN 1 AND 128
           AND audit_ref GLOB '[A-Za-z0-9]*'
           AND audit_ref NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(audit_ref, char(0)) = 0),

    FOREIGN KEY (workspace_id, snapshot_id)
        REFERENCES omnivia_engineering_snapshots (workspace_id, snapshot_id),
    FOREIGN KEY (audit_ref, workspace_id)
        REFERENCES omnivia_application_audit_events (audit_ref, workspace_id)
) WITHOUT ROWID;

ALTER TABLE omnivia_engineering_dependencies
ADD COLUMN expected_digest TEXT
CHECK (expected_digest IS NULL
       OR (typeof(expected_digest) = 'text' AND length(expected_digest) = 71
           AND substr(expected_digest, 1, 7) = 'sha256:'
           AND substr(expected_digest, 8) NOT GLOB '*[^0-9a-f]*'));

CREATE INDEX IF NOT EXISTS omnivia_idx_engineering_dependencies_version
    ON omnivia_engineering_dependencies (workspace_id, record_id, version);

CREATE TRIGGER IF NOT EXISTS omnivia_guard_omnivia_engineering_source_streams_insert
BEFORE INSERT ON omnivia_engineering_source_streams
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_engineering_source_streams')
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
    SELECT RAISE(ABORT, 'omnivia: a source stream starts uncovered and is owned by the principal whose audited source record opens it')
    WHERE NEW.covered_sequence IS NOT 0
       OR NOT EXISTS (
            SELECT 1 FROM omnivia_application_audit_events a
            WHERE a.audit_ref = NEW.audit_ref AND a.workspace_id = NEW.workspace_id
              AND a.principal_id = NEW.principal_id
              AND a.operation = 'engineering.source.record');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_omnivia_engineering_source_streams_update
BEFORE UPDATE ON omnivia_engineering_source_streams
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded UPDATE on omnivia_engineering_source_streams')
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
    SELECT RAISE(ABORT, 'omnivia: a source stream binding is immutable; only its head and coverage advance')
    WHERE NEW.workspace_id IS NOT OLD.workspace_id
       OR NEW.stream_id IS NOT OLD.stream_id
       OR NEW.repository_id IS NOT OLD.repository_id
       OR NEW.principal_id IS NOT OLD.principal_id
       OR NEW.registered_at_us IS NOT OLD.registered_at_us
       OR NEW.announced_sequence < OLD.announced_sequence
       OR NEW.covered_sequence < OLD.covered_sequence
       OR NEW.updated_at_us < OLD.updated_at_us;
    -- The old barrier was validated when it was written and never decreases, so
    -- only the newly covered range (OLD, NEW] is counted: one primary-key range
    -- scan of at most one pending window, however long the stream's history.
    SELECT RAISE(ABORT, 'omnivia: the source coverage barrier may not cross a missing event or advance past one pending window')
    WHERE NEW.covered_sequence > OLD.covered_sequence
      AND (NEW.covered_sequence - OLD.covered_sequence > 64
           OR (SELECT COUNT(*) FROM omnivia_engineering_source_events e
               WHERE e.workspace_id = NEW.workspace_id AND e.stream_id = NEW.stream_id
                 AND e.sequence > OLD.covered_sequence
                 AND e.sequence <= NEW.covered_sequence)
              IS NOT NEW.covered_sequence - OLD.covered_sequence);
    SELECT RAISE(ABORT, 'omnivia: only the stream owner''s audited source record may advance it')
    WHERE NOT EXISTS (
            SELECT 1 FROM omnivia_application_audit_events a
            WHERE a.audit_ref = NEW.audit_ref AND a.workspace_id = NEW.workspace_id
              AND a.principal_id = NEW.principal_id
              AND a.operation = 'engineering.source.record');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_omnivia_engineering_source_streams_delete
BEFORE DELETE ON omnivia_engineering_source_streams
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_engineering_source_streams is never deleted; source history is retained');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_omnivia_engineering_source_events_insert
BEFORE INSERT ON omnivia_engineering_source_events
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_engineering_source_events')
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
    SELECT RAISE(ABORT, 'omnivia: a source event must be announced by its stream owner''s audited source record')
    WHERE NOT EXISTS (
            SELECT 1 FROM omnivia_engineering_source_streams st
            JOIN omnivia_application_audit_events a
              ON a.audit_ref = NEW.audit_ref AND a.workspace_id = NEW.workspace_id
            WHERE st.workspace_id = NEW.workspace_id AND st.stream_id = NEW.stream_id
              AND st.announced_sequence >= NEW.sequence
              AND a.principal_id = st.principal_id
              AND a.operation = 'engineering.source.record');
    SELECT RAISE(ABORT, 'omnivia: a source event records a snapshot of its own stream''s repository with the same manifest digest')
    WHERE NOT EXISTS (
            SELECT 1 FROM omnivia_engineering_snapshots sn
            JOIN omnivia_engineering_source_streams st
              ON st.workspace_id = sn.workspace_id AND st.repository_id = sn.repository_id
            WHERE sn.workspace_id = NEW.workspace_id AND sn.snapshot_id = NEW.snapshot_id
              AND st.stream_id = NEW.stream_id
              AND sn.manifest_digest = NEW.manifest_digest);
    SELECT RAISE(ABORT, 'omnivia: a source event manifest entry count must match its body')
    WHERE (SELECT COUNT(*) FROM json_each(NEW.manifest_json)) IS NOT NEW.manifest_entry_count;
    SELECT RAISE(ABORT, 'omnivia: a source event must agree with the stored neighbours of its predecessor chain')
    WHERE EXISTS (
            SELECT 1 FROM omnivia_engineering_source_events p
            WHERE p.workspace_id = NEW.workspace_id AND p.stream_id = NEW.stream_id
              AND p.sequence = NEW.sequence - 1
              AND p.snapshot_id IS NOT NEW.predecessor_snapshot_id)
       OR EXISTS (
            SELECT 1 FROM omnivia_engineering_source_events n
            WHERE n.workspace_id = NEW.workspace_id AND n.stream_id = NEW.stream_id
              AND n.sequence = NEW.sequence + 1
              AND n.predecessor_snapshot_id IS NOT NEW.snapshot_id);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_omnivia_engineering_source_events_update
BEFORE UPDATE ON omnivia_engineering_source_events
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_engineering_source_events is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_omnivia_engineering_source_events_delete
BEFORE DELETE ON omnivia_engineering_source_events
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_engineering_source_events is append-only; DELETE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_omnivia_engineering_dependency_sets_insert
BEFORE INSERT ON omnivia_engineering_dependency_sets
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_engineering_dependency_sets')
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
    SELECT RAISE(ABORT, 'omnivia: a dependency set belongs to an exact record version proposed by its own audited memory.create')
    WHERE NOT EXISTS (
            SELECT 1 FROM omnivia_governed_version_assemblies v
            WHERE v.workspace_id = NEW.workspace_id
              AND v.governed_record_id = NEW.record_id
              AND v.governed_record_version_id = NEW.version
              AND v.audit_ref = NEW.audit_ref)
       OR NOT EXISTS (
            SELECT 1 FROM omnivia_application_audit_events a
            WHERE a.audit_ref = NEW.audit_ref AND a.workspace_id = NEW.workspace_id
              AND a.operation = 'memory.create');
    SELECT RAISE(ABORT, 'omnivia: a dependency set baseline must be a recorded source event of its stated repository and stream')
    WHERE NOT EXISTS (
            SELECT 1 FROM omnivia_engineering_source_events e
            JOIN omnivia_engineering_source_streams st
              ON st.workspace_id = e.workspace_id AND st.stream_id = e.stream_id
            WHERE e.workspace_id = NEW.workspace_id AND e.snapshot_id = NEW.snapshot_id
              AND e.stream_id = NEW.stream_id AND st.repository_id = NEW.repository_id);
    SELECT RAISE(ABORT, 'omnivia: a dependency set seals exactly its recorded dependencies, each whole-file one with its expected digest')
    WHERE (SELECT COUNT(*) FROM omnivia_engineering_dependencies d
           WHERE d.workspace_id = NEW.workspace_id AND d.record_id = NEW.record_id
             AND d.version = NEW.version) IS NOT NEW.dependency_count
       OR EXISTS (
            SELECT 1 FROM omnivia_engineering_dependencies d
            WHERE d.workspace_id = NEW.workspace_id AND d.record_id = NEW.record_id
              AND d.version = NEW.version
              AND (d.audit_ref IS NOT NEW.audit_ref
                   OR (d.selector_type = 'whole_file' AND d.expected_digest IS NULL)));
END;

-- 0049 lets dependency rows be inserted at any time; once a version's set row
-- exists its dependencies are sealed, so a later row can never widen the set.
CREATE TRIGGER IF NOT EXISTS omnivia_guard_omnivia_engineering_dependencies_sealed
BEFORE INSERT ON omnivia_engineering_dependencies
BEGIN
    SELECT RAISE(ABORT, 'omnivia: a sealed dependency set never gains a dependency')
    WHERE EXISTS (
            SELECT 1 FROM omnivia_engineering_dependency_sets s
            WHERE s.workspace_id = NEW.workspace_id AND s.record_id = NEW.record_id
              AND s.version = NEW.version);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_omnivia_engineering_dependency_sets_update
BEFORE UPDATE ON omnivia_engineering_dependency_sets
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_engineering_dependency_sets is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_omnivia_engineering_dependency_sets_delete
BEFORE DELETE ON omnivia_engineering_dependency_sets
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_engineering_dependency_sets is append-only; DELETE is never permitted');
END;
