-- SPEC-CORE-SEM-001 v0.2, Phase 2 retention/legal-hold/deletion receipts (0039).
-- Records are append-only and content-free: protected bytes never enter this schema.

CREATE TABLE IF NOT EXISTS omnivia_semantic_retention_policies (
    workspace_id TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    default_retention_days INTEGER NOT NULL,
    created_at_us INTEGER NOT NULL,
    created_at_precision TEXT NOT NULL,
    created_at_provenance TEXT NOT NULL,
    policy_digest TEXT NOT NULL,
    PRIMARY KEY (workspace_id, policy_version),
    CHECK (length(policy_version) > 0),
    CHECK (typeof(default_retention_days) = 'integer' AND default_retention_days > 0),
    CHECK (typeof(created_at_us) = 'integer' AND created_at_us > 0),
    CHECK (created_at_precision IN ('year','month','day','hour','minute','second')),
    CHECK (created_at_provenance IN ('stated','evidence_attested','ingestion_fallback')),
    CHECK (length(policy_digest) = 71 AND substr(policy_digest,1,7) = 'sha256:'
           AND substr(policy_digest,8) NOT GLOB '*[^0-9a-f]*')
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_semantic_evidence_legal_holds (
    workspace_id TEXT NOT NULL,
    hold_id TEXT NOT NULL,
    evidence_id TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    placed_at_us INTEGER NOT NULL,
    placed_at_precision TEXT NOT NULL,
    placed_at_provenance TEXT NOT NULL,
    hold_digest TEXT NOT NULL,
    PRIMARY KEY (workspace_id, hold_id),
    CHECK (length(reason_code) > 0),
    CHECK (typeof(placed_at_us) = 'integer' AND placed_at_us > 0),
    CHECK (placed_at_precision IN ('year','month','day','hour','minute','second')),
    CHECK (placed_at_provenance IN ('stated','evidence_attested','ingestion_fallback')),
    CHECK (length(hold_digest) = 71 AND substr(hold_digest,1,7) = 'sha256:'
           AND substr(hold_digest,8) NOT GLOB '*[^0-9a-f]*'),
    FOREIGN KEY (workspace_id, evidence_id)
        REFERENCES omnivia_semantic_evidence_items (workspace_id, evidence_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_semantic_evidence_legal_hold_releases (
    workspace_id TEXT NOT NULL,
    release_id TEXT NOT NULL,
    hold_id TEXT NOT NULL,
    actor_principal_id TEXT NOT NULL,
    released_at_us INTEGER NOT NULL,
    released_at_precision TEXT NOT NULL,
    released_at_provenance TEXT NOT NULL,
    release_digest TEXT NOT NULL,
    PRIMARY KEY (workspace_id, release_id),
    UNIQUE (workspace_id, hold_id),
    CHECK (length(actor_principal_id) > 0),
    CHECK (typeof(released_at_us) = 'integer' AND released_at_us > 0),
    CHECK (released_at_precision IN ('year','month','day','hour','minute','second')),
    CHECK (released_at_provenance IN ('stated','evidence_attested','ingestion_fallback')),
    CHECK (length(release_digest) = 71 AND substr(release_digest,1,7) = 'sha256:'
           AND substr(release_digest,8) NOT GLOB '*[^0-9a-f]*'),
    FOREIGN KEY (workspace_id, hold_id)
        REFERENCES omnivia_semantic_evidence_legal_holds (workspace_id, hold_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_semantic_evidence_deletion_plans (
    workspace_id TEXT NOT NULL,
    plan_id TEXT NOT NULL,
    evidence_id TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    requested_at_us INTEGER NOT NULL,
    requested_at_precision TEXT NOT NULL,
    requested_at_provenance TEXT NOT NULL,
    due_at_us INTEGER NOT NULL,
    due_at_precision TEXT NOT NULL,
    due_at_provenance TEXT NOT NULL,
    plan_state TEXT NOT NULL,
    plan_digest TEXT NOT NULL,
    PRIMARY KEY (workspace_id, plan_id),
    CHECK (length(reason_code) > 0),
    CHECK (typeof(requested_at_us) = 'integer' AND requested_at_us > 0),
    CHECK (typeof(due_at_us) = 'integer' AND due_at_us > 0),
    CHECK (requested_at_precision IN ('year','month','day','hour','minute','second')),
    CHECK (due_at_precision IN ('year','month','day','hour','minute','second')),
    CHECK (requested_at_provenance IN ('stated','evidence_attested','ingestion_fallback')),
    CHECK (due_at_provenance IN ('stated','evidence_attested','ingestion_fallback')),
    CHECK (plan_state IN ('ready','blocked_not_due','blocked_legal_hold')),
    CHECK (length(plan_digest) = 71 AND substr(plan_digest,1,7) = 'sha256:'
           AND substr(plan_digest,8) NOT GLOB '*[^0-9a-f]*'),
    FOREIGN KEY (workspace_id, evidence_id)
        REFERENCES omnivia_semantic_evidence_items (workspace_id, evidence_id),
    FOREIGN KEY (workspace_id, policy_version)
        REFERENCES omnivia_semantic_retention_policies (workspace_id, policy_version)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_semantic_evidence_deletion_targets (
    workspace_id TEXT NOT NULL,
    plan_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    storage_class TEXT NOT NULL,
    target_ref TEXT NOT NULL,
    target_digest TEXT NOT NULL,
    PRIMARY KEY (workspace_id, plan_id, ordinal),
    UNIQUE (workspace_id, plan_id, storage_class),
    CHECK (typeof(ordinal) = 'integer' AND ordinal >= 0),
    CHECK (storage_class IN (
        'canonical_metadata','protected_content','source_spans','raw_completions',
        'worker_scratch','search_projection','graph_projection','vector_projection',
        'caches','logs','backups')),
    CHECK (length(target_ref) > 0),
    CHECK (length(target_digest) = 71 AND substr(target_digest,1,7) = 'sha256:'
           AND substr(target_digest,8) NOT GLOB '*[^0-9a-f]*'),
    FOREIGN KEY (workspace_id, plan_id)
        REFERENCES omnivia_semantic_evidence_deletion_plans (workspace_id, plan_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_semantic_evidence_deletion_receipts (
    workspace_id TEXT NOT NULL,
    receipt_id TEXT NOT NULL,
    plan_id TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    completed_at_us INTEGER NOT NULL,
    completed_at_precision TEXT NOT NULL,
    completed_at_provenance TEXT NOT NULL,
    deleted_target_count INTEGER NOT NULL,
    receipt_digest TEXT NOT NULL,
    PRIMARY KEY (workspace_id, receipt_id),
    UNIQUE (workspace_id, plan_id),
    CHECK (typeof(completed_at_us) = 'integer' AND completed_at_us > 0),
    CHECK (length(reason_code) > 0),
    CHECK (completed_at_precision IN ('year','month','day','hour','minute','second')),
    CHECK (completed_at_provenance IN ('stated','evidence_attested','ingestion_fallback')),
    CHECK (typeof(deleted_target_count) = 'integer' AND deleted_target_count > 0),
    CHECK (length(receipt_digest) = 71 AND substr(receipt_digest,1,7) = 'sha256:'
           AND substr(receipt_digest,8) NOT GLOB '*[^0-9a-f]*'),
    FOREIGN KEY (workspace_id, plan_id)
        REFERENCES omnivia_semantic_evidence_deletion_plans (workspace_id, plan_id)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS omnivia_idx_semantic_retention_evidence_holds
    ON omnivia_semantic_evidence_legal_holds (workspace_id, evidence_id, placed_at_us);
CREATE INDEX IF NOT EXISTS omnivia_idx_semantic_retention_plan_due
    ON omnivia_semantic_evidence_deletion_plans (workspace_id, plan_state, due_at_us);

-- A receipt is valid only for a ready plan and must enumerate every planned target.
CREATE TRIGGER IF NOT EXISTS omnivia_validate_semantic_deletion_receipt
BEFORE INSERT ON omnivia_semantic_evidence_deletion_receipts BEGIN
 SELECT RAISE(ABORT,'omnivia: deletion receipt requires a ready plan')
 WHERE NOT EXISTS (
  SELECT 1 FROM omnivia_semantic_evidence_deletion_plans p
  WHERE p.workspace_id=NEW.workspace_id AND p.plan_id=NEW.plan_id AND p.plan_state='ready');
 SELECT RAISE(ABORT,'omnivia: deletion receipt target count mismatch')
 WHERE NEW.deleted_target_count IS NOT (
  SELECT COUNT(*) FROM omnivia_semantic_evidence_deletion_targets t
  WHERE t.workspace_id=NEW.workspace_id AND t.plan_id=NEW.plan_id);
END;

-- All canonical retention relations use the same lease/fence guard and are append-only.
CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_retention_policies_insert
BEFORE INSERT ON omnivia_semantic_retention_policies BEGIN
 SELECT RAISE(ABORT,'omnivia: unguarded INSERT on omnivia_semantic_retention_policies')
 WHERE omnivia_service_writer() IS NOT 1 OR NOT EXISTS (
  SELECT 1 FROM omnivia_mutation_guard g JOIN omnivia_workspace_state s ON s.singleton=1
  JOIN omnivia_workspace_lease l ON l.singleton=1 WHERE g.singleton=1
  AND g.fencing_generation=s.fencing_generation AND g.workspace_id=s.workspace_id
  AND l.fencing_generation=g.fencing_generation AND l.workspace_id=g.workspace_id
  AND l.service_instance_id=g.service_instance_id AND l.lifecycle IN ('acquiring','held','draining'))
  OR NEW.workspace_id IS NOT (SELECT workspace_id FROM omnivia_workspace_state WHERE singleton=1);
END;
CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_retention_policies_update BEFORE UPDATE ON omnivia_semantic_retention_policies BEGIN SELECT RAISE(ABORT,'omnivia: omnivia_semantic_retention_policies is append-only; UPDATE is never permitted'); END;
CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_retention_policies_delete BEFORE DELETE ON omnivia_semantic_retention_policies BEGIN SELECT RAISE(ABORT,'omnivia: omnivia_semantic_retention_policies is append-only; DELETE is never permitted'); END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_evidence_legal_holds_insert
BEFORE INSERT ON omnivia_semantic_evidence_legal_holds BEGIN
 SELECT RAISE(ABORT,'omnivia: unguarded INSERT on omnivia_semantic_evidence_legal_holds')
 WHERE omnivia_service_writer() IS NOT 1 OR NOT EXISTS (
  SELECT 1 FROM omnivia_mutation_guard g JOIN omnivia_workspace_state s ON s.singleton=1
  JOIN omnivia_workspace_lease l ON l.singleton=1 WHERE g.singleton=1
  AND g.fencing_generation=s.fencing_generation AND g.workspace_id=s.workspace_id
  AND l.fencing_generation=g.fencing_generation AND l.workspace_id=g.workspace_id
  AND l.service_instance_id=g.service_instance_id AND l.lifecycle IN ('acquiring','held','draining'))
  OR NEW.workspace_id IS NOT (SELECT workspace_id FROM omnivia_workspace_state WHERE singleton=1);
END;
CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_evidence_legal_holds_update BEFORE UPDATE ON omnivia_semantic_evidence_legal_holds BEGIN SELECT RAISE(ABORT,'omnivia: omnivia_semantic_evidence_legal_holds is append-only; UPDATE is never permitted'); END;
CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_evidence_legal_holds_delete BEFORE DELETE ON omnivia_semantic_evidence_legal_holds BEGIN SELECT RAISE(ABORT,'omnivia: omnivia_semantic_evidence_legal_holds is append-only; DELETE is never permitted'); END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_evidence_legal_hold_releases_insert
BEFORE INSERT ON omnivia_semantic_evidence_legal_hold_releases BEGIN
 SELECT RAISE(ABORT,'omnivia: unguarded INSERT on omnivia_semantic_evidence_legal_hold_releases')
 WHERE omnivia_service_writer() IS NOT 1 OR NOT EXISTS (
  SELECT 1 FROM omnivia_mutation_guard g JOIN omnivia_workspace_state s ON s.singleton=1
  JOIN omnivia_workspace_lease l ON l.singleton=1 WHERE g.singleton=1
  AND g.fencing_generation=s.fencing_generation AND g.workspace_id=s.workspace_id
  AND l.fencing_generation=g.fencing_generation AND l.workspace_id=g.workspace_id
  AND l.service_instance_id=g.service_instance_id AND l.lifecycle IN ('acquiring','held','draining'))
  OR NEW.workspace_id IS NOT (SELECT workspace_id FROM omnivia_workspace_state WHERE singleton=1);
END;
CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_evidence_legal_hold_releases_update BEFORE UPDATE ON omnivia_semantic_evidence_legal_hold_releases BEGIN SELECT RAISE(ABORT,'omnivia: omnivia_semantic_evidence_legal_hold_releases is append-only; UPDATE is never permitted'); END;
CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_evidence_legal_hold_releases_delete BEFORE DELETE ON omnivia_semantic_evidence_legal_hold_releases BEGIN SELECT RAISE(ABORT,'omnivia: omnivia_semantic_evidence_legal_hold_releases is append-only; DELETE is never permitted'); END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_evidence_deletion_plans_insert
BEFORE INSERT ON omnivia_semantic_evidence_deletion_plans BEGIN
 SELECT RAISE(ABORT,'omnivia: unguarded INSERT on omnivia_semantic_evidence_deletion_plans')
 WHERE omnivia_service_writer() IS NOT 1 OR NOT EXISTS (
  SELECT 1 FROM omnivia_mutation_guard g JOIN omnivia_workspace_state s ON s.singleton=1
  JOIN omnivia_workspace_lease l ON l.singleton=1 WHERE g.singleton=1
  AND g.fencing_generation=s.fencing_generation AND g.workspace_id=s.workspace_id
  AND l.fencing_generation=g.fencing_generation AND l.workspace_id=g.workspace_id
  AND l.service_instance_id=g.service_instance_id AND l.lifecycle IN ('acquiring','held','draining'))
  OR NEW.workspace_id IS NOT (SELECT workspace_id FROM omnivia_workspace_state WHERE singleton=1);
END;
CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_evidence_deletion_plans_update BEFORE UPDATE ON omnivia_semantic_evidence_deletion_plans BEGIN SELECT RAISE(ABORT,'omnivia: omnivia_semantic_evidence_deletion_plans is append-only; UPDATE is never permitted'); END;
CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_evidence_deletion_plans_delete BEFORE DELETE ON omnivia_semantic_evidence_deletion_plans BEGIN SELECT RAISE(ABORT,'omnivia: omnivia_semantic_evidence_deletion_plans is append-only; DELETE is never permitted'); END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_evidence_deletion_targets_insert
BEFORE INSERT ON omnivia_semantic_evidence_deletion_targets BEGIN
 SELECT RAISE(ABORT,'omnivia: unguarded INSERT on omnivia_semantic_evidence_deletion_targets')
 WHERE omnivia_service_writer() IS NOT 1 OR NOT EXISTS (
  SELECT 1 FROM omnivia_mutation_guard g JOIN omnivia_workspace_state s ON s.singleton=1
  JOIN omnivia_workspace_lease l ON l.singleton=1 WHERE g.singleton=1
  AND g.fencing_generation=s.fencing_generation AND g.workspace_id=s.workspace_id
  AND l.fencing_generation=g.fencing_generation AND l.workspace_id=g.workspace_id
  AND l.service_instance_id=g.service_instance_id AND l.lifecycle IN ('acquiring','held','draining'))
  OR NEW.workspace_id IS NOT (SELECT workspace_id FROM omnivia_workspace_state WHERE singleton=1);
END;
CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_evidence_deletion_targets_update BEFORE UPDATE ON omnivia_semantic_evidence_deletion_targets BEGIN SELECT RAISE(ABORT,'omnivia: omnivia_semantic_evidence_deletion_targets is append-only; UPDATE is never permitted'); END;
CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_evidence_deletion_targets_delete BEFORE DELETE ON omnivia_semantic_evidence_deletion_targets BEGIN SELECT RAISE(ABORT,'omnivia: omnivia_semantic_evidence_deletion_targets is append-only; DELETE is never permitted'); END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_evidence_deletion_receipts_insert
BEFORE INSERT ON omnivia_semantic_evidence_deletion_receipts BEGIN
 SELECT RAISE(ABORT,'omnivia: unguarded INSERT on omnivia_semantic_evidence_deletion_receipts')
 WHERE omnivia_service_writer() IS NOT 1 OR NOT EXISTS (
  SELECT 1 FROM omnivia_mutation_guard g JOIN omnivia_workspace_state s ON s.singleton=1
  JOIN omnivia_workspace_lease l ON l.singleton=1 WHERE g.singleton=1
  AND g.fencing_generation=s.fencing_generation AND g.workspace_id=s.workspace_id
  AND l.fencing_generation=g.fencing_generation AND l.workspace_id=g.workspace_id
  AND l.service_instance_id=g.service_instance_id AND l.lifecycle IN ('acquiring','held','draining'))
  OR NEW.workspace_id IS NOT (SELECT workspace_id FROM omnivia_workspace_state WHERE singleton=1);
END;
CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_evidence_deletion_receipts_update BEFORE UPDATE ON omnivia_semantic_evidence_deletion_receipts BEGIN SELECT RAISE(ABORT,'omnivia: omnivia_semantic_evidence_deletion_receipts is append-only; UPDATE is never permitted'); END;
CREATE TRIGGER IF NOT EXISTS omnivia_guard_semantic_evidence_deletion_receipts_delete BEFORE DELETE ON omnivia_semantic_evidence_deletion_receipts BEGIN SELECT RAISE(ABORT,'omnivia: omnivia_semantic_evidence_deletion_receipts is append-only; DELETE is never permitted'); END;
