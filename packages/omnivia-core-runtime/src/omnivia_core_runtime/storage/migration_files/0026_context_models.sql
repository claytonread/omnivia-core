-- Durable Context Model foundation: one aggregate, immutable versions, exact L2
-- grounding and append-only lifecycle facts.
--
-- Additive only. Four append-only tables, three named indexes and twelve statement
-- triggers. This file stores Context Model truth and nothing that acts on it: no
-- repository, no service, no handler, no renderer state, no Chat state, no workflow
-- state and no Provider invocation state. It holds no Provider credential, no SDK
-- object and no raw response body; the only thing it records about generation is who
-- is attributed for it and the digest of the instruction that produced it.
--
--   omnivia_context_models                  one stable Context Model aggregate
--   omnivia_context_model_versions          one immutable version of that aggregate
--   omnivia_context_model_grounding_refs    the exact sealed governed L2 sources
--   omnivia_context_model_lifecycle_events  the append-only governance history
--
-- There is one aggregate table rather than one root per subtype. The subtype is a
-- closed execution vocabulary of nine codes carried on the aggregate and repeated on
-- every version under a foreign key, so a version can never claim a subtype its own
-- aggregate never had, and an unrecognised subtype cannot be created at all.
--
-- Lifecycle is a chain of append-only events, never a column somebody overwrites. The
-- eight legal transitions are a CHECK, so `superseded`, `rejected` and `withdrawn` are
-- terminal by construction: no admitted transition starts from one of them, and the
-- insert trigger requires every event's `from_state` to equal the version's current
-- state, so a terminal version can never receive another event.
--
-- Grounding names an exact sealed governed L2 version in this workspace -- by record,
-- by version and by the content digest observed at grounding time -- never a range and
-- never a floating latest. `omnivia_authoritative_governed_versions` is the only thing
-- it is checked against, and only an accepted canonical row in it counts, so a governed
-- version that was never sealed -- or one that was sealed rejected, superseded or short
-- of canonical authority -- grounds nothing.
-- Grounding is fixed at generation: once any governance act beyond `generated` has been
-- recorded against a version, that version's grounding can no longer grow, so a stale
-- source is answered by a governed revision rather than by a silent rebind.
--
-- Attribution is stored, not evaluated. The generator of a version is recorded so a
-- later service can refuse self-promotion; the same refusal is also made here, for the
-- two positive promotions, because SQL can see both actors.
--
-- UPDATE and DELETE abort unconditionally, for the current fenced owner too.

CREATE TABLE IF NOT EXISTS omnivia_context_models (
    workspace_id        TEXT    NOT NULL,
    context_model_id    TEXT    NOT NULL,
    subtype             TEXT    NOT NULL,
    created_by_actor_id TEXT    NOT NULL,
    created_at_us       INTEGER NOT NULL,
    audit_ref           TEXT    NOT NULL,

    PRIMARY KEY (workspace_id, context_model_id),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(context_model_id) = 'text'
           AND length(context_model_id) BETWEEN 1 AND 128
           AND context_model_id GLOB '[A-Za-z0-9]*'
           AND context_model_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(context_model_id, char(0)) = 0),
    CHECK (subtype IN ('project', 'customer', 'persona', 'product', 'process',
                       'role', 'system', 'business_area', 'architecture')),
    CHECK (typeof(created_by_actor_id) = 'text'
           AND length(created_by_actor_id) BETWEEN 1 AND 128
           AND created_by_actor_id GLOB '[A-Za-z0-9]*'
           AND created_by_actor_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(created_by_actor_id, char(0)) = 0),
    CHECK (typeof(created_at_us) = 'integer' AND created_at_us > 0),
    CHECK (typeof(audit_ref) = 'text' AND length(audit_ref) BETWEEN 1 AND 128
           AND audit_ref GLOB '[A-Za-z0-9]*'
           AND audit_ref NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(audit_ref, char(0)) = 0),

    FOREIGN KEY (audit_ref, workspace_id)
        REFERENCES omnivia_application_audit_events (audit_ref, workspace_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_context_model_versions (
    workspace_id            TEXT    NOT NULL,
    context_model_id        TEXT    NOT NULL,
    version_number          INTEGER NOT NULL,
    subtype                 TEXT    NOT NULL,
    content_json            TEXT    NOT NULL,
    content_digest          TEXT    NOT NULL,
    generated_by_actor_id   TEXT    NOT NULL,
    generated_by_actor_kind TEXT    NOT NULL,
    instruction_digest      TEXT    NOT NULL,
    parent_version_number   INTEGER,
    created_at_us           INTEGER NOT NULL,
    audit_ref               TEXT    NOT NULL,

    PRIMARY KEY (workspace_id, context_model_id, version_number),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(context_model_id) = 'text'
           AND length(context_model_id) BETWEEN 1 AND 128
           AND context_model_id GLOB '[A-Za-z0-9]*'
           AND context_model_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(context_model_id, char(0)) = 0),
    CHECK (typeof(version_number) = 'integer'
           AND version_number BETWEEN 1 AND 1000000),
    CHECK (subtype IN ('project', 'customer', 'persona', 'product', 'process',
                       'role', 'system', 'business_area', 'architecture')),
    CHECK (typeof(content_json) = 'text'
           AND length(CAST(content_json AS BLOB)) BETWEEN 2 AND 1048576
           AND instr(content_json, char(0)) = 0),
    CHECK (typeof(content_digest) = 'text'
           AND length(content_digest) = 71
           AND substr(content_digest, 1, 7) = 'sha256:'
           AND substr(content_digest, 8) NOT GLOB '*[^0-9a-f]*'),
    CHECK (typeof(generated_by_actor_id) = 'text'
           AND length(generated_by_actor_id) BETWEEN 1 AND 128
           AND generated_by_actor_id GLOB '[A-Za-z0-9]*'
           AND generated_by_actor_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(generated_by_actor_id, char(0)) = 0),
    CHECK (typeof(generated_by_actor_kind) = 'text'
           AND length(generated_by_actor_kind) BETWEEN 1 AND 128
           AND generated_by_actor_kind GLOB '[a-z]*'
           AND generated_by_actor_kind NOT GLOB '*[^a-z0-9_.]*'
           AND generated_by_actor_kind NOT GLOB '*.'
           AND generated_by_actor_kind NOT GLOB '*.[^a-z]*'),
    CHECK (typeof(instruction_digest) = 'text'
           AND length(instruction_digest) = 71
           AND substr(instruction_digest, 1, 7) = 'sha256:'
           AND substr(instruction_digest, 8) NOT GLOB '*[^0-9a-f]*'),
    CHECK (parent_version_number IS NULL
           OR (typeof(parent_version_number) = 'integer'
               AND parent_version_number BETWEEN 1 AND 1000000
               AND parent_version_number < version_number)),
    CHECK ((version_number = 1 AND parent_version_number IS NULL)
           OR (version_number > 1 AND parent_version_number IS NOT NULL)),
    CHECK (typeof(created_at_us) = 'integer' AND created_at_us > 0),
    CHECK (typeof(audit_ref) = 'text' AND length(audit_ref) BETWEEN 1 AND 128
           AND audit_ref GLOB '[A-Za-z0-9]*'
           AND audit_ref NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(audit_ref, char(0)) = 0),

    FOREIGN KEY (workspace_id, context_model_id, subtype)
        REFERENCES omnivia_context_models (workspace_id, context_model_id, subtype),
    FOREIGN KEY (workspace_id, context_model_id, parent_version_number)
        REFERENCES omnivia_context_model_versions
            (workspace_id, context_model_id, version_number),
    FOREIGN KEY (audit_ref, workspace_id)
        REFERENCES omnivia_application_audit_events (audit_ref, workspace_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_context_model_grounding_refs (
    workspace_id               TEXT    NOT NULL,
    context_model_id           TEXT    NOT NULL,
    version_number             INTEGER NOT NULL,
    governed_record_id         TEXT    NOT NULL,
    governed_record_version_id TEXT    NOT NULL,
    grounded_content_digest    TEXT    NOT NULL,
    recorded_at_us             INTEGER NOT NULL,

    PRIMARY KEY (workspace_id, context_model_id, version_number,
                 governed_record_version_id),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(context_model_id) = 'text'
           AND length(context_model_id) BETWEEN 1 AND 128
           AND context_model_id GLOB '[A-Za-z0-9]*'
           AND context_model_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(context_model_id, char(0)) = 0),
    CHECK (typeof(version_number) = 'integer'
           AND version_number BETWEEN 1 AND 1000000),
    CHECK (typeof(governed_record_id) = 'text'
           AND length(governed_record_id) BETWEEN 1 AND 128
           AND governed_record_id GLOB '[A-Za-z0-9]*'
           AND governed_record_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(governed_record_id, char(0)) = 0),
    CHECK (typeof(governed_record_version_id) = 'text'
           AND length(governed_record_version_id) BETWEEN 1 AND 128
           AND governed_record_version_id GLOB '[A-Za-z0-9]*'
           AND governed_record_version_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(governed_record_version_id, char(0)) = 0),
    CHECK (typeof(grounded_content_digest) = 'text'
           AND length(grounded_content_digest) = 71
           AND substr(grounded_content_digest, 1, 7) = 'sha256:'
           AND substr(grounded_content_digest, 8) NOT GLOB '*[^0-9a-f]*'),
    CHECK (typeof(recorded_at_us) = 'integer' AND recorded_at_us > 0),

    FOREIGN KEY (workspace_id, context_model_id, version_number)
        REFERENCES omnivia_context_model_versions
            (workspace_id, context_model_id, version_number),
    FOREIGN KEY (workspace_id, governed_record_id)
        REFERENCES omnivia_governed_records (workspace_id, governed_record_id),
    FOREIGN KEY (workspace_id, governed_record_version_id)
        REFERENCES omnivia_governed_version_seals
            (workspace_id, governed_record_version_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_context_model_lifecycle_events (
    workspace_id     TEXT    NOT NULL,
    context_model_id TEXT    NOT NULL,
    version_number   INTEGER NOT NULL,
    event_sequence   INTEGER NOT NULL,
    from_state       TEXT,
    to_state         TEXT    NOT NULL,
    actor_id         TEXT    NOT NULL,
    occurred_at_us   INTEGER NOT NULL,
    reason           TEXT,
    audit_ref        TEXT    NOT NULL,

    PRIMARY KEY (workspace_id, context_model_id, version_number, event_sequence),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(context_model_id) = 'text'
           AND length(context_model_id) BETWEEN 1 AND 128
           AND context_model_id GLOB '[A-Za-z0-9]*'
           AND context_model_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(context_model_id, char(0)) = 0),
    CHECK (typeof(version_number) = 'integer'
           AND version_number BETWEEN 1 AND 1000000),
    CHECK (typeof(event_sequence) = 'integer'
           AND event_sequence BETWEEN 1 AND 1000000),
    CHECK (to_state IN ('generated', 'reviewed', 'published',
                        'superseded', 'rejected', 'withdrawn')),
    CHECK (from_state IS NULL
           OR from_state IN ('generated', 'reviewed', 'published')),
    CHECK ((from_state IS NULL AND to_state = 'generated' AND event_sequence = 1)
           OR (from_state = 'generated'
               AND to_state IN ('reviewed', 'rejected', 'withdrawn'))
           OR (from_state = 'reviewed'
               AND to_state IN ('published', 'rejected', 'withdrawn'))
           OR (from_state = 'published'
               AND to_state IN ('superseded', 'withdrawn'))),
    CHECK (typeof(actor_id) = 'text' AND length(actor_id) BETWEEN 1 AND 128
           AND actor_id GLOB '[A-Za-z0-9]*'
           AND actor_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(actor_id, char(0)) = 0),
    CHECK (typeof(occurred_at_us) = 'integer' AND occurred_at_us > 0),
    CHECK (reason IS NULL
           OR (typeof(reason) = 'text' AND length(reason) BETWEEN 1 AND 128
               AND reason GLOB '[a-z]*'
               AND reason NOT GLOB '*[^a-z0-9_.]*'
               AND reason NOT GLOB '*.'
               AND reason NOT GLOB '*.[^a-z]*')),
    CHECK (to_state NOT IN ('rejected', 'withdrawn') OR reason IS NOT NULL),
    CHECK (typeof(audit_ref) = 'text' AND length(audit_ref) BETWEEN 1 AND 128
           AND audit_ref GLOB '[A-Za-z0-9]*'
           AND audit_ref NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(audit_ref, char(0)) = 0),

    FOREIGN KEY (workspace_id, context_model_id, version_number)
        REFERENCES omnivia_context_model_versions
            (workspace_id, context_model_id, version_number),
    FOREIGN KEY (audit_ref, workspace_id)
        REFERENCES omnivia_application_audit_events (audit_ref, workspace_id)
) WITHOUT ROWID;

-- Parent keys for the composite foreign keys above, plus the one reverse read path.
-- The two unique indexes are declared by name rather than left to an implicit
-- `sqlite_autoindex_*`, because the canonical schema fingerprint filters implicit
-- indexes out and a constraint drift detection cannot see is not a constraint.
CREATE UNIQUE INDEX IF NOT EXISTS omnivia_idx_context_models_subtype
    ON omnivia_context_models (workspace_id, context_model_id, subtype);
CREATE UNIQUE INDEX IF NOT EXISTS omnivia_idx_context_model_versions_identity
    ON omnivia_context_model_versions
        (workspace_id, context_model_id, version_number);
CREATE INDEX IF NOT EXISTS omnivia_idx_context_model_grounding_refs_governed
    ON omnivia_context_model_grounding_refs
        (workspace_id, governed_record_version_id);

CREATE TRIGGER IF NOT EXISTS omnivia_guard_context_models_insert
BEFORE INSERT ON omnivia_context_models
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_context_models')
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
    SELECT RAISE(ABORT, 'omnivia: context model audit reference must belong to its workspace')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_application_audit_events
        WHERE audit_ref = NEW.audit_ref AND workspace_id = NEW.workspace_id);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_context_models_update
BEFORE UPDATE ON omnivia_context_models
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_context_models is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_context_models_delete
BEFORE DELETE ON omnivia_context_models
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_context_models is append-only; DELETE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_context_model_versions_insert
BEFORE INSERT ON omnivia_context_model_versions
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_context_model_versions')
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
    SELECT RAISE(ABORT, 'omnivia: a context model version cannot predate its aggregate')
    WHERE NEW.created_at_us < (
        SELECT created_at_us FROM omnivia_context_models
        WHERE workspace_id = NEW.workspace_id
          AND context_model_id = NEW.context_model_id);
    SELECT RAISE(ABORT, 'omnivia: context model version audit reference must belong to its workspace')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_application_audit_events
        WHERE audit_ref = NEW.audit_ref AND workspace_id = NEW.workspace_id);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_context_model_versions_update
BEFORE UPDATE ON omnivia_context_model_versions
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_context_model_versions is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_context_model_versions_delete
BEFORE DELETE ON omnivia_context_model_versions
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_context_model_versions is append-only; DELETE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_context_model_grounding_refs_insert
BEFORE INSERT ON omnivia_context_model_grounding_refs
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_context_model_grounding_refs')
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
    SELECT RAISE(ABORT, 'omnivia: context model grounding must name an exact sealed governed L2 version in this workspace')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_authoritative_governed_versions
        WHERE workspace_id = NEW.workspace_id
          AND governed_record_id = NEW.governed_record_id
          AND governed_record_version_id = NEW.governed_record_version_id
          AND layer = 'governed'
          AND governance_disposition = 'accepted'
          AND authority_level = 'canonical'
          AND content_digest = NEW.grounded_content_digest);
    SELECT RAISE(ABORT, 'omnivia: context model grounding is fixed at generation and is never rebound')
    WHERE EXISTS (
        SELECT 1 FROM omnivia_context_model_lifecycle_events
        WHERE workspace_id = NEW.workspace_id
          AND context_model_id = NEW.context_model_id
          AND version_number = NEW.version_number
          AND to_state <> 'generated');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_context_model_grounding_refs_update
BEFORE UPDATE ON omnivia_context_model_grounding_refs
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_context_model_grounding_refs is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_context_model_grounding_refs_delete
BEFORE DELETE ON omnivia_context_model_grounding_refs
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_context_model_grounding_refs is append-only; DELETE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_context_model_lifecycle_events_insert
BEFORE INSERT ON omnivia_context_model_lifecycle_events
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_context_model_lifecycle_events')
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
    SELECT RAISE(ABORT, 'omnivia: a context model lifecycle event must continue the version''s current state')
    WHERE NEW.event_sequence IS NOT (
            1 + (SELECT COUNT(*) FROM omnivia_context_model_lifecycle_events
                 WHERE workspace_id = NEW.workspace_id
                   AND context_model_id = NEW.context_model_id
                   AND version_number = NEW.version_number))
       OR NEW.from_state IS NOT (
            SELECT to_state FROM omnivia_context_model_lifecycle_events
            WHERE workspace_id = NEW.workspace_id
              AND context_model_id = NEW.context_model_id
              AND version_number = NEW.version_number
              AND event_sequence = NEW.event_sequence - 1);
    SELECT RAISE(ABORT, 'omnivia: a context model lifecycle event cannot predate its version')
    WHERE NEW.occurred_at_us < (
        SELECT created_at_us FROM omnivia_context_model_versions
        WHERE workspace_id = NEW.workspace_id
          AND context_model_id = NEW.context_model_id
          AND version_number = NEW.version_number);
    SELECT RAISE(ABORT, 'omnivia: publishing a context model version requires at least one exact grounding reference')
    WHERE NEW.to_state = 'published'
      AND NOT EXISTS (
        SELECT 1 FROM omnivia_context_model_grounding_refs
        WHERE workspace_id = NEW.workspace_id
          AND context_model_id = NEW.context_model_id
          AND version_number = NEW.version_number);
    SELECT RAISE(ABORT, 'omnivia: a context model version cannot be promoted by its own generator')
    WHERE NEW.to_state IN ('reviewed', 'published')
      AND NEW.actor_id = (
        SELECT generated_by_actor_id FROM omnivia_context_model_versions
        WHERE workspace_id = NEW.workspace_id
          AND context_model_id = NEW.context_model_id
          AND version_number = NEW.version_number);
    SELECT RAISE(ABORT, 'omnivia: context model lifecycle audit reference must belong to its workspace')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_application_audit_events
        WHERE audit_ref = NEW.audit_ref AND workspace_id = NEW.workspace_id);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_context_model_lifecycle_events_update
BEFORE UPDATE ON omnivia_context_model_lifecycle_events
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_context_model_lifecycle_events is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_context_model_lifecycle_events_delete
BEFORE DELETE ON omnivia_context_model_lifecycle_events
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_context_model_lifecycle_events is append-only; DELETE is never permitted');
END;
