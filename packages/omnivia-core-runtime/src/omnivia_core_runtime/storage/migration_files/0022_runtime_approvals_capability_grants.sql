-- Canonical Approval and CapabilityGrant records (RT-203).
--
-- Additive only. Four append-only tables and twelve statement triggers, on top of the
-- canonical records migrations 0018, 0019 and 0021 added. Every row here belongs to an
-- existing `omnivia_runtime_runs` row; nothing here creates a run, changes a run's
-- status, resolves a wait, or duplicates the event stream that already states it.
--
--   omnivia_runtime_approvals            one immutable approval *request*
--   omnivia_runtime_approval_decisions   the one decision that request ever receives
--   omnivia_runtime_approval_comments    the one comment that approval ever carries
--   omnivia_runtime_capability_grants    one immutable grant, backed by one policy
--
-- Four tables, and the reason each exists. The accepted `Approval` is one record with
-- two halves written at different instants, so a single row would have to be *edited*
-- when the decision arrives -- and an approval that can be updated is an approval that
-- can be re-decided. Splitting the halves makes the rule structural instead of
-- enforced: the decision table is keyed by the approval alone, so a second decision has
-- nowhere to live under any spelling, and no trigger has to say so. The comment is the
-- third table for the same reason and no other: `validate_approval` accepts a *pending*
-- approval that already carries a comment, and accepts a decision that adds one later,
-- so a comment column on either half would need an UPDATE to represent a state the
-- accepted contract allows. One row keyed by the approval represents both, refuses a
-- replacement by primary key, and keeps exactly one authoritative copy of the text.
-- There is no fourth approval fact and no fifth table: the request's own optional
-- fields never change after it is written, so they are columns of the request.
--
-- The grant is the fourth, and follows 0021 exactly rather than inventing a second
-- convention. The stored document is the record: each row carries the complete accepted
-- v1 `CapabilityGrant` as Core canonical-JSON bytes, with the `sha256:<64 lowercase
-- hex>` digest of exactly those bytes and their byte length beside them. The digest
-- addresses the whole wire document -- the contract's own `capability_grant_id`
-- included -- and is not that identifier and does not derive it. The identifier column,
-- the run, the policy and the issue instant are the *selectors* this database indexes
-- and joins on, copied out of that same document; the repository re-derives each of
-- them from the decoded document on every read and refuses a row whose columns and
-- document disagree. `capability_id`, `scopes`, `purpose` and `expires_at` get no
-- column: nothing joins on them, and a column beside a field of the stored document
-- would be a second answer with no rule about which wins.
--
-- Discovery is not authority, and neither is this schema. A grant's composite foreign
-- key requires the exact `PolicySnapshot` row it names to exist in this workspace, and
-- the insert guard requires that snapshot to be pinned for this same run and pinned no
-- later than the grant is issued. Whether the granted capability actually appears in
-- that policy's `granted_capabilities` -- rather than only in its
-- `discovered_capabilities` -- is the accepted contract's rule, checked by
-- `semantics_runtime.validate_capability_grant` before the insert is issued. SQL is not
-- asked to re-implement a set membership test it cannot express against a document it
-- does not decode, and the repository is not permitted to skip one.
--
-- An approval is one-to-one with an approval-kind `Wait` of its own run, and both
-- halves are written while that wait is still pending. `UNIQUE (workspace_id, wait_id)`
-- is the one-to-one; the insert guards are the rest: a request names a wait of its own
-- run whose kind is `approval`, neither half lands on a wait that already has a
-- resolution, a request does not predate its wait, and a decision does not predate its
-- request nor fall after the approval's or the wait's deadline -- a decision recorded
-- past either deadline describes an approval that expired, which is a different fact.
--
-- The forward reference 0018 promised is not available here. 0018's
-- `omnivia_runtime_wait_resolutions.approval_id` comment says it "gains a reference
-- when that store lands"; SQLite cannot add a foreign key to an existing table without
-- rebuilding it, and rebuilding an append-only canonical record to add a constraint is
-- exactly the mutation these tables exist to forbid. So the reference runs the other
-- way -- `Approval.wait_id` carries a real foreign key to `omnivia_runtime_waits` --
-- and the exact `approval_id` a resolution quotes stays validated in the command and
-- semantic layer, where `ResolveWait` already checks it. 0018 is not edited.
--
-- What binds an approval to an exact action and state is `Wait.resume_digest`, which
-- 0018 already stores and `ResolveWait` already checks. This migration adds no second
-- approval digest and changes no contract: a resolution quoting any other digest is
-- refused before policy is consulted, so the recorded approval cannot be spent on a
-- state it was not granted for.
--
-- UPDATE and DELETE abort unconditionally on all four tables, for the current fenced
-- owner too. That is what makes a request a request and a decision a decision: neither
-- can be rewritten, erased, or replaced under its own name.
--
-- What is deliberately absent: any requester identity and any approval-to-grant column,
-- because accepted v1 states neither, and a column for a field the contract does not
-- have would be this database inventing a record; any authorization rule about who
-- `decided_by` may be, which is the `WaitResolutionPolicy` seam's decision and not a
-- shape this schema can check; any DML, because no fact in this database is honestly
-- classifiable as an approval or a grant and manufacturing one would invent a decision
-- nobody made; and any index beyond the constraints already provide, because the only
-- reads are by identifier and by run.
--
-- Identity, types, bounds, timestamps and digests follow 0018's and 0021's rules
-- exactly. The comment is bounded at the 2048 characters the accepted contract's own
-- `Message` allows, empty included, and the stored grant document at the 65536 bytes an
-- event detail gets -- the largest grant the contract accepts, with 64 scopes of 128
-- characters and every bounded field at its own maximum, is under 10 kilobytes, so this
-- bound refuses no record the contract accepts and adds no ceiling of its own.
--
-- Every comment in this file sits between statements and never inside one, for the
-- fingerprint-loader reason 0018 states.

CREATE TABLE IF NOT EXISTS omnivia_runtime_approvals (
    workspace_id    TEXT    NOT NULL,
    approval_id     TEXT    NOT NULL,
    run_id          TEXT    NOT NULL,
    wait_id         TEXT    NOT NULL,
    requested_at_us INTEGER NOT NULL,
    approver_role   TEXT    NOT NULL,
    assigned_to     TEXT,
    escalated_to    TEXT,
    expires_at_us   INTEGER,

    PRIMARY KEY (workspace_id, approval_id),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(approval_id) = 'text' AND length(approval_id) BETWEEN 1 AND 128
           AND approval_id GLOB '[A-Za-z0-9]*'
           AND approval_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(approval_id, char(0)) = 0),
    CHECK (typeof(run_id) = 'text' AND length(run_id) BETWEEN 1 AND 128
           AND run_id GLOB '[A-Za-z0-9]*'
           AND run_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(run_id, char(0)) = 0),
    CHECK (typeof(wait_id) = 'text' AND length(wait_id) BETWEEN 1 AND 128
           AND wait_id GLOB '[A-Za-z0-9]*'
           AND wait_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(wait_id, char(0)) = 0),
    CHECK (typeof(requested_at_us) = 'integer' AND requested_at_us > 0),
    CHECK (typeof(approver_role) = 'text' AND length(approver_role) BETWEEN 1 AND 128
           AND approver_role GLOB '[A-Za-z0-9]*'
           AND approver_role NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(approver_role, char(0)) = 0),
    CHECK (assigned_to IS NULL OR (typeof(assigned_to) = 'text'
           AND length(assigned_to) BETWEEN 1 AND 128
           AND assigned_to GLOB '[A-Za-z0-9]*'
           AND assigned_to NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(assigned_to, char(0)) = 0)),
    CHECK (escalated_to IS NULL OR (typeof(escalated_to) = 'text'
           AND length(escalated_to) BETWEEN 1 AND 128
           AND escalated_to GLOB '[A-Za-z0-9]*'
           AND escalated_to NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(escalated_to, char(0)) = 0)),
    CHECK (expires_at_us IS NULL OR (typeof(expires_at_us) = 'integer'
           AND expires_at_us >= requested_at_us)),

    UNIQUE (workspace_id, wait_id),

    FOREIGN KEY (workspace_id, run_id)
        REFERENCES omnivia_runtime_runs (workspace_id, run_id),
    FOREIGN KEY (workspace_id, wait_id)
        REFERENCES omnivia_runtime_waits (workspace_id, wait_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_runtime_approval_decisions (
    workspace_id  TEXT    NOT NULL,
    approval_id   TEXT    NOT NULL,
    decision      TEXT    NOT NULL,
    decided_at_us INTEGER NOT NULL,
    decided_by    TEXT    NOT NULL,
    audit_ref     TEXT    NOT NULL,

    PRIMARY KEY (workspace_id, approval_id),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(approval_id) = 'text' AND length(approval_id) BETWEEN 1 AND 128
           AND approval_id GLOB '[A-Za-z0-9]*'
           AND approval_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(approval_id, char(0)) = 0),
    CHECK (decision IN ('approved', 'rejected')),
    CHECK (typeof(decided_at_us) = 'integer' AND decided_at_us > 0),
    CHECK (typeof(decided_by) = 'text' AND length(decided_by) BETWEEN 1 AND 128
           AND decided_by GLOB '[A-Za-z0-9]*'
           AND decided_by NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(decided_by, char(0)) = 0),
    CHECK (typeof(audit_ref) = 'text' AND length(audit_ref) BETWEEN 1 AND 128
           AND audit_ref GLOB '[A-Za-z0-9]*'
           AND audit_ref NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(audit_ref, char(0)) = 0),

    FOREIGN KEY (workspace_id, approval_id)
        REFERENCES omnivia_runtime_approvals (workspace_id, approval_id),
    FOREIGN KEY (audit_ref, workspace_id)
        REFERENCES omnivia_application_audit_events (audit_ref, workspace_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_runtime_approval_comments (
    workspace_id TEXT NOT NULL,
    approval_id  TEXT NOT NULL,
    comment      TEXT NOT NULL,

    PRIMARY KEY (workspace_id, approval_id),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(approval_id) = 'text' AND length(approval_id) BETWEEN 1 AND 128
           AND approval_id GLOB '[A-Za-z0-9]*'
           AND approval_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(approval_id, char(0)) = 0),
    CHECK (typeof(comment) = 'text' AND length(comment) BETWEEN 0 AND 2048
           AND instr(comment, char(0)) = 0),

    FOREIGN KEY (workspace_id, approval_id)
        REFERENCES omnivia_runtime_approvals (workspace_id, approval_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_runtime_capability_grants (
    workspace_id        TEXT    NOT NULL,
    capability_grant_id TEXT    NOT NULL,
    run_id              TEXT    NOT NULL,
    policy_snapshot_id  TEXT    NOT NULL,
    granted_at_us       INTEGER NOT NULL,
    grant_json          TEXT    NOT NULL,
    grant_digest        TEXT    NOT NULL,
    grant_byte_length   INTEGER NOT NULL,

    PRIMARY KEY (workspace_id, capability_grant_id),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(capability_grant_id) = 'text'
           AND length(capability_grant_id) BETWEEN 1 AND 128
           AND capability_grant_id GLOB '[A-Za-z0-9]*'
           AND capability_grant_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(capability_grant_id, char(0)) = 0),
    CHECK (typeof(run_id) = 'text' AND length(run_id) BETWEEN 1 AND 128
           AND run_id GLOB '[A-Za-z0-9]*'
           AND run_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(run_id, char(0)) = 0),
    CHECK (typeof(policy_snapshot_id) = 'text'
           AND length(policy_snapshot_id) BETWEEN 1 AND 128
           AND policy_snapshot_id GLOB '[A-Za-z0-9]*'
           AND policy_snapshot_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(policy_snapshot_id, char(0)) = 0),
    CHECK (typeof(granted_at_us) = 'integer' AND granted_at_us > 0),
    CHECK (typeof(grant_json) = 'text'
           AND length(CAST(grant_json AS BLOB)) BETWEEN 2 AND 65536),
    CHECK (typeof(grant_digest) = 'text' AND length(grant_digest) = 71
           AND substr(grant_digest, 1, 7) = 'sha256:'
           AND substr(grant_digest, 8) NOT GLOB '*[^0-9a-f]*'),
    CHECK (typeof(grant_byte_length) = 'integer'
           AND grant_byte_length = length(CAST(grant_json AS BLOB))),

    FOREIGN KEY (workspace_id, run_id)
        REFERENCES omnivia_runtime_runs (workspace_id, run_id),
    FOREIGN KEY (workspace_id, policy_snapshot_id)
        REFERENCES omnivia_runtime_policy_snapshots (workspace_id, policy_snapshot_id)
) WITHOUT ROWID;

-- The four INSERT guards below repeat the complete connection-authority,
-- mutation-guard, workspace-state and lease predicate 0005 established, then add the
-- correlation and ordering rules each fact has. UPDATE and DELETE abort unconditionally
-- on all four tables -- for the current fenced owner too.

CREATE TRIGGER IF NOT EXISTS omnivia_guard_runtime_approvals_insert
BEFORE INSERT ON omnivia_runtime_approvals
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_runtime_approvals')
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
    SELECT RAISE(ABORT, 'omnivia: an approval is requested on an approval wait of its own run')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_runtime_waits
        WHERE workspace_id = NEW.workspace_id AND wait_id = NEW.wait_id
          AND run_id = NEW.run_id AND kind = 'approval');
    SELECT RAISE(ABORT, 'omnivia: an approval must not be requested before its wait')
    WHERE NEW.requested_at_us < (
        SELECT created_at_us FROM omnivia_runtime_waits
        WHERE workspace_id = NEW.workspace_id AND wait_id = NEW.wait_id);
    SELECT RAISE(ABORT, 'omnivia: an approval is requested only on a wait still pending')
    WHERE EXISTS (
        SELECT 1 FROM omnivia_runtime_wait_resolutions
        WHERE workspace_id = NEW.workspace_id AND wait_id = NEW.wait_id);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_runtime_approvals_update
BEFORE UPDATE ON omnivia_runtime_approvals
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_runtime_approvals is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_runtime_approvals_delete
BEFORE DELETE ON omnivia_runtime_approvals
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_runtime_approvals is append-only; DELETE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_runtime_approval_decisions_insert
BEFORE INSERT ON omnivia_runtime_approval_decisions
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_runtime_approval_decisions')
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
    SELECT RAISE(ABORT, 'omnivia: a decision must not predate the approval it answers')
    WHERE NEW.decided_at_us < (
        SELECT requested_at_us FROM omnivia_runtime_approvals
        WHERE workspace_id = NEW.workspace_id AND approval_id = NEW.approval_id);
    SELECT RAISE(ABORT, 'omnivia: an approval decided past its deadline has expired, not been decided')
    WHERE NEW.decided_at_us > COALESCE((
        SELECT expires_at_us FROM omnivia_runtime_approvals
        WHERE workspace_id = NEW.workspace_id AND approval_id = NEW.approval_id),
        NEW.decided_at_us);
    SELECT RAISE(ABORT, 'omnivia: an approval decided past its wait deadline has expired, not been decided')
    WHERE NEW.decided_at_us > COALESCE((
        SELECT w.expires_at_us FROM omnivia_runtime_waits w
        JOIN omnivia_runtime_approvals a
          ON a.workspace_id = w.workspace_id AND a.wait_id = w.wait_id
        WHERE a.workspace_id = NEW.workspace_id AND a.approval_id = NEW.approval_id),
        NEW.decided_at_us);
    SELECT RAISE(ABORT, 'omnivia: an approval is decided only while its wait is still pending')
    WHERE EXISTS (
        SELECT 1 FROM omnivia_runtime_wait_resolutions r
        JOIN omnivia_runtime_approvals a
          ON a.workspace_id = r.workspace_id AND a.wait_id = r.wait_id
        WHERE a.workspace_id = NEW.workspace_id AND a.approval_id = NEW.approval_id);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_runtime_approval_decisions_update
BEFORE UPDATE ON omnivia_runtime_approval_decisions
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_runtime_approval_decisions is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_runtime_approval_decisions_delete
BEFORE DELETE ON omnivia_runtime_approval_decisions
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_runtime_approval_decisions is append-only; DELETE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_runtime_approval_comments_insert
BEFORE INSERT ON omnivia_runtime_approval_comments
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_runtime_approval_comments')
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
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_runtime_approval_comments_update
BEFORE UPDATE ON omnivia_runtime_approval_comments
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_runtime_approval_comments is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_runtime_approval_comments_delete
BEFORE DELETE ON omnivia_runtime_approval_comments
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_runtime_approval_comments is append-only; DELETE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_runtime_capability_grants_insert
BEFORE INSERT ON omnivia_runtime_capability_grants
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_runtime_capability_grants')
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
    SELECT RAISE(ABORT, 'omnivia: a grant must name a policy snapshot pinned for its own run')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_runtime_policy_snapshots
        WHERE workspace_id = NEW.workspace_id
          AND policy_snapshot_id = NEW.policy_snapshot_id
          AND run_id = NEW.run_id);
    SELECT RAISE(ABORT, 'omnivia: a grant must not be issued before the policy that backs it')
    WHERE NEW.granted_at_us < (
        SELECT pinned_at_us FROM omnivia_runtime_policy_snapshots
        WHERE workspace_id = NEW.workspace_id
          AND policy_snapshot_id = NEW.policy_snapshot_id);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_runtime_capability_grants_update
BEFORE UPDATE ON omnivia_runtime_capability_grants
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_runtime_capability_grants is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_runtime_capability_grants_delete
BEFORE DELETE ON omnivia_runtime_capability_grants
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_runtime_capability_grants is append-only; DELETE is never permitted');
END;
