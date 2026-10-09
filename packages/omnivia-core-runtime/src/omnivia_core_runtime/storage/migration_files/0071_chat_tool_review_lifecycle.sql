-- Chat C8: tool-call lifecycle ledger and Review / human-input decision commands.
--
-- Additive only. 0032 keeps the proposal, post-policy argument and result records
-- of a governed tool call; 0033 keeps the durable wait. This migration adds what
-- neither can express without rewriting an applied table:
--
--   * `omnivia_chat_tool_lifecycle_events` -- an append-only, per-tool-call ledger of
--     the nine C8 states (prepared, policy_checking, awaiting_review, running,
--     succeeded, failed, cancelled, interrupted, outcome_unknown). Replay is a read of
--     this ledger in sequence order, so no state is ever overwritten and a restart
--     cannot lose one. The transition guard lives here: `running` has no exit except
--     a terminal state, and `outcome_unknown` has no exit at all, so nothing can
--     re-drive an execution whose effect is ambiguous.
--   * target action / resource revision columns on the 0033 wait, so a Review binds
--     the exact action and the resource revision the reviewer saw.
--   * `omnivia_chat_wait_decisions` -- an append-only ledger of the two separate
--     decision commands (`review_decision`, `human_input`), keyed by command id for
--     idempotency and by wait id so a wait is decided at most once.
--
-- No column stores a credential, provider SDK object or endpoint. A human-input
-- answer is kept only as a digest and a byte length.

ALTER TABLE omnivia_chat_wait_interactions ADD COLUMN target_action_digest TEXT
    CHECK (target_action_digest IS NULL
           OR (target_action_digest GLOB 'sha256:[0-9a-f]*'
               AND length(target_action_digest) = 71));

ALTER TABLE omnivia_chat_wait_interactions ADD COLUMN target_resource_ref TEXT;

ALTER TABLE omnivia_chat_wait_interactions ADD COLUMN target_resource_revision INTEGER
    CHECK (target_resource_revision IS NULL
           OR (typeof(target_resource_revision) = 'integer' AND target_resource_revision >= 0));

DROP TRIGGER IF EXISTS omnivia_guard_chat_wait_interactions_update;

CREATE TRIGGER omnivia_guard_chat_wait_interactions_update
BEFORE UPDATE ON omnivia_chat_wait_interactions
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded UPDATE on omnivia_chat_wait_interactions')
    WHERE omnivia_service_writer() IS NOT 1;
    SELECT RAISE(ABORT, 'omnivia: chat wait identity is immutable')
    WHERE NEW.workspace_id <> OLD.workspace_id
       OR NEW.conversation_id <> OLD.conversation_id
       OR NEW.wait_id <> OLD.wait_id
       OR NEW.kind <> OLD.kind
       OR NEW.requester_actor_id <> OLD.requester_actor_id
       OR NEW.resume_token_digest <> OLD.resume_token_digest
       OR NEW.created_at_us <> OLD.created_at_us
       OR NEW.schema_version <> OLD.schema_version
       OR NEW.target_action_digest IS NOT OLD.target_action_digest
       OR NEW.target_resource_ref IS NOT OLD.target_resource_ref
       OR NEW.target_resource_revision IS NOT OLD.target_resource_revision
       OR NEW.expires_at_us IS NOT OLD.expires_at_us
       OR NEW.authorised_responder_policy_json <> OLD.authorised_responder_policy_json
       OR NEW.prompt_json <> OLD.prompt_json;
    SELECT RAISE(ABORT, 'omnivia: chat wait version must advance by one')
    WHERE NEW.version <> OLD.version + 1;
    SELECT RAISE(ABORT, 'omnivia: chat wait transition is invalid')
    WHERE NOT ((OLD.state = 'asked' AND NEW.state IN ('asked', 'decided', 'expired', 'failed'))
            OR (OLD.state IN ('decided', 'expired', 'failed') AND NEW.state = OLD.state));
END;

CREATE TABLE IF NOT EXISTS omnivia_chat_wait_decisions (
    workspace_id              TEXT    NOT NULL,
    wait_id                   TEXT    NOT NULL,
    command_id                TEXT    NOT NULL,
    command_kind              TEXT    NOT NULL,
    actor_id                  TEXT    NOT NULL,
    outcome                   TEXT    NOT NULL,
    request_version           INTEGER NOT NULL,
    target_action_digest      TEXT,
    target_resource_revision  INTEGER,
    authorization_basis       TEXT    NOT NULL,
    request_digest            TEXT    NOT NULL,
    answer_digest             TEXT,
    answer_bytes              INTEGER,
    audit_ref                 TEXT,
    decided_at_us             INTEGER NOT NULL,
    schema_version            INTEGER NOT NULL,

    PRIMARY KEY (workspace_id, command_id),
    UNIQUE (workspace_id, wait_id),

    CHECK (length(command_id) BETWEEN 1 AND 200),
    CHECK (command_kind IN ('review_decision', 'human_input')),
    CHECK (outcome IN ('approved', 'denied', 'answered')),
    CHECK ((command_kind = 'human_input') = (outcome = 'answered')),
    CHECK ((command_kind = 'human_input') = (answer_digest IS NOT NULL)),
    CHECK ((answer_digest IS NULL) = (answer_bytes IS NULL)),
    CHECK (answer_digest IS NULL
           OR (answer_digest GLOB 'sha256:[0-9a-f]*' AND length(answer_digest) = 71)),
    CHECK (answer_bytes IS NULL OR (typeof(answer_bytes) = 'integer' AND answer_bytes >= 1)),
    CHECK (authorization_basis IN ('actorId', 'actorIds', 'anyActor')),
    CHECK (request_digest GLOB 'sha256:[0-9a-f]*' AND length(request_digest) = 71),
    CHECK (target_action_digest IS NULL
           OR (target_action_digest GLOB 'sha256:[0-9a-f]*'
               AND length(target_action_digest) = 71)),
    CHECK (typeof(request_version) = 'integer' AND request_version >= 1),
    CHECK (typeof(decided_at_us) = 'integer' AND decided_at_us > 0),
    CHECK (typeof(schema_version) = 'integer' AND schema_version = 1),

    FOREIGN KEY (workspace_id, wait_id)
        REFERENCES omnivia_chat_wait_interactions (workspace_id, wait_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_chat_tool_lifecycle_events (
    workspace_id              TEXT    NOT NULL,
    tool_call_id              TEXT    NOT NULL,
    lifecycle_sequence        INTEGER NOT NULL,
    state                     TEXT    NOT NULL,
    wait_id                   TEXT,
    agent_run_id              TEXT,
    reason_code               TEXT,
    result_id                 TEXT,
    evidence_refs_json        TEXT    NOT NULL,
    artifact_refs_json        TEXT    NOT NULL,
    occurred_at_us            INTEGER NOT NULL,
    schema_version            INTEGER NOT NULL,

    PRIMARY KEY (workspace_id, tool_call_id, lifecycle_sequence),

    CHECK (state IN ('prepared', 'policy_checking', 'awaiting_review', 'running',
                     'succeeded', 'failed', 'cancelled', 'interrupted',
                     'outcome_unknown')),
    CHECK (typeof(lifecycle_sequence) = 'integer' AND lifecycle_sequence >= 1),
    CHECK (state <> 'awaiting_review' OR wait_id IS NOT NULL),
    CHECK (state NOT IN ('failed', 'cancelled', 'interrupted', 'outcome_unknown')
           OR (reason_code IS NOT NULL AND length(reason_code) BETWEEN 1 AND 64)),
    CHECK ((state = 'succeeded') = (result_id IS NOT NULL)),
    CHECK (json_valid(evidence_refs_json) AND json_type(evidence_refs_json) = 'array'),
    CHECK (json_valid(artifact_refs_json) AND json_type(artifact_refs_json) = 'array'),
    CHECK (state IN ('succeeded', 'failed', 'outcome_unknown')
           OR (evidence_refs_json = '[]' AND artifact_refs_json = '[]')),
    CHECK (typeof(occurred_at_us) = 'integer' AND occurred_at_us > 0),
    CHECK (typeof(schema_version) = 'integer' AND schema_version = 1),

    FOREIGN KEY (workspace_id, tool_call_id)
        REFERENCES omnivia_chat_tool_calls (workspace_id, tool_call_id),
    FOREIGN KEY (workspace_id, wait_id)
        REFERENCES omnivia_chat_wait_interactions (workspace_id, wait_id),
    FOREIGN KEY (workspace_id, agent_run_id)
        REFERENCES omnivia_chat_agent_runs (workspace_id, agent_run_id),
    FOREIGN KEY (workspace_id, result_id)
        REFERENCES omnivia_chat_tool_results (workspace_id, result_id)
) WITHOUT ROWID;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_chat_wait_decisions_insert
BEFORE INSERT ON omnivia_chat_wait_decisions
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_chat_wait_decisions')
    WHERE omnivia_service_writer() IS NOT 1;
    SELECT RAISE(ABORT, 'omnivia: a wait decision must record the decided wait it answers')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_chat_wait_interactions w
        WHERE w.workspace_id = NEW.workspace_id
          AND w.wait_id = NEW.wait_id
          AND w.state = 'decided'
          AND w.decision = NEW.outcome
          AND w.decided_by_actor_id = NEW.actor_id
          AND w.decided_at_us = NEW.decided_at_us
          AND w.version = NEW.request_version + 1
          AND w.target_action_digest IS NEW.target_action_digest
          AND w.target_resource_revision IS NEW.target_resource_revision
          AND w.sensitive_answer_ciphertext_digest IS NEW.answer_digest
    );
    SELECT RAISE(ABORT, 'omnivia: the decision command does not match the wait kind')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_chat_wait_interactions w
        WHERE w.workspace_id = NEW.workspace_id
          AND w.wait_id = NEW.wait_id
          AND ((NEW.command_kind = 'review_decision' AND w.kind IN ('approval', 'policy_pause'))
            OR (NEW.command_kind = 'human_input' AND w.kind = 'human_input'))
    );
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_chat_wait_decisions_update
BEFORE UPDATE ON omnivia_chat_wait_decisions
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_chat_wait_decisions is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_chat_wait_decisions_delete
BEFORE DELETE ON omnivia_chat_wait_decisions
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_chat_wait_decisions forbids DELETE');
END;

-- `running` has exits only to a terminal state and `outcome_unknown` has none, so an
-- execution whose effect is ambiguous can never be started again. Only the Review
-- decision command can authorise a run that waited for Review: the decision must be an
-- approval of the exact action. (Comments stay outside the trigger body: the migration
-- runner splits statements and the schema fingerprint covers the stored text.)
CREATE TRIGGER IF NOT EXISTS omnivia_guard_chat_tool_lifecycle_events_insert
BEFORE INSERT ON omnivia_chat_tool_lifecycle_events
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_chat_tool_lifecycle_events')
    WHERE omnivia_service_writer() IS NOT 1;
    SELECT RAISE(ABORT, 'omnivia: chat tool lifecycle sequence must follow the previous event')
    WHERE NEW.lifecycle_sequence <> COALESCE(
        (SELECT MAX(e.lifecycle_sequence) FROM omnivia_chat_tool_lifecycle_events e
         WHERE e.workspace_id = NEW.workspace_id AND e.tool_call_id = NEW.tool_call_id), 0) + 1;
    SELECT RAISE(ABORT, 'omnivia: chat tool lifecycle transition is invalid')
    WHERE COALESCE(
            (SELECT e.state FROM omnivia_chat_tool_lifecycle_events e
             WHERE e.workspace_id = NEW.workspace_id AND e.tool_call_id = NEW.tool_call_id
               AND e.lifecycle_sequence = NEW.lifecycle_sequence - 1), '') || '>' || NEW.state
        NOT IN (
            '>prepared',
            'prepared>policy_checking', 'prepared>interrupted', 'prepared>cancelled',
            'policy_checking>awaiting_review', 'policy_checking>running',
            'policy_checking>failed', 'policy_checking>cancelled',
            'policy_checking>interrupted',
            'awaiting_review>running', 'awaiting_review>cancelled',
            'interrupted>policy_checking', 'interrupted>cancelled',
            'running>succeeded', 'running>failed', 'running>cancelled',
            'running>outcome_unknown'
        );
    SELECT RAISE(ABORT, 'omnivia: awaiting_review requires an open Review wait for this tool call')
    WHERE NEW.state = 'awaiting_review'
      AND NOT EXISTS (
        SELECT 1 FROM omnivia_chat_wait_interactions w
        WHERE w.workspace_id = NEW.workspace_id AND w.wait_id = NEW.wait_id
          AND w.tool_call_id = NEW.tool_call_id AND w.state = 'asked'
          AND w.kind IN ('approval', 'policy_pause')
          AND w.expires_at_us IS NOT NULL
          AND w.target_action_digest IS NOT NULL
      );
    SELECT RAISE(ABORT, 'omnivia: running requires approved post-policy arguments')
    WHERE NEW.state = 'running'
      AND NOT EXISTS (
        SELECT 1 FROM omnivia_chat_tool_calls c
        WHERE c.workspace_id = NEW.workspace_id AND c.tool_call_id = NEW.tool_call_id
          AND c.post_policy_arguments_digest IS NOT NULL
      );
    SELECT RAISE(ABORT, 'omnivia: running after policy requires an approved policy state')
    WHERE NEW.state = 'running'
      AND (SELECT e.state FROM omnivia_chat_tool_lifecycle_events e
           WHERE e.workspace_id = NEW.workspace_id AND e.tool_call_id = NEW.tool_call_id
             AND e.lifecycle_sequence = NEW.lifecycle_sequence - 1) = 'policy_checking'
      AND NOT EXISTS (
        SELECT 1 FROM omnivia_chat_tool_calls c
        WHERE c.workspace_id = NEW.workspace_id AND c.tool_call_id = NEW.tool_call_id
          AND c.policy_state = 'approved'
      );
    SELECT RAISE(ABORT, 'omnivia: running after Review requires an approving decision command')
    WHERE NEW.state = 'running'
      AND (SELECT e.state FROM omnivia_chat_tool_lifecycle_events e
           WHERE e.workspace_id = NEW.workspace_id AND e.tool_call_id = NEW.tool_call_id
             AND e.lifecycle_sequence = NEW.lifecycle_sequence - 1) = 'awaiting_review'
      AND NOT EXISTS (
        SELECT 1
        FROM omnivia_chat_wait_decisions d
        JOIN omnivia_chat_wait_interactions w
          ON w.workspace_id = d.workspace_id AND w.wait_id = d.wait_id
        JOIN omnivia_chat_tool_calls c
          ON c.workspace_id = w.workspace_id AND c.tool_call_id = w.tool_call_id
        WHERE d.workspace_id = NEW.workspace_id AND d.wait_id = NEW.wait_id
          AND d.command_kind = 'review_decision' AND d.outcome = 'approved'
          AND w.tool_call_id = NEW.tool_call_id
          AND w.target_action_digest = c.post_policy_arguments_digest
      );
    SELECT RAISE(ABORT, 'omnivia: succeeded must cite its own tool call result')
    WHERE NEW.state = 'succeeded'
      AND NOT EXISTS (
        SELECT 1 FROM omnivia_chat_tool_results r
        WHERE r.workspace_id = NEW.workspace_id AND r.result_id = NEW.result_id
          AND r.tool_call_id = NEW.tool_call_id
      );
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_chat_tool_lifecycle_events_update
BEFORE UPDATE ON omnivia_chat_tool_lifecycle_events
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_chat_tool_lifecycle_events is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_chat_tool_lifecycle_events_delete
BEFORE DELETE ON omnivia_chat_tool_lifecycle_events
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_chat_tool_lifecycle_events forbids DELETE');
END;
