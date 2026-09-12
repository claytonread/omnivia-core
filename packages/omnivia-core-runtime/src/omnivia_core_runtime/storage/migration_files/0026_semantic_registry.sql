-- Phase 1 Semantic Registry: models, versions, review, publication, consumers,
-- outbox (SR-101).
--
-- Additive only, eighteen tables. Everything except models, current_pointers and
-- consumers is append-only: a version, a change operation, a review decision, an
-- approval and a publication record are facts, never edited once written. Models,
-- current_pointers and consumers are the three mutable relations, and each is
-- guarded by the same fencing predicate 0002 established rather than a weaker one.
--
--   omnivia_semantic_models                      registered model identities
--   omnivia_semantic_model_versions              one immutable version per model
--   omnivia_semantic_version_parents             a version's DAG parent edges
--   omnivia_semantic_version_elements            a version's immutable content
--   omnivia_semantic_current_pointers            the one mutable pointer per model
--   omnivia_semantic_version_activations         append log behind every pointer move
--   omnivia_semantic_change_sets                 a proposed group of operations
--   omnivia_semantic_change_operations           the ordinals inside a change set
--   omnivia_semantic_review_requests             a change set submitted for review
--   omnivia_semantic_review_decisions            the one decision a request receives
--   omnivia_semantic_approval_records            an approval bound to an exact digest
--   omnivia_semantic_consumers                   registered consumer identities
--   omnivia_semantic_consumer_dependencies       a consumer's declared model dependency
--   omnivia_semantic_consumer_supported_ranges   a dependency's supported version range
--   omnivia_semantic_consumer_version_bindings   a consumer bound to an exact version
--   omnivia_semantic_publication_records         one publication per idempotency key
--   omnivia_semantic_outbox                      per-aggregate contiguous event log
--   omnivia_semantic_outbox_dispatches           append-only dispatch acknowledgements
--
-- *The current pointer moves only through its activation log.* Exactly the pattern
-- 0011 established for the projection ledger: `omnivia_semantic_version_activations`
-- is the append-only record of every pointer move, and its AFTER INSERT trigger is
-- the only path that advances `omnivia_semantic_current_pointers`. The pointer's own
-- BEFORE UPDATE guard then requires a matching activation row at the new generation,
-- so no writer can move the pointer by any other route, and the generation column is
-- what makes "increment exactly by one" a provable arithmetic fact rather than a
-- convention. A model's pointer row is created alongside the model itself, at
-- generation zero with no current version, precisely as the ledger row is created
-- alongside a registered projection.
--
-- *A change set's ordinals, an outbox aggregate's sequence, and a dispatch's number
-- are all contiguous counters*, exactly as attempt numbers and dispatch numbers are
-- everywhere else in this schema: gaps would make "how many operations are in this
-- change set" and "has this aggregate lost an event" unanswerable from the row
-- count alone.
--
-- *One semantic proposal per model is one row.* `UNIQUE (workspace_id, model_id,
-- change_set_digest)` on `omnivia_semantic_change_sets` means two concurrent callers
-- proposing the same base and the same canonically ordered operations resolve to the
-- one change set that digest already names, never to two rows racing each other.
--
-- *An approval binds the exact change-set digest it approved.* A change set is
-- immutable once written, so `approval_records.change_set_digest` is checked against
-- the change set's own stored digest rather than trusted from the caller; a change
-- set that could be edited after approval would make that binding meaningless.
--
-- *Publication idempotency is one key, one outcome.* `UNIQUE (workspace_id,
-- idempotency_key)` on `omnivia_semantic_publication_records` is the same replay
-- contract 0023 uses for effect intents: the same key delivered twice is one
-- publication, and the row records the request digest, the base and resulting
-- versions, the pointer generation the caller expected and the one it produced, and
-- the approval and validation digests behind it, so a replay can be verified against
-- what actually happened rather than merely deduplicated.
--
-- *Outbox dispatch acknowledgement is append-only, not a status flip.* Exactly as
-- 0023's effect dispatches never mutate the intent that authorized them, a dispatch
-- acknowledgement is its own row and the outbox event it acknowledges is never
-- touched -- an outbox entry's authority is what was queued, not what was
-- eventually delivered.
--
-- What is deliberately absent. No DML or backfill: every row here is written by the
-- service this schema fences, never by this migration. No soft-delete column on any
-- append-only relation, for the reason every other append-only relation in this
-- database has none. No adapter, transport or credential column on the outbox
-- tables, for the same reason 0023 keeps those out of the effect tables.
--
-- Identity, types, bounds, timestamps and digests follow 0018's, 0021's and 0023's
-- rules exactly. Every comment in this file sits between statements and never
-- inside one, for the fingerprint-loader reason 0018 states.

CREATE TABLE IF NOT EXISTS omnivia_semantic_models (
    workspace_id  TEXT    NOT NULL,
    model_id      TEXT    NOT NULL,
    model_kind    TEXT    NOT NULL,
    created_at_us INTEGER NOT NULL,
    updated_at_us INTEGER NOT NULL,
    archived      INTEGER NOT NULL DEFAULT 0,

    PRIMARY KEY (workspace_id, model_id),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(model_id) = 'text' AND length(model_id) BETWEEN 1 AND 128
           AND model_id GLOB '[A-Za-z0-9]*'
           AND model_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(model_id, char(0)) = 0),
    CHECK (typeof(model_kind) = 'text' AND length(model_kind) BETWEEN 1 AND 128
           AND model_kind GLOB '[a-z]*' AND model_kind NOT GLOB '*[^a-z0-9_.]*'
           AND model_kind NOT GLOB '*.' AND model_kind NOT GLOB '*.[^a-z]*'),
    CHECK (typeof(created_at_us) = 'integer' AND created_at_us > 0),
    CHECK (typeof(updated_at_us) = 'integer' AND updated_at_us >= created_at_us),
    CHECK (typeof(archived) = 'integer' AND archived IN (0, 1))
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_semantic_model_versions (
    workspace_id    TEXT    NOT NULL,
    model_id        TEXT    NOT NULL,
    version_id      TEXT    NOT NULL,
    label           TEXT    NOT NULL,
    sequence        INTEGER NOT NULL,
    content_digest  TEXT    NOT NULL,
    content_json    TEXT    NOT NULL,
    created_at_us   INTEGER NOT NULL,

    PRIMARY KEY (workspace_id, model_id, version_id),
    UNIQUE (workspace_id, model_id, label),
    UNIQUE (workspace_id, model_id, sequence),
    UNIQUE (workspace_id, model_id, content_digest),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(model_id) = 'text' AND length(model_id) BETWEEN 1 AND 128
           AND model_id GLOB '[A-Za-z0-9]*'
           AND model_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(model_id, char(0)) = 0),
    CHECK (typeof(version_id) = 'text' AND length(version_id) BETWEEN 1 AND 128
           AND version_id GLOB '[A-Za-z0-9]*'
           AND version_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(version_id, char(0)) = 0),
    CHECK (typeof(label) = 'text' AND length(label) BETWEEN 1 AND 128
           AND label GLOB '[A-Za-z0-9]*'
           AND label NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(label, char(0)) = 0),
    CHECK (typeof(sequence) = 'integer' AND sequence >= 0),
    CHECK (typeof(content_digest) = 'text' AND length(content_digest) = 71
           AND substr(content_digest, 1, 7) = 'sha256:'
           AND substr(content_digest, 8) NOT GLOB '*[^0-9a-f]*'),
    CHECK (typeof(content_json) = 'text'
           AND length(CAST(content_json AS BLOB)) BETWEEN 2 AND 1048576
           AND json_valid(content_json) = 1 AND json(content_json) = content_json),
    CHECK (typeof(created_at_us) = 'integer' AND created_at_us > 0),

    FOREIGN KEY (workspace_id, model_id)
        REFERENCES omnivia_semantic_models (workspace_id, model_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_semantic_version_parents (
    workspace_id     TEXT NOT NULL,
    model_id         TEXT NOT NULL,
    version_id       TEXT NOT NULL,
    parent_version_id TEXT NOT NULL,

    PRIMARY KEY (workspace_id, model_id, version_id, parent_version_id),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(model_id) = 'text' AND length(model_id) BETWEEN 1 AND 128
           AND model_id GLOB '[A-Za-z0-9]*'
           AND model_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(model_id, char(0)) = 0),
    CHECK (typeof(version_id) = 'text' AND length(version_id) BETWEEN 1 AND 128
           AND version_id GLOB '[A-Za-z0-9]*'
           AND version_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(version_id, char(0)) = 0),
    CHECK (typeof(parent_version_id) = 'text'
           AND length(parent_version_id) BETWEEN 1 AND 128
           AND parent_version_id GLOB '[A-Za-z0-9]*'
           AND parent_version_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(parent_version_id, char(0)) = 0),
    CHECK (parent_version_id <> version_id),

    FOREIGN KEY (workspace_id, model_id, version_id)
        REFERENCES omnivia_semantic_model_versions (workspace_id, model_id, version_id),
    FOREIGN KEY (workspace_id, model_id, parent_version_id)
        REFERENCES omnivia_semantic_model_versions (workspace_id, model_id, version_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_semantic_version_elements (
    workspace_id    TEXT    NOT NULL,
    model_id        TEXT    NOT NULL,
    version_id      TEXT    NOT NULL,
    element_id      TEXT    NOT NULL,
    element_json    TEXT    NOT NULL,
    element_digest  TEXT    NOT NULL,

    PRIMARY KEY (workspace_id, model_id, version_id, element_id),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(model_id) = 'text' AND length(model_id) BETWEEN 1 AND 128
           AND model_id GLOB '[A-Za-z0-9]*'
           AND model_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(model_id, char(0)) = 0),
    CHECK (typeof(version_id) = 'text' AND length(version_id) BETWEEN 1 AND 128
           AND version_id GLOB '[A-Za-z0-9]*'
           AND version_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(version_id, char(0)) = 0),
    CHECK (typeof(element_id) = 'text' AND length(element_id) BETWEEN 1 AND 128
           AND element_id GLOB '[A-Za-z0-9]*'
           AND element_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(element_id, char(0)) = 0),
    CHECK (typeof(element_json) = 'text'
           AND length(CAST(element_json AS BLOB)) BETWEEN 2 AND 1048576
           AND json_valid(element_json) = 1 AND json(element_json) = element_json),
    CHECK (typeof(element_digest) = 'text' AND length(element_digest) = 71
           AND substr(element_digest, 1, 7) = 'sha256:'
           AND substr(element_digest, 8) NOT GLOB '*[^0-9a-f]*'),

    FOREIGN KEY (workspace_id, model_id, version_id)
        REFERENCES omnivia_semantic_model_versions (workspace_id, model_id, version_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_semantic_current_pointers (
    workspace_id        TEXT    NOT NULL,
    model_id            TEXT    NOT NULL,
    current_version_id  TEXT,
    generation          INTEGER NOT NULL,
    updated_at_us       INTEGER NOT NULL,

    PRIMARY KEY (workspace_id, model_id),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(model_id) = 'text' AND length(model_id) BETWEEN 1 AND 128
           AND model_id GLOB '[A-Za-z0-9]*'
           AND model_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(model_id, char(0)) = 0),
    CHECK (current_version_id IS NULL OR (typeof(current_version_id) = 'text'
           AND length(current_version_id) BETWEEN 1 AND 128
           AND current_version_id GLOB '[A-Za-z0-9]*'
           AND current_version_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(current_version_id, char(0)) = 0)),
    CHECK (typeof(generation) = 'integer' AND generation >= 0),
    CHECK ((generation = 0 AND current_version_id IS NULL)
           OR (generation > 0 AND current_version_id IS NOT NULL)),
    CHECK (typeof(updated_at_us) = 'integer' AND updated_at_us > 0),

    FOREIGN KEY (workspace_id, model_id)
        REFERENCES omnivia_semantic_models (workspace_id, model_id),
    FOREIGN KEY (workspace_id, model_id, current_version_id)
        REFERENCES omnivia_semantic_model_versions (workspace_id, model_id, version_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_semantic_version_activations (
    workspace_id        TEXT    NOT NULL,
    model_id            TEXT    NOT NULL,
    activation_sequence INTEGER NOT NULL,
    version_id          TEXT    NOT NULL,
    previous_version_id TEXT,
    generation          INTEGER NOT NULL,
    activated_at_us     INTEGER NOT NULL,
    audit_ref           TEXT    NOT NULL,

    PRIMARY KEY (workspace_id, model_id, activation_sequence),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(model_id) = 'text' AND length(model_id) BETWEEN 1 AND 128
           AND model_id GLOB '[A-Za-z0-9]*'
           AND model_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(model_id, char(0)) = 0),
    CHECK (typeof(activation_sequence) = 'integer' AND activation_sequence >= 0),
    CHECK (typeof(version_id) = 'text' AND length(version_id) BETWEEN 1 AND 128
           AND version_id GLOB '[A-Za-z0-9]*'
           AND version_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(version_id, char(0)) = 0),
    CHECK (previous_version_id IS NULL OR (typeof(previous_version_id) = 'text'
           AND length(previous_version_id) BETWEEN 1 AND 128
           AND previous_version_id GLOB '[A-Za-z0-9]*'
           AND previous_version_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(previous_version_id, char(0)) = 0)),
    CHECK (typeof(generation) = 'integer' AND generation > 0),
    CHECK (typeof(activated_at_us) = 'integer' AND activated_at_us > 0),
    CHECK (typeof(audit_ref) = 'text' AND length(audit_ref) BETWEEN 1 AND 128
           AND audit_ref GLOB '[A-Za-z0-9]*'
           AND audit_ref NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(audit_ref, char(0)) = 0),

    FOREIGN KEY (workspace_id, model_id, version_id)
        REFERENCES omnivia_semantic_model_versions (workspace_id, model_id, version_id),
    FOREIGN KEY (workspace_id, model_id, previous_version_id)
        REFERENCES omnivia_semantic_model_versions (workspace_id, model_id, version_id),
    FOREIGN KEY (audit_ref, workspace_id)
        REFERENCES omnivia_application_audit_events (audit_ref, workspace_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_semantic_change_sets (
    workspace_id      TEXT    NOT NULL,
    change_set_id     TEXT    NOT NULL,
    model_id          TEXT    NOT NULL,
    base_version_id   TEXT,
    change_set_digest TEXT    NOT NULL,
    created_at_us     INTEGER NOT NULL,

    PRIMARY KEY (workspace_id, change_set_id),
    UNIQUE (workspace_id, model_id, change_set_digest),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(change_set_id) = 'text' AND length(change_set_id) BETWEEN 1 AND 128
           AND change_set_id GLOB '[A-Za-z0-9]*'
           AND change_set_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(change_set_id, char(0)) = 0),
    CHECK (typeof(model_id) = 'text' AND length(model_id) BETWEEN 1 AND 128
           AND model_id GLOB '[A-Za-z0-9]*'
           AND model_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(model_id, char(0)) = 0),
    CHECK (base_version_id IS NULL OR (typeof(base_version_id) = 'text'
           AND length(base_version_id) BETWEEN 1 AND 128
           AND base_version_id GLOB '[A-Za-z0-9]*'
           AND base_version_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(base_version_id, char(0)) = 0)),
    CHECK (typeof(change_set_digest) = 'text' AND length(change_set_digest) = 71
           AND substr(change_set_digest, 1, 7) = 'sha256:'
           AND substr(change_set_digest, 8) NOT GLOB '*[^0-9a-f]*'),
    CHECK (typeof(created_at_us) = 'integer' AND created_at_us > 0),

    FOREIGN KEY (workspace_id, model_id)
        REFERENCES omnivia_semantic_models (workspace_id, model_id),
    FOREIGN KEY (workspace_id, model_id, base_version_id)
        REFERENCES omnivia_semantic_model_versions (workspace_id, model_id, version_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_semantic_change_operations (
    workspace_id      TEXT    NOT NULL,
    change_set_id     TEXT    NOT NULL,
    ordinal           INTEGER NOT NULL,
    operation_json    TEXT    NOT NULL,
    operation_digest  TEXT    NOT NULL,

    PRIMARY KEY (workspace_id, change_set_id, ordinal),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(change_set_id) = 'text' AND length(change_set_id) BETWEEN 1 AND 128
           AND change_set_id GLOB '[A-Za-z0-9]*'
           AND change_set_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(change_set_id, char(0)) = 0),
    CHECK (typeof(ordinal) = 'integer' AND ordinal >= 0),
    CHECK (typeof(operation_json) = 'text'
           AND length(CAST(operation_json AS BLOB)) BETWEEN 2 AND 1048576
           AND json_valid(operation_json) = 1 AND json(operation_json) = operation_json),
    CHECK (typeof(operation_digest) = 'text' AND length(operation_digest) = 71
           AND substr(operation_digest, 1, 7) = 'sha256:'
           AND substr(operation_digest, 8) NOT GLOB '*[^0-9a-f]*'),

    FOREIGN KEY (workspace_id, change_set_id)
        REFERENCES omnivia_semantic_change_sets (workspace_id, change_set_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_semantic_review_requests (
    workspace_id       TEXT    NOT NULL,
    review_request_id  TEXT    NOT NULL,
    change_set_id      TEXT    NOT NULL,
    requested_at_us    INTEGER NOT NULL,

    PRIMARY KEY (workspace_id, review_request_id),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(review_request_id) = 'text'
           AND length(review_request_id) BETWEEN 1 AND 128
           AND review_request_id GLOB '[A-Za-z0-9]*'
           AND review_request_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(review_request_id, char(0)) = 0),
    CHECK (typeof(change_set_id) = 'text' AND length(change_set_id) BETWEEN 1 AND 128
           AND change_set_id GLOB '[A-Za-z0-9]*'
           AND change_set_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(change_set_id, char(0)) = 0),
    CHECK (typeof(requested_at_us) = 'integer' AND requested_at_us > 0),

    FOREIGN KEY (workspace_id, change_set_id)
        REFERENCES omnivia_semantic_change_sets (workspace_id, change_set_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_semantic_review_decisions (
    workspace_id       TEXT    NOT NULL,
    review_decision_id TEXT    NOT NULL,
    review_request_id  TEXT    NOT NULL,
    reviewer_id        TEXT    NOT NULL,
    decision           TEXT    NOT NULL,
    decided_at_us      INTEGER NOT NULL,

    PRIMARY KEY (workspace_id, review_decision_id),
    UNIQUE (workspace_id, review_request_id),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(review_decision_id) = 'text'
           AND length(review_decision_id) BETWEEN 1 AND 128
           AND review_decision_id GLOB '[A-Za-z0-9]*'
           AND review_decision_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(review_decision_id, char(0)) = 0),
    CHECK (typeof(review_request_id) = 'text'
           AND length(review_request_id) BETWEEN 1 AND 128
           AND review_request_id GLOB '[A-Za-z0-9]*'
           AND review_request_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(review_request_id, char(0)) = 0),
    CHECK (typeof(reviewer_id) = 'text' AND length(reviewer_id) BETWEEN 1 AND 128
           AND reviewer_id GLOB '[A-Za-z0-9]*'
           AND reviewer_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(reviewer_id, char(0)) = 0),
    CHECK (decision IN ('approved', 'rejected')),
    CHECK (typeof(decided_at_us) = 'integer' AND decided_at_us > 0),

    FOREIGN KEY (workspace_id, review_request_id)
        REFERENCES omnivia_semantic_review_requests (workspace_id, review_request_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_semantic_approval_records (
    workspace_id       TEXT    NOT NULL,
    approval_id        TEXT    NOT NULL,
    change_set_id      TEXT    NOT NULL,
    change_set_digest  TEXT    NOT NULL,
    review_decision_id TEXT    NOT NULL,
    approved_at_us     INTEGER NOT NULL,

    PRIMARY KEY (workspace_id, approval_id),
    UNIQUE (workspace_id, change_set_id),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(approval_id) = 'text' AND length(approval_id) BETWEEN 1 AND 128
           AND approval_id GLOB '[A-Za-z0-9]*'
           AND approval_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(approval_id, char(0)) = 0),
    CHECK (typeof(change_set_id) = 'text' AND length(change_set_id) BETWEEN 1 AND 128
           AND change_set_id GLOB '[A-Za-z0-9]*'
           AND change_set_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(change_set_id, char(0)) = 0),
    CHECK (typeof(change_set_digest) = 'text' AND length(change_set_digest) = 71
           AND substr(change_set_digest, 1, 7) = 'sha256:'
           AND substr(change_set_digest, 8) NOT GLOB '*[^0-9a-f]*'),
    CHECK (typeof(review_decision_id) = 'text'
           AND length(review_decision_id) BETWEEN 1 AND 128
           AND review_decision_id GLOB '[A-Za-z0-9]*'
           AND review_decision_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(review_decision_id, char(0)) = 0),
    CHECK (typeof(approved_at_us) = 'integer' AND approved_at_us > 0),

    FOREIGN KEY (workspace_id, change_set_id)
        REFERENCES omnivia_semantic_change_sets (workspace_id, change_set_id),
    FOREIGN KEY (workspace_id, review_decision_id)
        REFERENCES omnivia_semantic_review_decisions (workspace_id, review_decision_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_semantic_consumers (
    workspace_id  TEXT    NOT NULL,
    consumer_id   TEXT    NOT NULL,
    created_at_us INTEGER NOT NULL,
    updated_at_us INTEGER NOT NULL,
    archived      INTEGER NOT NULL DEFAULT 0,

    PRIMARY KEY (workspace_id, consumer_id),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(consumer_id) = 'text' AND length(consumer_id) BETWEEN 1 AND 128
           AND consumer_id GLOB '[A-Za-z0-9]*'
           AND consumer_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(consumer_id, char(0)) = 0),
    CHECK (typeof(created_at_us) = 'integer' AND created_at_us > 0),
    CHECK (typeof(updated_at_us) = 'integer' AND updated_at_us >= created_at_us),
    CHECK (typeof(archived) = 'integer' AND archived IN (0, 1))
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_semantic_consumer_dependencies (
    workspace_id   TEXT    NOT NULL,
    consumer_id    TEXT    NOT NULL,
    model_id       TEXT    NOT NULL,
    declared_at_us INTEGER NOT NULL,

    PRIMARY KEY (workspace_id, consumer_id, model_id),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(consumer_id) = 'text' AND length(consumer_id) BETWEEN 1 AND 128
           AND consumer_id GLOB '[A-Za-z0-9]*'
           AND consumer_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(consumer_id, char(0)) = 0),
    CHECK (typeof(model_id) = 'text' AND length(model_id) BETWEEN 1 AND 128
           AND model_id GLOB '[A-Za-z0-9]*'
           AND model_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(model_id, char(0)) = 0),
    CHECK (typeof(declared_at_us) = 'integer' AND declared_at_us > 0),

    FOREIGN KEY (workspace_id, consumer_id)
        REFERENCES omnivia_semantic_consumers (workspace_id, consumer_id),
    FOREIGN KEY (workspace_id, model_id)
        REFERENCES omnivia_semantic_models (workspace_id, model_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_semantic_consumer_supported_ranges (
    workspace_id  TEXT    NOT NULL,
    consumer_id   TEXT    NOT NULL,
    model_id      TEXT    NOT NULL,
    min_sequence  INTEGER NOT NULL,
    max_sequence  INTEGER,
    declared_at_us INTEGER NOT NULL,

    PRIMARY KEY (workspace_id, consumer_id, model_id),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(consumer_id) = 'text' AND length(consumer_id) BETWEEN 1 AND 128
           AND consumer_id GLOB '[A-Za-z0-9]*'
           AND consumer_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(consumer_id, char(0)) = 0),
    CHECK (typeof(model_id) = 'text' AND length(model_id) BETWEEN 1 AND 128
           AND model_id GLOB '[A-Za-z0-9]*'
           AND model_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(model_id, char(0)) = 0),
    CHECK (typeof(min_sequence) = 'integer' AND min_sequence >= 0),
    CHECK (max_sequence IS NULL OR (typeof(max_sequence) = 'integer'
           AND max_sequence >= min_sequence)),
    CHECK (typeof(declared_at_us) = 'integer' AND declared_at_us > 0),

    FOREIGN KEY (workspace_id, consumer_id, model_id)
        REFERENCES omnivia_semantic_consumer_dependencies
            (workspace_id, consumer_id, model_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_semantic_consumer_version_bindings (
    workspace_id  TEXT    NOT NULL,
    consumer_id   TEXT    NOT NULL,
    model_id      TEXT    NOT NULL,
    version_id    TEXT    NOT NULL,
    bound_at_us   INTEGER NOT NULL,

    PRIMARY KEY (workspace_id, consumer_id, model_id, version_id),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(consumer_id) = 'text' AND length(consumer_id) BETWEEN 1 AND 128
           AND consumer_id GLOB '[A-Za-z0-9]*'
           AND consumer_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(consumer_id, char(0)) = 0),
    CHECK (typeof(model_id) = 'text' AND length(model_id) BETWEEN 1 AND 128
           AND model_id GLOB '[A-Za-z0-9]*'
           AND model_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(model_id, char(0)) = 0),
    CHECK (typeof(version_id) = 'text' AND length(version_id) BETWEEN 1 AND 128
           AND version_id GLOB '[A-Za-z0-9]*'
           AND version_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(version_id, char(0)) = 0),
    CHECK (typeof(bound_at_us) = 'integer' AND bound_at_us > 0),

    FOREIGN KEY (workspace_id, consumer_id, model_id)
        REFERENCES omnivia_semantic_consumer_dependencies
            (workspace_id, consumer_id, model_id),
    FOREIGN KEY (workspace_id, model_id, version_id)
        REFERENCES omnivia_semantic_model_versions (workspace_id, model_id, version_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_semantic_publication_records (
    workspace_id                TEXT    NOT NULL,
    publication_id               TEXT    NOT NULL,
    idempotency_key               TEXT    NOT NULL,
    request_digest                TEXT    NOT NULL,
    model_id                      TEXT    NOT NULL,
    base_version_id               TEXT,
    result_version_id             TEXT    NOT NULL,
    expected_pointer_generation   INTEGER NOT NULL,
    resulting_pointer_generation  INTEGER NOT NULL,
    approval_id                   TEXT    NOT NULL,
    validation_digest             TEXT    NOT NULL,
    published_at_us               INTEGER NOT NULL,

    PRIMARY KEY (workspace_id, publication_id),
    UNIQUE (workspace_id, idempotency_key),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(publication_id) = 'text' AND length(publication_id) BETWEEN 1 AND 128
           AND publication_id GLOB '[A-Za-z0-9]*'
           AND publication_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(publication_id, char(0)) = 0),
    CHECK (typeof(idempotency_key) = 'text'
           AND length(idempotency_key) BETWEEN 1 AND 128
           AND idempotency_key GLOB '[A-Za-z0-9]*'
           AND idempotency_key NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(idempotency_key, char(0)) = 0),
    CHECK (typeof(request_digest) = 'text' AND length(request_digest) = 71
           AND substr(request_digest, 1, 7) = 'sha256:'
           AND substr(request_digest, 8) NOT GLOB '*[^0-9a-f]*'),
    CHECK (typeof(model_id) = 'text' AND length(model_id) BETWEEN 1 AND 128
           AND model_id GLOB '[A-Za-z0-9]*'
           AND model_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(model_id, char(0)) = 0),
    CHECK (base_version_id IS NULL OR (typeof(base_version_id) = 'text'
           AND length(base_version_id) BETWEEN 1 AND 128
           AND base_version_id GLOB '[A-Za-z0-9]*'
           AND base_version_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(base_version_id, char(0)) = 0)),
    CHECK (typeof(result_version_id) = 'text'
           AND length(result_version_id) BETWEEN 1 AND 128
           AND result_version_id GLOB '[A-Za-z0-9]*'
           AND result_version_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(result_version_id, char(0)) = 0),
    CHECK (typeof(expected_pointer_generation) = 'integer'
           AND expected_pointer_generation >= 0),
    CHECK (typeof(resulting_pointer_generation) = 'integer'
           AND resulting_pointer_generation = expected_pointer_generation + 1),
    CHECK (typeof(approval_id) = 'text' AND length(approval_id) BETWEEN 1 AND 128
           AND approval_id GLOB '[A-Za-z0-9]*'
           AND approval_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(approval_id, char(0)) = 0),
    CHECK (typeof(validation_digest) = 'text' AND length(validation_digest) = 71
           AND substr(validation_digest, 1, 7) = 'sha256:'
           AND substr(validation_digest, 8) NOT GLOB '*[^0-9a-f]*'),
    CHECK (typeof(published_at_us) = 'integer' AND published_at_us > 0),

    FOREIGN KEY (workspace_id, model_id, base_version_id)
        REFERENCES omnivia_semantic_model_versions (workspace_id, model_id, version_id),
    FOREIGN KEY (workspace_id, model_id, result_version_id)
        REFERENCES omnivia_semantic_model_versions (workspace_id, model_id, version_id),
    FOREIGN KEY (workspace_id, approval_id)
        REFERENCES omnivia_semantic_approval_records (workspace_id, approval_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_semantic_outbox (
    workspace_id   TEXT    NOT NULL,
    aggregate_id   TEXT    NOT NULL,
    sequence       INTEGER NOT NULL,
    outbox_id      TEXT    NOT NULL,
    event_kind     TEXT    NOT NULL,
    payload_json   TEXT    NOT NULL,
    payload_digest TEXT    NOT NULL,
    created_at_us  INTEGER NOT NULL,

    PRIMARY KEY (workspace_id, aggregate_id, sequence),
    UNIQUE (workspace_id, outbox_id),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(aggregate_id) = 'text' AND length(aggregate_id) BETWEEN 1 AND 128
           AND aggregate_id GLOB '[A-Za-z0-9]*'
           AND aggregate_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(aggregate_id, char(0)) = 0),
    CHECK (typeof(sequence) = 'integer' AND sequence >= 0),
    CHECK (typeof(outbox_id) = 'text' AND length(outbox_id) BETWEEN 1 AND 128
           AND outbox_id GLOB '[A-Za-z0-9]*'
           AND outbox_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(outbox_id, char(0)) = 0),
    CHECK (typeof(event_kind) = 'text' AND length(event_kind) BETWEEN 1 AND 128
           AND event_kind GLOB '[a-z]*' AND event_kind NOT GLOB '*[^a-z0-9_.]*'
           AND event_kind NOT GLOB '*.' AND event_kind NOT GLOB '*.[^a-z]*'),
    CHECK (typeof(payload_json) = 'text'
           AND length(CAST(payload_json AS BLOB)) BETWEEN 2 AND 1048576
           AND json_valid(payload_json) = 1 AND json(payload_json) = payload_json),
    CHECK (typeof(payload_digest) = 'text' AND length(payload_digest) = 71
           AND substr(payload_digest, 1, 7) = 'sha256:'
           AND substr(payload_digest, 8) NOT GLOB '*[^0-9a-f]*'),
    CHECK (typeof(created_at_us) = 'integer' AND created_at_us > 0)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_semantic_outbox_dispatches (
    workspace_id      TEXT    NOT NULL,
    outbox_id         TEXT    NOT NULL,
    dispatch_number   INTEGER NOT NULL,
    acknowledged_at_us INTEGER NOT NULL,

    PRIMARY KEY (workspace_id, outbox_id, dispatch_number),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(outbox_id) = 'text' AND length(outbox_id) BETWEEN 1 AND 128
           AND outbox_id GLOB '[A-Za-z0-9]*'
           AND outbox_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(outbox_id, char(0)) = 0),
    CHECK (typeof(dispatch_number) = 'integer' AND dispatch_number BETWEEN 1 AND 256),
    CHECK (typeof(acknowledged_at_us) = 'integer' AND acknowledged_at_us > 0),

    FOREIGN KEY (workspace_id, outbox_id)
        REFERENCES omnivia_semantic_outbox (workspace_id, outbox_id)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS omnivia_idx_semantic_versions_model_sequence
    ON omnivia_semantic_model_versions (workspace_id, model_id, sequence DESC);
CREATE INDEX IF NOT EXISTS omnivia_idx_semantic_change_ops_change_set
    ON omnivia_semantic_change_operations (workspace_id, change_set_id, ordinal);
CREATE INDEX IF NOT EXISTS omnivia_idx_semantic_outbox_created
    ON omnivia_semantic_outbox (workspace_id, created_at_us);

-- Every guard below repeats the complete connection-authority, mutation-guard,
-- workspace-state and lease predicate 0002/0025 established: the current service
-- writer, a mutation-guard row matching the workspace's fencing generation, a held
-- lease at that same generation, and the new row's workspace matching the
-- authoritative singleton workspace.

CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_models_insert
BEFORE INSERT ON omnivia_semantic_models
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_semantic_models')
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
    SELECT RAISE(ABORT, 'omnivia: model created_at and updated_at must agree at insert')
    WHERE NEW.updated_at_us IS NOT NEW.created_at_us;
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_models_update
BEFORE UPDATE ON omnivia_semantic_models
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded UPDATE on omnivia_semantic_models')
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
       OR NEW.workspace_id IS NOT OLD.workspace_id;
    SELECT RAISE(ABORT, 'omnivia: model identity and creation time are immutable')
    WHERE NEW.model_id IS NOT OLD.model_id
       OR NEW.model_kind IS NOT OLD.model_kind
       OR NEW.created_at_us IS NOT OLD.created_at_us;
    SELECT RAISE(ABORT, 'omnivia: model update time must not regress')
    WHERE NEW.updated_at_us < OLD.updated_at_us;
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_models_delete
BEFORE DELETE ON omnivia_semantic_models
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded DELETE on omnivia_semantic_models')
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
       OR OLD.workspace_id IS NOT (
            SELECT workspace_id FROM omnivia_workspace_state WHERE singleton = 1);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_model_versions_insert
BEFORE INSERT ON omnivia_semantic_model_versions
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_semantic_model_versions')
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
    SELECT RAISE(ABORT, 'omnivia: version sequence must be contiguous within its model')
    WHERE NEW.sequence IS NOT (
        SELECT COALESCE(MAX(sequence), -1) + 1 FROM omnivia_semantic_model_versions
        WHERE workspace_id = NEW.workspace_id AND model_id = NEW.model_id);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_model_versions_update
BEFORE UPDATE ON omnivia_semantic_model_versions
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_semantic_model_versions is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_model_versions_delete
BEFORE DELETE ON omnivia_semantic_model_versions
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_semantic_model_versions is append-only; DELETE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_version_parents_insert
BEFORE INSERT ON omnivia_semantic_version_parents
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_semantic_version_parents')
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

CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_version_parents_update
BEFORE UPDATE ON omnivia_semantic_version_parents
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_semantic_version_parents is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_version_parents_delete
BEFORE DELETE ON omnivia_semantic_version_parents
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_semantic_version_parents is append-only; DELETE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_version_elements_insert
BEFORE INSERT ON omnivia_semantic_version_elements
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_semantic_version_elements')
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

CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_version_elements_update
BEFORE UPDATE ON omnivia_semantic_version_elements
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_semantic_version_elements is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_version_elements_delete
BEFORE DELETE ON omnivia_semantic_version_elements
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_semantic_version_elements is append-only; DELETE is never permitted');
END;

-- omnivia_semantic_current_pointers is created at generation zero when a model is
-- registered, and thereafter moves only through a matching
-- omnivia_semantic_version_activations row, exactly as 0011's projection ledger does.

CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_current_pointers_insert
BEFORE INSERT ON omnivia_semantic_current_pointers
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_semantic_current_pointers')
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
    SELECT RAISE(ABORT, 'omnivia: a current pointer must be created at generation zero')
    WHERE NEW.generation <> 0 OR NEW.current_version_id IS NOT NULL;
    SELECT RAISE(ABORT, 'omnivia: a current pointer must name a registered model')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_semantic_models
        WHERE workspace_id = NEW.workspace_id AND model_id = NEW.model_id);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_current_pointers_update
BEFORE UPDATE ON omnivia_semantic_current_pointers
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded UPDATE on omnivia_semantic_current_pointers')
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
       OR NEW.workspace_id IS NOT OLD.workspace_id;
    SELECT RAISE(ABORT, 'omnivia: a current pointer never changes model identity')
    WHERE NEW.model_id IS NOT OLD.model_id;
    SELECT RAISE(ABORT, 'omnivia: a current pointer generation must advance by exactly one')
    WHERE NEW.generation IS NOT OLD.generation + 1;
    SELECT RAISE(ABORT, 'omnivia: a current pointer version must belong to its own model')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_semantic_model_versions
        WHERE workspace_id = NEW.workspace_id AND model_id = NEW.model_id
          AND version_id = NEW.current_version_id);
    SELECT RAISE(ABORT, 'omnivia: a current pointer move requires a matching activation entry')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_semantic_version_activations a
        WHERE a.workspace_id = NEW.workspace_id AND a.model_id = NEW.model_id
          AND a.version_id = NEW.current_version_id
          AND a.previous_version_id IS OLD.current_version_id
          AND a.generation = NEW.generation
          AND a.activated_at_us = NEW.updated_at_us
          AND a.activation_sequence = (
              SELECT MAX(activation_sequence) FROM omnivia_semantic_version_activations
              WHERE workspace_id = a.workspace_id AND model_id = a.model_id));
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_current_pointers_delete
BEFORE DELETE ON omnivia_semantic_current_pointers
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded DELETE on omnivia_semantic_current_pointers')
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
       OR OLD.workspace_id IS NOT (
            SELECT workspace_id FROM omnivia_workspace_state WHERE singleton = 1);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_version_activations_insert
BEFORE INSERT ON omnivia_semantic_version_activations
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_semantic_version_activations')
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
    SELECT RAISE(ABORT, 'omnivia: activation sequence must be contiguous from zero')
    WHERE NEW.activation_sequence IS NOT (
        SELECT COALESCE(MAX(activation_sequence), -1) + 1
        FROM omnivia_semantic_version_activations
        WHERE workspace_id = NEW.workspace_id AND model_id = NEW.model_id);
    SELECT RAISE(ABORT, 'omnivia: activation generation must be contiguous from one')
    WHERE NEW.generation IS NOT NEW.activation_sequence + 1;
    SELECT RAISE(ABORT, 'omnivia: activation predecessor does not match current pointer')
    WHERE NEW.previous_version_id IS NOT (
        SELECT current_version_id FROM omnivia_semantic_current_pointers
        WHERE workspace_id = NEW.workspace_id AND model_id = NEW.model_id);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_apply_semantic_version_activation
AFTER INSERT ON omnivia_semantic_version_activations
BEGIN
    SELECT 'paired BEFORE INSERT ON omnivia_semantic_version_activations guard' WHERE 0;
    UPDATE omnivia_semantic_current_pointers
       SET current_version_id = NEW.version_id,
           generation = NEW.generation,
           updated_at_us = NEW.activated_at_us
     WHERE workspace_id = NEW.workspace_id AND model_id = NEW.model_id;
    SELECT RAISE(ABORT, 'omnivia: activation pointer was not updated')
    WHERE changes() <> 1;
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_version_activations_update
BEFORE UPDATE ON omnivia_semantic_version_activations
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_semantic_version_activations is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_version_activations_delete
BEFORE DELETE ON omnivia_semantic_version_activations
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_semantic_version_activations is append-only; DELETE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_change_sets_insert
BEFORE INSERT ON omnivia_semantic_change_sets
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_semantic_change_sets')
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

CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_change_sets_update
BEFORE UPDATE ON omnivia_semantic_change_sets
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_semantic_change_sets is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_change_sets_delete
BEFORE DELETE ON omnivia_semantic_change_sets
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_semantic_change_sets is append-only; DELETE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_change_operations_insert
BEFORE INSERT ON omnivia_semantic_change_operations
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_semantic_change_operations')
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
    SELECT RAISE(ABORT, 'omnivia: change operation ordinal must be contiguous from zero')
    WHERE NEW.ordinal IS NOT (
        SELECT COALESCE(MAX(ordinal), -1) + 1 FROM omnivia_semantic_change_operations
        WHERE workspace_id = NEW.workspace_id AND change_set_id = NEW.change_set_id);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_change_operations_update
BEFORE UPDATE ON omnivia_semantic_change_operations
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_semantic_change_operations is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_change_operations_delete
BEFORE DELETE ON omnivia_semantic_change_operations
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_semantic_change_operations is append-only; DELETE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_review_requests_insert
BEFORE INSERT ON omnivia_semantic_review_requests
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_semantic_review_requests')
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

CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_review_requests_update
BEFORE UPDATE ON omnivia_semantic_review_requests
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_semantic_review_requests is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_review_requests_delete
BEFORE DELETE ON omnivia_semantic_review_requests
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_semantic_review_requests is append-only; DELETE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_review_decisions_insert
BEFORE INSERT ON omnivia_semantic_review_decisions
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_semantic_review_decisions')
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

CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_review_decisions_update
BEFORE UPDATE ON omnivia_semantic_review_decisions
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_semantic_review_decisions is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_review_decisions_delete
BEFORE DELETE ON omnivia_semantic_review_decisions
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_semantic_review_decisions is append-only; DELETE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_approval_records_insert
BEFORE INSERT ON omnivia_semantic_approval_records
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_semantic_approval_records')
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
    SELECT RAISE(ABORT, 'omnivia: an approval must bind the change set''s own stored digest')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_semantic_change_sets
        WHERE workspace_id = NEW.workspace_id AND change_set_id = NEW.change_set_id
          AND change_set_digest = NEW.change_set_digest);
    SELECT RAISE(ABORT, 'omnivia: an approval must rest on an approved review decision for its change set')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_semantic_review_decisions d
        JOIN omnivia_semantic_review_requests r
          ON r.workspace_id = d.workspace_id AND r.review_request_id = d.review_request_id
        WHERE d.workspace_id = NEW.workspace_id
          AND d.review_decision_id = NEW.review_decision_id
          AND d.decision = 'approved'
          AND r.change_set_id = NEW.change_set_id);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_approval_records_update
BEFORE UPDATE ON omnivia_semantic_approval_records
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_semantic_approval_records is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_approval_records_delete
BEFORE DELETE ON omnivia_semantic_approval_records
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_semantic_approval_records is append-only; DELETE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_consumers_insert
BEFORE INSERT ON omnivia_semantic_consumers
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_semantic_consumers')
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
    SELECT RAISE(ABORT, 'omnivia: consumer created_at and updated_at must agree at insert')
    WHERE NEW.updated_at_us IS NOT NEW.created_at_us;
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_consumers_update
BEFORE UPDATE ON omnivia_semantic_consumers
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded UPDATE on omnivia_semantic_consumers')
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
       OR NEW.workspace_id IS NOT OLD.workspace_id;
    SELECT RAISE(ABORT, 'omnivia: consumer identity and creation time are immutable')
    WHERE NEW.consumer_id IS NOT OLD.consumer_id
       OR NEW.created_at_us IS NOT OLD.created_at_us;
    SELECT RAISE(ABORT, 'omnivia: consumer update time must not regress')
    WHERE NEW.updated_at_us < OLD.updated_at_us;
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_consumers_delete
BEFORE DELETE ON omnivia_semantic_consumers
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded DELETE on omnivia_semantic_consumers')
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
       OR OLD.workspace_id IS NOT (
            SELECT workspace_id FROM omnivia_workspace_state WHERE singleton = 1);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_consumer_dependencies_insert
BEFORE INSERT ON omnivia_semantic_consumer_dependencies
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_semantic_consumer_dependencies')
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
    SELECT RAISE(ABORT, 'omnivia: a dependency must name a registered consumer')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_semantic_consumers
        WHERE workspace_id = NEW.workspace_id AND consumer_id = NEW.consumer_id);
    SELECT RAISE(ABORT, 'omnivia: a dependency must name a registered model')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_semantic_models
        WHERE workspace_id = NEW.workspace_id AND model_id = NEW.model_id);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_consumer_dependencies_update
BEFORE UPDATE ON omnivia_semantic_consumer_dependencies
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_semantic_consumer_dependencies is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_consumer_dependencies_delete
BEFORE DELETE ON omnivia_semantic_consumer_dependencies
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_semantic_consumer_dependencies is append-only; DELETE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_consumer_supported_ranges_insert
BEFORE INSERT ON omnivia_semantic_consumer_supported_ranges
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_semantic_consumer_supported_ranges')
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
    SELECT RAISE(ABORT, 'omnivia: a supported range must name a declared dependency')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_semantic_consumer_dependencies
        WHERE workspace_id = NEW.workspace_id AND consumer_id = NEW.consumer_id
          AND model_id = NEW.model_id);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_consumer_supported_ranges_update
BEFORE UPDATE ON omnivia_semantic_consumer_supported_ranges
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_semantic_consumer_supported_ranges is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_consumer_supported_ranges_delete
BEFORE DELETE ON omnivia_semantic_consumer_supported_ranges
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_semantic_consumer_supported_ranges is append-only; DELETE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_consumer_version_bindings_insert
BEFORE INSERT ON omnivia_semantic_consumer_version_bindings
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_semantic_consumer_version_bindings')
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
    SELECT RAISE(ABORT, 'omnivia: a version binding must name a declared dependency')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_semantic_consumer_dependencies
        WHERE workspace_id = NEW.workspace_id AND consumer_id = NEW.consumer_id
          AND model_id = NEW.model_id);
    SELECT RAISE(ABORT, 'omnivia: a version binding must name an exact version of its own model')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_semantic_model_versions
        WHERE workspace_id = NEW.workspace_id AND model_id = NEW.model_id
          AND version_id = NEW.version_id);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_consumer_version_bindings_update
BEFORE UPDATE ON omnivia_semantic_consumer_version_bindings
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_semantic_consumer_version_bindings is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_consumer_version_bindings_delete
BEFORE DELETE ON omnivia_semantic_consumer_version_bindings
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_semantic_consumer_version_bindings is append-only; DELETE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_publication_records_insert
BEFORE INSERT ON omnivia_semantic_publication_records
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_semantic_publication_records')
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
    SELECT RAISE(ABORT, 'omnivia: a publication must cite an approval of its own model change set')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_semantic_approval_records a
        JOIN omnivia_semantic_change_sets c
          ON c.workspace_id = a.workspace_id AND c.change_set_id = a.change_set_id
        WHERE a.workspace_id = NEW.workspace_id AND a.approval_id = NEW.approval_id
          AND c.model_id = NEW.model_id);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_publication_records_update
BEFORE UPDATE ON omnivia_semantic_publication_records
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_semantic_publication_records is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_publication_records_delete
BEFORE DELETE ON omnivia_semantic_publication_records
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_semantic_publication_records is append-only; DELETE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_outbox_insert
BEFORE INSERT ON omnivia_semantic_outbox
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_semantic_outbox')
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
    SELECT RAISE(ABORT, 'omnivia: outbox sequence must be contiguous from zero within its aggregate')
    WHERE NEW.sequence IS NOT (
        SELECT COALESCE(MAX(sequence), -1) + 1 FROM omnivia_semantic_outbox
        WHERE workspace_id = NEW.workspace_id AND aggregate_id = NEW.aggregate_id);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_outbox_update
BEFORE UPDATE ON omnivia_semantic_outbox
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_semantic_outbox is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_outbox_delete
BEFORE DELETE ON omnivia_semantic_outbox
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_semantic_outbox is append-only; DELETE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_outbox_dispatches_insert
BEFORE INSERT ON omnivia_semantic_outbox_dispatches
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_semantic_outbox_dispatches')
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
    SELECT RAISE(ABORT, 'omnivia: dispatch number must be contiguous within its outbox event')
    WHERE NEW.dispatch_number IS NOT (
        SELECT COALESCE(MAX(dispatch_number), 0) + 1
        FROM omnivia_semantic_outbox_dispatches
        WHERE workspace_id = NEW.workspace_id AND outbox_id = NEW.outbox_id);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_outbox_dispatches_update
BEFORE UPDATE ON omnivia_semantic_outbox_dispatches
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_semantic_outbox_dispatches is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_outbox_dispatches_delete
BEFORE DELETE ON omnivia_semantic_outbox_dispatches
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_semantic_outbox_dispatches is append-only; DELETE is never permitted');
END;
