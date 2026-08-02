-- Immutable application audit events and scoped idempotency claims/outcomes (V06-1 M1).
--
-- The first forward-only durable slice for the application seam. It adds storage
-- facts only: no repository, handler, dispatcher, operation implementation,
-- pagination or token state, ACL/policy schema, and no legacy promotion. What an
-- operation does with these rows is a later, separately accepted vertical.
--
-- Three tables, each append-only:
--
--   omnivia_application_audit_events  what the server decided, for whom, about what
--   omnivia_idempotency_claims        one scoped key bound to one canonical request
--   omnivia_idempotency_outcomes      the single terminal answer that claim settled on
--
-- Immutability is the point, so it is enforced twice over. Every UPDATE and DELETE
-- aborts unconditionally -- for the current fenced owner too, because "the owner may
-- rewrite history" is exactly the property an audit trail cannot have -- and every
-- INSERT must satisfy the complete connection-authority, mutation-guard,
-- workspace-state and lease predicate that `0005_require_connection_authority.sql`
-- established. There is no supersession here and no writable current-state flag: a
-- replay is answered by *finding* the existing claim, never by overwriting it.
--
-- Ownership. These are workspace-owned tables in the fenced authoritative workspace
-- database, and every row binds the singleton workspace id -- checked in the INSERT
-- trigger rather than trusted from the caller. Installation-scoped operations are
-- installation-local and are deliberately absent: giving them a row here would mean
-- fabricating a workspace for them.
--
-- Timestamps are signed 64-bit UTC microseconds since the Unix epoch, in `*_at_us`.
-- SQLite's INTEGER storage class is exactly a signed 64-bit integer, so the domain
-- is carried without narrowing; the `typeof(...) = 'integer'` checks are what make
-- normalisation exact, because a fractional microsecond is then refused outright
-- instead of being silently rounded to a whole one by column affinity.
--
-- Digests are `sha256:<64 lowercase hex>` over exact Core canonical-JSON bytes
-- (ADR-039 I-JSON domain, RFC 8785). The checks below police the *shape* of that
-- value; the bytes it summarises are canonicalised and verified above this layer,
-- because SQLite's own serialisation is not the authority for what was hashed.
--
-- Equivalence is not redefined here. The application's existing public
-- `idempotency_equivalence()` remains the single semantic authority for what makes
-- two requests the same request: its scope is the validated principal, the workspace
-- where applicable, the operation and the key, and its fingerprint covers operation
-- input, purpose, scopes and required capabilities while excluding request,
-- correlation and trace ids, the deadline and client identity. This schema stores
-- that function's output; it does not compute a second opinion.
--
-- Bounds are on every text column on purpose. A schema cannot recognise a bearer
-- token, but it can refuse to be a place unbounded content goes: 128 characters is
-- the contract's own bounded-value ceiling (`_BOUNDED_VALUE_MAX_LENGTH`), and exactly
-- three columns are allowed past it -- `granted_authority_json` at 4096 and
-- `outcome_json` at 8192, which carry canonical documents rather than identifiers,
-- and `outcome_reference` at 256, which addresses one. Credentials, secrets,
-- exception text and caller-supplied identity
-- asserted as authenticated have no column here -- `principal_id` is the
-- server-validated principal, never a `PrincipalClaim`.

-- Every comment in this file sits *between* statements, never inside one. SQLite
-- stores a statement's original text in `sqlite_master`, and the migrator executes
-- statements one at a time with comments stripped so that a migration stays inside
-- the caller's transaction -- so a comment inside a CREATE would make the applied
-- schema differ, byte for byte, from the canonical fingerprint built by replaying the
-- same artifacts with `executescript`. Readiness compares exactly those two, and a
-- clean workspace would be reported as drifted.

-- Immutable record of one authenticated application request and how it was decided.
-- `granted_authority_json` carries the bounded evaluated authority facts the decision
-- actually relied on. That is evidence of an evaluation, not a policy store: the ACL
-- schema and its pre-ranking evaluator stay closed, and nothing here may be read as
-- their durable form.
--
-- The `error_code` check states one rule over two columns: a refusal or a failure
-- names a code, a success has nothing to name, and the columns cannot disagree.
-- `recorded_at_us` is positive because no application audit event predates the Unix
-- epoch; the column's domain is still the full signed 64-bit one the binding requires.
--
-- The two links are optional, declared rather than implied, and composite so a link
-- can only ever reach a claim or outcome in this row's own workspace. A NULL link
-- satisfies the constraint, which is what keeps it optional.
--
-- Every foreign key here is left at SQLite's default immediate enforcement, and that
-- is deliberate rather than an oversight, because the alternative was tried and is
-- worse. The references look mutually recursive -- an audit event may name the claim
-- and outcome it decided, and a claim and an outcome each name the audit event that
-- accounts for them -- but there is no cycle to break, because the audit links are
-- optional and SQLite's MATCH SIMPLE rule imposes no constraint on a NULL one. The
-- admissible order is also the only order the facts allow, since an event recorded
-- *before* an outcome exists cannot name it:
--
--     audit(no links) -> claim(audit) -> outcome(claim, audit) -> audit(claim, outcome)
--
-- all four inside one transaction. The links are for the later event, not a backfill
-- of the first, which is why refusing UPDATE costs nothing.
--
-- `DEFERRABLE INITIALLY DEFERRED` would move the check to COMMIT, and SQLite leaves a
-- transaction *open* when COMMIT fails on a deferred constraint. `fenced_transaction`
-- issues its COMMIT outside the block that rolls back, so a single dangling link
-- would leave the service's one authoritative write connection inside an open
-- transaction -- still reading its own uncommitted row, and refusing every later
-- write with "cannot start a transaction within a transaction" for the rest of the
-- process's life. Immediate enforcement fails the offending statement instead, and
-- the existing rollback path handles it.
CREATE TABLE IF NOT EXISTS omnivia_application_audit_events (
    audit_ref              TEXT    NOT NULL PRIMARY KEY,
    workspace_id           TEXT    NOT NULL,
    principal_id           TEXT    NOT NULL,
    operation              TEXT    NOT NULL,
    purpose                TEXT    NOT NULL,
    request_id             TEXT    NOT NULL,
    correlation_id         TEXT    NOT NULL,
    trace_id               TEXT    NOT NULL,
    granted_authority_json TEXT    NOT NULL,
    outcome_class          TEXT    NOT NULL,
    error_code             TEXT,
    recorded_at_us         INTEGER NOT NULL,
    claim_id               TEXT,
    outcome_id             TEXT,

    CHECK (length(audit_ref)    BETWEEN 1 AND 128),
    CHECK (length(workspace_id) BETWEEN 1 AND 128),
    CHECK (length(principal_id) BETWEEN 1 AND 128),
    CHECK (length(operation)    BETWEEN 1 AND 128),
    CHECK (length(purpose)      BETWEEN 1 AND 128),
    CHECK (length(request_id)     BETWEEN 1 AND 128),
    CHECK (length(correlation_id) BETWEEN 1 AND 128),
    CHECK (length(trace_id)       BETWEEN 1 AND 128),
    CHECK (length(granted_authority_json) BETWEEN 1 AND 4096),
    CHECK (outcome_class IN ('succeeded', 'failed', 'refused')),
    CHECK (
        (outcome_class = 'succeeded' AND error_code IS NULL)
        OR (
            outcome_class IN ('failed', 'refused')
            AND error_code IS NOT NULL
            AND length(error_code) BETWEEN 1 AND 128
        )
    ),
    CHECK (typeof(recorded_at_us) = 'integer' AND recorded_at_us > 0),
    CHECK (claim_id   IS NULL OR length(claim_id)   BETWEEN 1 AND 128),
    CHECK (outcome_id IS NULL OR length(outcome_id) BETWEEN 1 AND 128),

    FOREIGN KEY (claim_id, workspace_id)
        REFERENCES omnivia_idempotency_claims (claim_id, workspace_id),
    FOREIGN KEY (outcome_id, workspace_id)
        REFERENCES omnivia_idempotency_outcomes (outcome_id, workspace_id)
);

-- One scoped idempotency key, frozen against the canonical request it was used for.
-- The scope is the whole tuple -- workspace, validated principal, operation, key --
-- because a key is never global: the same string from a different principal, against
-- a different workspace, or on a different operation is a different key, or a leaked
-- one would let a caller choose whose recorded answer it receives.
--
-- The digest decides the rest. Equal scope and equal digest is an honest replay, to
-- be answered from this claim and its outcome; equal scope and a different digest is
-- a conflict, refused with the original bytes left exactly as they are. Nothing in
-- this schema overwrites a claim, because nothing in this schema can update one.
--
-- The digest check reads `sha256:` then exactly 64 lowercase hex digits and nothing
-- else; the negated GLOB is what makes that every digit rather than only the first.
-- The `audit_ref` foreign key makes every claim accountable: it exists because a
-- request was audited.
CREATE TABLE IF NOT EXISTS omnivia_idempotency_claims (
    claim_id        TEXT    NOT NULL PRIMARY KEY,
    workspace_id    TEXT    NOT NULL,
    principal_id    TEXT    NOT NULL,
    operation       TEXT    NOT NULL,
    idempotency_key TEXT    NOT NULL,
    request_digest  TEXT    NOT NULL,
    audit_ref       TEXT    NOT NULL,
    claimed_at_us   INTEGER NOT NULL,

    CHECK (length(claim_id)        BETWEEN 1 AND 128),
    CHECK (length(workspace_id)    BETWEEN 1 AND 128),
    CHECK (length(principal_id)    BETWEEN 1 AND 128),
    CHECK (length(operation)       BETWEEN 1 AND 128),
    CHECK (length(idempotency_key) BETWEEN 1 AND 128),
    CHECK (length(audit_ref)       BETWEEN 1 AND 128),
    CHECK (
        length(request_digest) = 71
        AND substr(request_digest, 1, 7) = 'sha256:'
        AND substr(request_digest, 8) NOT GLOB '*[^0-9a-f]*'
    ),
    CHECK (typeof(claimed_at_us) = 'integer' AND claimed_at_us > 0),

    FOREIGN KEY (audit_ref, workspace_id)
        REFERENCES omnivia_application_audit_events (audit_ref, workspace_id)
);

-- The single terminal answer a claim settled on: one success, or one typed error.
-- Exactly one representation is stored -- the canonical outcome bytes, or an accepted
-- reference to them when they are too large to inline -- so a reader is never left
-- choosing between two descriptions of the same answer. The representation check
-- reads "exactly one of the two", never both and never neither.
--
-- The claim foreign key is composite, so an outcome cannot be attached to a claim in
-- another workspace.
CREATE TABLE IF NOT EXISTS omnivia_idempotency_outcomes (
    outcome_id        TEXT    NOT NULL PRIMARY KEY,
    claim_id          TEXT    NOT NULL,
    workspace_id      TEXT    NOT NULL,
    outcome_branch    TEXT    NOT NULL,
    error_code        TEXT,
    outcome_json      TEXT,
    outcome_reference TEXT,
    outcome_digest    TEXT    NOT NULL,
    audit_ref         TEXT    NOT NULL,
    settled_at_us     INTEGER NOT NULL,

    CHECK (length(outcome_id)   BETWEEN 1 AND 128),
    CHECK (length(claim_id)     BETWEEN 1 AND 128),
    CHECK (length(workspace_id) BETWEEN 1 AND 128),
    CHECK (length(audit_ref)    BETWEEN 1 AND 128),
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
    CHECK (typeof(settled_at_us) = 'integer' AND settled_at_us > 0),

    FOREIGN KEY (claim_id, workspace_id)
        REFERENCES omnivia_idempotency_claims (claim_id, workspace_id),
    FOREIGN KEY (audit_ref, workspace_id)
        REFERENCES omnivia_application_audit_events (audit_ref, workspace_id)
);

-- Parent keys for the composite foreign keys above. SQLite requires the referenced
-- columns to carry a UNIQUE index, and these are declared by name rather than as
-- inline UNIQUE constraints so they appear in `sqlite_master` under a readable name
-- and therefore inside the canonical schema fingerprint -- an implicit
-- `sqlite_autoindex_*` is filtered out of it and would not be discoverable.
CREATE UNIQUE INDEX IF NOT EXISTS omnivia_idx_application_audit_events_ref_workspace
    ON omnivia_application_audit_events (audit_ref, workspace_id);
CREATE UNIQUE INDEX IF NOT EXISTS omnivia_idx_idempotency_claims_id_workspace
    ON omnivia_idempotency_claims (claim_id, workspace_id);
CREATE UNIQUE INDEX IF NOT EXISTS omnivia_idx_idempotency_outcomes_id_workspace
    ON omnivia_idempotency_outcomes (outcome_id, workspace_id);

-- The scope tuple is unique: one key, in one workspace, for one validated principal,
-- on one operation, claims once. This index is the durable statement of that rule.
CREATE UNIQUE INDEX IF NOT EXISTS omnivia_idx_idempotency_claims_scope
    ON omnivia_idempotency_claims (workspace_id, principal_id, operation, idempotency_key);

-- At most one terminal outcome per claim. A second one is refused by the index, not
-- by a convention a caller has to remember.
CREATE UNIQUE INDEX IF NOT EXISTS omnivia_idx_idempotency_outcomes_claim
    ON omnivia_idempotency_outcomes (claim_id);

-- The audit trail is read as "this workspace, in time order". Without this the only
-- way to read it is a full scan of an append-only table that only grows.
CREATE INDEX IF NOT EXISTS omnivia_idx_application_audit_events_workspace_time
    ON omnivia_application_audit_events (workspace_id, recorded_at_us);

-- Nine statement triggers: three tables, three statement classes each. The set is
-- exact, because a table can look guarded while one statement class walks past it.
--
-- INSERT carries the complete predicate 0005 arrived at -- the connection-local
-- authority function, the guard row, the authoritative workspace state and the lease
-- that agrees with both -- plus the workspace binding, so a row cannot be written
-- into a workspace other than the one this database is. It is evaluated inside the
-- writing transaction, so an owner whose generation was taken over between BEGIN and
-- COMMIT fails on the predicate rather than committing under authority it lost.
--
-- UPDATE and DELETE carry no predicate at all. There is no condition under which
-- rewriting or removing an audit event, a claim or a terminal outcome is correct, so
-- there is no condition to write down -- including for the current fenced owner.

CREATE TRIGGER IF NOT EXISTS omnivia_guard_application_audit_events_insert
BEFORE INSERT ON omnivia_application_audit_events
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
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_application_audit_events');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_application_audit_events_update
BEFORE UPDATE ON omnivia_application_audit_events
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_application_audit_events is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_application_audit_events_delete
BEFORE DELETE ON omnivia_application_audit_events
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_application_audit_events is append-only; DELETE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_idempotency_claims_insert
BEFORE INSERT ON omnivia_idempotency_claims
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
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_idempotency_claims');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_idempotency_claims_update
BEFORE UPDATE ON omnivia_idempotency_claims
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_idempotency_claims is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_idempotency_claims_delete
BEFORE DELETE ON omnivia_idempotency_claims
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_idempotency_claims is append-only; DELETE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_idempotency_outcomes_insert
BEFORE INSERT ON omnivia_idempotency_outcomes
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
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_idempotency_outcomes');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_idempotency_outcomes_update
BEFORE UPDATE ON omnivia_idempotency_outcomes
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_idempotency_outcomes is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_idempotency_outcomes_delete
BEFORE DELETE ON omnivia_idempotency_outcomes
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_idempotency_outcomes is append-only; DELETE is never permitted');
END;
