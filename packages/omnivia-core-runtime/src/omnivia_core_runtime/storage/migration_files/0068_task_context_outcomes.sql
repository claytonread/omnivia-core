-- Task-context exports and outcome requests (DEV-REQ-159 and DEV-REQ-008, Core routine-flow entry; migration 0068).
--
-- Additive only; allocation 0068 (predecessor 0067). Three tables and their guard triggers.
-- A row in `omnivia_task_context_exports` is one immutable, content-addressed export of one already-assembled
-- task-context handoff. Its identifier is `tcx-` followed by the SHA-256 of the canonical document it names,
-- so the identifier cannot name any other content. The stored document is exactly the canonical bytes that
-- the row's `byte_estimate` and `token_estimate` describe, and the bounds are checked here so that a row
-- cannot claim to fit a budget it does not fit. A row in `omnivia_outcome_requests` is one received outcome
-- request. It names its export by identity and carries the objective verbatim, and the insert guard refuses
-- it unless that export was recorded under the current fencing generation. A structured request also carries
-- its accepted admission: the canonical reviewed summary, its identity, the Project it names and the active
-- Project context generation it was accepted under. Those four columns are all set or all null, and a
-- structured row is refused unless that Project is the Workspace's active context at that generation.
-- `omnivia_project_contexts` holds one row per Workspace: its active Core Project and the generation of that
-- choice. The first choice is generation 1, and each change to a different Project advances the generation by
-- one. The row is updated in place, but only through the guarded write and only by that one advance, so a
-- generation can never go back or be set by a caller.
--
-- Every write runs inside the caller's `fenced_transaction`. The INSERT guards carry the same connection-
-- authority, guard, workspace-state and lease predicate as the other guarded tables, and bind each row to the
-- open workspace and to its current fencing generation. UPDATE and DELETE are refused for everyone, so an
-- export or a request is never edited or withdrawn.
--
-- Path safety. Nothing here names a filesystem path. Identifiers and principals are restricted to the same
-- closed character set as the other tables, and the document is opaque text that is never used as a path.
--
-- No DML, and no comment sits inside a statement below, for the migrator's statement splitter.

CREATE TABLE IF NOT EXISTS omnivia_task_context_exports (
    workspace_id                TEXT    NOT NULL,
    export_id                   TEXT    NOT NULL,
    content_identity            TEXT    NOT NULL,
    exported_by                 TEXT    NOT NULL,
    source_handoff_identity     TEXT    NOT NULL,
    policy_digest               TEXT    NOT NULL,
    fencing_generation          INTEGER NOT NULL,
    token_budget                INTEGER NOT NULL,
    byte_budget                 INTEGER NOT NULL,
    byte_estimate               INTEGER NOT NULL,
    token_estimate              INTEGER NOT NULL,
    created_at_us               INTEGER NOT NULL,
    document_json               TEXT    NOT NULL,

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(export_id) = 'text' AND length(export_id) = 68
           AND substr(export_id, 1, 4) = 'tcx-'
           AND substr(export_id, 5) NOT GLOB '*[^0-9a-f]*'),
    CHECK (typeof(content_identity) = 'text' AND length(content_identity) = 64
           AND content_identity NOT GLOB '*[^0-9a-f]*'
           AND export_id = 'tcx-' || content_identity),
    CHECK (typeof(exported_by) = 'text' AND length(exported_by) BETWEEN 1 AND 128
           AND exported_by GLOB '[A-Za-z0-9]*'
           AND exported_by NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(exported_by, char(0)) = 0),
    CHECK (typeof(source_handoff_identity) = 'text' AND length(source_handoff_identity) = 64
           AND source_handoff_identity NOT GLOB '*[^0-9a-f]*'),
    CHECK (typeof(policy_digest) = 'text' AND length(policy_digest) = 64
           AND policy_digest NOT GLOB '*[^0-9a-f]*'),
    CHECK (typeof(fencing_generation) = 'integer'
           AND fencing_generation BETWEEN 1 AND 9223372036854775807),
    CHECK (typeof(token_budget) = 'integer' AND token_budget BETWEEN 1 AND 4000000),
    CHECK (typeof(byte_budget) = 'integer' AND byte_budget BETWEEN 1 AND 1048576),
    CHECK (typeof(byte_estimate) = 'integer' AND byte_estimate BETWEEN 1 AND byte_budget),
    CHECK (typeof(token_estimate) = 'integer' AND token_estimate BETWEEN 1 AND token_budget
           AND token_estimate = (byte_estimate + 3) / 4),
    CHECK (typeof(created_at_us) = 'integer' AND created_at_us BETWEEN 1 AND 9223372036854775807),
    CHECK (typeof(document_json) = 'text' AND length(CAST(document_json AS BLOB)) = byte_estimate),

    PRIMARY KEY (workspace_id, export_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_project_contexts (
    workspace_id                TEXT    NOT NULL,
    project_id                  TEXT    NOT NULL,
    context_generation          INTEGER NOT NULL,
    fencing_generation          INTEGER NOT NULL,
    switched_by                 TEXT    NOT NULL,
    switched_at_us              INTEGER NOT NULL,

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(project_id) = 'text' AND length(project_id) BETWEEN 1 AND 128
           AND project_id GLOB '[A-Za-z0-9]*'
           AND project_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(project_id, char(0)) = 0),
    CHECK (typeof(context_generation) = 'integer'
           AND context_generation BETWEEN 1 AND 9223372036854775807),
    CHECK (typeof(fencing_generation) = 'integer'
           AND fencing_generation BETWEEN 1 AND 9223372036854775807),
    CHECK (typeof(switched_by) = 'text' AND length(switched_by) BETWEEN 1 AND 128
           AND switched_by GLOB '[A-Za-z0-9]*'
           AND switched_by NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(switched_by, char(0)) = 0),
    CHECK (typeof(switched_at_us) = 'integer' AND switched_at_us BETWEEN 1 AND 9223372036854775807),

    PRIMARY KEY (workspace_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_outcome_requests (
    workspace_id                TEXT    NOT NULL,
    outcome_request_id          TEXT    NOT NULL,
    requested_by                TEXT    NOT NULL,
    objective                   TEXT    NOT NULL,
    export_id                   TEXT    NOT NULL,
    source_handoff_identity     TEXT    NOT NULL,
    status                      TEXT    NOT NULL,
    fencing_generation          INTEGER NOT NULL,
    created_at_us               INTEGER NOT NULL,
    project_id                  TEXT,
    admission_identity          TEXT,
    admission_json              TEXT,
    context_generation          INTEGER,

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(outcome_request_id) = 'text' AND length(outcome_request_id) = 71
           AND substr(outcome_request_id, 1, 7) = 'outreq-'
           AND substr(outcome_request_id, 8) NOT GLOB '*[^0-9a-f]*'),
    CHECK (typeof(requested_by) = 'text' AND length(requested_by) BETWEEN 1 AND 128
           AND requested_by GLOB '[A-Za-z0-9]*'
           AND requested_by NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(requested_by, char(0)) = 0),
    CHECK (typeof(objective) = 'text' AND length(CAST(objective AS BLOB)) BETWEEN 1 AND 8192),
    CHECK (typeof(export_id) = 'text' AND length(export_id) = 68
           AND substr(export_id, 1, 4) = 'tcx-'
           AND substr(export_id, 5) NOT GLOB '*[^0-9a-f]*'),
    CHECK (typeof(source_handoff_identity) = 'text' AND length(source_handoff_identity) = 64
           AND source_handoff_identity NOT GLOB '*[^0-9a-f]*'),
    CHECK (status = 'received'),
    CHECK (typeof(fencing_generation) = 'integer'
           AND fencing_generation BETWEEN 1 AND 9223372036854775807),
    CHECK (typeof(created_at_us) = 'integer' AND created_at_us BETWEEN 1 AND 9223372036854775807),
    CHECK ((project_id IS NULL AND admission_identity IS NULL AND admission_json IS NULL
            AND context_generation IS NULL)
           OR (typeof(project_id) = 'text' AND length(project_id) BETWEEN 1 AND 128
               AND project_id GLOB '[A-Za-z0-9]*'
               AND project_id NOT GLOB '*[^A-Za-z0-9._:-]*'
               AND typeof(admission_identity) = 'text' AND length(admission_identity) = 64
               AND admission_identity NOT GLOB '*[^0-9a-f]*'
               AND typeof(admission_json) = 'text'
               AND length(CAST(admission_json AS BLOB)) BETWEEN 1 AND 65536
               AND typeof(context_generation) = 'integer'
               AND context_generation BETWEEN 1 AND 9223372036854775807)),

    PRIMARY KEY (workspace_id, outcome_request_id),
    FOREIGN KEY (workspace_id, export_id)
        REFERENCES omnivia_task_context_exports (workspace_id, export_id)
) WITHOUT ROWID;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_task_context_exports_insert
BEFORE INSERT ON omnivia_task_context_exports
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_task_context_exports')
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
    SELECT RAISE(ABORT, 'omnivia: a task-context export must bind the open workspace')
    WHERE NEW.workspace_id IS NOT (
        SELECT workspace_id FROM omnivia_workspace_state WHERE singleton = 1);
    SELECT RAISE(ABORT, 'omnivia: a task-context export must bind the current fencing generation')
    WHERE NEW.fencing_generation IS NOT (
        SELECT fencing_generation FROM omnivia_workspace_state WHERE singleton = 1);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_task_context_exports_update
BEFORE UPDATE ON omnivia_task_context_exports
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_task_context_exports is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_task_context_exports_delete
BEFORE DELETE ON omnivia_task_context_exports
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_task_context_exports is append-only; DELETE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_outcome_requests_insert
BEFORE INSERT ON omnivia_outcome_requests
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_outcome_requests')
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
    SELECT RAISE(ABORT, 'omnivia: an outcome request must bind the open workspace')
    WHERE NEW.workspace_id IS NOT (
        SELECT workspace_id FROM omnivia_workspace_state WHERE singleton = 1);
    SELECT RAISE(ABORT, 'omnivia: an outcome request must bind the current fencing generation')
    WHERE NEW.fencing_generation IS NOT (
        SELECT fencing_generation FROM omnivia_workspace_state WHERE singleton = 1);
    SELECT RAISE(ABORT, 'omnivia: an outcome request must name an export recorded under the current fencing generation')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_task_context_exports e
        WHERE e.workspace_id = NEW.workspace_id
          AND e.export_id = NEW.export_id
          AND e.source_handoff_identity = NEW.source_handoff_identity
          AND e.fencing_generation = NEW.fencing_generation);
    SELECT RAISE(ABORT, 'omnivia: a structured outcome request must name the active Project at its context generation')
    WHERE NEW.project_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM omnivia_project_contexts c
        WHERE c.workspace_id = NEW.workspace_id
          AND c.project_id = NEW.project_id
          AND c.context_generation = NEW.context_generation);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_outcome_requests_update
BEFORE UPDATE ON omnivia_outcome_requests
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_outcome_requests is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_outcome_requests_delete
BEFORE DELETE ON omnivia_outcome_requests
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_outcome_requests is append-only; DELETE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_project_contexts_insert
BEFORE INSERT ON omnivia_project_contexts
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_project_contexts')
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
    SELECT RAISE(ABORT, 'omnivia: a Project context must bind the open workspace')
    WHERE NEW.workspace_id IS NOT (
        SELECT workspace_id FROM omnivia_workspace_state WHERE singleton = 1);
    SELECT RAISE(ABORT, 'omnivia: a Project context must bind the current fencing generation')
    WHERE NEW.fencing_generation IS NOT (
        SELECT fencing_generation FROM omnivia_workspace_state WHERE singleton = 1);
    SELECT RAISE(ABORT, 'omnivia: a Project context starts at generation one')
    WHERE NEW.context_generation IS NOT 1;
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_project_contexts_update
BEFORE UPDATE ON omnivia_project_contexts
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded UPDATE on omnivia_project_contexts')
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
    SELECT RAISE(ABORT, 'omnivia: a Project context may only advance by one generation to another Project')
    WHERE NEW.workspace_id IS NOT OLD.workspace_id
       OR NEW.context_generation IS NOT OLD.context_generation + 1
       OR NEW.project_id IS OLD.project_id;
    SELECT RAISE(ABORT, 'omnivia: a Project context must bind the current fencing generation')
    WHERE NEW.fencing_generation IS NOT (
        SELECT fencing_generation FROM omnivia_workspace_state WHERE singleton = 1);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_project_contexts_delete
BEFORE DELETE ON omnivia_project_contexts
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_project_contexts is never deleted');
END;
