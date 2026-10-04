-- Runtime-owned completion decisions for final run settlement (DEV-REQ-137, Agent Runtime).
--
-- Additive only; allocation 0066 (Agent Runtime, predecessor 0065). One append-only table and
-- three guard triggers. A row is the Runtime's own accepted decision that one run's final step may
-- terminalize: the exact accepted criteria it proved, and the raw evidence identity each criterion
-- rests on. It is written in the same fenced transaction as that terminalization, so a decision
-- without a settled run, or a settled run without a decision, cannot be committed.
--
-- Identity. `decision_digest` is `sha256:` over the canonical decision body, which excludes the time
-- it was recorded. `(workspace_id, run_id)` is unique, so a run carries at most one decision; an
-- exact replay dedups in the storage layer and a different body is refused there. The body is kept
-- as canonical JSON and re-verified on every read, because the criteria, evidence and attribution
-- live there rather than in columns.
--
-- Every write runs inside the caller's `fenced_transaction`. The INSERT guard carries the same
-- connection-authority, guard, workspace-state and lease predicate as the other guarded tables,
-- binds the row to the open workspace and to its current fencing generation. UPDATE and DELETE are
-- refused for the fenced owner too.
--
-- No DML, and no comment sits inside a statement below, for the migrator's statement splitter.

CREATE TABLE IF NOT EXISTS omnivia_runtime_completion_decisions (
    workspace_id                TEXT    NOT NULL,
    decision_digest             TEXT    NOT NULL,
    run_id                      TEXT    NOT NULL,
    decision                    TEXT    NOT NULL,
    decision_body               TEXT    NOT NULL,
    decided_under_generation    INTEGER NOT NULL,
    decided_at_us               INTEGER NOT NULL,

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(decision_digest) = 'text' AND length(decision_digest) = 71
           AND substr(decision_digest, 1, 7) = 'sha256:'
           AND substr(decision_digest, 8) NOT GLOB '*[^0-9a-f]*'),
    CHECK (typeof(run_id) = 'text' AND length(run_id) BETWEEN 1 AND 128
           AND run_id GLOB '[A-Za-z0-9]*'
           AND run_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(run_id, char(0)) = 0),
    CHECK (decision = 'accepted'),
    CHECK (typeof(decision_body) = 'text' AND length(decision_body) BETWEEN 2 AND 1048576
           AND substr(decision_body, 1, 1) = '{'),
    CHECK (typeof(decided_under_generation) = 'integer'
           AND decided_under_generation BETWEEN 1 AND 9223372036854775807),
    CHECK (typeof(decided_at_us) = 'integer' AND decided_at_us BETWEEN 1 AND 9223372036854775807),

    PRIMARY KEY (workspace_id, decision_digest),
    UNIQUE (workspace_id, run_id)
) WITHOUT ROWID;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_runtime_completion_decisions_insert
BEFORE INSERT ON omnivia_runtime_completion_decisions
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_runtime_completion_decisions')
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
    SELECT RAISE(ABORT, 'omnivia: a completion decision must bind the open workspace')
    WHERE NEW.workspace_id IS NOT (
        SELECT workspace_id FROM omnivia_workspace_state WHERE singleton = 1);
    SELECT RAISE(ABORT, 'omnivia: a completion decision must bind the current fencing generation')
    WHERE NEW.decided_under_generation IS NOT (
        SELECT fencing_generation FROM omnivia_workspace_state WHERE singleton = 1);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_runtime_completion_decisions_update
BEFORE UPDATE ON omnivia_runtime_completion_decisions
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_runtime_completion_decisions is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_runtime_completion_decisions_delete
BEFORE DELETE ON omnivia_runtime_completion_decisions
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_runtime_completion_decisions is append-only; DELETE is never permitted');
END;
