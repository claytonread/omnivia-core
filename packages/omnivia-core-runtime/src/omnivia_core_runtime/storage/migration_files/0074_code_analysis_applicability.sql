-- Governed code-analysis import and atomic repository applicability (P8, W12).
--
-- A verified code-analysis batch becomes ordinary governed knowledge here, through the
-- 0009 tables and nothing beside them: every accepted node is a `knowledge.entity`
-- record and every accepted edge a `knowledge.relation` record whose endpoints are a
-- sealed `omnivia_governed_relation_endpoints` row. What this file adds is the two facts
-- 0009 cannot state on its own:
--
--   the bounded `code.*` relation namespace  -- twelve directed relation types the seal
--                                               trigger now admits, and nothing else;
--   repository applicability                 -- which one staged import is the current
--                                               code graph for one repository, producer
--                                               and producer profile, and since when.
--
-- Six tables, each with one job:
--
--   omnivia_code_analysis_import_policies     the owner-authorised, versioned acceptance
--                                             policy, recorded before any import may cite
--                                             it. Enabling analysis writes nothing here.
--   omnivia_code_analysis_imports             one staged import: the exact snapshot,
--                                             producer, profile, batch and policy it was
--                                             built from, and its declared counts.
--   omnivia_code_analysis_import_files        the files the import covers, each bound to
--                                             the sealed capture index by path and digest.
--   omnivia_code_analysis_import_members      the exact accepted governed versions the
--                                             import contributes to the graph.
--   omnivia_code_analysis_import_diagnostics  every candidate the policy did not accept,
--                                             with the reason; never a graph edge.
--   omnivia_code_analysis_activations         the append-only applicability log. Its
--                                             newest row per scope at an instant is the
--                                             active import at that instant.
--
-- **Staging is not activation.** An import's rows prove nothing until an activation names
-- it, and the activation's BEFORE INSERT trigger is where the cutover rules are enforced:
-- the compare-and-swap fence (the sequence advances by exactly one and names the import it
-- retires), the completeness of the staged import against its declared counts, and the
-- coverage gate (a cutover that drops a still-present file needs a policy that allows it).
-- Once an import has been activated its files, members and diagnostics refuse further
-- inserts, so the set an activation proved complete stays the set it proved.
--
-- **Retirement is a newer activation, never a deletion.** Every table here refuses UPDATE
-- and DELETE. A retired import's governed records are untouched; the resolver simply no
-- longer selects them as current, and an as-of read at an earlier instant still does.
--
-- **The seal trigger is replaced, not weakened.** It is dropped and recreated with exactly
-- two changes to 0009's text: the relation-type allowlist gains the twelve `code.*`
-- types, and those types join `reconciliation.supports` as directed relations exempt from
-- the symmetric-endpoint normalization (a call has a caller and a callee). Every other
-- invariant -- the event sets, the lineage matrix, evidence ancestry, accepted endpoints
-- for an accepted relation, duplicate semantics and the supersession rules -- is 0009's
-- text unchanged. A gate first proves the trigger being replaced is the 0009 one.
--
-- Every comment in this file sits between statements and never inside one: the migrator
-- strips comments while the canonical schema fingerprint replays the text verbatim.

CREATE TABLE omnivia_migration_0074_predecessor_gate (
    singleton INTEGER NOT NULL PRIMARY KEY CHECK (singleton = 1)
) WITHOUT ROWID;

CREATE TRIGGER omnivia_migration_0074_predecessor_gate_insert
BEFORE INSERT ON omnivia_migration_0074_predecessor_gate
BEGIN
    SELECT RAISE(ABORT, 'omnivia: migration 0074 requires the 0009 governed seal trigger')
    WHERE (SELECT COUNT(*) FROM sqlite_master
           WHERE type = 'trigger' AND name = 'omnivia_guard_governed_version_seals_insert') <> 1
       OR instr((SELECT sql FROM sqlite_master WHERE type = 'trigger'
                 AND name = 'omnivia_guard_governed_version_seals_insert'),
                '''reconciliation.supports''
              )') = 0
       OR instr((SELECT sql FROM sqlite_master WHERE type = 'trigger'
                 AND name = 'omnivia_guard_governed_version_seals_insert'),
                'code.') <> 0;
END;

INSERT INTO omnivia_migration_0074_predecessor_gate (singleton) VALUES (1);
DROP TRIGGER omnivia_migration_0074_predecessor_gate_insert;
DROP TABLE omnivia_migration_0074_predecessor_gate;

DROP TRIGGER omnivia_guard_governed_version_seals_insert;

-- 0009's seal trigger, verbatim except for the two `code.*` lists described above.
CREATE TRIGGER IF NOT EXISTS omnivia_guard_governed_version_seals_insert
BEFORE INSERT ON omnivia_governed_version_seals
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_governed_version_seals')
    WHERE omnivia_service_writer() IS NOT 1
       OR NOT EXISTS (
            SELECT 1
            FROM omnivia_mutation_guard g
            JOIN omnivia_workspace_state s ON s.singleton = 1
            JOIN omnivia_workspace_lease l ON l.singleton = 1
            WHERE g.singleton = 1
              AND g.fencing_generation = s.fencing_generation
              AND g.workspace_id = s.workspace_id
              AND l.fencing_generation = g.fencing_generation
              AND l.workspace_id = g.workspace_id
              AND l.service_instance_id = g.service_instance_id
              AND l.lifecycle IN ('acquiring', 'held', 'draining')
       )
       OR NEW.workspace_id IS NOT (
            SELECT workspace_id FROM omnivia_workspace_state WHERE singleton = 1
       );
    SELECT RAISE(ABORT, 'omnivia: version is already sealed')
    WHERE EXISTS (
        SELECT 1 FROM omnivia_governed_version_seals
        WHERE workspace_id = NEW.workspace_id
          AND (seal_id = NEW.seal_id
               OR assembly_id = NEW.assembly_id
               OR governed_record_version_id = NEW.governed_record_version_id)
    );
    SELECT RAISE(ABORT, 'omnivia: context-model assemblies cannot seal under M3')
    WHERE EXISTS (
        SELECT 1 FROM omnivia_governed_version_assemblies a
        WHERE a.workspace_id = NEW.workspace_id
          AND a.assembly_id = NEW.assembly_id
          AND a.layer = 'context_model'
    );
    SELECT RAISE(ABORT, 'omnivia: sealed_at_us precedes the assembly')
    WHERE EXISTS (
        SELECT 1 FROM omnivia_governed_version_assemblies a
        WHERE a.workspace_id = NEW.workspace_id
          AND a.assembly_id = NEW.assembly_id
          AND NEW.sealed_at_us < a.recorded_at_us
    );

    SELECT RAISE(ABORT, 'omnivia: candidate lineage does not match its origin')
    WHERE EXISTS (
        SELECT 1
        FROM omnivia_governed_version_assemblies a
        WHERE a.workspace_id = NEW.workspace_id
          AND a.assembly_id = NEW.assembly_id
          AND (
            (a.candidate_origin = 'extracted' AND (
                (SELECT COUNT(*) FROM omnivia_governed_extraction_lineage x
                 WHERE x.workspace_id = a.workspace_id AND x.assembly_id = a.assembly_id) <> 1
                OR EXISTS (
                    SELECT 1 FROM omnivia_governed_extraction_lineage x
                    JOIN omnivia_governed_provenance_events e
                      ON e.workspace_id = x.workspace_id
                     AND e.assembly_id = x.assembly_id
                     AND e.provenance_event_id = x.provenance_event_id
                    WHERE x.workspace_id = a.workspace_id
                      AND x.assembly_id = a.assembly_id
                      AND (x.extraction_kind <> a.extraction_kind
                           OR x.extraction_run_id <> a.correlation_id
                           OR x.correlation_id <> a.correlation_id
                           OR x.actor_id <> a.assertion_actor_id
                           OR x.actor_kind <> a.assertion_actor_kind
                           OR x.actor_role <> a.assertion_actor_role
                           OR x.evidence_disposition <> a.evidence_disposition
                           OR e.action <> 'candidate.extracted')
                )
                OR EXISTS (SELECT 1 FROM omnivia_governed_legacy_lineage l
                           WHERE l.workspace_id = a.workspace_id AND l.assembly_id = a.assembly_id)
            ))
            OR (a.candidate_origin = 'migrated_legacy_claim' AND (
                (SELECT COUNT(*) FROM omnivia_governed_legacy_lineage l
                 WHERE l.workspace_id = a.workspace_id AND l.assembly_id = a.assembly_id) <> 1
                OR EXISTS (
                    SELECT 1 FROM omnivia_governed_legacy_lineage l
                    JOIN omnivia_governed_provenance_events e
                      ON e.workspace_id = l.workspace_id
                     AND e.assembly_id = l.assembly_id
                     AND e.provenance_event_id = l.provenance_event_id
                    WHERE l.workspace_id = a.workspace_id
                      AND l.assembly_id = a.assembly_id
                      AND (l.migration_run_id <> a.correlation_id
                           OR l.correlation_id <> a.correlation_id
                           OR l.actor_id <> a.assertion_actor_id
                           OR l.actor_kind <> a.assertion_actor_kind
                           OR l.actor_role <> a.assertion_actor_role
                           OR l.evidence_disposition <> a.evidence_disposition
                           OR e.action <> 'candidate.legacy_migrated')
                )
                OR EXISTS (SELECT 1 FROM omnivia_governed_extraction_lineage x
                           WHERE x.workspace_id = a.workspace_id AND x.assembly_id = a.assembly_id)
            ))
            OR ((a.candidate_origin = 'human_proposed' OR a.layer = 'governed')
                AND (EXISTS (SELECT 1 FROM omnivia_governed_extraction_lineage x
                             WHERE x.workspace_id = a.workspace_id AND x.assembly_id = a.assembly_id)
                     OR EXISTS (SELECT 1 FROM omnivia_governed_legacy_lineage l
                                WHERE l.workspace_id = a.workspace_id AND l.assembly_id = a.assembly_id)))
          )
    );

    SELECT RAISE(ABORT, 'omnivia: provenance event set does not match the version variant')
    WHERE EXISTS (
        SELECT 1
        FROM omnivia_governed_version_assemblies a
        WHERE a.workspace_id = NEW.workspace_id
          AND a.assembly_id = NEW.assembly_id
          AND (
            (a.layer = 'candidate' AND (
                (SELECT COUNT(*) FROM omnivia_governed_provenance_events e
                 WHERE e.workspace_id = a.workspace_id AND e.assembly_id = a.assembly_id)
                <> CASE WHEN a.record_type = 'knowledge.relation' THEN 2 ELSE 1 END
                OR (SELECT COUNT(*) FROM omnivia_governed_provenance_events e
                    WHERE e.workspace_id = a.workspace_id AND e.assembly_id = a.assembly_id
                      AND e.action = CASE a.candidate_origin
                          WHEN 'extracted' THEN 'candidate.extracted'
                          WHEN 'human_proposed' THEN 'candidate.human_proposed'
                          ELSE 'candidate.legacy_migrated' END) <> 1
                OR (a.record_type = 'knowledge.relation'
                    AND (SELECT COUNT(*) FROM omnivia_governed_provenance_events e
                         WHERE e.workspace_id = a.workspace_id AND e.assembly_id = a.assembly_id
                           AND e.action = 'relation.asserted') <> 1)
            ))
            OR (a.layer = 'governed' AND (
                (SELECT COUNT(*) FROM omnivia_governed_provenance_events e
                 WHERE e.workspace_id = a.workspace_id AND e.assembly_id = a.assembly_id)
                <> 1
                   + CASE WHEN a.record_type = 'knowledge.relation' THEN 1 ELSE 0 END
                   + CASE WHEN EXISTS (
                       SELECT 1 FROM omnivia_record_supersessions r
                       WHERE r.workspace_id = a.workspace_id AND r.assembly_id = a.assembly_id
                     ) THEN 1 ELSE 0 END
                OR (a.governance_disposition <> 'accepted'
                    AND (SELECT COUNT(*) FROM omnivia_governed_provenance_events e
                         WHERE e.workspace_id = a.workspace_id AND e.assembly_id = a.assembly_id
                           AND e.action = 'governance.' || a.governance_disposition) <> 1)
                OR (a.governance_disposition = 'accepted'
                    AND (SELECT COUNT(*) FROM omnivia_governed_provenance_events e
                         WHERE e.workspace_id = a.workspace_id AND e.assembly_id = a.assembly_id
                           AND e.action IN ('governance.accepted', 'governance.corrected')) <> 1)
                OR (a.record_type = 'knowledge.relation'
                    AND (SELECT COUNT(*) FROM omnivia_governed_provenance_events e
                         WHERE e.workspace_id = a.workspace_id AND e.assembly_id = a.assembly_id
                           AND e.action = 'relation.asserted') <> 1)
                OR (EXISTS (SELECT 1 FROM omnivia_record_supersessions r
                            WHERE r.workspace_id = a.workspace_id AND r.assembly_id = a.assembly_id)
                    AND (SELECT COUNT(*) FROM omnivia_governed_provenance_events e
                         WHERE e.workspace_id = a.workspace_id AND e.assembly_id = a.assembly_id
                           AND e.action = 'record.superseded') <> 1)
            ))
          )
    );

    SELECT RAISE(ABORT, 'omnivia: provenance event metadata does not match the assembly')
    WHERE EXISTS (
        SELECT 1
        FROM omnivia_governed_version_assemblies a
        JOIN omnivia_governed_provenance_events e
          ON e.workspace_id = a.workspace_id AND e.assembly_id = a.assembly_id
        WHERE a.workspace_id = NEW.workspace_id
          AND a.assembly_id = NEW.assembly_id
          AND (e.governed_record_version_id <> a.governed_record_version_id
               OR e.correlation_kind <> a.correlation_kind
               OR e.correlation_id <> a.correlation_id
               OR e.evidence_disposition <> a.evidence_disposition
               OR (a.correlation_kind = 'm1_audit'
                   AND (e.audit_ref IS NOT a.audit_ref OR e.audit_ref IS NULL))
               OR (a.correlation_kind <> 'm1_audit' AND e.audit_ref IS NOT NULL)
               OR (a.layer = 'candidate'
                   AND (e.actor_id IS NOT a.assertion_actor_id
                        OR e.actor_kind IS NOT a.assertion_actor_kind
                        OR e.actor_role IS NOT a.assertion_actor_role
                        OR e.policy_id IS NOT NULL OR e.policy_version IS NOT NULL))
               OR (a.layer = 'governed' AND a.decision_source_kind = 'human_reviewer'
                   AND (e.actor_id IS NOT a.decision_source_id
                        OR e.actor_kind IS NULL OR e.actor_role IS NULL
                        OR e.policy_id IS NOT NULL OR e.policy_version IS NOT NULL))
               OR (a.layer = 'governed' AND a.decision_source_kind = 'policy_evaluator'
                   AND (e.policy_id IS NOT a.decision_source_id
                        OR e.policy_version IS NOT a.authority_policy_version
                        OR e.actor_id IS NOT NULL OR e.actor_kind IS NOT NULL
                        OR e.actor_role IS NOT NULL)))
    );

    SELECT RAISE(ABORT, 'omnivia: governance predecessor is not the required sealed version')
    WHERE EXISTS (
        SELECT 1
        FROM omnivia_governed_version_assemblies a
        JOIN omnivia_governed_provenance_events e
          ON e.workspace_id = a.workspace_id AND e.assembly_id = a.assembly_id
        WHERE a.workspace_id = NEW.workspace_id
          AND a.assembly_id = NEW.assembly_id
          AND ((a.layer = 'candidate'
                AND (e.predecessor_record_id IS NOT NULL
                     OR e.predecessor_version_id IS NOT NULL))
               OR (a.layer = 'governed'
                   AND e.action IN (
                       'governance.accepted', 'governance.rejected',
                       'governance.contested', 'governance.withdrawn',
                       'governance.superseded'
                   )
                   AND NOT EXISTS (
                       SELECT 1
                       FROM omnivia_governed_version_assemblies p
                       JOIN omnivia_governed_version_seals ps
                         ON ps.workspace_id = p.workspace_id
                        AND ps.assembly_id = p.assembly_id
                       WHERE p.workspace_id = a.workspace_id
                         AND p.governed_record_id = a.governed_record_id
                         AND p.governed_record_version_id = e.predecessor_version_id
                         AND e.predecessor_record_id = a.governed_record_id
                         AND p.layer = 'candidate'
                   ))
               OR (a.layer = 'governed'
                   AND e.action = 'governance.corrected'
                   AND NOT EXISTS (
                       SELECT 1
                       FROM omnivia_governed_version_assemblies p
                       JOIN omnivia_governed_version_seals ps
                         ON ps.workspace_id = p.workspace_id
                        AND ps.assembly_id = p.assembly_id
                       JOIN omnivia_record_supersessions r
                         ON r.workspace_id = a.workspace_id
                        AND r.assembly_id = a.assembly_id
                       WHERE p.workspace_id = a.workspace_id
                         AND p.governed_record_id = a.governed_record_id
                         AND p.governed_record_version_id = e.predecessor_version_id
                         AND e.predecessor_record_id = a.governed_record_id
                         AND r.source_version_id = p.governed_record_version_id
                         AND p.layer = 'governed'
                         AND p.governance_disposition = 'accepted'
                   )))
    );

    SELECT RAISE(ABORT, 'omnivia: evidence disposition or ancestry is incomplete')
    WHERE EXISTS (
        SELECT 1
        FROM omnivia_governed_version_assemblies a
        JOIN omnivia_governed_provenance_events e
          ON e.workspace_id = a.workspace_id AND e.assembly_id = a.assembly_id
        WHERE a.workspace_id = NEW.workspace_id
          AND a.assembly_id = NEW.assembly_id
          AND ((e.evidence_disposition = 'available'
                AND NOT EXISTS (
                    SELECT 1 FROM omnivia_governed_version_evidence_links l
                    WHERE l.workspace_id = e.workspace_id
                      AND l.assembly_id = e.assembly_id
                      AND l.provenance_event_id = e.provenance_event_id
                ))
               OR (e.evidence_disposition IN ('unavailable', 'redacted')
                   AND (e.reason_code IS NULL OR EXISTS (
                       SELECT 1 FROM omnivia_governed_version_evidence_links l
                       WHERE l.workspace_id = e.workspace_id
                         AND l.assembly_id = e.assembly_id
                         AND l.provenance_event_id = e.provenance_event_id
                   ))))
    );
    SELECT RAISE(ABORT, 'omnivia: evidence span ancestry does not form one M2 chain')
    WHERE EXISTS (
        SELECT 1
        FROM omnivia_governed_version_evidence_links l
        WHERE l.workspace_id = NEW.workspace_id
          AND l.assembly_id = NEW.assembly_id
          AND l.normalized_span_id IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM omnivia_normalized_source_spans s
              WHERE s.normalized_span_id = l.normalized_span_id
                AND s.normalized_record_id = l.normalized_record_id
                AND s.evidence_id = l.evidence_id
                AND s.workspace_id = l.workspace_id
          )
    );

    SELECT RAISE(ABORT, 'omnivia: relation endpoint/event invariant failed')
    WHERE EXISTS (
        SELECT 1
        FROM omnivia_governed_version_assemblies a
        WHERE a.workspace_id = NEW.workspace_id
          AND a.assembly_id = NEW.assembly_id
          AND ((a.record_type = 'knowledge.relation' AND (
                (SELECT COUNT(*) FROM omnivia_governed_relation_endpoints r
                 WHERE r.workspace_id = a.workspace_id AND r.assembly_id = a.assembly_id) <> 1
                OR (SELECT COUNT(*) FROM omnivia_governed_provenance_events e
                    WHERE e.workspace_id = a.workspace_id AND e.assembly_id = a.assembly_id
                      AND e.action = 'relation.asserted') <> 1
              ))
              OR (a.record_type <> 'knowledge.relation'
                  AND (EXISTS (SELECT 1 FROM omnivia_governed_relation_endpoints r
                               WHERE r.workspace_id = a.workspace_id AND r.assembly_id = a.assembly_id)
                       OR EXISTS (SELECT 1 FROM omnivia_governed_provenance_events e
                                  WHERE e.workspace_id = a.workspace_id AND e.assembly_id = a.assembly_id
                                    AND e.action = 'relation.asserted'))))
    );
    SELECT RAISE(ABORT, 'omnivia: relation type, normalization or endpoint authority failed')
    WHERE EXISTS (
        SELECT 1
        FROM omnivia_governed_version_assemblies a
        JOIN omnivia_governed_relation_endpoints r
          ON r.workspace_id = a.workspace_id AND r.assembly_id = a.assembly_id
        JOIN omnivia_governed_version_assemblies src
          ON src.workspace_id = r.workspace_id
         AND src.governed_record_version_id = r.source_version_id
        JOIN omnivia_governed_version_assemblies dst
          ON dst.workspace_id = r.workspace_id
         AND dst.governed_record_version_id = r.target_version_id
        WHERE a.workspace_id = NEW.workspace_id
          AND a.assembly_id = NEW.assembly_id
          AND (r.relation_type NOT IN (
                    'governance.contradicts',
                    'reconciliation.duplicate_of',
                    'reconciliation.equivalent_to',
                    'reconciliation.related_to',
                    'reconciliation.supports',
                    'code.aliases',
                    'code.calls',
                    'code.constructs',
                    'code.defines',
                    'code.extends',
                    'code.exports',
                    'code.groups',
                    'code.implements',
                    'code.imports',
                    'code.references',
                    'code.reexports',
                    'code.renders'
              )
               OR src.governed_record_id <> r.source_record_id
               OR dst.governed_record_id <> r.target_record_id
               OR NOT EXISTS (
                    SELECT 1 FROM omnivia_governed_provenance_events re
                    WHERE re.workspace_id = r.workspace_id
                      AND re.assembly_id = r.assembly_id
                      AND re.provenance_event_id = r.provenance_event_id
                      AND re.action = 'relation.asserted'
               )
               OR NOT EXISTS (SELECT 1 FROM omnivia_governed_version_seals ss
                              WHERE ss.workspace_id = src.workspace_id
                                AND ss.assembly_id = src.assembly_id)
               OR NOT EXISTS (SELECT 1 FROM omnivia_governed_version_seals ds
                              WHERE ds.workspace_id = dst.workspace_id
                                AND ds.assembly_id = dst.assembly_id)
               OR (r.relation_type NOT IN (
                       'reconciliation.supports',
                       'code.aliases',
                       'code.calls',
                       'code.constructs',
                       'code.defines',
                       'code.extends',
                       'code.exports',
                       'code.groups',
                       'code.implements',
                       'code.imports',
                       'code.references',
                       'code.reexports',
                       'code.renders'
                   )
                   AND NOT (
                       CAST(r.source_record_id AS BLOB) < CAST(r.target_record_id AS BLOB)
                       OR (r.source_record_id = r.target_record_id
                           AND CAST(r.source_version_id AS BLOB) < CAST(r.target_version_id AS BLOB))
                   ))
               OR (a.layer = 'governed' AND a.governance_disposition = 'accepted'
                   AND (src.layer <> 'governed' OR src.governance_disposition <> 'accepted'
                        OR dst.layer <> 'governed' OR dst.governance_disposition <> 'accepted'
                        OR a.evidence_disposition <> 'available')))
    );
    SELECT RAISE(ABORT, 'omnivia: duplicate sealed relation semantics refused')
    WHERE EXISTS (
        SELECT 1
        FROM omnivia_governed_version_assemblies a
        JOIN omnivia_governed_relation_endpoints r
          ON r.workspace_id = a.workspace_id AND r.assembly_id = a.assembly_id
        JOIN omnivia_governed_relation_endpoints oldr
          ON oldr.workspace_id = r.workspace_id
         AND oldr.relation_type = r.relation_type
         AND oldr.source_record_id = r.source_record_id
         AND oldr.source_version_id = r.source_version_id
         AND oldr.target_record_id = r.target_record_id
         AND oldr.target_version_id = r.target_version_id
        JOIN omnivia_governed_version_assemblies olda
          ON olda.workspace_id = oldr.workspace_id AND olda.assembly_id = oldr.assembly_id
        JOIN omnivia_governed_version_seals olds
          ON olds.workspace_id = olda.workspace_id AND olds.assembly_id = olda.assembly_id
        WHERE a.workspace_id = NEW.workspace_id
          AND a.assembly_id = NEW.assembly_id
          AND olda.domain_scope = a.domain_scope
          AND olda.valid_from_us = a.valid_from_us
          AND olda.valid_to_us IS a.valid_to_us
          AND olda.content_digest = a.content_digest
    );

    SELECT RAISE(ABORT, 'omnivia: supersession invariant failed')
    WHERE EXISTS (
        SELECT 1
        FROM omnivia_governed_version_assemblies a
        JOIN omnivia_record_supersessions r
          ON r.workspace_id = a.workspace_id AND r.assembly_id = a.assembly_id
        JOIN omnivia_governed_version_assemblies src
          ON src.workspace_id = r.workspace_id
         AND src.governed_record_version_id = r.source_version_id
        WHERE a.workspace_id = NEW.workspace_id
          AND a.assembly_id = NEW.assembly_id
          AND (a.layer <> 'governed'
               OR a.governance_disposition <> 'accepted'
               OR r.target_version_id <> a.governed_record_version_id
               OR r.governed_record_id <> a.governed_record_id
               OR src.governed_record_id <> a.governed_record_id
               OR src.layer <> 'governed'
               OR src.governance_disposition <> 'accepted'
               OR src.recorded_at_us > a.recorded_at_us
               OR NOT EXISTS (SELECT 1 FROM omnivia_governed_version_seals s
                              WHERE s.workspace_id = src.workspace_id
                                AND s.assembly_id = src.assembly_id)
               OR NOT EXISTS (
                    SELECT 1 FROM omnivia_governed_provenance_events e
                    WHERE e.workspace_id = r.workspace_id
                      AND e.assembly_id = r.assembly_id
                      AND e.provenance_event_id = r.provenance_event_id
                      AND e.action = 'record.superseded'
                      AND e.predecessor_record_id = r.governed_record_id
                      AND e.predecessor_version_id = r.source_version_id
               ))
    );
    SELECT RAISE(ABORT, 'omnivia: supersession cycle refused')
    WHERE EXISTS (
        WITH RECURSIVE walk(version_id) AS (
            SELECT r.target_version_id
            FROM omnivia_record_supersessions r
            WHERE r.workspace_id = NEW.workspace_id AND r.assembly_id = NEW.assembly_id
            UNION
            SELECT r.target_version_id
            FROM omnivia_record_supersessions r
            JOIN walk w ON r.source_version_id = w.version_id
            JOIN omnivia_governed_version_seals s
              ON s.workspace_id = r.workspace_id AND s.assembly_id = r.assembly_id
            WHERE r.workspace_id = NEW.workspace_id
        )
        SELECT 1
        FROM walk
        WHERE version_id = (
            SELECT source_version_id FROM omnivia_record_supersessions
            WHERE workspace_id = NEW.workspace_id AND assembly_id = NEW.assembly_id
        )
    );
END;

-- The acceptance policy an owner authorised, exactly as it was digested.
--
-- `authorised_by` is not in the document: it is the principal of the settlement that
-- registered the policy, and the insert guard requires the row's own successful audit
-- event, at exactly `recorded_at_us`, to name that principal. The service's own
-- principal never authorises a policy, so the import's audit can never stand in for it.
--
-- `policy_json` is the canonical policy document and `policy_digest` its digest; the
-- writer computes both and SQL stores, never recomputes, them. `allow_reduced_coverage`
-- is lifted out of the document because the activation gate below has to read it and
-- SQL does not parse the document. One row per `(policy_id, policy_version)`, forever:
-- a changed policy is a new version.
CREATE TABLE IF NOT EXISTS omnivia_code_analysis_import_policies (
    workspace_id           TEXT    NOT NULL,
    policy_id              TEXT    NOT NULL,
    policy_version         TEXT    NOT NULL,
    policy_digest          TEXT    NOT NULL,
    policy_json            TEXT    NOT NULL,
    authorised_by          TEXT    NOT NULL,
    allow_reduced_coverage INTEGER NOT NULL,
    audit_ref              TEXT    NOT NULL,
    recorded_at_us         INTEGER NOT NULL,

    PRIMARY KEY (workspace_id, policy_id, policy_version),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'),
    CHECK (typeof(policy_id) = 'text' AND length(policy_id) BETWEEN 1 AND 128
           AND policy_id GLOB '[A-Za-z0-9]*'
           AND policy_id NOT GLOB '*[^A-Za-z0-9._:-]*'),
    CHECK (typeof(policy_version) = 'text' AND length(policy_version) BETWEEN 1 AND 128
           AND policy_version GLOB '[A-Za-z0-9]*'
           AND policy_version NOT GLOB '*[^A-Za-z0-9._:-]*'),
    CHECK (typeof(policy_digest) = 'text' AND length(policy_digest) = 71
           AND substr(policy_digest, 1, 7) = 'sha256:'
           AND substr(policy_digest, 8) NOT GLOB '*[^0-9a-f]*'),
    CHECK (typeof(policy_json) = 'text'
           AND length(CAST(policy_json AS BLOB)) BETWEEN 2 AND 65536
           AND instr(policy_json, char(0)) = 0),
    CHECK (typeof(authorised_by) = 'text' AND length(authorised_by) BETWEEN 1 AND 128
           AND authorised_by GLOB '[A-Za-z0-9]*'
           AND authorised_by NOT GLOB '*[^A-Za-z0-9._:-]*'),
    CHECK (typeof(allow_reduced_coverage) = 'integer' AND allow_reduced_coverage IN (0, 1)),
    CHECK (typeof(audit_ref) = 'text' AND length(audit_ref) BETWEEN 1 AND 128),
    CHECK (typeof(recorded_at_us) = 'integer' AND recorded_at_us > 0),

    FOREIGN KEY (audit_ref, workspace_id)
        REFERENCES omnivia_application_audit_events (audit_ref, workspace_id)
) WITHOUT ROWID;

-- One staged import, content-addressed.
--
-- `import_id` is derived by the writer from the repository, snapshot, producer, profile,
-- batch and policy, and the unique index below makes that tuple identify one import, so
-- the same verified batch under the same policy is one import however many times it is
-- requested. The counts are declarations the activation gate holds the staged rows to.
CREATE TABLE IF NOT EXISTS omnivia_code_analysis_imports (
    workspace_id         TEXT    NOT NULL,
    import_id            TEXT    NOT NULL,
    repository_id        TEXT    NOT NULL,
    snapshot_id          TEXT    NOT NULL,
    producer_id          TEXT    NOT NULL,
    producer_version     TEXT    NOT NULL,
    profile_digest       TEXT    NOT NULL,
    batch_digest         TEXT    NOT NULL,
    policy_id            TEXT    NOT NULL,
    policy_version       TEXT    NOT NULL,
    policy_digest        TEXT    NOT NULL,
    manifest_digest      TEXT    NOT NULL,
    manifest_evidence_id TEXT    NOT NULL,
    extraction_run_id    TEXT    NOT NULL,
    audit_ref            TEXT    NOT NULL,
    entity_count         INTEGER NOT NULL,
    relation_count       INTEGER NOT NULL,
    diagnostic_count     INTEGER NOT NULL,
    file_count           INTEGER NOT NULL,
    recorded_at_us       INTEGER NOT NULL,

    PRIMARY KEY (workspace_id, import_id),

    CHECK (typeof(import_id) = 'text' AND length(import_id) BETWEEN 1 AND 128
           AND import_id GLOB '[A-Za-z0-9]*'
           AND import_id NOT GLOB '*[^A-Za-z0-9._:-]*'),
    CHECK (typeof(repository_id) = 'text' AND length(repository_id) BETWEEN 1 AND 256),
    CHECK (typeof(snapshot_id) = 'text' AND length(snapshot_id) BETWEEN 1 AND 256),
    CHECK (typeof(producer_id) = 'text' AND length(producer_id) BETWEEN 1 AND 128
           AND producer_id GLOB '[A-Za-z0-9]*'
           AND producer_id NOT GLOB '*[^A-Za-z0-9._:-]*'),
    CHECK (typeof(producer_version) = 'text' AND length(producer_version) BETWEEN 1 AND 128
           AND producer_version GLOB '[A-Za-z0-9]*'
           AND producer_version NOT GLOB '*[^A-Za-z0-9._:-]*'),
    CHECK (typeof(profile_digest) = 'text' AND length(profile_digest) = 71
           AND substr(profile_digest, 1, 7) = 'sha256:'
           AND substr(profile_digest, 8) NOT GLOB '*[^0-9a-f]*'),
    CHECK (typeof(batch_digest) = 'text' AND length(batch_digest) = 71
           AND substr(batch_digest, 1, 7) = 'sha256:'
           AND substr(batch_digest, 8) NOT GLOB '*[^0-9a-f]*'),
    CHECK (typeof(policy_digest) = 'text' AND length(policy_digest) = 71
           AND substr(policy_digest, 1, 7) = 'sha256:'
           AND substr(policy_digest, 8) NOT GLOB '*[^0-9a-f]*'),
    CHECK (typeof(manifest_digest) = 'text' AND length(manifest_digest) = 71
           AND substr(manifest_digest, 1, 7) = 'sha256:'
           AND substr(manifest_digest, 8) NOT GLOB '*[^0-9a-f]*'),
    CHECK (typeof(extraction_run_id) = 'text' AND length(extraction_run_id) BETWEEN 1 AND 128
           AND extraction_run_id GLOB '[A-Za-z0-9]*'
           AND extraction_run_id NOT GLOB '*[^A-Za-z0-9._:-]*'),
    CHECK (typeof(entity_count) = 'integer' AND entity_count >= 1),
    CHECK (typeof(relation_count) = 'integer' AND relation_count >= 0),
    CHECK (typeof(diagnostic_count) = 'integer' AND diagnostic_count >= 0),
    CHECK (typeof(file_count) = 'integer' AND file_count >= 1),
    CHECK (entity_count + relation_count + diagnostic_count <= 20000),
    CHECK (typeof(recorded_at_us) = 'integer' AND recorded_at_us > 0),

    FOREIGN KEY (workspace_id, snapshot_id)
        REFERENCES omnivia_engineering_snapshot_captures (workspace_id, snapshot_id),
    FOREIGN KEY (workspace_id, policy_id, policy_version)
        REFERENCES omnivia_code_analysis_import_policies
            (workspace_id, policy_id, policy_version),
    FOREIGN KEY (manifest_evidence_id, workspace_id)
        REFERENCES omnivia_evidence_artifacts (evidence_id, workspace_id),
    FOREIGN KEY (audit_ref, workspace_id)
        REFERENCES omnivia_application_audit_events (audit_ref, workspace_id)
) WITHOUT ROWID;

-- The files one import covers: every distinct path a candidate is bound to, at the digest
-- the sealed capture index holds for it. The foreign key reaches the capture index row
-- itself, so a covered file is always a file this snapshot captured.
CREATE TABLE IF NOT EXISTS omnivia_code_analysis_import_files (
    workspace_id   TEXT NOT NULL,
    import_id      TEXT NOT NULL,
    snapshot_id    TEXT NOT NULL,
    path           TEXT NOT NULL,
    content_digest TEXT NOT NULL,

    PRIMARY KEY (workspace_id, import_id, path),

    CHECK (typeof(path) = 'text' AND length(path) BETWEEN 1 AND 4096
           AND instr(path, char(0)) = 0),
    CHECK (typeof(content_digest) = 'text' AND length(content_digest) = 71
           AND substr(content_digest, 1, 7) = 'sha256:'
           AND substr(content_digest, 8) NOT GLOB '*[^0-9a-f]*'),

    FOREIGN KEY (workspace_id, import_id)
        REFERENCES omnivia_code_analysis_imports (workspace_id, import_id),
    FOREIGN KEY (workspace_id, snapshot_id, path)
        REFERENCES omnivia_engineering_snapshot_files (workspace_id, snapshot_id, path)
) WITHOUT ROWID;

-- The exact accepted governed versions one import contributes. A version belongs to at
-- most one import, and the INSERT trigger proves it is a sealed, governed, accepted,
-- canonical `engineering.code` version of the kind the row claims, assembled under this
-- import's own audit -- so a manual fact or another producer's record can never be
-- enrolled into, and then retired with, an import it did not come from.
CREATE TABLE IF NOT EXISTS omnivia_code_analysis_import_members (
    workspace_id               TEXT    NOT NULL,
    import_id                  TEXT    NOT NULL,
    member_ordinal             INTEGER NOT NULL,
    member_kind                TEXT    NOT NULL,
    candidate_key              TEXT    NOT NULL,
    governed_record_id         TEXT    NOT NULL,
    governed_record_version_id TEXT    NOT NULL,

    PRIMARY KEY (workspace_id, import_id, member_ordinal),

    CHECK (typeof(member_ordinal) = 'integer' AND member_ordinal >= 1),
    CHECK (typeof(member_kind) = 'text' AND member_kind IN ('entity', 'relation')),
    CHECK (typeof(candidate_key) = 'text' AND length(candidate_key) BETWEEN 1 AND 256
           AND instr(candidate_key, char(0)) = 0),

    FOREIGN KEY (workspace_id, import_id)
        REFERENCES omnivia_code_analysis_imports (workspace_id, import_id),
    FOREIGN KEY (workspace_id, governed_record_version_id)
        REFERENCES omnivia_governed_version_assemblies
            (workspace_id, governed_record_version_id)
) WITHOUT ROWID;

-- Every candidate the import did not accept, with the reason, for review. These rows are
-- not governed records and nothing reads them as graph facts.
CREATE TABLE IF NOT EXISTS omnivia_code_analysis_import_diagnostics (
    workspace_id       TEXT    NOT NULL,
    import_id          TEXT    NOT NULL,
    diagnostic_ordinal INTEGER NOT NULL,
    candidate_kind     TEXT    NOT NULL,
    candidate_key      TEXT    NOT NULL,
    reason_code        TEXT    NOT NULL,
    candidate_json     TEXT    NOT NULL,

    PRIMARY KEY (workspace_id, import_id, diagnostic_ordinal),

    CHECK (typeof(diagnostic_ordinal) = 'integer' AND diagnostic_ordinal >= 1),
    CHECK (typeof(candidate_kind) = 'text' AND candidate_kind IN ('node', 'edge')),
    CHECK (typeof(candidate_key) = 'text' AND length(candidate_key) BETWEEN 1 AND 256
           AND instr(candidate_key, char(0)) = 0),
    CHECK (typeof(reason_code) = 'text' AND reason_code IN (
        'unresolved', 'ambiguous', 'external', 'inferred_dynamic',
        'relationship_unmapped', 'policy_not_accepted', 'node_kind_not_accepted',
        'endpoint_not_accepted', 'self_relation'
    )),
    CHECK (typeof(candidate_json) = 'text'
           AND length(CAST(candidate_json AS BLOB)) BETWEEN 2 AND 16384
           AND instr(candidate_json, char(0)) = 0),

    FOREIGN KEY (workspace_id, import_id)
        REFERENCES omnivia_code_analysis_imports (workspace_id, import_id)
) WITHOUT ROWID;

-- The applicability log. The scope is `(repository_id, producer_id, profile_digest)`;
-- the newest row per scope with `activated_at_us <= T` names the import that is the
-- current code graph for that scope at T. `idempotency_key` is unique per workspace and
-- `request_digest` is what a replay of that key has to match.
--
-- `coverage_decision` and `reduced_file_count` are recomputed and checked by the INSERT
-- trigger rather than trusted: `initial` has no predecessor, `complete` drops no file the
-- new snapshot still holds, and `reduced_authorised` drops at least one and is admitted
-- only under a policy that allows it. A file the new snapshot no longer holds is a removal,
-- not reduced coverage: its facts retire with the import that carried them.
CREATE TABLE IF NOT EXISTS omnivia_code_analysis_activations (
    workspace_id        TEXT    NOT NULL,
    repository_id       TEXT    NOT NULL,
    producer_id         TEXT    NOT NULL,
    profile_digest      TEXT    NOT NULL,
    activation_sequence INTEGER NOT NULL,
    import_id           TEXT    NOT NULL,
    previous_import_id  TEXT,
    idempotency_key     TEXT    NOT NULL,
    request_digest      TEXT    NOT NULL,
    coverage_decision   TEXT    NOT NULL,
    reduced_file_count  INTEGER NOT NULL,
    audit_ref           TEXT    NOT NULL,
    activated_at_us     INTEGER NOT NULL,

    PRIMARY KEY (workspace_id, repository_id, producer_id, profile_digest,
                 activation_sequence),

    CHECK (typeof(activation_sequence) = 'integer' AND activation_sequence >= 1),
    CHECK (typeof(idempotency_key) = 'text' AND length(idempotency_key) BETWEEN 1 AND 128
           AND idempotency_key GLOB '[A-Za-z0-9]*'
           AND idempotency_key NOT GLOB '*[^A-Za-z0-9._:-]*'),
    CHECK (typeof(request_digest) = 'text' AND length(request_digest) = 71
           AND substr(request_digest, 1, 7) = 'sha256:'
           AND substr(request_digest, 8) NOT GLOB '*[^0-9a-f]*'),
    CHECK (typeof(coverage_decision) = 'text'
           AND coverage_decision IN ('initial', 'complete', 'reduced_authorised')),
    CHECK (typeof(reduced_file_count) = 'integer' AND reduced_file_count >= 0),
    CHECK ((coverage_decision = 'initial') = (previous_import_id IS NULL)),
    CHECK ((coverage_decision = 'reduced_authorised') = (reduced_file_count > 0)),
    CHECK (typeof(activated_at_us) = 'integer' AND activated_at_us > 0),

    FOREIGN KEY (workspace_id, import_id)
        REFERENCES omnivia_code_analysis_imports (workspace_id, import_id),
    FOREIGN KEY (workspace_id, previous_import_id)
        REFERENCES omnivia_code_analysis_imports (workspace_id, import_id),
    FOREIGN KEY (audit_ref, workspace_id)
        REFERENCES omnivia_application_audit_events (audit_ref, workspace_id)
) WITHOUT ROWID;

CREATE UNIQUE INDEX IF NOT EXISTS omnivia_idx_code_analysis_import_policies_identity
    ON omnivia_code_analysis_import_policies (workspace_id, policy_id, policy_version);
CREATE UNIQUE INDEX IF NOT EXISTS omnivia_idx_code_analysis_imports_identity
    ON omnivia_code_analysis_imports (workspace_id, import_id);
CREATE UNIQUE INDEX IF NOT EXISTS omnivia_idx_code_analysis_imports_source
    ON omnivia_code_analysis_imports
        (workspace_id, repository_id, snapshot_id, producer_id, profile_digest,
         batch_digest, policy_digest);
CREATE UNIQUE INDEX IF NOT EXISTS omnivia_idx_code_analysis_import_files_identity
    ON omnivia_code_analysis_import_files (workspace_id, import_id, path);
CREATE UNIQUE INDEX IF NOT EXISTS omnivia_idx_code_analysis_import_members_identity
    ON omnivia_code_analysis_import_members (workspace_id, import_id, member_ordinal);
CREATE UNIQUE INDEX IF NOT EXISTS omnivia_idx_code_analysis_import_members_version
    ON omnivia_code_analysis_import_members (workspace_id, governed_record_version_id);
CREATE UNIQUE INDEX IF NOT EXISTS omnivia_idx_code_analysis_import_members_candidate
    ON omnivia_code_analysis_import_members
        (workspace_id, import_id, member_kind, candidate_key);
CREATE UNIQUE INDEX IF NOT EXISTS omnivia_idx_code_analysis_import_diagnostics_identity
    ON omnivia_code_analysis_import_diagnostics
        (workspace_id, import_id, diagnostic_ordinal);
CREATE UNIQUE INDEX IF NOT EXISTS omnivia_idx_code_analysis_activations_identity
    ON omnivia_code_analysis_activations
        (workspace_id, repository_id, producer_id, profile_digest, activation_sequence);
CREATE UNIQUE INDEX IF NOT EXISTS omnivia_idx_code_analysis_activations_idempotency
    ON omnivia_code_analysis_activations (workspace_id, idempotency_key);

CREATE TRIGGER IF NOT EXISTS omnivia_guard_code_analysis_import_policies_insert
BEFORE INSERT ON omnivia_code_analysis_import_policies
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_code_analysis_import_policies')
    WHERE omnivia_service_writer() IS NOT 1
       OR NEW.workspace_id IS NOT (
            SELECT workspace_id FROM omnivia_workspace_state WHERE singleton = 1)
       OR NOT EXISTS (
            SELECT 1 FROM omnivia_mutation_guard g
            JOIN omnivia_workspace_state s ON s.singleton = 1
            JOIN omnivia_workspace_lease l ON l.singleton = 1
            WHERE g.singleton = 1 AND g.fencing_generation = s.fencing_generation
              AND g.workspace_id = s.workspace_id
              AND l.fencing_generation = g.fencing_generation
              AND l.workspace_id = g.workspace_id
              AND l.service_instance_id = g.service_instance_id
              AND l.lifecycle IN ('acquiring', 'held', 'draining'));
    SELECT RAISE(ABORT, 'omnivia: code analysis import policy version already exists')
    WHERE EXISTS (
        SELECT 1 FROM omnivia_code_analysis_import_policies
        WHERE workspace_id = NEW.workspace_id AND policy_id = NEW.policy_id
          AND policy_version = NEW.policy_version);
    SELECT RAISE(ABORT, 'omnivia: code analysis import policy audit is not its authoriser''s')
    WHERE NEW.authorised_by = 'core-service'
       OR NOT EXISTS (
        SELECT 1 FROM omnivia_application_audit_events e
        WHERE e.audit_ref = NEW.audit_ref AND e.workspace_id = NEW.workspace_id
          AND e.principal_id = NEW.authorised_by
          AND e.recorded_at_us = NEW.recorded_at_us
          AND e.outcome_class = 'succeeded');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_code_analysis_import_policies_update
BEFORE UPDATE ON omnivia_code_analysis_import_policies
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_code_analysis_import_policies is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_code_analysis_import_policies_delete
BEFORE DELETE ON omnivia_code_analysis_import_policies
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_code_analysis_import_policies is append-only; DELETE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_code_analysis_imports_insert
BEFORE INSERT ON omnivia_code_analysis_imports
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_code_analysis_imports')
    WHERE omnivia_service_writer() IS NOT 1
       OR NEW.workspace_id IS NOT (
            SELECT workspace_id FROM omnivia_workspace_state WHERE singleton = 1)
       OR NOT EXISTS (
            SELECT 1 FROM omnivia_mutation_guard g
            JOIN omnivia_workspace_state s ON s.singleton = 1
            JOIN omnivia_workspace_lease l ON l.singleton = 1
            WHERE g.singleton = 1 AND g.fencing_generation = s.fencing_generation
              AND g.workspace_id = s.workspace_id
              AND l.fencing_generation = g.fencing_generation
              AND l.workspace_id = g.workspace_id
              AND l.service_instance_id = g.service_instance_id
              AND l.lifecycle IN ('acquiring', 'held', 'draining'));
    SELECT RAISE(ABORT, 'omnivia: code analysis import identity already exists')
    WHERE EXISTS (
        SELECT 1 FROM omnivia_code_analysis_imports
        WHERE workspace_id = NEW.workspace_id AND import_id = NEW.import_id);
    SELECT RAISE(ABORT, 'omnivia: code analysis import cites a policy digest that was not authorised')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_code_analysis_import_policies p
        WHERE p.workspace_id = NEW.workspace_id AND p.policy_id = NEW.policy_id
          AND p.policy_version = NEW.policy_version
          AND p.policy_digest = NEW.policy_digest);
    SELECT RAISE(ABORT, 'omnivia: code analysis import requires one complete sealed capture of its repository')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_engineering_snapshot_captures c
        JOIN omnivia_engineering_snapshots s
          ON s.workspace_id = c.workspace_id AND s.snapshot_id = c.snapshot_id
        WHERE c.workspace_id = NEW.workspace_id AND c.snapshot_id = NEW.snapshot_id
          AND c.repository_id = NEW.repository_id
          AND s.repository_id = NEW.repository_id
          AND c.capture_status = 'complete'
          AND c.manifest_evidence_id = NEW.manifest_evidence_id
          AND c.rich_manifest_digest = NEW.manifest_digest);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_code_analysis_imports_update
BEFORE UPDATE ON omnivia_code_analysis_imports
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_code_analysis_imports is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_code_analysis_imports_delete
BEFORE DELETE ON omnivia_code_analysis_imports
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_code_analysis_imports is append-only; DELETE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_code_analysis_import_files_insert
BEFORE INSERT ON omnivia_code_analysis_import_files
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_code_analysis_import_files')
    WHERE omnivia_service_writer() IS NOT 1
       OR NEW.workspace_id IS NOT (
            SELECT workspace_id FROM omnivia_workspace_state WHERE singleton = 1)
       OR NOT EXISTS (
            SELECT 1 FROM omnivia_mutation_guard g
            JOIN omnivia_workspace_state s ON s.singleton = 1
            JOIN omnivia_workspace_lease l ON l.singleton = 1
            WHERE g.singleton = 1 AND g.fencing_generation = s.fencing_generation
              AND g.workspace_id = s.workspace_id
              AND l.fencing_generation = g.fencing_generation
              AND l.workspace_id = g.workspace_id
              AND l.service_instance_id = g.service_instance_id
              AND l.lifecycle IN ('acquiring', 'held', 'draining'));
    SELECT RAISE(ABORT, 'omnivia: code analysis import is already activated; its staged set is closed')
    WHERE EXISTS (
        SELECT 1 FROM omnivia_code_analysis_activations a
        WHERE a.workspace_id = NEW.workspace_id AND a.import_id = NEW.import_id);
    SELECT RAISE(ABORT, 'omnivia: covered file is not the import snapshot''s captured file')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_code_analysis_imports i
        JOIN omnivia_engineering_snapshot_files f
          ON f.workspace_id = i.workspace_id AND f.snapshot_id = i.snapshot_id
        WHERE i.workspace_id = NEW.workspace_id AND i.import_id = NEW.import_id
          AND i.snapshot_id = NEW.snapshot_id
          AND f.path = NEW.path AND f.content_digest = NEW.content_digest);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_code_analysis_import_files_update
BEFORE UPDATE ON omnivia_code_analysis_import_files
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_code_analysis_import_files is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_code_analysis_import_files_delete
BEFORE DELETE ON omnivia_code_analysis_import_files
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_code_analysis_import_files is append-only; DELETE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_code_analysis_import_members_insert
BEFORE INSERT ON omnivia_code_analysis_import_members
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_code_analysis_import_members')
    WHERE omnivia_service_writer() IS NOT 1
       OR NEW.workspace_id IS NOT (
            SELECT workspace_id FROM omnivia_workspace_state WHERE singleton = 1)
       OR NOT EXISTS (
            SELECT 1 FROM omnivia_mutation_guard g
            JOIN omnivia_workspace_state s ON s.singleton = 1
            JOIN omnivia_workspace_lease l ON l.singleton = 1
            WHERE g.singleton = 1 AND g.fencing_generation = s.fencing_generation
              AND g.workspace_id = s.workspace_id
              AND l.fencing_generation = g.fencing_generation
              AND l.workspace_id = g.workspace_id
              AND l.service_instance_id = g.service_instance_id
              AND l.lifecycle IN ('acquiring', 'held', 'draining'));
    SELECT RAISE(ABORT, 'omnivia: code analysis import is already activated; its staged set is closed')
    WHERE EXISTS (
        SELECT 1 FROM omnivia_code_analysis_activations a
        WHERE a.workspace_id = NEW.workspace_id AND a.import_id = NEW.import_id);
    SELECT RAISE(ABORT, 'omnivia: import member is not a sealed accepted code version of this import')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_authoritative_governed_versions v
        JOIN omnivia_code_analysis_imports i
          ON i.workspace_id = v.workspace_id AND i.import_id = NEW.import_id
        WHERE v.workspace_id = NEW.workspace_id
          AND v.governed_record_version_id = NEW.governed_record_version_id
          AND v.governed_record_id = NEW.governed_record_id
          AND v.layer = 'governed'
          AND v.governance_disposition = 'accepted'
          AND v.authority_level = 'canonical'
          AND v.decision_source_kind = 'policy_evaluator'
          AND v.decision_source_id = i.policy_id
          AND v.domain_scope = 'engineering.code'
          AND v.record_type = CASE NEW.member_kind
              WHEN 'entity' THEN 'knowledge.entity'
              ELSE 'knowledge.relation' END
          AND v.correlation_kind = 'm1_audit'
          AND v.audit_ref = i.audit_ref);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_code_analysis_import_members_update
BEFORE UPDATE ON omnivia_code_analysis_import_members
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_code_analysis_import_members is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_code_analysis_import_members_delete
BEFORE DELETE ON omnivia_code_analysis_import_members
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_code_analysis_import_members is append-only; DELETE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_code_analysis_import_diagnostics_insert
BEFORE INSERT ON omnivia_code_analysis_import_diagnostics
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_code_analysis_import_diagnostics')
    WHERE omnivia_service_writer() IS NOT 1
       OR NEW.workspace_id IS NOT (
            SELECT workspace_id FROM omnivia_workspace_state WHERE singleton = 1)
       OR NOT EXISTS (
            SELECT 1 FROM omnivia_mutation_guard g
            JOIN omnivia_workspace_state s ON s.singleton = 1
            JOIN omnivia_workspace_lease l ON l.singleton = 1
            WHERE g.singleton = 1 AND g.fencing_generation = s.fencing_generation
              AND g.workspace_id = s.workspace_id
              AND l.fencing_generation = g.fencing_generation
              AND l.workspace_id = g.workspace_id
              AND l.service_instance_id = g.service_instance_id
              AND l.lifecycle IN ('acquiring', 'held', 'draining'));
    SELECT RAISE(ABORT, 'omnivia: code analysis import is already activated; its staged set is closed')
    WHERE EXISTS (
        SELECT 1 FROM omnivia_code_analysis_activations a
        WHERE a.workspace_id = NEW.workspace_id AND a.import_id = NEW.import_id);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_code_analysis_import_diagnostics_update
BEFORE UPDATE ON omnivia_code_analysis_import_diagnostics
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_code_analysis_import_diagnostics is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_code_analysis_import_diagnostics_delete
BEFORE DELETE ON omnivia_code_analysis_import_diagnostics
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_code_analysis_import_diagnostics is append-only; DELETE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_code_analysis_activations_insert
BEFORE INSERT ON omnivia_code_analysis_activations
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_code_analysis_activations')
    WHERE omnivia_service_writer() IS NOT 1
       OR NEW.workspace_id IS NOT (
            SELECT workspace_id FROM omnivia_workspace_state WHERE singleton = 1)
       OR NOT EXISTS (
            SELECT 1 FROM omnivia_mutation_guard g
            JOIN omnivia_workspace_state s ON s.singleton = 1
            JOIN omnivia_workspace_lease l ON l.singleton = 1
            WHERE g.singleton = 1 AND g.fencing_generation = s.fencing_generation
              AND g.workspace_id = s.workspace_id
              AND l.fencing_generation = g.fencing_generation
              AND l.workspace_id = g.workspace_id
              AND l.service_instance_id = g.service_instance_id
              AND l.lifecycle IN ('acquiring', 'held', 'draining'));
    SELECT RAISE(ABORT, 'omnivia: code analysis activation idempotency key already used')
    WHERE EXISTS (
        SELECT 1 FROM omnivia_code_analysis_activations
        WHERE workspace_id = NEW.workspace_id AND idempotency_key = NEW.idempotency_key);
    SELECT RAISE(ABORT, 'omnivia: code analysis activation names an import of another scope')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_code_analysis_imports i
        WHERE i.workspace_id = NEW.workspace_id AND i.import_id = NEW.import_id
          AND i.repository_id = NEW.repository_id AND i.producer_id = NEW.producer_id
          AND i.profile_digest = NEW.profile_digest
          AND i.recorded_at_us <= NEW.activated_at_us);
    SELECT RAISE(ABORT, 'omnivia: stale code analysis activation fence')
    WHERE NEW.activation_sequence IS NOT (
            SELECT COALESCE(MAX(a.activation_sequence), 0) + 1
            FROM omnivia_code_analysis_activations a
            WHERE a.workspace_id = NEW.workspace_id AND a.repository_id = NEW.repository_id
              AND a.producer_id = NEW.producer_id AND a.profile_digest = NEW.profile_digest)
       OR NEW.previous_import_id IS NOT (
            SELECT a.import_id FROM omnivia_code_analysis_activations a
            WHERE a.workspace_id = NEW.workspace_id AND a.repository_id = NEW.repository_id
              AND a.producer_id = NEW.producer_id AND a.profile_digest = NEW.profile_digest
            ORDER BY a.activation_sequence DESC LIMIT 1)
       OR EXISTS (
            SELECT 1 FROM omnivia_code_analysis_activations a
            WHERE a.workspace_id = NEW.workspace_id AND a.repository_id = NEW.repository_id
              AND a.producer_id = NEW.producer_id AND a.profile_digest = NEW.profile_digest
              AND a.activated_at_us > NEW.activated_at_us);
    SELECT RAISE(ABORT, 'omnivia: code analysis import is incomplete')
    WHERE EXISTS (
        SELECT 1 FROM omnivia_code_analysis_imports i
        WHERE i.workspace_id = NEW.workspace_id AND i.import_id = NEW.import_id
          AND (i.entity_count <> (
                   SELECT COUNT(*) FROM omnivia_code_analysis_import_members m
                   WHERE m.workspace_id = i.workspace_id AND m.import_id = i.import_id
                     AND m.member_kind = 'entity')
               OR i.relation_count <> (
                   SELECT COUNT(*) FROM omnivia_code_analysis_import_members m
                   WHERE m.workspace_id = i.workspace_id AND m.import_id = i.import_id
                     AND m.member_kind = 'relation')
               OR i.diagnostic_count <> (
                   SELECT COUNT(*) FROM omnivia_code_analysis_import_diagnostics d
                   WHERE d.workspace_id = i.workspace_id AND d.import_id = i.import_id)
               OR i.file_count <> (
                   SELECT COUNT(*) FROM omnivia_code_analysis_import_files f
                   WHERE f.workspace_id = i.workspace_id AND f.import_id = i.import_id)));
    SELECT RAISE(ABORT, 'omnivia: code analysis activation misstates its coverage')
    WHERE NEW.reduced_file_count IS NOT (
        SELECT COUNT(*)
        FROM omnivia_code_analysis_import_files p
        JOIN omnivia_code_analysis_imports i
          ON i.workspace_id = NEW.workspace_id AND i.import_id = NEW.import_id
        JOIN omnivia_engineering_snapshot_files s
          ON s.workspace_id = i.workspace_id AND s.snapshot_id = i.snapshot_id
         AND s.path = p.path
        WHERE p.workspace_id = NEW.workspace_id AND p.import_id = NEW.previous_import_id
          AND NOT EXISTS (
              SELECT 1 FROM omnivia_code_analysis_import_files n
              WHERE n.workspace_id = NEW.workspace_id AND n.import_id = NEW.import_id
                AND n.path = p.path));
    SELECT RAISE(ABORT, 'omnivia: reduced code analysis coverage is not authorised by the import policy')
    WHERE NEW.coverage_decision = 'reduced_authorised'
      AND NOT EXISTS (
          SELECT 1 FROM omnivia_code_analysis_imports i
          JOIN omnivia_code_analysis_import_policies p
            ON p.workspace_id = i.workspace_id AND p.policy_id = i.policy_id
           AND p.policy_version = i.policy_version
          WHERE i.workspace_id = NEW.workspace_id AND i.import_id = NEW.import_id
            AND p.allow_reduced_coverage = 1);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_code_analysis_activations_update
BEFORE UPDATE ON omnivia_code_analysis_activations
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_code_analysis_activations is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_code_analysis_activations_delete
BEFORE DELETE ON omnivia_code_analysis_activations
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_code_analysis_activations is append-only; DELETE is never permitted');
END;
