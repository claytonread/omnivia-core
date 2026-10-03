-- Engineering scoped-difference classification (SPEC-CORE-ENGMEM-001
-- AC-050, spec §14.2).
--
-- Additive only; allocation 0063 (Engineering Memory, predecessor 0062).
--
-- 0055's candidate insert guard refused every `scoped_difference` because no
-- immutable snapshot-to-checkout binding existed. 0057 now provides one:
-- a sealed capture header, the checkout it names, and a committed `captured_v1`
-- source event whose stream origin binds the same checkout. This migration adds
--
--   omnivia_engineering_snapshot_checkout_proofs
--       one row per snapshot with that complete joined proof: a complete
--       capture header and snapshot, a checkout that still maps to the
--       header's repository and installation, and at least one committed
--       captured event on a stream origin agreeing on repository,
--       installation and checkout. Nothing here reads a label, path, stream
--       id, branch or remote; absent, incomplete or disagreeing evidence
--       yields no row.
--
-- and replaces the candidate insert guard (SQLite cannot alter a trigger body)
-- with every 0055 predicate intact except the blanket scoped_difference
-- refusal, which now admits one only when both endpoints' stored applicability
-- (repository, snapshot) each have a proof row for the same repository and the
-- two proofs name distinct checkouts. The guard reads the proof itself and
-- never trusts the writer. `unresolved_overlap` stays admissible for any pair.

CREATE VIEW IF NOT EXISTS omnivia_engineering_snapshot_checkout_proofs AS
SELECT c.workspace_id, c.snapshot_id, c.repository_id, c.checkout_id
FROM omnivia_engineering_snapshot_captures c
JOIN omnivia_engineering_snapshots sn
  ON sn.workspace_id = c.workspace_id AND sn.snapshot_id = c.snapshot_id
 AND sn.repository_id = c.repository_id AND sn.capture_status = 'complete'
JOIN omnivia_engineering_checkouts k
  ON k.workspace_id = c.workspace_id AND k.checkout_id = c.checkout_id
 AND k.repository_id = c.repository_id AND k.installation_id = c.installation_id
WHERE c.capture_status = 'complete'
  AND EXISTS (
      SELECT 1
      FROM omnivia_engineering_source_events e
      JOIN omnivia_engineering_source_stream_origins o
        ON o.workspace_id = e.workspace_id AND o.stream_id = e.stream_id
      WHERE e.workspace_id = c.workspace_id AND e.snapshot_id = c.snapshot_id
        AND e.manifest_format = 'captured_v1'
        AND o.repository_id = c.repository_id
        AND o.installation_id = c.installation_id
        AND o.checkout_id = c.checkout_id);

DROP TRIGGER omnivia_guard_engineering_relation_candidates_insert;

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
    SELECT RAISE(ABORT, 'omnivia: scoped_difference requires immutable trusted checkout proof that both endpoint snapshots belong to one repository and distinct checkouts')
    WHERE NEW.scope_classification = 'scoped_difference'
      AND NOT EXISTS (
          SELECT 1
          FROM omnivia_engineering_preview_projection pa
          JOIN omnivia_engineering_snapshot_checkout_proofs xa
            ON xa.workspace_id = pa.workspace_id AND xa.snapshot_id = pa.snapshot_id
           AND xa.repository_id = pa.repository_id
          JOIN omnivia_engineering_preview_projection pb
            ON pb.workspace_id = pa.workspace_id
          JOIN omnivia_engineering_snapshot_checkout_proofs xb
            ON xb.workspace_id = pb.workspace_id AND xb.snapshot_id = pb.snapshot_id
           AND xb.repository_id = pb.repository_id
          WHERE pa.workspace_id = NEW.workspace_id
            AND pa.assembly_id = NEW.endpoint_a_assembly_id
            AND pa.projection_version = 1 AND pa.content_digest = NEW.endpoint_a_digest
            AND pb.assembly_id = NEW.endpoint_b_assembly_id
            AND pb.projection_version = 1 AND pb.content_digest = NEW.endpoint_b_digest
            AND xa.repository_id = xb.repository_id
            AND xa.checkout_id <> xb.checkout_id);
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
