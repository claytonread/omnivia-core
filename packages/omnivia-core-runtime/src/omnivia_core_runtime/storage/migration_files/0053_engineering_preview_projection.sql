-- Engineering search previews served from a bounded projection
-- (SPEC-CORE-ENGMEM-001, plan P0-03; spec §11.1, AC-033).
--
-- Additive only; allocation 0053 (Engineering Memory, predecessor 0052). Until
-- now `engineering.search` hydrated every authorised version's full content and
-- claim before it ranked, filtered, paged or checked applicability, then cut the
-- preview from the body it had just read. AC-033 asks for no body read at all, so
-- a preview is stored, bounded, beside the version it describes:
--
--   omnivia_engineering_preview_projection  one row per (engineering-domain
--       assembly, projection version): the title (<= 200 code points), the preview
--       text (<= 480 code points and <= 2048 UTF-8 bytes), its truncation flag, the
--       observation kind, assertion basis, topic key, repository and snapshot the
--       version names, and the `content_digest` of the assembly it was derived
--       from. Append-only, like every other engineering fact: a new projection
--       version adds rows beside the old ones rather than rewriting them.
--   omnivia_engineering_preview_source      the one definition of what a preview
--       is. It reads `content_json`, and is read only by the service's writers
--       when they project a version, by the maintenance rebuild, by the INSERT
--       guard below and by the backfill in this file. A search never reads it.
--   omnivia_authoritative_governed_version_metadata  0009's
--       `omnivia_authoritative_governed_versions` without its one body column,
--       `content_json`: the same sealed versions, the same joins, every identity,
--       currentness and evidence fact and the stored `content_digest`. The
--       authorised frontier of a search reads this view, so its identity and
--       evidence-label stage cannot name a body at all: the object it reads has
--       none, and SQLite's own authorizer sees no body column read.
--
-- The projection is written where the version is: `memory.create` and each
-- governance transition that copies content into a new exact version project the
-- assembly they insert, in the same settlement, exactly as a dependency set is
-- written with it. The INSERT guard admits a row only when it is the derivation of
-- its own assembly under the current projection version, so no writer can leave a
-- row that describes other text, other content or other rules. Existing assemblies
-- are backfilled here, before the guards exist (the 0015 precedent), so an
-- upgraded or restored workspace reaches the same rows a fresh one writes.
--
-- Bounds, not repairs. A field outside its profile is left out of the projection
-- rather than cut into a different value: a metadata string longer than its
-- contract bound, a kind, basis, topic key or identifier that is not a bounded
-- string, or a text containing NUL, is NULL (a title falls back to the record id,
-- a preview to the next of summary, what and learned). Content that is not a valid
-- JSON object has no row, so a search over it refuses with `projection_unavailable`
-- instead of guessing. Ordinary observations are validated on save and lose
-- nothing.
--
-- A search reads a row only for a version its evidence-label grant already
-- admitted, and treats a missing row as `projection_unavailable` and a row of
-- another projection version or another content digest as `stale_projection`.
-- It never falls back to the body.
--
-- No comment sits inside a statement below, so the migrator's statement splitter
-- and `executescript` store the same schema.

CREATE TABLE IF NOT EXISTS omnivia_engineering_preview_projection (
    workspace_id       TEXT    NOT NULL,
    assembly_id        TEXT    NOT NULL,
    projection_version INTEGER NOT NULL,
    content_digest     TEXT    NOT NULL,
    title              TEXT    NOT NULL,
    preview            TEXT    NOT NULL,
    truncated          INTEGER NOT NULL,
    observation_kind   TEXT,
    assertion_basis    TEXT,
    topic_key          TEXT,
    repository_id      TEXT,
    snapshot_id        TEXT,

    PRIMARY KEY (workspace_id, assembly_id, projection_version),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(assembly_id) = 'text' AND length(assembly_id) BETWEEN 1 AND 128
           AND assembly_id GLOB '[A-Za-z0-9]*'
           AND assembly_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(assembly_id, char(0)) = 0),
    CHECK (typeof(projection_version) = 'integer' AND projection_version > 0),
    CHECK (typeof(content_digest) = 'text' AND length(content_digest) = 71
           AND substr(content_digest, 1, 7) = 'sha256:'
           AND substr(content_digest, 8) NOT GLOB '*[^0-9a-f]*'
           AND instr(content_digest, char(0)) = 0),
    CHECK (typeof(title) = 'text' AND length(title) BETWEEN 1 AND 200
           AND instr(title, char(0)) = 0),
    CHECK (typeof(preview) = 'text' AND length(preview) BETWEEN 1 AND 480
           AND length(CAST(preview AS BLOB)) <= 2048
           AND instr(preview, char(0)) = 0),
    CHECK (typeof(truncated) = 'integer' AND truncated IN (0, 1)),
    CHECK (observation_kind IS NULL
           OR (typeof(observation_kind) = 'text' AND length(observation_kind) BETWEEN 1 AND 64
               AND instr(observation_kind, char(0)) = 0)),
    CHECK (assertion_basis IS NULL
           OR (typeof(assertion_basis) = 'text' AND length(assertion_basis) BETWEEN 1 AND 32
               AND instr(assertion_basis, char(0)) = 0)),
    CHECK (topic_key IS NULL
           OR (typeof(topic_key) = 'text' AND length(topic_key) BETWEEN 1 AND 256
               AND instr(topic_key, char(0)) = 0)),
    CHECK (repository_id IS NULL
           OR (typeof(repository_id) = 'text' AND length(repository_id) BETWEEN 1 AND 128
               AND repository_id GLOB '[A-Za-z0-9]*'
               AND repository_id NOT GLOB '*[^A-Za-z0-9._:-]*'
               AND instr(repository_id, char(0)) = 0)),
    CHECK (snapshot_id IS NULL
           OR (typeof(snapshot_id) = 'text' AND length(snapshot_id) BETWEEN 1 AND 128
               AND snapshot_id GLOB '[A-Za-z0-9]*'
               AND snapshot_id NOT GLOB '*[^A-Za-z0-9._:-]*'
               AND instr(snapshot_id, char(0)) = 0)),

    FOREIGN KEY (workspace_id, assembly_id)
        REFERENCES omnivia_governed_version_assemblies (workspace_id, assembly_id)
) WITHOUT ROWID;

CREATE VIEW IF NOT EXISTS omnivia_engineering_preview_source AS
SELECT
    b.workspace_id,
    b.assembly_id,
    1 AS projection_version,
    b.content_digest,
    substr(b.full_title, 1, 200) AS title,
    CASE WHEN b.full_body IS NULL THEN substr(b.full_title, 1, 200)
         ELSE substr(b.full_body, 1, 480) END AS preview,
    CASE WHEN length(b.full_title) > 200 OR length(b.full_body) > 480 THEN 1 ELSE 0 END AS truncated,
    b.observation_kind,
    b.assertion_basis,
    b.topic_key,
    b.repository_id,
    b.snapshot_id
FROM (
    SELECT
        r.workspace_id,
        r.assembly_id,
        r.content_digest,
        coalesce(CASE WHEN instr(r.title, char(0)) = 0 THEN nullif(r.title, '') END,
                 r.governed_record_id) AS full_title,
        coalesce(CASE WHEN instr(r.summary, char(0)) = 0 THEN nullif(r.summary, '') END,
                 CASE WHEN instr(r.what, char(0)) = 0 THEN nullif(r.what, '') END,
                 CASE WHEN instr(r.learned, char(0)) = 0 THEN nullif(r.learned, '') END) AS full_body,
        CASE WHEN length(r.kind) BETWEEN 1 AND 64 AND instr(r.kind, char(0)) = 0
             THEN r.kind END AS observation_kind,
        CASE WHEN length(r.basis) BETWEEN 1 AND 32 AND instr(r.basis, char(0)) = 0
             THEN r.basis END AS assertion_basis,
        CASE WHEN length(r.topic) BETWEEN 1 AND 256 AND instr(r.topic, char(0)) = 0
             THEN r.topic END AS topic_key,
        CASE WHEN length(r.repository) BETWEEN 1 AND 128
                  AND r.repository GLOB '[A-Za-z0-9]*'
                  AND r.repository NOT GLOB '*[^A-Za-z0-9._:-]*'
                  AND instr(r.repository, char(0)) = 0
             THEN r.repository END AS repository_id,
        CASE WHEN length(r.snapshot) BETWEEN 1 AND 128
                  AND r.snapshot GLOB '[A-Za-z0-9]*'
                  AND r.snapshot NOT GLOB '*[^A-Za-z0-9._:-]*'
                  AND instr(r.snapshot, char(0)) = 0
             THEN r.snapshot END AS snapshot_id
    FROM (
        SELECT
            a.workspace_id,
            a.assembly_id,
            a.governed_record_id,
            a.content_digest,
            CASE WHEN json_type(a.content_json, '$.title') = 'text'
                 THEN json_extract(a.content_json, '$.title') END AS title,
            CASE WHEN json_type(a.content_json, '$.summary') = 'text'
                 THEN json_extract(a.content_json, '$.summary') END AS summary,
            CASE WHEN json_type(a.content_json, '$.what') = 'text'
                 THEN json_extract(a.content_json, '$.what') END AS what,
            CASE WHEN json_type(a.content_json, '$.learned') = 'text'
                 THEN json_extract(a.content_json, '$.learned') END AS learned,
            CASE WHEN json_type(a.content_json, '$.kind') = 'text'
                 THEN json_extract(a.content_json, '$.kind') END AS kind,
            CASE WHEN json_type(a.content_json, '$.assertion_basis') = 'text'
                 THEN json_extract(a.content_json, '$.assertion_basis') END AS basis,
            CASE WHEN json_type(a.content_json, '$.topic_ref.proposed_key') = 'text'
                 THEN json_extract(a.content_json, '$.topic_ref.proposed_key') END AS topic,
            CASE WHEN json_type(a.content_json, '$.applicability.repository_id') = 'text'
                 THEN json_extract(a.content_json, '$.applicability.repository_id') END AS repository,
            CASE WHEN json_type(a.content_json, '$.applicability.snapshot_id') = 'text'
                 THEN json_extract(a.content_json, '$.applicability.snapshot_id') END AS snapshot
        FROM omnivia_governed_version_assemblies a
        WHERE a.domain_scope = 'engineering.codebase'
          AND CASE WHEN json_valid(a.content_json)
                   THEN json_type(a.content_json) = 'object' ELSE 0 END
    ) r
) b;

CREATE VIEW IF NOT EXISTS omnivia_authoritative_governed_version_metadata AS
SELECT
    a.workspace_id,
    a.assembly_id,
    s.seal_id,
    a.governed_record_id,
    a.governed_record_version_id,
    a.record_type,
    a.domain_scope,
    a.layer,
    a.governance_disposition,
    a.authority_level,
    a.decision_source_kind,
    a.decision_source_id,
    a.content_schema_version,
    a.content_digest,
    a.evidence_disposition,
    a.valid_from_us,
    a.valid_to_us,
    a.recorded_at_us,
    a.append_ordinal,
    a.correlation_kind,
    a.correlation_id,
    a.audit_ref,
    s.sealed_at_us
FROM omnivia_governed_version_assemblies a
JOIN omnivia_governed_version_seals s
  ON s.workspace_id = a.workspace_id
 AND s.assembly_id = a.assembly_id
 AND s.governed_record_version_id = a.governed_record_version_id;

INSERT INTO omnivia_engineering_preview_projection
    (workspace_id, assembly_id, projection_version, content_digest, title, preview,
     truncated, observation_kind, assertion_basis, topic_key, repository_id, snapshot_id)
SELECT workspace_id, assembly_id, projection_version, content_digest, title, preview,
       truncated, observation_kind, assertion_basis, topic_key, repository_id, snapshot_id
FROM omnivia_engineering_preview_source;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_omnivia_engineering_preview_projection_insert
BEFORE INSERT ON omnivia_engineering_preview_projection
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_engineering_preview_projection')
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
    SELECT RAISE(ABORT, 'omnivia: a preview projection row is the derivation of its own assembly under the current projection version, and nothing else')
    WHERE NOT EXISTS (
            SELECT 1 FROM omnivia_engineering_preview_source s
            WHERE s.workspace_id = NEW.workspace_id
              AND s.assembly_id = NEW.assembly_id
              AND s.projection_version IS NEW.projection_version
              AND s.content_digest IS NEW.content_digest
              AND s.title IS NEW.title
              AND s.preview IS NEW.preview
              AND s.truncated IS NEW.truncated
              AND s.observation_kind IS NEW.observation_kind
              AND s.assertion_basis IS NEW.assertion_basis
              AND s.topic_key IS NEW.topic_key
              AND s.repository_id IS NEW.repository_id
              AND s.snapshot_id IS NEW.snapshot_id);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_omnivia_engineering_preview_projection_update
BEFORE UPDATE ON omnivia_engineering_preview_projection
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_engineering_preview_projection is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_omnivia_engineering_preview_projection_delete
BEFORE DELETE ON omnivia_engineering_preview_projection
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_engineering_preview_projection is append-only; DELETE is never permitted');
END;
