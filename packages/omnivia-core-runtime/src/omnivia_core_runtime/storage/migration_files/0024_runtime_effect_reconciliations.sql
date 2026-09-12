-- Canonical runtime effect reconciliation records (RT-203 successor).
--
-- Additive only. One append-only table and three statement triggers, on top of the
-- effect intent, receipt and settlement records migration 0023 added. A reconciliation
-- record does not rewrite the unknown settlement that made reconciliation necessary.
-- It records the audit bridge from that source settlement to a later appended settlement
-- whose outcome reflects what reconciliation established.
--
-- Reconciliation outcomes use the existing runtime execution-plane vocabulary:
-- `APPLIED`, `NOT_APPLIED`, `PARTIAL` and `UNKNOWN`. The bridge to public
-- `EffectSettlement` is structural:
--
--   APPLIED      -> resulting settlement is `committed` and names the proving receipt
--   NOT_APPLIED  -> resulting settlement is `not_committed` and names no receipt
--   PARTIAL      -> resulting settlement is `unknown` and names no receipt
--   UNKNOWN      -> resulting settlement is `unknown` and names no receipt
--
-- The source settlement must itself be `unknown`; reconciling a committed or
-- not-committed answer would be a second answer to a question already closed. A later
-- reconciliation may be attempted against any still-unknown resulting settlement, so this
-- table keeps history rather than enforcing one attempt forever.
--
-- UPDATE and DELETE abort unconditionally, for the current fenced owner too. Current
-- ownership is authority to append a reconciliation fact, never to revise one.

CREATE TABLE IF NOT EXISTS omnivia_runtime_effect_reconciliations (
    workspace_id                    TEXT    NOT NULL,
    effect_reconciliation_id        TEXT    NOT NULL,
    run_id                          TEXT    NOT NULL,
    effect_intent_id                TEXT    NOT NULL,
    source_effect_settlement_id     TEXT    NOT NULL,
    outcome                         TEXT    NOT NULL,
    effect_receipt_id               TEXT,
    resulting_effect_settlement_id  TEXT    NOT NULL,
    reconciled_at_us                INTEGER NOT NULL,
    reconciled_by                   TEXT    NOT NULL,
    audit_ref                       TEXT    NOT NULL,

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
    CHECK (typeof(source_effect_settlement_id) = 'text'
           AND length(source_effect_settlement_id) BETWEEN 1 AND 128
           AND source_effect_settlement_id GLOB '[A-Za-z0-9]*'
           AND source_effect_settlement_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(source_effect_settlement_id, char(0)) = 0),
    CHECK (outcome IN ('APPLIED', 'NOT_APPLIED', 'PARTIAL', 'UNKNOWN')),
    CHECK (effect_receipt_id IS NULL OR (typeof(effect_receipt_id) = 'text'
           AND length(effect_receipt_id) BETWEEN 1 AND 128
           AND effect_receipt_id GLOB '[A-Za-z0-9]*'
           AND effect_receipt_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(effect_receipt_id, char(0)) = 0)),
    CHECK ((outcome = 'APPLIED' AND effect_receipt_id IS NOT NULL)
           OR (outcome <> 'APPLIED' AND effect_receipt_id IS NULL)),
    CHECK (typeof(resulting_effect_settlement_id) = 'text'
           AND length(resulting_effect_settlement_id) BETWEEN 1 AND 128
           AND resulting_effect_settlement_id GLOB '[A-Za-z0-9]*'
           AND resulting_effect_settlement_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(resulting_effect_settlement_id, char(0)) = 0),
    CHECK (typeof(reconciled_at_us) = 'integer' AND reconciled_at_us > 0),
    CHECK (typeof(reconciled_by) = 'text' AND length(reconciled_by) BETWEEN 1 AND 128
           AND reconciled_by GLOB '[A-Za-z0-9]*'
           AND reconciled_by NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(reconciled_by, char(0)) = 0),
    CHECK (typeof(audit_ref) = 'text' AND length(audit_ref) BETWEEN 1 AND 128
           AND audit_ref GLOB '[A-Za-z0-9]*'
           AND audit_ref NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(audit_ref, char(0)) = 0),
    CHECK (source_effect_settlement_id <> resulting_effect_settlement_id),

    UNIQUE (workspace_id, resulting_effect_settlement_id),

    FOREIGN KEY (workspace_id, run_id)
        REFERENCES omnivia_runtime_runs (workspace_id, run_id),
    FOREIGN KEY (workspace_id, effect_intent_id)
        REFERENCES omnivia_runtime_effect_intents (workspace_id, effect_intent_id),
    FOREIGN KEY (workspace_id, source_effect_settlement_id)
        REFERENCES omnivia_runtime_effect_settlements (workspace_id, effect_settlement_id),
    FOREIGN KEY (workspace_id, effect_receipt_id)
        REFERENCES omnivia_runtime_effect_receipts (workspace_id, effect_receipt_id),
    FOREIGN KEY (workspace_id, resulting_effect_settlement_id)
        REFERENCES omnivia_runtime_effect_settlements (workspace_id, effect_settlement_id),
    FOREIGN KEY (audit_ref, workspace_id)
        REFERENCES omnivia_application_audit_events (audit_ref, workspace_id)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS omnivia_idx_runtime_effect_reconciliations_source
    ON omnivia_runtime_effect_reconciliations (
        workspace_id, source_effect_settlement_id, reconciled_at_us
    );

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
    SELECT RAISE(ABORT, 'omnivia: an effect reconciliation source must be an unknown settlement of its own intent')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_runtime_effect_settlements
        WHERE workspace_id = NEW.workspace_id
          AND effect_settlement_id = NEW.source_effect_settlement_id
          AND run_id = NEW.run_id
          AND effect_intent_id = NEW.effect_intent_id
          AND outcome = 'unknown');
    SELECT RAISE(ABORT, 'omnivia: an effect reconciliation cannot predate its source settlement')
    WHERE NEW.reconciled_at_us < (
        SELECT settled_at_us FROM omnivia_runtime_effect_settlements
        WHERE workspace_id = NEW.workspace_id
          AND effect_settlement_id = NEW.source_effect_settlement_id);
    SELECT RAISE(ABORT, 'omnivia: an APPLIED reconciliation must name a receipt for its own intent')
    WHERE NEW.outcome = 'APPLIED'
      AND NOT EXISTS (
        SELECT 1 FROM omnivia_runtime_effect_receipts
        WHERE workspace_id = NEW.workspace_id
          AND effect_receipt_id = NEW.effect_receipt_id
          AND run_id = NEW.run_id
          AND effect_intent_id = NEW.effect_intent_id);
    SELECT RAISE(ABORT, 'omnivia: an effect reconciliation result must be a later settlement of its own intent')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_runtime_effect_settlements
        WHERE workspace_id = NEW.workspace_id
          AND effect_settlement_id = NEW.resulting_effect_settlement_id
          AND run_id = NEW.run_id
          AND effect_intent_id = NEW.effect_intent_id
          AND settled_at_us >= NEW.reconciled_at_us);
    SELECT RAISE(ABORT, 'omnivia: an effect reconciliation outcome must match its resulting settlement')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_runtime_effect_settlements s
        WHERE s.workspace_id = NEW.workspace_id
          AND s.effect_settlement_id = NEW.resulting_effect_settlement_id
          AND (
            (NEW.outcome = 'APPLIED'
             AND s.outcome = 'committed'
             AND s.effect_receipt_id = NEW.effect_receipt_id)
            OR (NEW.outcome = 'NOT_APPLIED'
                AND s.outcome = 'not_committed'
                AND s.effect_receipt_id IS NULL)
            OR (NEW.outcome IN ('PARTIAL', 'UNKNOWN')
                AND s.outcome = 'unknown'
                AND s.effect_receipt_id IS NULL)
          ));
    SELECT RAISE(ABORT, 'omnivia: an effect reconciliation audit reference must belong to its workspace')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_application_audit_events
        WHERE audit_ref = NEW.audit_ref AND workspace_id = NEW.workspace_id);
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
