-- Managed Skills registry and per-Run skill bindings (C17, Agent Runtime).
--
-- Additive only. Eight append-only tables and twenty-four statement triggers. One registry
-- per workspace holds every skill the workspace authors, publishes, installs and binds to a
-- Run. Nothing here executes a skill, selects one for a Run on its own, or grants anything:
-- a manifest and its instructions are inert data, and no column here names a permission, a
-- tool, a budget, a path, a network right, a credential or a sandbox. The only authority a
-- skill version ever has is what the bound role's envelope already grants at execution time.
--
--   omnivia_skill_drafts              one authored draft: its skill name and origin, fixed at creation
--   omnivia_skill_draft_revisions     one numbered, immutable revision of a draft's manifest
--   omnivia_skill_proposals           one draft revision submitted to the publisher queue, with evidence
--   omnivia_skill_versions            one published, immutable, content-addressed manifest
--   omnivia_skill_deprecations        one version marked deprecated; the version itself stays
--   omnivia_skill_install_events      one install or removal of a published version in the workspace
--   omnivia_skill_run_bindings        one exact manifest a Run was admitted with, for one role
--   omnivia_skill_run_binding_seals   the point after which a Run's bindings can no longer grow
--
-- Identity, versions and content
-- ------------------------------
--
-- A skill is `(workspace_id, skill_name)`. A published version is one `manifest_id`: the
-- text `skill-` and the SHA-256 of the canonical manifest, recomputed by every read, so a
-- row edited outside this database reads as corrupt rather than as another manifest. A
-- `(skill_name, version)` pair is published at most once. Changed content therefore needs a
-- new version, and the same content under the same version is refused as well as different
-- content. A version publishes exactly the draft revision its proposal submitted, which the
-- insert trigger compares column for column. Dependencies are pinned by manifest id and must
-- already be published, so a published manifest cannot follow a moving dependency, and a
-- dependency cycle cannot be written: an id is a hash of content that contains it.
--
-- Roles are enforced above this layer, by the operation that writes each table. Authorship
-- never implies publication and publication never implies installation, so the actor of
-- each step is recorded on its own row and no row is inferred from another.
--
-- Install history is not a column
-- -------------------------------
--
-- The highest-numbered install event for a manifest is whether it is installed. Events
-- alternate between install and remove, so an install is idempotent above this layer and a
-- removal is history rather than deletion. A deprecated version cannot be installed. Removal
-- prevents new selection and touches nothing else: published versions, drafts, proposals and
-- every Run binding stay exactly as they were.
--
-- Run bindings are written once
-- -----------------------------
--
-- A binding names the exact manifest a Run was admitted with, for one role. It is written
-- only under the audit event of the Run's own admission and at the instant of that
-- admission, and a seal then closes the set. After the seal no binding is added, so a newer
-- version that is published or installed later never reaches a Run that already exists.
-- Core has no operation that amends a Run, so this migration provides none either.
--
-- UPDATE and DELETE abort unconditionally, for the current fenced owner too. Retention is
-- deliberately not provided; deletion or compaction needs its own migration.
--
-- Every comment in this file sits between statements and never inside one, for the
-- fingerprint-loader reason 0018 states.

CREATE TABLE IF NOT EXISTS omnivia_skill_drafts (
    workspace_id    TEXT NOT NULL,
    draft_id        TEXT NOT NULL,
    skill_name      TEXT NOT NULL,
    source_work_ref TEXT,
    created_by      TEXT NOT NULL,
    created_at_us   INTEGER NOT NULL,
    audit_ref       TEXT NOT NULL,

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(draft_id) = 'text' AND length(draft_id) BETWEEN 1 AND 128
           AND draft_id GLOB '[A-Za-z0-9]*'
           AND draft_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(draft_id, char(0)) = 0),
    CHECK (typeof(skill_name) = 'text' AND length(skill_name) BETWEEN 1 AND 128
           AND skill_name GLOB '[A-Za-z0-9]*'
           AND skill_name NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(skill_name, char(0)) = 0),
    CHECK (source_work_ref IS NULL OR (typeof(source_work_ref) = 'text' AND length(source_work_ref) BETWEEN 1 AND 128
           AND source_work_ref GLOB '[A-Za-z0-9]*'
           AND source_work_ref NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(source_work_ref, char(0)) = 0)),
    CHECK (typeof(created_by) = 'text' AND length(created_by) BETWEEN 1 AND 128 AND instr(created_by, char(0)) = 0),
    CHECK (typeof(created_at_us) = 'integer' AND created_at_us > 0),
    CHECK (typeof(audit_ref) = 'text' AND length(audit_ref) BETWEEN 1 AND 128
           AND audit_ref GLOB '[A-Za-z0-9]*'
           AND audit_ref NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(audit_ref, char(0)) = 0),

    PRIMARY KEY (workspace_id, draft_id),
    UNIQUE (workspace_id, draft_id, skill_name),
    FOREIGN KEY (audit_ref, workspace_id)
        REFERENCES omnivia_application_audit_events (audit_ref, workspace_id)
) WITHOUT ROWID;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_skill_drafts_insert
BEFORE INSERT ON omnivia_skill_drafts
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_skill_drafts')
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
    SELECT RAISE(ABORT, 'omnivia: skill draft audit reference must belong to its workspace')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_application_audit_events
        WHERE audit_ref = NEW.audit_ref AND workspace_id = NEW.workspace_id);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_skill_drafts_update
BEFORE UPDATE ON omnivia_skill_drafts
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_skill_drafts is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_skill_drafts_delete
BEFORE DELETE ON omnivia_skill_drafts
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_skill_drafts is append-only; DELETE is never permitted');
END;

CREATE TABLE IF NOT EXISTS omnivia_skill_draft_revisions (
    workspace_id      TEXT NOT NULL,
    draft_revision_id TEXT NOT NULL,
    draft_id          TEXT NOT NULL,
    skill_name        TEXT NOT NULL,
    draft_revision    INTEGER NOT NULL,
    version           TEXT NOT NULL,
    manifest_id       TEXT NOT NULL,
    manifest_json     TEXT NOT NULL,
    updated_by        TEXT NOT NULL,
    created_at_us     INTEGER NOT NULL,
    audit_ref         TEXT NOT NULL,

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(draft_revision_id) = 'text' AND length(draft_revision_id) BETWEEN 1 AND 128
           AND draft_revision_id GLOB '[A-Za-z0-9]*'
           AND draft_revision_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(draft_revision_id, char(0)) = 0),
    CHECK (typeof(draft_id) = 'text' AND length(draft_id) BETWEEN 1 AND 128
           AND draft_id GLOB '[A-Za-z0-9]*'
           AND draft_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(draft_id, char(0)) = 0),
    CHECK (typeof(skill_name) = 'text' AND length(skill_name) BETWEEN 1 AND 128
           AND skill_name GLOB '[A-Za-z0-9]*'
           AND skill_name NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(skill_name, char(0)) = 0),
    CHECK (typeof(draft_revision) = 'integer' AND draft_revision > 0),
    CHECK (typeof(version) = 'text' AND length(version) BETWEEN 5 AND 20
           AND version GLOB '[0-9]*.[0-9]*.[0-9]*'
           AND version NOT GLOB '*[^0-9.]*'),
    CHECK (typeof(manifest_id) = 'text' AND length(manifest_id) = 70
           AND substr(manifest_id, 1, 6) = 'skill-'
           AND substr(manifest_id, 7) NOT GLOB '*[^0-9a-f]*'),
    CHECK (typeof(manifest_json) = 'text' AND length(manifest_json) BETWEEN 2 AND 131072
           AND json_valid(manifest_json) IS 1 AND json_type(manifest_json) = 'object'),
    CHECK (typeof(updated_by) = 'text' AND length(updated_by) BETWEEN 1 AND 128 AND instr(updated_by, char(0)) = 0),
    CHECK (typeof(created_at_us) = 'integer' AND created_at_us > 0),
    CHECK (typeof(audit_ref) = 'text' AND length(audit_ref) BETWEEN 1 AND 128
           AND audit_ref GLOB '[A-Za-z0-9]*'
           AND audit_ref NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(audit_ref, char(0)) = 0),

    PRIMARY KEY (workspace_id, draft_revision_id),
    UNIQUE (workspace_id, draft_id, draft_revision),
    FOREIGN KEY (workspace_id, draft_id, skill_name)
        REFERENCES omnivia_skill_drafts (workspace_id, draft_id, skill_name),
    FOREIGN KEY (audit_ref, workspace_id)
        REFERENCES omnivia_application_audit_events (audit_ref, workspace_id)
) WITHOUT ROWID;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_skill_draft_revisions_insert
BEFORE INSERT ON omnivia_skill_draft_revisions
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_skill_draft_revisions')
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
    SELECT RAISE(ABORT, 'omnivia: skill draft revision must be contiguous')
    WHERE NEW.draft_revision IS NOT (
        SELECT COALESCE(MAX(draft_revision), 0) + 1
        FROM omnivia_skill_draft_revisions
        WHERE workspace_id = NEW.workspace_id AND draft_id = NEW.draft_id);
    SELECT RAISE(ABORT, 'omnivia: a skill draft revision must carry the draft skill name and version it declares')
    WHERE json_extract(NEW.manifest_json, '$.skill_name') IS NOT NEW.skill_name
       OR json_extract(NEW.manifest_json, '$.version') IS NOT NEW.version;
    SELECT RAISE(ABORT, 'omnivia: a skill draft revision must change the manifest')
    WHERE NEW.draft_revision > 1 AND EXISTS (
        SELECT 1 FROM omnivia_skill_draft_revisions prior
        WHERE prior.workspace_id = NEW.workspace_id
          AND prior.draft_id = NEW.draft_id
          AND prior.draft_revision = NEW.draft_revision - 1
          AND prior.manifest_id = NEW.manifest_id);
    SELECT RAISE(ABORT, 'omnivia: a submitted skill draft is closed to revision')
    WHERE EXISTS (
        SELECT 1 FROM omnivia_skill_proposals
        WHERE workspace_id = NEW.workspace_id AND draft_id = NEW.draft_id);
    SELECT RAISE(ABORT, 'omnivia: skill draft revision time must not regress')
    WHERE NEW.created_at_us < COALESCE((
        SELECT MAX(created_at_us) FROM omnivia_skill_draft_revisions
        WHERE workspace_id = NEW.workspace_id AND draft_id = NEW.draft_id),
        (SELECT created_at_us FROM omnivia_skill_drafts
         WHERE workspace_id = NEW.workspace_id AND draft_id = NEW.draft_id), 0);
    SELECT RAISE(ABORT, 'omnivia: skill draft revision audit reference must belong to its workspace')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_application_audit_events
        WHERE audit_ref = NEW.audit_ref AND workspace_id = NEW.workspace_id);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_skill_draft_revisions_update
BEFORE UPDATE ON omnivia_skill_draft_revisions
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_skill_draft_revisions is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_skill_draft_revisions_delete
BEFORE DELETE ON omnivia_skill_draft_revisions
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_skill_draft_revisions is append-only; DELETE is never permitted');
END;

CREATE TABLE IF NOT EXISTS omnivia_skill_proposals (
    workspace_id    TEXT NOT NULL,
    proposal_id     TEXT NOT NULL,
    draft_id        TEXT NOT NULL,
    draft_revision  INTEGER NOT NULL,
    skill_name      TEXT NOT NULL,
    evidence_json   TEXT NOT NULL,
    submitted_by    TEXT NOT NULL,
    submitted_at_us INTEGER NOT NULL,
    audit_ref       TEXT NOT NULL,

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(proposal_id) = 'text' AND length(proposal_id) BETWEEN 1 AND 128
           AND proposal_id GLOB '[A-Za-z0-9]*'
           AND proposal_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(proposal_id, char(0)) = 0),
    CHECK (typeof(draft_id) = 'text' AND length(draft_id) BETWEEN 1 AND 128
           AND draft_id GLOB '[A-Za-z0-9]*'
           AND draft_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(draft_id, char(0)) = 0),
    CHECK (typeof(draft_revision) = 'integer' AND draft_revision > 0),
    CHECK (typeof(skill_name) = 'text' AND length(skill_name) BETWEEN 1 AND 128
           AND skill_name GLOB '[A-Za-z0-9]*'
           AND skill_name NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(skill_name, char(0)) = 0),
    CHECK (typeof(evidence_json) = 'text' AND length(evidence_json) BETWEEN 2 AND 16384
           AND json_valid(evidence_json) IS 1 AND json_type(evidence_json) = 'array'),
    CHECK (typeof(submitted_by) = 'text' AND length(submitted_by) BETWEEN 1 AND 128 AND instr(submitted_by, char(0)) = 0),
    CHECK (typeof(submitted_at_us) = 'integer' AND submitted_at_us > 0),
    CHECK (typeof(audit_ref) = 'text' AND length(audit_ref) BETWEEN 1 AND 128
           AND audit_ref GLOB '[A-Za-z0-9]*'
           AND audit_ref NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(audit_ref, char(0)) = 0),

    PRIMARY KEY (workspace_id, proposal_id),
    UNIQUE (workspace_id, draft_id),
    UNIQUE (workspace_id, proposal_id, draft_id, draft_revision),
    FOREIGN KEY (workspace_id, draft_id, draft_revision)
        REFERENCES omnivia_skill_draft_revisions (workspace_id, draft_id, draft_revision),
    FOREIGN KEY (audit_ref, workspace_id)
        REFERENCES omnivia_application_audit_events (audit_ref, workspace_id)
) WITHOUT ROWID;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_skill_proposals_insert
BEFORE INSERT ON omnivia_skill_proposals
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_skill_proposals')
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
    SELECT RAISE(ABORT, 'omnivia: a skill proposal must name the latest draft revision')
    WHERE NEW.draft_revision IS NOT (
        SELECT MAX(draft_revision) FROM omnivia_skill_draft_revisions
        WHERE workspace_id = NEW.workspace_id AND draft_id = NEW.draft_id);
    SELECT RAISE(ABORT, 'omnivia: a skill proposal must carry the skill name of its draft')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_skill_drafts
        WHERE workspace_id = NEW.workspace_id AND draft_id = NEW.draft_id
          AND skill_name = NEW.skill_name);
    SELECT RAISE(ABORT, 'omnivia: a skill proposal cannot predate the revision it submits')
    WHERE NEW.submitted_at_us < COALESCE((
        SELECT created_at_us FROM omnivia_skill_draft_revisions
        WHERE workspace_id = NEW.workspace_id AND draft_id = NEW.draft_id
          AND draft_revision = NEW.draft_revision), 0);
    SELECT RAISE(ABORT, 'omnivia: skill proposal audit reference must belong to its workspace')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_application_audit_events
        WHERE audit_ref = NEW.audit_ref AND workspace_id = NEW.workspace_id);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_skill_proposals_update
BEFORE UPDATE ON omnivia_skill_proposals
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_skill_proposals is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_skill_proposals_delete
BEFORE DELETE ON omnivia_skill_proposals
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_skill_proposals is append-only; DELETE is never permitted');
END;

CREATE TABLE IF NOT EXISTS omnivia_skill_versions (
    workspace_id         TEXT NOT NULL,
    manifest_id          TEXT NOT NULL,
    skill_name           TEXT NOT NULL,
    version              TEXT NOT NULL,
    manifest_json        TEXT NOT NULL,
    draft_id             TEXT NOT NULL,
    draft_revision       INTEGER NOT NULL,
    proposal_id          TEXT NOT NULL,
    review_evidence_json TEXT NOT NULL,
    published_by         TEXT NOT NULL,
    published_at_us      INTEGER NOT NULL,
    audit_ref            TEXT NOT NULL,

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(manifest_id) = 'text' AND length(manifest_id) = 70
           AND substr(manifest_id, 1, 6) = 'skill-'
           AND substr(manifest_id, 7) NOT GLOB '*[^0-9a-f]*'),
    CHECK (typeof(skill_name) = 'text' AND length(skill_name) BETWEEN 1 AND 128
           AND skill_name GLOB '[A-Za-z0-9]*'
           AND skill_name NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(skill_name, char(0)) = 0),
    CHECK (typeof(version) = 'text' AND length(version) BETWEEN 5 AND 20
           AND version GLOB '[0-9]*.[0-9]*.[0-9]*'
           AND version NOT GLOB '*[^0-9.]*'),
    CHECK (typeof(manifest_json) = 'text' AND length(manifest_json) BETWEEN 2 AND 131072
           AND json_valid(manifest_json) IS 1 AND json_type(manifest_json) = 'object'),
    CHECK (typeof(draft_id) = 'text' AND length(draft_id) BETWEEN 1 AND 128
           AND draft_id GLOB '[A-Za-z0-9]*'
           AND draft_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(draft_id, char(0)) = 0),
    CHECK (typeof(draft_revision) = 'integer' AND draft_revision > 0),
    CHECK (typeof(proposal_id) = 'text' AND length(proposal_id) BETWEEN 1 AND 128
           AND proposal_id GLOB '[A-Za-z0-9]*'
           AND proposal_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(proposal_id, char(0)) = 0),
    CHECK (typeof(review_evidence_json) = 'text' AND length(review_evidence_json) BETWEEN 2 AND 16384
           AND json_valid(review_evidence_json) IS 1 AND json_type(review_evidence_json) = 'array'),
    CHECK (typeof(published_by) = 'text' AND length(published_by) BETWEEN 1 AND 128 AND instr(published_by, char(0)) = 0),
    CHECK (typeof(published_at_us) = 'integer' AND published_at_us > 0),
    CHECK (typeof(audit_ref) = 'text' AND length(audit_ref) BETWEEN 1 AND 128
           AND audit_ref GLOB '[A-Za-z0-9]*'
           AND audit_ref NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(audit_ref, char(0)) = 0),

    PRIMARY KEY (workspace_id, manifest_id),
    UNIQUE (workspace_id, manifest_id, skill_name),
    UNIQUE (workspace_id, skill_name, version),
    UNIQUE (workspace_id, proposal_id),
    FOREIGN KEY (workspace_id, proposal_id, draft_id, draft_revision)
        REFERENCES omnivia_skill_proposals (workspace_id, proposal_id, draft_id, draft_revision),
    FOREIGN KEY (audit_ref, workspace_id)
        REFERENCES omnivia_application_audit_events (audit_ref, workspace_id)
) WITHOUT ROWID;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_skill_versions_insert
BEFORE INSERT ON omnivia_skill_versions
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_skill_versions')
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
    SELECT RAISE(ABORT, 'omnivia: a skill version must publish exactly the revision its proposal submitted')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_skill_draft_revisions r
        WHERE r.workspace_id = NEW.workspace_id
          AND r.draft_id = NEW.draft_id
          AND r.draft_revision = NEW.draft_revision
          AND r.skill_name = NEW.skill_name
          AND r.version = NEW.version
          AND r.manifest_id = NEW.manifest_id
          AND r.manifest_json = NEW.manifest_json);
    SELECT RAISE(ABORT, 'omnivia: a skill version must pin only published dependencies of other skills')
    WHERE EXISTS (
        SELECT 1 FROM json_each(NEW.manifest_json, '$.dependencies') d
        WHERE NOT EXISTS (
            SELECT 1 FROM omnivia_skill_versions v
            WHERE v.workspace_id = NEW.workspace_id
              AND v.manifest_id = json_extract(d.value, '$.manifest_id')
              AND v.skill_name = json_extract(d.value, '$.skill_name')
              AND v.skill_name IS NOT NEW.skill_name));
    SELECT RAISE(ABORT, 'omnivia: a skill version cannot predate its proposal')
    WHERE NEW.published_at_us < COALESCE((
        SELECT submitted_at_us FROM omnivia_skill_proposals
        WHERE workspace_id = NEW.workspace_id AND proposal_id = NEW.proposal_id), 0);
    SELECT RAISE(ABORT, 'omnivia: skill version audit reference must belong to its workspace')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_application_audit_events
        WHERE audit_ref = NEW.audit_ref AND workspace_id = NEW.workspace_id);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_skill_versions_update
BEFORE UPDATE ON omnivia_skill_versions
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_skill_versions is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_skill_versions_delete
BEFORE DELETE ON omnivia_skill_versions
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_skill_versions is append-only; DELETE is never permitted');
END;

CREATE TABLE IF NOT EXISTS omnivia_skill_deprecations (
    workspace_id     TEXT NOT NULL,
    deprecation_id   TEXT NOT NULL,
    manifest_id      TEXT NOT NULL,
    reason           TEXT NOT NULL,
    deprecated_by    TEXT NOT NULL,
    deprecated_at_us INTEGER NOT NULL,
    audit_ref        TEXT NOT NULL,

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(deprecation_id) = 'text' AND length(deprecation_id) BETWEEN 1 AND 128
           AND deprecation_id GLOB '[A-Za-z0-9]*'
           AND deprecation_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(deprecation_id, char(0)) = 0),
    CHECK (typeof(manifest_id) = 'text' AND length(manifest_id) = 70
           AND substr(manifest_id, 1, 6) = 'skill-'
           AND substr(manifest_id, 7) NOT GLOB '*[^0-9a-f]*'),
    CHECK (typeof(reason) = 'text' AND length(reason) BETWEEN 1 AND 128
           AND reason GLOB '[a-z]*'
           AND reason NOT GLOB '*[^a-z0-9_.]*'
           AND reason NOT GLOB '*.'
           AND reason NOT GLOB '*.[^a-z]*'),
    CHECK (typeof(deprecated_by) = 'text' AND length(deprecated_by) BETWEEN 1 AND 128 AND instr(deprecated_by, char(0)) = 0),
    CHECK (typeof(deprecated_at_us) = 'integer' AND deprecated_at_us > 0),
    CHECK (typeof(audit_ref) = 'text' AND length(audit_ref) BETWEEN 1 AND 128
           AND audit_ref GLOB '[A-Za-z0-9]*'
           AND audit_ref NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(audit_ref, char(0)) = 0),

    PRIMARY KEY (workspace_id, deprecation_id),
    UNIQUE (workspace_id, manifest_id),
    FOREIGN KEY (workspace_id, manifest_id)
        REFERENCES omnivia_skill_versions (workspace_id, manifest_id),
    FOREIGN KEY (audit_ref, workspace_id)
        REFERENCES omnivia_application_audit_events (audit_ref, workspace_id)
) WITHOUT ROWID;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_skill_deprecations_insert
BEFORE INSERT ON omnivia_skill_deprecations
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_skill_deprecations')
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
    SELECT RAISE(ABORT, 'omnivia: a skill deprecation cannot predate the version it marks')
    WHERE NEW.deprecated_at_us < COALESCE((
        SELECT published_at_us FROM omnivia_skill_versions
        WHERE workspace_id = NEW.workspace_id AND manifest_id = NEW.manifest_id), 0);
    SELECT RAISE(ABORT, 'omnivia: skill deprecation audit reference must belong to its workspace')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_application_audit_events
        WHERE audit_ref = NEW.audit_ref AND workspace_id = NEW.workspace_id);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_skill_deprecations_update
BEFORE UPDATE ON omnivia_skill_deprecations
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_skill_deprecations is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_skill_deprecations_delete
BEFORE DELETE ON omnivia_skill_deprecations
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_skill_deprecations is append-only; DELETE is never permitted');
END;

CREATE TABLE IF NOT EXISTS omnivia_skill_install_events (
    workspace_id     TEXT NOT NULL,
    install_event_id TEXT NOT NULL,
    manifest_id      TEXT NOT NULL,
    skill_name       TEXT NOT NULL,
    event_sequence   INTEGER NOT NULL,
    event_kind       TEXT NOT NULL,
    actor            TEXT NOT NULL,
    occurred_at_us   INTEGER NOT NULL,
    audit_ref        TEXT NOT NULL,

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(install_event_id) = 'text' AND length(install_event_id) BETWEEN 1 AND 128
           AND install_event_id GLOB '[A-Za-z0-9]*'
           AND install_event_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(install_event_id, char(0)) = 0),
    CHECK (typeof(manifest_id) = 'text' AND length(manifest_id) = 70
           AND substr(manifest_id, 1, 6) = 'skill-'
           AND substr(manifest_id, 7) NOT GLOB '*[^0-9a-f]*'),
    CHECK (typeof(skill_name) = 'text' AND length(skill_name) BETWEEN 1 AND 128
           AND skill_name GLOB '[A-Za-z0-9]*'
           AND skill_name NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(skill_name, char(0)) = 0),
    CHECK (typeof(event_sequence) = 'integer' AND event_sequence > 0),
    CHECK (event_kind IN ('install', 'remove')),
    CHECK (typeof(actor) = 'text' AND length(actor) BETWEEN 1 AND 128 AND instr(actor, char(0)) = 0),
    CHECK (typeof(occurred_at_us) = 'integer' AND occurred_at_us > 0),
    CHECK (typeof(audit_ref) = 'text' AND length(audit_ref) BETWEEN 1 AND 128
           AND audit_ref GLOB '[A-Za-z0-9]*'
           AND audit_ref NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(audit_ref, char(0)) = 0),

    PRIMARY KEY (workspace_id, install_event_id),
    UNIQUE (workspace_id, manifest_id, event_sequence),
    FOREIGN KEY (workspace_id, manifest_id, skill_name)
        REFERENCES omnivia_skill_versions (workspace_id, manifest_id, skill_name),
    FOREIGN KEY (audit_ref, workspace_id)
        REFERENCES omnivia_application_audit_events (audit_ref, workspace_id)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS omnivia_idx_skill_install_events_name
    ON omnivia_skill_install_events (workspace_id, skill_name, manifest_id, event_sequence);

CREATE TRIGGER IF NOT EXISTS omnivia_guard_skill_install_events_insert
BEFORE INSERT ON omnivia_skill_install_events
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_skill_install_events')
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
    SELECT RAISE(ABORT, 'omnivia: skill install event sequence must be contiguous')
    WHERE NEW.event_sequence IS NOT (
        SELECT COALESCE(MAX(event_sequence), 0) + 1
        FROM omnivia_skill_install_events
        WHERE workspace_id = NEW.workspace_id AND manifest_id = NEW.manifest_id);
    SELECT RAISE(ABORT, 'omnivia: a skill install history starts with an install')
    WHERE NEW.event_sequence = 1 AND NEW.event_kind <> 'install';
    SELECT RAISE(ABORT, 'omnivia: skill install events must alternate between install and remove')
    WHERE NEW.event_sequence > 1 AND EXISTS (
        SELECT 1 FROM omnivia_skill_install_events prior
        WHERE prior.workspace_id = NEW.workspace_id
          AND prior.manifest_id = NEW.manifest_id
          AND prior.event_sequence = NEW.event_sequence - 1
          AND prior.event_kind = NEW.event_kind);
    SELECT RAISE(ABORT, 'omnivia: a deprecated skill version cannot be installed')
    WHERE NEW.event_kind = 'install' AND EXISTS (
        SELECT 1 FROM omnivia_skill_deprecations
        WHERE workspace_id = NEW.workspace_id AND manifest_id = NEW.manifest_id);
    SELECT RAISE(ABORT, 'omnivia: skill install event time must not regress')
    WHERE NEW.occurred_at_us < COALESCE((
        SELECT MAX(occurred_at_us) FROM omnivia_skill_install_events
        WHERE workspace_id = NEW.workspace_id AND manifest_id = NEW.manifest_id),
        (SELECT published_at_us FROM omnivia_skill_versions
         WHERE workspace_id = NEW.workspace_id AND manifest_id = NEW.manifest_id), 0);
    SELECT RAISE(ABORT, 'omnivia: skill install event audit reference must belong to its workspace')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_application_audit_events
        WHERE audit_ref = NEW.audit_ref AND workspace_id = NEW.workspace_id);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_skill_install_events_update
BEFORE UPDATE ON omnivia_skill_install_events
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_skill_install_events is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_skill_install_events_delete
BEFORE DELETE ON omnivia_skill_install_events
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_skill_install_events is append-only; DELETE is never permitted');
END;

CREATE TABLE IF NOT EXISTS omnivia_skill_run_bindings (
    workspace_id     TEXT NOT NULL,
    run_binding_id   TEXT NOT NULL,
    run_id           TEXT NOT NULL,
    binding_position INTEGER NOT NULL,
    role_id          TEXT NOT NULL,
    manifest_id      TEXT NOT NULL,
    skill_name       TEXT NOT NULL,
    selection        TEXT NOT NULL,
    binding_digest   TEXT NOT NULL,
    bound_at_us      INTEGER NOT NULL,
    audit_ref        TEXT NOT NULL,

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(run_binding_id) = 'text' AND length(run_binding_id) BETWEEN 1 AND 128
           AND run_binding_id GLOB '[A-Za-z0-9]*'
           AND run_binding_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(run_binding_id, char(0)) = 0),
    CHECK (typeof(run_id) = 'text' AND length(run_id) BETWEEN 1 AND 128
           AND run_id GLOB '[A-Za-z0-9]*'
           AND run_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(run_id, char(0)) = 0),
    CHECK (typeof(binding_position) = 'integer' AND binding_position > 0),
    CHECK (typeof(role_id) = 'text' AND length(role_id) BETWEEN 1 AND 128
           AND role_id GLOB '[A-Za-z0-9]*'
           AND role_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(role_id, char(0)) = 0),
    CHECK (typeof(manifest_id) = 'text' AND length(manifest_id) = 70
           AND substr(manifest_id, 1, 6) = 'skill-'
           AND substr(manifest_id, 7) NOT GLOB '*[^0-9a-f]*'),
    CHECK (typeof(skill_name) = 'text' AND length(skill_name) BETWEEN 1 AND 128
           AND skill_name GLOB '[A-Za-z0-9]*'
           AND skill_name NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(skill_name, char(0)) = 0),
    CHECK (selection IN ('explicit', 'highest_compatible', 'dependency')),
    CHECK (typeof(binding_digest) = 'text' AND length(binding_digest) = 71
           AND substr(binding_digest, 1, 7) = 'sha256:'
           AND substr(binding_digest, 8) NOT GLOB '*[^0-9a-f]*'),
    CHECK (typeof(bound_at_us) = 'integer' AND bound_at_us > 0),
    CHECK (typeof(audit_ref) = 'text' AND length(audit_ref) BETWEEN 1 AND 128
           AND audit_ref GLOB '[A-Za-z0-9]*'
           AND audit_ref NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(audit_ref, char(0)) = 0),

    PRIMARY KEY (workspace_id, run_binding_id),
    UNIQUE (workspace_id, run_id, binding_position),
    UNIQUE (workspace_id, run_id, role_id, manifest_id),
    FOREIGN KEY (workspace_id, run_id)
        REFERENCES omnivia_workflow_runs (workspace_id, run_id),
    FOREIGN KEY (workspace_id, manifest_id, skill_name)
        REFERENCES omnivia_skill_versions (workspace_id, manifest_id, skill_name),
    FOREIGN KEY (audit_ref, workspace_id)
        REFERENCES omnivia_application_audit_events (audit_ref, workspace_id)
) WITHOUT ROWID;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_skill_run_bindings_insert
BEFORE INSERT ON omnivia_skill_run_bindings
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_skill_run_bindings')
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
    SELECT RAISE(ABORT, 'omnivia: a skill run binding is written only under its run admission')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_workflow_runs w
        JOIN omnivia_runtime_runs r
          ON r.workspace_id = w.workspace_id AND r.run_id = w.run_id
        WHERE w.workspace_id = NEW.workspace_id
          AND w.run_id = NEW.run_id
          AND w.bound_at_us = NEW.bound_at_us
          AND r.audit_ref = NEW.audit_ref);
    SELECT RAISE(ABORT, 'omnivia: skill run binding position must be contiguous')
    WHERE NEW.binding_position IS NOT (
        SELECT COALESCE(MAX(binding_position), 0) + 1
        FROM omnivia_skill_run_bindings
        WHERE workspace_id = NEW.workspace_id AND run_id = NEW.run_id);
    SELECT RAISE(ABORT, 'omnivia: a sealed skill run binding set cannot grow')
    WHERE EXISTS (
        SELECT 1 FROM omnivia_skill_run_binding_seals
        WHERE workspace_id = NEW.workspace_id AND run_id = NEW.run_id);
    SELECT RAISE(ABORT, 'omnivia: a selected skill must be installed and not deprecated when a run binds it')
    WHERE NEW.selection IN ('explicit', 'highest_compatible') AND (
        EXISTS (
            SELECT 1 FROM omnivia_skill_deprecations
            WHERE workspace_id = NEW.workspace_id AND manifest_id = NEW.manifest_id)
        OR NOT EXISTS (
            SELECT 1 FROM omnivia_skill_install_events e
            WHERE e.workspace_id = NEW.workspace_id
              AND e.manifest_id = NEW.manifest_id
              AND e.event_kind = 'install'
              AND e.event_sequence = (
                SELECT MAX(event_sequence) FROM omnivia_skill_install_events
                WHERE workspace_id = NEW.workspace_id AND manifest_id = NEW.manifest_id)));
    SELECT RAISE(ABORT, 'omnivia: skill run binding audit reference must belong to its workspace')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_application_audit_events
        WHERE audit_ref = NEW.audit_ref AND workspace_id = NEW.workspace_id);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_skill_run_bindings_update
BEFORE UPDATE ON omnivia_skill_run_bindings
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_skill_run_bindings is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_skill_run_bindings_delete
BEFORE DELETE ON omnivia_skill_run_bindings
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_skill_run_bindings is append-only; DELETE is never permitted');
END;

CREATE TABLE IF NOT EXISTS omnivia_skill_run_binding_seals (
    workspace_id  TEXT NOT NULL,
    run_id        TEXT NOT NULL,
    binding_count INTEGER NOT NULL,
    set_digest    TEXT NOT NULL,
    sealed_at_us  INTEGER NOT NULL,
    audit_ref     TEXT NOT NULL,

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(run_id) = 'text' AND length(run_id) BETWEEN 1 AND 128
           AND run_id GLOB '[A-Za-z0-9]*'
           AND run_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(run_id, char(0)) = 0),
    CHECK (typeof(binding_count) = 'integer' AND binding_count BETWEEN 1 AND 1024),
    CHECK (typeof(set_digest) = 'text' AND length(set_digest) = 71
           AND substr(set_digest, 1, 7) = 'sha256:'
           AND substr(set_digest, 8) NOT GLOB '*[^0-9a-f]*'),
    CHECK (typeof(sealed_at_us) = 'integer' AND sealed_at_us > 0),
    CHECK (typeof(audit_ref) = 'text' AND length(audit_ref) BETWEEN 1 AND 128
           AND audit_ref GLOB '[A-Za-z0-9]*'
           AND audit_ref NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(audit_ref, char(0)) = 0),

    PRIMARY KEY (workspace_id, run_id),
    FOREIGN KEY (workspace_id, run_id)
        REFERENCES omnivia_workflow_runs (workspace_id, run_id),
    FOREIGN KEY (audit_ref, workspace_id)
        REFERENCES omnivia_application_audit_events (audit_ref, workspace_id)
) WITHOUT ROWID;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_skill_run_binding_seals_insert
BEFORE INSERT ON omnivia_skill_run_binding_seals
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_skill_run_binding_seals')
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
    SELECT RAISE(ABORT, 'omnivia: a skill run binding seal is written only under its run admission')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_workflow_runs w
        JOIN omnivia_runtime_runs r
          ON r.workspace_id = w.workspace_id AND r.run_id = w.run_id
        WHERE w.workspace_id = NEW.workspace_id
          AND w.run_id = NEW.run_id
          AND w.bound_at_us = NEW.sealed_at_us
          AND r.audit_ref = NEW.audit_ref);
    SELECT RAISE(ABORT, 'omnivia: a skill run binding seal must count exactly the bindings it seals')
    WHERE NEW.binding_count IS NOT (
        SELECT COUNT(*) FROM omnivia_skill_run_bindings
        WHERE workspace_id = NEW.workspace_id AND run_id = NEW.run_id);
    SELECT RAISE(ABORT, 'omnivia: skill run binding seal audit reference must belong to its workspace')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_application_audit_events
        WHERE audit_ref = NEW.audit_ref AND workspace_id = NEW.workspace_id);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_skill_run_binding_seals_update
BEFORE UPDATE ON omnivia_skill_run_binding_seals
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_skill_run_binding_seals is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_skill_run_binding_seals_delete
BEFORE DELETE ON omnivia_skill_run_binding_seals
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_skill_run_binding_seals is append-only; DELETE is never permitted');
END;
