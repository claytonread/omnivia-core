-- The durable content of the `evidence.fts5` search projection (V06-3 Lane B).
--
-- 0011 added the lifecycle -- runs, checkpoints, validations and the activation
-- statement whose AFTER trigger advances the ledger pointer -- and deliberately
-- built no projection.  This migration adds the one thing that was missing: the
-- table a build actually writes into, bound to the run that produced it.
--
-- **Why this is an ordinary table and not `CREATE VIRTUAL TABLE ... USING fts5`.**
-- SQLite refuses `CREATE TRIGGER` on a virtual table ("cannot create triggers on
-- virtual tables"), and an FTS5 table also materialises five shadow tables whose
-- internal INSERTs and DELETEs a guard trigger would abort mid-merge.  This
-- repository's guard coverage is not a convention that could bend for one table:
-- `fencing.guarded_tables()` derives itself from the canonical schema, so every
-- persisted table is guarded unless it is named substrate, and
-- `test_fencing_mutation.py::test_sb05_every_mutable_table_is_guarded_for_every_statement`
-- fails closed on any table missing one of its three statement triggers.  A
-- persisted FTS5 virtual table would therefore be six unguarded tables in the
-- authoritative database.
--
-- So the *projection* is this guarded table, and the FTS5 index over it is a
-- session-scoped `temp` virtual table the startup/maintenance path materialises
-- from the activated run (`storage/projections/fts.py`).  The index is derived and
-- reproducible from the rows below; the rows below are the evidence.
--
-- Rows are bound to a run, never to "the projection", which is what makes
-- activation atomic without a second write: a build appends under its own
-- `run_id` while the previous run's rows still serve reads, and the activation
-- INSERT flips the ledger pointer that decides which `run_id` a reader may see.
-- Nothing here updates the active pointer, and nothing here may.
--
-- Every comment in this file sits *between* statements and never inside one.  The
-- migrator applies these through `split_sql_statements`, which strips comments, while
-- `canonical_schema_fingerprint()` replays the same text with `executescript`, which
-- does not.  A comment inside a `CREATE` body is therefore stored in `sqlite_master`
-- by one path and not by the other, and the live workspace stops matching the
-- canonical schema it is supposed to be identical to.
--
-- An empty `search_text` is permitted below and is not a defect: it is the artifact's
-- own identity surface, and an artifact whose locator is NULL still has to be a member
-- of the projection.  Dropping it instead would make the projection's row count
-- disagree with the workspace's, which is exactly the discrepancy validation exists to
-- catch.

CREATE TABLE IF NOT EXISTS omnivia_evidence_search_documents (
    workspace_id  TEXT NOT NULL,
    projection_id TEXT NOT NULL,
    run_id        TEXT NOT NULL,
    evidence_id   TEXT NOT NULL,
    search_text   TEXT NOT NULL,

    PRIMARY KEY (workspace_id, projection_id, run_id, evidence_id),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(projection_id) = 'text' AND length(projection_id) BETWEEN 1 AND 128
           AND projection_id GLOB '[A-Za-z0-9]*'
           AND projection_id NOT GLOB '*[^A-Za-z0-9._:/-]*'
           AND instr(projection_id, char(0)) = 0),
    CHECK (typeof(run_id) = 'text' AND length(run_id) BETWEEN 1 AND 128
           AND run_id GLOB '[A-Za-z0-9]*'
           AND run_id NOT GLOB '*[^A-Za-z0-9._:/-]*'
           AND instr(run_id, char(0)) = 0),
    CHECK (typeof(evidence_id) = 'text' AND length(evidence_id) BETWEEN 1 AND 128
           AND evidence_id GLOB '[A-Za-z0-9]*'
           AND evidence_id NOT GLOB '*[^A-Za-z0-9._:/-]*'
           AND instr(evidence_id, char(0)) = 0),
    CHECK (typeof(search_text) = 'text' AND length(search_text) <= 4096
           AND instr(search_text, char(0)) = 0),

    FOREIGN KEY (workspace_id, projection_id, run_id)
        REFERENCES omnivia_projection_runs (workspace_id, projection_id, run_id)
) WITHOUT ROWID;

-- The reader's only query: every document of one run, in `evidence_id` order.
-- The primary key already serves it, so no second index is declared.
--
-- The INSERT guard below carries two conditions past the usual fencing check.
--
-- Only a *running* run may append, and that single condition is the whole of the
-- closure rule.  0011's checkpoint and failure guards need a second clause refusing
-- writes once a validation row exists, because those tables accept appends in
-- `running` *and* `validating`.  Content does not: a run leaves `running` before
-- validation starts and never returns to it, so requiring `running` already refuses
-- every row that would arrive after the build digest was computed over the content.
-- A validation-exists clause here would be unreachable, and an unreachable guard
-- reads like protection while proving nothing.
--
-- And a projection may narrow what it carries; it may never invent a member.  The
-- authorized frontier is built from 0008's own rows, so a document naming an artifact
-- that does not exist could only ever widen a result.

CREATE TRIGGER IF NOT EXISTS omnivia_guard_evidence_search_documents_insert
BEFORE INSERT ON omnivia_evidence_search_documents
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_evidence_search_documents')
    WHERE omnivia_service_writer() IS NOT 1
       OR NEW.workspace_id IS NOT (
            SELECT workspace_id FROM omnivia_workspace_state WHERE singleton = 1)
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
    SELECT RAISE(ABORT, 'omnivia: projection document identity already exists')
    WHERE EXISTS (
        SELECT 1 FROM omnivia_evidence_search_documents
        WHERE workspace_id = NEW.workspace_id AND projection_id = NEW.projection_id
          AND run_id = NEW.run_id AND evidence_id = NEW.evidence_id);
    SELECT RAISE(ABORT, 'omnivia: projection document requires a running projection run')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_projection_runs
        WHERE workspace_id = NEW.workspace_id AND projection_id = NEW.projection_id
          AND run_id = NEW.run_id AND state = 'running');
    SELECT RAISE(ABORT, 'omnivia: projection document requires a known evidence artifact')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_evidence_artifacts
        WHERE workspace_id = NEW.workspace_id AND evidence_id = NEW.evidence_id);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_evidence_search_documents_update
BEFORE UPDATE ON omnivia_evidence_search_documents
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_evidence_search_documents is append-only; UPDATE is never permitted');
END;

-- DELETE is permitted, and only here.  Projection content is derived and has to
-- be reclaimable: a failed run's partial rows and a superseded run's whole
-- content are garbage the next build must be able to drop.  What it may never
-- drop is the content the ledger currently points at, because that is what every
-- read is being served from.
CREATE TRIGGER IF NOT EXISTS omnivia_guard_evidence_search_documents_delete
BEFORE DELETE ON omnivia_evidence_search_documents
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded DELETE on omnivia_evidence_search_documents')
    WHERE omnivia_service_writer() IS NOT 1
       OR OLD.workspace_id IS NOT (
            SELECT workspace_id FROM omnivia_workspace_state WHERE singleton = 1)
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
    SELECT RAISE(ABORT, 'omnivia: the active projection build may not be deleted')
    WHERE EXISTS (
        SELECT 1 FROM omnivia_projection_ledger
        WHERE projection_id = OLD.projection_id AND active_run_id = OLD.run_id);
END;
