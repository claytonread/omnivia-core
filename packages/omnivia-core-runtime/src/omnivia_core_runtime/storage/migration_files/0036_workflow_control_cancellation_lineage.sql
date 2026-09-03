-- T-0693: the cancellation lineage a `workflow.control` cancel actually leaves behind.
--
-- Additive only in effect: no table, index or column is created, altered or dropped,
-- and this migration performs no DML. It replaces exactly one trigger --
-- `omnivia_guard_job_terminal_observations_insert`, last defined by migration 0015 --
-- and restates every one of that definition's nine refusals unchanged but one, which
-- is widened. Migrations 0015 and 0023 through 0035 remain byte-immutable.
--
-- The one refusal that changes
-- ---------------------------
--
-- 0015 admits a `cancelled` terminal observation only behind an accepted `job.cancel`
-- control. That was the whole of the cancellation vocabulary when it was written: the
-- durable job lane cancelled a job, and `job.cancel` was the operation that did it.
--
-- `workflow.control` is a second public authority over the same durable job, reached
-- through the canonical Runtime run the job carries. Cancelling a Workflow Run settles
-- the run and its job in one fenced transaction, and it does so through migration
-- 0025's stop ledger -- a stop *request* naming the run, and an *accepted* outcome
-- naming the `cancelled` runtime event that closed it. Under 0015 that transaction
-- could not write its own terminal observation, so the two histories had to be left
-- disagreeing: a terminal run beside a queued or claimed job, which RT-109 reads as
-- contradictory history and which leaves dead queued work visible forever.
--
-- The alternative the lane needed is not a forged `job.cancel`. Writing one would state
-- that an operation nobody invoked cancelled the job, and 0015's own audit rule would
-- then have to be satisfied by an audit event for an operation that never ran. What it
-- needed is for the schema to recognise the lineage the cancellation genuinely has --
-- and to recognise nothing else. This second branch is not a looser one: it admits an
-- observation only against the exact evidence the one `workflow.control` mutation that
-- could have written it leaves behind, and no other stop, audit or claim in the
-- workspace satisfies it. All of the following, together:
--
--   * the observation's own job carries a canonical Runtime run of this workspace, and
--     that run is a `workflow` run -- the only kind `workflow.control` serves;
--   * that run holds a stop request in 0025's ledger whose outcome is `accepted`, which
--     0025's own trigger will only admit when it names the run's `cancelled` runtime
--     event;
--   * the request and its outcome are one settlement rather than two rows that happen
--     to share an identifier: same workspace, same `stop_request_id`, same `audit_ref`,
--     same `reason`, and the same instant;
--   * that `stop_request_id` is the `claim_id` of an idempotency claim of this
--     workspace for `workflow.control`, carrying the same `audit_ref` and the principal
--     the stop was requested by -- which is what makes the stop this mutation's own
--     rather than a stop identifier reused from anywhere else;
--   * the audit event both rows name is a `workflow.control` event for `workflow_control`
--     in this workspace, by that same principal, recorded as `succeeded` with no error
--     code -- a refused or failed request accounts for no cancellation; and
--   * the instants agree exactly. `requested_at_us`, the outcome's `completed_at_us`,
--     the audit's `recorded_at_us` and this observation's `finished_at_us` are one
--     value, and `cancellation_reason` is the reason the stop was requested for, so
--     the observation records the cancellation that happened rather than merely one
--     that happened earlier.
--
-- Every clause is required, and the exactness is where the safety is: an accepted stop
-- with no `workflow.control` audit, a `workflow.control` audit with no accepted stop, a
-- stop against another workspace's or another job's run, a stop with no idempotency
-- claim or one claimed for another operation or principal, an audit recorded under
-- another purpose or as a refusal, a `rejected` or `ignored_already_terminal` outcome,
-- and any stop whose instants are not this observation's own -- an earlier settled stop
-- for the same run included -- are all refused exactly as an unrelated `job.cancel`
-- control always was. The observation-number bound is kept alongside the equality
-- because it bounds a different row, the previous terminal observation. The legacy
-- branch is restated verbatim, so a `job.cancel` cancellation is admitted on exactly
-- the terms it was before.
--
-- Nothing else here is new. The contiguity, time-ordering, scheduler/final-event
-- agreement, attempt-history, post-recovery, success-result-kind and repeated-failure
-- refusals are copied from 0015 unchanged, and the guard preamble -- service writer,
-- fenced mutation guard bound to the workspace lease, matching fencing generation,
-- `service_committed` provenance and this workspace's own id -- is copied with it.

DROP TRIGGER omnivia_guard_job_terminal_observations_insert;

CREATE TRIGGER omnivia_guard_job_terminal_observations_insert
BEFORE INSERT ON omnivia_job_terminal_observations
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_job_terminal_observations')
    WHERE omnivia_service_writer() IS NOT 1
       OR NEW.provenance_kind <> 'service_committed'
       OR NOT EXISTS (
            SELECT 1 FROM omnivia_mutation_guard g
            JOIN omnivia_workspace_state s ON s.singleton = 1
            JOIN omnivia_workspace_lease l ON l.singleton = 1
            WHERE g.singleton = 1 AND g.fencing_generation = s.fencing_generation
              AND g.workspace_id = s.workspace_id
              AND l.fencing_generation = g.fencing_generation
              AND l.workspace_id = g.workspace_id
              AND l.service_instance_id = g.service_instance_id
              AND l.lifecycle IN ('acquiring', 'held', 'draining')
              AND NEW.fencing_generation = g.fencing_generation
       )
       OR NEW.workspace_id IS NOT (
            SELECT workspace_id FROM omnivia_workspace_state WHERE singleton = 1
       );
    SELECT RAISE(ABORT, 'omnivia: terminal observation number must be contiguous')
    WHERE NEW.terminal_observation_number IS NOT (
        SELECT COALESCE(MAX(terminal_observation_number), 0) + 1
        FROM omnivia_job_terminal_observations
        WHERE workspace_id = NEW.workspace_id AND job_id = NEW.job_id
    );
    SELECT RAISE(ABORT, 'omnivia: terminal observation time must not regress')
    WHERE NEW.terminal_observation_number > 1
      AND NEW.finished_at_us < (
        SELECT finished_at_us FROM omnivia_job_terminal_observations
        WHERE workspace_id = NEW.workspace_id AND job_id = NEW.job_id
          AND terminal_observation_number = NEW.terminal_observation_number - 1
      );
    SELECT RAISE(ABORT, 'omnivia: terminal observation does not match scheduler and final event')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_durable_jobs j
        JOIN omnivia_job_events e ON e.job_id = j.job_id
        WHERE j.job_id = NEW.job_id AND j.state = NEW.terminal_state
          AND e.workspace_id = NEW.workspace_id AND e.state = NEW.terminal_state
          AND e.occurred_at_us = NEW.finished_at_us
          AND e.sequence = (
              SELECT MAX(e2.sequence) FROM omnivia_job_events e2
              WHERE e2.workspace_id = NEW.workspace_id AND e2.job_id = NEW.job_id
          )
    );
    SELECT RAISE(ABORT, 'omnivia: terminal observation attempt history is inconsistent')
    WHERE (NEW.terminal_state IN ('succeeded', 'failed') AND NOT EXISTS (
            SELECT 1 FROM omnivia_job_attempts a
            WHERE a.workspace_id = NEW.workspace_id AND a.job_id = NEW.job_id
              AND a.attempt_number = NEW.attempt_number
              AND a.attempt_number = (
                  SELECT MAX(a2.attempt_number) FROM omnivia_job_attempts a2
                  WHERE a2.workspace_id = NEW.workspace_id AND a2.job_id = NEW.job_id
              )
              AND a.state = NEW.terminal_state
              AND a.finished_at_us = NEW.finished_at_us
          ))
       OR (NEW.terminal_state = 'cancelled' AND (
            (NOT EXISTS (
                SELECT 1 FROM omnivia_job_attempts a3
                WHERE a3.workspace_id = NEW.workspace_id AND a3.job_id = NEW.job_id
             ) AND NEW.attempt_number IS NOT NULL)
            OR (EXISTS (
                SELECT 1 FROM omnivia_job_attempts a4
                WHERE a4.workspace_id = NEW.workspace_id AND a4.job_id = NEW.job_id
             ) AND NOT EXISTS (
                SELECT 1 FROM omnivia_job_attempts a5
                WHERE a5.workspace_id = NEW.workspace_id AND a5.job_id = NEW.job_id
                  AND a5.attempt_number = NEW.attempt_number
                  AND a5.attempt_number = (
                      SELECT MAX(a6.attempt_number) FROM omnivia_job_attempts a6
                      WHERE a6.workspace_id = NEW.workspace_id AND a6.job_id = NEW.job_id
                  )
                  AND a5.state = 'cancelled'
                  AND a5.finished_at_us = NEW.finished_at_us
             ))
          ));
    SELECT RAISE(ABORT, 'omnivia: terminal observation does not follow accepted recovery')
    WHERE NEW.terminal_observation_number > 1
      AND NOT EXISTS (
        SELECT 1 FROM omnivia_application_job_controls c
        WHERE c.workspace_id = NEW.workspace_id AND c.job_id = NEW.job_id
          AND ((c.operation = 'job.retry'
                AND c.disposition IN ('retry_scheduled', 'resume_scheduled')
                AND c.source_terminal_observation_number = NEW.terminal_observation_number - 1)
               OR (c.control_kind = 'system'
                   AND c.operation = 'system.recovery'
                   AND c.disposition = 'recovery_requeued'))
          AND EXISTS (
              SELECT 1 FROM omnivia_job_events q
              WHERE q.workspace_id = NEW.workspace_id AND q.job_id = NEW.job_id
                AND q.state = 'queued' AND q.occurred_at_us = c.settled_at_us
          )
      );
    SELECT RAISE(ABORT, 'omnivia: cancelled observation has no accepted cancellation control')
    WHERE NEW.terminal_state = 'cancelled'
      AND NOT EXISTS (
        SELECT 1 FROM omnivia_application_job_controls c
        WHERE c.workspace_id = NEW.workspace_id AND c.job_id = NEW.job_id
          AND c.operation = 'job.cancel'
          AND c.disposition = 'cancellation_requested'
          AND c.settled_at_us <= NEW.finished_at_us
          AND (NEW.terminal_observation_number = 1 OR c.settled_at_us >= (
              SELECT o.finished_at_us FROM omnivia_job_terminal_observations o
              WHERE o.workspace_id = NEW.workspace_id AND o.job_id = NEW.job_id
                AND o.terminal_observation_number = NEW.terminal_observation_number - 1
          ))
      )
      AND NOT EXISTS (
        SELECT 1 FROM omnivia_runtime_runs r
        JOIN omnivia_runtime_stop_requests q
          ON q.workspace_id = r.workspace_id AND q.run_id = r.run_id
        JOIN omnivia_runtime_stop_outcomes s
          ON s.workspace_id = q.workspace_id AND s.stop_request_id = q.stop_request_id
        JOIN omnivia_idempotency_claims c
          ON c.claim_id = q.stop_request_id AND c.workspace_id = q.workspace_id
        JOIN omnivia_application_audit_events a
          ON a.audit_ref = q.audit_ref AND a.workspace_id = q.workspace_id
        WHERE r.workspace_id = NEW.workspace_id AND r.job_id = NEW.job_id
          AND r.definition_kind = 'workflow'
          AND s.outcome = 'accepted'
          AND s.audit_ref = q.audit_ref
          AND s.reason = q.reason
          AND c.operation = 'workflow.control'
          AND c.audit_ref = q.audit_ref
          AND c.principal_id = q.requested_by
          AND a.operation = 'workflow.control'
          AND a.purpose = 'workflow_control'
          AND a.principal_id = q.requested_by
          AND a.outcome_class = 'succeeded'
          AND a.error_code IS NULL
          AND a.recorded_at_us = NEW.finished_at_us
          AND q.requested_at_us = NEW.finished_at_us
          AND s.completed_at_us = NEW.finished_at_us
          AND NEW.cancellation_reason = q.reason
          AND (NEW.terminal_observation_number = 1 OR q.requested_at_us >= (
              SELECT o.finished_at_us FROM omnivia_job_terminal_observations o
              WHERE o.workspace_id = NEW.workspace_id AND o.job_id = NEW.job_id
                AND o.terminal_observation_number = NEW.terminal_observation_number - 1
          ))
      );
    SELECT RAISE(ABORT, 'omnivia: terminal success result kind does not match job metadata')
    WHERE NEW.terminal_state = 'succeeded'
      AND EXISTS (
        SELECT 1 FROM omnivia_job_application_metadata m
        WHERE m.workspace_id = NEW.workspace_id AND m.job_id = NEW.job_id
          AND m.terminal_result_kind IS NOT NULL
          AND m.terminal_result_kind <> NEW.result_kind
      );
    SELECT RAISE(ABORT, 'omnivia: terminal failure must repeat the final attempt error')
    WHERE NEW.terminal_state = 'failed'
      AND NOT EXISTS (
        SELECT 1 FROM omnivia_job_attempts a
        WHERE a.workspace_id = NEW.workspace_id AND a.job_id = NEW.job_id
          AND a.attempt_number = NEW.attempt_number
          AND a.error_json = NEW.error_json
      );
END;
