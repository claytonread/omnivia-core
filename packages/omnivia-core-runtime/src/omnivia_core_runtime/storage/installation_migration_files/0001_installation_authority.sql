-- The durable installation catalogue: identity, ownership, workspaces, allocations,
-- audit, idempotency and spent grants (V06-5 S0, CP-S0-I-A).
--
-- This schema belongs to a separate database from the workspace one, at
-- `<installation-state>/catalogue/installation.sqlite`, and it never shares a
-- migration ledger, a fencing generation or a lock with any workspace. A workspace is
-- portable and may be copied, moved or replaced; the catalogue that says which
-- workspaces this installation authorised, and under whose authority they were
-- created, is not. Recording installation facts in a workspace would make the
-- installation's identity a property of whichever workspace happened to be open, which
-- is the mistake this file exists to make impossible.
--
-- Eight tables:
--
--   omnivia_installation_state              one identity, one schema version, one owner
--   omnivia_installation_schema_migrations  what ran, pinned by checksum
--   omnivia_installation_workspaces         the authorised workspace inventory
--   omnivia_installation_allocations        preparing | active | failed_recoverable
--   omnivia_installation_audit_events       what the server decided, append-only
--   omnivia_installation_idempotency_claims one scoped key, frozen to one request
--   omnivia_installation_idempotency_outcomes  the single terminal answer
--   omnivia_installation_grant_uses         the grant an accepted execution spent
--
-- Authority. Every write predicate is the same three facts, and none of them has a
-- permissive default: the connection-local `omnivia_installation_writer()` function
-- must be present and return 1, so a stock `sqlite3` client that walks in on this file
-- fails at statement preparation with "no such function"; the singleton state row must
-- name a current owner, so a catalogue nobody has acquired is not writable at all; and
-- the row being written must carry exactly the installation id and fencing generation
-- that state currently holds, so a writer whose generation was superseded between its
-- own check and its INSERT is refused by the database rather than by the code path that
-- lost the race. The bootstrap mutex is not represented here on purpose: it coordinates
-- launchers and grants nothing, and a schema that accepted it as evidence of ownership
-- would be accepting a lock any client may take.
--
-- Identity. `installation_id` is minted once, when this schema is created, and the
-- UPDATE trigger refuses any statement that changes it. Reopening the catalogue
-- advances the fencing generation and records the new owner; it never mints a second
-- identity, and no workspace or service instance can mint one at all.
--
-- Append-only means append-only. The audit events, both idempotency tables, the
-- workspace inventory and the grant-use records reject UPDATE and DELETE
-- unconditionally -- for the current owner as much as for anyone else, because "the
-- owner may rewrite history" is the one property this evidence cannot have. Only the
-- state row and an allocation's lifecycle are mutable, and both are mutable along
-- exactly one declared path.
--
-- Timestamps are signed 64-bit UTC microseconds since the Unix epoch, in `*_at_us`,
-- with `typeof(...) = 'integer'` so a fractional microsecond is refused rather than
-- silently rounded by column affinity. Digests are `sha256:<64 lowercase hex>` over
-- exact Core canonical-JSON bytes. Text columns are bounded at the contract's own
-- 128-character ceiling except the three that carry a document or a path.

-- Every comment in this file sits *between* statements, never inside one. SQLite stores
-- a statement's original text in `sqlite_master`, and the migrator executes statements
-- one at a time with comments stripped so a migration stays inside the caller's
-- transaction -- so a comment inside a CREATE would make the applied schema differ,
-- byte for byte, from the canonical fingerprint built by replaying the same artifact
-- with `executescript`, and a clean catalogue would be reported as drifted.

-- The one row that is the installation. `owner_instance_id` is the exact current
-- writer and `fencing_generation` is the durable generation it acquired; every other
-- table binds both, so authority is a stored fact rather than a value held in a
-- process's memory. A generation is never reused and never decremented, which is what
-- makes a resumed predecessor's generation permanently stale.
CREATE TABLE IF NOT EXISTS omnivia_installation_state (
    singleton                    INTEGER NOT NULL PRIMARY KEY,
    installation_id              TEXT    NOT NULL,
    installation_format_version  TEXT    NOT NULL,
    fencing_generation           INTEGER NOT NULL,
    owner_instance_id            TEXT,
    owner_acquired_at_us         INTEGER,
    created_at_us                INTEGER NOT NULL,
    updated_at_us                INTEGER NOT NULL,

    CHECK (singleton = 1),
    CHECK (length(installation_id) BETWEEN 1 AND 128),
    CHECK (length(installation_format_version) BETWEEN 1 AND 128),
    CHECK (typeof(fencing_generation) = 'integer' AND fencing_generation > 0),
    CHECK (owner_instance_id IS NULL OR length(owner_instance_id) BETWEEN 1 AND 128),
    CHECK (
        owner_acquired_at_us IS NULL
        OR (typeof(owner_acquired_at_us) = 'integer' AND owner_acquired_at_us > 0)
    ),
    CHECK (typeof(created_at_us) = 'integer' AND created_at_us > 0),
    CHECK (typeof(updated_at_us) = 'integer' AND updated_at_us > 0)
);

-- What ran against this catalogue, pinned by content checksum, under whose authority.
-- Append-only: an applied migration is a historical fact, and a ledger that can be
-- rewritten cannot detect an edited migration, which is the whole reason the checksum
-- is recorded.
CREATE TABLE IF NOT EXISTS omnivia_installation_schema_migrations (
    version                 INTEGER NOT NULL PRIMARY KEY,
    name                    TEXT    NOT NULL,
    checksum                TEXT    NOT NULL,
    installation_id         TEXT    NOT NULL,
    fencing_generation      INTEGER NOT NULL,
    applied_by_owner        TEXT    NOT NULL,
    applied_at_us           INTEGER NOT NULL,

    CHECK (typeof(version) = 'integer' AND version >= 0),
    CHECK (length(name) BETWEEN 1 AND 128),
    CHECK (length(checksum) = 64 AND checksum NOT GLOB '*[^0-9a-f]*'),
    CHECK (length(installation_id) BETWEEN 1 AND 128),
    CHECK (typeof(fencing_generation) = 'integer' AND fencing_generation > 0),
    CHECK (length(applied_by_owner) BETWEEN 1 AND 128),
    CHECK (typeof(applied_at_us) = 'integer' AND applied_at_us > 0)
);

-- The authorised workspace inventory: which workspaces this installation created and
-- stands behind. A row appears when an allocation is activated, never before, and
-- never by any other route -- membership is evidence that the installation authorised
-- the workspace, so a row that could be inserted without an allocation would be a
-- forged authorisation.
CREATE TABLE IF NOT EXISTS omnivia_installation_workspaces (
    workspace_id       TEXT    NOT NULL PRIMARY KEY,
    installation_id    TEXT    NOT NULL,
    workspace_path     TEXT    NOT NULL,
    workspace_label    TEXT,
    allocation_id      TEXT    NOT NULL,
    fencing_generation INTEGER NOT NULL,
    registered_at_us   INTEGER NOT NULL,

    CHECK (length(workspace_id)    BETWEEN 1 AND 128),
    CHECK (length(installation_id) BETWEEN 1 AND 128),
    CHECK (length(workspace_path)  BETWEEN 1 AND 1024),
    CHECK (workspace_label IS NULL OR length(workspace_label) BETWEEN 1 AND 256),
    CHECK (length(allocation_id)   BETWEEN 1 AND 128),
    CHECK (typeof(fencing_generation) = 'integer' AND fencing_generation > 0),
    CHECK (typeof(registered_at_us) = 'integer' AND registered_at_us > 0)
);

-- One workspace creation, from the moment the server minted its identity to the moment
-- it became real or recoverably failed.
--
-- The allocation id and the target workspace id are minted by the server and the target
-- path is derived from the installation root and that minted id. None of the three is
-- ever taken from a request: a caller that could choose the path could direct a
-- creation at a directory it does not own, and a caller that could choose the workspace
-- id could collide with an existing one deliberately.
--
-- The lifecycle is exactly three states. `preparing` is the only state a row is born
-- in; `active` is terminal and the UPDATE trigger refuses to leave it; and
-- `failed_recoverable` is exactly what its name says -- a failure a later attempt may
-- resume, which is why it may return to `preparing` or settle directly on `active`.
-- There is no `failed_permanent`, no `deleted` and no supersession: this table records
-- what was attempted, and a row that could be removed would take the record with it.
CREATE TABLE IF NOT EXISTS omnivia_installation_allocations (
    allocation_id       TEXT    NOT NULL PRIMARY KEY,
    installation_id     TEXT    NOT NULL,
    target_workspace_id TEXT    NOT NULL,
    target_path         TEXT    NOT NULL,
    principal_id        TEXT    NOT NULL,
    operation           TEXT    NOT NULL,
    purpose             TEXT    NOT NULL,
    claim_id            TEXT    NOT NULL,
    audit_ref           TEXT    NOT NULL,
    state               TEXT    NOT NULL,
    state_detail        TEXT,
    fencing_generation  INTEGER NOT NULL,
    created_at_us       INTEGER NOT NULL,
    updated_at_us       INTEGER NOT NULL,

    CHECK (length(allocation_id)       BETWEEN 1 AND 128),
    CHECK (length(installation_id)     BETWEEN 1 AND 128),
    CHECK (length(target_workspace_id) BETWEEN 1 AND 128),
    CHECK (length(target_path)         BETWEEN 1 AND 1024),
    CHECK (length(principal_id)        BETWEEN 1 AND 128),
    CHECK (length(operation)           BETWEEN 1 AND 128),
    CHECK (length(purpose)             BETWEEN 1 AND 128),
    CHECK (length(claim_id)            BETWEEN 1 AND 128),
    CHECK (length(audit_ref)           BETWEEN 1 AND 128),
    CHECK (state IN ('preparing', 'active', 'failed_recoverable')),
    CHECK (state_detail IS NULL OR length(state_detail) BETWEEN 1 AND 512),
    CHECK (typeof(fencing_generation) = 'integer' AND fencing_generation > 0),
    CHECK (typeof(created_at_us) = 'integer' AND created_at_us > 0),
    CHECK (typeof(updated_at_us) = 'integer' AND updated_at_us > 0),

    FOREIGN KEY (claim_id, installation_id)
        REFERENCES omnivia_installation_idempotency_claims (claim_id, installation_id),
    FOREIGN KEY (audit_ref, installation_id)
        REFERENCES omnivia_installation_audit_events (audit_ref, installation_id)
);

-- Immutable record of one authenticated installation-scoped request and how it was
-- decided. `principal_id` is the server-validated principal, never a caller-asserted
-- claim, and no credential, secret or exception text has a column here.
--
-- The `error_code` check states one rule over two columns: a refusal or a failure names
-- a code, a success has nothing to name, and the two columns cannot disagree.
CREATE TABLE IF NOT EXISTS omnivia_installation_audit_events (
    audit_ref          TEXT    NOT NULL PRIMARY KEY,
    installation_id    TEXT    NOT NULL,
    principal_id       TEXT    NOT NULL,
    operation          TEXT    NOT NULL,
    purpose            TEXT    NOT NULL,
    outcome_class      TEXT    NOT NULL,
    error_code         TEXT,
    fencing_generation INTEGER NOT NULL,
    recorded_at_us     INTEGER NOT NULL,

    CHECK (length(audit_ref)       BETWEEN 1 AND 128),
    CHECK (length(installation_id) BETWEEN 1 AND 128),
    CHECK (length(principal_id)    BETWEEN 1 AND 128),
    CHECK (length(operation)       BETWEEN 1 AND 128),
    CHECK (length(purpose)         BETWEEN 1 AND 128),
    CHECK (outcome_class IN ('accepted', 'succeeded', 'failed', 'refused')),
    CHECK (
        (outcome_class IN ('accepted', 'succeeded') AND error_code IS NULL)
        OR (
            outcome_class IN ('failed', 'refused')
            AND error_code IS NOT NULL
            AND length(error_code) BETWEEN 1 AND 128
        )
    ),
    CHECK (typeof(fencing_generation) = 'integer' AND fencing_generation > 0),
    CHECK (typeof(recorded_at_us) = 'integer' AND recorded_at_us > 0)
);

-- One scoped idempotency key, frozen against the canonical request it was used for.
-- The scope is the whole tuple -- installation, validated principal, operation, key --
-- because a key is never global: the same string from a different principal or on a
-- different operation is a different key, or a leaked one would let a caller choose
-- whose recorded answer it receives.
--
-- Equal scope and equal digest is an honest replay, answered from this claim and its
-- outcome; equal scope and a different digest is a conflict, refused with the original
-- bytes left exactly as they are. Nothing here overwrites a claim, because nothing here
-- can update one.
CREATE TABLE IF NOT EXISTS omnivia_installation_idempotency_claims (
    claim_id           TEXT    NOT NULL PRIMARY KEY,
    installation_id    TEXT    NOT NULL,
    principal_id       TEXT    NOT NULL,
    operation          TEXT    NOT NULL,
    idempotency_key    TEXT    NOT NULL,
    request_digest     TEXT    NOT NULL,
    audit_ref          TEXT    NOT NULL,
    fencing_generation INTEGER NOT NULL,
    claimed_at_us      INTEGER NOT NULL,

    CHECK (length(claim_id)        BETWEEN 1 AND 128),
    CHECK (length(installation_id) BETWEEN 1 AND 128),
    CHECK (length(principal_id)    BETWEEN 1 AND 128),
    CHECK (length(operation)       BETWEEN 1 AND 128),
    CHECK (length(idempotency_key) BETWEEN 1 AND 128),
    CHECK (length(audit_ref)       BETWEEN 1 AND 128),
    CHECK (
        length(request_digest) = 71
        AND substr(request_digest, 1, 7) = 'sha256:'
        AND substr(request_digest, 8) NOT GLOB '*[^0-9a-f]*'
    ),
    CHECK (typeof(fencing_generation) = 'integer' AND fencing_generation > 0),
    CHECK (typeof(claimed_at_us) = 'integer' AND claimed_at_us > 0),

    FOREIGN KEY (audit_ref, installation_id)
        REFERENCES omnivia_installation_audit_events (audit_ref, installation_id)
);

-- The single terminal answer a claim settled on: one success, or one typed error.
-- Exactly one representation is stored -- the canonical outcome bytes, or an accepted
-- reference to them -- so a reader is never left choosing between two descriptions of
-- the same answer.
CREATE TABLE IF NOT EXISTS omnivia_installation_idempotency_outcomes (
    outcome_id         TEXT    NOT NULL PRIMARY KEY,
    claim_id           TEXT    NOT NULL,
    installation_id    TEXT    NOT NULL,
    outcome_branch     TEXT    NOT NULL,
    error_code         TEXT,
    outcome_json       TEXT,
    outcome_reference  TEXT,
    outcome_digest     TEXT    NOT NULL,
    audit_ref          TEXT    NOT NULL,
    fencing_generation INTEGER NOT NULL,
    settled_at_us      INTEGER NOT NULL,

    CHECK (length(outcome_id)      BETWEEN 1 AND 128),
    CHECK (length(claim_id)        BETWEEN 1 AND 128),
    CHECK (length(installation_id) BETWEEN 1 AND 128),
    CHECK (length(audit_ref)       BETWEEN 1 AND 128),
    CHECK (outcome_branch IN ('success', 'error')),
    CHECK (
        (outcome_branch = 'success' AND error_code IS NULL)
        OR (
            outcome_branch = 'error'
            AND error_code IS NOT NULL
            AND length(error_code) BETWEEN 1 AND 128
        )
    ),
    CHECK (
        (outcome_json IS NOT NULL AND outcome_reference IS NULL
         AND length(outcome_json) BETWEEN 1 AND 8192)
        OR (outcome_reference IS NOT NULL AND outcome_json IS NULL
            AND length(outcome_reference) BETWEEN 1 AND 256)
    ),
    CHECK (
        length(outcome_digest) = 71
        AND substr(outcome_digest, 1, 7) = 'sha256:'
        AND substr(outcome_digest, 8) NOT GLOB '*[^0-9a-f]*'
    ),
    CHECK (typeof(fencing_generation) = 'integer' AND fencing_generation > 0),
    CHECK (typeof(settled_at_us) = 'integer' AND settled_at_us > 0),

    FOREIGN KEY (claim_id, installation_id)
        REFERENCES omnivia_installation_idempotency_claims (claim_id, installation_id),
    FOREIGN KEY (audit_ref, installation_id)
        REFERENCES omnivia_installation_audit_events (audit_ref, installation_id)
);

-- The durable evidence that an accepted installation-scoped execution held a
-- server-issued grant, and which one. The row is written in the same transaction as the
-- work it accounts for, so the two commit together or not at all.
--
-- Its links are checked for agreement, not merely for existence. A foreign key proves
-- the allocation, claim and audit event exist inside this installation; the INSERT
-- trigger additionally proves they do not contradict this row about the target
-- workspace, the principal, the operation or the purpose. Existence alone would admit a
-- grant use that named one principal while pointing at another principal's claim.
CREATE TABLE IF NOT EXISTS omnivia_installation_grant_uses (
    execution_id        TEXT    NOT NULL PRIMARY KEY,
    installation_id     TEXT    NOT NULL,
    allocation_id       TEXT    NOT NULL,
    target_workspace_id TEXT    NOT NULL,
    principal_id        TEXT    NOT NULL,
    operation           TEXT    NOT NULL,
    purpose             TEXT    NOT NULL,
    grant_id            TEXT    NOT NULL,
    required_role       TEXT    NOT NULL,
    execution_kind      TEXT    NOT NULL,
    claim_id            TEXT    NOT NULL,
    audit_ref           TEXT    NOT NULL,
    fencing_generation  INTEGER NOT NULL,
    recorded_at_us      INTEGER NOT NULL,

    CHECK (length(execution_id)        BETWEEN 1 AND 128),
    CHECK (length(installation_id)     BETWEEN 1 AND 128),
    CHECK (length(allocation_id)       BETWEEN 1 AND 128),
    CHECK (length(target_workspace_id) BETWEEN 1 AND 128),
    CHECK (length(principal_id)        BETWEEN 1 AND 128),
    CHECK (length(operation)           BETWEEN 1 AND 128),
    CHECK (length(purpose)             BETWEEN 1 AND 128),
    CHECK (length(grant_id)            BETWEEN 1 AND 128),
    CHECK (length(required_role)       BETWEEN 1 AND 128),
    CHECK (execution_kind IN ('executed', 'replayed')),
    CHECK (length(claim_id)            BETWEEN 1 AND 128),
    CHECK (length(audit_ref)           BETWEEN 1 AND 128),
    CHECK (typeof(fencing_generation) = 'integer' AND fencing_generation > 0),
    CHECK (typeof(recorded_at_us) = 'integer' AND recorded_at_us > 0),

    FOREIGN KEY (allocation_id, installation_id)
        REFERENCES omnivia_installation_allocations (allocation_id, installation_id),
    FOREIGN KEY (claim_id, installation_id)
        REFERENCES omnivia_installation_idempotency_claims (claim_id, installation_id),
    FOREIGN KEY (audit_ref, installation_id)
        REFERENCES omnivia_installation_audit_events (audit_ref, installation_id)
);

-- Parent keys for the composite foreign keys above. SQLite requires the referenced
-- columns to carry a UNIQUE index, and these are declared by name rather than as inline
-- UNIQUE constraints so they appear in `sqlite_master` under a readable name and
-- therefore inside the canonical schema fingerprint -- an implicit `sqlite_autoindex_*`
-- is filtered out of it and would not be discoverable.
CREATE UNIQUE INDEX IF NOT EXISTS omnivia_idx_installation_audit_events_ref
    ON omnivia_installation_audit_events (audit_ref, installation_id);
CREATE UNIQUE INDEX IF NOT EXISTS omnivia_idx_installation_claims_id
    ON omnivia_installation_idempotency_claims (claim_id, installation_id);
CREATE UNIQUE INDEX IF NOT EXISTS omnivia_idx_installation_outcomes_id
    ON omnivia_installation_idempotency_outcomes (outcome_id, installation_id);
CREATE UNIQUE INDEX IF NOT EXISTS omnivia_idx_installation_allocations_id
    ON omnivia_installation_allocations (allocation_id, installation_id);

-- The scope tuple is unique: one key, in one installation, for one validated principal,
-- on one operation, claims once.
CREATE UNIQUE INDEX IF NOT EXISTS omnivia_idx_installation_claims_scope
    ON omnivia_installation_idempotency_claims
       (installation_id, principal_id, operation, idempotency_key);

-- At most one terminal outcome per claim, refused by the index rather than by a
-- convention a caller has to remember.
CREATE UNIQUE INDEX IF NOT EXISTS omnivia_idx_installation_outcomes_claim
    ON omnivia_installation_idempotency_outcomes (claim_id);

-- One workspace identity is allocated once. A second allocation naming the same target
-- would mean two creations racing for one directory.
CREATE UNIQUE INDEX IF NOT EXISTS omnivia_idx_installation_allocations_target
    ON omnivia_installation_allocations (installation_id, target_workspace_id);

-- A grant is a one-shot authority. Replays consume a fresh grant and record
-- `execution_kind = 'replayed'`; the same grant id can never account for two uses.
CREATE UNIQUE INDEX IF NOT EXISTS omnivia_idx_installation_grant_uses_grant
    ON omnivia_installation_grant_uses (grant_id);

-- The inventory and the audit trail are both read as "this installation, in time
-- order". Without these the only way to read them is a full scan of a table that only
-- grows.
CREATE INDEX IF NOT EXISTS omnivia_idx_installation_workspaces_time
    ON omnivia_installation_workspaces (installation_id, registered_at_us);
CREATE INDEX IF NOT EXISTS omnivia_idx_installation_audit_events_time
    ON omnivia_installation_audit_events (installation_id, recorded_at_us);

-- The state row is created exactly once, by the bootstrap that also mints the identity,
-- and only through a connection carrying the writer function.
CREATE TRIGGER IF NOT EXISTS omnivia_guard_installation_state_insert
BEFORE INSERT ON omnivia_installation_state
WHEN omnivia_installation_writer() IS NOT 1
   OR NEW.singleton IS NOT 1
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_installation_state');
END;

-- The only mutable row in this schema, and mutable along exactly one path. The
-- identity, its format version and the creation time are frozen; the generation may
-- only increase, so a generation is never reused and a resumed predecessor's is
-- permanently stale; and an acquisition must name an owner, so ownership cannot be
-- silently cleared to leave a catalogue that anything may write.
CREATE TRIGGER IF NOT EXISTS omnivia_guard_installation_state_update
BEFORE UPDATE ON omnivia_installation_state
WHEN omnivia_installation_writer() IS NOT 1
   OR NEW.singleton IS NOT OLD.singleton
   OR NEW.installation_id IS NOT OLD.installation_id
   OR NEW.installation_format_version IS NOT OLD.installation_format_version
   OR NEW.created_at_us IS NOT OLD.created_at_us
   OR NEW.fencing_generation <= OLD.fencing_generation
   OR NEW.owner_instance_id IS NULL
   OR NEW.owner_acquired_at_us IS NULL
BEGIN
    SELECT RAISE(ABORT, 'omnivia: refused UPDATE on omnivia_installation_state');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_installation_state_delete
BEFORE DELETE ON omnivia_installation_state
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_installation_state is never deleted');
END;

-- The migration ledger. Its INSERT predicate omits the current-owner requirement that
-- every other table carries, and only that one, because the state row and the first
-- ledger row are written by the same bootstrap statement pair inside one transaction --
-- and it still requires the writer function and agreement with the identity and
-- generation the state row holds.
CREATE TRIGGER IF NOT EXISTS omnivia_guard_installation_schema_migrations_insert
BEFORE INSERT ON omnivia_installation_schema_migrations
WHEN omnivia_installation_writer() IS NOT 1
   OR NOT EXISTS (
    SELECT 1 FROM omnivia_installation_state s
    WHERE s.singleton = 1
      AND s.installation_id = NEW.installation_id
      AND s.fencing_generation = NEW.fencing_generation
)
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_installation_schema_migrations');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_installation_schema_migrations_update
BEFORE UPDATE ON omnivia_installation_schema_migrations
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_installation_schema_migrations is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_installation_schema_migrations_delete
BEFORE DELETE ON omnivia_installation_schema_migrations
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_installation_schema_migrations is append-only; DELETE is never permitted');
END;

-- A workspace enters the inventory only as the activation of an allocation that named
-- exactly this workspace, path and generation. Existence of the allocation is not
-- enough: an inventory row that disagreed with its allocation about which directory the
-- workspace lives in would be an authorisation for something nobody allocated.
CREATE TRIGGER IF NOT EXISTS omnivia_guard_installation_workspaces_insert
BEFORE INSERT ON omnivia_installation_workspaces
WHEN omnivia_installation_writer() IS NOT 1
   OR NOT EXISTS (
    SELECT 1 FROM omnivia_installation_state s
    WHERE s.singleton = 1
      AND s.owner_instance_id IS NOT NULL
      AND s.installation_id = NEW.installation_id
      AND s.fencing_generation = NEW.fencing_generation
)
   OR NOT EXISTS (
    SELECT 1 FROM omnivia_installation_allocations a
    WHERE a.allocation_id = NEW.allocation_id
      AND a.installation_id = NEW.installation_id
      AND a.target_workspace_id = NEW.workspace_id
      AND a.target_path = NEW.workspace_path
      AND a.state = 'active'
      AND a.fencing_generation = NEW.fencing_generation
)
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_installation_workspaces');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_installation_workspaces_update
BEFORE UPDATE ON omnivia_installation_workspaces
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_installation_workspaces is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_installation_workspaces_delete
BEFORE DELETE ON omnivia_installation_workspaces
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_installation_workspaces is append-only; DELETE is never permitted');
END;

-- An allocation is born `preparing`, under the current owner and generation, and must
-- agree with the claim and audit event it names about who asked for what.
CREATE TRIGGER IF NOT EXISTS omnivia_guard_installation_allocations_insert
BEFORE INSERT ON omnivia_installation_allocations
WHEN omnivia_installation_writer() IS NOT 1
   OR NOT EXISTS (
    SELECT 1 FROM omnivia_installation_state s
    WHERE s.singleton = 1
      AND s.owner_instance_id IS NOT NULL
      AND s.installation_id = NEW.installation_id
      AND s.fencing_generation = NEW.fencing_generation
)
   OR NEW.state IS NOT 'preparing'
   OR NOT EXISTS (
    SELECT 1 FROM omnivia_installation_idempotency_claims c
    WHERE c.claim_id = NEW.claim_id
      AND c.installation_id = NEW.installation_id
      AND c.principal_id = NEW.principal_id
      AND c.operation = NEW.operation
      AND c.audit_ref = NEW.audit_ref
)
   OR NOT EXISTS (
    SELECT 1 FROM omnivia_installation_audit_events e
    WHERE e.audit_ref = NEW.audit_ref
      AND e.installation_id = NEW.installation_id
      AND e.principal_id = NEW.principal_id
      AND e.operation = NEW.operation
      AND e.purpose = NEW.purpose
)
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_installation_allocations');
END;

-- The lifecycle, written down once. Everything that identifies the allocation is
-- frozen, so an update can change the state, its detail and the generation that state
-- was reached under and nothing else; the transition must be one of the three the
-- lifecycle allows; and `active` is terminal, so a workspace that exists cannot be
-- walked back to `preparing` by a later attempt that did not know it had succeeded.
CREATE TRIGGER IF NOT EXISTS omnivia_guard_installation_allocations_update
BEFORE UPDATE ON omnivia_installation_allocations
WHEN omnivia_installation_writer() IS NOT 1
   OR NOT EXISTS (
    SELECT 1 FROM omnivia_installation_state s
    WHERE s.singleton = 1
      AND s.owner_instance_id IS NOT NULL
      AND s.installation_id = NEW.installation_id
      AND s.fencing_generation = NEW.fencing_generation
)
   OR NEW.allocation_id IS NOT OLD.allocation_id
   OR NEW.installation_id IS NOT OLD.installation_id
   OR NEW.target_workspace_id IS NOT OLD.target_workspace_id
   OR NEW.target_path IS NOT OLD.target_path
   OR NEW.principal_id IS NOT OLD.principal_id
   OR NEW.operation IS NOT OLD.operation
   OR NEW.purpose IS NOT OLD.purpose
   OR NEW.claim_id IS NOT OLD.claim_id
   OR NEW.audit_ref IS NOT OLD.audit_ref
   OR NEW.created_at_us IS NOT OLD.created_at_us
   OR OLD.state = 'active'
   OR NOT (
    (OLD.state = 'preparing' AND NEW.state IN ('active', 'failed_recoverable'))
    OR (OLD.state = 'failed_recoverable' AND NEW.state IN ('preparing', 'active'))
)
BEGIN
    SELECT RAISE(ABORT, 'omnivia: refused UPDATE on omnivia_installation_allocations');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_installation_allocations_delete
BEFORE DELETE ON omnivia_installation_allocations
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_installation_allocations are never deleted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_installation_audit_events_insert
BEFORE INSERT ON omnivia_installation_audit_events
WHEN omnivia_installation_writer() IS NOT 1
   OR NOT EXISTS (
    SELECT 1 FROM omnivia_installation_state s
    WHERE s.singleton = 1
      AND s.owner_instance_id IS NOT NULL
      AND s.installation_id = NEW.installation_id
      AND s.fencing_generation = NEW.fencing_generation
)
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_installation_audit_events');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_installation_audit_events_update
BEFORE UPDATE ON omnivia_installation_audit_events
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_installation_audit_events is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_installation_audit_events_delete
BEFORE DELETE ON omnivia_installation_audit_events
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_installation_audit_events is append-only; DELETE is never permitted');
END;

-- A claim must agree with the audit event that accounts for it about the principal and
-- the operation, which is the scope the key is bound to.
CREATE TRIGGER IF NOT EXISTS omnivia_guard_installation_claims_insert
BEFORE INSERT ON omnivia_installation_idempotency_claims
WHEN omnivia_installation_writer() IS NOT 1
   OR NOT EXISTS (
    SELECT 1 FROM omnivia_installation_state s
    WHERE s.singleton = 1
      AND s.owner_instance_id IS NOT NULL
      AND s.installation_id = NEW.installation_id
      AND s.fencing_generation = NEW.fencing_generation
)
   OR NOT EXISTS (
    SELECT 1 FROM omnivia_installation_audit_events e
    WHERE e.audit_ref = NEW.audit_ref
      AND e.installation_id = NEW.installation_id
      AND e.principal_id = NEW.principal_id
      AND e.operation = NEW.operation
)
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_installation_idempotency_claims');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_installation_claims_update
BEFORE UPDATE ON omnivia_installation_idempotency_claims
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_installation_idempotency_claims is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_installation_claims_delete
BEFORE DELETE ON omnivia_installation_idempotency_claims
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_installation_idempotency_claims is append-only; DELETE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_installation_outcomes_insert
BEFORE INSERT ON omnivia_installation_idempotency_outcomes
WHEN omnivia_installation_writer() IS NOT 1
   OR NOT EXISTS (
    SELECT 1 FROM omnivia_installation_state s
    WHERE s.singleton = 1
      AND s.owner_instance_id IS NOT NULL
      AND s.installation_id = NEW.installation_id
      AND s.fencing_generation = NEW.fencing_generation
)
   OR NOT EXISTS (
    SELECT 1
    FROM omnivia_installation_idempotency_claims c
    JOIN omnivia_installation_audit_events e
      ON e.audit_ref = NEW.audit_ref
     AND e.installation_id = NEW.installation_id
    WHERE c.claim_id = NEW.claim_id
      AND c.installation_id = NEW.installation_id
      AND c.audit_ref = NEW.audit_ref
      AND e.principal_id = c.principal_id
      AND e.operation = c.operation
)
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_installation_idempotency_outcomes');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_installation_outcomes_update
BEFORE UPDATE ON omnivia_installation_idempotency_outcomes
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_installation_idempotency_outcomes is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_installation_outcomes_delete
BEFORE DELETE ON omnivia_installation_idempotency_outcomes
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_installation_idempotency_outcomes is append-only; DELETE is never permitted');
END;

-- The agreement check the foreign keys cannot express: the allocation must name this
-- row's target workspace, principal, operation, purpose, claim and audit event, and the
-- claim and the audit event must name this row's principal and operation. A grant use
-- that contradicts any of them is not a record of an execution that happened.
CREATE TRIGGER IF NOT EXISTS omnivia_guard_installation_grant_uses_insert
BEFORE INSERT ON omnivia_installation_grant_uses
WHEN omnivia_installation_writer() IS NOT 1
   OR NOT EXISTS (
    SELECT 1 FROM omnivia_installation_state s
    WHERE s.singleton = 1
      AND s.owner_instance_id IS NOT NULL
      AND s.installation_id = NEW.installation_id
      AND s.fencing_generation = NEW.fencing_generation
)
   OR NOT EXISTS (
    SELECT 1 FROM omnivia_installation_allocations a
    WHERE a.allocation_id = NEW.allocation_id
      AND a.installation_id = NEW.installation_id
      AND a.target_workspace_id = NEW.target_workspace_id
      AND a.principal_id = NEW.principal_id
      AND a.operation = NEW.operation
      AND a.purpose = NEW.purpose
      AND a.claim_id = NEW.claim_id
      AND a.audit_ref = NEW.audit_ref
)
   OR NOT EXISTS (
    SELECT 1 FROM omnivia_installation_idempotency_claims c
    WHERE c.claim_id = NEW.claim_id
      AND c.installation_id = NEW.installation_id
      AND c.principal_id = NEW.principal_id
      AND c.operation = NEW.operation
      AND c.audit_ref = NEW.audit_ref
)
   OR NOT EXISTS (
    SELECT 1 FROM omnivia_installation_audit_events e
    WHERE e.audit_ref = NEW.audit_ref
      AND e.installation_id = NEW.installation_id
      AND e.principal_id = NEW.principal_id
      AND e.operation = NEW.operation
      AND e.purpose = NEW.purpose
)
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_installation_grant_uses');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_installation_grant_uses_update
BEFORE UPDATE ON omnivia_installation_grant_uses
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_installation_grant_uses is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_installation_grant_uses_delete
BEFORE DELETE ON omnivia_installation_grant_uses
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_installation_grant_uses is append-only; DELETE is never permitted');
END;
