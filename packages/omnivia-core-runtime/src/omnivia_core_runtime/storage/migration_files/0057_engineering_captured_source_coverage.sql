-- Engineering captured source coverage: Stage 1 (SPEC-CORE-ENGMEM-001, plan
-- P0-04; spec §6.3, §15).
--
-- Additive only; allocation 0056 (Engineering Memory, predecessor 0055). This
-- migration prepares Core to represent a captured working-tree snapshot as a
-- source-coverage event without flattening the rich manifest into 0050's
-- 256-entry inline body. It adds:
--
--   omnivia_engineering_source_events.manifest_format
--       a closed representation marker (`flat_v1`, the migration invariant
--       default, or `captured_v1`). Every pre-0056 row and every unchanged
--       legacy insert stays `flat_v1`. A `captured_v1` event keeps its
--       inline columns as a backward-compatible sentinel -- `manifest_json`
--       is the canonical empty object, `manifest_entry_count` is 0 -- and
--       carries the rich working-tree manifest digest in `manifest_digest`
--       instead. No reader may treat that sentinel as an empty repository;
--       every reader here and in the runtime storage module branches on
--       `manifest_format` first.
--   omnivia_engineering_snapshot_files
--       the indexed path-to-digest projection of one captured snapshot,
--       keyed for the bounded per-path lookup applicability needs -- never
--       the full rich manifest, which stays canonical evidence elsewhere.
--       Append-only, and refused once its snapshot's header below exists.
--   omnivia_engineering_snapshot_captures
--       the sealed capture header: the checkout and evidence a captured
--       snapshot came from, its file count and status, and the two digests
--       Stage 2's producer will compute (SQLite cannot compute SHA-256).
--       Its successful insert seals the file index; insert order is
--       enforced by the files table's own guard. Append-only.
--   omnivia_engineering_source_stream_origins
--       the immutable (repository, installation, checkout) a stream is
--       bound to once a captured event is first committed to it. Bound
--       only to a stream that has recorded no event yet, so a captured
--       stream can never switch worktrees or installations, and a
--       pre-0056 rich snapshot -- which has no stored checkout origin --
--       stays permanently ineligible for `captured_v1`, never backfilled.
--
-- SQLite cannot alter an existing trigger body, so 0050's three source
-- guards that must recognise the new branch are replaced under their own
-- names, every prior predicate intact:
--
--   - `..._source_streams_insert` and `..._source_streams_update` now
--     accept either `engineering.source.record` or
--     `engineering.source.capture.commit` as the stream owner's audited
--     operation, because commit both advances the stream and appends its
--     captured event in one transaction;
--   - `..._source_events_insert` requires each event's authorizing audit
--     operation to match its own `manifest_format`; refuses a `flat_v1`
--     event on a stream already bound to a captured origin; and requires a
--     `captured_v1` event to carry the exact empty-manifest sentinel and to
--     be backed by one joined proof across its own stream, that stream's
--     origin and a sealed capture header, agreeing on repository,
--     installation and checkout, so an unbound or legacy flat stream -- and
--     a header sealed for a different checkout or installation -- can never
--     back one.
--
-- 0050's delete guards are untouched: history is never deleted, captured or
-- flat. Every 0050 predicate this migration does not mention -- ownership,
-- the mutation fence, the pending-window and predecessor-chain checks, the
-- snapshot repository/digest agreement -- is preserved byte for byte.

ALTER TABLE omnivia_engineering_source_events
ADD COLUMN manifest_format TEXT NOT NULL DEFAULT 'flat_v1'
CHECK (manifest_format IN ('flat_v1', 'captured_v1'));

CREATE TABLE IF NOT EXISTS omnivia_engineering_snapshot_files (
    workspace_id     TEXT    NOT NULL,
    snapshot_id      TEXT    NOT NULL,
    path             TEXT    NOT NULL,
    content_digest   TEXT    NOT NULL,
    audit_ref        TEXT    NOT NULL,

    PRIMARY KEY (workspace_id, snapshot_id, path),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(snapshot_id) = 'text' AND length(snapshot_id) BETWEEN 1 AND 128
           AND snapshot_id GLOB '[A-Za-z0-9]*'
           AND snapshot_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(snapshot_id, char(0)) = 0),
    CHECK (typeof(path) = 'text' AND length(path) BETWEEN 1 AND 512
           AND instr(path, char(0)) = 0),
    CHECK (typeof(content_digest) = 'text' AND length(content_digest) = 71
           AND substr(content_digest, 1, 7) = 'sha256:'
           AND substr(content_digest, 8) NOT GLOB '*[^0-9a-f]*'),
    CHECK (typeof(audit_ref) = 'text' AND length(audit_ref) BETWEEN 1 AND 128
           AND audit_ref GLOB '[A-Za-z0-9]*'
           AND audit_ref NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(audit_ref, char(0)) = 0),

    FOREIGN KEY (workspace_id, snapshot_id)
        REFERENCES omnivia_engineering_snapshots (workspace_id, snapshot_id),
    FOREIGN KEY (audit_ref, workspace_id)
        REFERENCES omnivia_application_audit_events (audit_ref, workspace_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_engineering_snapshot_captures (
    workspace_id          TEXT    NOT NULL,
    snapshot_id           TEXT    NOT NULL,
    repository_id         TEXT    NOT NULL,
    installation_id       TEXT    NOT NULL,
    checkout_id           TEXT    NOT NULL,
    manifest_evidence_id  TEXT    NOT NULL,
    rich_manifest_digest  TEXT    NOT NULL,
    coverage_digest       TEXT    NOT NULL,
    file_count            INTEGER NOT NULL,
    capture_status        TEXT    NOT NULL,
    captured_at_us        INTEGER NOT NULL,
    audit_ref             TEXT    NOT NULL,

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
    CHECK (typeof(installation_id) = 'text'
           AND length(installation_id) BETWEEN 1 AND 128),
    CHECK (typeof(checkout_id) = 'text' AND length(checkout_id) BETWEEN 1 AND 128
           AND checkout_id GLOB '[A-Za-z0-9]*'
           AND checkout_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(checkout_id, char(0)) = 0),
    CHECK (typeof(manifest_evidence_id) = 'text'
           AND length(manifest_evidence_id) BETWEEN 1 AND 128
           AND manifest_evidence_id GLOB '[A-Za-z0-9]*'
           AND manifest_evidence_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(manifest_evidence_id, char(0)) = 0),
    CHECK (typeof(rich_manifest_digest) = 'text' AND length(rich_manifest_digest) = 71
           AND substr(rich_manifest_digest, 1, 7) = 'sha256:'
           AND substr(rich_manifest_digest, 8) NOT GLOB '*[^0-9a-f]*'),
    CHECK (typeof(coverage_digest) = 'text' AND length(coverage_digest) = 71
           AND substr(coverage_digest, 1, 7) = 'sha256:'
           AND substr(coverage_digest, 8) NOT GLOB '*[^0-9a-f]*'),
    CHECK (typeof(file_count) = 'integer' AND file_count BETWEEN 0 AND 10000),
    CHECK (capture_status IN ('complete', 'incomplete')),
    CHECK (typeof(captured_at_us) = 'integer' AND captured_at_us > 0),
    CHECK (typeof(audit_ref) = 'text' AND length(audit_ref) BETWEEN 1 AND 128
           AND audit_ref GLOB '[A-Za-z0-9]*'
           AND audit_ref NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(audit_ref, char(0)) = 0),

    FOREIGN KEY (workspace_id, snapshot_id)
        REFERENCES omnivia_engineering_snapshots (workspace_id, snapshot_id),
    FOREIGN KEY (workspace_id, checkout_id)
        REFERENCES omnivia_engineering_checkouts (workspace_id, checkout_id),
    FOREIGN KEY (manifest_evidence_id)
        REFERENCES omnivia_evidence_artifacts (evidence_id),
    FOREIGN KEY (audit_ref, workspace_id)
        REFERENCES omnivia_application_audit_events (audit_ref, workspace_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_engineering_source_stream_origins (
    workspace_id     TEXT    NOT NULL,
    stream_id        TEXT    NOT NULL,
    repository_id    TEXT    NOT NULL,
    installation_id  TEXT    NOT NULL,
    checkout_id      TEXT    NOT NULL,
    bound_at_us      INTEGER NOT NULL,
    audit_ref        TEXT    NOT NULL,

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
    CHECK (typeof(installation_id) = 'text'
           AND length(installation_id) BETWEEN 1 AND 128),
    CHECK (typeof(checkout_id) = 'text' AND length(checkout_id) BETWEEN 1 AND 128
           AND checkout_id GLOB '[A-Za-z0-9]*'
           AND checkout_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(checkout_id, char(0)) = 0),
    CHECK (typeof(bound_at_us) = 'integer' AND bound_at_us > 0),
    CHECK (typeof(audit_ref) = 'text' AND length(audit_ref) BETWEEN 1 AND 128
           AND audit_ref GLOB '[A-Za-z0-9]*'
           AND audit_ref NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(audit_ref, char(0)) = 0),

    FOREIGN KEY (workspace_id, stream_id)
        REFERENCES omnivia_engineering_source_streams (workspace_id, stream_id),
    FOREIGN KEY (workspace_id, repository_id)
        REFERENCES omnivia_engineering_repositories (workspace_id, repository_id),
    FOREIGN KEY (workspace_id, checkout_id)
        REFERENCES omnivia_engineering_checkouts (workspace_id, checkout_id),
    FOREIGN KEY (audit_ref, workspace_id)
        REFERENCES omnivia_application_audit_events (audit_ref, workspace_id)
) WITHOUT ROWID;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_omnivia_engineering_snapshot_files_insert
BEFORE INSERT ON omnivia_engineering_snapshot_files
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_engineering_snapshot_files')
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
    SELECT RAISE(ABORT, 'omnivia: an indexed capture file is written by its own audited engineering.snapshot.capture')
    WHERE NOT EXISTS (
            SELECT 1 FROM omnivia_application_audit_events a
            WHERE a.audit_ref = NEW.audit_ref AND a.workspace_id = NEW.workspace_id
              AND a.operation = 'engineering.snapshot.capture');
    SELECT RAISE(ABORT, 'omnivia: a capture file cannot be indexed once its snapshot''s header is already sealed')
    WHERE EXISTS (
            SELECT 1 FROM omnivia_engineering_snapshot_captures c
            WHERE c.workspace_id = NEW.workspace_id AND c.snapshot_id = NEW.snapshot_id);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_omnivia_engineering_snapshot_files_update
BEFORE UPDATE ON omnivia_engineering_snapshot_files
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_engineering_snapshot_files is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_omnivia_engineering_snapshot_files_delete
BEFORE DELETE ON omnivia_engineering_snapshot_files
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_engineering_snapshot_files is append-only; DELETE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_omnivia_engineering_snapshot_captures_insert
BEFORE INSERT ON omnivia_engineering_snapshot_captures
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_engineering_snapshot_captures')
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
    SELECT RAISE(ABORT, 'omnivia: a captured snapshot header seals a snapshot whose repository, rich manifest digest, capture status and capture time already agree')
    WHERE NOT EXISTS (
            SELECT 1 FROM omnivia_engineering_snapshots sn
            WHERE sn.workspace_id = NEW.workspace_id AND sn.snapshot_id = NEW.snapshot_id
              AND sn.repository_id = NEW.repository_id
              AND sn.manifest_digest = NEW.rich_manifest_digest
              AND sn.capture_status = NEW.capture_status
              AND sn.captured_at_us = NEW.captured_at_us);
    SELECT RAISE(ABORT, 'omnivia: a captured snapshot header names a checkout of its own workspace, repository and installation')
    WHERE NOT EXISTS (
            SELECT 1 FROM omnivia_engineering_checkouts c
            WHERE c.workspace_id = NEW.workspace_id AND c.checkout_id = NEW.checkout_id
              AND c.repository_id = NEW.repository_id
              AND c.installation_id = NEW.installation_id);
    SELECT RAISE(ABORT, 'omnivia: a captured snapshot header names the rich-manifest evidence of its own workspace and digest')
    WHERE NOT EXISTS (
            SELECT 1 FROM omnivia_evidence_artifacts e
            WHERE e.evidence_id = NEW.manifest_evidence_id
              AND e.workspace_id = NEW.workspace_id
              AND e.source_native_id = 'working-tree-manifest.' || NEW.snapshot_id
              AND e.blob_content_digest = NEW.rich_manifest_digest);
    SELECT RAISE(ABORT, 'omnivia: a captured snapshot header seals exactly its indexed file rows, each under its own audit reference')
    WHERE NEW.file_count IS NOT (
            SELECT COUNT(*) FROM omnivia_engineering_snapshot_files f
            WHERE f.workspace_id = NEW.workspace_id AND f.snapshot_id = NEW.snapshot_id)
       OR EXISTS (
            SELECT 1 FROM omnivia_engineering_snapshot_files f
            WHERE f.workspace_id = NEW.workspace_id AND f.snapshot_id = NEW.snapshot_id
              AND f.audit_ref IS NOT NEW.audit_ref);
    SELECT RAISE(ABORT, 'omnivia: a captured snapshot header is sealed by its own audited engineering.snapshot.capture')
    WHERE NOT EXISTS (
            SELECT 1 FROM omnivia_application_audit_events a
            WHERE a.audit_ref = NEW.audit_ref AND a.workspace_id = NEW.workspace_id
              AND a.operation = 'engineering.snapshot.capture');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_omnivia_engineering_snapshot_captures_update
BEFORE UPDATE ON omnivia_engineering_snapshot_captures
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_engineering_snapshot_captures is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_omnivia_engineering_snapshot_captures_delete
BEFORE DELETE ON omnivia_engineering_snapshot_captures
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_engineering_snapshot_captures is append-only; DELETE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_omnivia_engineering_source_stream_origins_insert
BEFORE INSERT ON omnivia_engineering_source_stream_origins
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_engineering_source_stream_origins')
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
    SELECT RAISE(ABORT, 'omnivia: a source stream origin binds an uncovered stream to a checkout of the same repository and installation, before any event exists')
    WHERE NOT EXISTS (
            SELECT 1 FROM omnivia_engineering_source_streams st
            WHERE st.workspace_id = NEW.workspace_id AND st.stream_id = NEW.stream_id
              AND st.repository_id = NEW.repository_id)
       OR NOT EXISTS (
            SELECT 1 FROM omnivia_engineering_checkouts c
            WHERE c.workspace_id = NEW.workspace_id AND c.checkout_id = NEW.checkout_id
              AND c.repository_id = NEW.repository_id
              AND c.installation_id = NEW.installation_id)
       OR EXISTS (
            SELECT 1 FROM omnivia_engineering_source_events e
            WHERE e.workspace_id = NEW.workspace_id AND e.stream_id = NEW.stream_id);
    SELECT RAISE(ABORT, 'omnivia: a source stream origin is bound by its stream owner''s audited engineering.source.capture.commit')
    WHERE NOT EXISTS (
            SELECT 1 FROM omnivia_engineering_source_streams st
            JOIN omnivia_application_audit_events a
              ON a.audit_ref = NEW.audit_ref AND a.workspace_id = NEW.workspace_id
            WHERE st.workspace_id = NEW.workspace_id AND st.stream_id = NEW.stream_id
              AND a.principal_id = st.principal_id
              AND a.operation = 'engineering.source.capture.commit');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_omnivia_engineering_source_stream_origins_update
BEFORE UPDATE ON omnivia_engineering_source_stream_origins
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_engineering_source_stream_origins is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_omnivia_engineering_source_stream_origins_delete
BEFORE DELETE ON omnivia_engineering_source_stream_origins
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_engineering_source_stream_origins is append-only; DELETE is never permitted');
END;

DROP TRIGGER omnivia_guard_omnivia_engineering_source_streams_insert;

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
    SELECT RAISE(ABORT, 'omnivia: a source stream starts uncovered and is owned by the principal whose audited source record or capture commit opens it')
    WHERE NEW.covered_sequence IS NOT 0
       OR NOT EXISTS (
            SELECT 1 FROM omnivia_application_audit_events a
            WHERE a.audit_ref = NEW.audit_ref AND a.workspace_id = NEW.workspace_id
              AND a.principal_id = NEW.principal_id
              AND a.operation IN ('engineering.source.record', 'engineering.source.capture.commit'));
END;

-- The coverage barrier check: the old barrier was validated when it was
-- written and never decreases, so only the newly covered range (OLD, NEW] is
-- counted: one primary-key range scan of at most one pending window, however
-- long the stream's history.
DROP TRIGGER omnivia_guard_omnivia_engineering_source_streams_update;

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
    SELECT RAISE(ABORT, 'omnivia: the source coverage barrier may not cross a missing event or advance past one pending window')
    WHERE NEW.covered_sequence > OLD.covered_sequence
      AND (NEW.covered_sequence - OLD.covered_sequence > 64
           OR (SELECT COUNT(*) FROM omnivia_engineering_source_events e
               WHERE e.workspace_id = NEW.workspace_id AND e.stream_id = NEW.stream_id
                 AND e.sequence > OLD.covered_sequence
                 AND e.sequence <= NEW.covered_sequence)
              IS NOT NEW.covered_sequence - OLD.covered_sequence);
    SELECT RAISE(ABORT, 'omnivia: only the stream owner''s audited source record or capture commit may advance it')
    WHERE NOT EXISTS (
            SELECT 1 FROM omnivia_application_audit_events a
            WHERE a.audit_ref = NEW.audit_ref AND a.workspace_id = NEW.workspace_id
              AND a.principal_id = NEW.principal_id
              AND a.operation IN ('engineering.source.record', 'engineering.source.capture.commit'));
END;

DROP TRIGGER omnivia_guard_omnivia_engineering_source_events_insert;

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
    SELECT RAISE(ABORT, 'omnivia: a source event must be announced by its stream owner''s audited event of its own format''s operation')
    WHERE NOT EXISTS (
            SELECT 1 FROM omnivia_engineering_source_streams st
            JOIN omnivia_application_audit_events a
              ON a.audit_ref = NEW.audit_ref AND a.workspace_id = NEW.workspace_id
            WHERE st.workspace_id = NEW.workspace_id AND st.stream_id = NEW.stream_id
              AND st.announced_sequence >= NEW.sequence
              AND a.principal_id = st.principal_id
              AND a.operation = (CASE NEW.manifest_format
                                    WHEN 'flat_v1' THEN 'engineering.source.record'
                                    WHEN 'captured_v1' THEN 'engineering.source.capture.commit'
                                  END));
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
    SELECT RAISE(ABORT, 'omnivia: a flat_v1 source event is refused on a stream already bound to a captured origin')
    WHERE NEW.manifest_format IS 'flat_v1'
      AND EXISTS (
            SELECT 1 FROM omnivia_engineering_source_stream_origins o
            WHERE o.workspace_id = NEW.workspace_id AND o.stream_id = NEW.stream_id);
    SELECT RAISE(ABORT, 'omnivia: a captured_v1 source event requires the exact empty-manifest sentinel and one joined proof across its stream, stream origin and sealed capture header agreeing on repository, installation and checkout')
    WHERE NEW.manifest_format IS 'captured_v1'
      AND (
            NEW.manifest_json IS NOT '{}'
         OR NEW.manifest_entry_count IS NOT 0
         OR NOT EXISTS (
                SELECT 1
                FROM omnivia_engineering_source_streams st
                JOIN omnivia_engineering_source_stream_origins o
                  ON o.workspace_id = st.workspace_id AND o.stream_id = st.stream_id
                 AND o.repository_id = st.repository_id
                JOIN omnivia_engineering_snapshot_captures c
                  ON c.workspace_id = o.workspace_id
                 AND c.repository_id = o.repository_id
                 AND c.installation_id = o.installation_id
                 AND c.checkout_id = o.checkout_id
                WHERE st.workspace_id = NEW.workspace_id AND st.stream_id = NEW.stream_id
                  AND c.snapshot_id = NEW.snapshot_id
                  AND c.rich_manifest_digest = NEW.manifest_digest));
END;
