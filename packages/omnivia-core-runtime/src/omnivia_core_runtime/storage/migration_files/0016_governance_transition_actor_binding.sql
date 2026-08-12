-- Preserve authenticated transition authorship without corrupting claim authorship.
--
-- Migration 0014 required every target provenance event to use the authenticated
-- transition actor.  That is correct for reviewer-authored governed versions, but
-- impossible for knowledge.propose: the M3 candidate seal correctly requires its
-- candidate.human_proposed event to retain the claim assertion actor.  The transition
-- row and M1 audit still bind the authenticated contributor; only the target-event
-- predicate distinguishes the claim-authored candidate from reviewer decisions.

DROP TRIGGER IF EXISTS omnivia_guard_application_governance_transitions_consistency;

CREATE TRIGGER omnivia_guard_application_governance_transitions_consistency
BEFORE INSERT ON omnivia_application_governance_transitions
BEGIN
    SELECT RAISE(ABORT, 'omnivia: application transition endpoints are not the named sealed versions of one record')
    WHERE NOT EXISTS (
        SELECT 1
        FROM omnivia_governed_version_assemblies src
        JOIN omnivia_governed_version_seals ss
          ON ss.workspace_id = src.workspace_id
         AND ss.assembly_id = src.assembly_id
         AND ss.governed_record_version_id = src.governed_record_version_id
        JOIN omnivia_governed_version_assemblies dst
          ON dst.workspace_id = src.workspace_id
         AND dst.governed_record_id = src.governed_record_id
        JOIN omnivia_governed_version_seals ds
          ON ds.workspace_id = dst.workspace_id
         AND ds.assembly_id = dst.assembly_id
         AND ds.governed_record_version_id = dst.governed_record_version_id
        WHERE src.workspace_id = NEW.workspace_id
          AND src.governed_record_id = NEW.governed_record_id
          AND src.assembly_id = NEW.source_assembly_id
          AND src.governed_record_version_id = NEW.source_record_version_id
          AND dst.assembly_id = NEW.target_assembly_id
          AND dst.governed_record_version_id = NEW.target_record_version_id
    );
    SELECT RAISE(ABORT, 'omnivia: application transition target does not own the exact operation audit and settlement')
    WHERE NOT EXISTS (
        SELECT 1
        FROM omnivia_application_claim_lineage l
        JOIN omnivia_governed_version_assemblies a
          ON a.workspace_id = l.workspace_id AND a.assembly_id = l.assembly_id
        JOIN omnivia_application_audit_events e
          ON e.audit_ref = l.audit_ref AND e.workspace_id = l.workspace_id
        WHERE l.workspace_id = NEW.workspace_id
          AND l.assembly_id = NEW.target_assembly_id
          AND l.governed_record_version_id = NEW.target_record_version_id
          AND l.operation = NEW.operation
          AND l.audit_ref = NEW.audit_ref
          AND l.settled_at_us = NEW.settled_at_us
          AND a.recorded_at_us = NEW.settled_at_us
          AND e.operation = NEW.operation
          AND e.principal_id = NEW.actor_id
          AND e.recorded_at_us = NEW.settled_at_us
          AND (
              (NEW.operation = 'knowledge.propose' AND EXISTS (
                  SELECT 1
                  FROM omnivia_governed_provenance_events p
                  WHERE p.workspace_id = NEW.workspace_id
                    AND p.assembly_id = NEW.target_assembly_id
                    AND p.governed_record_version_id = NEW.target_record_version_id
                    AND p.audit_ref = NEW.audit_ref
                    AND p.action = 'candidate.human_proposed'
                    AND p.actor_id = a.assertion_actor_id
                    AND p.actor_kind = a.assertion_actor_kind
                    AND p.actor_role = a.assertion_actor_role
              ))
              OR (NEW.operation <> 'knowledge.propose' AND EXISTS (
                  SELECT 1
                  FROM omnivia_governed_provenance_events p
                  WHERE p.workspace_id = NEW.workspace_id
                    AND p.assembly_id = NEW.target_assembly_id
                    AND p.governed_record_version_id = NEW.target_record_version_id
                    AND p.audit_ref = NEW.audit_ref
                    AND p.actor_id = NEW.actor_id
                    AND p.actor_kind = NEW.actor_kind
              ))
          )
    );
    SELECT RAISE(ABORT, 'omnivia: application transition does not follow the accepted public state matrix')
    WHERE NOT EXISTS (
        SELECT 1
        FROM omnivia_application_claim_lineage src_l
        JOIN omnivia_governed_version_assemblies src
          ON src.workspace_id = src_l.workspace_id AND src.assembly_id = src_l.assembly_id
        JOIN omnivia_governed_version_assemblies dst
          ON dst.workspace_id = NEW.workspace_id AND dst.assembly_id = NEW.target_assembly_id
        WHERE src_l.workspace_id = NEW.workspace_id
          AND src_l.assembly_id = NEW.source_assembly_id
          AND src_l.governed_record_version_id = NEW.source_record_version_id
          AND (
            (NEW.operation = 'knowledge.propose'
             AND src_l.operation = 'memory.create'
             AND src.layer = 'candidate' AND src.authority_level = 'proposed'
             AND dst.layer = 'candidate' AND dst.authority_level = 'proposed')
            OR (NEW.operation IN ('candidate.approve', 'candidate.reject')
                AND src_l.operation = 'knowledge.propose'
                AND src.layer = 'candidate' AND src.authority_level = 'proposed'
                AND ((NEW.operation = 'candidate.approve' AND dst.layer = 'governed' AND dst.governance_disposition = 'accepted' AND dst.authority_level = 'canonical')
                     OR (NEW.operation = 'candidate.reject' AND dst.layer = 'governed' AND dst.governance_disposition = 'rejected' AND dst.authority_level = 'reviewed')))
            OR (NEW.operation = 'record.supersede'
                AND src_l.operation IN ('candidate.approve', 'record.supersede')
                AND src.layer = 'governed' AND src.governance_disposition = 'accepted' AND src.authority_level = 'canonical'
                AND dst.layer = 'governed' AND dst.governance_disposition = 'accepted' AND dst.authority_level = 'canonical')
          )
    );
    SELECT RAISE(ABORT, 'omnivia: claim-preserving transition changed canonical claim lineage')
    WHERE NEW.operation <> 'record.supersede'
      AND NOT EXISTS (
        SELECT 1
        FROM omnivia_application_claim_lineage src
        JOIN omnivia_application_claim_lineage dst
          ON dst.workspace_id = src.workspace_id
        WHERE src.workspace_id = NEW.workspace_id
          AND src.assembly_id = NEW.source_assembly_id
          AND dst.assembly_id = NEW.target_assembly_id
          AND dst.claim_json = src.claim_json
          AND dst.claim_digest = src.claim_digest
          AND dst.claim_byte_length = src.claim_byte_length
          AND dst.claim_ingested_at_us = src.claim_ingested_at_us
    );
    SELECT RAISE(ABORT, 'omnivia: record.supersede replacement claim was not ingested at settlement start')
    WHERE NEW.operation = 'record.supersede'
      AND NOT EXISTS (
        SELECT 1 FROM omnivia_application_claim_lineage dst
        WHERE dst.workspace_id = NEW.workspace_id
          AND dst.assembly_id = NEW.target_assembly_id
          AND dst.claim_ingested_at_us = NEW.settled_at_us
    );
    SELECT RAISE(ABORT, 'omnivia: non-superseding public transition fabricated a canonical supersession edge')
    WHERE NEW.operation <> 'record.supersede'
      AND EXISTS (
        SELECT 1 FROM omnivia_record_supersessions r
        WHERE r.workspace_id = NEW.workspace_id
          AND r.governed_record_id = NEW.governed_record_id
          AND r.source_version_id = NEW.source_record_version_id
          AND r.target_version_id = NEW.target_record_version_id
    );
    SELECT RAISE(ABORT, 'omnivia: record.supersede transition disagrees with its canonical supersession edge')
    WHERE NEW.operation = 'record.supersede'
      AND NOT EXISTS (
        SELECT 1
        FROM omnivia_record_supersessions r
        JOIN omnivia_governed_provenance_events p
          ON p.workspace_id = r.workspace_id
         AND p.assembly_id = r.assembly_id
         AND p.provenance_event_id = r.provenance_event_id
        WHERE r.workspace_id = NEW.workspace_id
          AND r.assembly_id = NEW.target_assembly_id
          AND r.governed_record_id = NEW.governed_record_id
          AND r.source_version_id = NEW.source_record_version_id
          AND r.target_version_id = NEW.target_record_version_id
          AND r.correlation_kind = 'm1_audit'
          AND r.correlation_id = NEW.audit_ref
          AND r.recorded_at_us = NEW.settled_at_us
          AND p.action = 'record.superseded'
          AND p.actor_id = NEW.actor_id
          AND p.actor_kind = NEW.actor_kind
          AND p.reason_code = NEW.reason_code
          AND p.reason_comment IS NEW.reason_comment
          AND p.audit_ref = NEW.audit_ref
          AND p.recorded_at_us = NEW.settled_at_us
    );
END;
