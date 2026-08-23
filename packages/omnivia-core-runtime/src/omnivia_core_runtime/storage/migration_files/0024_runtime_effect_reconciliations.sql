-- The reconciliation of an uncertain effect: one late, explicit, final answer (RT-206).
--
-- Additive only. One append-only table and three statement triggers, on top of the effect
-- family migration 0023 added. Nothing here creates a run, changes a run's status,
-- declares an intent, records a dispatch, retains a receipt, or writes a settlement.
--
--   omnivia_runtime_effect_reconciliations   the one final answer an `unknown` gets
--
-- *Why an additive relation rather than a second settlement.* 0023 keys a settlement on
-- its intent -- `UNIQUE (workspace_id, effect_intent_id)` -- and aborts UPDATE and DELETE
-- unconditionally. That is deliberate and stays: a settlement is what the runtime
-- concluded at the moment it concluded it, and rewriting it would erase the fact that the
-- effect was ever uncertain. But it also means an `unknown` settlement has nowhere to
-- record the answer that later arrives, which is the limitation this migration resolves.
-- A reconciliation is a *different fact about the same intent*: the `unknown` row stays
-- exactly as written, this row states what was finally established, and it names the
-- settlement it reconciles so the two are one auditable chain rather than two loose
-- opinions. The settlement history is immutable and the final outcome is durable, without
-- either being bought with the other.
--
-- *Only an uncertain effect is reconciled.* The named settlement must be `unknown`. A
-- `committed` or `not_committed` settlement is already final and reconciling it would be
-- overturning a concluded answer, not resolving an open one; and an intent with no
-- settlement at all is unsettled rather than uncertain, and what it gets is a settlement.
-- One reconciliation per intent, keyed on the intent, so a second final answer has nowhere
-- to live under any spelling.
--
-- *`unknown` is not one of the outcomes.* Reconciling an effect to uncertainty is not a
-- reconciliation, and recording one would let a caller close the question by restating it.
-- The two outcomes here are the two that can actually be established.
--
-- *A committed reconciliation requires the retained receipt that proves it.* It names one,
-- that receipt must be evidence for this same intent, the foreign key requires it to
-- exist, and it must have been observed at or before the instant reconciliation was made.
-- This is what makes the late-receipt path a *path* and not a licence: an `unknown` effect
-- becomes `committed` because an observation of it was retained, never because anybody
-- asserted it landed.
--
-- *A not_committed reconciliation is refused over any evidence that it happened.* No
-- receipt for the intent, and no dispatch record for it either. A receipt is proof it
-- landed and a dispatch record is proof it was handed out, and declaring that an effect
-- never happened while this database holds either is a contradiction. This schema fails
-- closed on one rather than choosing which half to believe -- the same rule 0023 states
-- for a `not_committed` settlement, extended to the outbox because an effect that reached
-- the world is exactly the effect nobody may declare away.
--
-- *Nothing here retries anything.* There is no attempt column, no redispatch, no trigger
-- that produces work. An uncertain effect whose evidence has not arrived stays uncertain:
-- the outcome is established from what is retained or it is not established at all, and a
-- blind retry of a logically identical effect is precisely what `unknown` exists to
-- prevent.
--
-- What is deliberately absent. No adapter, binding, endpoint, credential or transport
-- column, for 0023's reason. No response or request bytes. No reconciliation of anything
-- but an effect. And no DML, because no fact already in this database is honestly
-- classifiable as a reconciliation anybody performed.
--
-- UPDATE and DELETE abort unconditionally, for the current fenced owner too.
--
-- Identity, types, bounds, timestamps and digests follow 0018's, 0022's and 0023's rules
-- exactly. Every comment in this file sits between statements and never inside one, for
-- the fingerprint-loader reason 0018 states.

CREATE TABLE IF NOT EXISTS omnivia_runtime_effect_reconciliations (
    workspace_id             TEXT    NOT NULL,
    effect_reconciliation_id TEXT    NOT NULL,
    run_id                   TEXT    NOT NULL,
    effect_intent_id         TEXT    NOT NULL,
    effect_settlement_id     TEXT    NOT NULL,
    outcome                  TEXT    NOT NULL,
    reconciled_at_us         INTEGER NOT NULL,
    reason                   TEXT    NOT NULL,
    audit_ref                TEXT    NOT NULL,
    effect_receipt_id        TEXT,

    PRIMARY KEY (workspace_id, effect_reconciliation_id),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(effect_reconciliation_id) = 'text'
           AND length(effect_reconciliation_id) BETWEEN 1 AND 128
           AND effect_reconciliation_id GLOB '[A-Za-z0-9]*'
           AND effect_reconciliation_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(effect_reconciliation_id, char(0)) = 0),
    CHECK (typeof(run_id) = 'text' AND length(run_id) BETWEEN 1 AND 128
           AND run_id GLOB '[A-Za-z0-9]*'
           AND run_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(run_id, char(0)) = 0),
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
    CHECK (outcome IN ('committed', 'not_committed')),
    CHECK (typeof(reconciled_at_us) = 'integer' AND reconciled_at_us > 0),
    CHECK (typeof(reason) = 'text' AND length(reason) BETWEEN 1 AND 128
           AND reason GLOB '[a-z]*' AND reason NOT GLOB '*[^a-z0-9_.]*'
           AND reason NOT GLOB '*.' AND reason NOT GLOB '*.[^a-z]*'),
    CHECK (typeof(audit_ref) = 'text' AND length(audit_ref) BETWEEN 1 AND 128
           AND audit_ref GLOB '[A-Za-z0-9]*'
           AND audit_ref NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(audit_ref, char(0)) = 0),
    CHECK (effect_receipt_id IS NULL OR (typeof(effect_receipt_id) = 'text'
           AND length(effect_receipt_id) BETWEEN 1 AND 128
           AND effect_receipt_id GLOB '[A-Za-z0-9]*'
           AND effect_receipt_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(effect_receipt_id, char(0)) = 0)),
    CHECK ((outcome = 'committed' AND effect_receipt_id IS NOT NULL)
           OR (outcome <> 'committed' AND effect_receipt_id IS NULL)),

    UNIQUE (workspace_id, effect_intent_id),

    FOREIGN KEY (workspace_id, run_id)
        REFERENCES omnivia_runtime_runs (workspace_id, run_id),
    FOREIGN KEY (workspace_id, effect_intent_id)
        REFERENCES omnivia_runtime_effect_intents (workspace_id, effect_intent_id),
    FOREIGN KEY (workspace_id, effect_settlement_id)
        REFERENCES omnivia_runtime_effect_settlements
            (workspace_id, effect_settlement_id),
    FOREIGN KEY (workspace_id, effect_receipt_id)
        REFERENCES omnivia_runtime_effect_receipts (workspace_id, effect_receipt_id),
    FOREIGN KEY (audit_ref, workspace_id)
        REFERENCES omnivia_application_audit_events (audit_ref, workspace_id)
) WITHOUT ROWID;

-- The INSERT guard repeats the complete connection-authority, mutation-guard,
-- workspace-state and lease predicate 0005 established, then adds the correlation,
-- ordering and evidence rules a reconciliation has. UPDATE and DELETE abort
-- unconditionally -- for the current fenced owner too.

CREATE TRIGGER IF NOT EXISTS omnivia_guard_runtime_effect_reconciliations_insert
BEFORE INSERT ON omnivia_runtime_effect_reconciliations
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_runtime_effect_reconciliations')
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
    SELECT RAISE(ABORT, 'omnivia: a reconciliation must name an intent of its own run')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_runtime_effect_intents
        WHERE workspace_id = NEW.workspace_id
          AND effect_intent_id = NEW.effect_intent_id AND run_id = NEW.run_id);
    SELECT RAISE(ABORT, 'omnivia: a reconciliation must name the unknown settlement of its own intent')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_runtime_effect_settlements
        WHERE workspace_id = NEW.workspace_id
          AND effect_settlement_id = NEW.effect_settlement_id
          AND effect_intent_id = NEW.effect_intent_id
          AND outcome = 'unknown');
    SELECT RAISE(ABORT, 'omnivia: an effect is never reconciled before it was settled')
    WHERE NEW.reconciled_at_us < (
        SELECT settled_at_us FROM omnivia_runtime_effect_settlements
        WHERE workspace_id = NEW.workspace_id
          AND effect_settlement_id = NEW.effect_settlement_id);
    SELECT RAISE(ABORT, 'omnivia: a committed reconciliation must name a receipt for its own intent')
    WHERE NEW.effect_receipt_id IS NOT NULL
      AND NOT EXISTS (
        SELECT 1 FROM omnivia_runtime_effect_receipts
        WHERE workspace_id = NEW.workspace_id
          AND effect_receipt_id = NEW.effect_receipt_id
          AND effect_intent_id = NEW.effect_intent_id
          AND observed_at_us <= NEW.reconciled_at_us);
    SELECT RAISE(ABORT, 'omnivia: an observed effect cannot be reconciled not_committed')
    WHERE NEW.outcome = 'not_committed'
      AND EXISTS (
        SELECT 1 FROM omnivia_runtime_effect_receipts
        WHERE workspace_id = NEW.workspace_id
          AND effect_intent_id = NEW.effect_intent_id);
    SELECT RAISE(ABORT, 'omnivia: a dispatched effect cannot be reconciled not_committed')
    WHERE NEW.outcome = 'not_committed'
      AND EXISTS (
        SELECT 1 FROM omnivia_runtime_effect_dispatches
        WHERE workspace_id = NEW.workspace_id
          AND effect_intent_id = NEW.effect_intent_id);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_runtime_effect_reconciliations_update
BEFORE UPDATE ON omnivia_runtime_effect_reconciliations
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_runtime_effect_reconciliations is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_runtime_effect_reconciliations_delete
BEFORE DELETE ON omnivia_runtime_effect_reconciliations
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_runtime_effect_reconciliations is append-only; DELETE is never permitted');
END;
