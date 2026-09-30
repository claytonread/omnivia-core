-- Durable engineering conflict-discovery work and relationship candidates
-- (SPEC-CORE-ENGMEM-001 AC-049/AC-050, Stage A).
--
-- Additive only; allocation 0054 (Engineering Memory, predecessor 0053). This
-- migration deliberately performs no historical backfill. Existing versions are
-- not silently declared scanned; the next newly sealed engineering observation is
-- the first version that enqueues a discovery run.
--
-- Runs bind one detector invocation to an exact sealed anchor. Run events preserve
-- the small state machine. Candidate rows contain exact identities and digests but
-- no bodies or preview text. Observations explain deterministic selection without
-- carrying source paths. All four families are append-only and fenced.

CREATE TABLE IF NOT EXISTS omnivia_engineering_discovery_runs (
    workspace_id          TEXT    NOT NULL,
    discovery_run_id      TEXT    NOT NULL,
    anchor_assembly_id    TEXT    NOT NULL,
    anchor_record_id      TEXT    NOT NULL,
    anchor_version        TEXT    NOT NULL,
    anchor_content_digest TEXT    NOT NULL,
    principal_id          TEXT    NOT NULL,
    detector_version      TEXT    NOT NULL,
    candidate_budget      INTEGER NOT NULL,
    resolution_instant_us INTEGER NOT NULL,
    enqueued_at_us        INTEGER NOT NULL,
    audit_ref             TEXT    NOT NULL,

    PRIMARY KEY (workspace_id, discovery_run_id),
    UNIQUE (workspace_id, anchor_assembly_id, detector_version),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(discovery_run_id) = 'text'
           AND length(discovery_run_id) BETWEEN 1 AND 128
           AND discovery_run_id GLOB '[A-Za-z0-9]*'
           AND discovery_run_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(discovery_run_id, char(0)) = 0),
    CHECK (typeof(anchor_assembly_id) = 'text'
           AND length(anchor_assembly_id) BETWEEN 1 AND 128
           AND anchor_assembly_id GLOB '[A-Za-z0-9]*'
           AND anchor_assembly_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(anchor_assembly_id, char(0)) = 0),
    CHECK (typeof(anchor_record_id) = 'text'
           AND length(anchor_record_id) BETWEEN 1 AND 128
           AND anchor_record_id GLOB '[A-Za-z0-9]*'
           AND anchor_record_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(anchor_record_id, char(0)) = 0),
    CHECK (typeof(anchor_version) = 'text'
           AND length(anchor_version) BETWEEN 1 AND 128
           AND anchor_version GLOB '[A-Za-z0-9]*'
           AND anchor_version NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(anchor_version, char(0)) = 0),
    CHECK (typeof(anchor_content_digest) = 'text'
           AND length(anchor_content_digest) = 71
           AND substr(anchor_content_digest, 1, 7) = 'sha256:'
           AND substr(anchor_content_digest, 8) NOT GLOB '*[^0-9a-f]*'),
    CHECK (typeof(principal_id) = 'text' AND length(principal_id) BETWEEN 1 AND 128
           AND instr(principal_id, char(0)) = 0),
    CHECK (typeof(detector_version) = 'text'
           AND length(detector_version) BETWEEN 1 AND 64
           AND detector_version GLOB '[A-Za-z0-9]*'
           AND detector_version NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(detector_version, char(0)) = 0),
    CHECK (typeof(candidate_budget) = 'integer' AND candidate_budget BETWEEN 1 AND 32),
    CHECK (typeof(resolution_instant_us) = 'integer' AND resolution_instant_us > 0),
    CHECK (typeof(enqueued_at_us) = 'integer'
           AND enqueued_at_us = resolution_instant_us),
    CHECK (typeof(audit_ref) = 'text' AND length(audit_ref) BETWEEN 1 AND 128
           AND audit_ref GLOB '[A-Za-z0-9]*'
           AND audit_ref NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(audit_ref, char(0)) = 0),

    FOREIGN KEY (workspace_id, anchor_assembly_id, anchor_version)
        REFERENCES omnivia_governed_version_assemblies
            (workspace_id, assembly_id, governed_record_version_id),
    FOREIGN KEY (audit_ref, workspace_id)
        REFERENCES omnivia_application_audit_events (audit_ref, workspace_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_engineering_discovery_run_events (
    workspace_id             TEXT    NOT NULL,
    discovery_run_id         TEXT    NOT NULL,
    event_sequence           INTEGER NOT NULL,
    event_id                 TEXT    NOT NULL,
    state                    TEXT    NOT NULL,
    coverage                 TEXT,
    frontier_digest          TEXT,
    authorized_frontier_size INTEGER,
    structural_considered    INTEGER,
    lexical_considered       INTEGER,
    selected_count           INTEGER,
    failure_code             TEXT,
    occurred_at_us           INTEGER NOT NULL,

    PRIMARY KEY (workspace_id, discovery_run_id, event_sequence),
    UNIQUE (workspace_id, event_id),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(discovery_run_id) = 'text'
           AND length(discovery_run_id) BETWEEN 1 AND 128
           AND instr(discovery_run_id, char(0)) = 0),
    CHECK (typeof(event_sequence) = 'integer' AND event_sequence IN (1, 2)),
    CHECK (typeof(event_id) = 'text' AND length(event_id) BETWEEN 1 AND 128
           AND event_id GLOB '[A-Za-z0-9]*'
           AND event_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(event_id, char(0)) = 0),
    CHECK (state IN ('queued', 'completed', 'failed')),
    CHECK (coverage IS NULL
           OR coverage IN ('scan_complete_for_snapshot', 'partial', 'not_scanned')),
    CHECK (frontier_digest IS NULL
           OR (typeof(frontier_digest) = 'text' AND length(frontier_digest) = 71
               AND substr(frontier_digest, 1, 7) = 'sha256:'
               AND substr(frontier_digest, 8) NOT GLOB '*[^0-9a-f]*')),
    CHECK (authorized_frontier_size IS NULL
           OR (typeof(authorized_frontier_size) = 'integer'
               AND authorized_frontier_size BETWEEN 0 AND 2147483647)),
    CHECK (structural_considered IS NULL
           OR (typeof(structural_considered) = 'integer'
               AND structural_considered BETWEEN 0 AND 2147483647)),
    CHECK (lexical_considered IS NULL
           OR (typeof(lexical_considered) = 'integer'
               AND lexical_considered BETWEEN 0 AND 2147483647)),
    CHECK (selected_count IS NULL
           OR (typeof(selected_count) = 'integer' AND selected_count BETWEEN 0 AND 32)),
    CHECK (failure_code IS NULL
           OR (typeof(failure_code) = 'text' AND length(failure_code) BETWEEN 1 AND 64
               AND failure_code GLOB '[a-z]*'
               AND failure_code NOT GLOB '*[^a-z0-9_.]*'
               AND instr(failure_code, char(0)) = 0)),
    CHECK (typeof(occurred_at_us) = 'integer' AND occurred_at_us > 0),
    CHECK ((event_sequence = 1 AND state = 'queued'
            AND coverage IS NULL AND frontier_digest IS NULL
            AND authorized_frontier_size IS NULL AND structural_considered IS NULL
            AND lexical_considered IS NULL AND selected_count IS NULL
            AND failure_code IS NULL)
           OR (event_sequence = 2 AND state IN ('completed', 'failed')
               AND coverage IS NOT NULL
               AND authorized_frontier_size IS NOT NULL
               AND structural_considered IS NOT NULL
               AND lexical_considered IS NOT NULL
               AND selected_count IS NOT NULL
               AND ((state = 'completed'
                     AND coverage IN ('scan_complete_for_snapshot', 'partial')
                     AND frontier_digest IS NOT NULL
                     AND failure_code IS NULL)
                    OR (state = 'failed'
                        AND coverage IN ('partial', 'not_scanned')
                        AND ((coverage = 'partial' AND frontier_digest IS NOT NULL)
                             OR (coverage = 'not_scanned'
                                 AND frontier_digest IS NULL
                                 AND authorized_frontier_size = 0
                                 AND structural_considered = 0
                                 AND lexical_considered = 0))
                        AND selected_count = 0
                        AND failure_code IS NOT NULL)))),

    FOREIGN KEY (workspace_id, discovery_run_id)
        REFERENCES omnivia_engineering_discovery_runs (workspace_id, discovery_run_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_engineering_relation_candidates (
    workspace_id           TEXT    NOT NULL,
    relation_candidate_id  TEXT    NOT NULL,
    endpoint_a_assembly_id TEXT    NOT NULL,
    endpoint_a_record_id   TEXT    NOT NULL,
    endpoint_a_version     TEXT    NOT NULL,
    endpoint_a_digest      TEXT    NOT NULL,
    endpoint_b_assembly_id TEXT    NOT NULL,
    endpoint_b_record_id   TEXT    NOT NULL,
    endpoint_b_version     TEXT    NOT NULL,
    endpoint_b_digest      TEXT    NOT NULL,
    detector_version       TEXT    NOT NULL,
    scope_classification   TEXT    NOT NULL,
    proposed_relation      TEXT    NOT NULL,
    status                 TEXT    NOT NULL,
    first_discovery_run_id TEXT    NOT NULL,
    recorded_at_us         INTEGER NOT NULL,

    PRIMARY KEY (workspace_id, relation_candidate_id),
    UNIQUE (workspace_id, endpoint_a_record_id, endpoint_a_version,
            endpoint_b_record_id, endpoint_b_version, detector_version),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(relation_candidate_id) = 'text'
           AND length(relation_candidate_id) BETWEEN 1 AND 128
           AND relation_candidate_id GLOB '[A-Za-z0-9]*'
           AND relation_candidate_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(relation_candidate_id, char(0)) = 0),
    CHECK (typeof(endpoint_a_assembly_id) = 'text'
           AND length(endpoint_a_assembly_id) BETWEEN 1 AND 128
           AND instr(endpoint_a_assembly_id, char(0)) = 0),
    CHECK (typeof(endpoint_b_assembly_id) = 'text'
           AND length(endpoint_b_assembly_id) BETWEEN 1 AND 128
           AND instr(endpoint_b_assembly_id, char(0)) = 0),
    CHECK (typeof(endpoint_a_record_id) = 'text'
           AND length(endpoint_a_record_id) BETWEEN 1 AND 128
           AND instr(endpoint_a_record_id, char(0)) = 0),
    CHECK (typeof(endpoint_b_record_id) = 'text'
           AND length(endpoint_b_record_id) BETWEEN 1 AND 128
           AND instr(endpoint_b_record_id, char(0)) = 0),
    CHECK (typeof(endpoint_a_version) = 'text'
           AND length(endpoint_a_version) BETWEEN 1 AND 128
           AND instr(endpoint_a_version, char(0)) = 0),
    CHECK (typeof(endpoint_b_version) = 'text'
           AND length(endpoint_b_version) BETWEEN 1 AND 128
           AND instr(endpoint_b_version, char(0)) = 0),
    CHECK (endpoint_a_record_id <> endpoint_b_record_id),
    CHECK ((endpoint_a_record_id, endpoint_a_version)
           < (endpoint_b_record_id, endpoint_b_version)),
    CHECK (typeof(endpoint_a_digest) = 'text' AND length(endpoint_a_digest) = 71
           AND substr(endpoint_a_digest, 1, 7) = 'sha256:'
           AND substr(endpoint_a_digest, 8) NOT GLOB '*[^0-9a-f]*'),
    CHECK (typeof(endpoint_b_digest) = 'text' AND length(endpoint_b_digest) = 71
           AND substr(endpoint_b_digest, 1, 7) = 'sha256:'
           AND substr(endpoint_b_digest, 8) NOT GLOB '*[^0-9a-f]*'),
    CHECK (typeof(detector_version) = 'text'
           AND length(detector_version) BETWEEN 1 AND 64
           AND instr(detector_version, char(0)) = 0),
    CHECK (scope_classification IN ('unresolved_overlap', 'scoped_difference')),
    CHECK ((scope_classification = 'unresolved_overlap' AND proposed_relation = 'related')
           OR (scope_classification = 'scoped_difference'
               AND proposed_relation = 'scoped_difference')),
    CHECK (status = 'pending'),
    CHECK (typeof(first_discovery_run_id) = 'text'
           AND length(first_discovery_run_id) BETWEEN 1 AND 128
           AND instr(first_discovery_run_id, char(0)) = 0),
    CHECK (typeof(recorded_at_us) = 'integer' AND recorded_at_us > 0),

    FOREIGN KEY (workspace_id, endpoint_a_assembly_id, endpoint_a_version)
        REFERENCES omnivia_governed_version_assemblies
            (workspace_id, assembly_id, governed_record_version_id),
    FOREIGN KEY (workspace_id, endpoint_b_assembly_id, endpoint_b_version)
        REFERENCES omnivia_governed_version_assemblies
            (workspace_id, assembly_id, governed_record_version_id),
    FOREIGN KEY (workspace_id, first_discovery_run_id)
        REFERENCES omnivia_engineering_discovery_runs (workspace_id, discovery_run_id)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS omnivia_idx_engineering_relation_candidates_endpoint_a
    ON omnivia_engineering_relation_candidates
        (workspace_id, endpoint_a_assembly_id, status,
         endpoint_b_assembly_id, recorded_at_us, relation_candidate_id);

CREATE INDEX IF NOT EXISTS omnivia_idx_engineering_relation_candidates_endpoint_b
    ON omnivia_engineering_relation_candidates
        (workspace_id, endpoint_b_assembly_id, status,
         endpoint_a_assembly_id, recorded_at_us, relation_candidate_id);

CREATE INDEX IF NOT EXISTS omnivia_idx_engineering_relation_candidates_first_run
    ON omnivia_engineering_relation_candidates
        (workspace_id, first_discovery_run_id, relation_candidate_id);

CREATE TABLE IF NOT EXISTS omnivia_engineering_discovery_candidate_observations (
    workspace_id          TEXT    NOT NULL,
    discovery_run_id      TEXT    NOT NULL,
    relation_candidate_id TEXT    NOT NULL,
    selected_order        INTEGER NOT NULL,
    channel               TEXT    NOT NULL,
    score                 INTEGER NOT NULL,
    basis_json            TEXT    NOT NULL,
    recorded_at_us        INTEGER NOT NULL,

    PRIMARY KEY (workspace_id, discovery_run_id, relation_candidate_id),
    UNIQUE (workspace_id, discovery_run_id, selected_order),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(discovery_run_id) = 'text'
           AND length(discovery_run_id) BETWEEN 1 AND 128
           AND instr(discovery_run_id, char(0)) = 0),
    CHECK (typeof(relation_candidate_id) = 'text'
           AND length(relation_candidate_id) BETWEEN 1 AND 128
           AND instr(relation_candidate_id, char(0)) = 0),
    CHECK (typeof(selected_order) = 'integer' AND selected_order BETWEEN 1 AND 32),
    CHECK (channel IN ('structural', 'lexical')),
    CHECK (typeof(score) = 'integer' AND score BETWEEN 0 AND 2147483647),
    CHECK (typeof(basis_json) = 'text'
           AND length(CAST(basis_json AS BLOB)) BETWEEN 2 AND 4096
           AND json_valid(basis_json) = 1 AND json_type(basis_json) = 'object'
           AND json(basis_json) = basis_json),
    CHECK (typeof(recorded_at_us) = 'integer' AND recorded_at_us > 0),

    FOREIGN KEY (workspace_id, discovery_run_id)
        REFERENCES omnivia_engineering_discovery_runs (workspace_id, discovery_run_id),
    FOREIGN KEY (workspace_id, relation_candidate_id)
        REFERENCES omnivia_engineering_relation_candidates
            (workspace_id, relation_candidate_id)
) WITHOUT ROWID;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_engineering_discovery_runs_insert
BEFORE INSERT ON omnivia_engineering_discovery_runs
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_engineering_discovery_runs')
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
    SELECT RAISE(ABORT, 'omnivia: a discovery run must name its exact sealed engineering anchor and originating application audit')
    WHERE NOT EXISTS (
        SELECT 1
        FROM omnivia_governed_version_assemblies a
        JOIN omnivia_governed_version_seals s
          ON s.workspace_id = a.workspace_id
         AND s.assembly_id = a.assembly_id
         AND s.governed_record_version_id = a.governed_record_version_id
        JOIN omnivia_engineering_preview_projection p
          ON p.workspace_id = a.workspace_id AND p.assembly_id = a.assembly_id
         AND p.projection_version = 1 AND p.content_digest = a.content_digest
        JOIN omnivia_application_claim_lineage l
          ON l.workspace_id = a.workspace_id AND l.assembly_id = a.assembly_id
         AND l.governed_record_version_id = a.governed_record_version_id
        JOIN omnivia_application_audit_events e
          ON e.audit_ref = l.audit_ref AND e.workspace_id = l.workspace_id
        WHERE a.workspace_id = NEW.workspace_id
          AND a.assembly_id = NEW.anchor_assembly_id
          AND a.governed_record_id = NEW.anchor_record_id
          AND a.governed_record_version_id = NEW.anchor_version
          AND a.content_digest = NEW.anchor_content_digest
          AND a.record_type IN ('knowledge.finding', 'knowledge.risk', 'knowledge.decision')
          AND a.domain_scope = 'engineering.codebase'
          AND l.operation IN ('memory.create', 'knowledge.propose', 'candidate.approve', 'record.supersede')
          AND l.audit_ref = NEW.audit_ref
          AND l.settled_at_us = NEW.enqueued_at_us
          AND e.operation = l.operation
          AND e.principal_id = NEW.principal_id
          AND e.recorded_at_us = NEW.enqueued_at_us
          AND s.sealed_at_us = NEW.enqueued_at_us);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_engineering_discovery_runs_update
BEFORE UPDATE ON omnivia_engineering_discovery_runs
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_engineering_discovery_runs is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_engineering_discovery_runs_delete
BEFORE DELETE ON omnivia_engineering_discovery_runs
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_engineering_discovery_runs is append-only; DELETE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_engineering_discovery_run_events_insert
BEFORE INSERT ON omnivia_engineering_discovery_run_events
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_engineering_discovery_run_events')
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
    SELECT RAISE(ABORT, 'omnivia: discovery run events begin queued and have at most one ordered terminal event')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_engineering_discovery_runs r
        WHERE r.workspace_id = NEW.workspace_id
          AND r.discovery_run_id = NEW.discovery_run_id
          AND NEW.occurred_at_us >= r.enqueued_at_us
          AND ((NEW.event_sequence = 1 AND NEW.state = 'queued'
                AND NEW.occurred_at_us = r.enqueued_at_us
                AND NOT EXISTS (
                    SELECT 1 FROM omnivia_engineering_discovery_run_events prior
                    WHERE prior.workspace_id = NEW.workspace_id
                      AND prior.discovery_run_id = NEW.discovery_run_id))
               OR (NEW.event_sequence = 2 AND NEW.state IN ('completed', 'failed')
                   AND NEW.selected_count <= r.candidate_budget
                   AND EXISTS (
                       SELECT 1 FROM omnivia_engineering_discovery_run_events queued
                       WHERE queued.workspace_id = NEW.workspace_id
                         AND queued.discovery_run_id = NEW.discovery_run_id
                         AND queued.event_sequence = 1 AND queued.state = 'queued')
                   AND NOT EXISTS (
                       SELECT 1 FROM omnivia_engineering_discovery_run_events terminal
                       WHERE terminal.workspace_id = NEW.workspace_id
                         AND terminal.discovery_run_id = NEW.discovery_run_id
                         AND terminal.event_sequence = 2)
                   AND ((NEW.state = 'completed'
                         AND NEW.selected_count = (
                             SELECT COUNT(*)
                             FROM omnivia_engineering_discovery_candidate_observations o
                             WHERE o.workspace_id = NEW.workspace_id
                               AND o.discovery_run_id = NEW.discovery_run_id))
                        OR (NEW.state = 'failed'
                            AND NEW.selected_count = 0
                            AND NOT EXISTS (
                                SELECT 1
                                FROM omnivia_engineering_discovery_candidate_observations o
                                WHERE o.workspace_id = NEW.workspace_id
                                  AND o.discovery_run_id = NEW.discovery_run_id)
                            AND NOT EXISTS (
                                SELECT 1
                                FROM omnivia_engineering_relation_candidates AS c
                                INDEXED BY omnivia_idx_engineering_relation_candidates_first_run
                                WHERE c.workspace_id = NEW.workspace_id
                                  AND c.first_discovery_run_id = NEW.discovery_run_id))))));
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_engineering_discovery_run_events_update
BEFORE UPDATE ON omnivia_engineering_discovery_run_events
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_engineering_discovery_run_events is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_engineering_discovery_run_events_delete
BEFORE DELETE ON omnivia_engineering_discovery_run_events
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_engineering_discovery_run_events is append-only; DELETE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_engineering_relation_candidates_insert
BEFORE INSERT ON omnivia_engineering_relation_candidates
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_engineering_relation_candidates')
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
    SELECT RAISE(ABORT, 'omnivia: scoped_difference requires an immutable trusted checkout binding')
    WHERE NEW.scope_classification = 'scoped_difference';
    SELECT RAISE(ABORT, 'omnivia: a relation candidate must bind two exact sealed visible projections and its first run anchor')
    WHERE NOT EXISTS (
        SELECT 1
        FROM omnivia_engineering_discovery_runs r
        JOIN omnivia_engineering_discovery_run_events q
          ON q.workspace_id = r.workspace_id AND q.discovery_run_id = r.discovery_run_id
         AND q.event_sequence = 1 AND q.state = 'queued'
        JOIN omnivia_governed_version_assemblies a
          ON a.workspace_id = r.workspace_id
         AND a.assembly_id = NEW.endpoint_a_assembly_id
         AND a.governed_record_id = NEW.endpoint_a_record_id
         AND a.governed_record_version_id = NEW.endpoint_a_version
         AND a.content_digest = NEW.endpoint_a_digest
        JOIN omnivia_governed_version_seals sa
          ON sa.workspace_id = a.workspace_id AND sa.assembly_id = a.assembly_id
         AND sa.governed_record_version_id = a.governed_record_version_id
        JOIN omnivia_engineering_preview_projection pa
          ON pa.workspace_id = a.workspace_id AND pa.assembly_id = a.assembly_id
         AND pa.projection_version = 1 AND pa.content_digest = a.content_digest
        JOIN omnivia_governed_version_assemblies b
          ON b.workspace_id = r.workspace_id
         AND b.assembly_id = NEW.endpoint_b_assembly_id
         AND b.governed_record_id = NEW.endpoint_b_record_id
         AND b.governed_record_version_id = NEW.endpoint_b_version
         AND b.content_digest = NEW.endpoint_b_digest
        JOIN omnivia_governed_version_seals sb
          ON sb.workspace_id = b.workspace_id AND sb.assembly_id = b.assembly_id
         AND sb.governed_record_version_id = b.governed_record_version_id
        JOIN omnivia_engineering_preview_projection pb
          ON pb.workspace_id = b.workspace_id AND pb.assembly_id = b.assembly_id
         AND pb.projection_version = 1 AND pb.content_digest = b.content_digest
        WHERE r.workspace_id = NEW.workspace_id
          AND r.discovery_run_id = NEW.first_discovery_run_id
          AND r.detector_version = NEW.detector_version
          AND r.resolution_instant_us = NEW.recorded_at_us
          AND r.anchor_assembly_id IN (NEW.endpoint_a_assembly_id,
                                       NEW.endpoint_b_assembly_id)
          AND a.record_type IN ('knowledge.finding', 'knowledge.risk', 'knowledge.decision')
          AND b.record_type IN ('knowledge.finding', 'knowledge.risk', 'knowledge.decision')
          AND a.domain_scope = 'engineering.codebase'
          AND b.domain_scope = 'engineering.codebase'
          AND a.recorded_at_us <= r.resolution_instant_us
          AND b.recorded_at_us <= r.resolution_instant_us
          AND ((a.layer = 'candidate' AND a.governance_disposition IS NULL
                AND NOT EXISTS (
                    SELECT 1 FROM omnivia_application_governance_transitions ta
                    WHERE ta.workspace_id = a.workspace_id
                      AND ta.source_assembly_id = a.assembly_id
                      AND ta.source_record_version_id = a.governed_record_version_id
                      AND ta.settled_at_us <= r.resolution_instant_us))
               OR (a.layer = 'governed' AND a.governance_disposition = 'accepted'
                   AND a.authority_level = 'canonical'
                   AND (EXISTS (
                       SELECT 1 FROM omnivia_record_supersessions rsa
                       WHERE rsa.workspace_id = a.workspace_id
                         AND rsa.source_version_id = a.governed_record_version_id
                         AND rsa.recorded_at_us <= r.resolution_instant_us)
                        OR (a.valid_from_us <= r.resolution_instant_us
                            AND (a.valid_to_us IS NULL
                                 OR r.resolution_instant_us < a.valid_to_us)
                            AND NOT EXISTS (
                                SELECT 1 FROM omnivia_record_supersessions rsa
                                WHERE rsa.workspace_id = a.workspace_id
                                  AND rsa.source_version_id = a.governed_record_version_id
                                  AND rsa.recorded_at_us <= r.resolution_instant_us)))))
          AND ((b.layer = 'candidate' AND b.governance_disposition IS NULL
                AND NOT EXISTS (
                    SELECT 1 FROM omnivia_application_governance_transitions tb
                    WHERE tb.workspace_id = b.workspace_id
                      AND tb.source_assembly_id = b.assembly_id
                      AND tb.source_record_version_id = b.governed_record_version_id
                      AND tb.settled_at_us <= r.resolution_instant_us))
               OR (b.layer = 'governed' AND b.governance_disposition = 'accepted'
                   AND b.authority_level = 'canonical'
                   AND (EXISTS (
                       SELECT 1 FROM omnivia_record_supersessions rsb
                       WHERE rsb.workspace_id = b.workspace_id
                         AND rsb.source_version_id = b.governed_record_version_id
                         AND rsb.recorded_at_us <= r.resolution_instant_us)
                        OR (b.valid_from_us <= r.resolution_instant_us
                            AND (b.valid_to_us IS NULL
                                 OR r.resolution_instant_us < b.valid_to_us)
                            AND NOT EXISTS (
                                SELECT 1 FROM omnivia_record_supersessions rsb
                                WHERE rsb.workspace_id = b.workspace_id
                                  AND rsb.source_version_id = b.governed_record_version_id
                                  AND rsb.recorded_at_us <= r.resolution_instant_us)))))
          AND NOT EXISTS (
              SELECT 1 FROM omnivia_engineering_discovery_run_events terminal
              WHERE terminal.workspace_id = r.workspace_id
                AND terminal.discovery_run_id = r.discovery_run_id
                AND terminal.event_sequence = 2));
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_engineering_relation_candidates_update
BEFORE UPDATE ON omnivia_engineering_relation_candidates
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_engineering_relation_candidates is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_engineering_relation_candidates_delete
BEFORE DELETE ON omnivia_engineering_relation_candidates
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_engineering_relation_candidates is append-only; DELETE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_engineering_discovery_candidate_observations_insert
BEFORE INSERT ON omnivia_engineering_discovery_candidate_observations
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_engineering_discovery_candidate_observations')
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
    SELECT RAISE(ABORT, 'omnivia: discovery observations are ordered, bounded, anchor-related, structural-first evidence')
    WHERE NOT EXISTS (
        SELECT 1
        FROM omnivia_engineering_discovery_runs r
        JOIN omnivia_engineering_discovery_run_events q
          ON q.workspace_id = r.workspace_id AND q.discovery_run_id = r.discovery_run_id
         AND q.event_sequence = 1 AND q.state = 'queued'
        JOIN omnivia_engineering_relation_candidates c
          ON c.workspace_id = r.workspace_id
         AND c.relation_candidate_id = NEW.relation_candidate_id
         AND c.detector_version = r.detector_version
        WHERE r.workspace_id = NEW.workspace_id
          AND r.discovery_run_id = NEW.discovery_run_id
          AND r.anchor_assembly_id IN (c.endpoint_a_assembly_id, c.endpoint_b_assembly_id)
          AND NEW.recorded_at_us >= r.enqueued_at_us
          AND NEW.selected_order <= r.candidate_budget
          AND NEW.selected_order = 1 + (
              SELECT COUNT(*)
              FROM omnivia_engineering_discovery_candidate_observations prior
              WHERE prior.workspace_id = NEW.workspace_id
                AND prior.discovery_run_id = NEW.discovery_run_id)
          AND NOT EXISTS (
              SELECT 1 FROM omnivia_engineering_discovery_run_events terminal
              WHERE terminal.workspace_id = r.workspace_id
                AND terminal.discovery_run_id = r.discovery_run_id
                AND terminal.event_sequence = 2)
          AND (NEW.channel = 'lexical' OR NOT EXISTS (
              SELECT 1 FROM omnivia_engineering_discovery_candidate_observations prior
              WHERE prior.workspace_id = NEW.workspace_id
                AND prior.discovery_run_id = NEW.discovery_run_id
                AND prior.channel = 'lexical')));
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_engineering_discovery_candidate_observations_update
BEFORE UPDATE ON omnivia_engineering_discovery_candidate_observations
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_engineering_discovery_candidate_observations is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_engineering_discovery_candidate_observations_delete
BEFORE DELETE ON omnivia_engineering_discovery_candidate_observations
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_engineering_discovery_candidate_observations is append-only; DELETE is never permitted');
END;
