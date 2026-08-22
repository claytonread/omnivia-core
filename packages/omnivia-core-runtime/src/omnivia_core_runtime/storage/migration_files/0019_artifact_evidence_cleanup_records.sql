-- Canonical Artifact, EvidenceItem and CleanupReceipt metadata (RT-103).
--
-- Additive only. Three append-only tables, three indexes and nine statement triggers,
-- on top of the RT-102 records migration 0018 added. Every row here belongs to an
-- existing `omnivia_runtime_runs` row; nothing here creates a run, changes a run's
-- status, or duplicates the event stream that already states it.
--
--   omnivia_runtime_artifacts          one content-addressed output a run produced
--   omnivia_runtime_evidence           one piece of evidence a run captured
--   omnivia_runtime_cleanup_receipts   one attempted release of a run's resources
--
-- Content-addressed, not location-addressed. `omnivia_blob_objects` (migration 0008)
-- is the existing verified-identity catalogue for a `(workspace_id, content_digest)`
-- pair; it records that a digest was verified, never where the bytes physically live,
-- and there is no filesystem path, URL, bucket or credential column here or there. An
-- artifact's metadata is therefore storable and readable whether or not a catalogue
-- row -- or any physical object -- currently exists for its digest: an absent blob is
-- an availability fact about the bytes, not a permission to erase or corrupt the
-- Runtime record that outlives them. The one thing this migration does check against
-- the catalogue is a length that contradicts an already-verified one, because a second
-- length for one verified digest is not an availability question -- it is two
-- inconsistent claims about the same identity.
--
-- Every relation is workspace-scoped with a composite primary key and composite
-- foreign keys, WITHOUT ROWID, exactly as 0018 established; every timestamp is a
-- signed 64-bit UTC microsecond `*_at_us`, required positive because each records the
-- instant this runtime observed the fact; every digest is `sha256:<64 lowercase hex>`;
-- identifiers, open codes and bounded tokens use the same GLOB shapes 0007, 0008 and
-- 0018 already established, restated here rather than delegated to because the
-- generated decoder applies no `maxLength` or pattern of its own.
--
-- UPDATE and DELETE abort unconditionally on all three tables, for the current fenced
-- owner too. Metadata that could be rewritten would no longer be the record of what a
-- run actually produced, captured or released, and retained evidence that could be
-- deleted would not be retained. Artifacts, evidence and cleanup receipts may
-- legitimately be recorded against a run that has already reached a terminal status --
-- a closing run states its cleanup and whatever evidence its last step captured, and
-- late-arriving evidence is still evidence -- so, unlike 0018's step, attempt and wait
-- guards, none of the three insert guards below refuses a write merely because the run
-- is terminal.
--
-- What is deliberately absent: a media type, filesystem path or media_type-equivalent
-- on evidence (the accepted `EvidenceItem` carries none); an ordinal or sequence
-- column (nothing here has a required order, only a deterministic *read* order, which
-- the indexes below provide without a stored position); and any relation for
-- PolicySnapshot, BudgetSnapshot, CapabilityGrant, Approval or the Effect family,
-- which remain RT-202+'s.
--
-- Every comment in this file sits *between* statements and never inside one, for the
-- same fingerprint-loader reason 0018 states.

-- One content-addressed output a run produced. Identified by digest and length rather
-- than by location, so an artifact can be proven unchanged without this runtime owning
-- a byte of storage.
CREATE TABLE IF NOT EXISTS omnivia_runtime_artifacts (
    workspace_id          TEXT    NOT NULL,
    artifact_id           TEXT    NOT NULL,
    run_id                TEXT    NOT NULL,
    run_step_id           TEXT,
    artifact_kind         TEXT    NOT NULL,
    media_type            TEXT    NOT NULL,
    content_checksum      TEXT    NOT NULL,
    content_length_bytes  INTEGER NOT NULL,
    produced_at_us        INTEGER NOT NULL,

    PRIMARY KEY (workspace_id, artifact_id),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(artifact_id) = 'text' AND length(artifact_id) BETWEEN 1 AND 128
           AND artifact_id GLOB '[A-Za-z0-9]*'
           AND artifact_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(artifact_id, char(0)) = 0),
    CHECK (typeof(run_id) = 'text' AND length(run_id) BETWEEN 1 AND 128
           AND run_id GLOB '[A-Za-z0-9]*'
           AND run_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(run_id, char(0)) = 0),
    CHECK (run_step_id IS NULL OR (typeof(run_step_id) = 'text'
           AND length(run_step_id) BETWEEN 1 AND 128
           AND run_step_id GLOB '[A-Za-z0-9]*'
           AND run_step_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(run_step_id, char(0)) = 0)),
    CHECK (typeof(artifact_kind) = 'text' AND length(artifact_kind) BETWEEN 1 AND 128
           AND artifact_kind GLOB '[a-z]*' AND artifact_kind NOT GLOB '*[^a-z0-9_.]*'
           AND artifact_kind NOT GLOB '*.' AND artifact_kind NOT GLOB '*.[^a-z]*'),
    CHECK (
        typeof(media_type) = 'text'
        AND length(media_type) BETWEEN 3 AND 255
        AND length(media_type) - length(replace(media_type, '/', '')) = 1
        AND substr(media_type, 1, instr(media_type, '/') - 1) GLOB '[A-Za-z0-9]*'
        AND substr(media_type, 1, instr(media_type, '/') - 1)
            NOT GLOB '*[^A-Za-z0-9!#$&^_.+-]*'
        AND substr(media_type, instr(media_type, '/') + 1) GLOB '[A-Za-z0-9]*'
        AND substr(media_type, instr(media_type, '/') + 1)
            NOT GLOB '*[^A-Za-z0-9!#$&^_.+-]*'
        AND instr(media_type, char(0)) = 0
    ),
    CHECK (typeof(content_checksum) = 'text' AND length(content_checksum) = 71
           AND substr(content_checksum, 1, 7) = 'sha256:'
           AND substr(content_checksum, 8) NOT GLOB '*[^0-9a-f]*'),
    CHECK (typeof(content_length_bytes) = 'integer' AND content_length_bytes >= 0),
    CHECK (typeof(produced_at_us) = 'integer' AND produced_at_us > 0),

    FOREIGN KEY (workspace_id, run_id)
        REFERENCES omnivia_runtime_runs (workspace_id, run_id),
    FOREIGN KEY (workspace_id, run_step_id)
        REFERENCES omnivia_runtime_run_steps (workspace_id, run_step_id)
) WITHOUT ROWID;

-- One piece of evidence a run captured. `source_workspace_id` restates the workspace
-- the correlated `ExternalReference` names -- the accepted contract requires a
-- correlation to belong to the record that carries it, and the column-level `CHECK`
-- below makes that a fact SQLite enforces on every row rather than a convention this
-- migration merely documents. Only `authoritative` evidence whose `source_kind` is
-- `runtime` may claim to be the runtime's own record; every other kind is
-- corroboration, however complete it looks, and the insert guard refuses the
-- contradiction of an authoritative claim from a subordinate source.
CREATE TABLE IF NOT EXISTS omnivia_runtime_evidence (
    workspace_id         TEXT    NOT NULL,
    evidence_item_id     TEXT    NOT NULL,
    run_id               TEXT    NOT NULL,
    run_step_id          TEXT,
    evidence_kind        TEXT    NOT NULL,
    source_kind          TEXT    NOT NULL,
    source_id            TEXT    NOT NULL,
    source_workspace_id  TEXT    NOT NULL,
    content_checksum     TEXT    NOT NULL,
    artifact_id          TEXT,
    captured_at_us       INTEGER NOT NULL,
    authoritative        INTEGER NOT NULL,
    retained             INTEGER NOT NULL,

    PRIMARY KEY (workspace_id, evidence_item_id),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(evidence_item_id) = 'text'
           AND length(evidence_item_id) BETWEEN 1 AND 128
           AND evidence_item_id GLOB '[A-Za-z0-9]*'
           AND evidence_item_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(evidence_item_id, char(0)) = 0),
    CHECK (typeof(run_id) = 'text' AND length(run_id) BETWEEN 1 AND 128
           AND run_id GLOB '[A-Za-z0-9]*'
           AND run_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(run_id, char(0)) = 0),
    CHECK (run_step_id IS NULL OR (typeof(run_step_id) = 'text'
           AND length(run_step_id) BETWEEN 1 AND 128
           AND run_step_id GLOB '[A-Za-z0-9]*'
           AND run_step_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(run_step_id, char(0)) = 0)),
    CHECK (typeof(evidence_kind) = 'text' AND length(evidence_kind) BETWEEN 1 AND 128
           AND evidence_kind GLOB '[a-z]*' AND evidence_kind NOT GLOB '*[^a-z0-9_.]*'
           AND evidence_kind NOT GLOB '*.' AND evidence_kind NOT GLOB '*.[^a-z]*'),
    CHECK (source_kind IN ('runtime', 'application_job', 'control_plane_projection',
                           'agent_lane_ledger', 'external_log')),
    CHECK (typeof(source_id) = 'text' AND length(source_id) BETWEEN 1 AND 512
           AND source_id NOT GLOB '*[^!-~]*'),
    CHECK (typeof(source_workspace_id) = 'text'
           AND length(source_workspace_id) BETWEEN 1 AND 128
           AND source_workspace_id GLOB '[A-Za-z0-9]*'
           AND source_workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(source_workspace_id, char(0)) = 0),
    CHECK (source_workspace_id = workspace_id),
    CHECK (typeof(content_checksum) = 'text' AND length(content_checksum) = 71
           AND substr(content_checksum, 1, 7) = 'sha256:'
           AND substr(content_checksum, 8) NOT GLOB '*[^0-9a-f]*'),
    CHECK (artifact_id IS NULL OR (typeof(artifact_id) = 'text'
           AND length(artifact_id) BETWEEN 1 AND 128
           AND artifact_id GLOB '[A-Za-z0-9]*'
           AND artifact_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(artifact_id, char(0)) = 0)),
    CHECK (typeof(captured_at_us) = 'integer' AND captured_at_us > 0),
    CHECK (typeof(authoritative) = 'integer' AND authoritative IN (0, 1)),
    CHECK (typeof(retained) = 'integer' AND retained IN (0, 1)),

    FOREIGN KEY (workspace_id, run_id)
        REFERENCES omnivia_runtime_runs (workspace_id, run_id),
    FOREIGN KEY (workspace_id, run_step_id)
        REFERENCES omnivia_runtime_run_steps (workspace_id, run_step_id),
    FOREIGN KEY (workspace_id, artifact_id)
        REFERENCES omnivia_runtime_artifacts (workspace_id, artifact_id)
) WITHOUT ROWID;

-- The record that cleanup was attempted for one run, and what it achieved. Written
-- for the attempt, not for the success, so a failed release is a row rather than
-- silence indistinguishable from cleanup that never ran.
CREATE TABLE IF NOT EXISTS omnivia_runtime_cleanup_receipts (
    workspace_id         TEXT    NOT NULL,
    cleanup_receipt_id   TEXT    NOT NULL,
    run_id               TEXT    NOT NULL,
    resource_kind        TEXT    NOT NULL,
    outcome              TEXT    NOT NULL,
    reason               TEXT    NOT NULL,
    performed_at_us      INTEGER NOT NULL,
    audit_ref            TEXT    NOT NULL,

    PRIMARY KEY (workspace_id, cleanup_receipt_id),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(cleanup_receipt_id) = 'text'
           AND length(cleanup_receipt_id) BETWEEN 1 AND 128
           AND cleanup_receipt_id GLOB '[A-Za-z0-9]*'
           AND cleanup_receipt_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(cleanup_receipt_id, char(0)) = 0),
    CHECK (typeof(run_id) = 'text' AND length(run_id) BETWEEN 1 AND 128
           AND run_id GLOB '[A-Za-z0-9]*'
           AND run_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(run_id, char(0)) = 0),
    CHECK (typeof(resource_kind) = 'text' AND length(resource_kind) BETWEEN 1 AND 128
           AND resource_kind GLOB '[a-z]*' AND resource_kind NOT GLOB '*[^a-z0-9_.]*'
           AND resource_kind NOT GLOB '*.' AND resource_kind NOT GLOB '*.[^a-z]*'),
    CHECK (outcome IN ('released', 'not_required', 'failed')),
    CHECK (typeof(reason) = 'text' AND length(reason) BETWEEN 1 AND 128
           AND reason GLOB '[a-z]*' AND reason NOT GLOB '*[^a-z0-9_.]*'
           AND reason NOT GLOB '*.' AND reason NOT GLOB '*.[^a-z]*'),
    CHECK (typeof(performed_at_us) = 'integer' AND performed_at_us > 0),
    CHECK (typeof(audit_ref) = 'text' AND length(audit_ref) BETWEEN 1 AND 128
           AND audit_ref GLOB '[A-Za-z0-9]*'
           AND audit_ref NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(audit_ref, char(0)) = 0),

    FOREIGN KEY (workspace_id, run_id)
        REFERENCES omnivia_runtime_runs (workspace_id, run_id),
    FOREIGN KEY (audit_ref, workspace_id)
        REFERENCES omnivia_application_audit_events (audit_ref, workspace_id)
) WITHOUT ROWID;

-- Three indexes, one per read the repository actually performs: every artifact,
-- evidence item or cleanup receipt of one run, in deterministic order. The primary
-- keys already index lookup by identifier; nothing here duplicates them.
CREATE INDEX IF NOT EXISTS omnivia_idx_runtime_artifacts_run
    ON omnivia_runtime_artifacts (workspace_id, run_id, produced_at_us, artifact_id);
CREATE INDEX IF NOT EXISTS omnivia_idx_runtime_evidence_run
    ON omnivia_runtime_evidence (workspace_id, run_id, captured_at_us, evidence_item_id);
CREATE INDEX IF NOT EXISTS omnivia_idx_runtime_cleanup_receipts_run
    ON omnivia_runtime_cleanup_receipts (workspace_id, run_id, performed_at_us, cleanup_receipt_id);

-- The three INSERT guards below repeat the complete connection-authority,
-- mutation-guard, workspace-state and lease predicate 0005 established, then add the
-- rules specific to the record they admit. UPDATE and DELETE abort unconditionally on
-- all three tables -- for the current fenced owner too.

CREATE TRIGGER IF NOT EXISTS omnivia_guard_runtime_artifacts_insert
BEFORE INSERT ON omnivia_runtime_artifacts
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_runtime_artifacts')
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
    SELECT RAISE(ABORT, 'omnivia: an artifact must name the run of its own step')
    WHERE NEW.run_step_id IS NOT NULL
      AND NOT EXISTS (
        SELECT 1 FROM omnivia_runtime_run_steps
        WHERE workspace_id = NEW.workspace_id AND run_step_id = NEW.run_step_id
          AND run_id = NEW.run_id);
    SELECT RAISE(ABORT, 'omnivia: an artifact must not predate its run')
    WHERE NEW.produced_at_us < (
        SELECT created_at_us FROM omnivia_runtime_runs
        WHERE workspace_id = NEW.workspace_id AND run_id = NEW.run_id);
    SELECT RAISE(ABORT, 'omnivia: an artifact must not predate its step')
    WHERE NEW.run_step_id IS NOT NULL
      AND NEW.produced_at_us < (
        SELECT created_at_us FROM omnivia_runtime_run_steps
        WHERE workspace_id = NEW.workspace_id AND run_step_id = NEW.run_step_id);
    SELECT RAISE(ABORT, 'omnivia: an artifact contradicts a verified blob of the same address')
    WHERE EXISTS (
        SELECT 1 FROM omnivia_blob_objects
        WHERE workspace_id = NEW.workspace_id AND content_digest = NEW.content_checksum
          AND content_length_bytes <> NEW.content_length_bytes);
    SELECT RAISE(ABORT, 'omnivia: a run may hold at most 256 artifacts')
    WHERE 256 <= (
        SELECT COUNT(*) FROM omnivia_runtime_artifacts
        WHERE workspace_id = NEW.workspace_id AND run_id = NEW.run_id);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_runtime_artifacts_update
BEFORE UPDATE ON omnivia_runtime_artifacts
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_runtime_artifacts is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_runtime_artifacts_delete
BEFORE DELETE ON omnivia_runtime_artifacts
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_runtime_artifacts is append-only; DELETE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_runtime_evidence_insert
BEFORE INSERT ON omnivia_runtime_evidence
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_runtime_evidence')
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
    SELECT RAISE(ABORT, 'omnivia: evidence must name the run of its own step')
    WHERE NEW.run_step_id IS NOT NULL
      AND NOT EXISTS (
        SELECT 1 FROM omnivia_runtime_run_steps
        WHERE workspace_id = NEW.workspace_id AND run_step_id = NEW.run_step_id
          AND run_id = NEW.run_id);
    SELECT RAISE(ABORT, 'omnivia: evidence must not predate its run')
    WHERE NEW.captured_at_us < (
        SELECT created_at_us FROM omnivia_runtime_runs
        WHERE workspace_id = NEW.workspace_id AND run_id = NEW.run_id);
    SELECT RAISE(ABORT, 'omnivia: evidence must not predate its step')
    WHERE NEW.run_step_id IS NOT NULL
      AND NEW.captured_at_us < (
        SELECT created_at_us FROM omnivia_runtime_run_steps
        WHERE workspace_id = NEW.workspace_id AND run_step_id = NEW.run_step_id);
    SELECT RAISE(ABORT, 'omnivia: evidence naming an artifact must agree with its checksum')
    WHERE NEW.artifact_id IS NOT NULL
      AND NOT EXISTS (
        SELECT 1 FROM omnivia_runtime_artifacts
        WHERE workspace_id = NEW.workspace_id AND artifact_id = NEW.artifact_id
          AND run_id = NEW.run_id AND content_checksum = NEW.content_checksum);
    SELECT RAISE(ABORT, 'omnivia: only evidence from the runtime itself may be authoritative')
    WHERE NEW.authoritative = 1 AND NEW.source_kind <> 'runtime';
    SELECT RAISE(ABORT, 'omnivia: a run may hold at most 256 evidence items')
    WHERE 256 <= (
        SELECT COUNT(*) FROM omnivia_runtime_evidence
        WHERE workspace_id = NEW.workspace_id AND run_id = NEW.run_id);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_runtime_evidence_update
BEFORE UPDATE ON omnivia_runtime_evidence
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_runtime_evidence is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_runtime_evidence_delete
BEFORE DELETE ON omnivia_runtime_evidence
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_runtime_evidence is append-only; DELETE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_runtime_cleanup_receipts_insert
BEFORE INSERT ON omnivia_runtime_cleanup_receipts
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_runtime_cleanup_receipts')
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
    SELECT RAISE(ABORT, 'omnivia: a cleanup receipt must not predate its run')
    WHERE NEW.performed_at_us < (
        SELECT created_at_us FROM omnivia_runtime_runs
        WHERE workspace_id = NEW.workspace_id AND run_id = NEW.run_id);
    SELECT RAISE(ABORT, 'omnivia: a run may hold at most 64 cleanup receipts')
    WHERE 64 <= (
        SELECT COUNT(*) FROM omnivia_runtime_cleanup_receipts
        WHERE workspace_id = NEW.workspace_id AND run_id = NEW.run_id);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_runtime_cleanup_receipts_update
BEFORE UPDATE ON omnivia_runtime_cleanup_receipts
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_runtime_cleanup_receipts is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_runtime_cleanup_receipts_delete
BEFORE DELETE ON omnivia_runtime_cleanup_receipts
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_runtime_cleanup_receipts is append-only; DELETE is never permitted');
END;
