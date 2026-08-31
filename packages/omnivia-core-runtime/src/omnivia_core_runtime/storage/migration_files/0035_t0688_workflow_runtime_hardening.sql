-- T-0688: Workflow Runtime hardening -- runtime bindings, fenced transition bundles,
-- an integrity-linked runtime journal, parity reporting against the existing writer,
-- journal integrity verification, journal quarantine and journal retention boundaries.
--
-- Additive only. Seven append-only tables, nine named indexes and twenty-one
-- statement triggers, on the Durable Workflow Runtime foundation migration 0027
-- already established. No existing object is rebuilt or altered, and this migration
-- performs no DML: every `omnivia_workflow_runs` row already durable remains without
-- a binding until a later write records one. This migration promotes no rollout stage
-- and adds no new public Run state; it stores facts a later Runtime service reads and
-- writes, not a decision about what that service does with them.
--
--   omnivia_workflow_runtime_bindings          one runtime binding for a workflow run
--   omnivia_workflow_transition_bundles        fenced aggregate transition bundles
--   omnivia_workflow_runtime_journal_events    the journal event paired to each bundle
--   omnivia_workflow_transition_parity_reports  bundle-derived vs existing-writer parity
--   omnivia_workflow_journal_integrity_reports  per-rollout-stage journal verification
--   omnivia_workflow_journal_quarantine_events  quarantine and release of journal events
--   omnivia_workflow_journal_retention_boundaries  recorded (not enacted) removal bounds
--
-- A binding's canonical JSON must cross-check its own `bindingId`, `workflowId`,
-- `workflowVersion` and `definitionDigest` against the workflow run it binds and the
-- plan that run is bound to; `bindingId` is checked in a table CHECK since it is this
-- row's own column, and the other three are checked in the INSERT trigger since they
-- require the join to `omnivia_workflow_runs` and `omnivia_workflow_plans`.
--
-- A transition bundle is fenced by aggregate revision: `produced_revision` is always
-- `expected_revision + 1`, `produced_revision` is unique per run, and the INSERT
-- trigger requires the next `expected_revision` to equal the prior maximum
-- `produced_revision` for the run -- zero for the first bundle a run ever produces.
--
-- A bundle and its journal event are a required pair: a bundle without its event, or
-- an event without its bundle, may never be the durable state a transaction leaves
-- behind. Composite foreign keys in both directions carry `DEFERRABLE INITIALLY
-- DEFERRED` for this one relationship only, so both rows may be inserted in either
-- order within one transaction and neither can commit alone; the general policy in
-- migration 0007 keeps SQLite's default immediate enforcement everywhere else in this
-- schema; it does not apply here; this is the one relationship in this schema where
-- the fact enforced -- a required pair, not an optional link -- cannot be expressed
-- immediately without picking an artificial insertion order.
--
-- A journal event's own `payload_digest` must equal the digest recorded against its
-- `event_payload_json` document, its `sequence` must equal the bundle's own
-- `expected_revision` (the revision that bundle's transition produced), and its
-- `previous_link_digest` is never NULL: every event carries a sha256 link, matching
-- the `previousIntegrityLink` the public `RuntimeJournalEvent` contract requires of
-- every event including the run's first. For `sequence > 0` that link must be the
-- prior event's `event_digest` -- a hash chain a reader may walk without a second
-- index. For `sequence = 0` the link is the run's genesis digest, and SQL enforces
-- its shape only: the Python writer computes it and the Python verifier recomputes
-- it per Run, because deriving it needs SHA-256 over JCS bytes that SQLite cannot
-- produce. SQL therefore owns digest shape, contiguity from zero, and predecessor
-- equality after genesis; Python owns the exact genesis value.
--
-- A parity report is one per bundle, matching iff the existing writer's digest and
-- the bundle-derived digest agree bit for bit. A journal integrity report is one
-- verification pass over one rollout stage (`R0`, `R1`, `R2`); `verified` carries no
-- affected sequence and no diagnostic, and a finding carries both, with the
-- diagnostic naming exactly the code its outcome allows. A quarantine event is a
-- fenced, per-run append: quarantining requires the integrity report that found the
-- fault and forbids naming an actor or a reason, and releasing one requires both
-- and forbids naming a report or a diagnostic. The event it quarantines and the
-- report it cites must both belong to its own run, not merely to its workspace, so
-- both are reached by a run-scoped composite foreign key over a named unique parent
-- index rather than by a workspace-scoped one.
--
-- A quarantine's event citation is nullable for exactly one fault. A `sequence_gap`
-- is the fact that a row is *not* there, and a run whose journal is missing entirely
-- has no surviving event to cite -- so requiring one would leave the worst journal
-- fault the only one that cannot be held. A `quarantined` row may therefore omit its
-- event only when the same-run integrity report it cites found a `sequence_gap`; it
-- must always cite a report that found something, and an `integrity_failure` names
-- the surviving row it is about. A release carries forward whatever citation the
-- disposition before it held, present or absent, so releasing changes who decided and
-- never what was held. A retention boundary only ever records
-- that a range is now removable; this migration deletes no journal row and no
-- Evidence row, and a boundary that does name a removed range must record
-- `resumable_after = 0` for it.
--
-- Deliberately absent: any Provider credential, endpoint, header, raw external body,
-- filesystem path, or SDK type. Deliberately absent also: JCS recomputation itself --
-- every canonical JSON column here is checked for exact canonical form and paired
-- with its own recorded digest and byte length, but proving that digest against the
-- bytes is a Python-side responsibility this migration does not perform, and so is
-- recomputing a run's genesis integrity link.
--
-- Every comment sits between statements and never inside one, because the migrator
-- strips comments while the canonical fingerprint replays this text verbatim.
--
-- UPDATE and DELETE abort unconditionally, for the current fenced owner too.

CREATE TABLE IF NOT EXISTS omnivia_workflow_runtime_bindings (
    workspace_id          TEXT    NOT NULL,
    run_id                TEXT    NOT NULL,
    binding_id            TEXT    NOT NULL,
    binding_schema_version INTEGER NOT NULL,
    binding_json          TEXT    NOT NULL,
    binding_digest        TEXT    NOT NULL,
    binding_byte_length   INTEGER NOT NULL,
    bound_at_us           INTEGER NOT NULL,

    PRIMARY KEY (workspace_id, run_id),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(run_id) = 'text' AND length(run_id) BETWEEN 1 AND 128
           AND run_id GLOB '[A-Za-z0-9]*'
           AND run_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(run_id, char(0)) = 0),
    CHECK (typeof(binding_id) = 'text' AND length(binding_id) BETWEEN 1 AND 128
           AND binding_id GLOB '[A-Za-z0-9]*'
           AND binding_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(binding_id, char(0)) = 0),
    CHECK (typeof(binding_schema_version) = 'integer' AND binding_schema_version = 1),
    CHECK (typeof(binding_json) = 'text'
           AND length(CAST(binding_json AS BLOB)) BETWEEN 2 AND 65536
           AND instr(binding_json, char(0)) = 0
           AND json_valid(binding_json) IS 1
           AND json(binding_json) = binding_json
           AND json_type(binding_json) = 'object'
           AND json_extract(binding_json, '$.bindingId') = binding_id),
    CHECK (typeof(binding_digest) = 'text' AND length(binding_digest) = 71
           AND substr(binding_digest, 1, 7) = 'sha256:'
           AND substr(binding_digest, 8) NOT GLOB '*[^0-9a-f]*'),
    CHECK (typeof(binding_byte_length) = 'integer'
           AND binding_byte_length = length(CAST(binding_json AS BLOB))),
    CHECK (typeof(bound_at_us) = 'integer' AND bound_at_us > 0),

    FOREIGN KEY (workspace_id, run_id)
        REFERENCES omnivia_workflow_runs (workspace_id, run_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_workflow_transition_bundles (
    workspace_id       TEXT    NOT NULL,
    run_id             TEXT    NOT NULL,
    bundle_id          TEXT    NOT NULL,
    binding_id         TEXT    NOT NULL,
    expected_revision  INTEGER NOT NULL,
    produced_revision  INTEGER NOT NULL,
    payload_digest     TEXT    NOT NULL,
    bundle_json        TEXT    NOT NULL,
    bundle_digest      TEXT    NOT NULL,
    bundle_byte_length INTEGER NOT NULL,
    recorded_at_us     INTEGER NOT NULL,

    PRIMARY KEY (workspace_id, run_id, bundle_id),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(run_id) = 'text' AND length(run_id) BETWEEN 1 AND 128
           AND run_id GLOB '[A-Za-z0-9]*'
           AND run_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(run_id, char(0)) = 0),
    CHECK (typeof(bundle_id) = 'text' AND length(bundle_id) BETWEEN 1 AND 128
           AND bundle_id GLOB '[A-Za-z0-9]*'
           AND bundle_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(bundle_id, char(0)) = 0),
    CHECK (typeof(binding_id) = 'text' AND length(binding_id) BETWEEN 1 AND 128
           AND binding_id GLOB '[A-Za-z0-9]*'
           AND binding_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(binding_id, char(0)) = 0),
    CHECK (typeof(expected_revision) = 'integer'
           AND expected_revision BETWEEN 0 AND 1000000000),
    CHECK (typeof(produced_revision) = 'integer'
           AND produced_revision BETWEEN 1 AND 1000000001),
    CHECK (produced_revision = expected_revision + 1),
    CHECK (typeof(payload_digest) = 'text' AND length(payload_digest) = 71
           AND substr(payload_digest, 1, 7) = 'sha256:'
           AND substr(payload_digest, 8) NOT GLOB '*[^0-9a-f]*'),
    CHECK (typeof(bundle_json) = 'text'
           AND length(CAST(bundle_json AS BLOB)) BETWEEN 2 AND 65536
           AND instr(bundle_json, char(0)) = 0
           AND json_valid(bundle_json) IS 1
           AND json(bundle_json) = bundle_json
           AND json_type(bundle_json) = 'object'),
    CHECK (typeof(bundle_digest) = 'text' AND length(bundle_digest) = 71
           AND substr(bundle_digest, 1, 7) = 'sha256:'
           AND substr(bundle_digest, 8) NOT GLOB '*[^0-9a-f]*'),
    CHECK (typeof(bundle_byte_length) = 'integer'
           AND bundle_byte_length = length(CAST(bundle_json AS BLOB))),
    CHECK (typeof(recorded_at_us) = 'integer' AND recorded_at_us > 0),

    FOREIGN KEY (workspace_id, run_id)
        REFERENCES omnivia_workflow_runs (workspace_id, run_id),
    FOREIGN KEY (workspace_id, run_id)
        REFERENCES omnivia_workflow_runtime_bindings (workspace_id, run_id),
    FOREIGN KEY (workspace_id, bundle_id)
        REFERENCES omnivia_workflow_runtime_journal_events (workspace_id, bundle_id)
        DEFERRABLE INITIALLY DEFERRED
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_workflow_runtime_journal_events (
    workspace_id               TEXT    NOT NULL,
    run_id                     TEXT    NOT NULL,
    bundle_id                  TEXT    NOT NULL,
    event_id                   TEXT    NOT NULL,
    sequence                   INTEGER NOT NULL,
    previous_link_digest       TEXT    NOT NULL,
    payload_digest             TEXT    NOT NULL,
    event_json                 TEXT    NOT NULL,
    event_digest                TEXT    NOT NULL,
    event_byte_length           INTEGER NOT NULL,
    event_payload_json         TEXT    NOT NULL,
    event_payload_digest       TEXT    NOT NULL,
    event_payload_byte_length  INTEGER NOT NULL,
    recorded_at_us             INTEGER NOT NULL,

    PRIMARY KEY (workspace_id, event_id),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(run_id) = 'text' AND length(run_id) BETWEEN 1 AND 128
           AND run_id GLOB '[A-Za-z0-9]*'
           AND run_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(run_id, char(0)) = 0),
    CHECK (typeof(bundle_id) = 'text' AND length(bundle_id) BETWEEN 1 AND 128
           AND bundle_id GLOB '[A-Za-z0-9]*'
           AND bundle_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(bundle_id, char(0)) = 0),
    CHECK (typeof(event_id) = 'text' AND length(event_id) BETWEEN 1 AND 128
           AND event_id GLOB '[A-Za-z0-9]*'
           AND event_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(event_id, char(0)) = 0),
    CHECK (typeof(sequence) = 'integer' AND sequence BETWEEN 0 AND 1000000000),
    CHECK (typeof(previous_link_digest) = 'text'
           AND length(previous_link_digest) = 71
           AND substr(previous_link_digest, 1, 7) = 'sha256:'
           AND substr(previous_link_digest, 8) NOT GLOB '*[^0-9a-f]*'),
    CHECK (typeof(payload_digest) = 'text' AND length(payload_digest) = 71
           AND substr(payload_digest, 1, 7) = 'sha256:'
           AND substr(payload_digest, 8) NOT GLOB '*[^0-9a-f]*'),
    CHECK (typeof(event_json) = 'text'
           AND length(CAST(event_json AS BLOB)) BETWEEN 2 AND 65536
           AND instr(event_json, char(0)) = 0
           AND json_valid(event_json) IS 1
           AND json(event_json) = event_json
           AND json_type(event_json) = 'object'),
    CHECK (typeof(event_digest) = 'text' AND length(event_digest) = 71
           AND substr(event_digest, 1, 7) = 'sha256:'
           AND substr(event_digest, 8) NOT GLOB '*[^0-9a-f]*'),
    CHECK (typeof(event_byte_length) = 'integer'
           AND event_byte_length = length(CAST(event_json AS BLOB))),
    CHECK (typeof(event_payload_json) = 'text'
           AND length(CAST(event_payload_json AS BLOB)) BETWEEN 2 AND 65536
           AND instr(event_payload_json, char(0)) = 0
           AND json_valid(event_payload_json) IS 1
           AND json(event_payload_json) = event_payload_json
           AND json_type(event_payload_json) = 'object'),
    CHECK (typeof(event_payload_digest) = 'text' AND length(event_payload_digest) = 71
           AND substr(event_payload_digest, 1, 7) = 'sha256:'
           AND substr(event_payload_digest, 8) NOT GLOB '*[^0-9a-f]*'),
    CHECK (typeof(event_payload_byte_length) = 'integer'
           AND event_payload_byte_length = length(CAST(event_payload_json AS BLOB))),
    CHECK (payload_digest = event_payload_digest),
    CHECK (typeof(recorded_at_us) = 'integer' AND recorded_at_us > 0),

    UNIQUE (workspace_id, bundle_id),
    UNIQUE (workspace_id, run_id, sequence),

    FOREIGN KEY (workspace_id, run_id)
        REFERENCES omnivia_workflow_runs (workspace_id, run_id),
    FOREIGN KEY (workspace_id, run_id, bundle_id)
        REFERENCES omnivia_workflow_transition_bundles (workspace_id, run_id, bundle_id)
        DEFERRABLE INITIALLY DEFERRED
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_workflow_transition_parity_reports (
    workspace_id             TEXT    NOT NULL,
    report_id                TEXT    NOT NULL,
    run_id                   TEXT    NOT NULL,
    bundle_id                TEXT    NOT NULL,
    existing_writer_digest   TEXT    NOT NULL,
    bundle_derived_digest    TEXT    NOT NULL,
    status                   TEXT    NOT NULL,
    report_json              TEXT    NOT NULL,
    report_digest            TEXT    NOT NULL,
    report_byte_length       INTEGER NOT NULL,
    recorded_at_us           INTEGER NOT NULL,

    PRIMARY KEY (workspace_id, report_id),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(report_id) = 'text' AND length(report_id) BETWEEN 1 AND 128
           AND report_id GLOB '[A-Za-z0-9]*'
           AND report_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(report_id, char(0)) = 0),
    CHECK (typeof(run_id) = 'text' AND length(run_id) BETWEEN 1 AND 128
           AND run_id GLOB '[A-Za-z0-9]*'
           AND run_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(run_id, char(0)) = 0),
    CHECK (typeof(bundle_id) = 'text' AND length(bundle_id) BETWEEN 1 AND 128
           AND bundle_id GLOB '[A-Za-z0-9]*'
           AND bundle_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(bundle_id, char(0)) = 0),
    CHECK (typeof(existing_writer_digest) = 'text' AND length(existing_writer_digest) = 71
           AND substr(existing_writer_digest, 1, 7) = 'sha256:'
           AND substr(existing_writer_digest, 8) NOT GLOB '*[^0-9a-f]*'),
    CHECK (typeof(bundle_derived_digest) = 'text' AND length(bundle_derived_digest) = 71
           AND substr(bundle_derived_digest, 1, 7) = 'sha256:'
           AND substr(bundle_derived_digest, 8) NOT GLOB '*[^0-9a-f]*'),
    CHECK (status IN ('match', 'diverged')),
    CHECK ((status = 'match' AND existing_writer_digest = bundle_derived_digest)
           OR (status = 'diverged' AND existing_writer_digest <> bundle_derived_digest)),
    CHECK (typeof(report_json) = 'text'
           AND length(CAST(report_json AS BLOB)) BETWEEN 2 AND 65536
           AND instr(report_json, char(0)) = 0
           AND json_valid(report_json) IS 1
           AND json(report_json) = report_json
           AND json_type(report_json) = 'object'),
    CHECK (typeof(report_digest) = 'text' AND length(report_digest) = 71
           AND substr(report_digest, 1, 7) = 'sha256:'
           AND substr(report_digest, 8) NOT GLOB '*[^0-9a-f]*'),
    CHECK (typeof(report_byte_length) = 'integer'
           AND report_byte_length = length(CAST(report_json AS BLOB))),
    CHECK (typeof(recorded_at_us) = 'integer' AND recorded_at_us > 0),

    UNIQUE (workspace_id, bundle_id),

    FOREIGN KEY (workspace_id, run_id)
        REFERENCES omnivia_workflow_runs (workspace_id, run_id),
    FOREIGN KEY (workspace_id, run_id, bundle_id)
        REFERENCES omnivia_workflow_transition_bundles (workspace_id, run_id, bundle_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_workflow_journal_integrity_reports (
    workspace_id            TEXT    NOT NULL,
    report_id               TEXT    NOT NULL,
    run_id                  TEXT    NOT NULL,
    rollout_stage           TEXT    NOT NULL,
    outcome                 TEXT    NOT NULL,
    first_affected_sequence INTEGER,
    diagnostic              TEXT,
    observed_head           INTEGER NOT NULL,
    report_json             TEXT    NOT NULL,
    report_digest           TEXT    NOT NULL,
    report_byte_length      INTEGER NOT NULL,
    verified_at_us          INTEGER NOT NULL,

    PRIMARY KEY (workspace_id, report_id),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(report_id) = 'text' AND length(report_id) BETWEEN 1 AND 128
           AND report_id GLOB '[A-Za-z0-9]*'
           AND report_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(report_id, char(0)) = 0),
    CHECK (typeof(run_id) = 'text' AND length(run_id) BETWEEN 1 AND 128
           AND run_id GLOB '[A-Za-z0-9]*'
           AND run_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(run_id, char(0)) = 0),
    CHECK (rollout_stage IN ('R0', 'R1', 'R2')),
    CHECK (outcome IN ('verified', 'sequence_gap', 'integrity_failure')),
    CHECK (first_affected_sequence IS NULL
           OR (typeof(first_affected_sequence) = 'integer'
               AND first_affected_sequence BETWEEN 0 AND 1000000000)),
    CHECK (diagnostic IS NULL
           OR diagnostic IN ('RT_JOURNAL_SEQUENCE_GAP', 'RT_JOURNAL_INTEGRITY_FAILURE')),
    CHECK ((outcome = 'verified'
            AND first_affected_sequence IS NULL AND diagnostic IS NULL)
           OR (outcome = 'sequence_gap'
               AND first_affected_sequence IS NOT NULL
               AND diagnostic IS 'RT_JOURNAL_SEQUENCE_GAP')
           OR (outcome = 'integrity_failure'
               AND first_affected_sequence IS NOT NULL
               AND diagnostic IS 'RT_JOURNAL_INTEGRITY_FAILURE')),
    CHECK (typeof(observed_head) = 'integer' AND observed_head BETWEEN -1 AND 1000000000),
    CHECK (typeof(report_json) = 'text'
           AND length(CAST(report_json AS BLOB)) BETWEEN 2 AND 65536
           AND instr(report_json, char(0)) = 0
           AND json_valid(report_json) IS 1
           AND json(report_json) = report_json
           AND json_type(report_json) = 'object'),
    CHECK (typeof(report_digest) = 'text' AND length(report_digest) = 71
           AND substr(report_digest, 1, 7) = 'sha256:'
           AND substr(report_digest, 8) NOT GLOB '*[^0-9a-f]*'),
    CHECK (typeof(report_byte_length) = 'integer'
           AND report_byte_length = length(CAST(report_json AS BLOB))),
    CHECK (typeof(verified_at_us) = 'integer' AND verified_at_us > 0),

    FOREIGN KEY (workspace_id, run_id)
        REFERENCES omnivia_workflow_runs (workspace_id, run_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_workflow_journal_quarantine_events (
    workspace_id           TEXT    NOT NULL,
    run_id                 TEXT    NOT NULL,
    disposition_sequence   INTEGER NOT NULL,
    event_id               TEXT,
    action                 TEXT    NOT NULL,
    integrity_report_id    TEXT,
    diagnostic             TEXT,
    deciding_actor         TEXT,
    reason                 TEXT,
    recorded_at_us         INTEGER NOT NULL,

    PRIMARY KEY (workspace_id, run_id, disposition_sequence),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(run_id) = 'text' AND length(run_id) BETWEEN 1 AND 128
           AND run_id GLOB '[A-Za-z0-9]*'
           AND run_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(run_id, char(0)) = 0),
    CHECK (typeof(disposition_sequence) = 'integer'
           AND disposition_sequence BETWEEN 0 AND 1000000000),
    CHECK (event_id IS NULL
           OR (typeof(event_id) = 'text' AND length(event_id) BETWEEN 1 AND 128
               AND event_id GLOB '[A-Za-z0-9]*'
               AND event_id NOT GLOB '*[^A-Za-z0-9._:-]*'
               AND instr(event_id, char(0)) = 0)),
    CHECK (action IN ('quarantined', 'released')),
    CHECK (integrity_report_id IS NULL
           OR (typeof(integrity_report_id) = 'text'
               AND length(integrity_report_id) BETWEEN 1 AND 128
               AND integrity_report_id GLOB '[A-Za-z0-9]*'
               AND integrity_report_id NOT GLOB '*[^A-Za-z0-9._:-]*'
               AND instr(integrity_report_id, char(0)) = 0)),
    CHECK (diagnostic IS NULL OR diagnostic = 'RT_JOURNAL_QUARANTINED'),
    CHECK (deciding_actor IS NULL
           OR (typeof(deciding_actor) = 'text'
               AND length(deciding_actor) BETWEEN 1 AND 128
               AND deciding_actor GLOB '[A-Za-z0-9]*'
               AND deciding_actor NOT GLOB '*[^A-Za-z0-9._:-]*'
               AND instr(deciding_actor, char(0)) = 0)),
    CHECK (reason IS NULL
           OR (typeof(reason) = 'text' AND length(reason) BETWEEN 1 AND 128
               AND reason GLOB '[a-z]*'
               AND reason NOT GLOB '*[^a-z0-9_.]*'
               AND reason NOT GLOB '*.'
               AND reason NOT GLOB '*.[^a-z]*')),
    CHECK ((action = 'quarantined'
            AND integrity_report_id IS NOT NULL
            AND diagnostic IS 'RT_JOURNAL_QUARANTINED'
            AND deciding_actor IS NULL AND reason IS NULL)
           OR (action = 'released'
               AND integrity_report_id IS NULL AND diagnostic IS NULL
               AND deciding_actor IS NOT NULL AND reason IS NOT NULL)),
    CHECK (typeof(recorded_at_us) = 'integer' AND recorded_at_us > 0),

    FOREIGN KEY (workspace_id, run_id)
        REFERENCES omnivia_workflow_runs (workspace_id, run_id),
    FOREIGN KEY (workspace_id, run_id, event_id)
        REFERENCES omnivia_workflow_runtime_journal_events (workspace_id, run_id, event_id),
    FOREIGN KEY (workspace_id, run_id, integrity_report_id)
        REFERENCES omnivia_workflow_journal_integrity_reports
            (workspace_id, run_id, report_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_workflow_journal_retention_boundaries (
    workspace_id            TEXT    NOT NULL,
    boundary_id             TEXT    NOT NULL,
    run_id                  TEXT    NOT NULL,
    first_removed_sequence  INTEGER,
    last_removed_sequence   INTEGER,
    resumable_after         INTEGER NOT NULL,
    policy_ref              TEXT    NOT NULL,
    evidence_ref            TEXT    NOT NULL,
    audit_ref               TEXT    NOT NULL,
    recorded_at_us          INTEGER NOT NULL,

    PRIMARY KEY (workspace_id, boundary_id),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(boundary_id) = 'text' AND length(boundary_id) BETWEEN 1 AND 128
           AND boundary_id GLOB '[A-Za-z0-9]*'
           AND boundary_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(boundary_id, char(0)) = 0),
    CHECK (typeof(run_id) = 'text' AND length(run_id) BETWEEN 1 AND 128
           AND run_id GLOB '[A-Za-z0-9]*'
           AND run_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(run_id, char(0)) = 0),
    CHECK (first_removed_sequence IS NULL
           OR (typeof(first_removed_sequence) = 'integer'
               AND first_removed_sequence BETWEEN 0 AND 1000000000)),
    CHECK (last_removed_sequence IS NULL
           OR (typeof(last_removed_sequence) = 'integer'
               AND last_removed_sequence BETWEEN 0 AND 1000000000)),
    CHECK ((first_removed_sequence IS NULL AND last_removed_sequence IS NULL)
           OR (first_removed_sequence IS NOT NULL AND last_removed_sequence IS NOT NULL
               AND first_removed_sequence <= last_removed_sequence)),
    CHECK (resumable_after IN (0, 1)),
    CHECK (first_removed_sequence IS NULL OR resumable_after = 0),
    CHECK (typeof(policy_ref) = 'text' AND length(policy_ref) BETWEEN 1 AND 128
           AND policy_ref GLOB '[A-Za-z0-9]*'
           AND policy_ref NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(policy_ref, char(0)) = 0),
    CHECK (typeof(evidence_ref) = 'text' AND length(evidence_ref) BETWEEN 1 AND 128
           AND evidence_ref GLOB '[A-Za-z0-9]*'
           AND evidence_ref NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(evidence_ref, char(0)) = 0),
    CHECK (typeof(audit_ref) = 'text' AND length(audit_ref) BETWEEN 1 AND 128
           AND audit_ref GLOB '[A-Za-z0-9]*'
           AND audit_ref NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(audit_ref, char(0)) = 0),
    CHECK (typeof(recorded_at_us) = 'integer' AND recorded_at_us > 0),

    FOREIGN KEY (workspace_id, run_id)
        REFERENCES omnivia_workflow_runs (workspace_id, run_id),
    FOREIGN KEY (audit_ref, workspace_id)
        REFERENCES omnivia_application_audit_events (audit_ref, workspace_id)
) WITHOUT ROWID;

-- Nine named indexes: the parent keys the composite foreign keys need, the
-- uniqueness rules that would otherwise live in an implicit `sqlite_autoindex_*`, and
-- the ordered-read paths a later reader walks. They are declared by name rather than
-- as inline `UNIQUE` clauses where a name is not already required by SQLite for a
-- foreign key target, because the canonical schema fingerprint filters implicit
-- indexes out, and a constraint drift detection cannot see is not a constraint.
CREATE UNIQUE INDEX IF NOT EXISTS omnivia_idx_workflow_runtime_bindings_binding
    ON omnivia_workflow_runtime_bindings (workspace_id, binding_id);
CREATE UNIQUE INDEX IF NOT EXISTS omnivia_idx_workflow_transition_bundles_produced
    ON omnivia_workflow_transition_bundles (workspace_id, run_id, produced_revision);
CREATE INDEX IF NOT EXISTS omnivia_idx_workflow_runtime_journal_events_order
    ON omnivia_workflow_runtime_journal_events (workspace_id, run_id, sequence);
CREATE UNIQUE INDEX IF NOT EXISTS omnivia_idx_workflow_runtime_journal_events_run_event
    ON omnivia_workflow_runtime_journal_events (workspace_id, run_id, event_id);
CREATE INDEX IF NOT EXISTS omnivia_idx_workflow_journal_integrity_reports_run
    ON omnivia_workflow_journal_integrity_reports (workspace_id, run_id, verified_at_us);
CREATE UNIQUE INDEX IF NOT EXISTS omnivia_idx_workflow_journal_integrity_reports_run_report
    ON omnivia_workflow_journal_integrity_reports (workspace_id, run_id, report_id);
CREATE INDEX IF NOT EXISTS omnivia_idx_workflow_journal_quarantine_events_event
    ON omnivia_workflow_journal_quarantine_events (workspace_id, run_id, event_id);
CREATE INDEX IF NOT EXISTS omnivia_idx_workflow_journal_retention_boundaries_run
    ON omnivia_workflow_journal_retention_boundaries (workspace_id, run_id, recorded_at_us);
CREATE INDEX IF NOT EXISTS omnivia_idx_workflow_transition_parity_reports_run
    ON omnivia_workflow_transition_parity_reports (workspace_id, run_id, recorded_at_us);

CREATE TRIGGER IF NOT EXISTS omnivia_guard_workflow_runtime_bindings_insert
BEFORE INSERT ON omnivia_workflow_runtime_bindings
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_workflow_runtime_bindings')
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
    SELECT RAISE(ABORT, 'omnivia: a workflow runtime binding must name the run it binds and the plan that run is bound to')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_workflow_runs r
        JOIN omnivia_workflow_plans p
          ON p.workspace_id = r.workspace_id
         AND p.workflow_id = r.workflow_id
         AND p.workflow_version = r.workflow_version
         AND p.plan_hash = r.plan_hash
        WHERE r.workspace_id = NEW.workspace_id AND r.run_id = NEW.run_id
          AND json_extract(NEW.binding_json, '$.workflowId') = r.workflow_id
          AND json_extract(NEW.binding_json, '$.workflowVersion') = r.workflow_version
          AND json_extract(NEW.binding_json, '$.definitionDigest') = p.definition_hash);
    SELECT RAISE(ABORT, 'omnivia: a workflow runtime binding cannot predate the run it binds')
    WHERE NEW.bound_at_us < (
        SELECT bound_at_us FROM omnivia_workflow_runs
        WHERE workspace_id = NEW.workspace_id AND run_id = NEW.run_id);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_workflow_runtime_bindings_update
BEFORE UPDATE ON omnivia_workflow_runtime_bindings
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_workflow_runtime_bindings is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_workflow_runtime_bindings_delete
BEFORE DELETE ON omnivia_workflow_runtime_bindings
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_workflow_runtime_bindings is append-only; DELETE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_workflow_transition_bundles_insert
BEFORE INSERT ON omnivia_workflow_transition_bundles
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_workflow_transition_bundles')
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
    SELECT RAISE(ABORT, 'omnivia: a transition bundle must name the binding its run holds')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_workflow_runtime_bindings
        WHERE workspace_id = NEW.workspace_id AND run_id = NEW.run_id
          AND binding_id = NEW.binding_id);
    SELECT RAISE(ABORT, 'omnivia: a transition bundle''s expected revision must continue its run''s produced revisions')
    WHERE NEW.expected_revision IS NOT (
        SELECT COALESCE(MAX(produced_revision), 0) FROM omnivia_workflow_transition_bundles
        WHERE workspace_id = NEW.workspace_id AND run_id = NEW.run_id);
    SELECT RAISE(ABORT, 'omnivia: a journal event''s sequence must equal its bundle''s expected revision')
    WHERE EXISTS (
        SELECT 1 FROM omnivia_workflow_runtime_journal_events
        WHERE workspace_id = NEW.workspace_id AND run_id = NEW.run_id
          AND bundle_id = NEW.bundle_id AND sequence IS NOT NEW.expected_revision);
    SELECT RAISE(ABORT, 'omnivia: a transition bundle cannot predate the binding it transitions')
    WHERE NEW.recorded_at_us < (
        SELECT bound_at_us FROM omnivia_workflow_runtime_bindings
        WHERE workspace_id = NEW.workspace_id AND run_id = NEW.run_id);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_workflow_transition_bundles_update
BEFORE UPDATE ON omnivia_workflow_transition_bundles
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_workflow_transition_bundles is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_workflow_transition_bundles_delete
BEFORE DELETE ON omnivia_workflow_transition_bundles
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_workflow_transition_bundles is append-only; DELETE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_workflow_runtime_journal_events_insert
BEFORE INSERT ON omnivia_workflow_runtime_journal_events
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_workflow_runtime_journal_events')
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
    SELECT RAISE(ABORT, 'omnivia: a journal event''s sequence must equal its bundle''s expected revision')
    WHERE EXISTS (
        SELECT 1 FROM omnivia_workflow_transition_bundles
        WHERE workspace_id = NEW.workspace_id AND run_id = NEW.run_id
          AND bundle_id = NEW.bundle_id AND expected_revision IS NOT NEW.sequence);
    SELECT RAISE(ABORT, 'omnivia: journal events must be contiguous from zero')
    WHERE NEW.sequence IS NOT (
        SELECT COALESCE(MAX(sequence), -1) + 1 FROM omnivia_workflow_runtime_journal_events
        WHERE workspace_id = NEW.workspace_id AND run_id = NEW.run_id);
    SELECT RAISE(ABORT, 'omnivia: a journal event''s previous link must be its run''s prior event digest')
    WHERE NEW.sequence > 0
      AND NEW.previous_link_digest IS NOT (
        SELECT event_digest FROM omnivia_workflow_runtime_journal_events
        WHERE workspace_id = NEW.workspace_id AND run_id = NEW.run_id
          AND sequence = NEW.sequence - 1);
    SELECT RAISE(ABORT, 'omnivia: a journal event cannot predate its run')
    WHERE NEW.recorded_at_us < (
        SELECT bound_at_us FROM omnivia_workflow_runs
        WHERE workspace_id = NEW.workspace_id AND run_id = NEW.run_id);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_workflow_runtime_journal_events_update
BEFORE UPDATE ON omnivia_workflow_runtime_journal_events
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_workflow_runtime_journal_events is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_workflow_runtime_journal_events_delete
BEFORE DELETE ON omnivia_workflow_runtime_journal_events
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_workflow_runtime_journal_events is append-only; DELETE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_workflow_transition_parity_reports_insert
BEFORE INSERT ON omnivia_workflow_transition_parity_reports
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_workflow_transition_parity_reports')
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
    SELECT RAISE(ABORT, 'omnivia: a parity report cannot predate the bundle it reports on')
    WHERE NEW.recorded_at_us < (
        SELECT recorded_at_us FROM omnivia_workflow_transition_bundles
        WHERE workspace_id = NEW.workspace_id AND run_id = NEW.run_id
          AND bundle_id = NEW.bundle_id);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_workflow_transition_parity_reports_update
BEFORE UPDATE ON omnivia_workflow_transition_parity_reports
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_workflow_transition_parity_reports is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_workflow_transition_parity_reports_delete
BEFORE DELETE ON omnivia_workflow_transition_parity_reports
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_workflow_transition_parity_reports is append-only; DELETE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_workflow_journal_integrity_reports_insert
BEFORE INSERT ON omnivia_workflow_journal_integrity_reports
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_workflow_journal_integrity_reports')
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
    SELECT RAISE(ABORT, 'omnivia: a journal integrity report cannot predate the run it verifies')
    WHERE NEW.verified_at_us < (
        SELECT bound_at_us FROM omnivia_workflow_runs
        WHERE workspace_id = NEW.workspace_id AND run_id = NEW.run_id);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_workflow_journal_integrity_reports_update
BEFORE UPDATE ON omnivia_workflow_journal_integrity_reports
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_workflow_journal_integrity_reports is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_workflow_journal_integrity_reports_delete
BEFORE DELETE ON omnivia_workflow_journal_integrity_reports
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_workflow_journal_integrity_reports is append-only; DELETE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_workflow_journal_quarantine_events_insert
BEFORE INSERT ON omnivia_workflow_journal_quarantine_events
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_workflow_journal_quarantine_events')
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
    SELECT RAISE(ABORT, 'omnivia: a quarantine disposition must be contiguous from zero per run')
    WHERE NEW.disposition_sequence IS NOT (
        SELECT COALESCE(MAX(disposition_sequence), -1) + 1
        FROM omnivia_workflow_journal_quarantine_events
        WHERE workspace_id = NEW.workspace_id AND run_id = NEW.run_id);
    SELECT RAISE(ABORT, 'omnivia: a quarantine disposition must cite an integrity report that found a fault')
    WHERE NEW.action = 'quarantined'
      AND EXISTS (
        SELECT 1 FROM omnivia_workflow_journal_integrity_reports
        WHERE workspace_id = NEW.workspace_id AND run_id = NEW.run_id
          AND report_id = NEW.integrity_report_id AND outcome = 'verified');
    SELECT RAISE(ABORT, 'omnivia: a quarantine disposition may omit its event only for a sequence gap')
    WHERE NEW.action = 'quarantined'
      AND NEW.event_id IS NULL
      AND NOT EXISTS (
        SELECT 1 FROM omnivia_workflow_journal_integrity_reports
        WHERE workspace_id = NEW.workspace_id AND run_id = NEW.run_id
          AND report_id = NEW.integrity_report_id AND outcome = 'sequence_gap');
    SELECT RAISE(ABORT, 'omnivia: a quarantine release must carry forward the event citation it holds')
    WHERE NEW.action = 'released'
      AND EXISTS (
        SELECT 1 FROM omnivia_workflow_journal_quarantine_events
        WHERE workspace_id = NEW.workspace_id AND run_id = NEW.run_id)
      AND NEW.event_id IS NOT (
        SELECT event_id FROM omnivia_workflow_journal_quarantine_events
        WHERE workspace_id = NEW.workspace_id AND run_id = NEW.run_id
        ORDER BY disposition_sequence DESC LIMIT 1);
    SELECT RAISE(ABORT, 'omnivia: a quarantine disposition cannot predate its run')
    WHERE NEW.recorded_at_us < (
        SELECT bound_at_us FROM omnivia_workflow_runs
        WHERE workspace_id = NEW.workspace_id AND run_id = NEW.run_id);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_workflow_journal_quarantine_events_update
BEFORE UPDATE ON omnivia_workflow_journal_quarantine_events
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_workflow_journal_quarantine_events is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_workflow_journal_quarantine_events_delete
BEFORE DELETE ON omnivia_workflow_journal_quarantine_events
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_workflow_journal_quarantine_events is append-only; DELETE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_workflow_journal_retention_boundaries_insert
BEFORE INSERT ON omnivia_workflow_journal_retention_boundaries
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_workflow_journal_retention_boundaries')
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
    SELECT RAISE(ABORT, 'omnivia: a retention boundary audit reference must belong to its workspace')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_application_audit_events
        WHERE audit_ref = NEW.audit_ref AND workspace_id = NEW.workspace_id);
    SELECT RAISE(ABORT, 'omnivia: a retention boundary cannot predate its run')
    WHERE NEW.recorded_at_us < (
        SELECT bound_at_us FROM omnivia_workflow_runs
        WHERE workspace_id = NEW.workspace_id AND run_id = NEW.run_id);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_workflow_journal_retention_boundaries_update
BEFORE UPDATE ON omnivia_workflow_journal_retention_boundaries
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_workflow_journal_retention_boundaries is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_workflow_journal_retention_boundaries_delete
BEFORE DELETE ON omnivia_workflow_journal_retention_boundaries
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_workflow_journal_retention_boundaries is append-only; DELETE is never permitted');
END;
