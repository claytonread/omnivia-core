-- Blob objects, staged import sources and L0 evidence (V06-1 M2).
--
-- The second forward-only durable slice for the application seam, and like
-- `0007_application_audit_and_idempotency.sql` it adds storage facts only. Nine
-- append-preserved tables, and nothing that acts on them: no repository, no
-- filesystem blob store, no importer, handler, dispatcher, ACL evaluator, grant
-- resolver, garbage collector, governed record, job expansion or projection. What
-- a later vertical does with these rows is separately accepted work.
--
--   omnivia_blob_objects                 one verified immutable content identity
--   omnivia_blob_integrity_events        what an integrity or inventory pass saw
--   omnivia_staged_sources               one already-staged import source
--   omnivia_evidence_artifacts           one captured L0 evidence artifact
--   omnivia_evidence_permission_labels   raw label facts, as policy *input*
--   omnivia_evidence_provenance_events   who did what to one artifact, in order
--   omnivia_evidence_event_references    what one such event pointed at
--   omnivia_normalized_source_records    typed records parsed out of evidence
--   omnivia_normalized_source_spans      where in the evidence each one came from
--
-- Immutability is enforced twice over, exactly as 0007 established. Every UPDATE
-- and DELETE aborts unconditionally -- for the current fenced owner too, because
-- "the owner may rewrite history" is the one property an evidence trail cannot
-- have -- and every INSERT must satisfy the complete connection-authority,
-- mutation-guard, workspace-state and lease predicate plus the singleton workspace
-- binding. Corrections, tombstones, integrity outcomes and status changes are new
-- rows. There is no current-state flag anywhere below, because a flag is a thing
-- that gets rewritten.
--
-- Order within a stream is the sequence, not the clock. Every causally ordered
-- append stream carries a positive parent-scoped sequence that must strictly
-- advance: two opposite facts recorded in the same microsecond are still ordered,
-- and no reader has to break a tie on a timestamp that cannot break it. Duplicates
-- are refused by the stream's UNIQUE index and a backwards or non-positive value by
-- its INSERT trigger, so the two halves of "monotonic" are one statement about one
-- index rather than two rules that can disagree.
--
-- Timestamps are signed 64-bit UTC microseconds in `*_at_us`, carried without
-- narrowing by SQLite's INTEGER storage class; `typeof(...) = 'integer'` is what
-- makes that exact, because a fractional microsecond is then refused rather than
-- silently rounded by column affinity. Times the system itself stamps are positive.
-- `event_at_us` and `observed_at_us` are the two deliberate exceptions and carry the
-- full signed domain: they describe when something happened in the world, and a
-- document or message predating 1970 is an ordinary thing to capture evidence of.
--
-- Two digest domains meet here and are deliberately not collapsed into one.
-- `sha256:<64 lowercase hex>` is this runtime's *internal blob address* and the
-- staged-import checksum -- the accepted `ContentChecksum` domain, one algorithm,
-- one length, one letter case, because both sides must recompute and compare it
-- byte for byte. The public `EvidenceArtifact.content_checksum` is the separate
-- provider-neutral `EvidenceChecksum` domain (`algorithm:digest`, up to 256
-- characters), so an artifact whose provider published a non-SHA checksum is still
-- storable beside the exact SHA-256 address of the bytes this workspace holds.
-- Collapsing them would mean either refusing that artifact or lying about which
-- value addresses the blob.
--
-- Every column typed by the frozen public application contract carries that domain's
-- *pattern*, not merely its length. A length bound alone accepts ` ` and `../x` and
-- `SHA:abc` at the storage layer while the wire refuses them, and a durable table
-- that admits values its own contract cannot express is a divergence that only
-- surfaces once the rows exist. Four domains appear below, each written the way
-- SQLite can state it exactly:
--
--   Identifier / public id / open handle  ^[A-Za-z0-9][A-Za-z0-9._:-]*$, 1..128
--       first character alphanumeric, and no character outside the frozen set.
--   OpenCode                              ^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$, 1..128
--       first character lowercase, only `[a-z0-9_.]`, no trailing dot, and every dot
--       followed by a lowercase letter -- which is the same language, because those
--       four facts force each dot-separated segment to begin with `[a-z]`.
--   EvidenceChecksum                      ^[a-z][a-z0-9_]*:[A-Za-z0-9+/=_-]+$, <= 256
--   MediaType                             type `/` subtype, <= 255
--
-- The last two are split at their sole separator and each half validated on its own,
-- because "contains a colon" is not the rule -- `sha-256:abc:def` contains one and is
-- not an `EvidenceChecksum`. `length(x) - length(replace(x, ':', '')) = 1` is what
-- makes "exactly one" exact, and `substr`/`instr` is what makes each side a value the
-- pattern can then be applied to rather than a substring somewhere in the whole.
--
-- Open codes stay open. `sensitivity`, `source_kind`, `record_type`, `span_kind`,
-- `permission_label`, the actor kinds and the parser/ingestion statuses are validated
-- as *shapes*, never narrowed to a local enum: an unrecognised but well-formed code
-- is exactly the fact a later evaluator needs, and refusing it here would discard it.
-- Where the accepted contract is genuinely closed -- the integrity outcomes, the
-- staging outcomes, the label actions -- the closed `IN (...)` set stays, and each of
-- its members is itself a well-formed open code.
--
-- `OpaqueToken` (`staged_source_ref`, `import_run_id`) keeps its accepted domain
-- unchanged: printable ASCII, 1..512. It is a handle this runtime issued or a job id,
-- not a public identifier, and narrowing it here would refuse tokens the accepted
-- contract already admits.
--
-- Canonical structured JSON is stored as exact Core RFC 8785 (ADR-039 I-JSON) UTF-8
-- text alongside a `sha256:` digest of those bytes. The checks below police type,
-- bounds and digest *spelling* and nothing else. SQLite's own `json(...)` is not the
-- canonicalizer and is deliberately never called here, and no check recomputes the
-- digest: the bytes are canonicalised and the digest verified above this layer, and
-- a SQL check that appeared to confirm either would be asserting something this
-- layer cannot know. `→` The one honest claim is that a value which is not a
-- well-formed digest cannot be stored as one.
--
-- Every foreign key is left at SQLite's default immediate enforcement, for the
-- reason 0007 records at length: `DEFERRABLE INITIALLY DEFERRED` moves the check to
-- COMMIT, and SQLite leaves the transaction *open* when COMMIT fails, which would
-- strand the service's single authoritative write connection. The graph below is
-- acyclic -- blob, then staged source, then artifact, then that artifact's labels,
-- provenance, references and normalized records -- so nothing here needs a
-- commit-time cycle. A repository must insert an artifact and its initial capture
-- event in one transaction; that is a transaction shape, not a schema constraint,
-- and no deferred cyclic key is introduced to pretend otherwise.
--
-- The composite keys are what keep identity honest. A provenance event names the
-- artifact *and* its exact source, a reference names the event and repeats that same
-- source, and a normalized record names the artifact and the blob digest it was
-- parsed from -- so a foreign, undeclared source cannot be smuggled into an
-- artifact's own history, which is the same rule the public contract's
-- `_validate_evidence_reference` applies on the wire.
--
-- Indexes are named. SQLite would supply an implicit `sqlite_autoindex_*` for the
-- primary keys, but the canonical schema fingerprint filters those out, so a
-- constraint that exists only implicitly is a constraint drift detection cannot see.
--
-- What this file does *not* prove is stated plainly, because a durable schema that
-- looks like durability is worse than one that admits it is not. There is no
-- physical blob store in this checkout. Nothing here proves a byte was ever fsynced,
-- atomically published, or is still on disk; `omnivia_blob_objects` records that a
-- verification *happened*, and `omnivia_blob_integrity_events` exists precisely so a
-- later "it is gone" is recordable. Fresh-reference garbage collection, physical
-- missing detection and recovery, evidence retention and collection, ACL and grant
-- evaluation, archive safety, provider branch/asset mapping, import resume, durable
-- job expansion, governed records and projections are all later accepted slices.
--
-- A staged descriptor carries no filesystem path, URL, inline archive, credential,
-- bearer token, connector configuration, client-asserted authenticated identity,
-- parser implementation selector, workspace selector, or runtime/storage option --
-- by having no column any of them could be written into. That is the accepted
-- `ImportSourceDescriptor` boundary made structural: an import cannot be steered
-- from the wire into reading something the server did not already stage.

-- Every comment in this file sits *between* statements, never inside one. SQLite
-- stores a statement's original text in `sqlite_master`, and the migrator executes
-- statements one at a time with comments stripped so that a migration stays inside
-- the caller's transaction -- so a comment inside a CREATE would make the applied
-- schema differ, byte for byte, from the canonical fingerprint built by replaying the
-- same artifacts with `executescript`. Readiness compares exactly those two, and a
-- clean workspace would be reported as drifted.


-- One verified immutable content identity, and only that. The identity *is*
-- (workspace, sha256 content address, byte length): two sources or two evidence
-- artifacts that resolve to the same bytes share this one row, which is the point --
-- deduplication is a property of content, not a merge of the things that reference
-- it. Nothing about where the bytes live is stored, because a path is not part of an
-- identity and a schema that recorded one would be quietly asserting the bytes are
-- still there.
--
-- The two instants are kept apart rather than collapsed into "when we saw it": the
-- row is created once and verification is what earned it, so a later reader can tell
-- the order those two happened in. Zero length is a legitimate, checksummable
-- identity and is admitted deliberately.
CREATE TABLE IF NOT EXISTS omnivia_blob_objects (
    workspace_id         TEXT    NOT NULL,
    content_digest       TEXT    NOT NULL,
    content_length_bytes INTEGER NOT NULL,
    created_at_us        INTEGER NOT NULL,
    verified_at_us       INTEGER NOT NULL,

    PRIMARY KEY (workspace_id, content_digest),

    CHECK (
        typeof(workspace_id) = 'text'
        AND length(workspace_id) BETWEEN 1 AND 128
        AND workspace_id GLOB '[A-Za-z0-9]*'
        AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
        AND instr(workspace_id, char(0)) = 0
    ),
    CHECK (
        typeof(content_digest) = 'text' AND length(content_digest) = 71
        AND substr(content_digest, 1, 7) = 'sha256:'
        AND substr(content_digest, 8) NOT GLOB '*[^0-9a-f]*'
        AND instr(content_digest, char(0)) = 0
    ),
    CHECK (typeof(content_length_bytes) = 'integer' AND content_length_bytes >= 0),
    CHECK (typeof(created_at_us)  = 'integer' AND created_at_us  > 0),
    CHECK (typeof(verified_at_us) = 'integer' AND verified_at_us > 0),
    CHECK (verified_at_us >= created_at_us)
) WITHOUT ROWID;

-- What an integrity or inventory pass observed about one content address.
--
-- Deliberately *not* a child of `omnivia_blob_objects`. The facts most worth keeping
-- are the ones about a digest that has no accepted blob row -- a physical orphan, a
-- file that no longer verifies, bytes found where no identity claims them -- and a
-- foreign key would make exactly those unrecordable. `content_digest` is therefore
-- the expected address as a value, not as a reference.
--
-- The outcomes are a closed set because an unrecognised one is a reader guessing.
-- `digest_mismatch` must name what was actually found and it must differ from what
-- was expected; `verified` may not contradict itself by naming a different one. This
-- table never deletes bytes and performs no retention: `orphan_candidate`,
-- `staging_protected` and `collection_deferred` are observations a later, separately
-- accepted collector may read, not instructions it has already been given.
CREATE TABLE IF NOT EXISTS omnivia_blob_integrity_events (
    integrity_event_id    TEXT    NOT NULL PRIMARY KEY,
    workspace_id          TEXT    NOT NULL,
    content_digest        TEXT    NOT NULL,
    integrity_sequence    INTEGER NOT NULL,
    outcome               TEXT    NOT NULL,
    observed_digest       TEXT,
    observed_length_bytes INTEGER,
    expected_length_bytes INTEGER,
    inventory_id          TEXT,
    checked_at_us         INTEGER NOT NULL,

    CHECK (
        typeof(integrity_event_id) = 'text'
        AND length(integrity_event_id) BETWEEN 1 AND 128
        AND integrity_event_id GLOB '[A-Za-z0-9]*'
        AND integrity_event_id NOT GLOB '*[^A-Za-z0-9._:-]*'
        AND instr(integrity_event_id, char(0)) = 0
    ),
    CHECK (
        typeof(workspace_id) = 'text'
        AND length(workspace_id) BETWEEN 1 AND 128
        AND workspace_id GLOB '[A-Za-z0-9]*'
        AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
        AND instr(workspace_id, char(0)) = 0
    ),
    CHECK (
        inventory_id IS NULL
        OR (
            typeof(inventory_id) = 'text'
            AND length(inventory_id) BETWEEN 1 AND 128
            AND inventory_id GLOB '[A-Za-z0-9]*'
            AND inventory_id NOT GLOB '*[^A-Za-z0-9._:-]*'
            AND instr(inventory_id, char(0)) = 0
        )
    ),
    CHECK (
        typeof(content_digest) = 'text'
        AND length(content_digest) = 71
        AND substr(content_digest, 1, 7) = 'sha256:'
        AND substr(content_digest, 8) NOT GLOB '*[^0-9a-f]*'
        AND instr(content_digest, char(0)) = 0
    ),
    CHECK (
        observed_digest IS NULL
        OR (
            typeof(observed_digest) = 'text'
            AND length(observed_digest) = 71
            AND substr(observed_digest, 1, 7) = 'sha256:'
            AND substr(observed_digest, 8) NOT GLOB '*[^0-9a-f]*'
            AND instr(observed_digest, char(0)) = 0
        )
    ),
    CHECK (typeof(integrity_sequence) = 'integer' AND integrity_sequence > 0),
    CHECK (
        typeof(outcome) = 'text'
        AND outcome IN (
            'verified', 'missing', 'digest_mismatch', 'orphan_candidate',
            'staging_protected', 'collection_deferred'
        )
        AND instr(outcome, char(0)) = 0
    ),
    CHECK (
        observed_length_bytes IS NULL
        OR (typeof(observed_length_bytes) = 'integer' AND observed_length_bytes >= 0)
    ),
    CHECK (
        expected_length_bytes IS NULL
        OR (typeof(expected_length_bytes) = 'integer' AND expected_length_bytes >= 0)
    ),
    CHECK (
        (outcome = 'digest_mismatch'
         AND observed_digest IS NOT NULL AND observed_digest <> content_digest)
        OR (outcome = 'verified'
            AND (observed_digest IS NULL OR observed_digest = content_digest))
        OR outcome NOT IN ('digest_mismatch', 'verified')
    ),
    CHECK (typeof(checked_at_us) = 'integer' AND checked_at_us > 0)
) WITHOUT ROWID;

-- One already-staged import source, in the exact shape the accepted
-- `ImportSourceDescriptor` defines: a server-issued opaque handle and the content
-- facts that handle resolves to. Nothing about how the content got there and nothing
-- about how it will be read, so there is no column an import could be steered
-- through -- no path, URL, inline archive, credential, connector configuration,
-- parser selector, workspace selector or storage-engine locator.
--
-- `declared_checksum` is what the caller asserted; `computed_checksum` is what this
-- runtime independently made of the same bytes, kept separately and optional so a
-- disagreement is a recorded fact rather than a lost one. `content_length_bytes` is
-- part of the blob reference below, not merely stored beside it.
--
-- Verification is expressed as a constraint, not as a convention. A `verified`
-- staging must reference a blob, and the reference is composite over digest *and*
-- length, so "the blob it names agrees about the bytes" is enforced by the foreign
-- key rather than by whoever wrote the row. Every failing outcome is preserved and
-- inspectable, and none of them may name a blob at all -- which is what stops a
-- mismatch, a missing blob, an unsupported or an unsafe staging from ever becoming
-- the ancestor of accepted evidence.
CREATE TABLE IF NOT EXISTS omnivia_staged_sources (
    staged_source_ref        TEXT    NOT NULL PRIMARY KEY,
    workspace_id             TEXT    NOT NULL,
    source_kind              TEXT    NOT NULL,
    declared_checksum        TEXT    NOT NULL,
    content_length_bytes     INTEGER NOT NULL,
    media_type               TEXT    NOT NULL,
    source_version           TEXT,
    computed_checksum        TEXT,
    original_metadata_json   TEXT    NOT NULL,
    original_metadata_digest TEXT    NOT NULL,
    staging_outcome          TEXT    NOT NULL,
    blob_workspace_id        TEXT,
    blob_content_digest      TEXT,
    recorded_at_us           INTEGER NOT NULL,

    CHECK (typeof(staged_source_ref) = 'text'),
    CHECK (length(staged_source_ref) BETWEEN 1 AND 512),
    CHECK (staged_source_ref NOT GLOB '*[^!-~]*'),
    CHECK (instr(staged_source_ref, char(0)) = 0),
    CHECK (
        typeof(workspace_id) = 'text'
        AND length(workspace_id) BETWEEN 1 AND 128
        AND workspace_id GLOB '[A-Za-z0-9]*'
        AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
        AND instr(workspace_id, char(0)) = 0
    ),
    CHECK (
        typeof(source_kind) = 'text'
        AND length(source_kind) BETWEEN 1 AND 128
        AND source_kind GLOB '[a-z]*'
        AND source_kind NOT GLOB '*[^a-z0-9_.]*'
        AND source_kind NOT GLOB '*.'
        AND source_kind NOT GLOB '*.[^a-z]*'
        AND instr(source_kind, char(0)) = 0
    ),
    CHECK (
        source_version IS NULL
        OR (
            typeof(source_version) = 'text'
            AND length(source_version) BETWEEN 1 AND 128
            AND source_version GLOB '[A-Za-z0-9]*'
            AND source_version NOT GLOB '*[^A-Za-z0-9._:-]*'
            AND instr(source_version, char(0)) = 0
        )
    ),
    CHECK (
        typeof(declared_checksum) = 'text'
        AND length(declared_checksum) = 71
        AND substr(declared_checksum, 1, 7) = 'sha256:'
        AND substr(declared_checksum, 8) NOT GLOB '*[^0-9a-f]*'
        AND instr(declared_checksum, char(0)) = 0
    ),
    CHECK (
        computed_checksum IS NULL
        OR (
            typeof(computed_checksum) = 'text'
            AND length(computed_checksum) = 71
            AND substr(computed_checksum, 1, 7) = 'sha256:'
            AND substr(computed_checksum, 8) NOT GLOB '*[^0-9a-f]*'
            AND instr(computed_checksum, char(0)) = 0
        )
    ),
    CHECK (typeof(content_length_bytes) = 'integer' AND content_length_bytes >= 0),
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
    CHECK (
        typeof(original_metadata_json) = 'text'
        AND length(original_metadata_json) BETWEEN 1 AND 8192
        AND instr(original_metadata_json, char(0)) = 0
    ),
    CHECK (
        typeof(original_metadata_digest) = 'text'
        AND length(original_metadata_digest) = 71
        AND substr(original_metadata_digest, 1, 7) = 'sha256:'
        AND substr(original_metadata_digest, 8) NOT GLOB '*[^0-9a-f]*'
        AND instr(original_metadata_digest, char(0)) = 0
    ),
    CHECK (
        typeof(staging_outcome) = 'text'
        AND staging_outcome IN (
            'verified', 'digest_mismatch', 'missing_blob', 'unsupported', 'unsafe'
        )
        AND instr(staging_outcome, char(0)) = 0
    ),
    CHECK (
        blob_workspace_id IS NULL
        OR (
            typeof(blob_workspace_id) = 'text'
            AND blob_workspace_id = workspace_id
            AND instr(blob_workspace_id, char(0)) = 0
        )
    ),
    CHECK (
        blob_content_digest IS NULL
        OR (typeof(blob_content_digest) = 'text' AND instr(blob_content_digest, char(0)) = 0)
    ),
    CHECK (
        (staging_outcome = 'verified'
         AND blob_workspace_id IS NOT NULL
         AND blob_content_digest = declared_checksum
         AND (computed_checksum IS NULL OR computed_checksum = declared_checksum))
        OR (staging_outcome <> 'verified'
            AND blob_workspace_id IS NULL
            AND blob_content_digest IS NULL)
    ),
    CHECK (typeof(recorded_at_us) = 'integer' AND recorded_at_us > 0),

    FOREIGN KEY (blob_workspace_id, blob_content_digest, content_length_bytes)
        REFERENCES omnivia_blob_objects
            (workspace_id, content_digest, content_length_bytes)
) WITHOUT ROWID;

-- One captured L0 evidence artifact: raw material, never governed knowledge.
--
-- It carries no governance layer, governance state, currentness, authority level,
-- and no mutable current/truth/deleted flag, exactly as the accepted
-- `EvidenceArtifact` shape carries none -- this table must never be readable as a
-- `GovernedRecord`. `tombstoned` is likewise absent as a column: a tombstone is an
-- appended provenance event, and the current interpretation is derived from that
-- stream rather than stored as a value something would later have to overwrite.
-- Same for parser and ingestion state, which is stored here as the status *at
-- capture* and is corrected by appending, never by rewriting.
--
-- The four instants stay separate. `event_at_us` is when the fact occurred in the
-- world, `observed_at_us` when it was observed, `ingested_at_us` when this system
-- first took it in, `recorded_at_us` when this row was written; the first two carry
-- the full signed domain and the check that observation cannot follow ingestion is
-- the same rule the contract's semantic layer applies.
--
-- `content_checksum` is the public provider-neutral `EvidenceChecksum`;
-- `blob_content_digest` is the exact internal SHA-256 address of the bytes this
-- workspace verified. Both are required and neither substitutes for the other. The
-- blob reference is NOT NULL and composite over the workspace, so accepted evidence
-- always names an existing blob row in this workspace and can never name another's.
--
-- `import_run_id` is optional because evidence may be captured outside an import
-- run. When present it is an `OpaqueToken` in the same domain as `JobIdentity.job_id`
-- and it must be a real durable job -- declared as a foreign key, and required by the
-- INSERT trigger to be an `ingestion.import` job specifically, since a foreign key
-- can prove a job exists but not what kind of work it was. That is required to stay
-- true rather than merely to have been true once: the inherited durable-job UPDATE
-- guard is recreated at the foot of this file so a later retag of a referenced job
-- cannot quietly turn accepted evidence into evidence of some other kind of work.
-- The workspace database is already one singleton workspace, so the job schema is
-- left exactly as accepted rather than gaining a second workspace key it has no use
-- for.
--
-- `staged_source_ref` is held to the *whole* staged ancestry, not just to a staging
-- that happens to exist. A foreign key proves the reference resolves, and the INSERT
-- trigger proves the row it resolves to verified in this workspace, declared this
-- artifact's own `source_kind`, and addresses these exact bytes. Without the last
-- two, an artifact could name a staging that verified digest A while itself claiming
-- digest B -- an ancestry that reads as proven and is not -- or substitute a source
-- kind the staging never declared. Both are refused here permanently.
CREATE TABLE IF NOT EXISTS omnivia_evidence_artifacts (
    evidence_id              TEXT    NOT NULL PRIMARY KEY,
    workspace_id             TEXT    NOT NULL,
    source_kind              TEXT    NOT NULL,
    source_native_id         TEXT    NOT NULL,
    source_locator           TEXT,
    source_retrieved_at_us   INTEGER,
    event_at_us              INTEGER,
    observed_at_us           INTEGER,
    ingested_at_us           INTEGER NOT NULL,
    recorded_at_us           INTEGER NOT NULL,
    content_checksum         TEXT    NOT NULL,
    blob_content_digest      TEXT    NOT NULL,
    media_type               TEXT    NOT NULL,
    original_metadata_json   TEXT    NOT NULL,
    original_metadata_digest TEXT    NOT NULL,
    sensitivity              TEXT    NOT NULL,
    parser_status            TEXT    NOT NULL,
    ingestion_status         TEXT    NOT NULL,
    staged_source_ref        TEXT,
    import_run_id            TEXT,

    CHECK (
        typeof(evidence_id) = 'text'
        AND length(evidence_id) BETWEEN 1 AND 128
        AND evidence_id GLOB '[A-Za-z0-9]*'
        AND evidence_id NOT GLOB '*[^A-Za-z0-9._:-]*'
        AND instr(evidence_id, char(0)) = 0
    ),
    CHECK (
        typeof(workspace_id) = 'text'
        AND length(workspace_id) BETWEEN 1 AND 128
        AND workspace_id GLOB '[A-Za-z0-9]*'
        AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
        AND instr(workspace_id, char(0)) = 0
    ),
    CHECK (
        typeof(source_native_id) = 'text'
        AND length(source_native_id) BETWEEN 1 AND 128
        AND source_native_id GLOB '[A-Za-z0-9]*'
        AND source_native_id NOT GLOB '*[^A-Za-z0-9._:-]*'
        AND instr(source_native_id, char(0)) = 0
    ),
    CHECK (
        typeof(source_kind) = 'text'
        AND length(source_kind) BETWEEN 1 AND 128
        AND source_kind GLOB '[a-z]*'
        AND source_kind NOT GLOB '*[^a-z0-9_.]*'
        AND source_kind NOT GLOB '*.'
        AND source_kind NOT GLOB '*.[^a-z]*'
        AND instr(source_kind, char(0)) = 0
    ),
    CHECK (
        typeof(sensitivity) = 'text'
        AND length(sensitivity) BETWEEN 1 AND 128
        AND sensitivity GLOB '[a-z]*'
        AND sensitivity NOT GLOB '*[^a-z0-9_.]*'
        AND sensitivity NOT GLOB '*.'
        AND sensitivity NOT GLOB '*.[^a-z]*'
        AND instr(sensitivity, char(0)) = 0
    ),
    CHECK (
        typeof(parser_status) = 'text'
        AND length(parser_status) BETWEEN 1 AND 128
        AND parser_status GLOB '[a-z]*'
        AND parser_status NOT GLOB '*[^a-z0-9_.]*'
        AND parser_status NOT GLOB '*.'
        AND parser_status NOT GLOB '*.[^a-z]*'
        AND instr(parser_status, char(0)) = 0
    ),
    CHECK (
        typeof(ingestion_status) = 'text'
        AND length(ingestion_status) BETWEEN 1 AND 128
        AND ingestion_status GLOB '[a-z]*'
        AND ingestion_status NOT GLOB '*[^a-z0-9_.]*'
        AND ingestion_status NOT GLOB '*.'
        AND ingestion_status NOT GLOB '*.[^a-z]*'
        AND instr(ingestion_status, char(0)) = 0
    ),
    CHECK (
        source_locator IS NULL
        OR (
            typeof(source_locator) = 'text'
            AND length(source_locator) BETWEEN 1 AND 2048
            AND instr(source_locator, char(0)) = 0
        )
    ),
    CHECK (
        staged_source_ref IS NULL
        OR (typeof(staged_source_ref) = 'text'
            AND length(staged_source_ref) BETWEEN 1 AND 512
            AND staged_source_ref NOT GLOB '*[^!-~]*'
            AND instr(staged_source_ref, char(0)) = 0)
    ),
    CHECK (
        import_run_id IS NULL
        OR (typeof(import_run_id) = 'text'
            AND length(import_run_id) BETWEEN 1 AND 512
            AND import_run_id NOT GLOB '*[^!-~]*'
            AND instr(import_run_id, char(0)) = 0)
    ),
    CHECK (
        typeof(content_checksum) = 'text'
        AND length(content_checksum) BETWEEN 3 AND 256
        AND length(content_checksum) - length(replace(content_checksum, ':', '')) = 1
        AND substr(content_checksum, 1, instr(content_checksum, ':') - 1)
            GLOB '[a-z]*'
        AND substr(content_checksum, 1, instr(content_checksum, ':') - 1)
            NOT GLOB '*[^a-z0-9_]*'
        AND length(substr(content_checksum, instr(content_checksum, ':') + 1)) >= 1
        AND substr(content_checksum, instr(content_checksum, ':') + 1)
            NOT GLOB '*[^A-Za-z0-9+/=_-]*'
        AND instr(content_checksum, char(0)) = 0
    ),
    CHECK (
        typeof(blob_content_digest) = 'text'
        AND length(blob_content_digest) = 71
        AND substr(blob_content_digest, 1, 7) = 'sha256:'
        AND substr(blob_content_digest, 8) NOT GLOB '*[^0-9a-f]*'
        AND instr(blob_content_digest, char(0)) = 0
    ),
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
    CHECK (
        typeof(original_metadata_json) = 'text'
        AND length(original_metadata_json) BETWEEN 1 AND 8192
        AND instr(original_metadata_json, char(0)) = 0
    ),
    CHECK (
        typeof(original_metadata_digest) = 'text'
        AND length(original_metadata_digest) = 71
        AND substr(original_metadata_digest, 1, 7) = 'sha256:'
        AND substr(original_metadata_digest, 8) NOT GLOB '*[^0-9a-f]*'
        AND instr(original_metadata_digest, char(0)) = 0
    ),
    CHECK (
        source_retrieved_at_us IS NULL
        OR (typeof(source_retrieved_at_us) = 'integer' AND source_retrieved_at_us > 0)
    ),
    CHECK (event_at_us    IS NULL OR typeof(event_at_us)    = 'integer'),
    CHECK (observed_at_us IS NULL OR typeof(observed_at_us) = 'integer'),
    CHECK (typeof(ingested_at_us) = 'integer' AND ingested_at_us > 0),
    CHECK (typeof(recorded_at_us) = 'integer' AND recorded_at_us > 0),
    CHECK (observed_at_us IS NULL OR observed_at_us <= ingested_at_us),

    FOREIGN KEY (workspace_id, blob_content_digest)
        REFERENCES omnivia_blob_objects (workspace_id, content_digest),
    FOREIGN KEY (staged_source_ref, workspace_id)
        REFERENCES omnivia_staged_sources (staged_source_ref, workspace_id),
    FOREIGN KEY (import_run_id)
        REFERENCES omnivia_durable_jobs (job_id)
) WITHOUT ROWID;

-- Raw permission-label facts about one exact evidence artifact, as policy *input*.
--
-- Not a grant, not an entitlement, and not an evaluated decision: no ACL schema, no
-- grant resolver and no normalized permission exists in this slice, and nothing here
-- may be read as their durable form. An unrecognised label is representable on
-- purpose -- refusing to store a label this version does not know would silently
-- discard the very fact a later evaluator needs.
--
-- The stream is what makes the current interpretation deterministic without a
-- rewritable column. An `attached` and a `withdrawn` for the same label, in the same
-- microsecond, are ordered by `label_sequence` and nothing else; the reader takes the
-- highest sequence. `corrected` exists so a mis-recorded label is superseded by an
-- appended fact rather than by an UPDATE the triggers refuse anyway.
--
-- Labels attach to an evidence identity, never to content. Two artifacts sharing one
-- blob share no label, which is why the key is the artifact.
CREATE TABLE IF NOT EXISTS omnivia_evidence_permission_labels (
    label_event_id   TEXT    NOT NULL PRIMARY KEY,
    evidence_id      TEXT    NOT NULL,
    workspace_id     TEXT    NOT NULL,
    label_sequence   INTEGER NOT NULL,
    label_action     TEXT    NOT NULL,
    permission_label TEXT    NOT NULL,
    recorded_at_us   INTEGER NOT NULL,

    CHECK (
        typeof(label_event_id) = 'text'
        AND length(label_event_id) BETWEEN 1 AND 128
        AND label_event_id GLOB '[A-Za-z0-9]*'
        AND label_event_id NOT GLOB '*[^A-Za-z0-9._:-]*'
        AND instr(label_event_id, char(0)) = 0
    ),
    CHECK (
        typeof(evidence_id) = 'text'
        AND length(evidence_id) BETWEEN 1 AND 128
        AND evidence_id GLOB '[A-Za-z0-9]*'
        AND evidence_id NOT GLOB '*[^A-Za-z0-9._:-]*'
        AND instr(evidence_id, char(0)) = 0
    ),
    CHECK (
        typeof(workspace_id) = 'text'
        AND length(workspace_id) BETWEEN 1 AND 128
        AND workspace_id GLOB '[A-Za-z0-9]*'
        AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
        AND instr(workspace_id, char(0)) = 0
    ),
    CHECK (
        typeof(permission_label) = 'text'
        AND length(permission_label) BETWEEN 1 AND 128
        AND permission_label GLOB '[a-z]*'
        AND permission_label NOT GLOB '*[^a-z0-9_.]*'
        AND permission_label NOT GLOB '*.'
        AND permission_label NOT GLOB '*.[^a-z]*'
        AND instr(permission_label, char(0)) = 0
    ),
    CHECK (typeof(label_sequence) = 'integer' AND label_sequence > 0),
    CHECK (
        typeof(label_action) = 'text'
        AND label_action IN ('attached', 'withdrawn', 'corrected')
        AND instr(label_action, char(0)) = 0
    ),
    CHECK (typeof(recorded_at_us) = 'integer' AND recorded_at_us > 0),

    FOREIGN KEY (evidence_id, workspace_id)
        REFERENCES omnivia_evidence_artifacts (evidence_id, workspace_id)
) WITHOUT ROWID;

-- Who or what did something to one exact evidence artifact, and when, in order.
--
-- The artifact's tombstone status and its later parser/ingestion state are read from
-- this stream rather than stored on the artifact, so "it was tombstoned" is always
-- an audited act with an actor and a time behind it rather than a boolean somebody
-- set. `tombstoned_observation` here is what this event observed, not a flag this
-- event flips.
--
-- The foreign key is over four columns, not two, and that is the point: the event
-- names the artifact, the workspace *and* the artifact's own exact source identity,
-- so provenance cannot be written against an artifact while claiming a source that
-- artifact never declared. `audit_ref` optionally correlates the act with the M1
-- application audit trail; it is composite over the workspace for the same reason
-- every M1 link is.
CREATE TABLE IF NOT EXISTS omnivia_evidence_provenance_events (
    provenance_event_id    TEXT    NOT NULL PRIMARY KEY,
    evidence_id            TEXT    NOT NULL,
    workspace_id           TEXT    NOT NULL,
    provenance_sequence    INTEGER NOT NULL,
    actor_id               TEXT    NOT NULL,
    actor_kind             TEXT    NOT NULL,
    action                 TEXT    NOT NULL,
    occurred_at_us         INTEGER NOT NULL,
    reason_code            TEXT,
    reason_comment         TEXT,
    parser_status          TEXT,
    ingestion_status       TEXT,
    tombstoned_observation INTEGER,
    source_kind            TEXT    NOT NULL,
    source_native_id       TEXT    NOT NULL,
    audit_ref              TEXT,

    CHECK (
        typeof(provenance_event_id) = 'text'
        AND length(provenance_event_id) BETWEEN 1 AND 128
        AND provenance_event_id GLOB '[A-Za-z0-9]*'
        AND provenance_event_id NOT GLOB '*[^A-Za-z0-9._:-]*'
        AND instr(provenance_event_id, char(0)) = 0
    ),
    CHECK (
        typeof(evidence_id) = 'text'
        AND length(evidence_id) BETWEEN 1 AND 128
        AND evidence_id GLOB '[A-Za-z0-9]*'
        AND evidence_id NOT GLOB '*[^A-Za-z0-9._:-]*'
        AND instr(evidence_id, char(0)) = 0
    ),
    CHECK (
        typeof(workspace_id) = 'text'
        AND length(workspace_id) BETWEEN 1 AND 128
        AND workspace_id GLOB '[A-Za-z0-9]*'
        AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
        AND instr(workspace_id, char(0)) = 0
    ),
    CHECK (
        typeof(actor_id) = 'text'
        AND length(actor_id) BETWEEN 1 AND 128
        AND actor_id GLOB '[A-Za-z0-9]*'
        AND actor_id NOT GLOB '*[^A-Za-z0-9._:-]*'
        AND instr(actor_id, char(0)) = 0
    ),
    CHECK (
        typeof(source_native_id) = 'text'
        AND length(source_native_id) BETWEEN 1 AND 128
        AND source_native_id GLOB '[A-Za-z0-9]*'
        AND source_native_id NOT GLOB '*[^A-Za-z0-9._:-]*'
        AND instr(source_native_id, char(0)) = 0
    ),
    CHECK (
        audit_ref IS NULL
        OR (
            typeof(audit_ref) = 'text'
            AND length(audit_ref) BETWEEN 1 AND 128
            AND audit_ref GLOB '[A-Za-z0-9]*'
            AND audit_ref NOT GLOB '*[^A-Za-z0-9._:-]*'
            AND instr(audit_ref, char(0)) = 0
        )
    ),
    CHECK (
        typeof(actor_kind) = 'text'
        AND length(actor_kind) BETWEEN 1 AND 128
        AND actor_kind GLOB '[a-z]*'
        AND actor_kind NOT GLOB '*[^a-z0-9_.]*'
        AND actor_kind NOT GLOB '*.'
        AND actor_kind NOT GLOB '*.[^a-z]*'
        AND instr(actor_kind, char(0)) = 0
    ),
    CHECK (
        typeof(action) = 'text'
        AND length(action) BETWEEN 1 AND 128
        AND action GLOB '[a-z]*'
        AND action NOT GLOB '*[^a-z0-9_.]*'
        AND action NOT GLOB '*.'
        AND action NOT GLOB '*.[^a-z]*'
        AND instr(action, char(0)) = 0
    ),
    CHECK (
        typeof(source_kind) = 'text'
        AND length(source_kind) BETWEEN 1 AND 128
        AND source_kind GLOB '[a-z]*'
        AND source_kind NOT GLOB '*[^a-z0-9_.]*'
        AND source_kind NOT GLOB '*.'
        AND source_kind NOT GLOB '*.[^a-z]*'
        AND instr(source_kind, char(0)) = 0
    ),
    CHECK (
        reason_code IS NULL
        OR (
            typeof(reason_code) = 'text'
            AND length(reason_code) BETWEEN 1 AND 128
            AND reason_code GLOB '[a-z]*'
            AND reason_code NOT GLOB '*[^a-z0-9_.]*'
            AND reason_code NOT GLOB '*.'
            AND reason_code NOT GLOB '*.[^a-z]*'
            AND instr(reason_code, char(0)) = 0
        )
    ),
    CHECK (
        parser_status IS NULL
        OR (
            typeof(parser_status) = 'text'
            AND length(parser_status) BETWEEN 1 AND 128
            AND parser_status GLOB '[a-z]*'
            AND parser_status NOT GLOB '*[^a-z0-9_.]*'
            AND parser_status NOT GLOB '*.'
            AND parser_status NOT GLOB '*.[^a-z]*'
            AND instr(parser_status, char(0)) = 0
        )
    ),
    CHECK (
        ingestion_status IS NULL
        OR (
            typeof(ingestion_status) = 'text'
            AND length(ingestion_status) BETWEEN 1 AND 128
            AND ingestion_status GLOB '[a-z]*'
            AND ingestion_status NOT GLOB '*[^a-z0-9_.]*'
            AND ingestion_status NOT GLOB '*.'
            AND ingestion_status NOT GLOB '*.[^a-z]*'
            AND instr(ingestion_status, char(0)) = 0
        )
    ),
    CHECK (
        reason_comment IS NULL
        OR (
            typeof(reason_comment) = 'text'
            AND length(reason_comment) BETWEEN 1 AND 2048
            AND instr(reason_comment, char(0)) = 0
        )
    ),
    CHECK (typeof(provenance_sequence) = 'integer' AND provenance_sequence > 0),
    CHECK (
        tombstoned_observation IS NULL OR tombstoned_observation IN (0, 1)
    ),
    CHECK (typeof(occurred_at_us) = 'integer' AND occurred_at_us > 0),

    FOREIGN KEY (evidence_id, workspace_id, source_kind, source_native_id)
        REFERENCES omnivia_evidence_artifacts
            (evidence_id, workspace_id, source_kind, source_native_id),
    FOREIGN KEY (audit_ref, workspace_id)
        REFERENCES omnivia_application_audit_events (audit_ref, workspace_id)
) WITHOUT ROWID;

-- What one provenance event pointed at, within the artifact's own declared source.
--
-- The foreign key repeats the event, the artifact, the workspace and the source
-- identity together, so a reference can only ever address the source its own event's
-- artifact declared. That is the same rule the accepted contract applies to a nested
-- `EvidenceReference`, expressed as a key rather than as a validator: a foreign,
-- undeclared source has nowhere to attach.
--
-- The span is optional and bounded -- an offset pair must be non-negative and must
-- not run backwards -- and the excerpt is an aid to a reader, not a stable interface.
CREATE TABLE IF NOT EXISTS omnivia_evidence_event_references (
    event_reference_id     TEXT    NOT NULL PRIMARY KEY,
    provenance_event_id    TEXT    NOT NULL,
    evidence_id            TEXT    NOT NULL,
    workspace_id           TEXT    NOT NULL,
    reference_ordinal      INTEGER NOT NULL,
    source_kind            TEXT    NOT NULL,
    source_native_id       TEXT    NOT NULL,
    source_locator         TEXT,
    source_retrieved_at_us INTEGER,
    span_pointer           TEXT,
    span_start_offset      INTEGER,
    span_end_offset        INTEGER,
    excerpt                TEXT,

    CHECK (
        typeof(event_reference_id) = 'text'
        AND length(event_reference_id) BETWEEN 1 AND 128
        AND event_reference_id GLOB '[A-Za-z0-9]*'
        AND event_reference_id NOT GLOB '*[^A-Za-z0-9._:-]*'
        AND instr(event_reference_id, char(0)) = 0
    ),
    CHECK (
        typeof(provenance_event_id) = 'text'
        AND length(provenance_event_id) BETWEEN 1 AND 128
        AND provenance_event_id GLOB '[A-Za-z0-9]*'
        AND provenance_event_id NOT GLOB '*[^A-Za-z0-9._:-]*'
        AND instr(provenance_event_id, char(0)) = 0
    ),
    CHECK (
        typeof(evidence_id) = 'text'
        AND length(evidence_id) BETWEEN 1 AND 128
        AND evidence_id GLOB '[A-Za-z0-9]*'
        AND evidence_id NOT GLOB '*[^A-Za-z0-9._:-]*'
        AND instr(evidence_id, char(0)) = 0
    ),
    CHECK (
        typeof(workspace_id) = 'text'
        AND length(workspace_id) BETWEEN 1 AND 128
        AND workspace_id GLOB '[A-Za-z0-9]*'
        AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
        AND instr(workspace_id, char(0)) = 0
    ),
    CHECK (
        typeof(source_native_id) = 'text'
        AND length(source_native_id) BETWEEN 1 AND 128
        AND source_native_id GLOB '[A-Za-z0-9]*'
        AND source_native_id NOT GLOB '*[^A-Za-z0-9._:-]*'
        AND instr(source_native_id, char(0)) = 0
    ),
    CHECK (
        typeof(source_kind) = 'text'
        AND length(source_kind) BETWEEN 1 AND 128
        AND source_kind GLOB '[a-z]*'
        AND source_kind NOT GLOB '*[^a-z0-9_.]*'
        AND source_kind NOT GLOB '*.'
        AND source_kind NOT GLOB '*.[^a-z]*'
        AND instr(source_kind, char(0)) = 0
    ),
    CHECK (
        source_locator IS NULL
        OR (
            typeof(source_locator) = 'text'
            AND length(source_locator) BETWEEN 1 AND 2048
            AND instr(source_locator, char(0)) = 0
        )
    ),
    CHECK (
        span_pointer IS NULL
        OR (
            typeof(span_pointer) = 'text'
            AND length(span_pointer) BETWEEN 1 AND 2048
            AND instr(span_pointer, char(0)) = 0
        )
    ),
    CHECK (
        excerpt IS NULL
        OR (
            typeof(excerpt) = 'text'
            AND length(excerpt) BETWEEN 1 AND 4096
            AND instr(excerpt, char(0)) = 0
        )
    ),
    CHECK (typeof(reference_ordinal) = 'integer' AND reference_ordinal > 0),
    CHECK (
        source_retrieved_at_us IS NULL
        OR (typeof(source_retrieved_at_us) = 'integer' AND source_retrieved_at_us > 0)
    ),
    CHECK (
        span_start_offset IS NULL
        OR (typeof(span_start_offset) = 'integer' AND span_start_offset >= 0)
    ),
    CHECK (
        span_end_offset IS NULL
        OR (typeof(span_end_offset) = 'integer' AND span_end_offset >= 0)
    ),
    CHECK (
        span_start_offset IS NULL
        OR span_end_offset IS NULL
        OR span_end_offset >= span_start_offset
    ),

    FOREIGN KEY
        (provenance_event_id, evidence_id, workspace_id, source_kind, source_native_id)
        REFERENCES omnivia_evidence_provenance_events
            (provenance_event_id, evidence_id, workspace_id, source_kind,
             source_native_id)
) WITHOUT ROWID;

-- Typed, schema-versioned records parsed out of one exact evidence artifact.
--
-- Normalization is additive. It never replaces or deletes the evidence it came from:
-- the artifact and its blob stay exactly as captured, and a reparse is a new stream
-- of records, which is why the sequence is per evidence and why UPDATE and DELETE
-- are refused here as everywhere else.
--
-- The key names the artifact *and* the blob digest the parse actually read, so a
-- record cannot be attributed to an artifact whose bytes it was not derived from.
-- `parser_id`/`parser_version` are lineage: what produced this, so a later defect in
-- a parser version is traceable to the records it wrote rather than inferred.
--
-- `content_json` is exact Core canonical JSON text and `content_digest` is a
-- `sha256:` digest of those bytes. Neither is recomputed or reserialised here.
CREATE TABLE IF NOT EXISTS omnivia_normalized_source_records (
    normalized_record_id TEXT    NOT NULL PRIMARY KEY,
    evidence_id          TEXT    NOT NULL,
    workspace_id         TEXT    NOT NULL,
    evidence_blob_digest TEXT    NOT NULL,
    record_sequence      INTEGER NOT NULL,
    record_type          TEXT    NOT NULL,
    schema_version       TEXT    NOT NULL,
    content_json         TEXT    NOT NULL,
    content_digest       TEXT    NOT NULL,
    parser_id            TEXT    NOT NULL,
    parser_version       TEXT    NOT NULL,
    recorded_at_us       INTEGER NOT NULL,

    CHECK (
        typeof(normalized_record_id) = 'text'
        AND length(normalized_record_id) BETWEEN 1 AND 128
        AND normalized_record_id GLOB '[A-Za-z0-9]*'
        AND normalized_record_id NOT GLOB '*[^A-Za-z0-9._:-]*'
        AND instr(normalized_record_id, char(0)) = 0
    ),
    CHECK (
        typeof(evidence_id) = 'text'
        AND length(evidence_id) BETWEEN 1 AND 128
        AND evidence_id GLOB '[A-Za-z0-9]*'
        AND evidence_id NOT GLOB '*[^A-Za-z0-9._:-]*'
        AND instr(evidence_id, char(0)) = 0
    ),
    CHECK (
        typeof(workspace_id) = 'text'
        AND length(workspace_id) BETWEEN 1 AND 128
        AND workspace_id GLOB '[A-Za-z0-9]*'
        AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
        AND instr(workspace_id, char(0)) = 0
    ),
    CHECK (
        typeof(schema_version) = 'text'
        AND length(schema_version) BETWEEN 1 AND 128
        AND schema_version GLOB '[A-Za-z0-9]*'
        AND schema_version NOT GLOB '*[^A-Za-z0-9._:-]*'
        AND instr(schema_version, char(0)) = 0
    ),
    CHECK (
        typeof(parser_id) = 'text'
        AND length(parser_id) BETWEEN 1 AND 128
        AND parser_id GLOB '[A-Za-z0-9]*'
        AND parser_id NOT GLOB '*[^A-Za-z0-9._:-]*'
        AND instr(parser_id, char(0)) = 0
    ),
    CHECK (
        typeof(parser_version) = 'text'
        AND length(parser_version) BETWEEN 1 AND 128
        AND parser_version GLOB '[A-Za-z0-9]*'
        AND parser_version NOT GLOB '*[^A-Za-z0-9._:-]*'
        AND instr(parser_version, char(0)) = 0
    ),
    CHECK (
        typeof(record_type) = 'text'
        AND length(record_type) BETWEEN 1 AND 128
        AND record_type GLOB '[a-z]*'
        AND record_type NOT GLOB '*[^a-z0-9_.]*'
        AND record_type NOT GLOB '*.'
        AND record_type NOT GLOB '*.[^a-z]*'
        AND instr(record_type, char(0)) = 0
    ),
    CHECK (
        typeof(content_json) = 'text'
        AND length(content_json) BETWEEN 1 AND 8192
        AND instr(content_json, char(0)) = 0
    ),
    CHECK (typeof(record_sequence) = 'integer' AND record_sequence > 0),
    CHECK (
        typeof(evidence_blob_digest) = 'text'
        AND length(evidence_blob_digest) = 71
        AND substr(evidence_blob_digest, 1, 7) = 'sha256:'
        AND substr(evidence_blob_digest, 8) NOT GLOB '*[^0-9a-f]*'
        AND instr(evidence_blob_digest, char(0)) = 0
    ),
    CHECK (
        typeof(content_digest) = 'text'
        AND length(content_digest) = 71
        AND substr(content_digest, 1, 7) = 'sha256:'
        AND substr(content_digest, 8) NOT GLOB '*[^0-9a-f]*'
        AND instr(content_digest, char(0)) = 0
    ),
    CHECK (typeof(recorded_at_us) = 'integer' AND recorded_at_us > 0),

    FOREIGN KEY (evidence_id, workspace_id, evidence_blob_digest)
        REFERENCES omnivia_evidence_artifacts
            (evidence_id, workspace_id, blob_content_digest)
) WITHOUT ROWID;

-- Where in the evidence one normalized record came from.
--
-- The backreference is composite over the record, the evidence and the workspace, so
-- a span always points back at the same evidence its own record was parsed from --
-- an exact same-evidence backreference, not a loose pointer that happens to look
-- right. `span_kind` is an open code because how a parser addresses its input is not
-- a closed vocabulary, and the offsets are optional, non-negative and ordered.
CREATE TABLE IF NOT EXISTS omnivia_normalized_source_spans (
    normalized_span_id   TEXT    NOT NULL PRIMARY KEY,
    normalized_record_id TEXT    NOT NULL,
    evidence_id          TEXT    NOT NULL,
    workspace_id         TEXT    NOT NULL,
    span_sequence        INTEGER NOT NULL,
    span_kind            TEXT    NOT NULL,
    span_pointer         TEXT    NOT NULL,
    span_start_offset    INTEGER,
    span_end_offset      INTEGER,
    recorded_at_us       INTEGER NOT NULL,

    CHECK (
        typeof(normalized_span_id) = 'text'
        AND length(normalized_span_id) BETWEEN 1 AND 128
        AND normalized_span_id GLOB '[A-Za-z0-9]*'
        AND normalized_span_id NOT GLOB '*[^A-Za-z0-9._:-]*'
        AND instr(normalized_span_id, char(0)) = 0
    ),
    CHECK (
        typeof(normalized_record_id) = 'text'
        AND length(normalized_record_id) BETWEEN 1 AND 128
        AND normalized_record_id GLOB '[A-Za-z0-9]*'
        AND normalized_record_id NOT GLOB '*[^A-Za-z0-9._:-]*'
        AND instr(normalized_record_id, char(0)) = 0
    ),
    CHECK (
        typeof(evidence_id) = 'text'
        AND length(evidence_id) BETWEEN 1 AND 128
        AND evidence_id GLOB '[A-Za-z0-9]*'
        AND evidence_id NOT GLOB '*[^A-Za-z0-9._:-]*'
        AND instr(evidence_id, char(0)) = 0
    ),
    CHECK (
        typeof(workspace_id) = 'text'
        AND length(workspace_id) BETWEEN 1 AND 128
        AND workspace_id GLOB '[A-Za-z0-9]*'
        AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
        AND instr(workspace_id, char(0)) = 0
    ),
    CHECK (
        typeof(span_kind) = 'text'
        AND length(span_kind) BETWEEN 1 AND 128
        AND span_kind GLOB '[a-z]*'
        AND span_kind NOT GLOB '*[^a-z0-9_.]*'
        AND span_kind NOT GLOB '*.'
        AND span_kind NOT GLOB '*.[^a-z]*'
        AND instr(span_kind, char(0)) = 0
    ),
    CHECK (
        typeof(span_pointer) = 'text'
        AND length(span_pointer) BETWEEN 1 AND 2048
        AND instr(span_pointer, char(0)) = 0
    ),
    CHECK (typeof(span_sequence) = 'integer' AND span_sequence > 0),
    CHECK (
        span_start_offset IS NULL
        OR (typeof(span_start_offset) = 'integer' AND span_start_offset >= 0)
    ),
    CHECK (
        span_end_offset IS NULL
        OR (typeof(span_end_offset) = 'integer' AND span_end_offset >= 0)
    ),
    CHECK (
        span_start_offset IS NULL
        OR span_end_offset IS NULL
        OR span_end_offset >= span_start_offset
    ),
    CHECK (typeof(recorded_at_us) = 'integer' AND recorded_at_us > 0),

    FOREIGN KEY (normalized_record_id, evidence_id, workspace_id)
        REFERENCES omnivia_normalized_source_records
            (normalized_record_id, evidence_id, workspace_id)
) WITHOUT ROWID;

-- Parent keys for the composite foreign keys above. SQLite requires the referenced
-- columns to carry a UNIQUE index over exactly that column set, and these are declared
-- by name rather than left to an implicit `sqlite_autoindex_*` so they appear in
-- `sqlite_master` under a readable name and therefore inside the canonical schema
-- fingerprint, which filters implicit indexes out.
--
-- `omnivia_idx_blob_objects_identity` is not redundant with the two-column key beside
-- it: it is the parent of the staged-source reference, and including the length in the
-- key is what makes "the blob agrees about the bytes" a foreign key rather than a
-- comment.
CREATE UNIQUE INDEX IF NOT EXISTS omnivia_idx_blob_objects_workspace_digest
    ON omnivia_blob_objects (workspace_id, content_digest);
CREATE UNIQUE INDEX IF NOT EXISTS omnivia_idx_blob_objects_identity
    ON omnivia_blob_objects (workspace_id, content_digest, content_length_bytes);
CREATE UNIQUE INDEX IF NOT EXISTS omnivia_idx_staged_sources_ref_workspace
    ON omnivia_staged_sources (staged_source_ref, workspace_id);
CREATE UNIQUE INDEX IF NOT EXISTS omnivia_idx_evidence_artifacts_id_workspace
    ON omnivia_evidence_artifacts (evidence_id, workspace_id);
CREATE UNIQUE INDEX IF NOT EXISTS omnivia_idx_evidence_artifacts_identity_source
    ON omnivia_evidence_artifacts
        (evidence_id, workspace_id, source_kind, source_native_id);
CREATE UNIQUE INDEX IF NOT EXISTS omnivia_idx_evidence_artifacts_identity_blob
    ON omnivia_evidence_artifacts (evidence_id, workspace_id, blob_content_digest);
CREATE UNIQUE INDEX IF NOT EXISTS omnivia_idx_evidence_provenance_events_identity_source
    ON omnivia_evidence_provenance_events
        (provenance_event_id, evidence_id, workspace_id, source_kind, source_native_id);
CREATE UNIQUE INDEX IF NOT EXISTS omnivia_idx_normalized_source_records_id_evidence
    ON omnivia_normalized_source_records
        (normalized_record_id, evidence_id, workspace_id);

-- One sequence value per parent stream. These are the durable half of "monotonic":
-- the index refuses a duplicate, the INSERT trigger refuses a backwards or
-- non-positive one, and between them the sequence is a total order over the stream
-- that no clock collision can disturb.
CREATE UNIQUE INDEX IF NOT EXISTS omnivia_idx_blob_integrity_events_digest_sequence
    ON omnivia_blob_integrity_events
        (workspace_id, content_digest, integrity_sequence);
CREATE UNIQUE INDEX IF NOT EXISTS omnivia_idx_evidence_permission_labels_sequence
    ON omnivia_evidence_permission_labels (evidence_id, label_sequence);
CREATE UNIQUE INDEX IF NOT EXISTS omnivia_idx_evidence_provenance_events_sequence
    ON omnivia_evidence_provenance_events (evidence_id, provenance_sequence);
CREATE UNIQUE INDEX IF NOT EXISTS omnivia_idx_evidence_event_references_ordinal
    ON omnivia_evidence_event_references (provenance_event_id, reference_ordinal);
CREATE UNIQUE INDEX IF NOT EXISTS omnivia_idx_normalized_source_records_sequence
    ON omnivia_normalized_source_records (evidence_id, record_sequence);
CREATE UNIQUE INDEX IF NOT EXISTS omnivia_idx_normalized_source_spans_sequence
    ON omnivia_normalized_source_spans (normalized_record_id, span_sequence);

-- Material read paths. Each of these is a question something will actually ask of an
-- append-preserved table that only grows, and without them the answer is a full scan:
-- which digests failed their last integrity pass, which stagings are still unverified,
-- which artifact captured this native source, which artifacts share these bytes, and
-- what one import run produced.
CREATE INDEX IF NOT EXISTS omnivia_idx_blob_integrity_events_workspace_outcome_time
    ON omnivia_blob_integrity_events (workspace_id, outcome, checked_at_us);
CREATE INDEX IF NOT EXISTS omnivia_idx_staged_sources_workspace_outcome
    ON omnivia_staged_sources (workspace_id, staging_outcome, recorded_at_us);
CREATE INDEX IF NOT EXISTS omnivia_idx_evidence_artifacts_workspace_source
    ON omnivia_evidence_artifacts (workspace_id, source_kind, source_native_id);
CREATE INDEX IF NOT EXISTS omnivia_idx_evidence_artifacts_workspace_blob
    ON omnivia_evidence_artifacts (workspace_id, blob_content_digest);
CREATE INDEX IF NOT EXISTS omnivia_idx_evidence_artifacts_import_run
    ON omnivia_evidence_artifacts (import_run_id);

-- Twenty-seven statement triggers: nine tables, three statement classes each. The
-- set is exact, because a table can look guarded while one statement class walks
-- past it. A twenty-eighth CREATE TRIGGER follows them, but it creates no new object:
-- it recreates one inherited guard under its existing name, and is documented where
-- it stands.
--
-- INSERT carries the complete predicate 0005 arrived at -- the connection-local
-- authority function, the guard row, the authoritative workspace state and the lease
-- that agrees with both -- plus the workspace binding, so a row cannot be written
-- into a workspace other than the one this database is. It is evaluated inside the
-- writing transaction, so an owner whose generation was taken over between BEGIN and
-- COMMIT fails on the predicate rather than committing under authority it lost. A
-- stock `sqlite3` client fails on `omnivia_service_writer()` before a row is touched,
-- because that function is connection-local and cannot be created by writing rows.
--
-- Six of the INSERT triggers carry one further condition -- the stream's sequence
-- must strictly advance -- and the evidence trigger carries two, since a foreign key
-- can prove a durable job and a staged source exist but not that the job is an
-- `ingestion.import` or that the staging verified. Each of those triggers reports
-- which rule it was, so a refusal is diagnosable: the body is a series of
-- `RAISE ... WHERE`, the authority clause first, which keeps a writer with no
-- authority hearing about authority rather than about a sequence it was never
-- allowed to write.
--
-- UPDATE and DELETE carry no predicate at all. There is no condition under which
-- rewriting or removing a blob identity, an integrity observation, a staged source,
-- an evidence artifact, a label fact, a provenance event, a reference or a normalized
-- record is correct, so there is no condition to write down -- including for the
-- current fenced owner. A correction is a new row.

CREATE TRIGGER IF NOT EXISTS omnivia_guard_blob_objects_insert
BEFORE INSERT ON omnivia_blob_objects
WHEN omnivia_service_writer() IS NOT 1
   OR NOT EXISTS (
    SELECT 1
    FROM omnivia_mutation_guard g
    JOIN omnivia_workspace_state s ON s.singleton = 1
    JOIN omnivia_workspace_lease l ON l.singleton = 1
    WHERE g.singleton = 1
      AND g.fencing_generation = s.fencing_generation
      AND g.workspace_id       = s.workspace_id
      AND l.fencing_generation = g.fencing_generation
      AND l.workspace_id       = g.workspace_id
      AND l.service_instance_id = g.service_instance_id
      AND l.lifecycle IN ('acquiring', 'held', 'draining')
)
   OR NEW.workspace_id IS NOT (
    SELECT workspace_id FROM omnivia_workspace_state WHERE singleton = 1
)
   OR EXISTS (
    SELECT 1 FROM omnivia_blob_objects
    WHERE workspace_id = NEW.workspace_id AND content_digest = NEW.content_digest
)
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_blob_objects')
    WHERE omnivia_service_writer() IS NOT 1
       OR NOT EXISTS (
        SELECT 1
        FROM omnivia_mutation_guard g
        JOIN omnivia_workspace_state s ON s.singleton = 1
        JOIN omnivia_workspace_lease l ON l.singleton = 1
        WHERE g.singleton = 1
          AND g.fencing_generation = s.fencing_generation
          AND g.workspace_id       = s.workspace_id
          AND l.fencing_generation = g.fencing_generation
          AND l.workspace_id       = g.workspace_id
          AND l.service_instance_id = g.service_instance_id
          AND l.lifecycle IN ('acquiring', 'held', 'draining')
    )
       OR NEW.workspace_id IS NOT (
        SELECT workspace_id FROM omnivia_workspace_state WHERE singleton = 1
    );
    SELECT RAISE(ABORT, 'omnivia: omnivia_blob_objects (workspace_id, content_digest) identity is immutable; duplicate insert refused')
    WHERE EXISTS (
        SELECT 1 FROM omnivia_blob_objects
        WHERE workspace_id = NEW.workspace_id AND content_digest = NEW.content_digest
    );
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_blob_objects_update
BEFORE UPDATE ON omnivia_blob_objects
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_blob_objects is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_blob_objects_delete
BEFORE DELETE ON omnivia_blob_objects
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_blob_objects is append-only; DELETE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_blob_integrity_events_insert
BEFORE INSERT ON omnivia_blob_integrity_events
WHEN omnivia_service_writer() IS NOT 1
   OR NOT EXISTS (
    SELECT 1
    FROM omnivia_mutation_guard g
    JOIN omnivia_workspace_state s ON s.singleton = 1
    JOIN omnivia_workspace_lease l ON l.singleton = 1
    WHERE g.singleton = 1
      AND g.fencing_generation = s.fencing_generation
      AND g.workspace_id       = s.workspace_id
      AND l.fencing_generation = g.fencing_generation
      AND l.workspace_id       = g.workspace_id
      AND l.service_instance_id = g.service_instance_id
      AND l.lifecycle IN ('acquiring', 'held', 'draining')
)
   OR NEW.workspace_id IS NOT (
    SELECT workspace_id FROM omnivia_workspace_state WHERE singleton = 1
)
   OR NEW.integrity_sequence <= COALESCE((
        SELECT MAX(integrity_sequence) FROM omnivia_blob_integrity_events
        WHERE workspace_id = NEW.workspace_id AND content_digest = NEW.content_digest
    ), 0)
   OR EXISTS (
    SELECT 1 FROM omnivia_blob_integrity_events
    WHERE integrity_event_id = NEW.integrity_event_id
)
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_blob_integrity_events')
    WHERE omnivia_service_writer() IS NOT 1
       OR NOT EXISTS (
        SELECT 1
        FROM omnivia_mutation_guard g
        JOIN omnivia_workspace_state s ON s.singleton = 1
        JOIN omnivia_workspace_lease l ON l.singleton = 1
        WHERE g.singleton = 1
          AND g.fencing_generation = s.fencing_generation
          AND g.workspace_id       = s.workspace_id
          AND l.fencing_generation = g.fencing_generation
          AND l.workspace_id       = g.workspace_id
          AND l.service_instance_id = g.service_instance_id
          AND l.lifecycle IN ('acquiring', 'held', 'draining')
    )
       OR NEW.workspace_id IS NOT (
        SELECT workspace_id FROM omnivia_workspace_state WHERE singleton = 1
    );
    SELECT RAISE(ABORT, 'omnivia: omnivia_blob_integrity_events.integrity_sequence must advance within its parent stream')
    WHERE NEW.integrity_sequence <= COALESCE((
        SELECT MAX(integrity_sequence) FROM omnivia_blob_integrity_events
        WHERE workspace_id = NEW.workspace_id AND content_digest = NEW.content_digest
    ), 0);
    SELECT RAISE(ABORT, 'omnivia: omnivia_blob_integrity_events.integrity_event_id identity is immutable; duplicate insert refused')
    WHERE EXISTS (
        SELECT 1 FROM omnivia_blob_integrity_events
        WHERE integrity_event_id = NEW.integrity_event_id
    );
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_blob_integrity_events_update
BEFORE UPDATE ON omnivia_blob_integrity_events
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_blob_integrity_events is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_blob_integrity_events_delete
BEFORE DELETE ON omnivia_blob_integrity_events
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_blob_integrity_events is append-only; DELETE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_staged_sources_insert
BEFORE INSERT ON omnivia_staged_sources
WHEN omnivia_service_writer() IS NOT 1
   OR NOT EXISTS (
    SELECT 1
    FROM omnivia_mutation_guard g
    JOIN omnivia_workspace_state s ON s.singleton = 1
    JOIN omnivia_workspace_lease l ON l.singleton = 1
    WHERE g.singleton = 1
      AND g.fencing_generation = s.fencing_generation
      AND g.workspace_id       = s.workspace_id
      AND l.fencing_generation = g.fencing_generation
      AND l.workspace_id       = g.workspace_id
      AND l.service_instance_id = g.service_instance_id
      AND l.lifecycle IN ('acquiring', 'held', 'draining')
)
   OR NEW.workspace_id IS NOT (
    SELECT workspace_id FROM omnivia_workspace_state WHERE singleton = 1
)
   OR EXISTS (
    SELECT 1 FROM omnivia_staged_sources
    WHERE staged_source_ref = NEW.staged_source_ref
)
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_staged_sources')
    WHERE omnivia_service_writer() IS NOT 1
       OR NOT EXISTS (
        SELECT 1
        FROM omnivia_mutation_guard g
        JOIN omnivia_workspace_state s ON s.singleton = 1
        JOIN omnivia_workspace_lease l ON l.singleton = 1
        WHERE g.singleton = 1
          AND g.fencing_generation = s.fencing_generation
          AND g.workspace_id       = s.workspace_id
          AND l.fencing_generation = g.fencing_generation
          AND l.workspace_id       = g.workspace_id
          AND l.service_instance_id = g.service_instance_id
          AND l.lifecycle IN ('acquiring', 'held', 'draining')
    )
       OR NEW.workspace_id IS NOT (
        SELECT workspace_id FROM omnivia_workspace_state WHERE singleton = 1
    );
    SELECT RAISE(ABORT, 'omnivia: omnivia_staged_sources.staged_source_ref identity is immutable; duplicate insert refused')
    WHERE EXISTS (
        SELECT 1 FROM omnivia_staged_sources
        WHERE staged_source_ref = NEW.staged_source_ref
    );
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_staged_sources_update
BEFORE UPDATE ON omnivia_staged_sources
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_staged_sources is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_staged_sources_delete
BEFORE DELETE ON omnivia_staged_sources
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_staged_sources is append-only; DELETE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_evidence_artifacts_insert
BEFORE INSERT ON omnivia_evidence_artifacts
WHEN omnivia_service_writer() IS NOT 1
   OR NOT EXISTS (
    SELECT 1
    FROM omnivia_mutation_guard g
    JOIN omnivia_workspace_state s ON s.singleton = 1
    JOIN omnivia_workspace_lease l ON l.singleton = 1
    WHERE g.singleton = 1
      AND g.fencing_generation = s.fencing_generation
      AND g.workspace_id       = s.workspace_id
      AND l.fencing_generation = g.fencing_generation
      AND l.workspace_id       = g.workspace_id
      AND l.service_instance_id = g.service_instance_id
      AND l.lifecycle IN ('acquiring', 'held', 'draining')
)
   OR NEW.workspace_id IS NOT (
    SELECT workspace_id FROM omnivia_workspace_state WHERE singleton = 1
)
   OR NEW.import_run_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM omnivia_durable_jobs
    WHERE job_id = NEW.import_run_id AND job_type = 'ingestion.import'
)
   OR NEW.staged_source_ref IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM omnivia_staged_sources
    WHERE staged_source_ref = NEW.staged_source_ref
      AND workspace_id        = NEW.workspace_id
      AND staging_outcome     = 'verified'
      AND source_kind         = NEW.source_kind
      AND blob_content_digest = NEW.blob_content_digest
)
   OR EXISTS (
    SELECT 1 FROM omnivia_evidence_artifacts
    WHERE evidence_id = NEW.evidence_id
)
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_evidence_artifacts')
    WHERE omnivia_service_writer() IS NOT 1
       OR NOT EXISTS (
        SELECT 1
        FROM omnivia_mutation_guard g
        JOIN omnivia_workspace_state s ON s.singleton = 1
        JOIN omnivia_workspace_lease l ON l.singleton = 1
        WHERE g.singleton = 1
          AND g.fencing_generation = s.fencing_generation
          AND g.workspace_id       = s.workspace_id
          AND l.fencing_generation = g.fencing_generation
          AND l.workspace_id       = g.workspace_id
          AND l.service_instance_id = g.service_instance_id
          AND l.lifecycle IN ('acquiring', 'held', 'draining')
    )
       OR NEW.workspace_id IS NOT (
        SELECT workspace_id FROM omnivia_workspace_state WHERE singleton = 1
    );
    SELECT RAISE(ABORT, 'omnivia: omnivia_evidence_artifacts.import_run_id must name an existing ingestion.import durable job')
    WHERE NEW.import_run_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM omnivia_durable_jobs
        WHERE job_id = NEW.import_run_id AND job_type = 'ingestion.import'
    );
    SELECT RAISE(ABORT, 'omnivia: omnivia_evidence_artifacts.staged_source_ref must name a verified staged source in this workspace with the same source_kind and blob content digest')
    WHERE NEW.staged_source_ref IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM omnivia_staged_sources
        WHERE staged_source_ref = NEW.staged_source_ref
          AND workspace_id        = NEW.workspace_id
          AND staging_outcome     = 'verified'
          AND source_kind         = NEW.source_kind
          AND blob_content_digest = NEW.blob_content_digest
    );
    SELECT RAISE(ABORT, 'omnivia: omnivia_evidence_artifacts.evidence_id identity is immutable; duplicate insert refused')
    WHERE EXISTS (
        SELECT 1 FROM omnivia_evidence_artifacts
        WHERE evidence_id = NEW.evidence_id
    );
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_evidence_artifacts_update
BEFORE UPDATE ON omnivia_evidence_artifacts
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_evidence_artifacts is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_evidence_artifacts_delete
BEFORE DELETE ON omnivia_evidence_artifacts
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_evidence_artifacts is append-only; DELETE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_evidence_permission_labels_insert
BEFORE INSERT ON omnivia_evidence_permission_labels
WHEN omnivia_service_writer() IS NOT 1
   OR NOT EXISTS (
    SELECT 1
    FROM omnivia_mutation_guard g
    JOIN omnivia_workspace_state s ON s.singleton = 1
    JOIN omnivia_workspace_lease l ON l.singleton = 1
    WHERE g.singleton = 1
      AND g.fencing_generation = s.fencing_generation
      AND g.workspace_id       = s.workspace_id
      AND l.fencing_generation = g.fencing_generation
      AND l.workspace_id       = g.workspace_id
      AND l.service_instance_id = g.service_instance_id
      AND l.lifecycle IN ('acquiring', 'held', 'draining')
)
   OR NEW.workspace_id IS NOT (
    SELECT workspace_id FROM omnivia_workspace_state WHERE singleton = 1
)
   OR NEW.label_sequence <= COALESCE((
        SELECT MAX(label_sequence) FROM omnivia_evidence_permission_labels
        WHERE evidence_id = NEW.evidence_id
    ), 0)
   OR EXISTS (
    SELECT 1 FROM omnivia_evidence_permission_labels
    WHERE label_event_id = NEW.label_event_id
)
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_evidence_permission_labels')
    WHERE omnivia_service_writer() IS NOT 1
       OR NOT EXISTS (
        SELECT 1
        FROM omnivia_mutation_guard g
        JOIN omnivia_workspace_state s ON s.singleton = 1
        JOIN omnivia_workspace_lease l ON l.singleton = 1
        WHERE g.singleton = 1
          AND g.fencing_generation = s.fencing_generation
          AND g.workspace_id       = s.workspace_id
          AND l.fencing_generation = g.fencing_generation
          AND l.workspace_id       = g.workspace_id
          AND l.service_instance_id = g.service_instance_id
          AND l.lifecycle IN ('acquiring', 'held', 'draining')
    )
       OR NEW.workspace_id IS NOT (
        SELECT workspace_id FROM omnivia_workspace_state WHERE singleton = 1
    );
    SELECT RAISE(ABORT, 'omnivia: omnivia_evidence_permission_labels.label_sequence must advance within its parent stream')
    WHERE NEW.label_sequence <= COALESCE((
        SELECT MAX(label_sequence) FROM omnivia_evidence_permission_labels
        WHERE evidence_id = NEW.evidence_id
    ), 0);
    SELECT RAISE(ABORT, 'omnivia: omnivia_evidence_permission_labels.label_event_id identity is immutable; duplicate insert refused')
    WHERE EXISTS (
        SELECT 1 FROM omnivia_evidence_permission_labels
        WHERE label_event_id = NEW.label_event_id
    );
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_evidence_permission_labels_update
BEFORE UPDATE ON omnivia_evidence_permission_labels
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_evidence_permission_labels is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_evidence_permission_labels_delete
BEFORE DELETE ON omnivia_evidence_permission_labels
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_evidence_permission_labels is append-only; DELETE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_evidence_provenance_events_insert
BEFORE INSERT ON omnivia_evidence_provenance_events
WHEN omnivia_service_writer() IS NOT 1
   OR NOT EXISTS (
    SELECT 1
    FROM omnivia_mutation_guard g
    JOIN omnivia_workspace_state s ON s.singleton = 1
    JOIN omnivia_workspace_lease l ON l.singleton = 1
    WHERE g.singleton = 1
      AND g.fencing_generation = s.fencing_generation
      AND g.workspace_id       = s.workspace_id
      AND l.fencing_generation = g.fencing_generation
      AND l.workspace_id       = g.workspace_id
      AND l.service_instance_id = g.service_instance_id
      AND l.lifecycle IN ('acquiring', 'held', 'draining')
)
   OR NEW.workspace_id IS NOT (
    SELECT workspace_id FROM omnivia_workspace_state WHERE singleton = 1
)
   OR NEW.provenance_sequence <= COALESCE((
        SELECT MAX(provenance_sequence) FROM omnivia_evidence_provenance_events
        WHERE evidence_id = NEW.evidence_id
    ), 0)
   OR EXISTS (
    SELECT 1 FROM omnivia_evidence_provenance_events
    WHERE provenance_event_id = NEW.provenance_event_id
)
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_evidence_provenance_events')
    WHERE omnivia_service_writer() IS NOT 1
       OR NOT EXISTS (
        SELECT 1
        FROM omnivia_mutation_guard g
        JOIN omnivia_workspace_state s ON s.singleton = 1
        JOIN omnivia_workspace_lease l ON l.singleton = 1
        WHERE g.singleton = 1
          AND g.fencing_generation = s.fencing_generation
          AND g.workspace_id       = s.workspace_id
          AND l.fencing_generation = g.fencing_generation
          AND l.workspace_id       = g.workspace_id
          AND l.service_instance_id = g.service_instance_id
          AND l.lifecycle IN ('acquiring', 'held', 'draining')
    )
       OR NEW.workspace_id IS NOT (
        SELECT workspace_id FROM omnivia_workspace_state WHERE singleton = 1
    );
    SELECT RAISE(ABORT, 'omnivia: omnivia_evidence_provenance_events.provenance_sequence must advance within its parent stream')
    WHERE NEW.provenance_sequence <= COALESCE((
        SELECT MAX(provenance_sequence) FROM omnivia_evidence_provenance_events
        WHERE evidence_id = NEW.evidence_id
    ), 0);
    SELECT RAISE(ABORT, 'omnivia: omnivia_evidence_provenance_events.provenance_event_id identity is immutable; duplicate insert refused')
    WHERE EXISTS (
        SELECT 1 FROM omnivia_evidence_provenance_events
        WHERE provenance_event_id = NEW.provenance_event_id
    );
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_evidence_provenance_events_update
BEFORE UPDATE ON omnivia_evidence_provenance_events
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_evidence_provenance_events is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_evidence_provenance_events_delete
BEFORE DELETE ON omnivia_evidence_provenance_events
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_evidence_provenance_events is append-only; DELETE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_evidence_event_references_insert
BEFORE INSERT ON omnivia_evidence_event_references
WHEN omnivia_service_writer() IS NOT 1
   OR NOT EXISTS (
    SELECT 1
    FROM omnivia_mutation_guard g
    JOIN omnivia_workspace_state s ON s.singleton = 1
    JOIN omnivia_workspace_lease l ON l.singleton = 1
    WHERE g.singleton = 1
      AND g.fencing_generation = s.fencing_generation
      AND g.workspace_id       = s.workspace_id
      AND l.fencing_generation = g.fencing_generation
      AND l.workspace_id       = g.workspace_id
      AND l.service_instance_id = g.service_instance_id
      AND l.lifecycle IN ('acquiring', 'held', 'draining')
)
   OR NEW.workspace_id IS NOT (
    SELECT workspace_id FROM omnivia_workspace_state WHERE singleton = 1
)
   OR NEW.reference_ordinal <= COALESCE((
        SELECT MAX(reference_ordinal) FROM omnivia_evidence_event_references
        WHERE provenance_event_id = NEW.provenance_event_id
    ), 0)
   OR EXISTS (
    SELECT 1 FROM omnivia_evidence_event_references
    WHERE event_reference_id = NEW.event_reference_id
)
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_evidence_event_references')
    WHERE omnivia_service_writer() IS NOT 1
       OR NOT EXISTS (
        SELECT 1
        FROM omnivia_mutation_guard g
        JOIN omnivia_workspace_state s ON s.singleton = 1
        JOIN omnivia_workspace_lease l ON l.singleton = 1
        WHERE g.singleton = 1
          AND g.fencing_generation = s.fencing_generation
          AND g.workspace_id       = s.workspace_id
          AND l.fencing_generation = g.fencing_generation
          AND l.workspace_id       = g.workspace_id
          AND l.service_instance_id = g.service_instance_id
          AND l.lifecycle IN ('acquiring', 'held', 'draining')
    )
       OR NEW.workspace_id IS NOT (
        SELECT workspace_id FROM omnivia_workspace_state WHERE singleton = 1
    );
    SELECT RAISE(ABORT, 'omnivia: omnivia_evidence_event_references.reference_ordinal must advance within its parent stream')
    WHERE NEW.reference_ordinal <= COALESCE((
        SELECT MAX(reference_ordinal) FROM omnivia_evidence_event_references
        WHERE provenance_event_id = NEW.provenance_event_id
    ), 0);
    SELECT RAISE(ABORT, 'omnivia: omnivia_evidence_event_references.event_reference_id identity is immutable; duplicate insert refused')
    WHERE EXISTS (
        SELECT 1 FROM omnivia_evidence_event_references
        WHERE event_reference_id = NEW.event_reference_id
    );
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_evidence_event_references_update
BEFORE UPDATE ON omnivia_evidence_event_references
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_evidence_event_references is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_evidence_event_references_delete
BEFORE DELETE ON omnivia_evidence_event_references
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_evidence_event_references is append-only; DELETE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_normalized_source_records_insert
BEFORE INSERT ON omnivia_normalized_source_records
WHEN omnivia_service_writer() IS NOT 1
   OR NOT EXISTS (
    SELECT 1
    FROM omnivia_mutation_guard g
    JOIN omnivia_workspace_state s ON s.singleton = 1
    JOIN omnivia_workspace_lease l ON l.singleton = 1
    WHERE g.singleton = 1
      AND g.fencing_generation = s.fencing_generation
      AND g.workspace_id       = s.workspace_id
      AND l.fencing_generation = g.fencing_generation
      AND l.workspace_id       = g.workspace_id
      AND l.service_instance_id = g.service_instance_id
      AND l.lifecycle IN ('acquiring', 'held', 'draining')
)
   OR NEW.workspace_id IS NOT (
    SELECT workspace_id FROM omnivia_workspace_state WHERE singleton = 1
)
   OR NEW.record_sequence <= COALESCE((
        SELECT MAX(record_sequence) FROM omnivia_normalized_source_records
        WHERE evidence_id = NEW.evidence_id
    ), 0)
   OR EXISTS (
    SELECT 1 FROM omnivia_normalized_source_records
    WHERE normalized_record_id = NEW.normalized_record_id
)
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_normalized_source_records')
    WHERE omnivia_service_writer() IS NOT 1
       OR NOT EXISTS (
        SELECT 1
        FROM omnivia_mutation_guard g
        JOIN omnivia_workspace_state s ON s.singleton = 1
        JOIN omnivia_workspace_lease l ON l.singleton = 1
        WHERE g.singleton = 1
          AND g.fencing_generation = s.fencing_generation
          AND g.workspace_id       = s.workspace_id
          AND l.fencing_generation = g.fencing_generation
          AND l.workspace_id       = g.workspace_id
          AND l.service_instance_id = g.service_instance_id
          AND l.lifecycle IN ('acquiring', 'held', 'draining')
    )
       OR NEW.workspace_id IS NOT (
        SELECT workspace_id FROM omnivia_workspace_state WHERE singleton = 1
    );
    SELECT RAISE(ABORT, 'omnivia: omnivia_normalized_source_records.record_sequence must advance within its parent stream')
    WHERE NEW.record_sequence <= COALESCE((
        SELECT MAX(record_sequence) FROM omnivia_normalized_source_records
        WHERE evidence_id = NEW.evidence_id
    ), 0);
    SELECT RAISE(ABORT, 'omnivia: omnivia_normalized_source_records.normalized_record_id identity is immutable; duplicate insert refused')
    WHERE EXISTS (
        SELECT 1 FROM omnivia_normalized_source_records
        WHERE normalized_record_id = NEW.normalized_record_id
    );
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_normalized_source_records_update
BEFORE UPDATE ON omnivia_normalized_source_records
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_normalized_source_records is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_normalized_source_records_delete
BEFORE DELETE ON omnivia_normalized_source_records
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_normalized_source_records is append-only; DELETE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_normalized_source_spans_insert
BEFORE INSERT ON omnivia_normalized_source_spans
WHEN omnivia_service_writer() IS NOT 1
   OR NOT EXISTS (
    SELECT 1
    FROM omnivia_mutation_guard g
    JOIN omnivia_workspace_state s ON s.singleton = 1
    JOIN omnivia_workspace_lease l ON l.singleton = 1
    WHERE g.singleton = 1
      AND g.fencing_generation = s.fencing_generation
      AND g.workspace_id       = s.workspace_id
      AND l.fencing_generation = g.fencing_generation
      AND l.workspace_id       = g.workspace_id
      AND l.service_instance_id = g.service_instance_id
      AND l.lifecycle IN ('acquiring', 'held', 'draining')
)
   OR NEW.workspace_id IS NOT (
    SELECT workspace_id FROM omnivia_workspace_state WHERE singleton = 1
)
   OR NEW.span_sequence <= COALESCE((
        SELECT MAX(span_sequence) FROM omnivia_normalized_source_spans
        WHERE normalized_record_id = NEW.normalized_record_id
    ), 0)
   OR EXISTS (
    SELECT 1 FROM omnivia_normalized_source_spans
    WHERE normalized_span_id = NEW.normalized_span_id
)
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_normalized_source_spans')
    WHERE omnivia_service_writer() IS NOT 1
       OR NOT EXISTS (
        SELECT 1
        FROM omnivia_mutation_guard g
        JOIN omnivia_workspace_state s ON s.singleton = 1
        JOIN omnivia_workspace_lease l ON l.singleton = 1
        WHERE g.singleton = 1
          AND g.fencing_generation = s.fencing_generation
          AND g.workspace_id       = s.workspace_id
          AND l.fencing_generation = g.fencing_generation
          AND l.workspace_id       = g.workspace_id
          AND l.service_instance_id = g.service_instance_id
          AND l.lifecycle IN ('acquiring', 'held', 'draining')
    )
       OR NEW.workspace_id IS NOT (
        SELECT workspace_id FROM omnivia_workspace_state WHERE singleton = 1
    );
    SELECT RAISE(ABORT, 'omnivia: omnivia_normalized_source_spans.span_sequence must advance within its parent stream')
    WHERE NEW.span_sequence <= COALESCE((
        SELECT MAX(span_sequence) FROM omnivia_normalized_source_spans
        WHERE normalized_record_id = NEW.normalized_record_id
    ), 0);
    SELECT RAISE(ABORT, 'omnivia: omnivia_normalized_source_spans.normalized_span_id identity is immutable; duplicate insert refused')
    WHERE EXISTS (
        SELECT 1 FROM omnivia_normalized_source_spans
        WHERE normalized_span_id = NEW.normalized_span_id
    );
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_normalized_source_spans_update
BEFORE UPDATE ON omnivia_normalized_source_spans
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_normalized_source_spans is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_normalized_source_spans_delete
BEFORE DELETE ON omnivia_normalized_source_spans
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_normalized_source_spans is append-only; DELETE is never permitted');
END;

-- The one inherited guard this slice has to reopen, and the last statement pair in
-- the file.
--
-- `omnivia_evidence_artifacts.import_run_id` must name an `ingestion.import` durable
-- job. The evidence INSERT trigger establishes that at capture, and a foreign key
-- keeps the job row alive, but neither says anything about the job's *type* a moment
-- later: `UPDATE omnivia_durable_jobs SET job_type = 'maintenance.compaction'` would
-- leave every artifact that named it still pointing at a job that is no longer an
-- import, and the check that was supposed to establish provenance would have been
-- true only at the instant nobody was looking. A rule that holds only at insertion is
-- not the rule this schema claims.
--
-- SQLite cannot alter a trigger in place, so the inherited guard is dropped and
-- recreated under the same name -- the third time this exact trigger has been
-- rewritten that way, after `0004` bound it to the lease and `0005` bound it to
-- connection-local authority. Everything those two established is reproduced here
-- verbatim: the same complete authority predicate, over the same guard, workspace
-- state and lease, aborting with the same message. Nothing about durable jobs that
-- was permitted before is refused now, no job column changes, no other trigger is
-- touched, and no new trigger *name* enters the schema -- so this pair adds one more
-- CREATE TRIGGER statement to the migration and not one more trigger object to the
-- database.
--
-- What it adds is a second, narrow refusal: a job's type may not change while
-- evidence references that job. Any other update to a referenced job -- state,
-- payload, timestamps, the claiming instance -- is untouched, because the invariant
-- is about what kind of work the job was, not about the job standing still. Retagging
-- a job no evidence references stays ordinary.
DROP TRIGGER IF EXISTS omnivia_guard_durable_jobs_update;

CREATE TRIGGER omnivia_guard_durable_jobs_update
BEFORE UPDATE ON omnivia_durable_jobs
WHEN omnivia_service_writer() IS NOT 1
   OR NOT EXISTS (
    SELECT 1
    FROM omnivia_mutation_guard g
    JOIN omnivia_workspace_state s ON s.singleton = 1
    JOIN omnivia_workspace_lease l ON l.singleton = 1
    WHERE g.singleton = 1
      AND g.fencing_generation = s.fencing_generation
      AND g.workspace_id       = s.workspace_id
      AND l.fencing_generation = g.fencing_generation
      AND l.workspace_id       = g.workspace_id
      AND l.service_instance_id = g.service_instance_id
      AND l.lifecycle IN ('acquiring', 'held', 'draining')
)
   OR (NEW.job_type IS NOT OLD.job_type AND EXISTS (
        SELECT 1 FROM omnivia_evidence_artifacts
        WHERE import_run_id = OLD.job_id
    ))
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded UPDATE on omnivia_durable_jobs')
    WHERE omnivia_service_writer() IS NOT 1
       OR NOT EXISTS (
        SELECT 1
        FROM omnivia_mutation_guard g
        JOIN omnivia_workspace_state s ON s.singleton = 1
        JOIN omnivia_workspace_lease l ON l.singleton = 1
        WHERE g.singleton = 1
          AND g.fencing_generation = s.fencing_generation
          AND g.workspace_id       = s.workspace_id
          AND l.fencing_generation = g.fencing_generation
          AND l.workspace_id       = g.workspace_id
          AND l.service_instance_id = g.service_instance_id
          AND l.lifecycle IN ('acquiring', 'held', 'draining')
    );
    SELECT RAISE(ABORT, 'omnivia: omnivia_durable_jobs.job_type may not change while evidence references this job')
    WHERE NEW.job_type IS NOT OLD.job_type AND EXISTS (
        SELECT 1 FROM omnivia_evidence_artifacts
        WHERE import_run_id = OLD.job_id
    );
END;
