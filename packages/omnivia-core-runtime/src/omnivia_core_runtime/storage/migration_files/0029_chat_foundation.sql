-- Durable Chat foundation: Conversations, committed graph records, branches,
-- drafts, queued submissions, generation jobs/events and the transactional Chat
-- outbox.
--
-- Additive only. Thirteen tables, fourteen named indexes and guarded storage
-- triggers. This file stores Chat durable truth and no executable behaviour: no
-- repository, no service, no provider adapter, no renderer state and no Runtime
-- session. It holds no Provider credential, SDK object, endpoint address,
-- arbitrary request header, raw external body or hidden prompt text. Provider
-- invocation truth remains in 0028 and is linked only by stable identifiers.
--
-- Mutability is intentionally narrow. Conversations, branches, actor view state,
-- drafts, queued submissions, generation jobs and outbox delivery rows carry
-- projection/state columns that a later repository may update with fenced
-- compare-and-set operations. Messages, parts, derivations, branch-head events,
-- attempts and generation events are immutable append-only facts. DELETE is
-- forbidden everywhere.

CREATE TABLE IF NOT EXISTS omnivia_chat_conversations (
    workspace_id                 TEXT    NOT NULL,
    conversation_id              TEXT    NOT NULL,
    title                        TEXT,
    title_source                 TEXT,
    state                        TEXT    NOT NULL,
    default_branch_id            TEXT,
    graph_revision               INTEGER NOT NULL,
    latest_conversation_sequence INTEGER NOT NULL,
    schema_version               INTEGER NOT NULL,
    created_by_actor_id          TEXT    NOT NULL,
    created_at_us                INTEGER NOT NULL,
    updated_at_us                INTEGER NOT NULL,
    archived_at_us               INTEGER,
    tombstoned_at_us             INTEGER,

    PRIMARY KEY (workspace_id, conversation_id),
    UNIQUE (workspace_id, conversation_id, graph_revision),
    UNIQUE (workspace_id, conversation_id, latest_conversation_sequence),
    UNIQUE (workspace_id, conversation_id, default_branch_id),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(conversation_id) = 'text'
           AND length(conversation_id) BETWEEN 1 AND 128
           AND conversation_id GLOB '[A-Za-z0-9]*'
           AND conversation_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(conversation_id, char(0)) = 0),
    CHECK (title IS NULL OR (typeof(title) = 'text'
           AND length(CAST(title AS BLOB)) <= 2048
           AND instr(title, char(0)) = 0)),
    CHECK (title_source IS NULL OR title_source IN ('user', 'generated', 'imported')),
    CHECK ((title IS NULL AND title_source IS NULL)
           OR (title IS NOT NULL AND title_source IS NOT NULL)),
    CHECK (state IN ('active', 'archived', 'tombstoned')),
    CHECK (default_branch_id IS NULL
           OR (typeof(default_branch_id) = 'text'
               AND length(default_branch_id) BETWEEN 1 AND 128
               AND default_branch_id GLOB '[A-Za-z0-9]*'
               AND default_branch_id NOT GLOB '*[^A-Za-z0-9._:-]*'
               AND instr(default_branch_id, char(0)) = 0)),
    CHECK (typeof(graph_revision) = 'integer' AND graph_revision >= 0),
    CHECK (typeof(latest_conversation_sequence) = 'integer'
           AND latest_conversation_sequence >= 0),
    CHECK (typeof(schema_version) = 'integer' AND schema_version = 1),
    CHECK (typeof(created_by_actor_id) = 'text'
           AND length(created_by_actor_id) BETWEEN 1 AND 128
           AND created_by_actor_id GLOB '[A-Za-z0-9]*'
           AND created_by_actor_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(created_by_actor_id, char(0)) = 0),
    CHECK (typeof(created_at_us) = 'integer' AND created_at_us > 0),
    CHECK (typeof(updated_at_us) = 'integer' AND updated_at_us >= created_at_us),
    CHECK (archived_at_us IS NULL OR (typeof(archived_at_us) = 'integer'
           AND archived_at_us >= created_at_us)),
    CHECK (tombstoned_at_us IS NULL OR (typeof(tombstoned_at_us) = 'integer'
           AND tombstoned_at_us >= created_at_us)),
    CHECK ((state = 'active' AND archived_at_us IS NULL AND tombstoned_at_us IS NULL)
           OR (state = 'archived' AND archived_at_us IS NOT NULL AND tombstoned_at_us IS NULL)
           OR (state = 'tombstoned' AND tombstoned_at_us IS NOT NULL))
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_chat_messages (
    workspace_id            TEXT    NOT NULL,
    conversation_id         TEXT    NOT NULL,
    message_id              TEXT    NOT NULL,
    parent_message_id       TEXT,
    role                    TEXT    NOT NULL,
    author_type             TEXT    NOT NULL,
    author_id               TEXT,
    conversation_sequence   INTEGER NOT NULL,
    schema_version          INTEGER NOT NULL,
    content_hash            TEXT    NOT NULL,
    completion_status       TEXT    NOT NULL,
    visibility              TEXT    NOT NULL,
    created_on_branch_id    TEXT,
    generation_job_id       TEXT,
    created_at_us           INTEGER NOT NULL,
    committed_at_us         INTEGER NOT NULL,
    tombstoned_at_us        INTEGER,

    PRIMARY KEY (workspace_id, message_id),
    UNIQUE (workspace_id, conversation_id, conversation_sequence),
    UNIQUE (workspace_id, conversation_id, message_id),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(conversation_id) = 'text'
           AND length(conversation_id) BETWEEN 1 AND 128
           AND conversation_id GLOB '[A-Za-z0-9]*'
           AND conversation_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(conversation_id, char(0)) = 0),
    CHECK (typeof(message_id) = 'text' AND length(message_id) BETWEEN 1 AND 128
           AND message_id GLOB '[A-Za-z0-9]*'
           AND message_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(message_id, char(0)) = 0),
    CHECK (parent_message_id IS NULL
           OR (typeof(parent_message_id) = 'text'
               AND length(parent_message_id) BETWEEN 1 AND 128
               AND parent_message_id GLOB '[A-Za-z0-9]*'
               AND parent_message_id NOT GLOB '*[^A-Za-z0-9._:-]*'
               AND instr(parent_message_id, char(0)) = 0)),
    CHECK (role IN ('user', 'assistant', 'system', 'tool')),
    CHECK (author_type IN ('human', 'service', 'provider', 'system')),
    CHECK ((role = 'assistant' AND author_type = 'provider')
           OR (role = 'user' AND author_type IN ('human', 'service'))
           OR (role = 'system' AND author_type = 'system')
           OR (role = 'tool' AND author_type IN ('service', 'system'))),
    CHECK (author_id IS NULL
           OR (typeof(author_id) = 'text'
               AND length(author_id) BETWEEN 1 AND 128
               AND author_id GLOB '[A-Za-z0-9]*'
               AND author_id NOT GLOB '*[^A-Za-z0-9._:-]*'
               AND instr(author_id, char(0)) = 0)),
    CHECK (typeof(conversation_sequence) = 'integer'
           AND conversation_sequence BETWEEN 1 AND 1000000000),
    CHECK (typeof(schema_version) = 'integer' AND schema_version = 1),
    CHECK (typeof(content_hash) = 'text' AND length(content_hash) = 71
           AND substr(content_hash, 1, 7) = 'sha256:'
           AND substr(content_hash, 8) NOT GLOB '*[^0-9a-f]*'),
    CHECK (completion_status IN ('complete', 'interrupted', 'failed_partial')),
    CHECK (visibility IN ('standard', 'user_only', 'internal')),
    CHECK (created_on_branch_id IS NULL
           OR (typeof(created_on_branch_id) = 'text'
               AND length(created_on_branch_id) BETWEEN 1 AND 128
               AND created_on_branch_id GLOB '[A-Za-z0-9]*'
               AND created_on_branch_id NOT GLOB '*[^A-Za-z0-9._:-]*'
               AND instr(created_on_branch_id, char(0)) = 0)),
    CHECK (generation_job_id IS NULL
           OR (typeof(generation_job_id) = 'text'
               AND length(generation_job_id) BETWEEN 1 AND 128
               AND generation_job_id GLOB '[A-Za-z0-9]*'
               AND generation_job_id NOT GLOB '*[^A-Za-z0-9._:-]*'
               AND instr(generation_job_id, char(0)) = 0)),
    CHECK (typeof(created_at_us) = 'integer' AND created_at_us > 0),
    CHECK (typeof(committed_at_us) = 'integer' AND committed_at_us >= created_at_us),
    CHECK (tombstoned_at_us IS NULL OR (typeof(tombstoned_at_us) = 'integer'
           AND tombstoned_at_us >= committed_at_us)),

    FOREIGN KEY (workspace_id, conversation_id)
        REFERENCES omnivia_chat_conversations (workspace_id, conversation_id),
    FOREIGN KEY (workspace_id, conversation_id, parent_message_id)
        REFERENCES omnivia_chat_messages (workspace_id, conversation_id, message_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_chat_message_parts (
    workspace_id     TEXT    NOT NULL,
    conversation_id  TEXT    NOT NULL,
    message_id       TEXT    NOT NULL,
    part_id          TEXT    NOT NULL,
    part_index       INTEGER NOT NULL,
    part_type        TEXT    NOT NULL,
    schema_version   INTEGER NOT NULL,
    visibility       TEXT    NOT NULL,
    payload_json     TEXT    NOT NULL,
    provenance       TEXT,
    content_hash     TEXT    NOT NULL,
    created_at_us    INTEGER NOT NULL,

    PRIMARY KEY (workspace_id, part_id),
    UNIQUE (workspace_id, message_id, part_index),
    UNIQUE (workspace_id, message_id, part_id),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(conversation_id) = 'text'
           AND length(conversation_id) BETWEEN 1 AND 128
           AND conversation_id GLOB '[A-Za-z0-9]*'
           AND conversation_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(conversation_id, char(0)) = 0),
    CHECK (typeof(message_id) = 'text' AND length(message_id) BETWEEN 1 AND 128
           AND message_id GLOB '[A-Za-z0-9]*'
           AND message_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(message_id, char(0)) = 0),
    CHECK (typeof(part_id) = 'text' AND length(part_id) BETWEEN 1 AND 128
           AND part_id GLOB '[A-Za-z0-9]*'
           AND part_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(part_id, char(0)) = 0),
    CHECK (typeof(part_index) = 'integer' AND part_index BETWEEN 0 AND 4095),
    CHECK (typeof(part_type) = 'text' AND length(part_type) BETWEEN 1 AND 128
           AND part_type GLOB '[A-Za-z]*'
           AND part_type NOT GLOB '*[^A-Za-z0-9._-]*'
           AND instr(part_type, char(0)) = 0),
    CHECK (typeof(schema_version) = 'integer' AND schema_version = 1),
    CHECK (visibility IN ('standard', 'user_only', 'internal')),
    CHECK (typeof(payload_json) = 'text'
           AND length(CAST(payload_json AS BLOB)) BETWEEN 2 AND 262144
           AND instr(payload_json, char(0)) = 0),
    CHECK (provenance IS NULL
           OR provenance IN ('human', 'ai', 'import', 'migration', 'system')),
    CHECK (typeof(content_hash) = 'text' AND length(content_hash) = 71
           AND substr(content_hash, 1, 7) = 'sha256:'
           AND substr(content_hash, 8) NOT GLOB '*[^0-9a-f]*'),
    CHECK (typeof(created_at_us) = 'integer' AND created_at_us > 0),

    FOREIGN KEY (workspace_id, conversation_id, message_id)
        REFERENCES omnivia_chat_messages (workspace_id, conversation_id, message_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_chat_message_derivations (
    workspace_id        TEXT    NOT NULL,
    conversation_id     TEXT    NOT NULL,
    source_message_id   TEXT    NOT NULL,
    derived_message_id  TEXT    NOT NULL,
    derivation_kind     TEXT    NOT NULL,
    created_by_actor_id TEXT    NOT NULL,
    created_at_us       INTEGER NOT NULL,
    metadata_json       TEXT,

    PRIMARY KEY (workspace_id, source_message_id, derived_message_id, derivation_kind),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(conversation_id) = 'text'
           AND length(conversation_id) BETWEEN 1 AND 128
           AND conversation_id GLOB '[A-Za-z0-9]*'
           AND conversation_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(conversation_id, char(0)) = 0),
    CHECK (typeof(source_message_id) = 'text'
           AND length(source_message_id) BETWEEN 1 AND 128
           AND source_message_id GLOB '[A-Za-z0-9]*'
           AND source_message_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(source_message_id, char(0)) = 0),
    CHECK (typeof(derived_message_id) = 'text'
           AND length(derived_message_id) BETWEEN 1 AND 128
           AND derived_message_id GLOB '[A-Za-z0-9]*'
           AND derived_message_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(derived_message_id, char(0)) = 0),
    CHECK (source_message_id <> derived_message_id),
    CHECK (derivation_kind IN ('amendment', 'regeneration', 'reuse',
                               'imported_revision')),
    CHECK (typeof(created_by_actor_id) = 'text'
           AND length(created_by_actor_id) BETWEEN 1 AND 128
           AND created_by_actor_id GLOB '[A-Za-z0-9]*'
           AND created_by_actor_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(created_by_actor_id, char(0)) = 0),
    CHECK (typeof(created_at_us) = 'integer' AND created_at_us > 0),
    CHECK (metadata_json IS NULL OR (typeof(metadata_json) = 'text'
           AND length(CAST(metadata_json AS BLOB)) BETWEEN 2 AND 65536
           AND instr(metadata_json, char(0)) = 0)),

    FOREIGN KEY (workspace_id, conversation_id, source_message_id)
        REFERENCES omnivia_chat_messages (workspace_id, conversation_id, message_id),
    FOREIGN KEY (workspace_id, conversation_id, derived_message_id)
        REFERENCES omnivia_chat_messages (workspace_id, conversation_id, message_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_chat_message_branches (
    workspace_id                   TEXT    NOT NULL,
    conversation_id                TEXT    NOT NULL,
    branch_id                      TEXT    NOT NULL,
    origin_kind                    TEXT    NOT NULL,
    created_from_branch_id         TEXT,
    fork_parent_message_id         TEXT,
    fork_source_message_id         TEXT,
    initial_head_message_id        TEXT    NOT NULL,
    current_head_message_id        TEXT    NOT NULL,
    created_by_actor_id            TEXT    NOT NULL,
    created_at_us                  INTEGER NOT NULL,
    created_conversation_sequence  INTEGER NOT NULL,
    head_version                   INTEGER NOT NULL,
    schema_version                 INTEGER NOT NULL,
    state                          TEXT    NOT NULL,
    archived_at_us                 INTEGER,
    tombstoned_at_us               INTEGER,

    PRIMARY KEY (workspace_id, branch_id),
    UNIQUE (workspace_id, conversation_id, branch_id),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(conversation_id) = 'text'
           AND length(conversation_id) BETWEEN 1 AND 128
           AND conversation_id GLOB '[A-Za-z0-9]*'
           AND conversation_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(conversation_id, char(0)) = 0),
    CHECK (typeof(branch_id) = 'text' AND length(branch_id) BETWEEN 1 AND 128
           AND branch_id GLOB '[A-Za-z0-9]*'
           AND branch_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(branch_id, char(0)) = 0),
    CHECK (origin_kind IN ('original', 'message_amendment',
                           'assistant_regeneration', 'explicit_fork', 'import')),
    CHECK (created_from_branch_id IS NULL OR (typeof(created_from_branch_id) = 'text'
           AND length(created_from_branch_id) BETWEEN 1 AND 128
           AND created_from_branch_id GLOB '[A-Za-z0-9]*'
           AND created_from_branch_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(created_from_branch_id, char(0)) = 0)),
    CHECK (fork_parent_message_id IS NULL OR (typeof(fork_parent_message_id) = 'text'
           AND length(fork_parent_message_id) BETWEEN 1 AND 128
           AND fork_parent_message_id GLOB '[A-Za-z0-9]*'
           AND fork_parent_message_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(fork_parent_message_id, char(0)) = 0)),
    CHECK (fork_source_message_id IS NULL OR (typeof(fork_source_message_id) = 'text'
           AND length(fork_source_message_id) BETWEEN 1 AND 128
           AND fork_source_message_id GLOB '[A-Za-z0-9]*'
           AND fork_source_message_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(fork_source_message_id, char(0)) = 0)),
    CHECK (typeof(initial_head_message_id) = 'text'
           AND length(initial_head_message_id) BETWEEN 1 AND 128
           AND initial_head_message_id GLOB '[A-Za-z0-9]*'
           AND initial_head_message_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(initial_head_message_id, char(0)) = 0),
    CHECK (typeof(current_head_message_id) = 'text'
           AND length(current_head_message_id) BETWEEN 1 AND 128
           AND current_head_message_id GLOB '[A-Za-z0-9]*'
           AND current_head_message_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(current_head_message_id, char(0)) = 0),
    CHECK (typeof(created_by_actor_id) = 'text'
           AND length(created_by_actor_id) BETWEEN 1 AND 128
           AND created_by_actor_id GLOB '[A-Za-z0-9]*'
           AND created_by_actor_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(created_by_actor_id, char(0)) = 0),
    CHECK (typeof(created_at_us) = 'integer' AND created_at_us > 0),
    CHECK (typeof(created_conversation_sequence) = 'integer'
           AND created_conversation_sequence >= 1),
    CHECK (typeof(head_version) = 'integer' AND head_version >= 1),
    CHECK (typeof(schema_version) = 'integer' AND schema_version = 1),
    CHECK (state IN ('open', 'archived', 'tombstoned')),
    CHECK (archived_at_us IS NULL OR (typeof(archived_at_us) = 'integer'
           AND archived_at_us >= created_at_us)),
    CHECK (tombstoned_at_us IS NULL OR (typeof(tombstoned_at_us) = 'integer'
           AND tombstoned_at_us >= created_at_us)),
    CHECK ((state = 'open' AND archived_at_us IS NULL AND tombstoned_at_us IS NULL)
           OR (state = 'archived' AND archived_at_us IS NOT NULL AND tombstoned_at_us IS NULL)
           OR (state = 'tombstoned' AND tombstoned_at_us IS NOT NULL)),

    FOREIGN KEY (workspace_id, conversation_id)
        REFERENCES omnivia_chat_conversations (workspace_id, conversation_id),
    FOREIGN KEY (workspace_id, conversation_id, initial_head_message_id)
        REFERENCES omnivia_chat_messages (workspace_id, conversation_id, message_id),
    FOREIGN KEY (workspace_id, conversation_id, current_head_message_id)
        REFERENCES omnivia_chat_messages (workspace_id, conversation_id, message_id),
    FOREIGN KEY (workspace_id, conversation_id, fork_parent_message_id)
        REFERENCES omnivia_chat_messages (workspace_id, conversation_id, message_id),
    FOREIGN KEY (workspace_id, conversation_id, fork_source_message_id)
        REFERENCES omnivia_chat_messages (workspace_id, conversation_id, message_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_chat_branch_head_events (
    workspace_id             TEXT    NOT NULL,
    conversation_id          TEXT    NOT NULL,
    branch_id                TEXT    NOT NULL,
    event_id                 TEXT    NOT NULL,
    head_version             INTEGER NOT NULL,
    previous_head_message_id TEXT,
    new_head_message_id      TEXT    NOT NULL,
    cause                    TEXT    NOT NULL,
    command_id               TEXT    NOT NULL,
    graph_revision           INTEGER NOT NULL,
    conversation_sequence    INTEGER NOT NULL,
    actor_id                 TEXT    NOT NULL,
    occurred_at_us           INTEGER NOT NULL,
    schema_version           INTEGER NOT NULL,

    PRIMARY KEY (workspace_id, branch_id, head_version),
    UNIQUE (workspace_id, event_id),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(conversation_id) = 'text'
           AND length(conversation_id) BETWEEN 1 AND 128
           AND conversation_id GLOB '[A-Za-z0-9]*'
           AND conversation_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(conversation_id, char(0)) = 0),
    CHECK (typeof(branch_id) = 'text' AND length(branch_id) BETWEEN 1 AND 128
           AND branch_id GLOB '[A-Za-z0-9]*'
           AND branch_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(branch_id, char(0)) = 0),
    CHECK (typeof(event_id) = 'text' AND length(event_id) BETWEEN 1 AND 128
           AND event_id GLOB '[A-Za-z0-9]*'
           AND event_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(event_id, char(0)) = 0),
    CHECK (typeof(head_version) = 'integer' AND head_version >= 1),
    CHECK ((head_version = 1 AND previous_head_message_id IS NULL)
           OR (head_version > 1 AND previous_head_message_id IS NOT NULL)),
    CHECK (previous_head_message_id IS NULL
           OR (typeof(previous_head_message_id) = 'text'
               AND length(previous_head_message_id) BETWEEN 1 AND 128
               AND previous_head_message_id GLOB '[A-Za-z0-9]*'
               AND previous_head_message_id NOT GLOB '*[^A-Za-z0-9._:-]*'
               AND instr(previous_head_message_id, char(0)) = 0)),
    CHECK (typeof(new_head_message_id) = 'text'
           AND length(new_head_message_id) BETWEEN 1 AND 128
           AND new_head_message_id GLOB '[A-Za-z0-9]*'
           AND new_head_message_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(new_head_message_id, char(0)) = 0),
    CHECK (cause IN ('branch_created', 'user_message_appended',
                     'assistant_message_materialised', 'human_input_appended',
                     'imported', 'recovered_projection')),
    CHECK (typeof(command_id) = 'text' AND length(command_id) BETWEEN 1 AND 128
           AND command_id GLOB '[A-Za-z0-9]*'
           AND command_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(command_id, char(0)) = 0),
    CHECK (typeof(graph_revision) = 'integer' AND graph_revision >= 1),
    CHECK (typeof(conversation_sequence) = 'integer'
           AND conversation_sequence >= 1),
    CHECK (typeof(actor_id) = 'text' AND length(actor_id) BETWEEN 1 AND 128
           AND actor_id GLOB '[A-Za-z0-9]*'
           AND actor_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(actor_id, char(0)) = 0),
    CHECK (typeof(occurred_at_us) = 'integer' AND occurred_at_us > 0),
    CHECK (typeof(schema_version) = 'integer' AND schema_version = 1),

    FOREIGN KEY (workspace_id, conversation_id, branch_id)
        REFERENCES omnivia_chat_message_branches
            (workspace_id, conversation_id, branch_id),
    FOREIGN KEY (workspace_id, conversation_id, previous_head_message_id)
        REFERENCES omnivia_chat_messages (workspace_id, conversation_id, message_id),
    FOREIGN KEY (workspace_id, conversation_id, new_head_message_id)
        REFERENCES omnivia_chat_messages (workspace_id, conversation_id, message_id),
    FOREIGN KEY (workspace_id, conversation_id, graph_revision)
        REFERENCES omnivia_chat_conversations
            (workspace_id, conversation_id, graph_revision),
    FOREIGN KEY (workspace_id, conversation_id, conversation_sequence)
        REFERENCES omnivia_chat_messages
            (workspace_id, conversation_id, conversation_sequence)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_chat_conversation_view_states (
    workspace_id              TEXT    NOT NULL,
    conversation_id           TEXT    NOT NULL,
    actor_id                  TEXT    NOT NULL,
    device_id                 TEXT    NOT NULL DEFAULT '',
    active_branch_id          TEXT    NOT NULL,
    focused_message_id        TEXT,
    last_seen_graph_revision  INTEGER NOT NULL,
    schema_version            INTEGER NOT NULL,
    version                   INTEGER NOT NULL,
    updated_at_us             INTEGER NOT NULL,

    PRIMARY KEY (workspace_id, conversation_id, actor_id, device_id),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(conversation_id) = 'text'
           AND length(conversation_id) BETWEEN 1 AND 128
           AND conversation_id GLOB '[A-Za-z0-9]*'
           AND conversation_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(conversation_id, char(0)) = 0),
    CHECK (typeof(actor_id) = 'text' AND length(actor_id) BETWEEN 1 AND 128
           AND actor_id GLOB '[A-Za-z0-9]*'
           AND actor_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(actor_id, char(0)) = 0),
    CHECK (typeof(device_id) = 'text' AND length(device_id) <= 128
           AND instr(device_id, char(0)) = 0),
    CHECK (typeof(active_branch_id) = 'text'
           AND length(active_branch_id) BETWEEN 1 AND 128
           AND active_branch_id GLOB '[A-Za-z0-9]*'
           AND active_branch_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(active_branch_id, char(0)) = 0),
    CHECK (focused_message_id IS NULL
           OR (typeof(focused_message_id) = 'text'
               AND length(focused_message_id) BETWEEN 1 AND 128
               AND focused_message_id GLOB '[A-Za-z0-9]*'
               AND focused_message_id NOT GLOB '*[^A-Za-z0-9._:-]*'
               AND instr(focused_message_id, char(0)) = 0)),
    CHECK (typeof(last_seen_graph_revision) = 'integer'
           AND last_seen_graph_revision >= 0),
    CHECK (typeof(schema_version) = 'integer' AND schema_version = 1),
    CHECK (typeof(version) = 'integer' AND version >= 1),
    CHECK (typeof(updated_at_us) = 'integer' AND updated_at_us > 0),

    FOREIGN KEY (workspace_id, conversation_id)
        REFERENCES omnivia_chat_conversations (workspace_id, conversation_id),
    FOREIGN KEY (workspace_id, conversation_id, active_branch_id)
        REFERENCES omnivia_chat_message_branches
            (workspace_id, conversation_id, branch_id),
    FOREIGN KEY (workspace_id, conversation_id, focused_message_id)
        REFERENCES omnivia_chat_messages (workspace_id, conversation_id, message_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_chat_drafts (
    workspace_id          TEXT    NOT NULL,
    conversation_id       TEXT    NOT NULL,
    actor_id              TEXT    NOT NULL,
    device_id             TEXT    NOT NULL DEFAULT '',
    draft_id              TEXT    NOT NULL,
    mode                  TEXT    NOT NULL,
    source_message_id     TEXT,
    text_content          TEXT    NOT NULL,
    references_json       TEXT    NOT NULL,
    target_json           TEXT,
    stashed_from_draft_id TEXT,
    schema_version        INTEGER NOT NULL,
    version               INTEGER NOT NULL,
    updated_at_us         INTEGER NOT NULL,
    expires_at_us         INTEGER,

    PRIMARY KEY (workspace_id, draft_id),
    UNIQUE (workspace_id, conversation_id, actor_id, device_id, mode),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(conversation_id) = 'text'
           AND length(conversation_id) BETWEEN 1 AND 128
           AND conversation_id GLOB '[A-Za-z0-9]*'
           AND conversation_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(conversation_id, char(0)) = 0),
    CHECK (typeof(actor_id) = 'text' AND length(actor_id) BETWEEN 1 AND 128
           AND actor_id GLOB '[A-Za-z0-9]*'
           AND actor_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(actor_id, char(0)) = 0),
    CHECK (typeof(device_id) = 'text' AND length(device_id) <= 128
           AND instr(device_id, char(0)) = 0),
    CHECK (typeof(draft_id) = 'text' AND length(draft_id) BETWEEN 1 AND 128
           AND draft_id GLOB '[A-Za-z0-9]*'
           AND draft_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(draft_id, char(0)) = 0),
    CHECK (mode IN ('normal', 'edit_message', 'reuse_message')),
    CHECK ((mode = 'normal' AND source_message_id IS NULL)
           OR (mode IN ('edit_message', 'reuse_message')
               AND source_message_id IS NOT NULL)),
    CHECK (source_message_id IS NULL OR (typeof(source_message_id) = 'text'
           AND length(source_message_id) BETWEEN 1 AND 128
           AND source_message_id GLOB '[A-Za-z0-9]*'
           AND source_message_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(source_message_id, char(0)) = 0)),
    CHECK (typeof(text_content) = 'text'
           AND length(CAST(text_content AS BLOB)) <= 262144
           AND instr(text_content, char(0)) = 0),
    CHECK (typeof(references_json) = 'text'
           AND length(CAST(references_json AS BLOB)) BETWEEN 2 AND 65536
           AND instr(references_json, char(0)) = 0),
    CHECK (target_json IS NULL OR (typeof(target_json) = 'text'
           AND length(CAST(target_json AS BLOB)) BETWEEN 2 AND 4096
           AND instr(target_json, char(0)) = 0)),
    CHECK (stashed_from_draft_id IS NULL
           OR (typeof(stashed_from_draft_id) = 'text'
               AND length(stashed_from_draft_id) BETWEEN 1 AND 128
               AND stashed_from_draft_id GLOB '[A-Za-z0-9]*'
               AND stashed_from_draft_id NOT GLOB '*[^A-Za-z0-9._:-]*'
               AND instr(stashed_from_draft_id, char(0)) = 0)),
    CHECK (typeof(schema_version) = 'integer' AND schema_version = 1),
    CHECK (typeof(version) = 'integer' AND version >= 1),
    CHECK (typeof(updated_at_us) = 'integer' AND updated_at_us > 0),
    CHECK (expires_at_us IS NULL OR (typeof(expires_at_us) = 'integer'
           AND expires_at_us > updated_at_us)),

    FOREIGN KEY (workspace_id, conversation_id)
        REFERENCES omnivia_chat_conversations (workspace_id, conversation_id),
    FOREIGN KEY (workspace_id, conversation_id, source_message_id)
        REFERENCES omnivia_chat_messages (workspace_id, conversation_id, message_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_chat_queued_submissions (
    workspace_id              TEXT    NOT NULL,
    conversation_id           TEXT    NOT NULL,
    actor_id                  TEXT    NOT NULL,
    queued_submission_id      TEXT    NOT NULL,
    queue_sequence            INTEGER NOT NULL,
    branch_id                 TEXT    NOT NULL,
    editable_parts_json       TEXT    NOT NULL,
    references_json           TEXT    NOT NULL,
    idempotency_key           TEXT    NOT NULL,
    state                     TEXT    NOT NULL,
    version                   INTEGER NOT NULL,
    claimed_by                TEXT,
    claim_epoch               INTEGER,
    claim_expires_at_us       INTEGER,
    submitted_message_id      TEXT,
    submitted_generation_job_id TEXT,
    sanitized_error_code      TEXT,
    sanitized_error_detail    TEXT,
    created_at_us             INTEGER NOT NULL,
    updated_at_us             INTEGER NOT NULL,

    PRIMARY KEY (workspace_id, queued_submission_id),
    UNIQUE (workspace_id, idempotency_key),
    UNIQUE (workspace_id, conversation_id, queue_sequence),
    UNIQUE (workspace_id, conversation_id, queued_submission_id),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(conversation_id) = 'text'
           AND length(conversation_id) BETWEEN 1 AND 128
           AND conversation_id GLOB '[A-Za-z0-9]*'
           AND conversation_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(conversation_id, char(0)) = 0),
    CHECK (typeof(actor_id) = 'text' AND length(actor_id) BETWEEN 1 AND 128
           AND actor_id GLOB '[A-Za-z0-9]*'
           AND actor_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(actor_id, char(0)) = 0),
    CHECK (typeof(queued_submission_id) = 'text'
           AND length(queued_submission_id) BETWEEN 1 AND 128
           AND queued_submission_id GLOB '[A-Za-z0-9]*'
           AND queued_submission_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(queued_submission_id, char(0)) = 0),
    CHECK (typeof(queue_sequence) = 'integer'
           AND queue_sequence BETWEEN 1 AND 1000000),
    CHECK (typeof(branch_id) = 'text' AND length(branch_id) BETWEEN 1 AND 128
           AND branch_id GLOB '[A-Za-z0-9]*'
           AND branch_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(branch_id, char(0)) = 0),
    CHECK (typeof(editable_parts_json) = 'text'
           AND length(CAST(editable_parts_json AS BLOB)) BETWEEN 2 AND 262144
           AND instr(editable_parts_json, char(0)) = 0),
    CHECK (typeof(references_json) = 'text'
           AND length(CAST(references_json AS BLOB)) BETWEEN 2 AND 65536
           AND instr(references_json, char(0)) = 0),
    CHECK (typeof(idempotency_key) = 'text'
           AND length(idempotency_key) BETWEEN 1 AND 128
           AND idempotency_key GLOB '[A-Za-z0-9]*'
           AND idempotency_key NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(idempotency_key, char(0)) = 0),
    CHECK (state IN ('queued', 'claimed', 'submitted', 'cancelled', 'failed')),
    CHECK (typeof(version) = 'integer' AND version >= 1),
    CHECK (claimed_by IS NULL OR (typeof(claimed_by) = 'text'
           AND length(claimed_by) BETWEEN 1 AND 128
           AND claimed_by GLOB '[A-Za-z0-9]*'
           AND claimed_by NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(claimed_by, char(0)) = 0)),
    CHECK (claim_epoch IS NULL OR (typeof(claim_epoch) = 'integer'
           AND claim_epoch >= 1)),
    CHECK (claim_expires_at_us IS NULL OR (typeof(claim_expires_at_us) = 'integer'
           AND claim_expires_at_us > updated_at_us)),
    CHECK ((state = 'claimed'
            AND claimed_by IS NOT NULL
            AND claim_epoch IS NOT NULL
            AND claim_expires_at_us IS NOT NULL)
           OR (state <> 'claimed')),
    CHECK ((state = 'submitted'
            AND submitted_message_id IS NOT NULL
            AND submitted_generation_job_id IS NOT NULL)
           OR (state <> 'submitted')),
    CHECK ((state = 'failed'
            AND sanitized_error_code IS NOT NULL)
           OR (state <> 'failed')),
    CHECK (submitted_message_id IS NULL OR (typeof(submitted_message_id) = 'text'
           AND length(submitted_message_id) BETWEEN 1 AND 128
           AND submitted_message_id GLOB '[A-Za-z0-9]*'
           AND submitted_message_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(submitted_message_id, char(0)) = 0)),
    CHECK (submitted_generation_job_id IS NULL
           OR (typeof(submitted_generation_job_id) = 'text'
               AND length(submitted_generation_job_id) BETWEEN 1 AND 128
               AND submitted_generation_job_id GLOB '[A-Za-z0-9]*'
               AND submitted_generation_job_id NOT GLOB '*[^A-Za-z0-9._:-]*'
               AND instr(submitted_generation_job_id, char(0)) = 0)),
    CHECK (sanitized_error_code IS NULL OR (typeof(sanitized_error_code) = 'text'
           AND length(sanitized_error_code) BETWEEN 1 AND 128
           AND sanitized_error_code GLOB '[a-z]*'
           AND sanitized_error_code NOT GLOB '*[^a-z0-9._-]*')),
    CHECK (sanitized_error_detail IS NULL
           OR (typeof(sanitized_error_detail) = 'text'
               AND length(CAST(sanitized_error_detail AS BLOB)) <= 4096
               AND instr(sanitized_error_detail, char(0)) = 0)),
    CHECK (typeof(created_at_us) = 'integer' AND created_at_us > 0),
    CHECK (typeof(updated_at_us) = 'integer' AND updated_at_us >= created_at_us),

    FOREIGN KEY (workspace_id, conversation_id)
        REFERENCES omnivia_chat_conversations (workspace_id, conversation_id),
    FOREIGN KEY (workspace_id, conversation_id, branch_id)
        REFERENCES omnivia_chat_message_branches
            (workspace_id, conversation_id, branch_id),
    FOREIGN KEY (workspace_id, conversation_id, submitted_message_id)
        REFERENCES omnivia_chat_messages (workspace_id, conversation_id, message_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_chat_generation_jobs (
    workspace_id              TEXT    NOT NULL,
    conversation_id           TEXT    NOT NULL,
    branch_id                 TEXT    NOT NULL,
    trigger_message_id        TEXT    NOT NULL,
    generation_job_id         TEXT    NOT NULL,
    state                     TEXT    NOT NULL,
    graph_revision_observed   INTEGER NOT NULL,
    idempotency_key           TEXT    NOT NULL,
    current_attempt_id        TEXT,
    result_message_id         TEXT,
    lease_owner               TEXT,
    lease_epoch               INTEGER NOT NULL,
    lease_expires_at_us       INTEGER,
    heartbeat_at_us           INTEGER,
    last_event_sequence       INTEGER NOT NULL,
    sanitized_error_code      TEXT,
    sanitized_error_detail    TEXT,
    schema_version            INTEGER NOT NULL,
    created_at_us             INTEGER NOT NULL,
    updated_at_us             INTEGER NOT NULL,
    started_at_us             INTEGER,
    finished_at_us            INTEGER,

    PRIMARY KEY (workspace_id, generation_job_id),
    UNIQUE (workspace_id, idempotency_key),
    UNIQUE (workspace_id, conversation_id, generation_job_id),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(conversation_id) = 'text'
           AND length(conversation_id) BETWEEN 1 AND 128
           AND conversation_id GLOB '[A-Za-z0-9]*'
           AND conversation_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(conversation_id, char(0)) = 0),
    CHECK (typeof(branch_id) = 'text' AND length(branch_id) BETWEEN 1 AND 128
           AND branch_id GLOB '[A-Za-z0-9]*'
           AND branch_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(branch_id, char(0)) = 0),
    CHECK (typeof(trigger_message_id) = 'text'
           AND length(trigger_message_id) BETWEEN 1 AND 128
           AND trigger_message_id GLOB '[A-Za-z0-9]*'
           AND trigger_message_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(trigger_message_id, char(0)) = 0),
    CHECK (typeof(generation_job_id) = 'text'
           AND length(generation_job_id) BETWEEN 1 AND 128
           AND generation_job_id GLOB '[A-Za-z0-9]*'
           AND generation_job_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(generation_job_id, char(0)) = 0),
    CHECK (state IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')),
    CHECK (typeof(graph_revision_observed) = 'integer'
           AND graph_revision_observed >= 0),
    CHECK (typeof(idempotency_key) = 'text'
           AND length(idempotency_key) BETWEEN 1 AND 128
           AND idempotency_key GLOB '[A-Za-z0-9]*'
           AND idempotency_key NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(idempotency_key, char(0)) = 0),
    CHECK (current_attempt_id IS NULL OR (typeof(current_attempt_id) = 'text'
           AND length(current_attempt_id) BETWEEN 1 AND 128
           AND current_attempt_id GLOB '[A-Za-z0-9]*'
           AND current_attempt_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(current_attempt_id, char(0)) = 0)),
    CHECK (result_message_id IS NULL OR (typeof(result_message_id) = 'text'
           AND length(result_message_id) BETWEEN 1 AND 128
           AND result_message_id GLOB '[A-Za-z0-9]*'
           AND result_message_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(result_message_id, char(0)) = 0)),
    CHECK ((state = 'succeeded' AND result_message_id IS NOT NULL)
           OR (state <> 'succeeded' AND result_message_id IS NULL)),
    CHECK (lease_owner IS NULL OR (typeof(lease_owner) = 'text'
           AND length(lease_owner) BETWEEN 1 AND 128
           AND lease_owner GLOB '[A-Za-z0-9]*'
           AND lease_owner NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(lease_owner, char(0)) = 0)),
    CHECK (typeof(lease_epoch) = 'integer' AND lease_epoch >= 0),
    CHECK ((state = 'running'
            AND current_attempt_id IS NOT NULL
            AND lease_owner IS NOT NULL
            AND lease_epoch >= 1
            AND lease_expires_at_us IS NOT NULL
            AND heartbeat_at_us IS NOT NULL)
           OR (state <> 'running')),
    CHECK (lease_expires_at_us IS NULL OR (typeof(lease_expires_at_us) = 'integer'
           AND lease_expires_at_us > updated_at_us)),
    CHECK (heartbeat_at_us IS NULL OR (typeof(heartbeat_at_us) = 'integer'
           AND heartbeat_at_us >= created_at_us)),
    CHECK (typeof(last_event_sequence) = 'integer' AND last_event_sequence >= 0),
    CHECK ((state IN ('failed', 'cancelled') AND sanitized_error_code IS NOT NULL)
           OR (state NOT IN ('failed', 'cancelled'))),
    CHECK (sanitized_error_code IS NULL OR (typeof(sanitized_error_code) = 'text'
           AND length(sanitized_error_code) BETWEEN 1 AND 128
           AND sanitized_error_code GLOB '[a-z]*'
           AND sanitized_error_code NOT GLOB '*[^a-z0-9._-]*')),
    CHECK (sanitized_error_detail IS NULL
           OR (typeof(sanitized_error_detail) = 'text'
               AND length(CAST(sanitized_error_detail AS BLOB)) <= 4096
               AND instr(sanitized_error_detail, char(0)) = 0)),
    CHECK (typeof(schema_version) = 'integer' AND schema_version = 1),
    CHECK (typeof(created_at_us) = 'integer' AND created_at_us > 0),
    CHECK (typeof(updated_at_us) = 'integer' AND updated_at_us >= created_at_us),
    CHECK (started_at_us IS NULL OR (typeof(started_at_us) = 'integer'
           AND started_at_us >= created_at_us)),
    CHECK (finished_at_us IS NULL OR (typeof(finished_at_us) = 'integer'
           AND finished_at_us >= created_at_us)),
    CHECK ((state IN ('succeeded', 'failed', 'cancelled')
            AND finished_at_us IS NOT NULL)
           OR (state NOT IN ('succeeded', 'failed', 'cancelled')
               AND finished_at_us IS NULL)),

    FOREIGN KEY (workspace_id, conversation_id)
        REFERENCES omnivia_chat_conversations (workspace_id, conversation_id),
    FOREIGN KEY (workspace_id, conversation_id, branch_id)
        REFERENCES omnivia_chat_message_branches
            (workspace_id, conversation_id, branch_id),
    FOREIGN KEY (workspace_id, conversation_id, trigger_message_id)
        REFERENCES omnivia_chat_messages (workspace_id, conversation_id, message_id),
    FOREIGN KEY (workspace_id, conversation_id, result_message_id)
        REFERENCES omnivia_chat_messages (workspace_id, conversation_id, message_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_chat_generation_attempts (
    workspace_id             TEXT    NOT NULL,
    conversation_id          TEXT    NOT NULL,
    generation_job_id        TEXT    NOT NULL,
    generation_attempt_id    TEXT    NOT NULL,
    attempt_number           INTEGER NOT NULL,
    retry_of_attempt_id      TEXT,
    state                    TEXT    NOT NULL,
    provider_invocation_id   TEXT,
    schema_version           INTEGER NOT NULL,
    started_at_us            INTEGER NOT NULL,
    ended_at_us              INTEGER,

    PRIMARY KEY (workspace_id, generation_attempt_id),
    UNIQUE (workspace_id, generation_job_id, attempt_number),
    UNIQUE (workspace_id, generation_job_id, generation_attempt_id),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(conversation_id) = 'text'
           AND length(conversation_id) BETWEEN 1 AND 128
           AND conversation_id GLOB '[A-Za-z0-9]*'
           AND conversation_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(conversation_id, char(0)) = 0),
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
    CHECK (typeof(attempt_number) = 'integer'
           AND attempt_number BETWEEN 1 AND 1000),
    CHECK (retry_of_attempt_id IS NULL OR (typeof(retry_of_attempt_id) = 'text'
           AND length(retry_of_attempt_id) BETWEEN 1 AND 128
           AND retry_of_attempt_id GLOB '[A-Za-z0-9]*'
           AND retry_of_attempt_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(retry_of_attempt_id, char(0)) = 0)),
    CHECK ((attempt_number = 1 AND retry_of_attempt_id IS NULL)
           OR (attempt_number > 1 AND retry_of_attempt_id IS NOT NULL)),
    CHECK (state IN ('running', 'succeeded', 'failed', 'cancelled')),
    CHECK (provider_invocation_id IS NULL
           OR (typeof(provider_invocation_id) = 'text'
               AND length(provider_invocation_id) BETWEEN 1 AND 128
               AND provider_invocation_id GLOB '[A-Za-z0-9]*'
               AND provider_invocation_id NOT GLOB '*[^A-Za-z0-9._:-]*'
               AND instr(provider_invocation_id, char(0)) = 0)),
    CHECK (typeof(schema_version) = 'integer' AND schema_version = 1),
    CHECK (typeof(started_at_us) = 'integer' AND started_at_us > 0),
    CHECK (ended_at_us IS NULL OR (typeof(ended_at_us) = 'integer'
           AND ended_at_us >= started_at_us)),
    CHECK ((state = 'running' AND ended_at_us IS NULL)
           OR (state <> 'running' AND ended_at_us IS NOT NULL)),

    FOREIGN KEY (workspace_id, conversation_id, generation_job_id)
        REFERENCES omnivia_chat_generation_jobs
            (workspace_id, conversation_id, generation_job_id),
    FOREIGN KEY (workspace_id, generation_job_id, retry_of_attempt_id)
        REFERENCES omnivia_chat_generation_attempts
            (workspace_id, generation_job_id, generation_attempt_id),
    FOREIGN KEY (workspace_id, provider_invocation_id)
        REFERENCES omnivia_provider_invocations (workspace_id, invocation_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_chat_generation_events (
    workspace_id                 TEXT    NOT NULL,
    conversation_id              TEXT    NOT NULL,
    branch_id                    TEXT    NOT NULL,
    generation_job_id            TEXT    NOT NULL,
    generation_attempt_id        TEXT,
    event_id                     TEXT    NOT NULL,
    event_type                   TEXT    NOT NULL,
    generation_event_sequence    INTEGER NOT NULL,
    trigger_message_id           TEXT    NOT NULL,
    result_message_id            TEXT,
    provider_event_id            TEXT,
    cursor                       TEXT    NOT NULL,
    payload_json                 TEXT    NOT NULL,
    occurred_at_us               INTEGER NOT NULL,
    schema_version               INTEGER NOT NULL,

    PRIMARY KEY (workspace_id, event_id),
    UNIQUE (workspace_id, generation_job_id, generation_event_sequence),
    UNIQUE (workspace_id, cursor),
    UNIQUE (workspace_id, generation_job_id, generation_attempt_id, provider_event_id),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(conversation_id) = 'text'
           AND length(conversation_id) BETWEEN 1 AND 128
           AND conversation_id GLOB '[A-Za-z0-9]*'
           AND conversation_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(conversation_id, char(0)) = 0),
    CHECK (typeof(branch_id) = 'text' AND length(branch_id) BETWEEN 1 AND 128
           AND branch_id GLOB '[A-Za-z0-9]*'
           AND branch_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(branch_id, char(0)) = 0),
    CHECK (typeof(generation_job_id) = 'text'
           AND length(generation_job_id) BETWEEN 1 AND 128
           AND generation_job_id GLOB '[A-Za-z0-9]*'
           AND generation_job_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(generation_job_id, char(0)) = 0),
    CHECK (generation_attempt_id IS NULL
           OR (typeof(generation_attempt_id) = 'text'
               AND length(generation_attempt_id) BETWEEN 1 AND 128
               AND generation_attempt_id GLOB '[A-Za-z0-9]*'
               AND generation_attempt_id NOT GLOB '*[^A-Za-z0-9._:-]*'
               AND instr(generation_attempt_id, char(0)) = 0)),
    CHECK (typeof(event_id) = 'text' AND length(event_id) BETWEEN 1 AND 128
           AND event_id GLOB '[A-Za-z0-9]*'
           AND event_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(event_id, char(0)) = 0),
    CHECK (event_type IN ('chat.generation.queued', 'chat.generation.started',
                          'chat.generation.succeeded', 'chat.generation.failed',
                          'chat.generation.cancelled')),
    CHECK (typeof(generation_event_sequence) = 'integer'
           AND generation_event_sequence BETWEEN 1 AND 1000000),
    CHECK (typeof(trigger_message_id) = 'text'
           AND length(trigger_message_id) BETWEEN 1 AND 128
           AND trigger_message_id GLOB '[A-Za-z0-9]*'
           AND trigger_message_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(trigger_message_id, char(0)) = 0),
    CHECK (result_message_id IS NULL OR (typeof(result_message_id) = 'text'
           AND length(result_message_id) BETWEEN 1 AND 128
           AND result_message_id GLOB '[A-Za-z0-9]*'
           AND result_message_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(result_message_id, char(0)) = 0)),
    CHECK ((event_type = 'chat.generation.queued'
            AND generation_attempt_id IS NULL AND result_message_id IS NULL)
           OR (event_type = 'chat.generation.started'
               AND generation_attempt_id IS NOT NULL AND result_message_id IS NULL)
           OR (event_type = 'chat.generation.succeeded'
               AND generation_attempt_id IS NOT NULL AND result_message_id IS NOT NULL)
           OR (event_type IN ('chat.generation.failed', 'chat.generation.cancelled')
               AND result_message_id IS NULL)),
    CHECK (provider_event_id IS NULL OR (typeof(provider_event_id) = 'text'
           AND length(provider_event_id) BETWEEN 1 AND 128
           AND provider_event_id GLOB '[A-Za-z0-9]*'
           AND provider_event_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(provider_event_id, char(0)) = 0)),
    CHECK (provider_event_id IS NULL OR generation_attempt_id IS NOT NULL),
    CHECK (typeof(cursor) = 'text' AND length(cursor) BETWEEN 1 AND 256
           AND cursor GLOB '[A-Za-z0-9]*'
           AND cursor NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(cursor, char(0)) = 0),
    CHECK (typeof(payload_json) = 'text'
           AND length(CAST(payload_json AS BLOB)) BETWEEN 2 AND 65536
           AND instr(payload_json, char(0)) = 0),
    CHECK (typeof(occurred_at_us) = 'integer' AND occurred_at_us > 0),
    CHECK (typeof(schema_version) = 'integer' AND schema_version = 1),

    FOREIGN KEY (workspace_id, conversation_id, generation_job_id)
        REFERENCES omnivia_chat_generation_jobs
            (workspace_id, conversation_id, generation_job_id),
    FOREIGN KEY (workspace_id, conversation_id, branch_id)
        REFERENCES omnivia_chat_message_branches
            (workspace_id, conversation_id, branch_id),
    FOREIGN KEY (workspace_id, conversation_id, trigger_message_id)
        REFERENCES omnivia_chat_messages (workspace_id, conversation_id, message_id),
    FOREIGN KEY (workspace_id, conversation_id, result_message_id)
        REFERENCES omnivia_chat_messages (workspace_id, conversation_id, message_id),
    FOREIGN KEY (workspace_id, generation_job_id, generation_attempt_id)
        REFERENCES omnivia_chat_generation_attempts
            (workspace_id, generation_job_id, generation_attempt_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_chat_transactional_outbox (
    workspace_id               TEXT    NOT NULL,
    outbox_cursor              INTEGER NOT NULL,
    domain_event_id            TEXT    NOT NULL,
    event_kind                 TEXT    NOT NULL,
    conversation_id            TEXT,
    generation_job_id          TEXT,
    payload_json               TEXT    NOT NULL,
    delivery_state             TEXT    NOT NULL,
    delivery_attempts          INTEGER NOT NULL,
    next_delivery_after_us     INTEGER,
    delivered_at_us            INTEGER,
    retained_until_us          INTEGER,
    created_at_us              INTEGER NOT NULL,

    PRIMARY KEY (workspace_id, outbox_cursor),
    UNIQUE (workspace_id, domain_event_id),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(outbox_cursor) = 'integer' AND outbox_cursor >= 1),
    CHECK (typeof(domain_event_id) = 'text'
           AND length(domain_event_id) BETWEEN 1 AND 128
           AND domain_event_id GLOB '[A-Za-z0-9]*'
           AND domain_event_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(domain_event_id, char(0)) = 0),
    CHECK (typeof(event_kind) = 'text' AND length(event_kind) BETWEEN 1 AND 128
           AND event_kind GLOB '[a-z]*'
           AND event_kind NOT GLOB '*[^a-z0-9._-]*'
           AND event_kind NOT GLOB '*.'
           AND event_kind NOT GLOB '*.[^a-z]*'),
    CHECK (conversation_id IS NULL OR (typeof(conversation_id) = 'text'
           AND length(conversation_id) BETWEEN 1 AND 128
           AND conversation_id GLOB '[A-Za-z0-9]*'
           AND conversation_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(conversation_id, char(0)) = 0)),
    CHECK (generation_job_id IS NULL OR (typeof(generation_job_id) = 'text'
           AND length(generation_job_id) BETWEEN 1 AND 128
           AND generation_job_id GLOB '[A-Za-z0-9]*'
           AND generation_job_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(generation_job_id, char(0)) = 0)),
    CHECK (generation_job_id IS NULL OR conversation_id IS NOT NULL),
    CHECK (typeof(payload_json) = 'text'
           AND length(CAST(payload_json AS BLOB)) BETWEEN 2 AND 65536
           AND instr(payload_json, char(0)) = 0),
    CHECK (delivery_state IN ('pending', 'delivering', 'delivered', 'failed')),
    CHECK (typeof(delivery_attempts) = 'integer' AND delivery_attempts >= 0),
    CHECK (next_delivery_after_us IS NULL
           OR (typeof(next_delivery_after_us) = 'integer'
               AND next_delivery_after_us >= created_at_us)),
    CHECK (delivered_at_us IS NULL OR (typeof(delivered_at_us) = 'integer'
           AND delivered_at_us >= created_at_us)),
    CHECK ((delivery_state = 'delivered' AND delivered_at_us IS NOT NULL)
           OR (delivery_state <> 'delivered')),
    CHECK (retained_until_us IS NULL OR (typeof(retained_until_us) = 'integer'
           AND retained_until_us >= created_at_us)),
    CHECK (typeof(created_at_us) = 'integer' AND created_at_us > 0),

    FOREIGN KEY (workspace_id, conversation_id)
        REFERENCES omnivia_chat_conversations (workspace_id, conversation_id),
    FOREIGN KEY (workspace_id, conversation_id, generation_job_id)
        REFERENCES omnivia_chat_generation_jobs
            (workspace_id, conversation_id, generation_job_id)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS omnivia_idx_chat_conversations_list
    ON omnivia_chat_conversations
        (workspace_id, state, updated_at_us DESC, conversation_id DESC);

CREATE INDEX IF NOT EXISTS omnivia_idx_chat_messages_conversation_sequence
    ON omnivia_chat_messages
        (workspace_id, conversation_id, conversation_sequence, message_id);

CREATE INDEX IF NOT EXISTS omnivia_idx_chat_messages_parent
    ON omnivia_chat_messages (workspace_id, conversation_id, parent_message_id);

CREATE INDEX IF NOT EXISTS omnivia_idx_chat_message_parts_order
    ON omnivia_chat_message_parts (workspace_id, message_id, part_index, part_id);

CREATE INDEX IF NOT EXISTS omnivia_idx_chat_message_derivations_source
    ON omnivia_chat_message_derivations
        (workspace_id, conversation_id, source_message_id);

CREATE INDEX IF NOT EXISTS omnivia_idx_chat_message_derivations_derived
    ON omnivia_chat_message_derivations
        (workspace_id, conversation_id, derived_message_id);

CREATE INDEX IF NOT EXISTS omnivia_idx_chat_message_branches_conversation
    ON omnivia_chat_message_branches (workspace_id, conversation_id, created_at_us, branch_id);

CREATE INDEX IF NOT EXISTS omnivia_idx_chat_branch_head_events_order
    ON omnivia_chat_branch_head_events
        (workspace_id, conversation_id, branch_id, head_version);

CREATE INDEX IF NOT EXISTS omnivia_idx_chat_view_states_actor
    ON omnivia_chat_conversation_view_states
        (workspace_id, actor_id, conversation_id, version);

CREATE INDEX IF NOT EXISTS omnivia_idx_chat_drafts_actor_updated
    ON omnivia_chat_drafts (workspace_id, conversation_id, actor_id, updated_at_us);

CREATE INDEX IF NOT EXISTS omnivia_idx_chat_queued_submissions_order
    ON omnivia_chat_queued_submissions
        (workspace_id, conversation_id, actor_id, state, queue_sequence, queued_submission_id);

CREATE INDEX IF NOT EXISTS omnivia_idx_chat_generation_jobs_claim
    ON omnivia_chat_generation_jobs (workspace_id, state, lease_expires_at_us, created_at_us);

CREATE INDEX IF NOT EXISTS omnivia_idx_chat_generation_events_order
    ON omnivia_chat_generation_events
        (workspace_id, generation_job_id, generation_event_sequence);

CREATE INDEX IF NOT EXISTS omnivia_idx_chat_outbox_delivery
    ON omnivia_chat_transactional_outbox
        (workspace_id, delivery_state, next_delivery_after_us, outbox_cursor);

CREATE TRIGGER IF NOT EXISTS omnivia_guard_chat_conversations_insert
BEFORE INSERT ON omnivia_chat_conversations
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_chat_conversations')
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

CREATE TRIGGER IF NOT EXISTS omnivia_guard_chat_conversations_update
BEFORE UPDATE ON omnivia_chat_conversations
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded UPDATE on omnivia_chat_conversations')
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
    SELECT RAISE(ABORT, 'omnivia: conversation identity and counters are immutable here')
    WHERE NEW.workspace_id <> OLD.workspace_id
       OR NEW.conversation_id <> OLD.conversation_id
       OR NEW.created_by_actor_id <> OLD.created_by_actor_id
       OR NEW.created_at_us <> OLD.created_at_us
       OR NEW.graph_revision < OLD.graph_revision
       OR NEW.latest_conversation_sequence < OLD.latest_conversation_sequence
       OR NEW.schema_version <> OLD.schema_version;
    SELECT RAISE(ABORT, 'omnivia: a terminal conversation cannot reopen')
    WHERE OLD.state IN ('archived', 'tombstoned')
      AND NEW.state <> OLD.state;
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_chat_conversations_delete
BEFORE DELETE ON omnivia_chat_conversations
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_chat_conversations forbids DELETE');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_chat_messages_insert
BEFORE INSERT ON omnivia_chat_messages
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_chat_messages')
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
    SELECT RAISE(ABORT, 'omnivia: message sequence cannot point behind conversation')
    WHERE NEW.conversation_sequence > (
        SELECT latest_conversation_sequence FROM omnivia_chat_conversations
        WHERE workspace_id = NEW.workspace_id
          AND conversation_id = NEW.conversation_id);
    SELECT RAISE(ABORT, 'omnivia: message parent cannot be from the future')
    WHERE NEW.parent_message_id IS NOT NULL
      AND NEW.conversation_sequence <= (
        SELECT conversation_sequence FROM omnivia_chat_messages
        WHERE workspace_id = NEW.workspace_id
          AND conversation_id = NEW.conversation_id
          AND message_id = NEW.parent_message_id);
    SELECT RAISE(ABORT, 'omnivia: assistant message result must name its generation job')
    WHERE NEW.role = 'assistant' AND NEW.generation_job_id IS NULL;
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_chat_messages_update
BEFORE UPDATE ON omnivia_chat_messages
BEGIN
    SELECT RAISE(ABORT, 'omnivia: committed chat messages are immutable; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_chat_messages_delete
BEFORE DELETE ON omnivia_chat_messages
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_chat_messages forbids DELETE');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_chat_message_parts_insert
BEFORE INSERT ON omnivia_chat_message_parts
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_chat_message_parts')
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
    SELECT RAISE(ABORT, 'omnivia: message parts must be contiguous from zero')
    WHERE NEW.part_index IS NOT (
        SELECT COUNT(*) FROM omnivia_chat_message_parts
        WHERE workspace_id = NEW.workspace_id
          AND message_id = NEW.message_id);
    SELECT RAISE(ABORT, 'omnivia: message part payload must be an exact canonical JSON object')
    WHERE json_valid(NEW.payload_json) IS NOT 1
       OR json(NEW.payload_json) <> NEW.payload_json
       OR json_type(NEW.payload_json) <> 'object';
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_chat_message_parts_update
BEFORE UPDATE ON omnivia_chat_message_parts
BEGIN
    SELECT RAISE(ABORT, 'omnivia: committed chat message parts are immutable; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_chat_message_parts_delete
BEFORE DELETE ON omnivia_chat_message_parts
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_chat_message_parts forbids DELETE');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_chat_message_derivations_insert
BEFORE INSERT ON omnivia_chat_message_derivations
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_chat_message_derivations')
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
    SELECT RAISE(ABORT, 'omnivia: derived message must be newer than its source')
    WHERE (SELECT conversation_sequence FROM omnivia_chat_messages
           WHERE workspace_id = NEW.workspace_id
             AND conversation_id = NEW.conversation_id
             AND message_id = NEW.derived_message_id)
          <=
          (SELECT conversation_sequence FROM omnivia_chat_messages
           WHERE workspace_id = NEW.workspace_id
             AND conversation_id = NEW.conversation_id
             AND message_id = NEW.source_message_id);
    SELECT RAISE(ABORT, 'omnivia: amendment derivation requires two user messages with the same structural parent')
    WHERE NEW.derivation_kind = 'amendment'
      AND NOT EXISTS (
        SELECT 1 FROM omnivia_chat_messages source
        JOIN omnivia_chat_messages derived
          ON derived.workspace_id = source.workspace_id
         AND derived.conversation_id = source.conversation_id
         AND derived.message_id = NEW.derived_message_id
        WHERE source.workspace_id = NEW.workspace_id
          AND source.conversation_id = NEW.conversation_id
          AND source.message_id = NEW.source_message_id
          AND source.role = 'user'
          AND derived.role = 'user'
          AND source.parent_message_id IS derived.parent_message_id);
    SELECT RAISE(ABORT, 'omnivia: regeneration derivation requires two assistant messages with the same structural parent')
    WHERE NEW.derivation_kind = 'regeneration'
      AND NOT EXISTS (
        SELECT 1 FROM omnivia_chat_messages source
        JOIN omnivia_chat_messages derived
          ON derived.workspace_id = source.workspace_id
         AND derived.conversation_id = source.conversation_id
         AND derived.message_id = NEW.derived_message_id
        WHERE source.workspace_id = NEW.workspace_id
          AND source.conversation_id = NEW.conversation_id
          AND source.message_id = NEW.source_message_id
          AND source.role = 'assistant'
          AND derived.role = 'assistant'
          AND source.parent_message_id IS derived.parent_message_id);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_chat_message_derivations_update
BEFORE UPDATE ON omnivia_chat_message_derivations
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_chat_message_derivations is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_chat_message_derivations_delete
BEFORE DELETE ON omnivia_chat_message_derivations
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_chat_message_derivations forbids DELETE');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_chat_message_branches_insert
BEFORE INSERT ON omnivia_chat_message_branches
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_chat_message_branches')
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
    SELECT RAISE(ABORT, 'omnivia: new branch starts at head version one')
    WHERE NEW.head_version <> 1;
    SELECT RAISE(ABORT, 'omnivia: branch initial head must match current head at creation')
    WHERE NEW.initial_head_message_id <> NEW.current_head_message_id;
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_chat_message_branches_update
BEFORE UPDATE ON omnivia_chat_message_branches
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded UPDATE on omnivia_chat_message_branches')
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
    SELECT RAISE(ABORT, 'omnivia: branch identity and origin are immutable')
    WHERE NEW.workspace_id <> OLD.workspace_id
       OR NEW.conversation_id <> OLD.conversation_id
       OR NEW.branch_id <> OLD.branch_id
       OR NEW.origin_kind <> OLD.origin_kind
       OR NEW.created_from_branch_id IS NOT OLD.created_from_branch_id
       OR NEW.fork_parent_message_id IS NOT OLD.fork_parent_message_id
       OR NEW.fork_source_message_id IS NOT OLD.fork_source_message_id
       OR NEW.initial_head_message_id <> OLD.initial_head_message_id
       OR NEW.created_by_actor_id <> OLD.created_by_actor_id
       OR NEW.created_at_us <> OLD.created_at_us
       OR NEW.created_conversation_sequence <> OLD.created_conversation_sequence
       OR NEW.schema_version <> OLD.schema_version;
    SELECT RAISE(ABORT, 'omnivia: branch head projection must be backed by its durable head event')
    WHERE NEW.head_version <> OLD.head_version
      AND NOT EXISTS (
        SELECT 1 FROM omnivia_chat_branch_head_events
        WHERE workspace_id = NEW.workspace_id
          AND conversation_id = NEW.conversation_id
          AND branch_id = NEW.branch_id
          AND head_version = NEW.head_version
          AND new_head_message_id = NEW.current_head_message_id);
    SELECT RAISE(ABORT, 'omnivia: terminal branch cannot reopen')
    WHERE OLD.state IN ('archived', 'tombstoned')
      AND NEW.state <> OLD.state;
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_chat_message_branches_delete
BEFORE DELETE ON omnivia_chat_message_branches
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_chat_message_branches forbids DELETE');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_chat_branch_head_events_insert
BEFORE INSERT ON omnivia_chat_branch_head_events
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_chat_branch_head_events')
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
    SELECT RAISE(ABORT, 'omnivia: branch head events must be contiguous')
    WHERE NEW.head_version IS NOT (
        SELECT COALESCE(MAX(head_version), 0) + 1
        FROM omnivia_chat_branch_head_events
        WHERE workspace_id = NEW.workspace_id
          AND branch_id = NEW.branch_id);
    SELECT RAISE(ABORT, 'omnivia: branch head event must name the current prior head')
    WHERE NEW.head_version > 1
      AND NEW.previous_head_message_id IS NOT (
        SELECT current_head_message_id FROM omnivia_chat_message_branches
        WHERE workspace_id = NEW.workspace_id
          AND conversation_id = NEW.conversation_id
          AND branch_id = NEW.branch_id);
    SELECT RAISE(ABORT, 'omnivia: first branch head event must name the branch initial head')
    WHERE NEW.head_version = 1
      AND NEW.new_head_message_id <> (
        SELECT initial_head_message_id FROM omnivia_chat_message_branches
        WHERE workspace_id = NEW.workspace_id
          AND conversation_id = NEW.conversation_id
          AND branch_id = NEW.branch_id);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_chat_branch_head_events_update
BEFORE UPDATE ON omnivia_chat_branch_head_events
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_chat_branch_head_events is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_chat_branch_head_events_delete
BEFORE DELETE ON omnivia_chat_branch_head_events
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_chat_branch_head_events forbids DELETE');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_chat_conversation_view_states_insert
BEFORE INSERT ON omnivia_chat_conversation_view_states
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_chat_conversation_view_states')
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

CREATE TRIGGER IF NOT EXISTS omnivia_guard_chat_conversation_view_states_update
BEFORE UPDATE ON omnivia_chat_conversation_view_states
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded UPDATE on omnivia_chat_conversation_view_states')
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
    SELECT RAISE(ABORT, 'omnivia: view-state identity is immutable')
    WHERE NEW.workspace_id <> OLD.workspace_id
       OR NEW.conversation_id <> OLD.conversation_id
       OR NEW.actor_id <> OLD.actor_id
       OR NEW.device_id <> OLD.device_id
       OR NEW.schema_version <> OLD.schema_version;
    SELECT RAISE(ABORT, 'omnivia: view-state version must advance by one')
    WHERE NEW.version <> OLD.version + 1;
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_chat_conversation_view_states_delete
BEFORE DELETE ON omnivia_chat_conversation_view_states
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_chat_conversation_view_states forbids DELETE');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_chat_drafts_insert
BEFORE INSERT ON omnivia_chat_drafts
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_chat_drafts')
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
    SELECT RAISE(ABORT, 'omnivia: draft documents must be exact canonical JSON')
    WHERE json_valid(NEW.references_json) IS NOT 1
       OR json(NEW.references_json) <> NEW.references_json
       OR (NEW.target_json IS NOT NULL
           AND (json_valid(NEW.target_json) IS NOT 1
                OR json(NEW.target_json) <> NEW.target_json));
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_chat_drafts_update
BEFORE UPDATE ON omnivia_chat_drafts
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded UPDATE on omnivia_chat_drafts')
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
    SELECT RAISE(ABORT, 'omnivia: draft identity and mode are immutable')
    WHERE NEW.workspace_id <> OLD.workspace_id
       OR NEW.conversation_id <> OLD.conversation_id
       OR NEW.actor_id <> OLD.actor_id
       OR NEW.device_id <> OLD.device_id
       OR NEW.draft_id <> OLD.draft_id
       OR NEW.mode <> OLD.mode
       OR NEW.source_message_id IS NOT OLD.source_message_id
       OR NEW.schema_version <> OLD.schema_version;
    SELECT RAISE(ABORT, 'omnivia: draft version must advance by one')
    WHERE NEW.version <> OLD.version + 1;
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_chat_drafts_delete
BEFORE DELETE ON omnivia_chat_drafts
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_chat_drafts forbids DELETE');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_chat_queued_submissions_insert
BEFORE INSERT ON omnivia_chat_queued_submissions
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_chat_queued_submissions')
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
    SELECT RAISE(ABORT, 'omnivia: queued submissions are inserted only in queued state')
    WHERE NEW.state <> 'queued';
    SELECT RAISE(ABORT, 'omnivia: queued submission documents must be exact canonical JSON')
    WHERE json_valid(NEW.editable_parts_json) IS NOT 1
       OR json(NEW.editable_parts_json) <> NEW.editable_parts_json
       OR json_valid(NEW.references_json) IS NOT 1
       OR json(NEW.references_json) <> NEW.references_json;
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_chat_queued_submissions_update
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

CREATE TRIGGER IF NOT EXISTS omnivia_guard_chat_queued_submissions_delete
BEFORE DELETE ON omnivia_chat_queued_submissions
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_chat_queued_submissions forbids DELETE');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_chat_generation_jobs_insert
BEFORE INSERT ON omnivia_chat_generation_jobs
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_chat_generation_jobs')
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
    SELECT RAISE(ABORT, 'omnivia: generation jobs are inserted only in queued state')
    WHERE NEW.state <> 'queued';
    SELECT RAISE(ABORT, 'omnivia: generation job must observe an existing conversation graph revision')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_chat_conversations
        WHERE workspace_id = NEW.workspace_id
          AND conversation_id = NEW.conversation_id
          AND graph_revision = NEW.graph_revision_observed);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_chat_generation_jobs_update
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
    SELECT RAISE(ABORT, 'omnivia: generation job transition is invalid')
    WHERE NOT ((OLD.state = 'queued'
                AND NEW.state IN ('queued', 'running', 'cancelled', 'failed'))
            OR (OLD.state = 'running'
                AND NEW.state IN ('running', 'succeeded', 'failed', 'cancelled'))
            OR (OLD.state IN ('succeeded', 'failed', 'cancelled')
                AND NEW.state = OLD.state));
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_chat_generation_jobs_delete
BEFORE DELETE ON omnivia_chat_generation_jobs
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_chat_generation_jobs forbids DELETE');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_chat_generation_attempts_insert
BEFORE INSERT ON omnivia_chat_generation_attempts
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_chat_generation_attempts')
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
    SELECT RAISE(ABORT, 'omnivia: generation attempts must be contiguous from one')
    WHERE NEW.attempt_number IS NOT (
        SELECT COALESCE(MAX(attempt_number), 0) + 1
        FROM omnivia_chat_generation_attempts
        WHERE workspace_id = NEW.workspace_id
          AND generation_job_id = NEW.generation_job_id);
    SELECT RAISE(ABORT, 'omnivia: generation attempt cannot start before its job')
    WHERE NEW.started_at_us < (
        SELECT created_at_us FROM omnivia_chat_generation_jobs
        WHERE workspace_id = NEW.workspace_id
          AND generation_job_id = NEW.generation_job_id);
    SELECT RAISE(ABORT, 'omnivia: generation attempt provider invocation must name the same job and attempt')
    WHERE NEW.provider_invocation_id IS NOT NULL
      AND NOT EXISTS (
        SELECT 1 FROM omnivia_provider_invocations
        WHERE workspace_id = NEW.workspace_id
          AND invocation_id = NEW.provider_invocation_id
          AND conversation_id = NEW.conversation_id
          AND job_id = NEW.generation_job_id
          AND generation_attempt_id = NEW.generation_attempt_id);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_chat_generation_attempts_update
BEFORE UPDATE ON omnivia_chat_generation_attempts
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_chat_generation_attempts is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_chat_generation_attempts_delete
BEFORE DELETE ON omnivia_chat_generation_attempts
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_chat_generation_attempts forbids DELETE');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_chat_generation_events_insert
BEFORE INSERT ON omnivia_chat_generation_events
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_chat_generation_events')
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
    SELECT RAISE(ABORT, 'omnivia: generation events must be contiguous from one')
    WHERE NEW.generation_event_sequence IS NOT (
        SELECT COALESCE(MAX(generation_event_sequence), 0) + 1
        FROM omnivia_chat_generation_events
        WHERE workspace_id = NEW.workspace_id
          AND generation_job_id = NEW.generation_job_id);
    SELECT RAISE(ABORT, 'omnivia: generation event cannot predate its job')
    WHERE NEW.occurred_at_us < (
        SELECT created_at_us FROM omnivia_chat_generation_jobs
        WHERE workspace_id = NEW.workspace_id
          AND generation_job_id = NEW.generation_job_id);
    SELECT RAISE(ABORT, 'omnivia: generation event payload must be an exact canonical JSON object')
    WHERE json_valid(NEW.payload_json) IS NOT 1
       OR json(NEW.payload_json) <> NEW.payload_json
       OR json_type(NEW.payload_json) <> 'object';
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_chat_generation_events_update
BEFORE UPDATE ON omnivia_chat_generation_events
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_chat_generation_events is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_chat_generation_events_delete
BEFORE DELETE ON omnivia_chat_generation_events
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_chat_generation_events forbids DELETE');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_chat_transactional_outbox_insert
BEFORE INSERT ON omnivia_chat_transactional_outbox
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_chat_transactional_outbox')
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
    SELECT RAISE(ABORT, 'omnivia: chat outbox cursor must be contiguous from one')
    WHERE NEW.outbox_cursor IS NOT (
        SELECT COALESCE(MAX(outbox_cursor), 0) + 1
        FROM omnivia_chat_transactional_outbox
        WHERE workspace_id = NEW.workspace_id);
    SELECT RAISE(ABORT, 'omnivia: chat outbox payload must be an exact canonical JSON object')
    WHERE json_valid(NEW.payload_json) IS NOT 1
       OR json(NEW.payload_json) <> NEW.payload_json
       OR json_type(NEW.payload_json) <> 'object';
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_chat_transactional_outbox_update
BEFORE UPDATE ON omnivia_chat_transactional_outbox
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded UPDATE on omnivia_chat_transactional_outbox')
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
    SELECT RAISE(ABORT, 'omnivia: chat outbox identity and payload are immutable')
    WHERE NEW.workspace_id <> OLD.workspace_id
       OR NEW.outbox_cursor <> OLD.outbox_cursor
       OR NEW.domain_event_id <> OLD.domain_event_id
       OR NEW.event_kind <> OLD.event_kind
       OR NEW.conversation_id IS NOT OLD.conversation_id
       OR NEW.generation_job_id IS NOT OLD.generation_job_id
       OR NEW.payload_json <> OLD.payload_json
       OR NEW.created_at_us <> OLD.created_at_us;
    SELECT RAISE(ABORT, 'omnivia: delivered outbox entry cannot reopen')
    WHERE OLD.delivery_state = 'delivered'
      AND NEW.delivery_state <> 'delivered';
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_chat_transactional_outbox_delete
BEFORE DELETE ON omnivia_chat_transactional_outbox
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_chat_transactional_outbox forbids DELETE');
END;
