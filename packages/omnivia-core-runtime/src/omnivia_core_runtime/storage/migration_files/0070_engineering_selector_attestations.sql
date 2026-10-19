-- Trusted selector attestations for `symbol` and `source_span` dependencies.
--
-- Core never parses source.  An installed Dev adapter running as the authenticated
-- stream owner states what one selector resolved to in one sealed snapshot, and Core
-- stores that statement as immutable, bound evidence.  The evaluator later compares
-- selector digests between a dependency's baseline snapshot and a covered target.
--
--   omnivia_engineering_selector_attestations
--       one immutable statement per (snapshot, selector type, selector).  It binds the
--       workspace, the installation and the stream owner (the producer principal), the
--       repository, the stream, the sealed snapshot, the normalized path, the snapshot
--       index's whole-file digest for that path, the selector, the adapter's identity
--       and version, the file coverage and whether the selector was present.  The
--       adapter's selector digest is evidence only: the insert trigger accepts it only
--       after the stream owner, repository, recorded snapshot and whole-file digest
--       all agree with what Core already holds.
--
-- Trigger notes (kept out of the trigger bodies, whose stored text the schema
-- fingerprint compares).  The insert requires the
-- snapshot to be a recorded event of the named stream, and the stated whole-file digest
-- to be exactly what that snapshot's own index (`captured_v1`) or inline manifest
-- (`flat_v1`) holds for the path: anything else is evidence about a file Core never
-- captured.
--
-- A second, different statement for the same selector in the same snapshot is refused;
-- the row is never updated or deleted.  An adapter upgrade therefore cannot re-attest an
-- old snapshot, and digests from different adapter versions are never compared: that
-- reads as `unknown`, never as a match.

CREATE TABLE IF NOT EXISTS omnivia_engineering_selector_attestations (
    workspace_id          TEXT    NOT NULL,
    attestation_id        TEXT    NOT NULL,
    installation_id       TEXT    NOT NULL,
    producer_principal_id TEXT    NOT NULL,
    repository_id         TEXT    NOT NULL,
    stream_id             TEXT    NOT NULL,
    snapshot_id           TEXT    NOT NULL,
    path                  TEXT    NOT NULL,
    file_digest           TEXT    NOT NULL,
    selector_type         TEXT    NOT NULL,
    selector              TEXT    NOT NULL,
    file_coverage         TEXT    NOT NULL,
    selector_state        TEXT    NOT NULL,
    selector_digest       TEXT,
    adapter_id            TEXT    NOT NULL,
    adapter_version       TEXT    NOT NULL,
    recorded_at_us        INTEGER NOT NULL,
    audit_ref             TEXT    NOT NULL,

    PRIMARY KEY (workspace_id, attestation_id),
    UNIQUE (workspace_id, snapshot_id, selector_type, selector),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(attestation_id) = 'text' AND length(attestation_id) BETWEEN 1 AND 128
           AND attestation_id GLOB '[A-Za-z0-9]*'
           AND attestation_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(attestation_id, char(0)) = 0),
    CHECK (typeof(installation_id) = 'text' AND length(installation_id) BETWEEN 1 AND 128),
    CHECK (typeof(producer_principal_id) = 'text'
           AND length(producer_principal_id) BETWEEN 1 AND 128),
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
    CHECK (typeof(path) = 'text' AND length(path) BETWEEN 1 AND 512
           AND instr(path, char(0)) = 0),
    CHECK (typeof(file_digest) = 'text' AND length(file_digest) = 71
           AND substr(file_digest, 1, 7) = 'sha256:'
           AND substr(file_digest, 8) NOT GLOB '*[^0-9a-f]*'),
    CHECK (selector_type IN ('symbol', 'source_span')),
    CHECK (typeof(selector) = 'text' AND length(selector) BETWEEN 1 AND 512
           AND instr(selector, char(0)) = 0),
    CHECK (file_coverage IN ('complete', 'partial')),
    CHECK (selector_state IN ('present', 'absent')),
    CHECK ((selector_state = 'present') = (selector_digest IS NOT NULL)),
    CHECK (selector_digest IS NULL
           OR (typeof(selector_digest) = 'text' AND length(selector_digest) = 71
               AND substr(selector_digest, 1, 7) = 'sha256:'
               AND substr(selector_digest, 8) NOT GLOB '*[^0-9a-f]*')),
    CHECK (typeof(adapter_id) = 'text' AND length(adapter_id) BETWEEN 1 AND 128
           AND instr(adapter_id, char(0)) = 0),
    CHECK (typeof(adapter_version) = 'text' AND length(adapter_version) BETWEEN 1 AND 64
           AND instr(adapter_version, char(0)) = 0),
    CHECK (typeof(recorded_at_us) = 'integer' AND recorded_at_us > 0),
    CHECK (typeof(audit_ref) = 'text' AND length(audit_ref) BETWEEN 1 AND 128
           AND audit_ref GLOB '[A-Za-z0-9]*'
           AND audit_ref NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(audit_ref, char(0)) = 0),

    FOREIGN KEY (workspace_id, stream_id)
        REFERENCES omnivia_engineering_source_streams (workspace_id, stream_id),
    FOREIGN KEY (workspace_id, repository_id)
        REFERENCES omnivia_engineering_repositories (workspace_id, repository_id),
    FOREIGN KEY (workspace_id, snapshot_id)
        REFERENCES omnivia_engineering_snapshots (workspace_id, snapshot_id),
    FOREIGN KEY (audit_ref, workspace_id)
        REFERENCES omnivia_application_audit_events (audit_ref, workspace_id)
) WITHOUT ROWID;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_engineering_selector_attestations_insert
BEFORE INSERT ON omnivia_engineering_selector_attestations
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_engineering_selector_attestations')
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
              AND l.lifecycle IN ('acquiring', 'held', 'draining')
       )
       OR NEW.workspace_id IS NOT (
            SELECT workspace_id FROM omnivia_workspace_state WHERE singleton = 1);

    SELECT RAISE(ABORT, 'omnivia: a selector attestation requires its producer''s exact successful audit')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_application_audit_events a
        WHERE a.workspace_id = NEW.workspace_id
          AND a.audit_ref = NEW.audit_ref
          AND a.principal_id = NEW.producer_principal_id
          AND a.operation = 'engineering.selector.attest'
          AND a.outcome_class = 'succeeded'
          AND a.recorded_at_us = NEW.recorded_at_us
    );

    SELECT RAISE(ABORT, 'omnivia: a selector attestation must come from its stream''s owner for its repository')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_engineering_source_streams st
        WHERE st.workspace_id = NEW.workspace_id
          AND st.stream_id = NEW.stream_id
          AND st.repository_id = NEW.repository_id
          AND st.principal_id = NEW.producer_principal_id
    );

    SELECT RAISE(ABORT, 'omnivia: a selector attestation must come from its stream''s own installation')
    WHERE EXISTS (
        SELECT 1 FROM omnivia_engineering_source_stream_origins o
        WHERE o.workspace_id = NEW.workspace_id
          AND o.stream_id = NEW.stream_id
          AND o.installation_id <> NEW.installation_id
    ) OR EXISTS (
        SELECT 1 FROM omnivia_engineering_snapshot_captures c
        WHERE c.workspace_id = NEW.workspace_id
          AND c.snapshot_id = NEW.snapshot_id
          AND c.installation_id <> NEW.installation_id
    );

    SELECT RAISE(ABORT, 'omnivia: a selector attestation must bind its snapshot''s captured whole-file digest')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_engineering_source_events e
        WHERE e.workspace_id = NEW.workspace_id
          AND e.stream_id = NEW.stream_id
          AND e.snapshot_id = NEW.snapshot_id
          AND (
              (e.manifest_format = 'flat_v1'
               AND EXISTS (
                   SELECT 1 FROM json_each(e.manifest_json) j
                   WHERE j.key = NEW.path AND j.value = NEW.file_digest))
              OR
              (e.manifest_format = 'captured_v1'
               AND EXISTS (
                   SELECT 1 FROM omnivia_engineering_snapshot_files f
                   WHERE f.workspace_id = e.workspace_id
                     AND f.snapshot_id = e.snapshot_id
                     AND f.path = NEW.path
                     AND f.content_digest = NEW.file_digest))
          )
    );
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_engineering_selector_attestations_update
BEFORE UPDATE ON omnivia_engineering_selector_attestations
BEGIN
    SELECT RAISE(ABORT, 'omnivia: selector attestations are append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_engineering_selector_attestations_delete
BEFORE DELETE ON omnivia_engineering_selector_attestations
BEGIN
    SELECT RAISE(ABORT, 'omnivia: selector attestations are append-only; DELETE is never permitted');
END;
