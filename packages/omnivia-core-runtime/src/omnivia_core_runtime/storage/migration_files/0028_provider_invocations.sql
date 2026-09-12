-- Durable Provider Invocation foundation: one logical invocation, its ordered transport
-- attempts, the one complete terminal route evidence fact, and an append-only lifecycle.
--
-- Additive only. Four append-only tables, four named indexes and twelve statement
-- triggers. This file stores `ProviderInvocationRecord` truth (contracts/chat/v1
-- provider.schema.json) and nothing that acts on it: no adapter, no transport, no
-- repository, no service, no renderer state and no Chat state. It holds no credential,
-- no Provider SDK object or exception, no endpoint address, no arbitrary request header,
-- no raw provider payload and no prompt: only stable identities, closed vocabulary
-- members, bounded counts, timestamps and one bounded canonical usage document.
--
--   omnivia_provider_invocations                    one logical invocation identity
--   omnivia_provider_invocation_attempts            its ordered transport attempts
--   omnivia_provider_invocation_route_evidence      the one complete terminal evidence
--   omnivia_provider_invocation_lifecycle_events    the append-only lifecycle history
--
-- The record is a projection of durable truth, so it is stored as facts rather than as
-- the wire document. `attemptIds` becomes one row per transport attempt, keyed by a
-- contiguous sequence so "oldest first" is the key rather than a convention, and made
-- duplicate-free by a named unique index on the provider-issued attempt identity. The
-- lifecycle is a chain of events, not a mutable column: the six closed states admit
-- exactly the transitions checked below, and because `succeeded`, `failed`,
-- `cancelled` and `indeterminate` never appear as a `from_state`, they are terminal
-- by construction.
--
-- Route evidence is one row per invocation and is complete or absent. `RouteEvidence`
-- requires every field it declares whenever it is present at all, so the columns are
-- `NOT NULL` (only `estimated_cost_json` is genuinely optional in the contract), and
-- the three route decisions are a single `CHECK` over columns of the same row:
-- `configured` and `same_route_retry` admit no fallback and an admitted route equal to
-- the configured one, separated by whether the retry count is zero or positive, while
-- `fallback` requires an authorised fallback to a route that actually differs. The
-- configured pair is repeated here rather than only on the invocation so that
-- comparison is a row-local `CHECK`; the insert trigger then requires it to equal the
-- invocation's own configured route, so the repetition cannot disagree with its source.
--
-- What each lifecycle state is allowed to assert is enforced where the state is
-- recorded. A `requested` invocation may have no transport attempt at all; every other
-- state requires at least one. A terminal state requires its terminal time -- carried on
-- the event, not on a mutable header -- and complete route evidence. `indeterminate`
-- requires a reconciliation state and forbids route evidence, checked in both
-- directions: an indeterminate invocation refuses later evidence just as evidence
-- refuses a later indeterminate event.
--
-- Every comment sits between statements and never inside one, because the migrator
-- strips comments while the canonical fingerprint replays this text verbatim.
--
-- UPDATE and DELETE abort unconditionally, for the current fenced owner too.

CREATE TABLE IF NOT EXISTS omnivia_provider_invocations (
    workspace_id             TEXT    NOT NULL,
    invocation_id            TEXT    NOT NULL,
    conversation_id          TEXT    NOT NULL,
    job_id                   TEXT    NOT NULL,
    generation_attempt_id    TEXT    NOT NULL,
    operation                TEXT    NOT NULL,
    configured_connection_id TEXT    NOT NULL,
    configured_model_id      TEXT    NOT NULL,
    created_at_us            INTEGER NOT NULL,

    PRIMARY KEY (workspace_id, invocation_id),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(invocation_id) = 'text' AND length(invocation_id) BETWEEN 1 AND 128
           AND invocation_id GLOB '[A-Za-z0-9]*'
           AND invocation_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(invocation_id, char(0)) = 0),
    CHECK (typeof(conversation_id) = 'text'
           AND length(conversation_id) BETWEEN 1 AND 128
           AND conversation_id GLOB '[A-Za-z0-9]*'
           AND conversation_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(conversation_id, char(0)) = 0),
    CHECK (typeof(job_id) = 'text' AND length(job_id) BETWEEN 1 AND 128
           AND job_id GLOB '[A-Za-z0-9]*'
           AND job_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(job_id, char(0)) = 0),
    CHECK (typeof(generation_attempt_id) = 'text'
           AND length(generation_attempt_id) BETWEEN 1 AND 128
           AND generation_attempt_id GLOB '[A-Za-z0-9]*'
           AND generation_attempt_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(generation_attempt_id, char(0)) = 0),
    CHECK (operation = 'language.stream'),
    CHECK (typeof(configured_connection_id) = 'text'
           AND length(configured_connection_id) BETWEEN 1 AND 128
           AND configured_connection_id GLOB '[A-Za-z0-9]*'
           AND configured_connection_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(configured_connection_id, char(0)) = 0),
    CHECK (typeof(configured_model_id) = 'text'
           AND length(configured_model_id) BETWEEN 1 AND 256
           AND configured_model_id GLOB '[A-Za-z0-9]*'
           AND configured_model_id NOT GLOB '*[^A-Za-z0-9/._:@-]*'
           AND instr(configured_model_id, '://') = 0
           AND instr(configured_model_id, char(0)) = 0),
    CHECK (typeof(created_at_us) = 'integer' AND created_at_us > 0)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_provider_invocation_attempts (
    workspace_id        TEXT    NOT NULL,
    invocation_id       TEXT    NOT NULL,
    attempt_sequence    INTEGER NOT NULL,
    provider_attempt_id TEXT    NOT NULL,
    admitted_at_us      INTEGER NOT NULL,

    PRIMARY KEY (workspace_id, invocation_id, attempt_sequence),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(invocation_id) = 'text' AND length(invocation_id) BETWEEN 1 AND 128
           AND invocation_id GLOB '[A-Za-z0-9]*'
           AND invocation_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(invocation_id, char(0)) = 0),
    CHECK (typeof(attempt_sequence) = 'integer'
           AND attempt_sequence BETWEEN 1 AND 1000),
    CHECK (typeof(provider_attempt_id) = 'text'
           AND length(provider_attempt_id) BETWEEN 1 AND 128
           AND provider_attempt_id GLOB '[A-Za-z0-9]*'
           AND provider_attempt_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(provider_attempt_id, char(0)) = 0),
    CHECK (typeof(admitted_at_us) = 'integer' AND admitted_at_us > 0),

    FOREIGN KEY (workspace_id, invocation_id)
        REFERENCES omnivia_provider_invocations (workspace_id, invocation_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_provider_invocation_route_evidence (
    workspace_id             TEXT    NOT NULL,
    invocation_id            TEXT    NOT NULL,
    configured_connection_id TEXT    NOT NULL,
    configured_model_id      TEXT    NOT NULL,
    admitted_connection_id   TEXT    NOT NULL,
    admitted_model_id        TEXT    NOT NULL,
    adapter_name             TEXT    NOT NULL,
    adapter_version          TEXT    NOT NULL,
    route_decision           TEXT    NOT NULL,
    same_route_retry_count   INTEGER NOT NULL,
    fallback_authorised      INTEGER NOT NULL,
    attempt_started_at_us    INTEGER NOT NULL,
    attempt_ended_at_us      INTEGER NOT NULL,
    terminal_reason          TEXT    NOT NULL,
    usage_json               TEXT    NOT NULL,
    estimated_cost_json      TEXT,
    reconciliation_state     TEXT    NOT NULL,
    recorded_at_us           INTEGER NOT NULL,

    PRIMARY KEY (workspace_id, invocation_id),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(invocation_id) = 'text' AND length(invocation_id) BETWEEN 1 AND 128
           AND invocation_id GLOB '[A-Za-z0-9]*'
           AND invocation_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(invocation_id, char(0)) = 0),
    CHECK (typeof(configured_connection_id) = 'text'
           AND length(configured_connection_id) BETWEEN 1 AND 128
           AND configured_connection_id GLOB '[A-Za-z0-9]*'
           AND configured_connection_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(configured_connection_id, char(0)) = 0),
    CHECK (typeof(admitted_connection_id) = 'text'
           AND length(admitted_connection_id) BETWEEN 1 AND 128
           AND admitted_connection_id GLOB '[A-Za-z0-9]*'
           AND admitted_connection_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(admitted_connection_id, char(0)) = 0),
    CHECK (typeof(configured_model_id) = 'text'
           AND length(configured_model_id) BETWEEN 1 AND 256
           AND configured_model_id GLOB '[A-Za-z0-9]*'
           AND configured_model_id NOT GLOB '*[^A-Za-z0-9/._:@-]*'
           AND instr(configured_model_id, '://') = 0
           AND instr(configured_model_id, char(0)) = 0),
    CHECK (typeof(admitted_model_id) = 'text'
           AND length(admitted_model_id) BETWEEN 1 AND 256
           AND admitted_model_id GLOB '[A-Za-z0-9]*'
           AND admitted_model_id NOT GLOB '*[^A-Za-z0-9/._:@-]*'
           AND instr(admitted_model_id, '://') = 0
           AND instr(admitted_model_id, char(0)) = 0),
    CHECK (typeof(adapter_name) = 'text' AND length(adapter_name) BETWEEN 1 AND 128
           AND adapter_name GLOB '[A-Za-z0-9]*'
           AND adapter_name NOT GLOB '*[^A-Za-z0-9._-]*'),
    CHECK (typeof(adapter_version) = 'text'
           AND length(adapter_version) BETWEEN 1 AND 64
           AND adapter_version GLOB '[0-9]*'
           AND adapter_version NOT GLOB '*[^0-9A-Za-z.+-]*'),
    CHECK (route_decision IN ('configured', 'same_route_retry', 'fallback')),
    CHECK (typeof(same_route_retry_count) = 'integer'
           AND same_route_retry_count BETWEEN 0 AND 1000),
    CHECK (fallback_authorised IN (0, 1)),
    CHECK ((route_decision = 'configured'
            AND fallback_authorised = 0
            AND same_route_retry_count = 0
            AND admitted_connection_id = configured_connection_id
            AND admitted_model_id = configured_model_id)
           OR (route_decision = 'same_route_retry'
               AND fallback_authorised = 0
               AND same_route_retry_count > 0
               AND admitted_connection_id = configured_connection_id
               AND admitted_model_id = configured_model_id)
           OR (route_decision = 'fallback'
               AND fallback_authorised = 1
               AND (admitted_connection_id <> configured_connection_id
                    OR admitted_model_id <> configured_model_id))),
    CHECK (typeof(attempt_started_at_us) = 'integer' AND attempt_started_at_us > 0),
    CHECK (typeof(attempt_ended_at_us) = 'integer'
           AND attempt_ended_at_us >= attempt_started_at_us),
    CHECK (terminal_reason IN ('stop', 'length', 'tool-calls', 'content-filter',
                               'error', 'cancelled', 'unknown', 'authentication',
                               'permission', 'model-not-found', 'rate-limited',
                               'quota-or-budget', 'invalid-request',
                               'context-window-exceeded', 'content-policy',
                               'endpoint-policy', 'timeout', 'transport',
                               'provider-unavailable', 'malformed-response',
                               'unsupported-operation')),
    CHECK (typeof(usage_json) = 'text'
           AND length(CAST(usage_json AS BLOB)) BETWEEN 2 AND 4096
           AND instr(usage_json, char(0)) = 0),
    CHECK (estimated_cost_json IS NULL
           OR (typeof(estimated_cost_json) = 'text'
               AND length(CAST(estimated_cost_json AS BLOB)) BETWEEN 2 AND 4096
               AND instr(estimated_cost_json, char(0)) = 0)),
    CHECK (reconciliation_state IN ('reconciled', 'pending_reconciliation',
                                    'unreconciled')),
    CHECK (typeof(recorded_at_us) = 'integer' AND recorded_at_us > 0),

    FOREIGN KEY (workspace_id, invocation_id)
        REFERENCES omnivia_provider_invocations (workspace_id, invocation_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_provider_invocation_lifecycle_events (
    workspace_id         TEXT    NOT NULL,
    invocation_id        TEXT    NOT NULL,
    event_sequence       INTEGER NOT NULL,
    from_state           TEXT,
    to_state             TEXT    NOT NULL,
    terminal_at_us       INTEGER,
    reconciliation_state TEXT,
    occurred_at_us       INTEGER NOT NULL,

    PRIMARY KEY (workspace_id, invocation_id, event_sequence),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(invocation_id) = 'text' AND length(invocation_id) BETWEEN 1 AND 128
           AND invocation_id GLOB '[A-Za-z0-9]*'
           AND invocation_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(invocation_id, char(0)) = 0),
    CHECK (typeof(event_sequence) = 'integer'
           AND event_sequence BETWEEN 1 AND 1000000),
    CHECK (to_state IN ('requested', 'in_progress', 'succeeded', 'failed',
                        'cancelled', 'indeterminate')),
    CHECK (from_state IS NULL
           OR from_state IN ('requested', 'in_progress')),
    CHECK ((from_state IS NULL AND to_state = 'requested' AND event_sequence = 1)
           OR (from_state = 'requested'
               AND to_state IN ('in_progress', 'failed', 'cancelled',
                                'indeterminate'))
           OR (from_state = 'in_progress'
               AND to_state IN ('succeeded', 'failed', 'cancelled',
                                'indeterminate'))),
    CHECK (terminal_at_us IS NULL
           OR (typeof(terminal_at_us) = 'integer' AND terminal_at_us > 0)),
    CHECK ((to_state IN ('succeeded', 'failed', 'cancelled')
            AND terminal_at_us IS NOT NULL)
           OR (to_state NOT IN ('succeeded', 'failed', 'cancelled')
               AND terminal_at_us IS NULL)),
    CHECK (reconciliation_state IS NULL
           OR reconciliation_state IN ('reconciled', 'pending_reconciliation',
                                       'unreconciled')),
    CHECK ((to_state = 'indeterminate' AND reconciliation_state IS NOT NULL)
           OR (to_state <> 'indeterminate' AND reconciliation_state IS NULL)),
    CHECK (typeof(occurred_at_us) = 'integer' AND occurred_at_us > 0),

    FOREIGN KEY (workspace_id, invocation_id)
        REFERENCES omnivia_provider_invocations (workspace_id, invocation_id)
) WITHOUT ROWID;

-- Four named indexes: the two W3 read paths that are not a prefix of a primary key,
-- the per-invocation uniqueness of a provider-issued attempt identity, and lifecycle
-- discovery by state. The unique one is declared by name rather than as an inline
-- `UNIQUE` clause because the canonical schema fingerprint filters implicit indexes
-- out, and a constraint drift detection cannot see is not a constraint.
CREATE INDEX IF NOT EXISTS omnivia_idx_provider_invocations_job
    ON omnivia_provider_invocations (workspace_id, job_id, invocation_id);
CREATE INDEX IF NOT EXISTS omnivia_idx_provider_invocations_generation_attempt
    ON omnivia_provider_invocations
        (workspace_id, generation_attempt_id, invocation_id);
CREATE UNIQUE INDEX IF NOT EXISTS omnivia_idx_provider_invocation_attempts_identity
    ON omnivia_provider_invocation_attempts
        (workspace_id, invocation_id, provider_attempt_id);
CREATE INDEX IF NOT EXISTS omnivia_idx_provider_invocation_lifecycle_events_state
    ON omnivia_provider_invocation_lifecycle_events
        (workspace_id, to_state, invocation_id, event_sequence);

CREATE TRIGGER IF NOT EXISTS omnivia_guard_provider_invocations_insert
BEFORE INSERT ON omnivia_provider_invocations
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_provider_invocations')
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
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_provider_invocations_update
BEFORE UPDATE ON omnivia_provider_invocations
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_provider_invocations is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_provider_invocations_delete
BEFORE DELETE ON omnivia_provider_invocations
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_provider_invocations is append-only; DELETE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_provider_invocation_attempts_insert
BEFORE INSERT ON omnivia_provider_invocation_attempts
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_provider_invocation_attempts')
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
    SELECT RAISE(ABORT, 'omnivia: provider invocation attempts must be contiguous from one, oldest first')
    WHERE NEW.attempt_sequence IS NOT (
        SELECT COALESCE(MAX(attempt_sequence), 0) + 1
        FROM omnivia_provider_invocation_attempts
        WHERE workspace_id = NEW.workspace_id
          AND invocation_id = NEW.invocation_id);
    SELECT RAISE(ABORT, 'omnivia: a closed provider invocation admits no further transport attempt')
    WHERE EXISTS (
        SELECT 1 FROM omnivia_provider_invocation_lifecycle_events
        WHERE workspace_id = NEW.workspace_id
          AND invocation_id = NEW.invocation_id
          AND to_state IN ('succeeded', 'failed', 'cancelled', 'indeterminate'));
    SELECT RAISE(ABORT, 'omnivia: provider invocation attempt times must not move backward')
    WHERE NEW.attempt_sequence > 1
      AND NEW.admitted_at_us < (
        SELECT MAX(admitted_at_us)
        FROM omnivia_provider_invocation_attempts
        WHERE workspace_id = NEW.workspace_id
          AND invocation_id = NEW.invocation_id);
    SELECT RAISE(ABORT, 'omnivia: a provider invocation attempt cannot predate its invocation')
    WHERE NEW.admitted_at_us < (
        SELECT created_at_us FROM omnivia_provider_invocations
        WHERE workspace_id = NEW.workspace_id
          AND invocation_id = NEW.invocation_id);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_provider_invocation_attempts_update
BEFORE UPDATE ON omnivia_provider_invocation_attempts
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_provider_invocation_attempts is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_provider_invocation_attempts_delete
BEFORE DELETE ON omnivia_provider_invocation_attempts
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_provider_invocation_attempts is append-only; DELETE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_provider_invocation_route_evidence_insert
BEFORE INSERT ON omnivia_provider_invocation_route_evidence
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_provider_invocation_route_evidence')
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
    SELECT RAISE(ABORT, 'omnivia: route evidence must name the route its invocation was configured with')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_provider_invocations
        WHERE workspace_id = NEW.workspace_id
          AND invocation_id = NEW.invocation_id
          AND configured_connection_id = NEW.configured_connection_id
          AND configured_model_id = NEW.configured_model_id);
    SELECT RAISE(ABORT, 'omnivia: route evidence requires at least one transport attempt')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_provider_invocation_attempts
        WHERE workspace_id = NEW.workspace_id
          AND invocation_id = NEW.invocation_id);
    SELECT RAISE(ABORT, 'omnivia: route evidence retry count must match admitted transport attempts')
    WHERE (NEW.route_decision = 'configured'
           AND (SELECT COUNT(*) FROM omnivia_provider_invocation_attempts
                WHERE workspace_id = NEW.workspace_id
                  AND invocation_id = NEW.invocation_id) <> 1)
       OR (NEW.route_decision = 'same_route_retry'
           AND (SELECT COUNT(*) FROM omnivia_provider_invocation_attempts
                WHERE workspace_id = NEW.workspace_id
                  AND invocation_id = NEW.invocation_id)
               <> NEW.same_route_retry_count + 1);
    SELECT RAISE(ABORT, 'omnivia: an indeterminate provider invocation carries no route evidence')
    WHERE EXISTS (
        SELECT 1 FROM omnivia_provider_invocation_lifecycle_events
        WHERE workspace_id = NEW.workspace_id
          AND invocation_id = NEW.invocation_id
          AND to_state = 'indeterminate');
    SELECT RAISE(ABORT, 'omnivia: route evidence cannot predate its invocation')
    WHERE NEW.attempt_started_at_us < (
        SELECT created_at_us FROM omnivia_provider_invocations
        WHERE workspace_id = NEW.workspace_id
          AND invocation_id = NEW.invocation_id);
    SELECT RAISE(ABORT, 'omnivia: route evidence documents must be exact canonical JSON objects')
    WHERE json_valid(NEW.usage_json) IS NOT 1
       OR json(NEW.usage_json) <> NEW.usage_json
       OR json_type(NEW.usage_json) <> 'object'
       OR (NEW.estimated_cost_json IS NOT NULL
           AND (json_valid(NEW.estimated_cost_json) IS NOT 1
                OR json(NEW.estimated_cost_json) <> NEW.estimated_cost_json
                OR json_type(NEW.estimated_cost_json) <> 'object'));
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_provider_invocation_route_evidence_update
BEFORE UPDATE ON omnivia_provider_invocation_route_evidence
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_provider_invocation_route_evidence is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_provider_invocation_route_evidence_delete
BEFORE DELETE ON omnivia_provider_invocation_route_evidence
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_provider_invocation_route_evidence is append-only; DELETE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_provider_invocation_lifecycle_events_insert
BEFORE INSERT ON omnivia_provider_invocation_lifecycle_events
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_provider_invocation_lifecycle_events')
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
    SELECT RAISE(ABORT, 'omnivia: a provider invocation lifecycle event must continue the invocation''s current state')
    WHERE NEW.event_sequence IS NOT (
            1 + (SELECT COUNT(*) FROM omnivia_provider_invocation_lifecycle_events
                 WHERE workspace_id = NEW.workspace_id
                   AND invocation_id = NEW.invocation_id))
       OR NEW.from_state IS NOT (
            SELECT to_state FROM omnivia_provider_invocation_lifecycle_events
            WHERE workspace_id = NEW.workspace_id
              AND invocation_id = NEW.invocation_id
              AND event_sequence = NEW.event_sequence - 1);
    SELECT RAISE(ABORT, 'omnivia: only a requested provider invocation may have no transport attempt')
    WHERE NEW.to_state <> 'requested'
      AND NOT EXISTS (
        SELECT 1 FROM omnivia_provider_invocation_attempts
        WHERE workspace_id = NEW.workspace_id
          AND invocation_id = NEW.invocation_id);
    SELECT RAISE(ABORT, 'omnivia: a terminal provider invocation requires complete route evidence')
    WHERE NEW.to_state IN ('succeeded', 'failed', 'cancelled')
      AND NOT EXISTS (
        SELECT 1 FROM omnivia_provider_invocation_route_evidence
        WHERE workspace_id = NEW.workspace_id
          AND invocation_id = NEW.invocation_id);
    SELECT RAISE(ABORT, 'omnivia: a terminal provider invocation cannot predate its route evidence')
    WHERE NEW.to_state IN ('succeeded', 'failed', 'cancelled')
      AND NEW.terminal_at_us < (
        SELECT attempt_ended_at_us
        FROM omnivia_provider_invocation_route_evidence
        WHERE workspace_id = NEW.workspace_id
          AND invocation_id = NEW.invocation_id);
    SELECT RAISE(ABORT, 'omnivia: an indeterminate provider invocation carries no route evidence')
    WHERE NEW.to_state = 'indeterminate'
      AND EXISTS (
        SELECT 1 FROM omnivia_provider_invocation_route_evidence
        WHERE workspace_id = NEW.workspace_id
          AND invocation_id = NEW.invocation_id);
    SELECT RAISE(ABORT, 'omnivia: a provider invocation lifecycle event cannot predate its invocation')
    WHERE NEW.occurred_at_us < (
        SELECT created_at_us FROM omnivia_provider_invocations
        WHERE workspace_id = NEW.workspace_id
          AND invocation_id = NEW.invocation_id);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_provider_invocation_lifecycle_events_update
BEFORE UPDATE ON omnivia_provider_invocation_lifecycle_events
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_provider_invocation_lifecycle_events is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_provider_invocation_lifecycle_events_delete
BEFORE DELETE ON omnivia_provider_invocation_lifecycle_events
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_provider_invocation_lifecycle_events is append-only; DELETE is never permitted');
END;
