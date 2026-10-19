-- Document intake: logical documents, original bindings, immutable source captures, their outbox and immutable extraction revisions (migration 0069).
--
-- Additive only; allocation 0069 (predecessor 0068). Nine tables and their guard triggers.
--
--   omnivia_documents                      one logical document; immutable; its identity is Core-owned
--   omnivia_document_original_bindings     one opaque native owner/device/access route to a document's original
--   omnivia_document_captures              one immutable captured source revision of a document
--   omnivia_document_capture_retention     one capture's retention state and its generation; guarded advances only
--   omnivia_document_capture_outbox        one capture-processing obligation per capture; settles forward only
--   omnivia_extraction_profiles            one content-addressed ExtractionProfileV1; immutable
--   omnivia_extraction_revisions           one admitted extraction over one capture; immutable
--   omnivia_extraction_results             the one terminal result of a revision; immutable
--   omnivia_extracted_segments             immutable structurally located output of one revision
--   omnivia_document_extraction_projection current promoted retrieval rows of one document
--   omnivia_document_heads                 one document's current capture, current extraction and source state
--
-- Bindings. A binding is an opaque `owner_ref` (the native owner, device and access identity), unique in the
-- workspace and bound to exactly one document, a route kind (`direct_file`, or `root_member` with an opaque root
-- and member), a binding generation and a connection state. It never holds a path, bookmark, credential or
-- transferable capability. A binding may be recorded as disconnected before the document has any capture, which is
-- how a disconnect that arrives first is remembered. Disconnecting keeps the generation; reconnecting a disconnected
-- binding advances it by one; nothing else moves it. One owner_ref is one route, so disconnecting it revokes no
-- other owner or device.
--
-- Captures. A capture names an existing evidence artifact and the exact digest and length of the immutable
-- binary source that evidence holds. Two captures may name the same blob, but each names its own evidence, so
-- a shared blob never merges two captures or two documents. A capture is numbered per document in admission
-- order. It records its intent kind, the capture the caller expected to be current (null when it expected none), an
-- association (snapshot only, or the binding generation it was captured under) and a stable disposition reason. It
-- is `head_eligible` exactly when the document source is active, its binding generation is still the connected
-- current one and its creation or base precondition still held when it was admitted; any other valid capture is
-- `historical_only` with the first reason that applies, in this order: removed source, superseded generation,
-- disconnected generation, creation conflict (a new-document capture of a document that already has any capture),
-- stale base (any other intent whose expected base, null included, is not the current capture). A capture row is never edited, so neither a late
-- callback nor a restored source can make a historical capture current.
--
-- Heads. The capture head and the extraction head are separate pairs of columns in one row, and each moves only
-- by its own guarded write: a capture head moves only to a head-eligible, retained capture with a greater
-- sequence whose base is still the head and whose binding is still the connected current generation, an
-- extraction head moves only to a promoted, head-eligible extraction with a greater ordinal whose frozen
-- generations are still current, and one write moves exactly one of the capture head, the extraction
-- head and the source state. So an extraction can never move a capture head, and a late older write can never
-- replace a newer one.
--
-- Generations. `source_removal_generation` (per document, in the head row) and `capture_retention_generation`
-- (per capture, in the retention row) only ever advance by one. A revision freezes both when it is admitted, and
-- its promotion is refused unless both are unchanged and the capture is still retained.
--
-- Outbox. Admitting a capture writes exactly one pending `capture_processing` obligation, keyed by workspace,
-- capture and kind, in the same transaction. It is numbered in admission order, settles once from pending to
-- settled and is never deleted. Nothing here dispatches it.
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

CREATE TABLE IF NOT EXISTS omnivia_document_original_bindings (
    workspace_id                TEXT    NOT NULL,
    owner_ref                   TEXT    NOT NULL,
    document_id                 TEXT    NOT NULL,
    route_kind                  TEXT    NOT NULL,
    root_ref                    TEXT,
    member_ref                  TEXT,
    binding_generation          INTEGER NOT NULL,
    connection_state            TEXT    NOT NULL,
    fencing_generation          INTEGER NOT NULL,
    created_at_us               INTEGER NOT NULL,
    changed_at_us               INTEGER NOT NULL,

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(owner_ref) = 'text' AND length(owner_ref) BETWEEN 1 AND 128
           AND owner_ref GLOB '[A-Za-z0-9]*'
           AND owner_ref NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(owner_ref, char(0)) = 0),
    CHECK (typeof(document_id) = 'text' AND length(document_id) BETWEEN 1 AND 128
           AND document_id GLOB '[A-Za-z0-9]*'
           AND document_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(document_id, char(0)) = 0),
    CHECK (route_kind IN ('direct_file', 'root_member')),
    CHECK ((route_kind = 'direct_file' AND root_ref IS NULL AND member_ref IS NULL)
           OR (route_kind = 'root_member'
               AND (typeof(root_ref) = 'text' AND length(root_ref) BETWEEN 1 AND 128
           AND root_ref GLOB '[A-Za-z0-9]*'
           AND root_ref NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(root_ref, char(0)) = 0)
               AND (typeof(member_ref) = 'text' AND length(member_ref) BETWEEN 1 AND 128
           AND member_ref GLOB '[A-Za-z0-9]*'
           AND member_ref NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(member_ref, char(0)) = 0))),
    CHECK (typeof(binding_generation) = 'integer' AND binding_generation BETWEEN 1 AND 9223372036854775807),
    CHECK (connection_state IN ('connected', 'disconnected')),
    CHECK (typeof(fencing_generation) = 'integer'
           AND fencing_generation BETWEEN 1 AND 9223372036854775807),
    CHECK (typeof(created_at_us) = 'integer' AND created_at_us BETWEEN 1 AND 9223372036854775807),
    CHECK (typeof(changed_at_us) = 'integer' AND changed_at_us BETWEEN 1 AND 9223372036854775807),

    PRIMARY KEY (workspace_id, owner_ref),
    UNIQUE (workspace_id, owner_ref, document_id),
    FOREIGN KEY (workspace_id, document_id)
        REFERENCES omnivia_documents (workspace_id, document_id)
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
    requested_source_identity   TEXT    NOT NULL,
    target_binding_revision     TEXT    NOT NULL,
    processing_profile_ref      TEXT    NOT NULL,
    processing_profile_revision TEXT    NOT NULL,
    consent_json                TEXT,
    bulk_origin_json            TEXT,
    display_name                TEXT,
    expected_base_capture_id    TEXT,
    intent_kind                 TEXT    NOT NULL,
    association_kind            TEXT    NOT NULL,
    binding_owner_ref           TEXT,
    binding_generation          INTEGER,
    binding_route_kind          TEXT,
    binding_root_ref            TEXT,
    binding_member_ref          TEXT,
    disposition                 TEXT    NOT NULL,
    disposition_reason          TEXT    NOT NULL,
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
    CHECK (typeof(requested_source_identity) = 'text' AND length(requested_source_identity) BETWEEN 1 AND 128
           AND requested_source_identity NOT GLOB '*[^A-Za-z0-9._:-]*' AND instr(requested_source_identity, char(0)) = 0),
    CHECK (typeof(target_binding_revision) = 'text' AND length(target_binding_revision) BETWEEN 1 AND 128
           AND target_binding_revision NOT GLOB '*[^A-Za-z0-9._:-]*' AND instr(target_binding_revision, char(0)) = 0),
    CHECK (typeof(processing_profile_ref) = 'text' AND length(processing_profile_ref) BETWEEN 1 AND 128
           AND processing_profile_ref NOT GLOB '*[^A-Za-z0-9._:-]*' AND instr(processing_profile_ref, char(0)) = 0),
    CHECK (typeof(processing_profile_revision) = 'text' AND length(processing_profile_revision) BETWEEN 1 AND 128
           AND processing_profile_revision NOT GLOB '*[^A-Za-z0-9._:-]*' AND instr(processing_profile_revision, char(0)) = 0),
    CHECK (consent_json IS NULL OR (typeof(consent_json) = 'text' AND json_valid(consent_json))),
    CHECK (bulk_origin_json IS NULL OR (typeof(bulk_origin_json) = 'text' AND json_valid(bulk_origin_json))),
    CHECK (display_name IS NULL OR (typeof(display_name) = 'text' AND length(display_name) BETWEEN 1 AND 1024
           AND instr(display_name, char(0)) = 0)),
    CHECK (expected_base_capture_id IS NULL
           OR (typeof(expected_base_capture_id) = 'text' AND length(expected_base_capture_id) = 69
               AND substr(expected_base_capture_id, 1, 5) = 'dcap-'
               AND substr(expected_base_capture_id, 6) NOT GLOB '*[^0-9a-f]*'
               AND expected_base_capture_id <> capture_id)),
    CHECK (disposition IN ('head_eligible', 'historical_only')),
    CHECK (intent_kind IN ('new_capture', 'refresh', 'automatic_capture', 'folder_capture')),
    CHECK (intent_kind <> 'new_capture' OR expected_base_capture_id IS NULL),
    CHECK (association_kind IN ('snapshot_only', 'active_binding', 'historical_binding')),
    CHECK ((association_kind = 'snapshot_only'
            AND binding_owner_ref IS NULL AND binding_generation IS NULL AND binding_route_kind IS NULL
            AND binding_root_ref IS NULL AND binding_member_ref IS NULL)
           OR (association_kind IN ('active_binding', 'historical_binding')
               AND typeof(binding_owner_ref) = 'text' AND length(binding_owner_ref) BETWEEN 1 AND 128
               AND binding_owner_ref GLOB '[A-Za-z0-9]*'
               AND binding_owner_ref NOT GLOB '*[^A-Za-z0-9._:-]*'
               AND instr(binding_owner_ref, char(0)) = 0
               AND typeof(binding_generation) = 'integer'
               AND binding_generation BETWEEN 1 AND 9223372036854775807
               AND ((binding_route_kind = 'direct_file' AND binding_root_ref IS NULL AND binding_member_ref IS NULL)
                    OR (binding_route_kind = 'root_member'
                        AND (typeof(binding_root_ref) = 'text' AND length(binding_root_ref) BETWEEN 1 AND 128
               AND binding_root_ref GLOB '[A-Za-z0-9]*'
               AND binding_root_ref NOT GLOB '*[^A-Za-z0-9._:-]*'
               AND instr(binding_root_ref, char(0)) = 0)
                        AND (typeof(binding_member_ref) = 'text' AND length(binding_member_ref) BETWEEN 1 AND 128
               AND binding_member_ref GLOB '[A-Za-z0-9]*'
               AND binding_member_ref NOT GLOB '*[^A-Za-z0-9._:-]*'
               AND instr(binding_member_ref, char(0)) = 0))))),
    CHECK (disposition_reason IN
           ('eligible', 'removed_source', 'superseded_generation', 'disconnected_generation',
            'creation_conflict', 'stale_base')),
    CHECK ((disposition = 'head_eligible' AND disposition_reason = 'eligible'
            AND association_kind <> 'historical_binding')
           OR (disposition = 'historical_only' AND disposition_reason <> 'eligible')),
    CHECK (disposition_reason <> 'creation_conflict' OR (intent_kind = 'new_capture' AND capture_sequence > 1)),
    CHECK (disposition_reason <> 'stale_base' OR intent_kind <> 'new_capture'),
    CHECK (disposition_reason NOT IN ('superseded_generation', 'disconnected_generation')
           OR association_kind = 'historical_binding'),
    CHECK (association_kind <> 'historical_binding'
           OR disposition_reason IN ('removed_source', 'superseded_generation', 'disconnected_generation')),
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
    FOREIGN KEY (workspace_id, binding_owner_ref, document_id)
        REFERENCES omnivia_document_original_bindings (workspace_id, owner_ref, document_id),
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

CREATE TABLE IF NOT EXISTS omnivia_document_capture_outbox (
    workspace_id                TEXT    NOT NULL,
    capture_id                  TEXT    NOT NULL,
    kind                        TEXT    NOT NULL,
    document_id                 TEXT    NOT NULL,
    outbox_sequence             INTEGER NOT NULL,
    outbox_state                TEXT    NOT NULL,
    fencing_generation          INTEGER NOT NULL,
    created_at_us               INTEGER NOT NULL,
    settled_at_us               INTEGER,

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(capture_id) = 'text' AND length(capture_id) = 69
           AND substr(capture_id, 1, 5) = 'dcap-'
           AND substr(capture_id, 6) NOT GLOB '*[^0-9a-f]*'),
    CHECK (kind IN ('capture_processing')),
    CHECK (typeof(document_id) = 'text' AND length(document_id) BETWEEN 1 AND 128
           AND document_id GLOB '[A-Za-z0-9]*'
           AND document_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(document_id, char(0)) = 0),
    CHECK (typeof(outbox_sequence) = 'integer' AND outbox_sequence BETWEEN 1 AND 9223372036854775807),
    CHECK (outbox_state IN ('pending', 'settled')),
    CHECK (typeof(fencing_generation) = 'integer'
           AND fencing_generation BETWEEN 1 AND 9223372036854775807),
    CHECK (typeof(created_at_us) = 'integer' AND created_at_us BETWEEN 1 AND 9223372036854775807),
    CHECK ((outbox_state = 'pending' AND settled_at_us IS NULL)
           OR (outbox_state = 'settled' AND typeof(settled_at_us) = 'integer'
               AND settled_at_us BETWEEN 1 AND 9223372036854775807)),

    PRIMARY KEY (workspace_id, capture_id, kind),
    UNIQUE (workspace_id, outbox_sequence),
    FOREIGN KEY (workspace_id, capture_id, document_id)
        REFERENCES omnivia_document_captures (workspace_id, capture_id, document_id)
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
    segments_digest             TEXT    NOT NULL,
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
    CHECK (typeof(segments_digest) = 'text' AND length(segments_digest) = 71
           AND substr(segments_digest, 1, 7) = 'sha256:'
           AND substr(segments_digest, 8) NOT GLOB '*[^0-9a-f]*'),
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

CREATE TABLE IF NOT EXISTS omnivia_extracted_segments (
    workspace_id                TEXT    NOT NULL,
    extraction_revision_id      TEXT    NOT NULL,
    segment_ordinal             INTEGER NOT NULL,
    semantic_role               TEXT    NOT NULL,
    text                        TEXT,
    structured_value_json       TEXT,
    source_locator_json         TEXT    NOT NULL,
    extraction_method           TEXT    NOT NULL,
    visibility                  TEXT    NOT NULL,
    revision_state              TEXT    NOT NULL,
    language                    TEXT,
    confidence                  REAL,
    fencing_generation          INTEGER NOT NULL,

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(extraction_revision_id) = 'text' AND length(extraction_revision_id) = 69
           AND substr(extraction_revision_id, 1, 5) = 'dext-'
           AND substr(extraction_revision_id, 6) NOT GLOB '*[^0-9a-f]*'),
    CHECK (typeof(segment_ordinal) = 'integer' AND segment_ordinal BETWEEN 1 AND 100000),
    CHECK (semantic_role IN
           ('body', 'heading', 'list_item', 'table_cell', 'formula', 'saved_value', 'chart',
            'image_alt', 'equation', 'comment', 'note', 'hidden_content', 'deleted_text',
            'field_instruction', 'object_inventory')),
    CHECK ((typeof(text) = 'text' AND length(CAST(text AS BLOB)) BETWEEN 1 AND 131072
            AND structured_value_json IS NULL)
           OR (text IS NULL AND typeof(structured_value_json) = 'text'
               AND length(CAST(structured_value_json AS BLOB)) BETWEEN 1 AND 131072)),
    CHECK (typeof(source_locator_json) = 'text'
           AND length(CAST(source_locator_json AS BLOB)) BETWEEN 2 AND 131072),
    CHECK (extraction_method IN ('direct', 'ocr', 'derived', 'inventory')),
    CHECK (visibility IN ('visible', 'hidden', 'annotation')),
    CHECK (revision_state IN ('current', 'inserted', 'deleted', 'none')),
    CHECK (language IS NULL OR (typeof(language) = 'text' AND length(language) BETWEEN 1 AND 128
           AND language GLOB '[A-Za-z0-9]*'
           AND language NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(language, char(0)) = 0)),
    CHECK (confidence IS NULL OR (typeof(confidence) IN ('integer', 'real')
           AND confidence BETWEEN 0.0 AND 1.0)),
    CHECK (typeof(fencing_generation) = 'integer'
           AND fencing_generation BETWEEN 1 AND 9223372036854775807),

    PRIMARY KEY (workspace_id, extraction_revision_id, segment_ordinal),
    FOREIGN KEY (workspace_id, extraction_revision_id)
        REFERENCES omnivia_extraction_results (workspace_id, extraction_revision_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_document_extraction_projection (
    workspace_id                TEXT    NOT NULL,
    document_id                 TEXT    NOT NULL,
    extraction_revision_id      TEXT    NOT NULL,
    capture_id                  TEXT    NOT NULL,
    profile_digest              TEXT    NOT NULL,
    segment_ordinal             INTEGER NOT NULL,
    semantic_role               TEXT    NOT NULL,
    text                        TEXT,
    structured_value_json       TEXT,
    source_locator_json         TEXT    NOT NULL,
    extraction_method           TEXT    NOT NULL,
    visibility                  TEXT    NOT NULL,
    revision_state              TEXT    NOT NULL,
    language                    TEXT,
    confidence                  REAL,
    fencing_generation          INTEGER NOT NULL,
    published_at_us             INTEGER NOT NULL,

    CHECK (typeof(document_id) = 'text' AND length(document_id) BETWEEN 1 AND 128
           AND document_id GLOB '[A-Za-z0-9]*'
           AND document_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(document_id, char(0)) = 0),
    CHECK (typeof(segment_ordinal) = 'integer' AND segment_ordinal BETWEEN 1 AND 100000),
    CHECK (semantic_role IN
           ('body', 'heading', 'list_item', 'table_cell', 'formula', 'saved_value', 'chart',
            'image_alt', 'equation', 'comment', 'note', 'hidden_content', 'deleted_text',
            'field_instruction', 'object_inventory')),
    CHECK ((typeof(text) = 'text' AND structured_value_json IS NULL)
           OR (text IS NULL AND typeof(structured_value_json) = 'text')),
    CHECK (extraction_method IN ('direct', 'ocr', 'derived', 'inventory')),
    CHECK (visibility IN ('visible', 'hidden', 'annotation')),
    CHECK (revision_state IN ('current', 'inserted', 'deleted', 'none')),
    CHECK (confidence IS NULL OR confidence BETWEEN 0.0 AND 1.0),
    CHECK (typeof(fencing_generation) = 'integer'
           AND fencing_generation BETWEEN 1 AND 9223372036854775807),
    CHECK (typeof(published_at_us) = 'integer' AND published_at_us BETWEEN 1 AND 9223372036854775807),

    PRIMARY KEY (workspace_id, document_id, segment_ordinal),
    FOREIGN KEY (workspace_id, extraction_revision_id, segment_ordinal)
        REFERENCES omnivia_extracted_segments (workspace_id, extraction_revision_id, segment_ordinal),
    FOREIGN KEY (workspace_id, extraction_revision_id, document_id)
        REFERENCES omnivia_extraction_revisions (workspace_id, extraction_revision_id, document_id),
    FOREIGN KEY (workspace_id, capture_id, document_id)
        REFERENCES omnivia_document_captures (workspace_id, capture_id, document_id),
    FOREIGN KEY (workspace_id, profile_digest)
        REFERENCES omnivia_extraction_profiles (workspace_id, profile_digest)
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

CREATE TRIGGER IF NOT EXISTS omnivia_guard_document_original_bindings_insert
BEFORE INSERT ON omnivia_document_original_bindings
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_document_original_bindings')
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
    SELECT RAISE(ABORT, 'omnivia: an original binding must bind the open workspace')
    WHERE NEW.workspace_id IS NOT (
        SELECT workspace_id FROM omnivia_workspace_state WHERE singleton = 1);
    SELECT RAISE(ABORT, 'omnivia: an original binding must bind the current fencing generation')
    WHERE NEW.fencing_generation IS NOT (
        SELECT fencing_generation FROM omnivia_workspace_state WHERE singleton = 1);
    SELECT RAISE(ABORT, 'omnivia: an original binding starts at generation one')
    WHERE NEW.binding_generation IS NOT 1;
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_document_original_bindings_update
BEFORE UPDATE ON omnivia_document_original_bindings
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded UPDATE on omnivia_document_original_bindings')
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
    SELECT RAISE(ABORT, 'omnivia: an original binding keeps its identity and route')
    WHERE NEW.workspace_id IS NOT OLD.workspace_id
       OR NEW.owner_ref IS NOT OLD.owner_ref
       OR NEW.document_id IS NOT OLD.document_id
       OR NEW.route_kind IS NOT OLD.route_kind
       OR NEW.root_ref IS NOT OLD.root_ref
       OR NEW.member_ref IS NOT OLD.member_ref
       OR NEW.created_at_us IS NOT OLD.created_at_us;
    SELECT RAISE(ABORT, 'omnivia: an original binding disconnects at its generation or reconnects one generation later')
    WHERE NOT ((OLD.connection_state = 'connected' AND NEW.connection_state = 'disconnected'
                AND NEW.binding_generation IS OLD.binding_generation)
               OR (OLD.connection_state = 'disconnected' AND NEW.connection_state = 'connected'
                   AND NEW.binding_generation IS OLD.binding_generation + 1));
    SELECT RAISE(ABORT, 'omnivia: an original binding must bind the current fencing generation')
    WHERE NEW.fencing_generation IS NOT (
        SELECT fencing_generation FROM omnivia_workspace_state WHERE singleton = 1);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_document_original_bindings_delete
BEFORE DELETE ON omnivia_document_original_bindings
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_document_original_bindings is never deleted');
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
    SELECT RAISE(ABORT, 'omnivia: a document capture needs a document head at its frozen removal generation')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_document_heads h
        WHERE h.workspace_id = NEW.workspace_id
          AND h.document_id = NEW.document_id
          AND h.source_removal_generation = NEW.source_removal_generation);
    SELECT RAISE(ABORT, 'omnivia: a document capture sequence must follow the last capture of the document by one')
    WHERE NEW.capture_sequence IS NOT 1 + (
        SELECT COALESCE(MAX(c.capture_sequence), 0) FROM omnivia_document_captures c
        WHERE c.workspace_id = NEW.workspace_id AND c.document_id = NEW.document_id);
    SELECT RAISE(ABORT, 'omnivia: a document capture binding must be a known binding of its document, at or before its current generation, with the same route lineage')
    WHERE NEW.binding_owner_ref IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM omnivia_document_original_bindings b
        WHERE b.workspace_id = NEW.workspace_id
          AND b.owner_ref = NEW.binding_owner_ref
          AND b.document_id = NEW.document_id
          AND NEW.binding_generation <= b.binding_generation
          AND b.route_kind IS NEW.binding_route_kind
          AND b.root_ref IS NEW.binding_root_ref
          AND b.member_ref IS NEW.binding_member_ref);
    SELECT RAISE(ABORT, 'omnivia: a document capture association must be the one its binding implies')
    WHERE NEW.association_kind IS NOT (
        SELECT CASE
                WHEN NEW.binding_owner_ref IS NULL THEN 'snapshot_only'
                WHEN b.connection_state = 'connected' AND b.binding_generation = NEW.binding_generation
                    THEN 'active_binding'
                ELSE 'historical_binding' END
        FROM omnivia_document_heads h
        LEFT JOIN omnivia_document_original_bindings b
          ON b.workspace_id = h.workspace_id AND b.document_id = h.document_id
         AND b.owner_ref = NEW.binding_owner_ref
        WHERE h.workspace_id = NEW.workspace_id AND h.document_id = NEW.document_id);
    SELECT RAISE(ABORT, 'omnivia: a document capture disposition reason must be the one its source state, binding, base and creation precondition imply')
    WHERE NEW.disposition_reason IS NOT (
        SELECT CASE
                WHEN h.source_state <> 'active' THEN 'removed_source'
                WHEN b.owner_ref IS NOT NULL AND NEW.binding_generation < b.binding_generation
                    THEN 'superseded_generation'
                WHEN b.owner_ref IS NOT NULL AND b.connection_state <> 'connected'
                    THEN 'disconnected_generation'
                WHEN NEW.intent_kind = 'new_capture' AND NEW.capture_sequence > 1
                    THEN 'creation_conflict'
                WHEN NEW.intent_kind <> 'new_capture'
                     AND NEW.expected_base_capture_id IS NOT h.current_capture_id
                    THEN 'stale_base'
                ELSE 'eligible' END
        FROM omnivia_document_heads h
        LEFT JOIN omnivia_document_original_bindings b
          ON b.workspace_id = h.workspace_id AND b.document_id = h.document_id
         AND b.owner_ref = NEW.binding_owner_ref
        WHERE h.workspace_id = NEW.workspace_id AND h.document_id = NEW.document_id);
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

CREATE TRIGGER IF NOT EXISTS omnivia_guard_document_capture_outbox_insert
BEFORE INSERT ON omnivia_document_capture_outbox
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_document_capture_outbox')
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
    SELECT RAISE(ABORT, 'omnivia: a capture outbox row must bind the open workspace')
    WHERE NEW.workspace_id IS NOT (
        SELECT workspace_id FROM omnivia_workspace_state WHERE singleton = 1);
    SELECT RAISE(ABORT, 'omnivia: a capture outbox row must bind the current fencing generation')
    WHERE NEW.fencing_generation IS NOT (
        SELECT fencing_generation FROM omnivia_workspace_state WHERE singleton = 1);
    SELECT RAISE(ABORT, 'omnivia: a capture outbox row starts pending')
    WHERE NEW.outbox_state IS NOT 'pending';
    SELECT RAISE(ABORT, 'omnivia: a capture outbox sequence must follow the last outbox row of the workspace by one')
    WHERE NEW.outbox_sequence IS NOT 1 + (
        SELECT COALESCE(MAX(o.outbox_sequence), 0) FROM omnivia_document_capture_outbox o
        WHERE o.workspace_id = NEW.workspace_id);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_document_capture_outbox_update
BEFORE UPDATE ON omnivia_document_capture_outbox
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded UPDATE on omnivia_document_capture_outbox')
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
    SELECT RAISE(ABORT, 'omnivia: a capture outbox row only settles, from pending, and keeps everything else')
    WHERE NEW.workspace_id IS NOT OLD.workspace_id
       OR NEW.capture_id IS NOT OLD.capture_id
       OR NEW.kind IS NOT OLD.kind
       OR NEW.document_id IS NOT OLD.document_id
       OR NEW.outbox_sequence IS NOT OLD.outbox_sequence
       OR NEW.created_at_us IS NOT OLD.created_at_us
       OR OLD.outbox_state IS NOT 'pending'
       OR NEW.outbox_state IS NOT 'settled';
    SELECT RAISE(ABORT, 'omnivia: a capture outbox row must bind the current fencing generation')
    WHERE NEW.fencing_generation IS NOT (
        SELECT fencing_generation FROM omnivia_workspace_state WHERE singleton = 1);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_document_capture_outbox_delete
BEFORE DELETE ON omnivia_document_capture_outbox
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_document_capture_outbox is never deleted');
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

CREATE TRIGGER IF NOT EXISTS omnivia_guard_extracted_segments_insert
BEFORE INSERT ON omnivia_extracted_segments
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_extracted_segments')
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
    SELECT RAISE(ABORT, 'omnivia: an extracted segment must bind the open workspace and current fence')
    WHERE NEW.workspace_id IS NOT (
              SELECT workspace_id FROM omnivia_workspace_state WHERE singleton = 1)
       OR NEW.fencing_generation IS NOT (
              SELECT fencing_generation FROM omnivia_workspace_state WHERE singleton = 1);
    SELECT RAISE(ABORT, 'omnivia: extracted segment ordinals are contiguous within a revision')
    WHERE NEW.segment_ordinal IS NOT 1 + (
        SELECT COALESCE(MAX(s.segment_ordinal), 0) FROM omnivia_extracted_segments s
        WHERE s.workspace_id = NEW.workspace_id
          AND s.extraction_revision_id = NEW.extraction_revision_id);
    SELECT RAISE(ABORT, 'omnivia: only a publishable extraction result can carry segments')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_extraction_results r
        WHERE r.workspace_id = NEW.workspace_id
          AND r.extraction_revision_id = NEW.extraction_revision_id
          AND r.result_state IN ('succeeded', 'partial'));
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_extracted_segments_update
BEFORE UPDATE ON omnivia_extracted_segments
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_extracted_segments is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_extracted_segments_delete
BEFORE DELETE ON omnivia_extracted_segments
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_extracted_segments is append-only; DELETE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_document_extraction_projection_insert
BEFORE INSERT ON omnivia_document_extraction_projection
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_document_extraction_projection')
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
    SELECT RAISE(ABORT, 'omnivia: a document extraction projection must bind the open workspace and current fence')
    WHERE NEW.workspace_id IS NOT (
              SELECT workspace_id FROM omnivia_workspace_state WHERE singleton = 1)
       OR NEW.fencing_generation IS NOT (
              SELECT fencing_generation FROM omnivia_workspace_state WHERE singleton = 1);
    SELECT RAISE(ABORT, 'omnivia: a projected segment must exactly reproduce its immutable segment and promoted revision')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_extracted_segments s
        JOIN omnivia_extraction_revisions v
          ON v.workspace_id = s.workspace_id
         AND v.extraction_revision_id = s.extraction_revision_id
        JOIN omnivia_extraction_results r
          ON r.workspace_id = v.workspace_id
         AND r.extraction_revision_id = v.extraction_revision_id
        WHERE s.workspace_id = NEW.workspace_id
          AND s.extraction_revision_id = NEW.extraction_revision_id
          AND s.segment_ordinal = NEW.segment_ordinal
          AND v.document_id = NEW.document_id
          AND v.capture_id = NEW.capture_id
          AND v.profile_digest = NEW.profile_digest
          AND r.publication_disposition = 'promoted'
          AND s.semantic_role IS NEW.semantic_role
          AND s.text IS NEW.text
          AND s.structured_value_json IS NEW.structured_value_json
          AND s.source_locator_json IS NEW.source_locator_json
          AND s.extraction_method IS NEW.extraction_method
          AND s.visibility IS NEW.visibility
          AND s.revision_state IS NEW.revision_state
          AND s.language IS NEW.language
          AND s.confidence IS NEW.confidence);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_document_extraction_projection_update
BEFORE UPDATE ON omnivia_document_extraction_projection
BEGIN
    SELECT RAISE(ABORT, 'omnivia: a document extraction projection is replaced, never updated');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_document_extraction_projection_delete
BEFORE DELETE ON omnivia_document_extraction_projection
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded DELETE on omnivia_document_extraction_projection')
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
    SELECT RAISE(ABORT, 'omnivia: a capture head moves only to a retained head-eligible capture with a greater sequence, its base still the head and its binding still the connected current generation')
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
          AND c.expected_base_capture_id IS OLD.current_capture_id
          AND (c.binding_owner_ref IS NULL OR EXISTS (
                SELECT 1 FROM omnivia_document_original_bindings b
                WHERE b.workspace_id = c.workspace_id
                  AND b.owner_ref = c.binding_owner_ref
                  AND b.document_id = c.document_id
                  AND b.binding_generation = c.binding_generation
                  AND b.connection_state = 'connected'))
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
