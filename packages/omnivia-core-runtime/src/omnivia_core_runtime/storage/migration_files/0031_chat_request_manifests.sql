-- Chat request manifests.
--
-- Additive only. A request manifest is the durable, privacy-preserving account
-- of the final post-policy ProviderInvocationRequest one Chat Generation Attempt
-- was authorised to submit. It stores stable identifiers, content digests,
-- policy references and redacted route/options metadata; it does not store
-- credentials, arbitrary headers, endpoint URLs, SDK payloads, raw rendered
-- prompt blobs or hidden reasoning.

CREATE TABLE IF NOT EXISTS omnivia_chat_request_manifests (
    workspace_id                    TEXT    NOT NULL,
    conversation_id                 TEXT    NOT NULL,
    branch_id                       TEXT    NOT NULL,
    generation_job_id               TEXT    NOT NULL,
    generation_attempt_id           TEXT    NOT NULL,
    trigger_message_id              TEXT    NOT NULL,
    provider_invocation_id          TEXT    NOT NULL,
    request_manifest_id             TEXT    NOT NULL,
    idempotency_key                 TEXT    NOT NULL,
    schema_version                  INTEGER NOT NULL,
    manifest_digest                 TEXT    NOT NULL,
    request_manifest_body_json      TEXT    NOT NULL,
    created_at_us                   INTEGER NOT NULL,

    PRIMARY KEY (workspace_id, generation_attempt_id),
    UNIQUE (workspace_id, request_manifest_id),
    UNIQUE (workspace_id, generation_job_id, generation_attempt_id),
    UNIQUE (workspace_id, provider_invocation_id),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(conversation_id) = 'text'
           AND length(conversation_id) BETWEEN 1 AND 128
           AND conversation_id GLOB '[A-Za-z0-9]*'
           AND conversation_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(conversation_id, char(0)) = 0),
    CHECK (typeof(branch_id) = 'text'
           AND length(branch_id) BETWEEN 1 AND 128
           AND branch_id GLOB '[A-Za-z0-9]*'
           AND branch_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(branch_id, char(0)) = 0),
    CHECK (typeof(generation_job_id) = 'text'
           AND length(generation_job_id) BETWEEN 1 AND 128
           AND generation_job_id GLOB '[A-Za-z0-9]*'
           AND generation_job_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(generation_job_id, char(0)) = 0),
    CHECK (typeof(generation_attempt_id) = 'text'
           AND length(generation_attempt_id) BETWEEN 1 AND 128
           AND generation_attempt_id GLOB '[A-Za-z0-9]*'
           AND generation_attempt_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(generation_attempt_id, char(0)) = 0),
    CHECK (typeof(trigger_message_id) = 'text'
           AND length(trigger_message_id) BETWEEN 1 AND 128
           AND trigger_message_id GLOB '[A-Za-z0-9]*'
           AND trigger_message_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(trigger_message_id, char(0)) = 0),
    CHECK (typeof(provider_invocation_id) = 'text'
           AND length(provider_invocation_id) BETWEEN 1 AND 128
           AND provider_invocation_id GLOB '[A-Za-z0-9]*'
           AND provider_invocation_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(provider_invocation_id, char(0)) = 0),
    CHECK (typeof(request_manifest_id) = 'text'
           AND length(request_manifest_id) BETWEEN 1 AND 128
           AND request_manifest_id GLOB '[A-Za-z0-9]*'
           AND request_manifest_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(request_manifest_id, char(0)) = 0),
    CHECK (typeof(idempotency_key) = 'text'
           AND length(idempotency_key) BETWEEN 1 AND 128
           AND instr(idempotency_key, char(0)) = 0),
    CHECK (typeof(schema_version) = 'integer' AND schema_version = 1),
    CHECK (typeof(manifest_digest) = 'text' AND length(manifest_digest) = 71
           AND substr(manifest_digest, 1, 7) = 'sha256:'
           AND substr(manifest_digest, 8) NOT GLOB '*[^0-9a-f]*'),
    CHECK (typeof(request_manifest_body_json) = 'text'
           AND length(CAST(request_manifest_body_json AS BLOB)) BETWEEN 2 AND 1048576
           AND instr(request_manifest_body_json, char(0)) = 0),
    CHECK (typeof(created_at_us) = 'integer' AND created_at_us > 0),

    FOREIGN KEY (workspace_id, conversation_id, generation_job_id)
        REFERENCES omnivia_chat_generation_jobs
            (workspace_id, conversation_id, generation_job_id),
    FOREIGN KEY (workspace_id, generation_job_id, generation_attempt_id)
        REFERENCES omnivia_chat_generation_attempts
            (workspace_id, generation_job_id, generation_attempt_id),
    FOREIGN KEY (workspace_id, conversation_id, branch_id)
        REFERENCES omnivia_chat_message_branches
            (workspace_id, conversation_id, branch_id),
    FOREIGN KEY (workspace_id, conversation_id, trigger_message_id)
        REFERENCES omnivia_chat_messages (workspace_id, conversation_id, message_id)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS omnivia_idx_chat_request_manifests_job
    ON omnivia_chat_request_manifests
        (workspace_id, generation_job_id, generation_attempt_id);

CREATE INDEX IF NOT EXISTS omnivia_idx_chat_request_manifests_conversation
    ON omnivia_chat_request_manifests
        (workspace_id, conversation_id, branch_id, created_at_us);

CREATE TRIGGER IF NOT EXISTS omnivia_guard_chat_request_manifests_insert
BEFORE INSERT ON omnivia_chat_request_manifests
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_chat_request_manifests')
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

CREATE TRIGGER IF NOT EXISTS omnivia_guard_chat_request_manifests_update
BEFORE UPDATE ON omnivia_chat_request_manifests
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_chat_request_manifests is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_chat_request_manifests_delete
BEFORE DELETE ON omnivia_chat_request_manifests
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_chat_request_manifests forbids DELETE');
END;
