-- Canonical EffectIntent, dispatch outbox, EffectReceipt and EffectSettlement (RT-205).
--
-- Additive only. Four append-only tables and twelve statement triggers, on top of the
-- canonical records migrations 0018, 0019, 0021 and 0022 added. Every row here belongs
-- to an existing `omnivia_runtime_runs` row; nothing here creates a run, changes a run's
-- status, resolves a wait, issues a grant, or duplicates the event stream.
--
--   omnivia_runtime_effect_intents      one durable intent, declared before anything acts
--   omnivia_runtime_effect_dispatches   the outbox: each time a dispatch request was made
--   omnivia_runtime_effect_receipts     the one observation that intent ever receives
--   omnivia_runtime_effect_settlements  the one final answer that intent ever receives
--
-- *No effect before intent*, made structural rather than policed. A receipt names its
-- intent by foreign key and a dispatch record does too, so neither can exist without a
-- committed intent row -- and because a dispatch row is what a dispatch request is
-- recorded as, a request that was produced before its intent was durable has nowhere to
-- be written down. The Python seam refuses to produce one from inside the intent's own
-- open transaction; this schema is what makes the refusal load-bearing rather than
-- advisory, because there is no second way to record a dispatch.
--
-- *Idempotency is logical, and one level down from the application's.* The accepted
-- contract keys an effect by `idempotency_key` over `request_digest`, so
-- `UNIQUE (workspace_id, idempotency_key)` is that identity: the same key is one effect
-- however many times it is delivered. Whether a second delivery under that key is a
-- replay of the same request or a *conflict* between two different ones is
-- `semantics_runtime.classify_effect_replay`'s answer, and the repository asks it before
-- issuing an insert. This is deliberately not the application's
-- `omnivia_idempotency_claims`: that relation scopes a claim to a caller and a wire
-- operation, and an effect is neither. One authority per level, not one relation for
-- both.
--
-- *The outbox is a table because the crash window is a fact, not an inference.* An
-- intent with no receipt and no recorded dispatch was never handed out, so it settles
-- `not_committed` deterministically. An intent with no receipt and a recorded dispatch
-- may or may not have landed, so it settles `unknown` -- the honest third answer, which
-- is uncertainty and not failure. Without this table the two are indistinguishable and
-- every unreceipted effect would have to settle `unknown`, which would make the answer
-- useless. Dispatch numbers are contiguous from one, exactly as attempt numbers are, so
-- a redelivery is counted rather than overwritten.
--
-- *A receipt is retained, and never becomes a settlement.* One receipt per intent, keyed
-- by the intent, so a duplicate delivery of the same observation has nowhere to land
-- twice and a second, different observation of one effect is refused rather than
-- silently accepted as an amendment. A receipt requires a dispatch to have been
-- recorded: an observation of an effect this runtime never handed out is evidence of
-- something other than what the intent describes, so it is refused. And a receipt is
-- refused once a `not_committed` settlement stands, because that pair is a contradiction
-- -- proof it did not happen beside proof it did -- and this schema fails closed on a
-- contradiction rather than choosing which half to believe.
--
-- *A settlement is made once, and never fabricates success.* One settlement per intent,
-- keyed by the intent, so a second answer of any outcome has nowhere to live. A
-- `committed` settlement must name a receipt, that receipt must be for this same intent,
-- and the foreign key requires it to actually exist -- so `committed` cannot be asserted
-- without a stored observation behind it. A `not_committed` or `unknown` settlement
-- names no receipt at all, because carrying one would claim and deny the same
-- observation; and `not_committed` is refused outright while a receipt for the intent
-- exists, for the same fail-closed reason the receipt guard states.
--
-- What is deliberately absent. No effect-class column: the accepted `EffectIntent` has
-- `effect_kind` and no effect class, and a column for a field the contract does not have
-- would be this database inventing a record -- `EffectClass` stays where RT-204 reads
-- it, in the Host Contract's own vocabulary at the authorization seam. No adapter,
-- binding, endpoint, credential or transport column: what may be invoked is authority
-- and lives here, and the means of invoking it is Platform's and is never persisted by
-- Core. No request or response *bytes*: an intent stores the digest of the request it
-- will send and a receipt the digest of the response that came back, so no payload this
-- database never validated is retained. No reconciliation of an `unknown` settlement,
-- which is a later milestone and not a rule this schema can state. And no DML, because
-- no fact in this database is honestly classifiable as an effect anybody intended, and
-- manufacturing one would invent an action nobody took.
--
-- UPDATE and DELETE abort unconditionally on all four tables, for the current fenced
-- owner too. That is what makes an intent an intent and a settlement a settlement:
-- neither can be rewritten, erased, or replaced under its own name.
--
-- Identity, types, bounds, timestamps and digests follow 0018's, 0019's and 0022's rules
-- exactly. Every comment in this file sits between statements and never inside one, for
-- the fingerprint-loader reason 0018 states.

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
    CHECK (typeof(capability_id) = 'text' AND length(capability_id) BETWEEN 3 AND 128
           AND capability_id GLOB '[a-z]*'
           AND capability_id NOT GLOB '*[^a-z0-9._-]*'
           AND instr(capability_id, '.') > 1),
    CHECK (typeof(capability_grant_id) = 'text'
           AND length(capability_grant_id) BETWEEN 1 AND 128
           AND capability_grant_id GLOB '[A-Za-z0-9]*'
           AND capability_grant_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(capability_grant_id, char(0)) = 0),
    CHECK (typeof(effect_kind) = 'text' AND length(effect_kind) BETWEEN 1 AND 128
           AND effect_kind GLOB '[a-z]*' AND effect_kind NOT GLOB '*[^a-z0-9_.]*'
           AND effect_kind NOT GLOB '*.' AND effect_kind NOT GLOB '*.[^a-z]*'),
    CHECK (typeof(idempotency_key) = 'text'
           AND length(idempotency_key) BETWEEN 1 AND 128
           AND idempotency_key GLOB '[A-Za-z0-9]*'
           AND idempotency_key NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(idempotency_key, char(0)) = 0),
    CHECK (typeof(request_digest) = 'text' AND length(request_digest) = 71
           AND substr(request_digest, 1, 7) = 'sha256:'
           AND substr(request_digest, 8) NOT GLOB '*[^0-9a-f]*'),
    CHECK (typeof(declared_at_us) = 'integer' AND declared_at_us > 0),

    UNIQUE (workspace_id, idempotency_key),

    FOREIGN KEY (workspace_id, run_id)
        REFERENCES omnivia_runtime_runs (workspace_id, run_id),
    FOREIGN KEY (workspace_id, run_step_id)
        REFERENCES omnivia_runtime_run_steps (workspace_id, run_step_id),
    FOREIGN KEY (workspace_id, attempt_id)
        REFERENCES omnivia_runtime_attempts (workspace_id, attempt_id),
    FOREIGN KEY (workspace_id, capability_grant_id)
        REFERENCES omnivia_runtime_capability_grants
            (workspace_id, capability_grant_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_runtime_effect_dispatches (
    workspace_id     TEXT    NOT NULL,
    effect_intent_id TEXT    NOT NULL,
    dispatch_number  INTEGER NOT NULL,
    requested_at_us  INTEGER NOT NULL,

    PRIMARY KEY (workspace_id, effect_intent_id, dispatch_number),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(effect_intent_id) = 'text'
           AND length(effect_intent_id) BETWEEN 1 AND 128
           AND effect_intent_id GLOB '[A-Za-z0-9]*'
           AND effect_intent_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(effect_intent_id, char(0)) = 0),
    CHECK (typeof(dispatch_number) = 'integer'
           AND dispatch_number BETWEEN 1 AND 256),
    CHECK (typeof(requested_at_us) = 'integer' AND requested_at_us > 0),

    FOREIGN KEY (workspace_id, effect_intent_id)
        REFERENCES omnivia_runtime_effect_intents (workspace_id, effect_intent_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_runtime_effect_receipts (
    workspace_id        TEXT    NOT NULL,
    effect_receipt_id   TEXT    NOT NULL,
    run_id              TEXT    NOT NULL,
    effect_intent_id    TEXT    NOT NULL,
    observed_at_us      INTEGER NOT NULL,
    response_digest     TEXT    NOT NULL,
    source_kind         TEXT,
    source_id           TEXT,
    source_workspace_id TEXT,

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
    CHECK (source_kind IS NULL OR source_kind IN ('runtime', 'application_job',
                           'control_plane_projection', 'agent_lane_ledger',
                           'external_log')),
    CHECK (source_id IS NULL OR (typeof(source_id) = 'text'
           AND length(source_id) BETWEEN 1 AND 512
           AND source_id NOT GLOB '*[^!-~]*')),
    CHECK (source_workspace_id IS NULL OR (typeof(source_workspace_id) = 'text'
           AND length(source_workspace_id) BETWEEN 1 AND 128
           AND source_workspace_id GLOB '[A-Za-z0-9]*'
           AND source_workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(source_workspace_id, char(0)) = 0)),
    CHECK ((source_kind IS NULL AND source_id IS NULL
            AND source_workspace_id IS NULL)
           OR (source_kind IS NOT NULL AND source_id IS NOT NULL
               AND source_workspace_id IS NOT NULL)),

    UNIQUE (workspace_id, effect_intent_id),

    FOREIGN KEY (workspace_id, run_id)
        REFERENCES omnivia_runtime_runs (workspace_id, run_id),
    FOREIGN KEY (workspace_id, effect_intent_id)
        REFERENCES omnivia_runtime_effect_intents (workspace_id, effect_intent_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_runtime_effect_settlements (
    workspace_id         TEXT    NOT NULL,
    effect_settlement_id TEXT    NOT NULL,
    run_id               TEXT    NOT NULL,
    effect_intent_id     TEXT    NOT NULL,
    outcome              TEXT    NOT NULL,
    settled_at_us        INTEGER NOT NULL,
    reason               TEXT    NOT NULL,
    audit_ref            TEXT    NOT NULL,
    effect_receipt_id    TEXT,

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
    CHECK (typeof(settled_at_us) = 'integer' AND settled_at_us > 0),
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
    FOREIGN KEY (workspace_id, effect_receipt_id)
        REFERENCES omnivia_runtime_effect_receipts (workspace_id, effect_receipt_id),
    FOREIGN KEY (audit_ref, workspace_id)
        REFERENCES omnivia_application_audit_events (audit_ref, workspace_id)
) WITHOUT ROWID;

-- The four INSERT guards below repeat the complete connection-authority,
-- mutation-guard, workspace-state and lease predicate 0005 established, then add the
-- correlation and ordering rules each fact has. UPDATE and DELETE abort unconditionally
-- on all four tables -- for the current fenced owner too.

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
    SELECT RAISE(ABORT, 'omnivia: an intent must name an attempt of its own step and run')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_runtime_attempts
        WHERE workspace_id = NEW.workspace_id AND attempt_id = NEW.attempt_id
          AND run_step_id = NEW.run_step_id AND run_id = NEW.run_id);
    SELECT RAISE(ABORT, 'omnivia: an intent must act through a grant issued to its own run')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_runtime_capability_grants
        WHERE workspace_id = NEW.workspace_id
          AND capability_grant_id = NEW.capability_grant_id
          AND run_id = NEW.run_id);
    SELECT RAISE(ABORT, 'omnivia: an intent must not be declared before its attempt started')
    WHERE NEW.declared_at_us < (
        SELECT started_at_us FROM omnivia_runtime_attempts
        WHERE workspace_id = NEW.workspace_id AND attempt_id = NEW.attempt_id);
    SELECT RAISE(ABORT, 'omnivia: a finished attempt declares no further effect')
    WHERE EXISTS (
        SELECT 1 FROM omnivia_runtime_attempt_outcomes
        WHERE workspace_id = NEW.workspace_id AND attempt_id = NEW.attempt_id);
    SELECT RAISE(ABORT, 'omnivia: a terminal run admits no further history')
    WHERE EXISTS (
        SELECT 1 FROM omnivia_runtime_events
        WHERE workspace_id = NEW.workspace_id AND run_id = NEW.run_id
          AND run_status IN ('succeeded', 'partially_completed', 'failed', 'cancelled'));
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

CREATE TRIGGER IF NOT EXISTS omnivia_guard_runtime_effect_dispatches_insert
BEFORE INSERT ON omnivia_runtime_effect_dispatches
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_runtime_effect_dispatches')
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
    SELECT RAISE(ABORT, 'omnivia: a dispatch must not be recorded before its intent was declared')
    WHERE NEW.requested_at_us < (
        SELECT declared_at_us FROM omnivia_runtime_effect_intents
        WHERE workspace_id = NEW.workspace_id
          AND effect_intent_id = NEW.effect_intent_id);
    SELECT RAISE(ABORT, 'omnivia: dispatch number must be contiguous within its intent')
    WHERE NEW.dispatch_number IS NOT (
        SELECT COALESCE(MAX(dispatch_number), 0) + 1
        FROM omnivia_runtime_effect_dispatches
        WHERE workspace_id = NEW.workspace_id
          AND effect_intent_id = NEW.effect_intent_id);
    SELECT RAISE(ABORT, 'omnivia: a settled effect is never dispatched again')
    WHERE EXISTS (
        SELECT 1 FROM omnivia_runtime_effect_settlements
        WHERE workspace_id = NEW.workspace_id
          AND effect_intent_id = NEW.effect_intent_id);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_runtime_effect_dispatches_update
BEFORE UPDATE ON omnivia_runtime_effect_dispatches
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_runtime_effect_dispatches is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_runtime_effect_dispatches_delete
BEFORE DELETE ON omnivia_runtime_effect_dispatches
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_runtime_effect_dispatches is append-only; DELETE is never permitted');
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
    SELECT RAISE(ABORT, 'omnivia: a receipt must name an intent of its own run')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_runtime_effect_intents
        WHERE workspace_id = NEW.workspace_id
          AND effect_intent_id = NEW.effect_intent_id AND run_id = NEW.run_id);
    SELECT RAISE(ABORT, 'omnivia: an effect is never observed before it was intended')
    WHERE NEW.observed_at_us < (
        SELECT declared_at_us FROM omnivia_runtime_effect_intents
        WHERE workspace_id = NEW.workspace_id
          AND effect_intent_id = NEW.effect_intent_id);
    SELECT RAISE(ABORT, 'omnivia: an effect never dispatched has no observation to receive')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_runtime_effect_dispatches
        WHERE workspace_id = NEW.workspace_id
          AND effect_intent_id = NEW.effect_intent_id);
    SELECT RAISE(ABORT, 'omnivia: an effect settled not_committed cannot then be observed')
    WHERE EXISTS (
        SELECT 1 FROM omnivia_runtime_effect_settlements
        WHERE workspace_id = NEW.workspace_id
          AND effect_intent_id = NEW.effect_intent_id
          AND outcome = 'not_committed');
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
    SELECT RAISE(ABORT, 'omnivia: a settlement must name an intent of its own run')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_runtime_effect_intents
        WHERE workspace_id = NEW.workspace_id
          AND effect_intent_id = NEW.effect_intent_id AND run_id = NEW.run_id);
    SELECT RAISE(ABORT, 'omnivia: an effect is never settled before it was intended')
    WHERE NEW.settled_at_us < (
        SELECT declared_at_us FROM omnivia_runtime_effect_intents
        WHERE workspace_id = NEW.workspace_id
          AND effect_intent_id = NEW.effect_intent_id);
    SELECT RAISE(ABORT, 'omnivia: a committed settlement must name a receipt for its own intent')
    WHERE NEW.effect_receipt_id IS NOT NULL
      AND NOT EXISTS (
        SELECT 1 FROM omnivia_runtime_effect_receipts
        WHERE workspace_id = NEW.workspace_id
          AND effect_receipt_id = NEW.effect_receipt_id
          AND effect_intent_id = NEW.effect_intent_id);
    SELECT RAISE(ABORT, 'omnivia: an observed effect cannot be settled not_committed')
    WHERE NEW.outcome = 'not_committed'
      AND EXISTS (
        SELECT 1 FROM omnivia_runtime_effect_receipts
        WHERE workspace_id = NEW.workspace_id
          AND effect_intent_id = NEW.effect_intent_id);
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
