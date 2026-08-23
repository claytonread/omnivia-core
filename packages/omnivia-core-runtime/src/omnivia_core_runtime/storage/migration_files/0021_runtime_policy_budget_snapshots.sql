-- Canonical PolicySnapshot and BudgetSnapshot records (RT-202).
--
-- Additive only. Two append-only tables and six statement triggers, on top of the
-- canonical records migrations 0018 and 0019 added. Every row here belongs to an
-- existing `omnivia_runtime_runs` row; nothing here creates a run, changes a run's
-- status, or duplicates the event stream that already states it.
--
--   omnivia_runtime_policy_snapshots   one immutable policy decision, at one revision
--   omnivia_runtime_budget_snapshots   one immutable budget decision, at one revision
--
-- The stored document is the record. Each row carries the complete accepted v1
-- snapshot as Core canonical-JSON bytes, with the `sha256:<64 lowercase hex>` digest of
-- exactly those bytes and their byte length beside them, so what was hashed is recorded
-- next to the hash and a reader can recompute both before believing either. The digest
-- addresses the whole wire document -- the contract's own `policy_snapshot_id` or
-- `budget_snapshot_id` included -- and is not that identifier and does not derive it:
-- an id hashed from a document that contains the id is not addressable at all. The
-- identifier column, the run, the revision and the pinned instant are the *selectors*
-- this database indexes and joins on, copied out of that same document; the repository
-- re-derives each of them from the decoded document on every read and refuses a row
-- whose columns and document disagree.
--
-- Monotonic, and enforced where it can be. A revision must advance on the latest one
-- stored for its run, and may not be pinned before one already recorded, so a history
-- that went backwards is unrepresentable rather than merely discouraged. Whether the
-- *content* of a later revision is a legal successor -- grants narrowing and never
-- widening, ceilings narrowing and never widening, consumption never decreasing -- is
-- the accepted contract's rule, checked by `semantics_runtime`'s progression validators
-- before the insert is issued. SQL is not asked to re-implement a capability-set
-- comparison it cannot express, and the repository is not permitted to skip one.
--
-- UPDATE and DELETE abort unconditionally on both tables, for the current fenced owner
-- too. That is what makes an accepted decision an accepted decision: a widening cannot
-- rewrite the row it disagrees with, cannot delete it, and cannot take its place -- the
-- identity primary key and the per-run revision unique constraint refuse a replacement
-- under the same name, and a legal narrowing is a new row rather than an edit to an old
-- one. A snapshot may legitimately be re-pinned after a run has reached a terminal
-- status, so, like 0019's guards and unlike 0018's step and attempt guards, neither
-- insert guard below refuses a write merely because the run is terminal.
--
-- What is deliberately absent: a capability, grant, approval or effect relation (still
-- RT-202+'s successors'), any second copy of the audit reference, decision reason or
-- capability sets as columns -- they are fields of the stored document, and a column
-- beside one would be a second answer with no rule about which wins -- and any index
-- beyond the two constraints already provide, because the only reads are by identifier
-- and by run in revision order.
--
-- Identity, types, bounds, timestamps and digests follow 0018's rules exactly, with one
-- deliberate difference: the stored document is bounded at 131072 bytes rather than the
-- 65536 an event detail gets. The accepted contract allows a policy 256 granted and 256
-- discovered capability identifiers of up to 128 characters each, which is a legal
-- snapshot of roughly 67 kilobytes, and a column that refused a record the contract
-- accepts would be a bound this database has no authority to impose.
--
-- Every comment in this file sits between statements and never inside one, for the
-- fingerprint-loader reason 0018 states.

CREATE TABLE IF NOT EXISTS omnivia_runtime_policy_snapshots (
    workspace_id         TEXT    NOT NULL,
    policy_snapshot_id   TEXT    NOT NULL,
    run_id               TEXT    NOT NULL,
    revision             INTEGER NOT NULL,
    pinned_at_us         INTEGER NOT NULL,
    snapshot_json        TEXT    NOT NULL,
    snapshot_digest      TEXT    NOT NULL,
    snapshot_byte_length INTEGER NOT NULL,

    PRIMARY KEY (workspace_id, policy_snapshot_id),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(policy_snapshot_id) = 'text'
           AND length(policy_snapshot_id) BETWEEN 1 AND 128
           AND policy_snapshot_id GLOB '[A-Za-z0-9]*'
           AND policy_snapshot_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(policy_snapshot_id, char(0)) = 0),
    CHECK (typeof(run_id) = 'text' AND length(run_id) BETWEEN 1 AND 128
           AND run_id GLOB '[A-Za-z0-9]*'
           AND run_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(run_id, char(0)) = 0),
    CHECK (typeof(revision) = 'integer' AND revision >= 1),
    CHECK (typeof(pinned_at_us) = 'integer' AND pinned_at_us > 0),
    CHECK (typeof(snapshot_json) = 'text'
           AND length(CAST(snapshot_json AS BLOB)) BETWEEN 2 AND 131072),
    CHECK (typeof(snapshot_digest) = 'text' AND length(snapshot_digest) = 71
           AND substr(snapshot_digest, 1, 7) = 'sha256:'
           AND substr(snapshot_digest, 8) NOT GLOB '*[^0-9a-f]*'),
    CHECK (typeof(snapshot_byte_length) = 'integer'
           AND snapshot_byte_length = length(CAST(snapshot_json AS BLOB))),

    UNIQUE (workspace_id, run_id, revision),

    FOREIGN KEY (workspace_id, run_id)
        REFERENCES omnivia_runtime_runs (workspace_id, run_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_runtime_budget_snapshots (
    workspace_id         TEXT    NOT NULL,
    budget_snapshot_id   TEXT    NOT NULL,
    run_id               TEXT    NOT NULL,
    revision             INTEGER NOT NULL,
    pinned_at_us         INTEGER NOT NULL,
    snapshot_json        TEXT    NOT NULL,
    snapshot_digest      TEXT    NOT NULL,
    snapshot_byte_length INTEGER NOT NULL,

    PRIMARY KEY (workspace_id, budget_snapshot_id),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(budget_snapshot_id) = 'text'
           AND length(budget_snapshot_id) BETWEEN 1 AND 128
           AND budget_snapshot_id GLOB '[A-Za-z0-9]*'
           AND budget_snapshot_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(budget_snapshot_id, char(0)) = 0),
    CHECK (typeof(run_id) = 'text' AND length(run_id) BETWEEN 1 AND 128
           AND run_id GLOB '[A-Za-z0-9]*'
           AND run_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(run_id, char(0)) = 0),
    CHECK (typeof(revision) = 'integer' AND revision >= 1),
    CHECK (typeof(pinned_at_us) = 'integer' AND pinned_at_us > 0),
    CHECK (typeof(snapshot_json) = 'text'
           AND length(CAST(snapshot_json AS BLOB)) BETWEEN 2 AND 131072),
    CHECK (typeof(snapshot_digest) = 'text' AND length(snapshot_digest) = 71
           AND substr(snapshot_digest, 1, 7) = 'sha256:'
           AND substr(snapshot_digest, 8) NOT GLOB '*[^0-9a-f]*'),
    CHECK (typeof(snapshot_byte_length) = 'integer'
           AND snapshot_byte_length = length(CAST(snapshot_json AS BLOB))),

    UNIQUE (workspace_id, run_id, revision),

    FOREIGN KEY (workspace_id, run_id)
        REFERENCES omnivia_runtime_runs (workspace_id, run_id)
) WITHOUT ROWID;

-- The two INSERT guards below repeat the complete connection-authority,
-- mutation-guard, workspace-state and lease predicate 0005 established, then add the
-- ordering rules a snapshot history has. UPDATE and DELETE abort unconditionally on
-- both tables -- for the current fenced owner too.

CREATE TRIGGER IF NOT EXISTS omnivia_guard_runtime_policy_snapshots_insert
BEFORE INSERT ON omnivia_runtime_policy_snapshots
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_runtime_policy_snapshots')
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
    SELECT RAISE(ABORT, 'omnivia: a policy snapshot must not predate its run')
    WHERE NEW.pinned_at_us < (
        SELECT created_at_us FROM omnivia_runtime_runs
        WHERE workspace_id = NEW.workspace_id AND run_id = NEW.run_id);
    SELECT RAISE(ABORT, 'omnivia: a policy revision must advance on the latest stored revision')
    WHERE NEW.revision <= (
        SELECT MAX(revision) FROM omnivia_runtime_policy_snapshots
        WHERE workspace_id = NEW.workspace_id AND run_id = NEW.run_id);
    SELECT RAISE(ABORT, 'omnivia: a policy revision must not be pinned before an earlier one')
    WHERE NEW.pinned_at_us < (
        SELECT MAX(pinned_at_us) FROM omnivia_runtime_policy_snapshots
        WHERE workspace_id = NEW.workspace_id AND run_id = NEW.run_id);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_runtime_policy_snapshots_update
BEFORE UPDATE ON omnivia_runtime_policy_snapshots
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_runtime_policy_snapshots is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_runtime_policy_snapshots_delete
BEFORE DELETE ON omnivia_runtime_policy_snapshots
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_runtime_policy_snapshots is append-only; DELETE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_runtime_budget_snapshots_insert
BEFORE INSERT ON omnivia_runtime_budget_snapshots
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_runtime_budget_snapshots')
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
    SELECT RAISE(ABORT, 'omnivia: a budget snapshot must not predate its run')
    WHERE NEW.pinned_at_us < (
        SELECT created_at_us FROM omnivia_runtime_runs
        WHERE workspace_id = NEW.workspace_id AND run_id = NEW.run_id);
    SELECT RAISE(ABORT, 'omnivia: a budget revision must advance on the latest stored revision')
    WHERE NEW.revision <= (
        SELECT MAX(revision) FROM omnivia_runtime_budget_snapshots
        WHERE workspace_id = NEW.workspace_id AND run_id = NEW.run_id);
    SELECT RAISE(ABORT, 'omnivia: a budget revision must not be pinned before an earlier one')
    WHERE NEW.pinned_at_us < (
        SELECT MAX(pinned_at_us) FROM omnivia_runtime_budget_snapshots
        WHERE workspace_id = NEW.workspace_id AND run_id = NEW.run_id);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_runtime_budget_snapshots_update
BEFORE UPDATE ON omnivia_runtime_budget_snapshots
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_runtime_budget_snapshots is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_runtime_budget_snapshots_delete
BEFORE DELETE ON omnivia_runtime_budget_snapshots
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_runtime_budget_snapshots is append-only; DELETE is never permitted');
END;
