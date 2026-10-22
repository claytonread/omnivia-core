-- Chat model switching: a durable per-job Provider route (migration 0074).
--
-- Core took one provider connection and model from the executor's startup configuration, so a model
-- switch meant a restart. The trusted host now states a route (`providerRoute`) on each generation-
-- producing command, and Core snapshots it where the work is born so queued work, retries, direction
-- continuations and crash recovery keep the route chosen for that job while later jobs may differ.
--
-- Two nullable columns on `omnivia_chat_queued_submissions` (a route pinned at enqueue time, which a
-- later drain must honour) and on `omnivia_chat_generation_jobs` (the route the executor runs). Both
-- columns are NULL together on every pre-0074 row, which keeps meaning "use the executor's startup
-- configuration". The pair is all-or-none by CHECK, and each member holds the contract's own bounds:
-- `ProviderConnectionId` is a WorkspaceScopedId (1..128), `ProviderModelId` is 1..256 provider-safe
-- characters that never contain the `://` URL-scheme marker (0028's `configured_model_id` rule).
--
-- A route is execution authority fixed at insertion. The two UPDATE guards are 0029's text, carried
-- over unaltered, plus one RAISE each that refuses any change to either route column. Comments stay
-- outside the trigger bodies because the migration runner splits SQL statements and the schema
-- fingerprint covers the stored trigger text.

ALTER TABLE omnivia_chat_queued_submissions ADD COLUMN provider_connection_id TEXT
CHECK (provider_connection_id IS NULL OR (typeof(provider_connection_id) = 'text'
       AND length(provider_connection_id) BETWEEN 1 AND 128
       AND provider_connection_id GLOB '[A-Za-z0-9]*'
       AND provider_connection_id NOT GLOB '*[^A-Za-z0-9._:-]*'
       AND instr(provider_connection_id, char(0)) = 0));

ALTER TABLE omnivia_chat_queued_submissions ADD COLUMN provider_model_id TEXT
CHECK ((provider_model_id IS NULL) = (provider_connection_id IS NULL)
       AND (provider_model_id IS NULL OR (typeof(provider_model_id) = 'text'
            AND length(provider_model_id) BETWEEN 1 AND 256
            AND provider_model_id GLOB '[A-Za-z0-9]*'
            AND provider_model_id NOT GLOB '*[^A-Za-z0-9/._:@-]*'
            AND instr(provider_model_id, '://') = 0
            AND instr(provider_model_id, char(0)) = 0)));

ALTER TABLE omnivia_chat_generation_jobs ADD COLUMN provider_connection_id TEXT
CHECK (provider_connection_id IS NULL OR (typeof(provider_connection_id) = 'text'
       AND length(provider_connection_id) BETWEEN 1 AND 128
       AND provider_connection_id GLOB '[A-Za-z0-9]*'
       AND provider_connection_id NOT GLOB '*[^A-Za-z0-9._:-]*'
       AND instr(provider_connection_id, char(0)) = 0));

ALTER TABLE omnivia_chat_generation_jobs ADD COLUMN provider_model_id TEXT
CHECK ((provider_model_id IS NULL) = (provider_connection_id IS NULL)
       AND (provider_model_id IS NULL OR (typeof(provider_model_id) = 'text'
            AND length(provider_model_id) BETWEEN 1 AND 256
            AND provider_model_id GLOB '[A-Za-z0-9]*'
            AND provider_model_id NOT GLOB '*[^A-Za-z0-9/._:@-]*'
            AND instr(provider_model_id, '://') = 0
            AND instr(provider_model_id, char(0)) = 0)));

DROP TRIGGER omnivia_guard_chat_queued_submissions_update;

CREATE TRIGGER omnivia_guard_chat_queued_submissions_update
BEFORE UPDATE ON omnivia_chat_queued_submissions
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded UPDATE on omnivia_chat_queued_submissions')
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
       OR NEW.workspace_id <> OLD.workspace_id;
    SELECT RAISE(ABORT, 'omnivia: queued submission identity and order are immutable')
    WHERE NEW.workspace_id <> OLD.workspace_id
       OR NEW.conversation_id <> OLD.conversation_id
       OR NEW.actor_id <> OLD.actor_id
       OR NEW.queued_submission_id <> OLD.queued_submission_id
       OR NEW.queue_sequence <> OLD.queue_sequence
       OR NEW.branch_id <> OLD.branch_id
       OR NEW.idempotency_key <> OLD.idempotency_key
       OR NEW.created_at_us <> OLD.created_at_us;
    SELECT RAISE(ABORT, 'omnivia: queued submission provider route is immutable')
    WHERE NEW.provider_connection_id IS NOT OLD.provider_connection_id
       OR NEW.provider_model_id IS NOT OLD.provider_model_id;
    SELECT RAISE(ABORT, 'omnivia: queued submission version must advance by one')
    WHERE NEW.version <> OLD.version + 1;
    SELECT RAISE(ABORT, 'omnivia: queued submission transition is invalid')
    WHERE NOT ((OLD.state = 'queued'
                AND NEW.state IN ('queued', 'claimed', 'cancelled', 'failed'))
            OR (OLD.state = 'claimed'
                AND NEW.state IN ('claimed', 'submitted', 'cancelled', 'failed'))
            OR (OLD.state IN ('submitted', 'cancelled', 'failed')
                AND NEW.state = OLD.state));
END;

DROP TRIGGER omnivia_guard_chat_generation_jobs_update;

CREATE TRIGGER omnivia_guard_chat_generation_jobs_update
BEFORE UPDATE ON omnivia_chat_generation_jobs
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded UPDATE on omnivia_chat_generation_jobs')
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
       OR NEW.workspace_id <> OLD.workspace_id;
    SELECT RAISE(ABORT, 'omnivia: generation job identity and trigger are immutable')
    WHERE NEW.workspace_id <> OLD.workspace_id
       OR NEW.conversation_id <> OLD.conversation_id
       OR NEW.branch_id <> OLD.branch_id
       OR NEW.trigger_message_id <> OLD.trigger_message_id
       OR NEW.generation_job_id <> OLD.generation_job_id
       OR NEW.graph_revision_observed <> OLD.graph_revision_observed
       OR NEW.idempotency_key <> OLD.idempotency_key
       OR NEW.schema_version <> OLD.schema_version
       OR NEW.created_at_us <> OLD.created_at_us;
    SELECT RAISE(ABORT, 'omnivia: generation job provider route is immutable')
    WHERE NEW.provider_connection_id IS NOT OLD.provider_connection_id
       OR NEW.provider_model_id IS NOT OLD.provider_model_id;
    SELECT RAISE(ABORT, 'omnivia: generation job transition is invalid')
    WHERE NOT ((OLD.state = 'queued'
                AND NEW.state IN ('queued', 'running', 'cancelled', 'failed'))
            OR (OLD.state = 'running'
                AND NEW.state IN ('running', 'succeeded', 'failed', 'cancelled'))
            OR (OLD.state IN ('succeeded', 'failed', 'cancelled')
                AND NEW.state = OLD.state));
END;
