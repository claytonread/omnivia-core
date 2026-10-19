-- Evidence-only quarantine of review findings that cannot be validated (DEV-REQ-176, Agent Runtime).
--
-- Additive only; allocation 0065 (Agent Runtime, predecessor 0064). One append-only table and
-- three guard triggers. A row records that a finding was submitted against a stale generation or
-- against a generation, workspace or run that is absent, together with its content digest and
-- attribution. It is evidence and nothing else: no column names a Task, lease, job, approval,
-- acceptance or validation, and no trigger here writes to any table but this one.
--
-- Identity. `finding_digest` is `sha256:` over the canonical envelope, so the same submission
-- is one row and different evidence bytes or different reason or binding facts are distinct rows.
-- `(workspace_id, idempotency_key)` is unique, so one caller key cannot name two envelopes.
--
-- Reason shapes. `stale_generation` names an observed generation strictly below the generation
-- the row was quarantined under, and a binding. `missing_generation` has no observed generation.
-- `missing_workspace` has no observed binding. `missing_run` has no run. Each shape is a CHECK, so
-- a row that claims validation or names no reason cannot be written by any writer.
--
-- Every write runs inside the caller's `fenced_transaction`. The INSERT guard carries the same
-- connection-authority, guard, workspace-state and lease predicate as the other guarded tables,
-- binds the row to the open workspace and to its current fencing generation, and refuses a stale
-- observed generation that the CHECK did not already decide. UPDATE and DELETE are refused for
-- the fenced owner too.
--
-- No DML, and no comment sits inside a statement below, for the migrator's statement splitter.

CREATE TABLE IF NOT EXISTS omnivia_review_finding_quarantines (
    workspace_id                 TEXT    NOT NULL,
    finding_digest               TEXT    NOT NULL,
    idempotency_key              TEXT    NOT NULL,
    run_id                       TEXT,
    candidate_id                 TEXT    NOT NULL,
    evidence_id                  TEXT    NOT NULL,
    content_digest               TEXT    NOT NULL,
    reason                       TEXT    NOT NULL,
    observed_generation          INTEGER,
    observed_binding             TEXT,
    quarantined_under_generation INTEGER NOT NULL,
    attributed_to                TEXT    NOT NULL,
    recorded_at_us               INTEGER NOT NULL,

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(finding_digest) = 'text' AND length(finding_digest) = 71
           AND substr(finding_digest, 1, 7) = 'sha256:'
           AND substr(finding_digest, 8) NOT GLOB '*[^0-9a-f]*'),
    CHECK (typeof(idempotency_key) = 'text' AND length(idempotency_key) BETWEEN 1 AND 128
           AND idempotency_key GLOB '[A-Za-z0-9]*'
           AND idempotency_key NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(idempotency_key, char(0)) = 0),
    CHECK (run_id IS NULL OR (typeof(run_id) = 'text' AND length(run_id) BETWEEN 1 AND 128
           AND run_id GLOB '[A-Za-z0-9]*'
           AND run_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(run_id, char(0)) = 0)),
    CHECK (typeof(candidate_id) = 'text' AND length(candidate_id) BETWEEN 1 AND 128
           AND candidate_id GLOB '[A-Za-z0-9]*'
           AND candidate_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(candidate_id, char(0)) = 0),
    CHECK (typeof(evidence_id) = 'text' AND length(evidence_id) BETWEEN 1 AND 128
           AND evidence_id GLOB '[A-Za-z0-9]*'
           AND evidence_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(evidence_id, char(0)) = 0),
    CHECK (typeof(content_digest) = 'text' AND length(content_digest) = 71
           AND substr(content_digest, 1, 7) = 'sha256:'
           AND substr(content_digest, 8) NOT GLOB '*[^0-9a-f]*'),
    CHECK (reason IN ('stale_generation', 'missing_generation', 'missing_workspace', 'missing_run')),
    CHECK (observed_generation IS NULL OR (typeof(observed_generation) = 'integer'
           AND observed_generation BETWEEN 0 AND 9223372036854775807)),
    CHECK (observed_binding IS NULL OR (typeof(observed_binding) = 'text'
           AND length(observed_binding) BETWEEN 1 AND 128
           AND observed_binding GLOB '[A-Za-z0-9]*'
           AND observed_binding NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(observed_binding, char(0)) = 0)),
    CHECK (typeof(quarantined_under_generation) = 'integer'
           AND quarantined_under_generation BETWEEN 1 AND 9223372036854775807),
    CHECK (typeof(attributed_to) = 'text' AND length(attributed_to) BETWEEN 1 AND 128
           AND attributed_to GLOB '[A-Za-z0-9]*'
           AND attributed_to NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(attributed_to, char(0)) = 0),
    CHECK (typeof(recorded_at_us) = 'integer' AND recorded_at_us BETWEEN 1 AND 9223372036854775807),
    CHECK ((reason = 'stale_generation' AND run_id IS NOT NULL
            AND observed_generation IS NOT NULL AND observed_binding IS NOT NULL
            AND observed_generation < quarantined_under_generation)
        OR (reason = 'missing_generation' AND run_id IS NOT NULL
            AND observed_generation IS NULL)
        OR (reason = 'missing_workspace' AND run_id IS NOT NULL
            AND observed_binding IS NULL)
        OR (reason = 'missing_run' AND run_id IS NULL)),

    PRIMARY KEY (workspace_id, finding_digest),
    UNIQUE (workspace_id, idempotency_key)
) WITHOUT ROWID;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_review_finding_quarantines_insert
BEFORE INSERT ON omnivia_review_finding_quarantines
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_review_finding_quarantines')
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
    SELECT RAISE(ABORT, 'omnivia: a review finding quarantine must bind the open workspace')
    WHERE NEW.workspace_id IS NOT (
        SELECT workspace_id FROM omnivia_workspace_state WHERE singleton = 1);
    SELECT RAISE(ABORT, 'omnivia: a review finding quarantine must bind the current fencing generation')
    WHERE NEW.quarantined_under_generation IS NOT (
        SELECT fencing_generation FROM omnivia_workspace_state WHERE singleton = 1);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_review_finding_quarantines_update
BEFORE UPDATE ON omnivia_review_finding_quarantines
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_review_finding_quarantines is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_review_finding_quarantines_delete
BEFORE DELETE ON omnivia_review_finding_quarantines
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_review_finding_quarantines is append-only; DELETE is never permitted');
END;
