-- Explicit cross-Project knowledge sharing (DEV-REQ-081, Core governance; migration 0067).
--
-- Additive only; allocation 0067 (predecessor 0066). Two append-only tables and their guard triggers.
-- A row in `omnivia_knowledge_shares` is one owner-proposed sharing of one sealed, canonical governed
-- version from a source Project to a recipient Project in the same workspace. The row names the
-- governed record, the assembly, the sealed version, the domain scope the record was created in and
-- the content digest, and the insert guard below refuses a row whose five facts do not all agree with
-- one authoritative governed version, so a share cannot be written against a scope or digest the
-- record does not hold. Which Project owns that scope is a server binding, never a column here. A row in
-- `omnivia_knowledge_share_decisions` is one accepted or revoked decision on that proposal. A recipient
-- is eligible only while an accepted decision exists and no revoked decision does, so eligibility is
-- derived from these rows on every read and is never cached as a grant.
--
-- Identity. `share_digest` is `sha256:` over the canonical share body, which excludes the time it was
-- proposed. `(workspace_id, share_id)` is the key and `(workspace_id, share_digest)` is unique, so an
-- exact replay dedups in the storage layer and a different body for an existing id is refused there.
-- Reads re-derive the digest from the columns, so a row edited outside this database reads as corrupt.
--
-- Lifecycle. A decision is `accepted` or `revoked`. Each kind is recorded at most once per share. A
-- revocation requires an earlier acceptance, and an acceptance cannot follow a revocation. Both are
-- also refused here, so a decision cannot be committed out of order even by a caller that skips the
-- service seam.
--
-- Every write runs inside the caller's `fenced_transaction`. The INSERT guards carry the same
-- connection-authority, guard, workspace-state and lease predicate as the other guarded tables, bind
-- each row to the open workspace and to its current fencing generation. UPDATE and DELETE are refused
-- for everyone, so a revocation is a new row and never an edit.
--
-- No DML, and no comment sits inside a statement below, for the migrator's statement splitter.

CREATE TABLE IF NOT EXISTS omnivia_knowledge_shares (
    workspace_id                TEXT    NOT NULL,
    share_id                    TEXT    NOT NULL,
    share_digest                TEXT    NOT NULL,
    source_project_id           TEXT    NOT NULL,
    recipient_project_id        TEXT    NOT NULL,
    governed_record_id          TEXT    NOT NULL,
    governed_assembly_id        TEXT    NOT NULL,
    governed_record_version_id  TEXT    NOT NULL,
    domain_scope                TEXT    NOT NULL,
    content_digest              TEXT    NOT NULL,
    proposed_by                 TEXT    NOT NULL,
    proposed_under_generation   INTEGER NOT NULL,
    proposed_at_us              INTEGER NOT NULL,

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(share_id) = 'text' AND length(share_id) BETWEEN 1 AND 128
           AND share_id GLOB '[A-Za-z0-9]*'
           AND share_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(share_id, char(0)) = 0),
    CHECK (typeof(share_digest) = 'text' AND length(share_digest) = 71
           AND substr(share_digest, 1, 7) = 'sha256:'
           AND substr(share_digest, 8) NOT GLOB '*[^0-9a-f]*'),
    CHECK (typeof(source_project_id) = 'text' AND length(source_project_id) BETWEEN 1 AND 128
           AND source_project_id GLOB '[A-Za-z0-9]*'
           AND source_project_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(source_project_id, char(0)) = 0),
    CHECK (typeof(recipient_project_id) = 'text' AND length(recipient_project_id) BETWEEN 1 AND 128
           AND recipient_project_id GLOB '[A-Za-z0-9]*'
           AND recipient_project_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(recipient_project_id, char(0)) = 0),
    CHECK (source_project_id <> recipient_project_id),
    CHECK (typeof(governed_record_id) = 'text' AND length(governed_record_id) BETWEEN 1 AND 128
           AND governed_record_id GLOB '[A-Za-z0-9]*'
           AND governed_record_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(governed_record_id, char(0)) = 0),
    CHECK (typeof(domain_scope) = 'text' AND length(domain_scope) BETWEEN 1 AND 128
           AND domain_scope GLOB '[a-z]*'
           AND domain_scope NOT GLOB '*[^a-z0-9_.]*'
           AND domain_scope NOT GLOB '*.'
           AND domain_scope NOT GLOB '*.[^a-z]*'
           AND instr(domain_scope, char(0)) = 0),
    CHECK (typeof(governed_assembly_id) = 'text' AND length(governed_assembly_id) BETWEEN 1 AND 128
           AND governed_assembly_id GLOB '[A-Za-z0-9]*'
           AND governed_assembly_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(governed_assembly_id, char(0)) = 0),
    CHECK (typeof(governed_record_version_id) = 'text' AND length(governed_record_version_id) BETWEEN 1 AND 128
           AND governed_record_version_id GLOB '[A-Za-z0-9]*'
           AND governed_record_version_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(governed_record_version_id, char(0)) = 0),
    CHECK (typeof(content_digest) = 'text' AND length(content_digest) = 71
           AND substr(content_digest, 1, 7) = 'sha256:'
           AND substr(content_digest, 8) NOT GLOB '*[^0-9a-f]*'),
    CHECK (typeof(proposed_by) = 'text' AND length(proposed_by) BETWEEN 1 AND 128
           AND proposed_by GLOB '[A-Za-z0-9]*'
           AND proposed_by NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(proposed_by, char(0)) = 0),
    CHECK (typeof(proposed_under_generation) = 'integer'
           AND proposed_under_generation BETWEEN 1 AND 9223372036854775807),
    CHECK (typeof(proposed_at_us) = 'integer' AND proposed_at_us BETWEEN 1 AND 9223372036854775807),

    PRIMARY KEY (workspace_id, share_id),
    UNIQUE (workspace_id, share_digest)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_knowledge_share_decisions (
    workspace_id                TEXT    NOT NULL,
    share_id                    TEXT    NOT NULL,
    decision                    TEXT    NOT NULL,
    decided_by                  TEXT    NOT NULL,
    decided_under_generation    INTEGER NOT NULL,
    decided_at_us               INTEGER NOT NULL,

    CHECK (decision IN ('accepted', 'revoked')),
    CHECK (typeof(decided_by) = 'text' AND length(decided_by) BETWEEN 1 AND 128
           AND decided_by GLOB '[A-Za-z0-9]*'
           AND decided_by NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(decided_by, char(0)) = 0),
    CHECK (typeof(decided_under_generation) = 'integer'
           AND decided_under_generation BETWEEN 1 AND 9223372036854775807),
    CHECK (typeof(decided_at_us) = 'integer' AND decided_at_us BETWEEN 1 AND 9223372036854775807),

    PRIMARY KEY (workspace_id, share_id, decision),
    FOREIGN KEY (workspace_id, share_id)
        REFERENCES omnivia_knowledge_shares (workspace_id, share_id)
) WITHOUT ROWID;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_knowledge_shares_insert
BEFORE INSERT ON omnivia_knowledge_shares
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_knowledge_shares')
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
              AND l.lifecycle IN ('acquiring', 'held', 'draining'));
    SELECT RAISE(ABORT, 'omnivia: a knowledge share must bind the open workspace')
    WHERE NEW.workspace_id IS NOT (
        SELECT workspace_id FROM omnivia_workspace_state WHERE singleton = 1);
    SELECT RAISE(ABORT, 'omnivia: a knowledge share must bind the current fencing generation')
    WHERE NEW.proposed_under_generation IS NOT (
        SELECT fencing_generation FROM omnivia_workspace_state WHERE singleton = 1);
    SELECT RAISE(ABORT, 'omnivia: a knowledge share must bind one sealed, canonical, unsuperseded governed version')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_authoritative_governed_versions v
        WHERE v.workspace_id = NEW.workspace_id
          AND v.assembly_id = NEW.governed_assembly_id
          AND v.governed_record_id = NEW.governed_record_id
          AND v.governed_record_version_id = NEW.governed_record_version_id
          AND v.domain_scope = NEW.domain_scope
          AND v.content_digest = NEW.content_digest
          AND v.authority_level = 'canonical'
          AND v.governance_disposition = 'accepted'
          AND NOT EXISTS (
              SELECT 1 FROM omnivia_record_supersessions r
              WHERE r.workspace_id = v.workspace_id
                AND r.source_version_id = v.governed_record_version_id));
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_knowledge_shares_update
BEFORE UPDATE ON omnivia_knowledge_shares
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_knowledge_shares is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_knowledge_shares_delete
BEFORE DELETE ON omnivia_knowledge_shares
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_knowledge_shares is append-only; DELETE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_knowledge_share_decisions_insert
BEFORE INSERT ON omnivia_knowledge_share_decisions
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_knowledge_share_decisions')
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
              AND l.lifecycle IN ('acquiring', 'held', 'draining'));
    SELECT RAISE(ABORT, 'omnivia: a knowledge share decision must bind the open workspace')
    WHERE NEW.workspace_id IS NOT (
        SELECT workspace_id FROM omnivia_workspace_state WHERE singleton = 1);
    SELECT RAISE(ABORT, 'omnivia: a knowledge share decision must bind the current fencing generation')
    WHERE NEW.decided_under_generation IS NOT (
        SELECT fencing_generation FROM omnivia_workspace_state WHERE singleton = 1);
    SELECT RAISE(ABORT, 'omnivia: a knowledge share can be revoked only after it is accepted')
    WHERE NEW.decision = 'revoked' AND NOT EXISTS (
        SELECT 1 FROM omnivia_knowledge_share_decisions d
        WHERE d.workspace_id = NEW.workspace_id AND d.share_id = NEW.share_id
          AND d.decision = 'accepted');
    SELECT RAISE(ABORT, 'omnivia: a revoked knowledge share cannot be accepted')
    WHERE NEW.decision = 'accepted' AND EXISTS (
        SELECT 1 FROM omnivia_knowledge_share_decisions d
        WHERE d.workspace_id = NEW.workspace_id AND d.share_id = NEW.share_id
          AND d.decision = 'revoked');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_knowledge_share_decisions_update
BEFORE UPDATE ON omnivia_knowledge_share_decisions
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_knowledge_share_decisions is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_knowledge_share_decisions_delete
BEFORE DELETE ON omnivia_knowledge_share_decisions
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_knowledge_share_decisions is append-only; DELETE is never permitted');
END;
