-- Canonical EffectIntent, EffectReceipt and EffectSettlement records (RT-203 successor).
--
-- Additive only. Three append-only tables and nine statement triggers, on top of the
-- canonical runtime records migrations 0018, 0021 and 0022 added. Every row here belongs
-- to an existing `omnivia_runtime_runs` row; nothing here creates a run, changes a run's
-- status, dispatches an effect, retries an uncertain attempt, or reconciles provider
-- state outside the canonical runtime record.
--
--   omnivia_runtime_effect_intents      one immutable declaration before acting
--   omnivia_runtime_effect_receipts     one immutable observation of an intended effect
--   omnivia_runtime_effect_settlements  an audited answer for an intended effect
--
-- The stored shape follows the accepted v1 contract without inventing a provider store.
-- Intent columns are the canonical selectors a runtime command needs before acting:
-- run, step, attempt, capability, grant, idempotency key, request digest and declaration
-- instant. A receipt is subordinate evidence for an intent; its optional external
-- reference is stored as canonical JSON bytes with the digest and byte length beside it.
-- A settlement is an audited answer for one intent: `committed` names the receipt
-- proving it, while `not_committed` and `unknown` name no receipt. `unknown` is not a
-- failure and grants no retry authority; it records that reconciliation is still owed,
-- and later reconciliation may append a later answer rather than rewriting this one.
--
-- The SQL states the rules it can know from existing rows: the run must be running before
-- a new intent is declared; the step, attempt and grant must all belong to that same run;
-- the attempt must still be open; an idempotency key cannot be rebound to different
-- request bytes within the run; receipts and settlements cannot predate the intent; and
-- committed settlements must point at a receipt for the same intent. Whether the grant's
-- stored JSON actually grants `capability_id`, and whether an external reference is
-- semantically valid, remains the accepted contract validator's job because 0022 stores
-- grants as canonical JSON rather than decomposed capability columns.
--
-- UPDATE and DELETE abort unconditionally on all three tables, for the current fenced
-- owner too. Current ownership is authority to append a new fact, never to revise one.
--
-- Every comment in this file sits between statements and never inside one, for the
-- fingerprint-loader reason 0018 states.

CREATE TABLE IF NOT EXISTS omnivia_runtime_effect_intents (
    workspace_id        TEXT    NOT NULL,
    effect_intent_id    TEXT    NOT NULL,
    run_id              TEXT    NOT NULL,
    run_step_id         TEXT    NOT NULL,
    attempt_id          TEXT    NOT NULL,
    capability_id       TEXT    NOT NULL,
    capability_grant_id TEXT    NOT NULL,
    effect_kind         TEXT    NOT NULL,
    idempotency_key     TEXT    NOT NULL,
    request_digest      TEXT    NOT NULL,
    declared_at_us      INTEGER NOT NULL,

    PRIMARY KEY (workspace_id, effect_intent_id),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(effect_intent_id) = 'text'
           AND length(effect_intent_id) BETWEEN 1 AND 128
           AND effect_intent_id GLOB '[A-Za-z0-9]*'
           AND effect_intent_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(effect_intent_id, char(0)) = 0),
    CHECK (typeof(run_id) = 'text' AND length(run_id) BETWEEN 1 AND 128
           AND run_id GLOB '[A-Za-z0-9]*'
           AND run_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(run_id, char(0)) = 0),
    CHECK (typeof(run_step_id) = 'text' AND length(run_step_id) BETWEEN 1 AND 128
           AND run_step_id GLOB '[A-Za-z0-9]*'
           AND run_step_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(run_step_id, char(0)) = 0),
    CHECK (typeof(attempt_id) = 'text' AND length(attempt_id) BETWEEN 1 AND 128
           AND attempt_id GLOB '[A-Za-z0-9]*'
           AND attempt_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(attempt_id, char(0)) = 0),
    CHECK (typeof(capability_id) = 'text'
           AND length(capability_id) BETWEEN 1 AND 128
           AND capability_id GLOB '[A-Za-z0-9]*'
           AND capability_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(capability_id, char(0)) = 0),
    CHECK (typeof(capability_grant_id) = 'text'
           AND length(capability_grant_id) BETWEEN 1 AND 128
           AND capability_grant_id GLOB '[A-Za-z0-9]*'
           AND capability_grant_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(capability_grant_id, char(0)) = 0),
    CHECK (typeof(effect_kind) = 'text' AND length(effect_kind) BETWEEN 1 AND 128
           AND effect_kind GLOB '[a-z]*'
           AND effect_kind NOT GLOB '*[^a-z0-9_.]*'
           AND effect_kind NOT GLOB '*.'
           AND effect_kind NOT GLOB '*.[^a-z]*'),
    CHECK (typeof(idempotency_key) = 'text'
           AND length(idempotency_key) BETWEEN 1 AND 128
           AND idempotency_key GLOB '[A-Za-z0-9]*'
           AND idempotency_key NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(idempotency_key, char(0)) = 0),
    CHECK (typeof(request_digest) = 'text' AND length(request_digest) = 71
           AND substr(request_digest, 1, 7) = 'sha256:'
           AND substr(request_digest, 8) NOT GLOB '*[^0-9a-f]*'),
    CHECK (typeof(declared_at_us) = 'integer' AND declared_at_us > 0),

    UNIQUE (workspace_id, run_id, idempotency_key),

    FOREIGN KEY (workspace_id, run_id)
        REFERENCES omnivia_runtime_runs (workspace_id, run_id),
    FOREIGN KEY (workspace_id, run_step_id)
        REFERENCES omnivia_runtime_run_steps (workspace_id, run_step_id),
    FOREIGN KEY (workspace_id, attempt_id)
        REFERENCES omnivia_runtime_attempts (workspace_id, attempt_id),
    FOREIGN KEY (workspace_id, capability_grant_id)
        REFERENCES omnivia_runtime_capability_grants (workspace_id, capability_grant_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_runtime_effect_receipts (
    workspace_id                    TEXT    NOT NULL,
    effect_receipt_id               TEXT    NOT NULL,
    run_id                          TEXT    NOT NULL,
    effect_intent_id                TEXT    NOT NULL,
    observed_at_us                  INTEGER NOT NULL,
    response_digest                 TEXT    NOT NULL,
    external_reference_json         TEXT,
    external_reference_digest       TEXT,
    external_reference_byte_length  INTEGER,

    PRIMARY KEY (workspace_id, effect_receipt_id),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(effect_receipt_id) = 'text'
           AND length(effect_receipt_id) BETWEEN 1 AND 128
           AND effect_receipt_id GLOB '[A-Za-z0-9]*'
           AND effect_receipt_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(effect_receipt_id, char(0)) = 0),
    CHECK (typeof(run_id) = 'text' AND length(run_id) BETWEEN 1 AND 128
           AND run_id GLOB '[A-Za-z0-9]*'
           AND run_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(run_id, char(0)) = 0),
    CHECK (typeof(effect_intent_id) = 'text'
           AND length(effect_intent_id) BETWEEN 1 AND 128
           AND effect_intent_id GLOB '[A-Za-z0-9]*'
           AND effect_intent_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(effect_intent_id, char(0)) = 0),
    CHECK (typeof(observed_at_us) = 'integer' AND observed_at_us > 0),
    CHECK (typeof(response_digest) = 'text' AND length(response_digest) = 71
           AND substr(response_digest, 1, 7) = 'sha256:'
           AND substr(response_digest, 8) NOT GLOB '*[^0-9a-f]*'),
    CHECK (external_reference_json IS NULL OR (typeof(external_reference_json) = 'text'
           AND length(CAST(external_reference_json AS BLOB)) BETWEEN 2 AND 8192)),
    CHECK (external_reference_digest IS NULL OR (typeof(external_reference_digest) = 'text'
           AND length(external_reference_digest) = 71
           AND substr(external_reference_digest, 1, 7) = 'sha256:'
           AND substr(external_reference_digest, 8) NOT GLOB '*[^0-9a-f]*')),
    CHECK (external_reference_byte_length IS NULL OR (
           typeof(external_reference_byte_length) = 'integer'
           AND external_reference_byte_length = length(CAST(external_reference_json AS BLOB)))),
    CHECK (
        (external_reference_json IS NULL AND external_reference_digest IS NULL
         AND external_reference_byte_length IS NULL)
        OR (external_reference_json IS NOT NULL AND external_reference_digest IS NOT NULL
            AND external_reference_byte_length IS NOT NULL)
    ),

    FOREIGN KEY (workspace_id, run_id)
        REFERENCES omnivia_runtime_runs (workspace_id, run_id),
    FOREIGN KEY (workspace_id, effect_intent_id)
        REFERENCES omnivia_runtime_effect_intents (workspace_id, effect_intent_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_runtime_effect_settlements (
    workspace_id          TEXT    NOT NULL,
    effect_settlement_id  TEXT    NOT NULL,
    run_id                TEXT    NOT NULL,
    effect_intent_id      TEXT    NOT NULL,
    outcome               TEXT    NOT NULL,
    effect_receipt_id     TEXT,
    settled_at_us         INTEGER NOT NULL,
    reason                TEXT    NOT NULL,
    audit_ref             TEXT    NOT NULL,

    PRIMARY KEY (workspace_id, effect_settlement_id),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(effect_settlement_id) = 'text'
           AND length(effect_settlement_id) BETWEEN 1 AND 128
           AND effect_settlement_id GLOB '[A-Za-z0-9]*'
           AND effect_settlement_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(effect_settlement_id, char(0)) = 0),
    CHECK (typeof(run_id) = 'text' AND length(run_id) BETWEEN 1 AND 128
           AND run_id GLOB '[A-Za-z0-9]*'
           AND run_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(run_id, char(0)) = 0),
    CHECK (typeof(effect_intent_id) = 'text'
           AND length(effect_intent_id) BETWEEN 1 AND 128
           AND effect_intent_id GLOB '[A-Za-z0-9]*'
           AND effect_intent_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(effect_intent_id, char(0)) = 0),
    CHECK (outcome IN ('committed', 'not_committed', 'unknown')),
    CHECK (effect_receipt_id IS NULL OR (typeof(effect_receipt_id) = 'text'
           AND length(effect_receipt_id) BETWEEN 1 AND 128
           AND effect_receipt_id GLOB '[A-Za-z0-9]*'
           AND effect_receipt_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(effect_receipt_id, char(0)) = 0)),
    CHECK ((outcome = 'committed' AND effect_receipt_id IS NOT NULL)
           OR (outcome <> 'committed' AND effect_receipt_id IS NULL)),
    CHECK (typeof(settled_at_us) = 'integer' AND settled_at_us > 0),
    CHECK (typeof(reason) = 'text' AND length(reason) BETWEEN 1 AND 128
           AND reason GLOB '[a-z]*'
           AND reason NOT GLOB '*[^a-z0-9_.]*'
           AND reason NOT GLOB '*.'
           AND reason NOT GLOB '*.[^a-z]*'),
    CHECK (typeof(audit_ref) = 'text' AND length(audit_ref) BETWEEN 1 AND 128
           AND audit_ref GLOB '[A-Za-z0-9]*'
           AND audit_ref NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(audit_ref, char(0)) = 0),

    FOREIGN KEY (workspace_id, run_id)
        REFERENCES omnivia_runtime_runs (workspace_id, run_id),
    FOREIGN KEY (workspace_id, effect_intent_id)
        REFERENCES omnivia_runtime_effect_intents (workspace_id, effect_intent_id),
    FOREIGN KEY (workspace_id, effect_receipt_id)
        REFERENCES omnivia_runtime_effect_receipts (workspace_id, effect_receipt_id),
    FOREIGN KEY (audit_ref, workspace_id)
        REFERENCES omnivia_application_audit_events (audit_ref, workspace_id)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS omnivia_idx_runtime_effect_intents_run
    ON omnivia_runtime_effect_intents (workspace_id, run_id, declared_at_us, effect_intent_id);
CREATE INDEX IF NOT EXISTS omnivia_idx_runtime_effect_receipts_intent
    ON omnivia_runtime_effect_receipts (workspace_id, effect_intent_id, observed_at_us);

CREATE TRIGGER IF NOT EXISTS omnivia_guard_runtime_effect_intents_insert
BEFORE INSERT ON omnivia_runtime_effect_intents
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_runtime_effect_intents')
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
    SELECT RAISE(ABORT, 'omnivia: an effect intent may be declared only by a running run')
    WHERE 'running' IS NOT (
        SELECT run_status FROM omnivia_runtime_events
        WHERE workspace_id = NEW.workspace_id AND run_id = NEW.run_id
        ORDER BY sequence DESC LIMIT 1);
    SELECT RAISE(ABORT, 'omnivia: an effect intent must name the run of its own step and attempt')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_runtime_attempts a
        JOIN omnivia_runtime_run_steps s
          ON s.workspace_id = a.workspace_id AND s.run_step_id = a.run_step_id
        WHERE a.workspace_id = NEW.workspace_id
          AND a.attempt_id = NEW.attempt_id
          AND a.run_id = NEW.run_id
          AND a.run_step_id = NEW.run_step_id
          AND s.run_id = NEW.run_id);
    SELECT RAISE(ABORT, 'omnivia: an effect intent cannot be declared before its attempt starts')
    WHERE NEW.declared_at_us < (
        SELECT started_at_us FROM omnivia_runtime_attempts
        WHERE workspace_id = NEW.workspace_id AND attempt_id = NEW.attempt_id);
    SELECT RAISE(ABORT, 'omnivia: an effect intent may be declared only while its attempt is open')
    WHERE EXISTS (
        SELECT 1 FROM omnivia_runtime_attempt_outcomes
        WHERE workspace_id = NEW.workspace_id AND attempt_id = NEW.attempt_id);
    SELECT RAISE(ABORT, 'omnivia: an effect intent must name a grant issued for its own run')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_runtime_capability_grants
        WHERE workspace_id = NEW.workspace_id
          AND capability_grant_id = NEW.capability_grant_id
          AND run_id = NEW.run_id);
    SELECT RAISE(ABORT, 'omnivia: an effect idempotency key cannot be rebound to different request bytes')
    WHERE EXISTS (
        SELECT 1 FROM omnivia_runtime_effect_intents
        WHERE workspace_id = NEW.workspace_id
          AND run_id = NEW.run_id
          AND idempotency_key = NEW.idempotency_key
          AND request_digest <> NEW.request_digest);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_runtime_effect_intents_update
BEFORE UPDATE ON omnivia_runtime_effect_intents
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_runtime_effect_intents is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_runtime_effect_intents_delete
BEFORE DELETE ON omnivia_runtime_effect_intents
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_runtime_effect_intents is append-only; DELETE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_runtime_effect_receipts_insert
BEFORE INSERT ON omnivia_runtime_effect_receipts
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_runtime_effect_receipts')
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
    SELECT RAISE(ABORT, 'omnivia: an effect receipt must name an intent of its own run')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_runtime_effect_intents
        WHERE workspace_id = NEW.workspace_id
          AND effect_intent_id = NEW.effect_intent_id
          AND run_id = NEW.run_id);
    SELECT RAISE(ABORT, 'omnivia: an effect receipt cannot predate its intent')
    WHERE NEW.observed_at_us < (
        SELECT declared_at_us FROM omnivia_runtime_effect_intents
        WHERE workspace_id = NEW.workspace_id
          AND effect_intent_id = NEW.effect_intent_id);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_runtime_effect_receipts_update
BEFORE UPDATE ON omnivia_runtime_effect_receipts
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_runtime_effect_receipts is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_runtime_effect_receipts_delete
BEFORE DELETE ON omnivia_runtime_effect_receipts
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_runtime_effect_receipts is append-only; DELETE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_runtime_effect_settlements_insert
BEFORE INSERT ON omnivia_runtime_effect_settlements
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_runtime_effect_settlements')
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
    SELECT RAISE(ABORT, 'omnivia: an effect settlement must name an intent of its own run')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_runtime_effect_intents
        WHERE workspace_id = NEW.workspace_id
          AND effect_intent_id = NEW.effect_intent_id
          AND run_id = NEW.run_id);
    SELECT RAISE(ABORT, 'omnivia: an effect settlement cannot predate its intent')
    WHERE NEW.settled_at_us < (
        SELECT declared_at_us FROM omnivia_runtime_effect_intents
        WHERE workspace_id = NEW.workspace_id
          AND effect_intent_id = NEW.effect_intent_id);
    SELECT RAISE(ABORT, 'omnivia: a committed settlement must name a receipt for the same intent')
    WHERE NEW.outcome = 'committed'
      AND NOT EXISTS (
        SELECT 1 FROM omnivia_runtime_effect_receipts
        WHERE workspace_id = NEW.workspace_id
          AND effect_receipt_id = NEW.effect_receipt_id
          AND effect_intent_id = NEW.effect_intent_id
          AND run_id = NEW.run_id);
    SELECT RAISE(ABORT, 'omnivia: an effect settlement audit reference must belong to its workspace')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_application_audit_events
        WHERE audit_ref = NEW.audit_ref AND workspace_id = NEW.workspace_id);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_runtime_effect_settlements_update
BEFORE UPDATE ON omnivia_runtime_effect_settlements
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_runtime_effect_settlements is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_runtime_effect_settlements_delete
BEFORE DELETE ON omnivia_runtime_effect_settlements
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_runtime_effect_settlements is append-only; DELETE is never permitted');
END;
