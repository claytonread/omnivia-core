-- Durable record of the server-issued grant each accepted mutation ran under (V06-5 S0).
--
-- One append-only table and nothing else. `0007_application_audit_and_idempotency.sql`
-- already records *what* was decided (the audit event), *which request* claimed a key
-- (the claim) and *which answer* that claim settled on (the outcome), and none of that
-- is duplicated, widened or weakened here. What 0007 has no column for is the
-- authority the mutation actually executed under: which server-issued grant, held by
-- which role, scopes and capabilities, at which fencing generation, issued when and
-- expiring when.
--
-- That gap matters because the grant is the whole S0 control. A caller cannot obtain
-- or widen one from request metadata, and this table is the durable evidence that a
-- committed mutation had one -- rather than a property that lives only in a code path
-- and disappears the moment the process exits.
--
-- The row is written inside the same transaction as the domain mutation, the audit
-- event, the claim and the outcome, so the five commit together or not at all. Its two
-- composite foreign keys are what make that structural rather than conventional: an
-- execution row cannot exist without the claim and the audit event it names, and
-- neither can reach out of this row's own workspace.
--
-- `settled_monotonic_us < grant_expires_monotonic_us` is the one rule here that is not a
-- bound. It makes "an expired grant cannot have executed" a fact the schema refuses to
-- store the contradiction of, so it survives an edit to the execution seam that forgot
-- to check.
--
-- All three of those columns are readings of the *same process's monotonic clock*, in
-- microseconds, and the expiry rule compares them only to each other. `recorded_at_us`
-- and `grant_issued_at_us` are wall-clock readings and are compared to nothing: a wall
-- clock steps over an NTP correction, so a rule that read one as an expiry authority
-- would extend a grant across a backwards step and revoke a live one across a forwards
-- step. They are kept because an operator reading this table needs to know when a
-- mutation settled in a time they can name, which a monotonic reading cannot tell them.
-- `recorded_at_us` is read at settlement rather than at issuance, so it is when the
-- mutation committed rather than when its grant was obtained.
--
-- Monotonic columns carry no `> 0`: a monotonic clock's origin is arbitrary and may be
-- at or near zero on a freshly booted host, so the only true statements about them are
-- that they are integers and that they are ordered.
--
-- `execution_kind` records which of the two ways a grant was spent: `executed`, where
-- the domain mutation ran, or `replayed`, where an identical request was answered from
-- the stored outcome. Both are written because both consume the grant one-shot, and the
-- kind is what lets the claim index still admit exactly one real execution per key.
--
-- Append-only, enforced exactly as 0007 enforces it. UPDATE and DELETE abort
-- unconditionally -- for the current fenced owner too -- and INSERT carries the
-- complete connection-authority, mutation-guard, workspace-state and lease predicate
-- `0005_require_connection_authority.sql` established, plus the singleton workspace
-- binding. There is no supersession and no writable flag: a replay is answered by
-- finding the existing claim, never by rewriting this row.
--
-- Timestamps are signed 64-bit UTC microseconds since the Unix epoch, in `*_at_us`,
-- with `typeof(...) = 'integer'` so a fractional microsecond is refused rather than
-- silently rounded by column affinity. Text columns are bounded at 128, the contract's
-- own bounded-value ceiling; nothing here carries a document, so nothing is past it.
-- No credential, no caller-asserted identity and no exception text has a column: every
-- value is server-side, and `principal_id` is the validated principal rather than any
-- `PrincipalClaim`.

-- Every comment in this file sits *between* statements, never inside one. SQLite
-- stores a statement's original text in `sqlite_master`, and the migrator executes
-- statements one at a time with comments stripped so that a migration stays inside the
-- caller's transaction -- so a comment inside a CREATE would make the applied schema
-- differ, byte for byte, from the canonical fingerprint built by replaying the same
-- artifacts with `executescript`, and a clean workspace would be reported as drifted.

CREATE TABLE IF NOT EXISTS omnivia_mutation_executions (
    execution_id        TEXT    NOT NULL PRIMARY KEY,
    workspace_id        TEXT    NOT NULL,
    principal_id        TEXT    NOT NULL,
    operation           TEXT    NOT NULL,
    purpose             TEXT    NOT NULL,
    grant_id            TEXT    NOT NULL,
    required_role       TEXT    NOT NULL,
    scopes_json         TEXT    NOT NULL,
    capabilities_json   TEXT    NOT NULL,
    execution_kind      TEXT    NOT NULL,
    fencing_generation  INTEGER NOT NULL,
    grant_issued_at_us  INTEGER NOT NULL,
    grant_issued_monotonic_us  INTEGER NOT NULL,
    grant_expires_monotonic_us INTEGER NOT NULL,
    settled_monotonic_us       INTEGER NOT NULL,
    claim_id            TEXT    NOT NULL,
    audit_ref           TEXT    NOT NULL,
    recorded_at_us      INTEGER NOT NULL,

    CHECK (length(execution_id)  BETWEEN 1 AND 128),
    CHECK (length(workspace_id)  BETWEEN 1 AND 128),
    CHECK (length(principal_id)  BETWEEN 1 AND 128),
    CHECK (length(operation)     BETWEEN 1 AND 128),
    CHECK (length(purpose)       BETWEEN 1 AND 128),
    CHECK (length(grant_id)      BETWEEN 1 AND 128),
    CHECK (length(required_role) BETWEEN 1 AND 128),
    CHECK (length(scopes_json) BETWEEN 2 AND 4096),
    CHECK (length(capabilities_json) BETWEEN 2 AND 4096),
    CHECK (execution_kind IN ('executed', 'replayed')),
    CHECK (length(claim_id)      BETWEEN 1 AND 128),
    CHECK (length(audit_ref)     BETWEEN 1 AND 128),
    CHECK (typeof(fencing_generation) = 'integer' AND fencing_generation > 0),
    CHECK (typeof(grant_issued_at_us) = 'integer' AND grant_issued_at_us > 0),
    CHECK (typeof(grant_issued_monotonic_us) = 'integer'),
    CHECK (
        typeof(grant_expires_monotonic_us) = 'integer'
        AND grant_expires_monotonic_us > grant_issued_monotonic_us
    ),
    CHECK (
        typeof(settled_monotonic_us) = 'integer'
        AND settled_monotonic_us >= grant_issued_monotonic_us
        AND settled_monotonic_us < grant_expires_monotonic_us
    ),
    CHECK (typeof(recorded_at_us) = 'integer' AND recorded_at_us > 0),

    FOREIGN KEY (claim_id, workspace_id)
        REFERENCES omnivia_idempotency_claims (claim_id, workspace_id),
    FOREIGN KEY (audit_ref, workspace_id)
        REFERENCES omnivia_application_audit_events (audit_ref, workspace_id)
);

-- At most one *executed* row per idempotency claim: a second one would mean the domain
-- mutation ran twice under one key -- refused by the index rather than by a convention
-- the execution seam has to remember. Replays are excluded from the index rather than
-- from the table, because a replay still spends the grant it was presented with and
-- that expenditure has to be durable; what it must not do is run the mutation again.
CREATE UNIQUE INDEX IF NOT EXISTS omnivia_idx_mutation_executions_claim
    ON omnivia_mutation_executions (claim_id)
    WHERE execution_kind = 'executed';

-- A grant authorizes one use, whichever kind it turns out to be. Executing under it and
-- answering a replay from it are both spending it, so the uniqueness is unconditional:
-- a grant that has already answered a replay cannot go on to authorize a mutation that
-- writes. Refused structurally as well as in the seam.
CREATE UNIQUE INDEX IF NOT EXISTS omnivia_idx_mutation_executions_grant
    ON omnivia_mutation_executions (grant_id);

-- The audit trail is read as "this workspace, in time order", and this table is read
-- the same way. Without the index the only reading is a full scan of an append-only
-- table that only grows.
CREATE INDEX IF NOT EXISTS omnivia_idx_mutation_executions_workspace_time
    ON omnivia_mutation_executions (workspace_id, recorded_at_us);

-- Four statement triggers on this table, because a table can look guarded while one
-- statement class walks past it. A fifth, on 0007's outcome table, closes the same
-- cross-link one level down and is described where it stands. The INSERT predicate is 0007's, unchanged: the connection-local
-- authority function, the guard row, the authoritative workspace state, the lease that
-- agrees with both, and the workspace binding. It is evaluated inside the writing
-- transaction, so an owner whose generation was taken over between BEGIN and COMMIT
-- fails on the predicate rather than committing under authority it lost.
--
-- The second INSERT trigger is what makes the links evidence rather than decoration. The
-- composite foreign keys above prove the claim and the audit event exist in this row's
-- workspace, and stop there: a row could name a real claim while stating a principal,
-- operation or purpose that claim never carried, and every column an auditor reads would
-- still be internally consistent. This trigger refuses that row. An execution may only
-- name a claim agreeing on workspace, principal and operation, and an audit event
-- agreeing on those and on the purpose the mutation was served under -- so the authority
-- this table records cannot disagree with the authority 0007 recorded for the same
-- mutation, whatever wrote it.
--
-- The claim must also name *this row's* audit event (`c.audit_ref = NEW.audit_ref`).
-- Agreement on the authority fields alone is satisfied by any same-authority pair, so
-- without this a row could cross-link one real mutation's claim to a different real
-- mutation's audit event -- two honest halves of two different mutations, describing
-- one execution that never happened that way. 0007 already binds a claim to exactly one
-- audit event, so this only requires the execution to follow the link rather than pick
-- its own.

CREATE TRIGGER IF NOT EXISTS omnivia_guard_mutation_executions_insert
BEFORE INSERT ON omnivia_mutation_executions
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
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_mutation_executions');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_mutation_executions_authority
BEFORE INSERT ON omnivia_mutation_executions
WHEN NOT EXISTS (
    SELECT 1
    FROM omnivia_idempotency_claims c
    WHERE c.claim_id     = NEW.claim_id
      AND c.workspace_id = NEW.workspace_id
      AND c.principal_id = NEW.principal_id
      AND c.operation    = NEW.operation
      AND c.audit_ref    = NEW.audit_ref
)
   OR NOT EXISTS (
    SELECT 1
    FROM omnivia_application_audit_events a
    WHERE a.audit_ref    = NEW.audit_ref
      AND a.workspace_id = NEW.workspace_id
      AND a.principal_id = NEW.principal_id
      AND a.operation    = NEW.operation
      AND a.purpose      = NEW.purpose
)
BEGIN
    SELECT RAISE(ABORT, 'omnivia: mutation execution contradicts the claim or audit event it names');
END;

-- One trigger on 0007's own outcome table, because the same crossing is possible one
-- level down and 0007 is frozen. `omnivia_idempotency_outcomes` foreign-keys `claim_id`
-- and `audit_ref` independently, by workspace, and never requires the outcome's audit
-- event to be the one its claim already named. Two mutations by the same principal, for
-- the same operation and purpose, therefore admit an outcome that carries claim A's
-- answer under audit event B -- both halves real, every column consistent, and the pair
-- describing an execution that never happened. Requiring the claim to agree on
-- `(claim_id, workspace_id, audit_ref)` closes that for every write after this upgrade
-- without editing 0007. Rows written before it are the execution seam's problem, and
-- `_replay` refuses them there.
--
-- It fires only where both parents are present and disagree, which is the one case 0007
-- does not already refuse. A row naming a claim or an audit event that does not exist in
-- this workspace -- or naming neither, with a NULL -- is left to 0007's own foreign keys
-- and NOT NULLs, so this trigger adds a refusal rather than renaming existing ones.

CREATE TRIGGER IF NOT EXISTS omnivia_guard_idempotency_outcomes_audit_link
BEFORE INSERT ON omnivia_idempotency_outcomes
WHEN EXISTS (
    SELECT 1
    FROM omnivia_idempotency_claims c
    JOIN omnivia_application_audit_events a
      ON a.audit_ref = NEW.audit_ref AND a.workspace_id = NEW.workspace_id
    WHERE c.claim_id     = NEW.claim_id
      AND c.workspace_id = NEW.workspace_id
      AND c.audit_ref IS NOT NEW.audit_ref
)
BEGIN
    SELECT RAISE(ABORT, 'omnivia: idempotency outcome names an audit event its claim does not');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_mutation_executions_update
BEFORE UPDATE ON omnivia_mutation_executions
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_mutation_executions is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_mutation_executions_delete
BEFORE DELETE ON omnivia_mutation_executions
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_mutation_executions is append-only; DELETE is never permitted');
END;
