-- Durable progress of a recorded stop request, for the C05a `RuntimeStopProjection`.
--
-- Additive only. Three append-only tables and nine statement triggers, on top of the
-- stop ledger migration 0025 added. Migration 0025 stays byte-immutable and stays the
-- one authority on *whether* a run was asked to stop and *how that request settled*;
-- nothing here is a second stop authority. Every row below hangs off an existing
-- `omnivia_runtime_stop_requests` row by its `stop_request_id`, and no row here can
-- create a stop, change a stop outcome, close a run or delete any history.
--
--   omnivia_runtime_stop_progress          one observation of where one stop stands
--   omnivia_runtime_stop_obligations       the unresolved effects that observation found
--   omnivia_runtime_stop_cleanup_receipts  what cleanup for that stop achieved
--
-- Why an observation rather than a state column
-- ---------------------------------------------
--
-- The contract's `RuntimeStopPhase` is progress, not a disposition, and progress is
-- something a build *observed* at an instant rather than a value anyone may revise.
-- Recording it as a mutable column would mean a stop that once reported two pending
-- effects and later reported none had no durable evidence it was ever blocked -- which
-- is precisely the history `cancellation_pending_reconciliation` exists to be able to
-- show. So each look at a stop appends a numbered observation, contiguous from 1 and
-- non-regressing in time, and the highest-numbered one is what a read reports. Earlier
-- observations remain exactly as they were written.
--
-- Why the obligations hang off the observation and not off the request
-- --------------------------------------------------------------------
--
-- An obligation is "this effect was unresolved *when we looked*", and two looks may
-- legitimately disagree because reconciliation happened in between. Keying obligations
-- to `stop_progress_id` lets both answers stand: the set an observation carries is
-- immutable, a later observation carries its own set, and no row ever has to be
-- rewritten to say an effect is now resolved. Whether an obligation has *since* been
-- discharged is not stored here at all -- it is read from migrations 0023 and 0024, by
-- following the reconciliation links from the settlement this row names to the current
-- head of that effect's chain. Storing a resolved flag beside those tables would be a
-- second answer to a question the effect ledger already answers once.
--
-- An obligation may therefore only name a settlement whose outcome is `unknown`: a
-- `committed` or `not_committed` settlement is an answer, and an answered effect is not
-- something a stop is blocked on. The intent must also belong to the very run its stop
-- request names, so a stop cannot report itself blocked on another run's work.
--
-- The 256 bound is the contract's
-- ------------------------------
--
-- `RuntimeStopProjection.pending_effect_count` is capped at 256 by the accepted schema.
-- A workspace that could record a 257th obligation against one observation would hold a
-- durable fact no valid projection can state, so the 257th is refused at write time
-- rather than discovered at read time as a projection that cannot be published.
--
-- Cleanup receipts and the fourth outcome
-- ---------------------------------------
--
-- Migration 0019's `omnivia_runtime_cleanup_receipts` records per-*run* cleanup with the
-- three historical `CleanupOutcome` values. These receipts are per-*stop*: they answer
-- "what did cleanup for this stop request achieve", which is a different question about
-- a different subject, and 0019's table carries no `stop_request_id` to hang them from.
-- They admit a fourth value, `unknown`, because `RuntimeStopCleanupState` has
-- `uncertain` and a rolled-up `uncertain` has to come from somewhere: a resource whose
-- release could not be established is the honest third answer, and without a way to
-- write it down the roll-up would have to report `failed` or `completed` about a
-- resource nobody established anything about. `unknown` is 0023's word for the same
-- shape of not-knowing, used here for the same reason.
--
-- The roll-up itself -- which combination of receipts reads as `completed`, `partial`,
-- `failed`, `uncertain`, `requested` or `not_required` -- is a read and lives in the
-- repository, not in the schema. `cleanup_required` on the observation is what keeps
-- "nothing to free" distinguishable from "cleanup has been asked for and no receipt has
-- landed yet", which an empty receipt set alone cannot say.
--
-- UPDATE and DELETE abort unconditionally on all three tables, for the current fenced
-- owner too. Current ownership is authority to append a progress fact, never to revise
-- one.
--
-- Every comment in this file sits between statements and never inside one, for the
-- fingerprint-loader reason 0018 states.

CREATE TABLE IF NOT EXISTS omnivia_runtime_stop_progress (
    workspace_id      TEXT    NOT NULL,
    stop_progress_id  TEXT    NOT NULL,
    stop_request_id   TEXT    NOT NULL,
    progress_number   INTEGER NOT NULL,
    observed_at_us    INTEGER NOT NULL,
    cleanup_required  INTEGER NOT NULL,
    reason            TEXT    NOT NULL,
    audit_ref         TEXT    NOT NULL,

    PRIMARY KEY (workspace_id, stop_progress_id),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(stop_progress_id) = 'text'
           AND length(stop_progress_id) BETWEEN 1 AND 128
           AND stop_progress_id GLOB '[A-Za-z0-9]*'
           AND stop_progress_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(stop_progress_id, char(0)) = 0),
    CHECK (typeof(stop_request_id) = 'text' AND length(stop_request_id) BETWEEN 1 AND 128
           AND stop_request_id GLOB '[A-Za-z0-9]*'
           AND stop_request_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(stop_request_id, char(0)) = 0),
    CHECK (typeof(progress_number) = 'integer' AND progress_number > 0),
    CHECK (typeof(observed_at_us) = 'integer' AND observed_at_us > 0),
    CHECK (cleanup_required IN (0, 1)),
    CHECK (typeof(reason) = 'text' AND length(reason) BETWEEN 1 AND 128
           AND reason GLOB '[a-z]*'
           AND reason NOT GLOB '*[^a-z0-9_.]*'
           AND reason NOT GLOB '*.'
           AND reason NOT GLOB '*.[^a-z]*'),
    CHECK (typeof(audit_ref) = 'text' AND length(audit_ref) BETWEEN 1 AND 128
           AND audit_ref GLOB '[A-Za-z0-9]*'
           AND audit_ref NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(audit_ref, char(0)) = 0),

    UNIQUE (workspace_id, stop_request_id, progress_number),

    FOREIGN KEY (workspace_id, stop_request_id)
        REFERENCES omnivia_runtime_stop_requests (workspace_id, stop_request_id),
    FOREIGN KEY (audit_ref, workspace_id)
        REFERENCES omnivia_application_audit_events (audit_ref, workspace_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_runtime_stop_obligations (
    workspace_id          TEXT NOT NULL,
    stop_progress_id      TEXT NOT NULL,
    effect_intent_id      TEXT NOT NULL,
    effect_settlement_id  TEXT NOT NULL,

    PRIMARY KEY (workspace_id, stop_progress_id, effect_intent_id),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(stop_progress_id) = 'text'
           AND length(stop_progress_id) BETWEEN 1 AND 128
           AND stop_progress_id GLOB '[A-Za-z0-9]*'
           AND stop_progress_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(stop_progress_id, char(0)) = 0),
    CHECK (typeof(effect_intent_id) = 'text'
           AND length(effect_intent_id) BETWEEN 1 AND 128
           AND effect_intent_id GLOB '[A-Za-z0-9]*'
           AND effect_intent_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(effect_intent_id, char(0)) = 0),
    CHECK (typeof(effect_settlement_id) = 'text'
           AND length(effect_settlement_id) BETWEEN 1 AND 128
           AND effect_settlement_id GLOB '[A-Za-z0-9]*'
           AND effect_settlement_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(effect_settlement_id, char(0)) = 0),

    FOREIGN KEY (workspace_id, stop_progress_id)
        REFERENCES omnivia_runtime_stop_progress (workspace_id, stop_progress_id),
    FOREIGN KEY (workspace_id, effect_intent_id)
        REFERENCES omnivia_runtime_effect_intents (workspace_id, effect_intent_id),
    FOREIGN KEY (workspace_id, effect_settlement_id)
        REFERENCES omnivia_runtime_effect_settlements (workspace_id, effect_settlement_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_runtime_stop_cleanup_receipts (
    workspace_id             TEXT    NOT NULL,
    stop_cleanup_receipt_id  TEXT    NOT NULL,
    stop_request_id          TEXT    NOT NULL,
    resource_kind            TEXT    NOT NULL,
    outcome                  TEXT    NOT NULL,
    performed_at_us          INTEGER NOT NULL,
    reason                   TEXT    NOT NULL,
    audit_ref                TEXT    NOT NULL,

    PRIMARY KEY (workspace_id, stop_cleanup_receipt_id),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(stop_cleanup_receipt_id) = 'text'
           AND length(stop_cleanup_receipt_id) BETWEEN 1 AND 128
           AND stop_cleanup_receipt_id GLOB '[A-Za-z0-9]*'
           AND stop_cleanup_receipt_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(stop_cleanup_receipt_id, char(0)) = 0),
    CHECK (typeof(stop_request_id) = 'text' AND length(stop_request_id) BETWEEN 1 AND 128
           AND stop_request_id GLOB '[A-Za-z0-9]*'
           AND stop_request_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(stop_request_id, char(0)) = 0),
    CHECK (typeof(resource_kind) = 'text' AND length(resource_kind) BETWEEN 1 AND 128
           AND resource_kind GLOB '[a-z]*' AND resource_kind NOT GLOB '*[^a-z0-9_.]*'
           AND resource_kind NOT GLOB '*.' AND resource_kind NOT GLOB '*.[^a-z]*'),
    CHECK (outcome IN ('released', 'not_required', 'failed', 'unknown')),
    CHECK (typeof(performed_at_us) = 'integer' AND performed_at_us > 0),
    CHECK (typeof(reason) = 'text' AND length(reason) BETWEEN 1 AND 128
           AND reason GLOB '[a-z]*'
           AND reason NOT GLOB '*[^a-z0-9_.]*'
           AND reason NOT GLOB '*.'
           AND reason NOT GLOB '*.[^a-z]*'),
    CHECK (typeof(audit_ref) = 'text' AND length(audit_ref) BETWEEN 1 AND 128
           AND audit_ref GLOB '[A-Za-z0-9]*'
           AND audit_ref NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(audit_ref, char(0)) = 0),

    FOREIGN KEY (workspace_id, stop_request_id)
        REFERENCES omnivia_runtime_stop_requests (workspace_id, stop_request_id),
    FOREIGN KEY (audit_ref, workspace_id)
        REFERENCES omnivia_application_audit_events (audit_ref, workspace_id)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS omnivia_idx_runtime_stop_cleanup_receipts_request
    ON omnivia_runtime_stop_cleanup_receipts (
        workspace_id, stop_request_id, performed_at_us, stop_cleanup_receipt_id
    );

CREATE TRIGGER IF NOT EXISTS omnivia_guard_runtime_stop_progress_insert
BEFORE INSERT ON omnivia_runtime_stop_progress
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_runtime_stop_progress')
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
    SELECT RAISE(ABORT, 'omnivia: stop progress number must be contiguous')
    WHERE NEW.progress_number IS NOT (
        SELECT COALESCE(MAX(progress_number), 0) + 1
        FROM omnivia_runtime_stop_progress
        WHERE workspace_id = NEW.workspace_id
          AND stop_request_id = NEW.stop_request_id);
    SELECT RAISE(ABORT, 'omnivia: stop progress cannot predate the stop it observes')
    WHERE NEW.observed_at_us < (
        SELECT requested_at_us FROM omnivia_runtime_stop_requests
        WHERE workspace_id = NEW.workspace_id
          AND stop_request_id = NEW.stop_request_id);
    SELECT RAISE(ABORT, 'omnivia: stop progress time must not regress')
    WHERE NEW.progress_number > 1
      AND NEW.observed_at_us < (
        SELECT observed_at_us FROM omnivia_runtime_stop_progress
        WHERE workspace_id = NEW.workspace_id
          AND stop_request_id = NEW.stop_request_id
          AND progress_number = NEW.progress_number - 1);
    SELECT RAISE(ABORT, 'omnivia: stop progress audit reference must belong to its workspace')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_application_audit_events
        WHERE audit_ref = NEW.audit_ref AND workspace_id = NEW.workspace_id);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_runtime_stop_progress_update
BEFORE UPDATE ON omnivia_runtime_stop_progress
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_runtime_stop_progress is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_runtime_stop_progress_delete
BEFORE DELETE ON omnivia_runtime_stop_progress
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_runtime_stop_progress is append-only; DELETE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_runtime_stop_obligations_insert
BEFORE INSERT ON omnivia_runtime_stop_obligations
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_runtime_stop_obligations')
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
    SELECT RAISE(ABORT, 'omnivia: a stop obligation must name an unresolved settlement of its own intent')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_runtime_effect_settlements
        WHERE workspace_id = NEW.workspace_id
          AND effect_settlement_id = NEW.effect_settlement_id
          AND effect_intent_id = NEW.effect_intent_id
          AND outcome = 'unknown');
    SELECT RAISE(ABORT, 'omnivia: a stop obligation must name an effect of the run its stop named')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_runtime_stop_progress p
        JOIN omnivia_runtime_stop_requests r
          ON r.workspace_id = p.workspace_id AND r.stop_request_id = p.stop_request_id
        JOIN omnivia_runtime_effect_intents i
          ON i.workspace_id = p.workspace_id AND i.run_id = r.run_id
        WHERE p.workspace_id = NEW.workspace_id
          AND p.stop_progress_id = NEW.stop_progress_id
          AND i.effect_intent_id = NEW.effect_intent_id);
    SELECT RAISE(ABORT, 'omnivia: a stop observation may not carry more than 256 obligations')
    WHERE 256 <= (
        SELECT COUNT(*) FROM omnivia_runtime_stop_obligations
        WHERE workspace_id = NEW.workspace_id
          AND stop_progress_id = NEW.stop_progress_id);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_runtime_stop_obligations_update
BEFORE UPDATE ON omnivia_runtime_stop_obligations
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_runtime_stop_obligations is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_runtime_stop_obligations_delete
BEFORE DELETE ON omnivia_runtime_stop_obligations
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_runtime_stop_obligations is append-only; DELETE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_runtime_stop_cleanup_receipts_insert
BEFORE INSERT ON omnivia_runtime_stop_cleanup_receipts
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_runtime_stop_cleanup_receipts')
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
    SELECT RAISE(ABORT, 'omnivia: a stop cleanup receipt cannot predate the stop it accounts for')
    WHERE NEW.performed_at_us < (
        SELECT requested_at_us FROM omnivia_runtime_stop_requests
        WHERE workspace_id = NEW.workspace_id
          AND stop_request_id = NEW.stop_request_id);
    SELECT RAISE(ABORT, 'omnivia: a stop cleanup receipt audit reference must belong to its workspace')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_application_audit_events
        WHERE audit_ref = NEW.audit_ref AND workspace_id = NEW.workspace_id);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_runtime_stop_cleanup_receipts_update
BEFORE UPDATE ON omnivia_runtime_stop_cleanup_receipts
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_runtime_stop_cleanup_receipts is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_runtime_stop_cleanup_receipts_delete
BEFORE DELETE ON omnivia_runtime_stop_cleanup_receipts
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_runtime_stop_cleanup_receipts is append-only; DELETE is never permitted');
END;
