-- Document intake: logical documents, immutable source captures and immutable extraction revisions (migration 0069).
--
-- Additive only; allocation 0069 (predecessor 0068). Seven tables and their guard triggers.
--
--   omnivia_documents                      one logical document; immutable
--   omnivia_document_captures              one immutable captured source revision of a document
--   omnivia_document_capture_retention     one capture's retention state and its generation; guarded advances only
--   omnivia_extraction_profiles            one content-addressed ExtractionProfileV1; immutable
--   omnivia_extraction_revisions           one admitted extraction over one capture; immutable
--   omnivia_extraction_results             the one terminal result of a revision; immutable
--   omnivia_document_heads                 one document's current capture, current extraction and source state
--
-- Captures. A capture names an existing evidence artifact and the exact digest and length of the immutable
-- binary source that evidence holds. Two captures may name the same blob, but each names its own evidence, so
-- a shared blob never merges two captures or two documents. A capture is numbered per document in admission
-- order. It records the capture the caller expected to be current (null when it expected none) and is
-- `head_eligible` exactly when that expectation was still the document's head when it was admitted; any other
-- capture is `historical_only`. A capture row is never edited.
--
-- Heads. The capture head and the extraction head are separate pairs of columns in one row, and each moves only
-- by its own guarded write: a capture head moves only to a head-eligible, retained capture with a greater
-- sequence, an extraction head moves only to a promoted, head-eligible extraction with a greater ordinal whose
-- frozen generations are still current, and one write moves exactly one of the capture head, the extraction
-- head and the source state. So an extraction can never move a capture head, and a late older write can never
-- replace a newer one.
--
-- Generations. `source_removal_generation` (per document, in the head row) and `capture_retention_generation`
-- (per capture, in the retention row) only ever advance by one. A revision freezes both when it is admitted, and
-- its promotion is refused unless both are unchanged and the capture is still retained.
--
-- Operation keys. A capture and an extraction revision each carry an operation key and the digest of the request
-- that used it, unique within the Workspace, so replaying the same request is recognisable and a different
-- request cannot reuse the key.
--
-- Every write runs inside the caller's `fenced_transaction`. The INSERT guards carry the same connection-
-- authority, guard, workspace-state and lease predicate as the other guarded tables and bind each row to the
-- open workspace and its current fencing generation. Identifiers are restricted to the same closed character
-- set as the other tables. Nothing here names a filesystem path.
--
-- No DML, and no comment sits inside a statement below, for the migrator's statement splitter.

CREATE TABLE IF NOT EXISTS omnivia_documents (
    workspace_id                TEXT    NOT NULL,
    document_id                 TEXT    NOT NULL,
    fencing_generation          INTEGER NOT NULL,
    created_at_us               INTEGER NOT NULL,

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(document_id) = 'text' AND length(document_id) BETWEEN 1 AND 128
           AND document_id GLOB '[A-Za-z0-9]*'
           AND document_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(document_id, char(0)) = 0),
    CHECK (typeof(fencing_generation) = 'integer'
           AND fencing_generation BETWEEN 1 AND 9223372036854775807),
    CHECK (typeof(created_at_us) = 'integer' AND created_at_us BETWEEN 1 AND 9223372036854775807),

    PRIMARY KEY (workspace_id, document_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_document_captures (
    workspace_id                TEXT    NOT NULL,
    capture_id                  TEXT    NOT NULL,
    document_id                 TEXT    NOT NULL,
    capture_sequence            INTEGER NOT NULL,
    evidence_id                 TEXT    NOT NULL,
    source_digest               TEXT    NOT NULL,
    source_length_bytes         INTEGER NOT NULL,
    media_type                  TEXT    NOT NULL,
    expected_base_capture_id    TEXT,
    disposition                 TEXT    NOT NULL,
    source_removal_generation   INTEGER NOT NULL,
    operation_key               TEXT    NOT NULL,
    request_digest              TEXT    NOT NULL,
    fencing_generation          INTEGER NOT NULL,
    created_at_us               INTEGER NOT NULL,

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(capture_id) = 'text' AND length(capture_id) = 69
           AND substr(capture_id, 1, 5) = 'dcap-'
           AND substr(capture_id, 6) NOT GLOB '*[^0-9a-f]*'),
    CHECK (typeof(document_id) = 'text' AND length(document_id) BETWEEN 1 AND 128
           AND document_id GLOB '[A-Za-z0-9]*'
           AND document_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(document_id, char(0)) = 0),
    CHECK (typeof(capture_sequence) = 'integer' AND capture_sequence BETWEEN 1 AND 9223372036854775807),
    CHECK (typeof(evidence_id) = 'text' AND length(evidence_id) BETWEEN 1 AND 128
           AND evidence_id GLOB '[A-Za-z0-9]*'
           AND evidence_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(evidence_id, char(0)) = 0),
    CHECK (typeof(source_digest) = 'text' AND length(source_digest) = 71
           AND substr(source_digest, 1, 7) = 'sha256:'
           AND substr(source_digest, 8) NOT GLOB '*[^0-9a-f]*'),
    CHECK (typeof(source_length_bytes) = 'integer' AND source_length_bytes BETWEEN 0 AND 16777216),
    CHECK (typeof(media_type) = 'text' AND length(media_type) BETWEEN 3 AND 255
           AND instr(media_type, '/') > 1 AND instr(media_type, char(0)) = 0),
    CHECK (expected_base_capture_id IS NULL
           OR (typeof(expected_base_capture_id) = 'text' AND length(expected_base_capture_id) = 69
               AND substr(expected_base_capture_id, 1, 5) = 'dcap-'
               AND substr(expected_base_capture_id, 6) NOT GLOB '*[^0-9a-f]*'
               AND expected_base_capture_id <> capture_id)),
    CHECK (disposition IN ('head_eligible', 'historical_only')),
    CHECK (typeof(source_removal_generation) = 'integer'
           AND source_removal_generation BETWEEN 1 AND 9223372036854775807),
    CHECK (typeof(operation_key) = 'text' AND length(operation_key) BETWEEN 1 AND 128
           AND operation_key GLOB '[A-Za-z0-9]*'
           AND operation_key NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(operation_key, char(0)) = 0),
    CHECK (typeof(request_digest) = 'text' AND length(request_digest) = 71
           AND substr(request_digest, 1, 7) = 'sha256:'
           AND substr(request_digest, 8) NOT GLOB '*[^0-9a-f]*'),
    CHECK (typeof(fencing_generation) = 'integer'
           AND fencing_generation BETWEEN 1 AND 9223372036854775807),
    CHECK (typeof(created_at_us) = 'integer' AND created_at_us BETWEEN 1 AND 9223372036854775807),

    PRIMARY KEY (workspace_id, capture_id),
    UNIQUE (workspace_id, capture_id, document_id),
    UNIQUE (workspace_id, document_id, capture_sequence),
    UNIQUE (workspace_id, operation_key),
    UNIQUE (workspace_id, evidence_id),
    FOREIGN KEY (workspace_id, document_id)
        REFERENCES omnivia_documents (workspace_id, document_id),
    FOREIGN KEY (workspace_id, expected_base_capture_id, document_id)
        REFERENCES omnivia_document_captures (workspace_id, capture_id, document_id),
    FOREIGN KEY (evidence_id, workspace_id, source_digest)
        REFERENCES omnivia_evidence_artifacts (evidence_id, workspace_id, blob_content_digest),
    FOREIGN KEY (workspace_id, source_digest, source_length_bytes)
        REFERENCES omnivia_blob_objects (workspace_id, content_digest, content_length_bytes)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_document_capture_retention (
    workspace_id                TEXT    NOT NULL,
    capture_id                  TEXT    NOT NULL,
    retention_state             TEXT    NOT NULL,
    capture_retention_generation INTEGER NOT NULL,
    fencing_generation          INTEGER NOT NULL,
    changed_at_us               INTEGER NOT NULL,

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(capture_id) = 'text' AND length(capture_id) = 69
           AND substr(capture_id, 1, 5) = 'dcap-'
           AND substr(capture_id, 6) NOT GLOB '*[^0-9a-f]*'),
    CHECK (retention_state IN ('retained', 'expired', 'deleted')),
    CHECK (typeof(capture_retention_generation) = 'integer'
           AND capture_retention_generation BETWEEN 1 AND 9223372036854775807),
    CHECK (typeof(fencing_generation) = 'integer'
           AND fencing_generation BETWEEN 1 AND 9223372036854775807),
    CHECK (typeof(changed_at_us) = 'integer' AND changed_at_us BETWEEN 1 AND 9223372036854775807),

    PRIMARY KEY (workspace_id, capture_id),
    FOREIGN KEY (workspace_id, capture_id)
        REFERENCES omnivia_document_captures (workspace_id, capture_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_extraction_profiles (
    workspace_id                TEXT    NOT NULL,
    profile_digest              TEXT    NOT NULL,
    profile_json                TEXT    NOT NULL,
    fencing_generation          INTEGER NOT NULL,
    created_at_us               INTEGER NOT NULL,

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(profile_digest) = 'text' AND length(profile_digest) = 71
           AND substr(profile_digest, 1, 7) = 'sha256:'
           AND substr(profile_digest, 8) NOT GLOB '*[^0-9a-f]*'),
    CHECK (typeof(profile_json) = 'text' AND length(CAST(profile_json AS BLOB)) BETWEEN 2 AND 65536),
    CHECK (typeof(fencing_generation) = 'integer'
           AND fencing_generation BETWEEN 1 AND 9223372036854775807),
    CHECK (typeof(created_at_us) = 'integer' AND created_at_us BETWEEN 1 AND 9223372036854775807),

    PRIMARY KEY (workspace_id, profile_digest)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_extraction_revisions (
    workspace_id                TEXT    NOT NULL,
    extraction_revision_id      TEXT    NOT NULL,
    document_id                 TEXT    NOT NULL,
    capture_id                  TEXT    NOT NULL,
    input_digest                TEXT    NOT NULL,
    profile_digest              TEXT    NOT NULL,
    extraction_sequence         INTEGER NOT NULL,
    document_ordinal            INTEGER NOT NULL,
    expected_base_extraction_id TEXT,
    head_eligible               INTEGER NOT NULL,
    source_removal_generation   INTEGER NOT NULL,
    capture_retention_generation INTEGER NOT NULL,
    operation_key               TEXT    NOT NULL,
    request_digest              TEXT    NOT NULL,
    fencing_generation          INTEGER NOT NULL,
    created_at_us               INTEGER NOT NULL,

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(extraction_revision_id) = 'text' AND length(extraction_revision_id) = 69
           AND substr(extraction_revision_id, 1, 5) = 'dext-'
           AND substr(extraction_revision_id, 6) NOT GLOB '*[^0-9a-f]*'),
    CHECK (typeof(document_id) = 'text' AND length(document_id) BETWEEN 1 AND 128
           AND document_id GLOB '[A-Za-z0-9]*'
           AND document_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(document_id, char(0)) = 0),
    CHECK (typeof(capture_id) = 'text' AND length(capture_id) = 69
           AND substr(capture_id, 1, 5) = 'dcap-'
           AND substr(capture_id, 6) NOT GLOB '*[^0-9a-f]*'),
    CHECK (typeof(input_digest) = 'text' AND length(input_digest) = 71
           AND substr(input_digest, 1, 7) = 'sha256:'
           AND substr(input_digest, 8) NOT GLOB '*[^0-9a-f]*'),
    CHECK (typeof(profile_digest) = 'text' AND length(profile_digest) = 71
           AND substr(profile_digest, 1, 7) = 'sha256:'
           AND substr(profile_digest, 8) NOT GLOB '*[^0-9a-f]*'),
    CHECK (typeof(extraction_sequence) = 'integer'
           AND extraction_sequence BETWEEN 1 AND 9223372036854775807),
    CHECK (typeof(document_ordinal) = 'integer'
           AND document_ordinal BETWEEN 1 AND 9223372036854775807),
    CHECK (expected_base_extraction_id IS NULL
           OR (typeof(expected_base_extraction_id) = 'text' AND length(expected_base_extraction_id) = 69
               AND substr(expected_base_extraction_id, 1, 5) = 'dext-'
               AND substr(expected_base_extraction_id, 6) NOT GLOB '*[^0-9a-f]*'
               AND expected_base_extraction_id <> extraction_revision_id)),
    CHECK (head_eligible IN (0, 1)),
    CHECK (typeof(source_removal_generation) = 'integer'
           AND source_removal_generation BETWEEN 1 AND 9223372036854775807),
    CHECK (typeof(capture_retention_generation) = 'integer'
           AND capture_retention_generation BETWEEN 1 AND 9223372036854775807),
    CHECK (typeof(operation_key) = 'text' AND length(operation_key) BETWEEN 1 AND 128
           AND operation_key GLOB '[A-Za-z0-9]*'
           AND operation_key NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(operation_key, char(0)) = 0),
    CHECK (typeof(request_digest) = 'text' AND length(request_digest) = 71
           AND substr(request_digest, 1, 7) = 'sha256:'
           AND substr(request_digest, 8) NOT GLOB '*[^0-9a-f]*'),
    CHECK (typeof(fencing_generation) = 'integer'
           AND fencing_generation BETWEEN 1 AND 9223372036854775807),
    CHECK (typeof(created_at_us) = 'integer' AND created_at_us BETWEEN 1 AND 9223372036854775807),

    PRIMARY KEY (workspace_id, extraction_revision_id),
    UNIQUE (workspace_id, extraction_revision_id, document_id),
    UNIQUE (workspace_id, capture_id, extraction_sequence),
    UNIQUE (workspace_id, document_id, document_ordinal),
    UNIQUE (workspace_id, operation_key),
    FOREIGN KEY (workspace_id, capture_id, document_id)
        REFERENCES omnivia_document_captures (workspace_id, capture_id, document_id),
    FOREIGN KEY (workspace_id, profile_digest)
        REFERENCES omnivia_extraction_profiles (workspace_id, profile_digest),
    FOREIGN KEY (workspace_id, expected_base_extraction_id, document_id)
        REFERENCES omnivia_extraction_revisions (workspace_id, extraction_revision_id, document_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_extraction_results (
    workspace_id                TEXT    NOT NULL,
    extraction_revision_id      TEXT    NOT NULL,
    result_state                TEXT    NOT NULL,
    output_digest               TEXT,
    coverage_json               TEXT    NOT NULL,
    limitations_json            TEXT    NOT NULL,
    worker_build                TEXT    NOT NULL,
    publication_disposition     TEXT    NOT NULL,
    fencing_generation          INTEGER NOT NULL,
    completed_at_us             INTEGER NOT NULL,

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(extraction_revision_id) = 'text' AND length(extraction_revision_id) = 69
           AND substr(extraction_revision_id, 1, 5) = 'dext-'
           AND substr(extraction_revision_id, 6) NOT GLOB '*[^0-9a-f]*'),
    CHECK (result_state IN ('succeeded', 'partial', 'unsupported', 'failed')),
    CHECK ((result_state IN ('succeeded', 'partial')
            AND typeof(output_digest) = 'text' AND length(output_digest) = 71
            AND substr(output_digest, 1, 7) = 'sha256:'
            AND substr(output_digest, 8) NOT GLOB '*[^0-9a-f]*')
           OR (result_state IN ('unsupported', 'failed') AND output_digest IS NULL)),
    CHECK (typeof(coverage_json) = 'text' AND length(CAST(coverage_json AS BLOB)) BETWEEN 2 AND 65536),
    CHECK (typeof(limitations_json) = 'text' AND length(CAST(limitations_json AS BLOB)) BETWEEN 2 AND 65536),
    CHECK (typeof(worker_build) = 'text' AND length(worker_build) BETWEEN 1 AND 128
           AND instr(worker_build, char(0)) = 0),
    CHECK (publication_disposition IN
           ('promoted', 'superseded', 'historical_only', 'fenced_removal', 'fenced_retention', 'not_published')),
    CHECK ((result_state IN ('unsupported', 'failed')) = (publication_disposition = 'not_published')),
    CHECK (typeof(fencing_generation) = 'integer'
           AND fencing_generation BETWEEN 1 AND 9223372036854775807),
    CHECK (typeof(completed_at_us) = 'integer' AND completed_at_us BETWEEN 1 AND 9223372036854775807),

    PRIMARY KEY (workspace_id, extraction_revision_id),
    FOREIGN KEY (workspace_id, extraction_revision_id)
        REFERENCES omnivia_extraction_revisions (workspace_id, extraction_revision_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_document_heads (
    workspace_id                TEXT    NOT NULL,
    document_id                 TEXT    NOT NULL,
    source_state                TEXT    NOT NULL,
    source_removal_generation   INTEGER NOT NULL,
    current_capture_id          TEXT,
    capture_head_sequence       INTEGER NOT NULL,
    current_extraction_id       TEXT,
    extraction_head_ordinal     INTEGER NOT NULL,
    fencing_generation          INTEGER NOT NULL,
    updated_at_us               INTEGER NOT NULL,

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(document_id) = 'text' AND length(document_id) BETWEEN 1 AND 128
           AND document_id GLOB '[A-Za-z0-9]*'
           AND document_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(document_id, char(0)) = 0),
    CHECK (source_state IN ('active', 'removed')),
    CHECK (typeof(source_removal_generation) = 'integer'
           AND source_removal_generation BETWEEN 1 AND 9223372036854775807),
    CHECK ((current_capture_id IS NULL AND capture_head_sequence = 0)
           OR (typeof(current_capture_id) = 'text' AND length(current_capture_id) = 69
               AND substr(current_capture_id, 1, 5) = 'dcap-'
               AND substr(current_capture_id, 6) NOT GLOB '*[^0-9a-f]*'
               AND typeof(capture_head_sequence) = 'integer'
               AND capture_head_sequence BETWEEN 1 AND 9223372036854775807)),
    CHECK ((current_extraction_id IS NULL AND extraction_head_ordinal = 0)
           OR (typeof(current_extraction_id) = 'text' AND length(current_extraction_id) = 69
               AND substr(current_extraction_id, 1, 5) = 'dext-'
               AND substr(current_extraction_id, 6) NOT GLOB '*[^0-9a-f]*'
               AND typeof(extraction_head_ordinal) = 'integer'
               AND extraction_head_ordinal BETWEEN 1 AND 9223372036854775807)),
    CHECK (typeof(fencing_generation) = 'integer'
           AND fencing_generation BETWEEN 1 AND 9223372036854775807),
    CHECK (typeof(updated_at_us) = 'integer' AND updated_at_us BETWEEN 1 AND 9223372036854775807),

    PRIMARY KEY (workspace_id, document_id),
    FOREIGN KEY (workspace_id, document_id)
        REFERENCES omnivia_documents (workspace_id, document_id),
    FOREIGN KEY (workspace_id, current_capture_id, document_id)
        REFERENCES omnivia_document_captures (workspace_id, capture_id, document_id),
    FOREIGN KEY (workspace_id, current_extraction_id, document_id)
        REFERENCES omnivia_extraction_revisions (workspace_id, extraction_revision_id, document_id)
) WITHOUT ROWID;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_documents_insert
BEFORE INSERT ON omnivia_documents
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_documents')
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
              AND l.lifecycle IN ('acquiring', 'held', 'draining'));
    SELECT RAISE(ABORT, 'omnivia: a document must bind the open workspace')
    WHERE NEW.workspace_id IS NOT (
        SELECT workspace_id FROM omnivia_workspace_state WHERE singleton = 1);
    SELECT RAISE(ABORT, 'omnivia: a document must bind the current fencing generation')
    WHERE NEW.fencing_generation IS NOT (
        SELECT fencing_generation FROM omnivia_workspace_state WHERE singleton = 1);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_documents_update
BEFORE UPDATE ON omnivia_documents
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_documents is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_documents_delete
BEFORE DELETE ON omnivia_documents
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_documents is append-only; DELETE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_document_captures_insert
BEFORE INSERT ON omnivia_document_captures
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_document_captures')
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
              AND l.lifecycle IN ('acquiring', 'held', 'draining'));
    SELECT RAISE(ABORT, 'omnivia: a document capture must bind the open workspace')
    WHERE NEW.workspace_id IS NOT (
        SELECT workspace_id FROM omnivia_workspace_state WHERE singleton = 1);
    SELECT RAISE(ABORT, 'omnivia: a document capture must bind the current fencing generation')
    WHERE NEW.fencing_generation IS NOT (
        SELECT fencing_generation FROM omnivia_workspace_state WHERE singleton = 1);
    SELECT RAISE(ABORT, 'omnivia: a document capture must name document evidence of the same source digest')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_evidence_artifacts e
        WHERE e.workspace_id = NEW.workspace_id
          AND e.evidence_id = NEW.evidence_id
          AND e.source_kind = 'document'
          AND e.source_native_id = NEW.capture_id
          AND e.blob_content_digest = NEW.source_digest
          AND e.media_type = NEW.media_type);
    SELECT RAISE(ABORT, 'omnivia: a document capture needs an active document at its frozen removal generation')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_document_heads h
        WHERE h.workspace_id = NEW.workspace_id
          AND h.document_id = NEW.document_id
          AND h.source_state = 'active'
          AND h.source_removal_generation = NEW.source_removal_generation);
    SELECT RAISE(ABORT, 'omnivia: a document capture sequence must follow the last capture of the document by one')
    WHERE NEW.capture_sequence IS NOT 1 + (
        SELECT COALESCE(MAX(c.capture_sequence), 0) FROM omnivia_document_captures c
        WHERE c.workspace_id = NEW.workspace_id AND c.document_id = NEW.document_id);
    SELECT RAISE(ABORT, 'omnivia: a document capture is head-eligible exactly when its expected base is the current head')
    WHERE (NEW.disposition = 'head_eligible') IS NOT (
        NEW.expected_base_capture_id IS (
            SELECT h.current_capture_id FROM omnivia_document_heads h
            WHERE h.workspace_id = NEW.workspace_id AND h.document_id = NEW.document_id));
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_document_captures_update
BEFORE UPDATE ON omnivia_document_captures
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_document_captures is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_document_captures_delete
BEFORE DELETE ON omnivia_document_captures
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_document_captures is append-only; DELETE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_document_capture_retention_insert
BEFORE INSERT ON omnivia_document_capture_retention
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_document_capture_retention')
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
              AND l.lifecycle IN ('acquiring', 'held', 'draining'));
    SELECT RAISE(ABORT, 'omnivia: capture retention must bind the open workspace')
    WHERE NEW.workspace_id IS NOT (
        SELECT workspace_id FROM omnivia_workspace_state WHERE singleton = 1);
    SELECT RAISE(ABORT, 'omnivia: capture retention must bind the current fencing generation')
    WHERE NEW.fencing_generation IS NOT (
        SELECT fencing_generation FROM omnivia_workspace_state WHERE singleton = 1);
    SELECT RAISE(ABORT, 'omnivia: capture retention starts retained at generation one')
    WHERE NEW.retention_state IS NOT 'retained' OR NEW.capture_retention_generation IS NOT 1;
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_document_capture_retention_update
BEFORE UPDATE ON omnivia_document_capture_retention
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded UPDATE on omnivia_document_capture_retention')
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
              AND l.lifecycle IN ('acquiring', 'held', 'draining'));
    SELECT RAISE(ABORT, 'omnivia: capture retention may only move forward, one generation at a time')
    WHERE NEW.workspace_id IS NOT OLD.workspace_id
       OR NEW.capture_id IS NOT OLD.capture_id
       OR NEW.capture_retention_generation IS NOT OLD.capture_retention_generation + 1
       OR NOT ((OLD.retention_state = 'retained' AND NEW.retention_state IN ('expired', 'deleted'))
               OR (OLD.retention_state = 'expired' AND NEW.retention_state = 'deleted'));
    SELECT RAISE(ABORT, 'omnivia: capture retention must bind the current fencing generation')
    WHERE NEW.fencing_generation IS NOT (
        SELECT fencing_generation FROM omnivia_workspace_state WHERE singleton = 1);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_document_capture_retention_delete
BEFORE DELETE ON omnivia_document_capture_retention
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_document_capture_retention is never deleted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_extraction_profiles_insert
BEFORE INSERT ON omnivia_extraction_profiles
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_extraction_profiles')
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
              AND l.lifecycle IN ('acquiring', 'held', 'draining'));
    SELECT RAISE(ABORT, 'omnivia: an extraction profile must bind the open workspace')
    WHERE NEW.workspace_id IS NOT (
        SELECT workspace_id FROM omnivia_workspace_state WHERE singleton = 1);
    SELECT RAISE(ABORT, 'omnivia: an extraction profile must bind the current fencing generation')
    WHERE NEW.fencing_generation IS NOT (
        SELECT fencing_generation FROM omnivia_workspace_state WHERE singleton = 1);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_extraction_profiles_update
BEFORE UPDATE ON omnivia_extraction_profiles
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_extraction_profiles is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_extraction_profiles_delete
BEFORE DELETE ON omnivia_extraction_profiles
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_extraction_profiles is append-only; DELETE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_extraction_revisions_insert
BEFORE INSERT ON omnivia_extraction_revisions
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_extraction_revisions')
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
              AND l.lifecycle IN ('acquiring', 'held', 'draining'));
    SELECT RAISE(ABORT, 'omnivia: an extraction revision must bind the open workspace')
    WHERE NEW.workspace_id IS NOT (
        SELECT workspace_id FROM omnivia_workspace_state WHERE singleton = 1);
    SELECT RAISE(ABORT, 'omnivia: an extraction revision must bind the current fencing generation')
    WHERE NEW.fencing_generation IS NOT (
        SELECT fencing_generation FROM omnivia_workspace_state WHERE singleton = 1);
    SELECT RAISE(ABORT, 'omnivia: an extraction revision must bind the exact source digest of its capture')
    WHERE NEW.input_digest IS NOT (
        SELECT c.source_digest FROM omnivia_document_captures c
        WHERE c.workspace_id = NEW.workspace_id AND c.capture_id = NEW.capture_id);
    SELECT RAISE(ABORT, 'omnivia: an extraction revision needs a retained capture at its frozen retention generation')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_document_capture_retention r
        WHERE r.workspace_id = NEW.workspace_id
          AND r.capture_id = NEW.capture_id
          AND r.retention_state = 'retained'
          AND r.capture_retention_generation = NEW.capture_retention_generation);
    SELECT RAISE(ABORT, 'omnivia: an extraction revision needs an active document at its frozen removal generation')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_document_heads h
        WHERE h.workspace_id = NEW.workspace_id
          AND h.document_id = NEW.document_id
          AND h.source_state = 'active'
          AND h.source_removal_generation = NEW.source_removal_generation);
    SELECT RAISE(ABORT, 'omnivia: an extraction sequence must follow the last extraction of the capture by one')
    WHERE NEW.extraction_sequence IS NOT 1 + (
        SELECT COALESCE(MAX(v.extraction_sequence), 0) FROM omnivia_extraction_revisions v
        WHERE v.workspace_id = NEW.workspace_id AND v.capture_id = NEW.capture_id);
    SELECT RAISE(ABORT, 'omnivia: an extraction ordinal must follow the last extraction of the document by one')
    WHERE NEW.document_ordinal IS NOT 1 + (
        SELECT COALESCE(MAX(v.document_ordinal), 0) FROM omnivia_extraction_revisions v
        WHERE v.workspace_id = NEW.workspace_id AND v.document_id = NEW.document_id);
    SELECT RAISE(ABORT, 'omnivia: an extraction revision is head-eligible exactly when its expected base is the current head')
    WHERE NEW.head_eligible IS NOT (
        NEW.expected_base_extraction_id IS (
            SELECT h.current_extraction_id FROM omnivia_document_heads h
            WHERE h.workspace_id = NEW.workspace_id AND h.document_id = NEW.document_id));
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_extraction_revisions_update
BEFORE UPDATE ON omnivia_extraction_revisions
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_extraction_revisions is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_extraction_revisions_delete
BEFORE DELETE ON omnivia_extraction_revisions
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_extraction_revisions is append-only; DELETE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_extraction_results_insert
BEFORE INSERT ON omnivia_extraction_results
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_extraction_results')
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
              AND l.lifecycle IN ('acquiring', 'held', 'draining'));
    SELECT RAISE(ABORT, 'omnivia: an extraction result must bind the open workspace')
    WHERE NEW.workspace_id IS NOT (
        SELECT workspace_id FROM omnivia_workspace_state WHERE singleton = 1);
    SELECT RAISE(ABORT, 'omnivia: an extraction result must bind the current fencing generation')
    WHERE NEW.fencing_generation IS NOT (
        SELECT fencing_generation FROM omnivia_workspace_state WHERE singleton = 1);
    SELECT RAISE(ABORT, 'omnivia: a promoted or superseded result needs a head-eligible revision and a historical-only result needs an ineligible one')
    WHERE (NEW.publication_disposition IN ('promoted', 'superseded')
           AND (SELECT v.head_eligible FROM omnivia_extraction_revisions v
                WHERE v.workspace_id = NEW.workspace_id
                  AND v.extraction_revision_id = NEW.extraction_revision_id) IS NOT 1)
       OR (NEW.publication_disposition = 'historical_only'
           AND (SELECT v.head_eligible FROM omnivia_extraction_revisions v
                WHERE v.workspace_id = NEW.workspace_id
                  AND v.extraction_revision_id = NEW.extraction_revision_id) IS NOT 0);
    SELECT RAISE(ABORT, 'omnivia: a promoted result must outrank the current extraction head under unchanged generations')
    WHERE NEW.publication_disposition = 'promoted' AND NOT EXISTS (
        SELECT 1 FROM omnivia_extraction_revisions v
        JOIN omnivia_document_heads h
          ON h.workspace_id = v.workspace_id AND h.document_id = v.document_id
        JOIN omnivia_document_capture_retention r
          ON r.workspace_id = v.workspace_id AND r.capture_id = v.capture_id
        WHERE v.workspace_id = NEW.workspace_id
          AND v.extraction_revision_id = NEW.extraction_revision_id
          AND v.document_ordinal > h.extraction_head_ordinal
          AND h.source_state = 'active'
          AND h.source_removal_generation = v.source_removal_generation
          AND r.retention_state = 'retained'
          AND r.capture_retention_generation = v.capture_retention_generation);
    SELECT RAISE(ABORT, 'omnivia: a superseded result needs a newer extraction head under unchanged generations')
    WHERE NEW.publication_disposition = 'superseded' AND NOT EXISTS (
        SELECT 1 FROM omnivia_extraction_revisions v
        JOIN omnivia_document_heads h
          ON h.workspace_id = v.workspace_id AND h.document_id = v.document_id
        JOIN omnivia_document_capture_retention r
          ON r.workspace_id = v.workspace_id AND r.capture_id = v.capture_id
        WHERE v.workspace_id = NEW.workspace_id
          AND v.extraction_revision_id = NEW.extraction_revision_id
          AND v.document_ordinal < h.extraction_head_ordinal
          AND h.source_state = 'active'
          AND h.source_removal_generation = v.source_removal_generation
          AND r.retention_state = 'retained'
          AND r.capture_retention_generation = v.capture_retention_generation);
    SELECT RAISE(ABORT, 'omnivia: a removal-fenced result needs a changed source removal generation or a removed source')
    WHERE NEW.publication_disposition = 'fenced_removal' AND NOT EXISTS (
        SELECT 1 FROM omnivia_extraction_revisions v
        JOIN omnivia_document_heads h
          ON h.workspace_id = v.workspace_id AND h.document_id = v.document_id
        WHERE v.workspace_id = NEW.workspace_id
          AND v.extraction_revision_id = NEW.extraction_revision_id
          AND (h.source_state <> 'active' OR h.source_removal_generation <> v.source_removal_generation));
    SELECT RAISE(ABORT, 'omnivia: a retention-fenced result needs a changed retention generation or a capture that is no longer retained')
    WHERE NEW.publication_disposition = 'fenced_retention' AND NOT EXISTS (
        SELECT 1 FROM omnivia_extraction_revisions v
        JOIN omnivia_document_capture_retention r
          ON r.workspace_id = v.workspace_id AND r.capture_id = v.capture_id
        WHERE v.workspace_id = NEW.workspace_id
          AND v.extraction_revision_id = NEW.extraction_revision_id
          AND (r.retention_state <> 'retained' OR r.capture_retention_generation <> v.capture_retention_generation));
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_extraction_results_update
BEFORE UPDATE ON omnivia_extraction_results
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_extraction_results is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_extraction_results_delete
BEFORE DELETE ON omnivia_extraction_results
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_extraction_results is append-only; DELETE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_document_heads_insert
BEFORE INSERT ON omnivia_document_heads
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_document_heads')
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
              AND l.lifecycle IN ('acquiring', 'held', 'draining'));
    SELECT RAISE(ABORT, 'omnivia: a document head must bind the open workspace')
    WHERE NEW.workspace_id IS NOT (
        SELECT workspace_id FROM omnivia_workspace_state WHERE singleton = 1);
    SELECT RAISE(ABORT, 'omnivia: a document head must bind the current fencing generation')
    WHERE NEW.fencing_generation IS NOT (
        SELECT fencing_generation FROM omnivia_workspace_state WHERE singleton = 1);
    SELECT RAISE(ABORT, 'omnivia: a document head starts active at generation one with no capture and no extraction')
    WHERE NEW.source_state IS NOT 'active'
       OR NEW.source_removal_generation IS NOT 1
       OR NEW.current_capture_id IS NOT NULL
       OR NEW.current_extraction_id IS NOT NULL;
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_document_heads_update
BEFORE UPDATE ON omnivia_document_heads
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded UPDATE on omnivia_document_heads')
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
              AND l.lifecycle IN ('acquiring', 'held', 'draining'));
    SELECT RAISE(ABORT, 'omnivia: a document head update must keep its identity')
    WHERE NEW.workspace_id IS NOT OLD.workspace_id OR NEW.document_id IS NOT OLD.document_id;
    SELECT RAISE(ABORT, 'omnivia: a document head must bind the current fencing generation')
    WHERE NEW.fencing_generation IS NOT (
        SELECT fencing_generation FROM omnivia_workspace_state WHERE singleton = 1);
    SELECT RAISE(ABORT, 'omnivia: one document head write advances exactly one of the capture head, the extraction head and the source state')
    WHERE (NEW.current_capture_id IS NOT OLD.current_capture_id)
        + (NEW.current_extraction_id IS NOT OLD.current_extraction_id)
        + (NEW.source_state IS NOT OLD.source_state) <> 1
       OR (NEW.capture_head_sequence IS NOT OLD.capture_head_sequence)
          <> (NEW.current_capture_id IS NOT OLD.current_capture_id)
       OR (NEW.extraction_head_ordinal IS NOT OLD.extraction_head_ordinal)
          <> (NEW.current_extraction_id IS NOT OLD.current_extraction_id)
       OR (NEW.source_removal_generation IS NOT OLD.source_removal_generation)
          <> (NEW.source_state IS NOT OLD.source_state);
    SELECT RAISE(ABORT, 'omnivia: a source state change advances the removal generation by exactly one')
    WHERE NEW.source_state IS NOT OLD.source_state
      AND NEW.source_removal_generation IS NOT OLD.source_removal_generation + 1;
    SELECT RAISE(ABORT, 'omnivia: a capture head moves only to a retained head-eligible capture with a greater sequence')
    WHERE NEW.current_capture_id IS NOT OLD.current_capture_id AND NOT EXISTS (
        SELECT 1 FROM omnivia_document_captures c
        JOIN omnivia_document_capture_retention r
          ON r.workspace_id = c.workspace_id AND r.capture_id = c.capture_id
        WHERE c.workspace_id = NEW.workspace_id
          AND c.capture_id = NEW.current_capture_id
          AND c.document_id = NEW.document_id
          AND c.disposition = 'head_eligible'
          AND c.capture_sequence = NEW.capture_head_sequence
          AND NEW.capture_head_sequence > OLD.capture_head_sequence
          AND c.source_removal_generation = NEW.source_removal_generation
          AND NEW.source_state = 'active'
          AND r.retention_state = 'retained');
    SELECT RAISE(ABORT, 'omnivia: an extraction head moves only to a promoted extraction with a greater ordinal under unchanged generations')
    WHERE NEW.current_extraction_id IS NOT OLD.current_extraction_id AND NOT EXISTS (
        SELECT 1 FROM omnivia_extraction_revisions v
        JOIN omnivia_extraction_results x
          ON x.workspace_id = v.workspace_id AND x.extraction_revision_id = v.extraction_revision_id
        JOIN omnivia_document_capture_retention r
          ON r.workspace_id = v.workspace_id AND r.capture_id = v.capture_id
        WHERE v.workspace_id = NEW.workspace_id
          AND v.extraction_revision_id = NEW.current_extraction_id
          AND v.document_id = NEW.document_id
          AND v.head_eligible = 1
          AND v.document_ordinal = NEW.extraction_head_ordinal
          AND NEW.extraction_head_ordinal > OLD.extraction_head_ordinal
          AND x.result_state IN ('succeeded', 'partial')
          AND x.publication_disposition = 'promoted'
          AND v.source_removal_generation = NEW.source_removal_generation
          AND NEW.source_state = 'active'
          AND r.retention_state = 'retained'
          AND r.capture_retention_generation = v.capture_retention_generation);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_document_heads_delete
BEFORE DELETE ON omnivia_document_heads
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_document_heads is never deleted');
END;
