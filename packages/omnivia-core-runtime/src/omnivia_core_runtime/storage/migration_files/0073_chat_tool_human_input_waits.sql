-- Chat C8: admit a human-input wait on a tool-call lifecycle (migration 0073).
--
-- 0071 permitted `awaiting_review` only when the linked open wait was an approval
-- or policy pause carrying the exact action digest. That correctly guarded Review,
-- but made the separately defined `human_input` command impossible to project on a
-- tool call: the lifecycle event is the route by which the authoritative wait reaches
-- `ToolCallProjection.review` and therefore the renderer.
--
-- This migration replaces only that insert trigger. Approval and policy-pause waits
-- retain their action-digest requirement. A human-input wait is admitted only when it
-- is open, linked to this exact tool call, expiring, and carries no action digest. The
-- closed prompt and answer are validated by the service contract; answer bytes remain
-- absent from durable state, which stores only their digest and byte count.
--
-- Comments stay outside the trigger body because the migration runner splits SQL
-- statements and the schema fingerprint covers the stored trigger text.

DROP TRIGGER omnivia_guard_chat_tool_lifecycle_events_insert;

CREATE TRIGGER omnivia_guard_chat_tool_lifecycle_events_insert
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
    SELECT RAISE(ABORT, 'omnivia: awaiting_review requires an open tool interaction wait')
    WHERE NEW.state = 'awaiting_review'
      AND NOT EXISTS (
        SELECT 1 FROM omnivia_chat_wait_interactions w
        WHERE w.workspace_id = NEW.workspace_id AND w.wait_id = NEW.wait_id
          AND w.tool_call_id = NEW.tool_call_id AND w.state = 'asked'
          AND w.expires_at_us IS NOT NULL
          AND ((w.kind IN ('approval', 'policy_pause')
                AND w.target_action_digest IS NOT NULL)
            OR (w.kind = 'human_input' AND w.target_action_digest IS NULL))
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
