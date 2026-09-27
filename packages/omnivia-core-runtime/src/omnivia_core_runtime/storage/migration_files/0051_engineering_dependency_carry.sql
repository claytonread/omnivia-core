-- Engineering dependency sets carried across claim-preserving governance
-- (SPEC-CORE-ENGMEM-001, plan P0-04; spec §15).
--
-- Additive only; allocation 0051 (Engineering Memory, predecessor 0050). No
-- table, column or index changes. `knowledge.propose` and `candidate.approve`
-- mint a new exact version whose content, claim and evidence are byte copies of
-- the version they transition, so a sealed dependency set may travel with them.
-- SQLite cannot alter a trigger, so 0050's dependency-set INSERT guard is replaced
-- under its own name. Every 0050 check stays as it was, and exactly one path is
-- added: a set carried by `knowledge.propose` or `candidate.approve`, admitted
-- only when
--
--   - the set's audit is that operation's own audit event, its record version is
--     the target of that audited transition, and it is written inside that same
--     settlement: the claim is recorded and no outcome has settled it yet;
--   - the transition's two exact versions agree on content, claim, evidence
--     disposition and the set of linked evidence;
--   - the source version's set is consistent: it seals exactly its stored rows,
--     all under its own audit, each whole-file one with its expected digest;
--   - the carried set repeats the source's baseline (repository, stream,
--     snapshot), producer, producer version, coverage and count, and its rows
--     are the source rows as a multiset of (selector type, selector, meaning,
--     producer, expected digest).
--
-- Anything else is refused: `candidate.reject`, `record.supersede` and every other
-- operation, an insert outside the transition's own settlement, and one whose
-- source set is missing or inconsistent. A carried set proves nothing by itself:
-- the evaluator still checks the baseline and target manifests, evidence, stream
-- and repository identity and every digest on each read. Review never writes a
-- set. The 0050 sealed, UPDATE and DELETE guards keep a carried set as immutable
-- as an original one.
--
-- The settlement check reads the acting principal's claims for that one operation
-- through the claim scope index; every other lookup is keyed. The row comparison
-- is bounded by the 64-dependency cap and names 0050's version index explicitly:
-- without statistics the planner otherwise walks the workspace's whole
-- dependency table for each compared row.

DROP TRIGGER omnivia_guard_omnivia_engineering_dependency_sets_insert;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_omnivia_engineering_dependency_sets_insert
BEFORE INSERT ON omnivia_engineering_dependency_sets
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_engineering_dependency_sets')
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
    SELECT RAISE(ABORT, 'omnivia: a dependency set belongs to an exact record version proposed by its own audited memory.create or carried to it by its own audited knowledge.propose or candidate.approve')
    WHERE NOT EXISTS (
            SELECT 1 FROM omnivia_governed_version_assemblies v
            WHERE v.workspace_id = NEW.workspace_id
              AND v.governed_record_id = NEW.record_id
              AND v.governed_record_version_id = NEW.version
              AND v.audit_ref = NEW.audit_ref)
       OR NOT EXISTS (
            SELECT 1 FROM omnivia_application_audit_events a
            WHERE a.audit_ref = NEW.audit_ref AND a.workspace_id = NEW.workspace_id
              AND a.operation IN ('memory.create', 'knowledge.propose', 'candidate.approve'));
    SELECT RAISE(ABORT, 'omnivia: a carried dependency set repeats the consistent sealed set of the exact version its own unsettled claim-preserving transition copied')
    WHERE EXISTS (
            SELECT 1 FROM omnivia_application_audit_events a
            WHERE a.audit_ref = NEW.audit_ref AND a.workspace_id = NEW.workspace_id
              AND a.operation IN ('knowledge.propose', 'candidate.approve'))
      AND NOT EXISTS (
            SELECT 1
            FROM omnivia_application_governance_transitions t
            JOIN omnivia_application_audit_events a
              ON a.audit_ref = t.audit_ref AND a.workspace_id = t.workspace_id
             AND a.operation = t.operation
            JOIN omnivia_idempotency_claims c
              ON c.workspace_id = a.workspace_id AND c.principal_id = a.principal_id
             AND c.operation = a.operation AND c.audit_ref = a.audit_ref
            JOIN omnivia_governed_version_assemblies src
              ON src.workspace_id = t.workspace_id AND src.assembly_id = t.source_assembly_id
             AND src.governed_record_version_id = t.source_record_version_id
            JOIN omnivia_governed_version_assemblies dst
              ON dst.workspace_id = t.workspace_id AND dst.assembly_id = t.target_assembly_id
             AND dst.governed_record_version_id = t.target_record_version_id
            JOIN omnivia_application_claim_lineage src_l
              ON src_l.workspace_id = src.workspace_id AND src_l.assembly_id = src.assembly_id
            JOIN omnivia_application_claim_lineage dst_l
              ON dst_l.workspace_id = dst.workspace_id AND dst_l.assembly_id = dst.assembly_id
            JOIN omnivia_engineering_dependency_sets s
              ON s.workspace_id = t.workspace_id AND s.record_id = t.governed_record_id
             AND s.version = t.source_record_version_id
            WHERE t.workspace_id = NEW.workspace_id
              AND t.governed_record_id = NEW.record_id
              AND t.target_record_version_id = NEW.version
              AND t.audit_ref = NEW.audit_ref
              AND t.operation IN ('knowledge.propose', 'candidate.approve')
              AND t.settled_at_us = NEW.recorded_at_us
              AND NOT EXISTS (
                    SELECT 1 FROM omnivia_idempotency_outcomes o
                    WHERE o.claim_id = c.claim_id)
              AND dst.content_json = src.content_json
              AND dst.content_digest = src.content_digest
              AND dst.evidence_disposition = src.evidence_disposition
              AND dst_l.claim_json = src_l.claim_json
              AND dst_l.claim_digest = src_l.claim_digest
              AND NOT EXISTS (
                    SELECT 1 FROM omnivia_governed_version_evidence_links sl
                    WHERE sl.workspace_id = src.workspace_id AND sl.assembly_id = src.assembly_id
                      AND NOT EXISTS (
                            SELECT 1 FROM omnivia_governed_version_evidence_links dl
                            WHERE dl.workspace_id = dst.workspace_id
                              AND dl.assembly_id = dst.assembly_id
                              AND dl.evidence_id = sl.evidence_id))
              AND NOT EXISTS (
                    SELECT 1 FROM omnivia_governed_version_evidence_links dl
                    WHERE dl.workspace_id = dst.workspace_id AND dl.assembly_id = dst.assembly_id
                      AND NOT EXISTS (
                            SELECT 1 FROM omnivia_governed_version_evidence_links sl
                            WHERE sl.workspace_id = src.workspace_id
                              AND sl.assembly_id = src.assembly_id
                              AND sl.evidence_id = dl.evidence_id))
              AND s.repository_id = NEW.repository_id
              AND s.stream_id = NEW.stream_id
              AND s.snapshot_id = NEW.snapshot_id
              AND s.producer = NEW.producer
              AND s.producer_version = NEW.producer_version
              AND s.coverage = NEW.coverage
              AND s.dependency_count = NEW.dependency_count
              AND (SELECT COUNT(*) FROM omnivia_engineering_dependencies d
                   INDEXED BY omnivia_idx_engineering_dependencies_version
                   WHERE d.workspace_id = s.workspace_id AND d.record_id = s.record_id
                     AND d.version = s.version) = s.dependency_count
              AND NOT EXISTS (
                    SELECT 1 FROM omnivia_engineering_dependencies d
                    INDEXED BY omnivia_idx_engineering_dependencies_version
                    WHERE d.workspace_id = s.workspace_id AND d.record_id = s.record_id
                      AND d.version = s.version
                      AND (d.audit_ref IS NOT s.audit_ref
                           OR (d.selector_type = 'whole_file' AND d.expected_digest IS NULL)))
              AND NOT EXISTS (
                    SELECT 1 FROM omnivia_engineering_dependencies d
                    INDEXED BY omnivia_idx_engineering_dependencies_version
                    WHERE d.workspace_id = NEW.workspace_id AND d.record_id = NEW.record_id
                      AND d.version = NEW.version
                      AND (SELECT COUNT(*) FROM omnivia_engineering_dependencies x
                           INDEXED BY omnivia_idx_engineering_dependencies_version
                           WHERE x.workspace_id = NEW.workspace_id
                             AND x.record_id = NEW.record_id AND x.version = NEW.version
                             AND x.selector_type = d.selector_type AND x.selector = d.selector
                             AND x.meaning = d.meaning AND x.producer = d.producer
                             AND x.expected_digest IS d.expected_digest)
                          IS NOT (SELECT COUNT(*) FROM omnivia_engineering_dependencies x
                           INDEXED BY omnivia_idx_engineering_dependencies_version
                           WHERE x.workspace_id = s.workspace_id
                             AND x.record_id = s.record_id AND x.version = s.version
                             AND x.selector_type = d.selector_type AND x.selector = d.selector
                             AND x.meaning = d.meaning AND x.producer = d.producer
                             AND x.expected_digest IS d.expected_digest)));
    SELECT RAISE(ABORT, 'omnivia: a dependency set baseline must be a recorded source event of its stated repository and stream')
    WHERE NOT EXISTS (
            SELECT 1 FROM omnivia_engineering_source_events e
            JOIN omnivia_engineering_source_streams st
              ON st.workspace_id = e.workspace_id AND st.stream_id = e.stream_id
            WHERE e.workspace_id = NEW.workspace_id AND e.snapshot_id = NEW.snapshot_id
              AND e.stream_id = NEW.stream_id AND st.repository_id = NEW.repository_id);
    SELECT RAISE(ABORT, 'omnivia: a dependency set seals exactly its recorded dependencies, each whole-file one with its expected digest')
    WHERE (SELECT COUNT(*) FROM omnivia_engineering_dependencies d
           WHERE d.workspace_id = NEW.workspace_id AND d.record_id = NEW.record_id
             AND d.version = NEW.version) IS NOT NEW.dependency_count
       OR EXISTS (
            SELECT 1 FROM omnivia_engineering_dependencies d
            WHERE d.workspace_id = NEW.workspace_id AND d.record_id = NEW.record_id
              AND d.version = NEW.version
              AND (d.audit_ref IS NOT NEW.audit_ref
                   OR (d.selector_type = 'whole_file' AND d.expected_digest IS NULL)));
END;
