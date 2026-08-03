-- Governed record identity, version assemblies and relations (V06-1 M3).
--
-- The third forward-only durable slice for the application seam. Like `0007` and
-- `0008` before it, this file adds storage facts and nothing that acts on them:
-- no repository, unit of work, canonical resolver, application operation, policy
-- evaluator, SemanticIndex, job, projection, legacy import or physical deletion.
-- What a later accepted vertical does with these rows is separately accepted work.
--
--   omnivia_governed_schema_catalogue        the 13 frozen (type, 1.0, member) rows
--   omnivia_governed_records                 one stable governed identity
--   omnivia_governed_version_assemblies      one immutable version of one record
--   omnivia_governed_extraction_lineage      what extracted a candidate, exactly
--   omnivia_governed_legacy_lineage          what migrated a legacy claim
--   omnivia_governed_provenance_events       who did what to one version, in order
--   omnivia_governed_version_evidence_links  the exact M2 ancestry of one event
--   omnivia_governed_relation_endpoints      one relation's exact two endpoints
--   omnivia_record_supersessions             one directed, acyclic replacement edge
--   omnivia_governed_version_seals           the last step; only sealed rows count
--
-- The shape of the slice is one idea: a governed record has a stable identity that
-- never changes, and everything else about it is an append-only *version* that is
-- assembled in pieces and then sealed. Nothing here carries a currentness flag, a
-- canonical pointer, a `is_current` boolean or a mutable truth column, because
-- every one of those is a value something would later have to overwrite -- and
-- `0007` and `0008` both refuse UPDATE unconditionally for exactly that reason.
-- "Which version is current" is a question a later, separately accepted resolver
-- answers by reading these facts, not a column this file lets anybody set.
--
-- Assembly then seal is what makes a partial write harmless. The components -- the
-- provenance events, the lineage, the evidence links, the endpoints, the
-- supersession edge -- are inserted first and prove nothing on their own. The seal
-- is the last statement, it is the only thing the authoritative view joins to, and
-- its BEFORE INSERT trigger is where every cross-row invariant in this slice is
-- actually enforced. A committed but unsealed assembly is inert: it is not in the
-- view, it is not ancestry anything else may name, and it is deliberately *not*
-- claimed to be an ordinary SQLite integrity failure, because it is not one. It is
-- a row that never earned authority, and this file does not pretend a `PRAGMA
-- integrity_check` can tell the difference.
--
-- Immutability is enforced exactly as the two accepted predecessors established.
-- Every UPDATE and DELETE aborts unconditionally -- for the current fenced owner
-- too -- and every INSERT carries the complete connection-authority, mutation
-- guard, workspace-state and lease predicate plus the singleton workspace binding.
-- A correction is a new version with a `governance.corrected` event and a
-- supersession edge, never a rewrite.
--
-- The one deliberate deviation from `0008`'s trigger shape is the absence of a
-- `WHEN` clause. `0008` states each INSERT predicate twice -- once in `WHEN` as a
-- gate and once in the body as the diagnosable refusal -- which is affordable at
-- two or three conditions. The seal trigger below carries more than twenty, and a
-- duplicated form of it would be several hundred lines whose two halves a reviewer
-- would have to diff by eye to know they still agree; `0008` needed a dedicated
-- test (M2-18b) to police that duplication for a single reopened trigger. Omitting
-- `WHEN` changes no behaviour -- the body's guarded `RAISE ... WHERE` statements
-- are the whole rule either way -- and leaves one authoritative copy of each
-- predicate. The authority clause is still first in every body, so a writer with no
-- authority hears about authority rather than about an invariant it was never
-- allowed to reach.
--
-- Identifiers, open codes, digests and times keep the exact domains `0008` froze:
-- `Identifier` is `^[A-Za-z0-9][A-Za-z0-9._:-]*$` at 1..128; `OpenCode` is
-- lowercase dot-namespaced at 1..128; an internal digest is `sha256:` plus 64
-- lowercase hex; and every comparison-bearing time is a signed 64-bit integer of
-- UTC microseconds, proved integer by `typeof(...) = 'integer'` so a fractional
-- microsecond is refused rather than silently rounded by column affinity. Times the
-- system itself stamps are positive; `valid_from_us` carries the full signed domain
-- because a fact may be valid from before 1970.
--
-- Canonical content is stored as exact Core RFC 8785 (ADR-039 I-JSON) UTF-8 text
-- beside a `sha256:` digest of those bytes, bounded at 2..1,048,576 *bytes*. The
-- bound is written `length(CAST(x AS BLOB))` because SQLite's `length()` on text
-- counts characters, and a character bound is not the bound the accepted contract
-- states. As in `0008`, SQLite's own `json(...)` is never called and no check
-- recomputes a digest: canonicalisation and digest equality happen above this layer,
-- and a SQL check that appeared to confirm either would assert something this layer
-- cannot know. The one honest claim is that a value which is not a well-formed
-- digest cannot be stored as one.
--
-- `omnivia_governed_schema_catalogue` is the single exception to "everything here is
-- workspace data". It is an immutable structural catalogue of the 13 accepted
-- `(record_type, 1.0, primary_member)` pairs, seeded by this migration and then
-- closed: its three guards refuse INSERT, UPDATE and DELETE unconditionally, which
-- also refuses `INSERT OR REPLACE`, since REPLACE resolves to a DELETE plus an
-- INSERT and meets both. The guards deliberately do *not* call the connection
-- authority function, because there is no authority under which a mutation is
-- correct and a predicate would imply there might be. The statement order is fixed
-- and load-bearing: create the table, seed it, and only then close it.
--
-- The catalogue records which member of a record type's content carries its primary
-- text, and SQL does not parse `content_json` to prove the member is present. That
-- stays a consuming-contract obligation, together with the reserved-member rules,
-- I-JSON admission, canonicalisation, digest equality and the 1..262,144-byte
-- primary-member bound. This file claims the pair exists and is one of thirteen.
--
-- Every foreign key is left at SQLite's default immediate enforcement, for the
-- reason `0007` records at length: `DEFERRABLE INITIALLY DEFERRED` moves the check
-- to COMMIT, and SQLite leaves the transaction *open* when COMMIT fails, which
-- would strand the service's single authoritative write connection. The graph below
-- is acyclic -- catalogue and record, then assembly, then that assembly's lineage,
-- events, links, endpoints and edges, then the seal -- so nothing needs a
-- commit-time cycle, and no second authoritative connection is introduced.
--
-- The composite keys are what keep identity honest, and they are wider here than in
-- `0008` on purpose. An assembly names its stable record *and* repeats that record's
-- type and scope, so a version cannot claim a type its record never had. Every
-- component names its assembly *and* repeats the assembly's exact correlation tuple,
-- so a fact assembled under one audit cannot be smuggled into a version assembled
-- under another. A provenance event repeats the version id as well. Each of those is
-- a foreign key over the whole tuple rather than a check somebody has to remember to
-- write, which is the same rule the accepted contract applies on the wire.
--
-- Indexes are named. SQLite would supply an implicit `sqlite_autoindex_*` for the
-- primary keys, but the canonical schema fingerprint filters those out, so a
-- constraint that exists only implicitly is a constraint drift detection cannot see.
--
-- What this file does *not* do is stated plainly. There is no resolver, so nothing
-- here decides which accepted version is canonical. There is no policy evaluator, so
-- `policy_id` and `authority_policy_id` are recorded identities and not evaluated
-- decisions. There is no ACL, so nothing here decides who may read a version. There
-- is no public DTO: `omnivia_authoritative_governed_versions` is an internal storage
-- projection over sealed rows, carrying no `l1`/`l2`/`l3` wire layer, no derived
-- currentness, no supersession projection and no `(V, T)` defaulting. And there is
-- no import: `migrated_legacy_claim` is a shape a row may declare, not a mechanism
-- this slice provides.

-- Every comment in this file sits *between* statements, never inside one. SQLite
-- stores a statement's original text in `sqlite_master`, and the migrator executes
-- statements one at a time with comments stripped so that a migration stays inside
-- the caller's transaction -- so a comment inside a CREATE would make the applied
-- schema differ, byte for byte, from the canonical fingerprint built by replaying
-- the same artifacts with `executescript`. Readiness compares exactly those two, and
-- a clean workspace would be reported as drifted.


-- The frozen structural catalogue. Thirteen rows, one schema version, created here
-- and never written again.
--
-- Not workspace data: it carries no `workspace_id`, describes no fact anybody
-- asserted, and is the same in every workspace because it is part of the schema
-- rather than part of the contents. It exists so that "this version claims a type
-- and a content schema" is a foreign key into an accepted pair rather than a string
-- somebody typed, and so the primary member of each type is written down once in the
-- database instead of only in a document.
CREATE TABLE IF NOT EXISTS omnivia_governed_schema_catalogue (
    record_type            TEXT NOT NULL,
    content_schema_version TEXT NOT NULL,
    primary_member         TEXT NOT NULL,

    PRIMARY KEY (record_type, content_schema_version),

    CHECK (
        typeof(record_type) = 'text'
        AND length(record_type) BETWEEN 1 AND 128
        AND record_type GLOB '[a-z]*'
        AND record_type NOT GLOB '*[^a-z0-9_.]*'
        AND record_type NOT GLOB '*.'
        AND record_type NOT GLOB '*.[^a-z]*'
        AND instr(record_type, char(0)) = 0
    ),
    CHECK (
        typeof(content_schema_version) = 'text'
        AND length(content_schema_version) BETWEEN 1 AND 128
        AND content_schema_version GLOB '[A-Za-z0-9]*'
        AND content_schema_version NOT GLOB '*[^A-Za-z0-9._:-]*'
        AND instr(content_schema_version, char(0)) = 0
    ),
    CHECK (
        typeof(primary_member) = 'text'
        AND length(primary_member) BETWEEN 1 AND 128
        AND primary_member GLOB '[a-z]*'
        AND primary_member NOT GLOB '*[^a-z0-9_]*'
        AND instr(primary_member, char(0)) = 0
    )
) WITHOUT ROWID;

-- The thirteen accepted pairs, seeded as one statement so an interruption can leave
-- the catalogue whole or absent and never partial. This runs before the guards below
-- exist; that ordering is the only reason it can run at all.
INSERT INTO omnivia_governed_schema_catalogue
    (record_type, content_schema_version, primary_member)
VALUES
    ('knowledge.claim', '1.0', 'statement'),
    ('knowledge.decision', '1.0', 'decision'),
    ('knowledge.proposal', '1.0', 'proposal'),
    ('knowledge.requirement', '1.0', 'requirement'),
    ('knowledge.constraint', '1.0', 'constraint'),
    ('knowledge.assumption', '1.0', 'assumption'),
    ('knowledge.preference', '1.0', 'preference'),
    ('knowledge.question', '1.0', 'question'),
    ('knowledge.finding', '1.0', 'finding'),
    ('knowledge.risk', '1.0', 'risk'),
    ('knowledge.outcome', '1.0', 'outcome'),
    ('knowledge.entity', '1.0', 'name'),
    ('knowledge.relation', '1.0', 'statement');

-- The catalogue is now closed, permanently and without condition.
--
-- No authority predicate: a mutation here is wrong under every authority, and
-- writing a predicate would suggest there is one that permits it. Refusing all three
-- statement classes is also what refuses `INSERT OR REPLACE` and an upsert, since
-- REPLACE resolves to DELETE plus INSERT and meets both guards.
CREATE TRIGGER IF NOT EXISTS omnivia_guard_governed_schema_catalogue_insert
BEFORE INSERT ON omnivia_governed_schema_catalogue
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_governed_schema_catalogue is a frozen structural catalogue; INSERT is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_governed_schema_catalogue_update
BEFORE UPDATE ON omnivia_governed_schema_catalogue
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_governed_schema_catalogue is a frozen structural catalogue; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_governed_schema_catalogue_delete
BEFORE DELETE ON omnivia_governed_schema_catalogue
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_governed_schema_catalogue is a frozen structural catalogue; DELETE is never permitted');
END;

-- One stable governed identity, and only that.
--
-- This row is what survives every version, correction and supersession: the identity
-- a reader cites, the type it will always be and the domain scope it was created in.
-- It deliberately owns no content, no pointer to a "current" version, no authority
-- level and no truth flag, because each of those changes over time and this row does
-- not. Promotion from candidate to governed appends a new assembly against the same
-- `governed_record_id`; it does not touch this table at all.
CREATE TABLE IF NOT EXISTS omnivia_governed_records (
    workspace_id       TEXT    NOT NULL,
    governed_record_id TEXT    NOT NULL,
    record_type        TEXT    NOT NULL,
    domain_scope       TEXT    NOT NULL,
    recorded_at_us     INTEGER NOT NULL,

    PRIMARY KEY (workspace_id, governed_record_id),

    CHECK (
        typeof(workspace_id) = 'text'
        AND length(workspace_id) BETWEEN 1 AND 128
        AND workspace_id GLOB '[A-Za-z0-9]*'
        AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
        AND instr(workspace_id, char(0)) = 0
    ),
    CHECK (
        typeof(governed_record_id) = 'text'
        AND length(governed_record_id) BETWEEN 1 AND 128
        AND governed_record_id GLOB '[A-Za-z0-9]*'
        AND governed_record_id NOT GLOB '*[^A-Za-z0-9._:-]*'
        AND instr(governed_record_id, char(0)) = 0
    ),
    CHECK (
        typeof(record_type) = 'text'
        AND length(record_type) BETWEEN 1 AND 128
        AND record_type GLOB '[a-z]*'
        AND record_type NOT GLOB '*[^a-z0-9_.]*'
        AND record_type NOT GLOB '*.'
        AND record_type NOT GLOB '*.[^a-z]*'
        AND instr(record_type, char(0)) = 0
    ),
    CHECK (
        typeof(domain_scope) = 'text'
        AND length(domain_scope) BETWEEN 1 AND 128
        AND domain_scope GLOB '[a-z]*'
        AND domain_scope NOT GLOB '*[^a-z0-9_.]*'
        AND domain_scope NOT GLOB '*.'
        AND domain_scope NOT GLOB '*.[^a-z]*'
        AND instr(domain_scope, char(0)) = 0
    ),
    CHECK (typeof(recorded_at_us) = 'integer' AND recorded_at_us > 0)
) WITHOUT ROWID;

-- One immutable version of one stable record: the whole of what was asserted, who
-- asserted it, under what authority, and what correlated the act.
--
-- Type and scope are repeated from the stable record and bound back to it by a
-- four-column foreign key, so a version cannot claim a type or a scope its own
-- record never had, and cannot reach a record in another workspace.
--
-- The layer decides which half of the table applies, and the CHECKs make the two
-- halves exclusive rather than merely conventional. A candidate carries an origin
-- and no decision source at all -- it cannot name a reviewer or a policy, because
-- there are no columns it may put them in without failing. A governed version
-- carries a disposition, the exact reviewer principal or policy id in
-- `decision_source_id`, and the authority rule and version that permitted its level.
-- `context_model` is recognised as a durable layer and is refused a seal, so it can
-- be recorded and can never become authoritative under M3.
--
-- `authority_rank` is a virtual generated column, not a stored number. The ranks
-- 0/100/200 are fixed semantics of the three levels, and a caller that could write
-- the rank could assert a precedence its level does not carry; generated means there
-- is no value to supply and none to disagree with.
--
-- The correlation tuple is the version's one parent, and which parent is legal
-- depends on where the version came from: any governed fact and any human proposal
-- correlate to an exact M1 audit event, an internal extraction to its run, and a
-- migrated legacy claim to its migration run. The two internal lanes are forbidden
-- an `audit_ref` outright, which is what stops an internal pipeline from fabricating
-- application-audit identity it never had.
CREATE TABLE IF NOT EXISTS omnivia_governed_version_assemblies (
    workspace_id               TEXT    NOT NULL,
    assembly_id                TEXT    NOT NULL,
    governed_record_id         TEXT    NOT NULL,
    governed_record_version_id TEXT    NOT NULL,
    record_type                TEXT    NOT NULL,
    domain_scope               TEXT    NOT NULL,
    layer                      TEXT    NOT NULL,
    authority_level            TEXT    NOT NULL,
    authority_rank             INTEGER GENERATED ALWAYS AS (
        CASE authority_level
            WHEN 'proposed' THEN 0
            WHEN 'reviewed' THEN 100
            WHEN 'canonical' THEN 200
        END
    ) VIRTUAL,
    governance_disposition     TEXT,
    candidate_origin           TEXT,
    extraction_kind            TEXT,
    decision_source_kind       TEXT,
    decision_source_id         TEXT,
    authority_policy_id        TEXT,
    authority_policy_version   TEXT,
    policy_decision_ref        TEXT,
    content_schema_version     TEXT    NOT NULL,
    content_json               TEXT    NOT NULL,
    content_digest             TEXT    NOT NULL,
    evidence_disposition       TEXT    NOT NULL,
    confidence_ppm             INTEGER,
    assertion_actor_id         TEXT    NOT NULL,
    assertion_actor_kind       TEXT    NOT NULL,
    assertion_actor_role       TEXT    NOT NULL,
    reason_code                TEXT,
    reason_comment             TEXT,
    valid_from_us              INTEGER NOT NULL,
    valid_to_us                INTEGER,
    recorded_at_us             INTEGER NOT NULL,
    append_ordinal             INTEGER NOT NULL,
    correlation_kind           TEXT    NOT NULL,
    correlation_id             TEXT    NOT NULL,
    audit_ref                  TEXT,

    PRIMARY KEY (workspace_id, assembly_id),

    CHECK (
        typeof(workspace_id) = 'text'
        AND length(workspace_id) BETWEEN 1 AND 128
        AND workspace_id GLOB '[A-Za-z0-9]*'
        AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
        AND instr(workspace_id, char(0)) = 0
    ),
    CHECK (
        typeof(assembly_id) = 'text'
        AND length(assembly_id) BETWEEN 1 AND 128
        AND assembly_id GLOB '[A-Za-z0-9]*'
        AND assembly_id NOT GLOB '*[^A-Za-z0-9._:-]*'
        AND instr(assembly_id, char(0)) = 0
    ),
    CHECK (
        typeof(governed_record_id) = 'text'
        AND length(governed_record_id) BETWEEN 1 AND 128
        AND governed_record_id GLOB '[A-Za-z0-9]*'
        AND governed_record_id NOT GLOB '*[^A-Za-z0-9._:-]*'
        AND instr(governed_record_id, char(0)) = 0
    ),
    CHECK (
        typeof(governed_record_version_id) = 'text'
        AND length(governed_record_version_id) BETWEEN 1 AND 128
        AND governed_record_version_id GLOB '[A-Za-z0-9]*'
        AND governed_record_version_id NOT GLOB '*[^A-Za-z0-9._:-]*'
        AND instr(governed_record_version_id, char(0)) = 0
    ),
    CHECK (
        typeof(record_type) = 'text'
        AND length(record_type) BETWEEN 1 AND 128
        AND record_type GLOB '[a-z]*'
        AND record_type NOT GLOB '*[^a-z0-9_.]*'
        AND record_type NOT GLOB '*.'
        AND record_type NOT GLOB '*.[^a-z]*'
        AND instr(record_type, char(0)) = 0
    ),
    CHECK (
        typeof(domain_scope) = 'text'
        AND length(domain_scope) BETWEEN 1 AND 128
        AND domain_scope GLOB '[a-z]*'
        AND domain_scope NOT GLOB '*[^a-z0-9_.]*'
        AND domain_scope NOT GLOB '*.'
        AND domain_scope NOT GLOB '*.[^a-z]*'
        AND instr(domain_scope, char(0)) = 0
    ),
    CHECK (
        typeof(content_schema_version) = 'text'
        AND length(content_schema_version) BETWEEN 1 AND 128
        AND content_schema_version GLOB '[A-Za-z0-9]*'
        AND content_schema_version NOT GLOB '*[^A-Za-z0-9._:-]*'
        AND instr(content_schema_version, char(0)) = 0
    ),
    CHECK (
        typeof(layer) = 'text'
        AND layer IN ('candidate', 'governed', 'context_model')
        AND instr(layer, char(0)) = 0
    ),
    CHECK (
        typeof(authority_level) = 'text'
        AND authority_level IN ('proposed', 'reviewed', 'canonical')
        AND instr(authority_level, char(0)) = 0
    ),
    CHECK (
        governance_disposition IS NULL
        OR (
            typeof(governance_disposition) = 'text'
            AND governance_disposition IN (
                'accepted', 'rejected', 'contested', 'withdrawn', 'superseded'
            )
            AND instr(governance_disposition, char(0)) = 0
        )
    ),
    CHECK (
        candidate_origin IS NULL
        OR (
            typeof(candidate_origin) = 'text'
            AND candidate_origin IN (
                'extracted', 'human_proposed', 'migrated_legacy_claim'
            )
            AND instr(candidate_origin, char(0)) = 0
        )
    ),
    CHECK (
        extraction_kind IS NULL
        OR (
            typeof(extraction_kind) = 'text'
            AND extraction_kind IN (
                'deterministic', 'model_free_nondeterministic', 'model_backed'
            )
            AND instr(extraction_kind, char(0)) = 0
        )
    ),
    CHECK (
        decision_source_kind IS NULL
        OR (
            typeof(decision_source_kind) = 'text'
            AND decision_source_kind IN ('human_reviewer', 'policy_evaluator')
            AND instr(decision_source_kind, char(0)) = 0
        )
    ),
    CHECK (
        typeof(evidence_disposition) = 'text'
        AND evidence_disposition IN ('available', 'unavailable', 'redacted')
        AND instr(evidence_disposition, char(0)) = 0
    ),
    CHECK (
        decision_source_id IS NULL
        OR (
            typeof(decision_source_id) = 'text'
            AND length(decision_source_id) BETWEEN 1 AND 128
            AND decision_source_id GLOB '[A-Za-z0-9]*'
            AND decision_source_id NOT GLOB '*[^A-Za-z0-9._:-]*'
            AND instr(decision_source_id, char(0)) = 0
        )
    ),
    CHECK (
        authority_policy_id IS NULL
        OR (
            typeof(authority_policy_id) = 'text'
            AND length(authority_policy_id) BETWEEN 1 AND 128
            AND authority_policy_id GLOB '[A-Za-z0-9]*'
            AND authority_policy_id NOT GLOB '*[^A-Za-z0-9._:-]*'
            AND instr(authority_policy_id, char(0)) = 0
        )
    ),
    CHECK (
        authority_policy_version IS NULL
        OR (
            typeof(authority_policy_version) = 'text'
            AND length(authority_policy_version) BETWEEN 1 AND 128
            AND authority_policy_version GLOB '[A-Za-z0-9]*'
            AND authority_policy_version NOT GLOB '*[^A-Za-z0-9._:-]*'
            AND instr(authority_policy_version, char(0)) = 0
        )
    ),
    CHECK (
        policy_decision_ref IS NULL
        OR (
            typeof(policy_decision_ref) = 'text'
            AND length(policy_decision_ref) BETWEEN 1 AND 512
            AND policy_decision_ref NOT GLOB '*[^!-~]*'
            AND instr(policy_decision_ref, char(0)) = 0
        )
    ),
    CHECK (
        typeof(assertion_actor_id) = 'text'
        AND length(assertion_actor_id) BETWEEN 1 AND 128
        AND assertion_actor_id GLOB '[A-Za-z0-9]*'
        AND assertion_actor_id NOT GLOB '*[^A-Za-z0-9._:-]*'
        AND instr(assertion_actor_id, char(0)) = 0
    ),
    CHECK (
        typeof(assertion_actor_kind) = 'text'
        AND length(assertion_actor_kind) BETWEEN 1 AND 128
        AND assertion_actor_kind GLOB '[a-z]*'
        AND assertion_actor_kind NOT GLOB '*[^a-z0-9_.]*'
        AND assertion_actor_kind NOT GLOB '*.'
        AND assertion_actor_kind NOT GLOB '*.[^a-z]*'
        AND instr(assertion_actor_kind, char(0)) = 0
    ),
    CHECK (
        typeof(assertion_actor_role) = 'text'
        AND length(assertion_actor_role) BETWEEN 1 AND 128
        AND assertion_actor_role GLOB '[a-z]*'
        AND assertion_actor_role NOT GLOB '*[^a-z0-9_.]*'
        AND assertion_actor_role NOT GLOB '*.'
        AND assertion_actor_role NOT GLOB '*.[^a-z]*'
        AND instr(assertion_actor_role, char(0)) = 0
    ),
    CHECK (
        reason_code IS NULL
        OR (
            typeof(reason_code) = 'text'
            AND length(reason_code) BETWEEN 1 AND 128
            AND reason_code GLOB '[a-z]*'
            AND reason_code NOT GLOB '*[^a-z0-9_.]*'
            AND reason_code NOT GLOB '*.'
            AND reason_code NOT GLOB '*.[^a-z]*'
            AND instr(reason_code, char(0)) = 0
        )
    ),
    CHECK (
        reason_comment IS NULL
        OR (
            typeof(reason_comment) = 'text'
            AND length(reason_comment) BETWEEN 1 AND 2048
            AND instr(reason_comment, char(0)) = 0
        )
    ),
    CHECK (
        typeof(content_json) = 'text'
        AND length(CAST(content_json AS BLOB)) BETWEEN 2 AND 1048576
        AND instr(content_json, char(0)) = 0
    ),
    CHECK (
        typeof(content_digest) = 'text'
        AND length(content_digest) = 71
        AND substr(content_digest, 1, 7) = 'sha256:'
        AND substr(content_digest, 8) NOT GLOB '*[^0-9a-f]*'
        AND instr(content_digest, char(0)) = 0
    ),
    CHECK (
        confidence_ppm IS NULL
        OR (
            typeof(confidence_ppm) = 'integer'
            AND confidence_ppm BETWEEN 0 AND 1000000
        )
    ),
    CHECK (typeof(valid_from_us) = 'integer'),
    CHECK (valid_to_us IS NULL OR typeof(valid_to_us) = 'integer'),
    CHECK (valid_to_us IS NULL OR valid_to_us > valid_from_us),
    CHECK (typeof(recorded_at_us) = 'integer' AND recorded_at_us > 0),
    CHECK (typeof(append_ordinal) = 'integer' AND append_ordinal > 0),
    CHECK (
        typeof(correlation_kind) = 'text'
        AND correlation_kind IN ('m1_audit', 'extraction_run', 'migration_run')
        AND instr(correlation_kind, char(0)) = 0
    ),
    CHECK (
        typeof(correlation_id) = 'text'
        AND length(correlation_id) BETWEEN 1 AND 128
        AND correlation_id GLOB '[A-Za-z0-9]*'
        AND correlation_id NOT GLOB '*[^A-Za-z0-9._:-]*'
        AND instr(correlation_id, char(0)) = 0
    ),
    CHECK (
        audit_ref IS NULL
        OR (
            typeof(audit_ref) = 'text'
            AND length(audit_ref) BETWEEN 1 AND 128
            AND audit_ref GLOB '[A-Za-z0-9]*'
            AND audit_ref NOT GLOB '*[^A-Za-z0-9._:-]*'
            AND instr(audit_ref, char(0)) = 0
        )
    ),
    CHECK (
        (layer = 'governed'
         AND governance_disposition IS NOT NULL
         AND candidate_origin IS NULL
         AND extraction_kind IS NULL
         AND decision_source_kind IS NOT NULL
         AND decision_source_id IS NOT NULL
         AND authority_policy_id IS NOT NULL
         AND authority_policy_version IS NOT NULL)
        OR (layer <> 'governed'
            AND governance_disposition IS NULL
            AND candidate_origin IS NOT NULL
            AND decision_source_kind IS NULL
            AND decision_source_id IS NULL
            AND authority_policy_id IS NULL
            AND authority_policy_version IS NULL
            AND policy_decision_ref IS NULL)
    ),
    CHECK (
        (candidate_origin = 'extracted' AND extraction_kind IS NOT NULL)
        OR (candidate_origin IS NOT NULL AND candidate_origin <> 'extracted'
            AND extraction_kind IS NULL)
        OR candidate_origin IS NULL
    ),
    CHECK (
        (layer = 'governed' AND authority_level IN ('reviewed', 'canonical'))
        OR (layer <> 'governed' AND authority_level = 'proposed')
    ),
    CHECK (
        authority_level <> 'canonical'
        OR (layer = 'governed' AND governance_disposition = 'accepted')
    ),
    CHECK (
        evidence_disposition = 'available' OR reason_code IS NOT NULL
    ),
    CHECK (
        (layer = 'governed'
         AND correlation_kind = 'm1_audit'
         AND audit_ref IS NOT NULL
         AND correlation_id = audit_ref)
        OR (candidate_origin = 'human_proposed'
            AND correlation_kind = 'm1_audit'
            AND audit_ref IS NOT NULL
            AND correlation_id = audit_ref)
        OR (candidate_origin = 'extracted'
            AND correlation_kind = 'extraction_run'
            AND audit_ref IS NULL)
        OR (candidate_origin = 'migrated_legacy_claim'
            AND correlation_kind = 'migration_run'
            AND audit_ref IS NULL)
    ),

    FOREIGN KEY (workspace_id, governed_record_id, record_type, domain_scope)
        REFERENCES omnivia_governed_records
            (workspace_id, governed_record_id, record_type, domain_scope),
    FOREIGN KEY (record_type, content_schema_version)
        REFERENCES omnivia_governed_schema_catalogue
            (record_type, content_schema_version),
    FOREIGN KEY (audit_ref, workspace_id)
        REFERENCES omnivia_application_audit_events (audit_ref, workspace_id)
) WITHOUT ROWID;

-- What extracted one candidate, in enough detail to reproduce or indict it.
--
-- Exists exactly once for an extracted candidate and never for any other origin;
-- both halves of that are seal-time rules, because "there is no lineage row" is not
-- something a foreign key can refuse. It names the exact `candidate.extracted` event
-- it explains, so lineage and provenance cannot drift apart into two stories.
--
-- The conditional matrix is the point of the table. A deterministic extraction must
-- name its algorithm and the complete configuration that produced the output, and is
-- forbidden randomness, a model and a prompt, because a deterministic claim that
-- carries any of those is not deterministic. A model-free nondeterministic
-- extraction is the same minus the determinism, and may record the seed identity
-- when randomness actually affected the output. A model-backed extraction must name
-- the model, its version and the inference configuration; the prompt or template is
-- all-or-nothing, so an extraction cannot claim a template it cannot identify or
-- identify one it cannot digest. Every one of these is written as a CHECK rather
-- than left to the writer, and each missing or forbidden field refuses the row here
-- and therefore refuses the seal.
CREATE TABLE IF NOT EXISTS omnivia_governed_extraction_lineage (
    workspace_id                   TEXT    NOT NULL,
    assembly_id                    TEXT    NOT NULL,
    provenance_event_id            TEXT    NOT NULL,
    correlation_kind               TEXT    NOT NULL,
    correlation_id                 TEXT    NOT NULL,
    extraction_kind                TEXT    NOT NULL,
    extraction_run_id              TEXT    NOT NULL,
    extractor_id                   TEXT    NOT NULL,
    extractor_version              TEXT    NOT NULL,
    pipeline_version               TEXT    NOT NULL,
    configuration_version          TEXT    NOT NULL,
    configuration_digest           TEXT,
    algorithm_id                   TEXT,
    algorithm_version              TEXT,
    randomness_seed                TEXT,
    model_id                       TEXT,
    model_version                  TEXT,
    inference_configuration_digest TEXT,
    prompt_template_id             TEXT,
    prompt_template_version        TEXT,
    prompt_template_digest         TEXT,
    actor_id                       TEXT    NOT NULL,
    actor_kind                     TEXT    NOT NULL,
    actor_role                     TEXT    NOT NULL,
    evidence_disposition           TEXT    NOT NULL,
    occurred_at_us                 INTEGER NOT NULL,
    recorded_at_us                 INTEGER NOT NULL,
    append_ordinal                 INTEGER NOT NULL,

    PRIMARY KEY (workspace_id, assembly_id),

    CHECK (
        typeof(workspace_id) = 'text'
        AND length(workspace_id) BETWEEN 1 AND 128
        AND workspace_id GLOB '[A-Za-z0-9]*'
        AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
        AND instr(workspace_id, char(0)) = 0
    ),
    CHECK (
        typeof(assembly_id) = 'text'
        AND length(assembly_id) BETWEEN 1 AND 128
        AND assembly_id GLOB '[A-Za-z0-9]*'
        AND assembly_id NOT GLOB '*[^A-Za-z0-9._:-]*'
        AND instr(assembly_id, char(0)) = 0
    ),
    CHECK (
        typeof(provenance_event_id) = 'text'
        AND length(provenance_event_id) BETWEEN 1 AND 128
        AND provenance_event_id GLOB '[A-Za-z0-9]*'
        AND provenance_event_id NOT GLOB '*[^A-Za-z0-9._:-]*'
        AND instr(provenance_event_id, char(0)) = 0
    ),
    CHECK (
        typeof(correlation_kind) = 'text'
        AND correlation_kind = 'extraction_run'
        AND instr(correlation_kind, char(0)) = 0
    ),
    CHECK (
        typeof(correlation_id) = 'text'
        AND length(correlation_id) BETWEEN 1 AND 128
        AND correlation_id GLOB '[A-Za-z0-9]*'
        AND correlation_id NOT GLOB '*[^A-Za-z0-9._:-]*'
        AND instr(correlation_id, char(0)) = 0
    ),
    CHECK (
        typeof(extraction_kind) = 'text'
        AND extraction_kind IN (
            'deterministic', 'model_free_nondeterministic', 'model_backed'
        )
        AND instr(extraction_kind, char(0)) = 0
    ),
    CHECK (
        typeof(extraction_run_id) = 'text'
        AND length(extraction_run_id) BETWEEN 1 AND 128
        AND extraction_run_id GLOB '[A-Za-z0-9]*'
        AND extraction_run_id NOT GLOB '*[^A-Za-z0-9._:-]*'
        AND instr(extraction_run_id, char(0)) = 0
    ),
    CHECK (
        typeof(extractor_id) = 'text'
        AND length(extractor_id) BETWEEN 1 AND 128
        AND extractor_id GLOB '[A-Za-z0-9]*'
        AND extractor_id NOT GLOB '*[^A-Za-z0-9._:-]*'
        AND instr(extractor_id, char(0)) = 0
    ),
    CHECK (
        typeof(extractor_version) = 'text'
        AND length(extractor_version) BETWEEN 1 AND 128
        AND extractor_version GLOB '[A-Za-z0-9]*'
        AND extractor_version NOT GLOB '*[^A-Za-z0-9._:-]*'
        AND instr(extractor_version, char(0)) = 0
    ),
    CHECK (
        typeof(pipeline_version) = 'text'
        AND length(pipeline_version) BETWEEN 1 AND 128
        AND pipeline_version GLOB '[A-Za-z0-9]*'
        AND pipeline_version NOT GLOB '*[^A-Za-z0-9._:-]*'
        AND instr(pipeline_version, char(0)) = 0
    ),
    CHECK (
        typeof(configuration_version) = 'text'
        AND length(configuration_version) BETWEEN 1 AND 128
        AND configuration_version GLOB '[A-Za-z0-9]*'
        AND configuration_version NOT GLOB '*[^A-Za-z0-9._:-]*'
        AND instr(configuration_version, char(0)) = 0
    ),
    CHECK (
        configuration_digest IS NULL
        OR (
            typeof(configuration_digest) = 'text'
            AND length(configuration_digest) = 71
            AND substr(configuration_digest, 1, 7) = 'sha256:'
            AND substr(configuration_digest, 8) NOT GLOB '*[^0-9a-f]*'
            AND instr(configuration_digest, char(0)) = 0
        )
    ),
    CHECK (
        inference_configuration_digest IS NULL
        OR (
            typeof(inference_configuration_digest) = 'text'
            AND length(inference_configuration_digest) = 71
            AND substr(inference_configuration_digest, 1, 7) = 'sha256:'
            AND substr(inference_configuration_digest, 8) NOT GLOB '*[^0-9a-f]*'
            AND instr(inference_configuration_digest, char(0)) = 0
        )
    ),
    CHECK (
        prompt_template_digest IS NULL
        OR (
            typeof(prompt_template_digest) = 'text'
            AND length(prompt_template_digest) = 71
            AND substr(prompt_template_digest, 1, 7) = 'sha256:'
            AND substr(prompt_template_digest, 8) NOT GLOB '*[^0-9a-f]*'
            AND instr(prompt_template_digest, char(0)) = 0
        )
    ),
    CHECK (
        algorithm_id IS NULL
        OR (
            typeof(algorithm_id) = 'text'
            AND length(algorithm_id) BETWEEN 1 AND 128
            AND algorithm_id GLOB '[A-Za-z0-9]*'
            AND algorithm_id NOT GLOB '*[^A-Za-z0-9._:-]*'
            AND instr(algorithm_id, char(0)) = 0
        )
    ),
    CHECK (
        algorithm_version IS NULL
        OR (
            typeof(algorithm_version) = 'text'
            AND length(algorithm_version) BETWEEN 1 AND 128
            AND algorithm_version GLOB '[A-Za-z0-9]*'
            AND algorithm_version NOT GLOB '*[^A-Za-z0-9._:-]*'
            AND instr(algorithm_version, char(0)) = 0
        )
    ),
    CHECK (
        randomness_seed IS NULL
        OR (
            typeof(randomness_seed) = 'text'
            AND length(randomness_seed) BETWEEN 1 AND 128
            AND randomness_seed GLOB '[A-Za-z0-9]*'
            AND randomness_seed NOT GLOB '*[^A-Za-z0-9._:-]*'
            AND instr(randomness_seed, char(0)) = 0
        )
    ),
    CHECK (
        model_id IS NULL
        OR (
            typeof(model_id) = 'text'
            AND length(model_id) BETWEEN 1 AND 128
            AND model_id GLOB '[A-Za-z0-9]*'
            AND model_id NOT GLOB '*[^A-Za-z0-9._:-]*'
            AND instr(model_id, char(0)) = 0
        )
    ),
    CHECK (
        model_version IS NULL
        OR (
            typeof(model_version) = 'text'
            AND length(model_version) BETWEEN 1 AND 128
            AND model_version GLOB '[A-Za-z0-9]*'
            AND model_version NOT GLOB '*[^A-Za-z0-9._:-]*'
            AND instr(model_version, char(0)) = 0
        )
    ),
    CHECK (
        prompt_template_id IS NULL
        OR (
            typeof(prompt_template_id) = 'text'
            AND length(prompt_template_id) BETWEEN 1 AND 128
            AND prompt_template_id GLOB '[A-Za-z0-9]*'
            AND prompt_template_id NOT GLOB '*[^A-Za-z0-9._:-]*'
            AND instr(prompt_template_id, char(0)) = 0
        )
    ),
    CHECK (
        prompt_template_version IS NULL
        OR (
            typeof(prompt_template_version) = 'text'
            AND length(prompt_template_version) BETWEEN 1 AND 128
            AND prompt_template_version GLOB '[A-Za-z0-9]*'
            AND prompt_template_version NOT GLOB '*[^A-Za-z0-9._:-]*'
            AND instr(prompt_template_version, char(0)) = 0
        )
    ),
    CHECK (
        typeof(actor_id) = 'text'
        AND length(actor_id) BETWEEN 1 AND 128
        AND actor_id GLOB '[A-Za-z0-9]*'
        AND actor_id NOT GLOB '*[^A-Za-z0-9._:-]*'
        AND instr(actor_id, char(0)) = 0
    ),
    CHECK (
        typeof(actor_kind) = 'text'
        AND length(actor_kind) BETWEEN 1 AND 128
        AND actor_kind GLOB '[a-z]*'
        AND actor_kind NOT GLOB '*[^a-z0-9_.]*'
        AND actor_kind NOT GLOB '*.'
        AND actor_kind NOT GLOB '*.[^a-z]*'
        AND instr(actor_kind, char(0)) = 0
    ),
    CHECK (
        typeof(actor_role) = 'text'
        AND length(actor_role) BETWEEN 1 AND 128
        AND actor_role GLOB '[a-z]*'
        AND actor_role NOT GLOB '*[^a-z0-9_.]*'
        AND actor_role NOT GLOB '*.'
        AND actor_role NOT GLOB '*.[^a-z]*'
        AND instr(actor_role, char(0)) = 0
    ),
    CHECK (
        typeof(evidence_disposition) = 'text'
        AND evidence_disposition IN ('available', 'unavailable', 'redacted')
        AND instr(evidence_disposition, char(0)) = 0
    ),
    CHECK (typeof(occurred_at_us) = 'integer' AND occurred_at_us > 0),
    CHECK (typeof(recorded_at_us) = 'integer' AND recorded_at_us > 0),
    CHECK (occurred_at_us <= recorded_at_us),
    CHECK (typeof(append_ordinal) = 'integer' AND append_ordinal > 0),
    CHECK (
        (extraction_kind = 'deterministic'
         AND algorithm_id IS NOT NULL
         AND algorithm_version IS NOT NULL
         AND configuration_digest IS NOT NULL
         AND randomness_seed IS NULL
         AND model_id IS NULL
         AND model_version IS NULL
         AND inference_configuration_digest IS NULL
         AND prompt_template_id IS NULL
         AND prompt_template_version IS NULL
         AND prompt_template_digest IS NULL)
        OR (extraction_kind = 'model_free_nondeterministic'
            AND algorithm_id IS NOT NULL
            AND algorithm_version IS NOT NULL
            AND configuration_digest IS NOT NULL
            AND model_id IS NULL
            AND model_version IS NULL
            AND inference_configuration_digest IS NULL
            AND prompt_template_id IS NULL
            AND prompt_template_version IS NULL
            AND prompt_template_digest IS NULL)
        OR (extraction_kind = 'model_backed'
            AND model_id IS NOT NULL
            AND model_version IS NOT NULL
            AND inference_configuration_digest IS NOT NULL
            AND randomness_seed IS NULL)
    ),
    CHECK (
        (prompt_template_id IS NULL
         AND prompt_template_version IS NULL
         AND prompt_template_digest IS NULL)
        OR (prompt_template_id IS NOT NULL
            AND prompt_template_version IS NOT NULL
            AND prompt_template_digest IS NOT NULL)
    ),

    FOREIGN KEY (workspace_id, assembly_id, correlation_kind, correlation_id)
        REFERENCES omnivia_governed_version_assemblies
            (workspace_id, assembly_id, correlation_kind, correlation_id),
    FOREIGN KEY (workspace_id, assembly_id, provenance_event_id)
        REFERENCES omnivia_governed_provenance_events
            (workspace_id, assembly_id, provenance_event_id)
) WITHOUT ROWID;

-- What migrated one legacy claim into this workspace.
--
-- The columns it does *not* have are the substance of the table. There is no
-- reviewer, no policy, no model and no prompt, so a migrated legacy claim cannot
-- present itself as reviewed, evaluated or generated: it is a claim somebody's older
-- system held, carried across with the identity of that source, its version, the
-- migration run that moved it and a digest of what was read. Exactly one row for a
-- `migrated_legacy_claim` candidate, none for anything else, and it names the exact
-- `candidate.legacy_migrated` event it explains.
CREATE TABLE IF NOT EXISTS omnivia_governed_legacy_lineage (
    workspace_id          TEXT    NOT NULL,
    assembly_id           TEXT    NOT NULL,
    provenance_event_id   TEXT    NOT NULL,
    correlation_kind      TEXT    NOT NULL,
    correlation_id        TEXT    NOT NULL,
    legacy_source_id      TEXT    NOT NULL,
    legacy_source_version TEXT    NOT NULL,
    legacy_source_digest  TEXT    NOT NULL,
    migration_run_id      TEXT    NOT NULL,
    actor_id              TEXT    NOT NULL,
    actor_kind            TEXT    NOT NULL,
    actor_role            TEXT    NOT NULL,
    evidence_disposition  TEXT    NOT NULL,
    occurred_at_us        INTEGER NOT NULL,
    recorded_at_us        INTEGER NOT NULL,
    append_ordinal        INTEGER NOT NULL,

    PRIMARY KEY (workspace_id, assembly_id),

    CHECK (
        typeof(workspace_id) = 'text'
        AND length(workspace_id) BETWEEN 1 AND 128
        AND workspace_id GLOB '[A-Za-z0-9]*'
        AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
        AND instr(workspace_id, char(0)) = 0
    ),
    CHECK (
        typeof(assembly_id) = 'text'
        AND length(assembly_id) BETWEEN 1 AND 128
        AND assembly_id GLOB '[A-Za-z0-9]*'
        AND assembly_id NOT GLOB '*[^A-Za-z0-9._:-]*'
        AND instr(assembly_id, char(0)) = 0
    ),
    CHECK (
        typeof(provenance_event_id) = 'text'
        AND length(provenance_event_id) BETWEEN 1 AND 128
        AND provenance_event_id GLOB '[A-Za-z0-9]*'
        AND provenance_event_id NOT GLOB '*[^A-Za-z0-9._:-]*'
        AND instr(provenance_event_id, char(0)) = 0
    ),
    CHECK (
        typeof(correlation_kind) = 'text'
        AND correlation_kind = 'migration_run'
        AND instr(correlation_kind, char(0)) = 0
    ),
    CHECK (
        typeof(correlation_id) = 'text'
        AND length(correlation_id) BETWEEN 1 AND 128
        AND correlation_id GLOB '[A-Za-z0-9]*'
        AND correlation_id NOT GLOB '*[^A-Za-z0-9._:-]*'
        AND instr(correlation_id, char(0)) = 0
    ),
    CHECK (
        typeof(legacy_source_id) = 'text'
        AND length(legacy_source_id) BETWEEN 1 AND 128
        AND legacy_source_id GLOB '[A-Za-z0-9]*'
        AND legacy_source_id NOT GLOB '*[^A-Za-z0-9._:-]*'
        AND instr(legacy_source_id, char(0)) = 0
    ),
    CHECK (
        typeof(legacy_source_version) = 'text'
        AND length(legacy_source_version) BETWEEN 1 AND 128
        AND legacy_source_version GLOB '[A-Za-z0-9]*'
        AND legacy_source_version NOT GLOB '*[^A-Za-z0-9._:-]*'
        AND instr(legacy_source_version, char(0)) = 0
    ),
    CHECK (
        typeof(migration_run_id) = 'text'
        AND length(migration_run_id) BETWEEN 1 AND 128
        AND migration_run_id GLOB '[A-Za-z0-9]*'
        AND migration_run_id NOT GLOB '*[^A-Za-z0-9._:-]*'
        AND instr(migration_run_id, char(0)) = 0
    ),
    CHECK (
        typeof(legacy_source_digest) = 'text'
        AND length(legacy_source_digest) = 71
        AND substr(legacy_source_digest, 1, 7) = 'sha256:'
        AND substr(legacy_source_digest, 8) NOT GLOB '*[^0-9a-f]*'
        AND instr(legacy_source_digest, char(0)) = 0
    ),
    CHECK (
        typeof(actor_id) = 'text'
        AND length(actor_id) BETWEEN 1 AND 128
        AND actor_id GLOB '[A-Za-z0-9]*'
        AND actor_id NOT GLOB '*[^A-Za-z0-9._:-]*'
        AND instr(actor_id, char(0)) = 0
    ),
    CHECK (
        typeof(actor_kind) = 'text'
        AND length(actor_kind) BETWEEN 1 AND 128
        AND actor_kind GLOB '[a-z]*'
        AND actor_kind NOT GLOB '*[^a-z0-9_.]*'
        AND actor_kind NOT GLOB '*.'
        AND actor_kind NOT GLOB '*.[^a-z]*'
        AND instr(actor_kind, char(0)) = 0
    ),
    CHECK (
        typeof(actor_role) = 'text'
        AND length(actor_role) BETWEEN 1 AND 128
        AND actor_role GLOB '[a-z]*'
        AND actor_role NOT GLOB '*[^a-z0-9_.]*'
        AND actor_role NOT GLOB '*.'
        AND actor_role NOT GLOB '*.[^a-z]*'
        AND instr(actor_role, char(0)) = 0
    ),
    CHECK (
        typeof(evidence_disposition) = 'text'
        AND evidence_disposition IN ('available', 'unavailable', 'redacted')
        AND instr(evidence_disposition, char(0)) = 0
    ),
    CHECK (typeof(occurred_at_us) = 'integer' AND occurred_at_us > 0),
    CHECK (typeof(recorded_at_us) = 'integer' AND recorded_at_us > 0),
    CHECK (occurred_at_us <= recorded_at_us),
    CHECK (typeof(append_ordinal) = 'integer' AND append_ordinal > 0),

    FOREIGN KEY (workspace_id, assembly_id, correlation_kind, correlation_id)
        REFERENCES omnivia_governed_version_assemblies
            (workspace_id, assembly_id, correlation_kind, correlation_id),
    FOREIGN KEY (workspace_id, assembly_id, provenance_event_id)
        REFERENCES omnivia_governed_provenance_events
            (workspace_id, assembly_id, provenance_event_id)
) WITHOUT ROWID;

-- Who or what did something to one exact version, and when, in order.
--
-- The eleven admitted actions are a closed set because each one is a transition this
-- slice knows how to check at seal time, and an unrecognised twelfth would be a
-- transition nothing validates. `provenance_sequence` orders the stream within the
-- assembly, so two facts recorded in the same microsecond are still ordered and no
-- reader has to break a tie on a clock that cannot break it.
--
-- Actor and policy are an exclusive pair, enforced here and matched to the
-- assembly's decision source at seal time. A human review names a principal and no
-- policy; a policy evaluation names a policy and its version and no principal. The
-- pair is exclusive rather than optional because "a reviewer and a policy both did
-- this" is not a thing that happened, and a row that says so would make the audit
-- trail unreadable exactly where it matters most.
--
-- The five-column foreign key ties the event to its assembly, that assembly's exact
-- version id and its exact correlation tuple together, so an event cannot be written
-- against a version assembled under a different audit or extraction run. A reason is
-- mandatory for every governed outcome, correction and supersession, because those
-- are the events somebody will later need explained.
CREATE TABLE IF NOT EXISTS omnivia_governed_provenance_events (
    workspace_id               TEXT    NOT NULL,
    provenance_event_id        TEXT    NOT NULL,
    assembly_id                TEXT    NOT NULL,
    governed_record_version_id TEXT    NOT NULL,
    provenance_sequence        INTEGER NOT NULL,
    action                     TEXT    NOT NULL,
    actor_id                   TEXT,
    actor_kind                 TEXT,
    actor_role                 TEXT,
    policy_id                  TEXT,
    policy_version             TEXT,
    occurred_at_us             INTEGER NOT NULL,
    recorded_at_us             INTEGER NOT NULL,
    reason_code                TEXT,
    reason_comment             TEXT,
    audit_ref                  TEXT,
    correlation_kind           TEXT    NOT NULL,
    correlation_id             TEXT    NOT NULL,
    predecessor_record_id      TEXT,
    predecessor_version_id     TEXT,
    evidence_disposition       TEXT    NOT NULL,

    PRIMARY KEY (workspace_id, provenance_event_id),

    CHECK (
        typeof(workspace_id) = 'text'
        AND length(workspace_id) BETWEEN 1 AND 128
        AND workspace_id GLOB '[A-Za-z0-9]*'
        AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
        AND instr(workspace_id, char(0)) = 0
    ),
    CHECK (
        typeof(provenance_event_id) = 'text'
        AND length(provenance_event_id) BETWEEN 1 AND 128
        AND provenance_event_id GLOB '[A-Za-z0-9]*'
        AND provenance_event_id NOT GLOB '*[^A-Za-z0-9._:-]*'
        AND instr(provenance_event_id, char(0)) = 0
    ),
    CHECK (
        typeof(assembly_id) = 'text'
        AND length(assembly_id) BETWEEN 1 AND 128
        AND assembly_id GLOB '[A-Za-z0-9]*'
        AND assembly_id NOT GLOB '*[^A-Za-z0-9._:-]*'
        AND instr(assembly_id, char(0)) = 0
    ),
    CHECK (
        typeof(governed_record_version_id) = 'text'
        AND length(governed_record_version_id) BETWEEN 1 AND 128
        AND governed_record_version_id GLOB '[A-Za-z0-9]*'
        AND governed_record_version_id NOT GLOB '*[^A-Za-z0-9._:-]*'
        AND instr(governed_record_version_id, char(0)) = 0
    ),
    CHECK (
        typeof(action) = 'text'
        AND action IN (
            'candidate.extracted',
            'candidate.human_proposed',
            'candidate.legacy_migrated',
            'governance.accepted',
            'governance.rejected',
            'governance.contested',
            'governance.withdrawn',
            'governance.superseded',
            'governance.corrected',
            'relation.asserted',
            'record.superseded'
        )
        AND instr(action, char(0)) = 0
    ),
    CHECK (
        actor_id IS NULL
        OR (
            typeof(actor_id) = 'text'
            AND length(actor_id) BETWEEN 1 AND 128
            AND actor_id GLOB '[A-Za-z0-9]*'
            AND actor_id NOT GLOB '*[^A-Za-z0-9._:-]*'
            AND instr(actor_id, char(0)) = 0
        )
    ),
    CHECK (
        actor_kind IS NULL
        OR (
            typeof(actor_kind) = 'text'
            AND length(actor_kind) BETWEEN 1 AND 128
            AND actor_kind GLOB '[a-z]*'
            AND actor_kind NOT GLOB '*[^a-z0-9_.]*'
            AND actor_kind NOT GLOB '*.'
            AND actor_kind NOT GLOB '*.[^a-z]*'
            AND instr(actor_kind, char(0)) = 0
        )
    ),
    CHECK (
        actor_role IS NULL
        OR (
            typeof(actor_role) = 'text'
            AND length(actor_role) BETWEEN 1 AND 128
            AND actor_role GLOB '[a-z]*'
            AND actor_role NOT GLOB '*[^a-z0-9_.]*'
            AND actor_role NOT GLOB '*.'
            AND actor_role NOT GLOB '*.[^a-z]*'
            AND instr(actor_role, char(0)) = 0
        )
    ),
    CHECK (
        policy_id IS NULL
        OR (
            typeof(policy_id) = 'text'
            AND length(policy_id) BETWEEN 1 AND 128
            AND policy_id GLOB '[A-Za-z0-9]*'
            AND policy_id NOT GLOB '*[^A-Za-z0-9._:-]*'
            AND instr(policy_id, char(0)) = 0
        )
    ),
    CHECK (
        policy_version IS NULL
        OR (
            typeof(policy_version) = 'text'
            AND length(policy_version) BETWEEN 1 AND 128
            AND policy_version GLOB '[A-Za-z0-9]*'
            AND policy_version NOT GLOB '*[^A-Za-z0-9._:-]*'
            AND instr(policy_version, char(0)) = 0
        )
    ),
    CHECK (
        reason_code IS NULL
        OR (
            typeof(reason_code) = 'text'
            AND length(reason_code) BETWEEN 1 AND 128
            AND reason_code GLOB '[a-z]*'
            AND reason_code NOT GLOB '*[^a-z0-9_.]*'
            AND reason_code NOT GLOB '*.'
            AND reason_code NOT GLOB '*.[^a-z]*'
            AND instr(reason_code, char(0)) = 0
        )
    ),
    CHECK (
        reason_comment IS NULL
        OR (
            typeof(reason_comment) = 'text'
            AND length(reason_comment) BETWEEN 1 AND 2048
            AND instr(reason_comment, char(0)) = 0
        )
    ),
    CHECK (
        audit_ref IS NULL
        OR (
            typeof(audit_ref) = 'text'
            AND length(audit_ref) BETWEEN 1 AND 128
            AND audit_ref GLOB '[A-Za-z0-9]*'
            AND audit_ref NOT GLOB '*[^A-Za-z0-9._:-]*'
            AND instr(audit_ref, char(0)) = 0
        )
    ),
    CHECK (
        typeof(correlation_kind) = 'text'
        AND correlation_kind IN ('m1_audit', 'extraction_run', 'migration_run')
        AND instr(correlation_kind, char(0)) = 0
    ),
    CHECK (
        typeof(correlation_id) = 'text'
        AND length(correlation_id) BETWEEN 1 AND 128
        AND correlation_id GLOB '[A-Za-z0-9]*'
        AND correlation_id NOT GLOB '*[^A-Za-z0-9._:-]*'
        AND instr(correlation_id, char(0)) = 0
    ),
    CHECK (
        predecessor_record_id IS NULL
        OR (
            typeof(predecessor_record_id) = 'text'
            AND length(predecessor_record_id) BETWEEN 1 AND 128
            AND predecessor_record_id GLOB '[A-Za-z0-9]*'
            AND predecessor_record_id NOT GLOB '*[^A-Za-z0-9._:-]*'
            AND instr(predecessor_record_id, char(0)) = 0
        )
    ),
    CHECK (
        predecessor_version_id IS NULL
        OR (
            typeof(predecessor_version_id) = 'text'
            AND length(predecessor_version_id) BETWEEN 1 AND 128
            AND predecessor_version_id GLOB '[A-Za-z0-9]*'
            AND predecessor_version_id NOT GLOB '*[^A-Za-z0-9._:-]*'
            AND instr(predecessor_version_id, char(0)) = 0
        )
    ),
    CHECK (
        typeof(evidence_disposition) = 'text'
        AND evidence_disposition IN ('available', 'unavailable', 'redacted')
        AND instr(evidence_disposition, char(0)) = 0
    ),
    CHECK (typeof(provenance_sequence) = 'integer' AND provenance_sequence > 0),
    CHECK (typeof(occurred_at_us) = 'integer' AND occurred_at_us > 0),
    CHECK (typeof(recorded_at_us) = 'integer' AND recorded_at_us > 0),
    CHECK (occurred_at_us <= recorded_at_us),
    CHECK (
        (actor_id IS NOT NULL
         AND actor_kind IS NOT NULL
         AND actor_role IS NOT NULL
         AND policy_id IS NULL
         AND policy_version IS NULL)
        OR (policy_id IS NOT NULL
            AND policy_version IS NOT NULL
            AND actor_id IS NULL
            AND actor_kind IS NULL
            AND actor_role IS NULL)
    ),
    CHECK (
        (predecessor_record_id IS NULL AND predecessor_version_id IS NULL)
        OR (predecessor_record_id IS NOT NULL AND predecessor_version_id IS NOT NULL)
    ),
    CHECK (
        action NOT IN (
            'governance.accepted',
            'governance.rejected',
            'governance.contested',
            'governance.withdrawn',
            'governance.superseded',
            'governance.corrected',
            'record.superseded'
        )
        OR reason_code IS NOT NULL
    ),
    CHECK (
        action NOT IN (
            'candidate.extracted',
            'candidate.human_proposed',
            'candidate.legacy_migrated'
        )
        OR (predecessor_record_id IS NULL AND predecessor_version_id IS NULL)
    ),

    FOREIGN KEY (
        workspace_id, assembly_id, governed_record_version_id,
        correlation_kind, correlation_id
    )
        REFERENCES omnivia_governed_version_assemblies (
            workspace_id, assembly_id, governed_record_version_id,
            correlation_kind, correlation_id
        ),
    FOREIGN KEY (audit_ref, workspace_id)
        REFERENCES omnivia_application_audit_events (audit_ref, workspace_id)
) WITHOUT ROWID;

-- The exact M2 ancestry of one provenance event: which captured evidence, which
-- parsed record and which span of it justify what this event claims.
--
-- Three foreign keys reach back into `0008` and each is composite over the
-- workspace, so a link cannot borrow another workspace's evidence: the artifact by
-- `(evidence_id, workspace_id)`, the normalized record by
-- `(normalized_record_id, evidence_id, workspace_id)` -- which is also what stops a
-- record being attributed to evidence it was not parsed from -- and the span by its
-- own id, with the full same-evidence chain re-proved at seal time.
--
-- The record and the span are optional, and that is what forces the physical key to
-- differ from the logical one. The logical identity of a link is the whole of
-- `(workspace, assembly, event, evidence, record, span, ordinal)`, but a
-- `WITHOUT ROWID` primary key cannot carry nullable members and SQLite's UNIQUE
-- treats two NULLs as distinct, so a duplicate unbounded link would slip through an
-- index that looked like it forbade one. The key here is therefore the ordinal --
-- `(workspace, assembly, event, ordinal)` -- and the INSERT trigger enforces the
-- complete logical identity with NULL-safe `IS`, refusing the same evidence, record
-- and span under the same event at any other ordinal. Ordering is by that positive
-- event-scoped ordinal, and duplicate links never increase evidence authority.
CREATE TABLE IF NOT EXISTS omnivia_governed_version_evidence_links (
    workspace_id         TEXT    NOT NULL,
    assembly_id          TEXT    NOT NULL,
    provenance_event_id  TEXT    NOT NULL,
    link_ordinal         INTEGER NOT NULL,
    evidence_id          TEXT    NOT NULL,
    normalized_record_id TEXT,
    normalized_span_id   TEXT,
    recorded_at_us       INTEGER NOT NULL,

    PRIMARY KEY (workspace_id, assembly_id, provenance_event_id, link_ordinal),

    CHECK (
        typeof(workspace_id) = 'text'
        AND length(workspace_id) BETWEEN 1 AND 128
        AND workspace_id GLOB '[A-Za-z0-9]*'
        AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
        AND instr(workspace_id, char(0)) = 0
    ),
    CHECK (
        typeof(assembly_id) = 'text'
        AND length(assembly_id) BETWEEN 1 AND 128
        AND assembly_id GLOB '[A-Za-z0-9]*'
        AND assembly_id NOT GLOB '*[^A-Za-z0-9._:-]*'
        AND instr(assembly_id, char(0)) = 0
    ),
    CHECK (
        typeof(provenance_event_id) = 'text'
        AND length(provenance_event_id) BETWEEN 1 AND 128
        AND provenance_event_id GLOB '[A-Za-z0-9]*'
        AND provenance_event_id NOT GLOB '*[^A-Za-z0-9._:-]*'
        AND instr(provenance_event_id, char(0)) = 0
    ),
    CHECK (
        typeof(evidence_id) = 'text'
        AND length(evidence_id) BETWEEN 1 AND 128
        AND evidence_id GLOB '[A-Za-z0-9]*'
        AND evidence_id NOT GLOB '*[^A-Za-z0-9._:-]*'
        AND instr(evidence_id, char(0)) = 0
    ),
    CHECK (
        normalized_record_id IS NULL
        OR (
            typeof(normalized_record_id) = 'text'
            AND length(normalized_record_id) BETWEEN 1 AND 128
            AND normalized_record_id GLOB '[A-Za-z0-9]*'
            AND normalized_record_id NOT GLOB '*[^A-Za-z0-9._:-]*'
            AND instr(normalized_record_id, char(0)) = 0
        )
    ),
    CHECK (
        normalized_span_id IS NULL
        OR (
            typeof(normalized_span_id) = 'text'
            AND length(normalized_span_id) BETWEEN 1 AND 128
            AND normalized_span_id GLOB '[A-Za-z0-9]*'
            AND normalized_span_id NOT GLOB '*[^A-Za-z0-9._:-]*'
            AND instr(normalized_span_id, char(0)) = 0
        )
    ),
    CHECK (normalized_span_id IS NULL OR normalized_record_id IS NOT NULL),
    CHECK (typeof(link_ordinal) = 'integer' AND link_ordinal > 0),
    CHECK (typeof(recorded_at_us) = 'integer' AND recorded_at_us > 0),

    FOREIGN KEY (workspace_id, assembly_id, provenance_event_id)
        REFERENCES omnivia_governed_provenance_events
            (workspace_id, assembly_id, provenance_event_id),
    FOREIGN KEY (evidence_id, workspace_id)
        REFERENCES omnivia_evidence_artifacts (evidence_id, workspace_id),
    FOREIGN KEY (normalized_record_id, evidence_id, workspace_id)
        REFERENCES omnivia_normalized_source_records
            (normalized_record_id, evidence_id, workspace_id),
    FOREIGN KEY (normalized_span_id)
        REFERENCES omnivia_normalized_source_spans (normalized_span_id)
) WITHOUT ROWID;

-- The two exact endpoints of one relation.
--
-- A relation is an ordinary governed record whose type is `knowledge.relation`, and
-- this is a component of that record's version assembly -- not a parallel graph with
-- its own identity, its own lifecycle and its own truth. That is why the key is the
-- assembly and why there is no `relation_id`: everything that makes a relation
-- durable, governed, superseded or evidenced is already the assembly's machinery.
--
-- Endpoints are exact versions, not records. A relation asserted about version 3 of
-- something stays about version 3 forever; a later version 4 does not silently
-- retarget it, because there is nothing here that could change.
--
-- `relation_type` carries only its `OpenCode` shape as a column CHECK. The five
-- accepted codes are a *seal-time* predicate, so a well-formed code this version
-- does not recognise may be stored as an inert future fact and can never seal --
-- which keeps a forward-compatible row representable without letting an unknown
-- relation become authoritative under M3.
CREATE TABLE IF NOT EXISTS omnivia_governed_relation_endpoints (
    workspace_id        TEXT    NOT NULL,
    assembly_id         TEXT    NOT NULL,
    provenance_event_id TEXT    NOT NULL,
    correlation_kind    TEXT    NOT NULL,
    correlation_id      TEXT    NOT NULL,
    relation_type       TEXT    NOT NULL,
    source_record_id    TEXT    NOT NULL,
    source_version_id   TEXT    NOT NULL,
    target_record_id    TEXT    NOT NULL,
    target_version_id   TEXT    NOT NULL,
    recorded_at_us      INTEGER NOT NULL,

    PRIMARY KEY (workspace_id, assembly_id),

    CHECK (
        typeof(workspace_id) = 'text'
        AND length(workspace_id) BETWEEN 1 AND 128
        AND workspace_id GLOB '[A-Za-z0-9]*'
        AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
        AND instr(workspace_id, char(0)) = 0
    ),
    CHECK (
        typeof(assembly_id) = 'text'
        AND length(assembly_id) BETWEEN 1 AND 128
        AND assembly_id GLOB '[A-Za-z0-9]*'
        AND assembly_id NOT GLOB '*[^A-Za-z0-9._:-]*'
        AND instr(assembly_id, char(0)) = 0
    ),
    CHECK (
        typeof(provenance_event_id) = 'text'
        AND length(provenance_event_id) BETWEEN 1 AND 128
        AND provenance_event_id GLOB '[A-Za-z0-9]*'
        AND provenance_event_id NOT GLOB '*[^A-Za-z0-9._:-]*'
        AND instr(provenance_event_id, char(0)) = 0
    ),
    CHECK (
        typeof(correlation_kind) = 'text'
        AND correlation_kind IN ('m1_audit', 'extraction_run', 'migration_run')
        AND instr(correlation_kind, char(0)) = 0
    ),
    CHECK (
        typeof(correlation_id) = 'text'
        AND length(correlation_id) BETWEEN 1 AND 128
        AND correlation_id GLOB '[A-Za-z0-9]*'
        AND correlation_id NOT GLOB '*[^A-Za-z0-9._:-]*'
        AND instr(correlation_id, char(0)) = 0
    ),
    CHECK (
        typeof(relation_type) = 'text'
        AND length(relation_type) BETWEEN 1 AND 128
        AND relation_type GLOB '[a-z]*'
        AND relation_type NOT GLOB '*[^a-z0-9_.]*'
        AND relation_type NOT GLOB '*.'
        AND relation_type NOT GLOB '*.[^a-z]*'
        AND instr(relation_type, char(0)) = 0
    ),
    CHECK (
        typeof(source_record_id) = 'text'
        AND length(source_record_id) BETWEEN 1 AND 128
        AND source_record_id GLOB '[A-Za-z0-9]*'
        AND source_record_id NOT GLOB '*[^A-Za-z0-9._:-]*'
        AND instr(source_record_id, char(0)) = 0
    ),
    CHECK (
        typeof(source_version_id) = 'text'
        AND length(source_version_id) BETWEEN 1 AND 128
        AND source_version_id GLOB '[A-Za-z0-9]*'
        AND source_version_id NOT GLOB '*[^A-Za-z0-9._:-]*'
        AND instr(source_version_id, char(0)) = 0
    ),
    CHECK (
        typeof(target_record_id) = 'text'
        AND length(target_record_id) BETWEEN 1 AND 128
        AND target_record_id GLOB '[A-Za-z0-9]*'
        AND target_record_id NOT GLOB '*[^A-Za-z0-9._:-]*'
        AND instr(target_record_id, char(0)) = 0
    ),
    CHECK (
        typeof(target_version_id) = 'text'
        AND length(target_version_id) BETWEEN 1 AND 128
        AND target_version_id GLOB '[A-Za-z0-9]*'
        AND target_version_id NOT GLOB '*[^A-Za-z0-9._:-]*'
        AND instr(target_version_id, char(0)) = 0
    ),
    CHECK (typeof(recorded_at_us) = 'integer' AND recorded_at_us > 0),
    CHECK (source_record_id <> target_record_id),

    FOREIGN KEY (workspace_id, assembly_id, correlation_kind, correlation_id)
        REFERENCES omnivia_governed_version_assemblies
            (workspace_id, assembly_id, correlation_kind, correlation_id),
    FOREIGN KEY (workspace_id, assembly_id, provenance_event_id)
        REFERENCES omnivia_governed_provenance_events
            (workspace_id, assembly_id, provenance_event_id),
    FOREIGN KEY (workspace_id, source_version_id)
        REFERENCES omnivia_governed_version_assemblies
            (workspace_id, governed_record_version_id),
    FOREIGN KEY (workspace_id, target_version_id)
        REFERENCES omnivia_governed_version_assemblies
            (workspace_id, governed_record_version_id)
) WITHOUT ROWID;

-- One directed replacement edge: this version supersedes that one.
--
-- The edge is explicit and exact because nothing else can order two versions
-- reliably. Recorded times may be equal, append ordinals from different correlation
-- parents are not comparable at all, and a reader that guessed from either would
-- eventually guess wrong. The directed edge, over exact version ids, is the whole
-- answer -- so at equal timestamps causality is still unambiguous.
--
-- Three unique indexes carry three separate rules that are easy to conflate: one
-- edge per assembly, one edge per source (no branching -- a version is superseded
-- once) and one edge per target (no converging -- a version supersedes one thing).
-- Cycles are a different shape of wrong and are refused at seal time by a recursive
-- walk of the sealed graph, which is the only graph that means anything.
CREATE TABLE IF NOT EXISTS omnivia_record_supersessions (
    workspace_id        TEXT    NOT NULL,
    supersession_id     TEXT    NOT NULL,
    assembly_id         TEXT    NOT NULL,
    governed_record_id  TEXT    NOT NULL,
    source_version_id   TEXT    NOT NULL,
    target_version_id   TEXT    NOT NULL,
    provenance_event_id TEXT    NOT NULL,
    correlation_kind    TEXT    NOT NULL,
    correlation_id      TEXT    NOT NULL,
    recorded_at_us      INTEGER NOT NULL,

    PRIMARY KEY (workspace_id, supersession_id),

    CHECK (
        typeof(workspace_id) = 'text'
        AND length(workspace_id) BETWEEN 1 AND 128
        AND workspace_id GLOB '[A-Za-z0-9]*'
        AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
        AND instr(workspace_id, char(0)) = 0
    ),
    CHECK (
        typeof(supersession_id) = 'text'
        AND length(supersession_id) BETWEEN 1 AND 128
        AND supersession_id GLOB '[A-Za-z0-9]*'
        AND supersession_id NOT GLOB '*[^A-Za-z0-9._:-]*'
        AND instr(supersession_id, char(0)) = 0
    ),
    CHECK (
        typeof(assembly_id) = 'text'
        AND length(assembly_id) BETWEEN 1 AND 128
        AND assembly_id GLOB '[A-Za-z0-9]*'
        AND assembly_id NOT GLOB '*[^A-Za-z0-9._:-]*'
        AND instr(assembly_id, char(0)) = 0
    ),
    CHECK (
        typeof(governed_record_id) = 'text'
        AND length(governed_record_id) BETWEEN 1 AND 128
        AND governed_record_id GLOB '[A-Za-z0-9]*'
        AND governed_record_id NOT GLOB '*[^A-Za-z0-9._:-]*'
        AND instr(governed_record_id, char(0)) = 0
    ),
    CHECK (
        typeof(source_version_id) = 'text'
        AND length(source_version_id) BETWEEN 1 AND 128
        AND source_version_id GLOB '[A-Za-z0-9]*'
        AND source_version_id NOT GLOB '*[^A-Za-z0-9._:-]*'
        AND instr(source_version_id, char(0)) = 0
    ),
    CHECK (
        typeof(target_version_id) = 'text'
        AND length(target_version_id) BETWEEN 1 AND 128
        AND target_version_id GLOB '[A-Za-z0-9]*'
        AND target_version_id NOT GLOB '*[^A-Za-z0-9._:-]*'
        AND instr(target_version_id, char(0)) = 0
    ),
    CHECK (
        typeof(provenance_event_id) = 'text'
        AND length(provenance_event_id) BETWEEN 1 AND 128
        AND provenance_event_id GLOB '[A-Za-z0-9]*'
        AND provenance_event_id NOT GLOB '*[^A-Za-z0-9._:-]*'
        AND instr(provenance_event_id, char(0)) = 0
    ),
    CHECK (
        typeof(correlation_kind) = 'text'
        AND correlation_kind = 'm1_audit'
        AND instr(correlation_kind, char(0)) = 0
    ),
    CHECK (
        typeof(correlation_id) = 'text'
        AND length(correlation_id) BETWEEN 1 AND 128
        AND correlation_id GLOB '[A-Za-z0-9]*'
        AND correlation_id NOT GLOB '*[^A-Za-z0-9._:-]*'
        AND instr(correlation_id, char(0)) = 0
    ),
    CHECK (typeof(recorded_at_us) = 'integer' AND recorded_at_us > 0),
    CHECK (source_version_id <> target_version_id),

    FOREIGN KEY (workspace_id, assembly_id, correlation_kind, correlation_id)
        REFERENCES omnivia_governed_version_assemblies
            (workspace_id, assembly_id, correlation_kind, correlation_id),
    FOREIGN KEY (workspace_id, assembly_id, provenance_event_id)
        REFERENCES omnivia_governed_provenance_events
            (workspace_id, assembly_id, provenance_event_id),
    FOREIGN KEY (workspace_id, governed_record_id)
        REFERENCES omnivia_governed_records (workspace_id, governed_record_id),
    FOREIGN KEY (workspace_id, source_version_id)
        REFERENCES omnivia_governed_version_assemblies
            (workspace_id, governed_record_version_id),
    FOREIGN KEY (workspace_id, target_version_id)
        REFERENCES omnivia_governed_version_assemblies
            (workspace_id, governed_record_version_id)
) WITHOUT ROWID;

-- The last step, and the only one that confers authority.
--
-- Everything else in this slice is assembly. This row is what turns an assembly into
-- something the authoritative view will show and something a later version may name
-- as its predecessor, and its BEFORE INSERT trigger is where every cross-row
-- invariant M3 states is actually checked -- the exact event set, each event's actor,
-- policy, time, reason, audit and predecessor, the lineage matrix, the evidence
-- links and their full M2 ancestry, the relation invariants, the semantic duplicate
-- key, and the supersession rules including the cycle walk.
--
-- Seal once: one seal per assembly, one per version. After it, every component table
-- refuses an insert naming this assembly, so the sealed set is closed and a version
-- cannot grow a new event, link or endpoint after it became authoritative.
CREATE TABLE IF NOT EXISTS omnivia_governed_version_seals (
    workspace_id               TEXT    NOT NULL,
    seal_id                    TEXT    NOT NULL,
    assembly_id                TEXT    NOT NULL,
    governed_record_version_id TEXT    NOT NULL,
    correlation_kind           TEXT    NOT NULL,
    correlation_id             TEXT    NOT NULL,
    sealed_at_us               INTEGER NOT NULL,

    PRIMARY KEY (workspace_id, seal_id),

    CHECK (
        typeof(workspace_id) = 'text'
        AND length(workspace_id) BETWEEN 1 AND 128
        AND workspace_id GLOB '[A-Za-z0-9]*'
        AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
        AND instr(workspace_id, char(0)) = 0
    ),
    CHECK (
        typeof(seal_id) = 'text'
        AND length(seal_id) BETWEEN 1 AND 128
        AND seal_id GLOB '[A-Za-z0-9]*'
        AND seal_id NOT GLOB '*[^A-Za-z0-9._:-]*'
        AND instr(seal_id, char(0)) = 0
    ),
    CHECK (
        typeof(assembly_id) = 'text'
        AND length(assembly_id) BETWEEN 1 AND 128
        AND assembly_id GLOB '[A-Za-z0-9]*'
        AND assembly_id NOT GLOB '*[^A-Za-z0-9._:-]*'
        AND instr(assembly_id, char(0)) = 0
    ),
    CHECK (
        typeof(governed_record_version_id) = 'text'
        AND length(governed_record_version_id) BETWEEN 1 AND 128
        AND governed_record_version_id GLOB '[A-Za-z0-9]*'
        AND governed_record_version_id NOT GLOB '*[^A-Za-z0-9._:-]*'
        AND instr(governed_record_version_id, char(0)) = 0
    ),
    CHECK (
        typeof(correlation_kind) = 'text'
        AND correlation_kind IN ('m1_audit', 'extraction_run', 'migration_run')
        AND instr(correlation_kind, char(0)) = 0
    ),
    CHECK (
        typeof(correlation_id) = 'text'
        AND length(correlation_id) BETWEEN 1 AND 128
        AND correlation_id GLOB '[A-Za-z0-9]*'
        AND correlation_id NOT GLOB '*[^A-Za-z0-9._:-]*'
        AND instr(correlation_id, char(0)) = 0
    ),
    CHECK (typeof(sealed_at_us) = 'integer' AND sealed_at_us > 0),

    FOREIGN KEY (
        workspace_id, assembly_id, governed_record_version_id,
        correlation_kind, correlation_id
    )
        REFERENCES omnivia_governed_version_assemblies (
            workspace_id, assembly_id, governed_record_version_id,
            correlation_kind, correlation_id
        )
) WITHOUT ROWID;

-- Parent keys for the composite foreign keys above. SQLite requires the referenced
-- columns to carry a UNIQUE index over exactly that column set, and these are
-- declared by name rather than left to an implicit `sqlite_autoindex_*` so they
-- appear in `sqlite_master` under a readable name and therefore inside the canonical
-- schema fingerprint, which filters implicit indexes out.
--
-- Several of them restate a primary key deliberately, for that reason alone. The
-- wider ones are not redundant with the narrower ones: `..._typed_identity` is what
-- makes "the version agrees with its record about type and scope" a foreign key,
-- and `..._version_correlation` is what makes "this event belongs to this exact
-- version assembled under this exact audit" one.
CREATE UNIQUE INDEX IF NOT EXISTS omnivia_idx_governed_schema_catalogue_identity
    ON omnivia_governed_schema_catalogue (record_type, content_schema_version);
CREATE UNIQUE INDEX IF NOT EXISTS omnivia_idx_governed_records_identity
    ON omnivia_governed_records (workspace_id, governed_record_id);
CREATE UNIQUE INDEX IF NOT EXISTS omnivia_idx_governed_records_typed_identity
    ON omnivia_governed_records
        (workspace_id, governed_record_id, record_type, domain_scope);
CREATE UNIQUE INDEX IF NOT EXISTS omnivia_idx_governed_version_assemblies_identity
    ON omnivia_governed_version_assemblies (workspace_id, assembly_id);
CREATE UNIQUE INDEX IF NOT EXISTS omnivia_idx_governed_version_assemblies_version
    ON omnivia_governed_version_assemblies
        (workspace_id, governed_record_version_id);
CREATE UNIQUE INDEX IF NOT EXISTS omnivia_idx_governed_version_assemblies_correlation
    ON omnivia_governed_version_assemblies
        (workspace_id, assembly_id, correlation_kind, correlation_id);
CREATE UNIQUE INDEX IF NOT EXISTS
    omnivia_idx_governed_version_assemblies_version_correlation
    ON omnivia_governed_version_assemblies
        (workspace_id, assembly_id, governed_record_version_id,
         correlation_kind, correlation_id);
CREATE UNIQUE INDEX IF NOT EXISTS omnivia_idx_governed_provenance_events_identity
    ON omnivia_governed_provenance_events
        (workspace_id, assembly_id, provenance_event_id);

-- The durable half of "ordered": one ordinal value per parent stream. The index
-- refuses a duplicate and the INSERT trigger refuses a non-positive one, so the
-- ordinal is a total order over its stream that no clock collision can disturb.
--
-- The assembly ordinal is scoped to the *correlation parent*, not to the record and
-- not to the workspace: two versions produced under one audit are ordered against
-- each other, and ordinals from different parents are never compared, because they
-- were never counting the same thing.
CREATE UNIQUE INDEX IF NOT EXISTS
    omnivia_idx_governed_version_assemblies_append_ordinal
    ON omnivia_governed_version_assemblies
        (workspace_id, correlation_kind, correlation_id, append_ordinal);
CREATE UNIQUE INDEX IF NOT EXISTS omnivia_idx_governed_provenance_events_sequence
    ON omnivia_governed_provenance_events
        (workspace_id, assembly_id, provenance_sequence);
CREATE UNIQUE INDEX IF NOT EXISTS omnivia_idx_governed_version_evidence_links_ordinal
    ON omnivia_governed_version_evidence_links
        (workspace_id, assembly_id, provenance_event_id, link_ordinal);

-- One seal per assembly and one per version, and the three separate supersession
-- rules that are easy to conflate: one edge per assembly, no branching source and no
-- converging target. Each is a distinct index because each is a distinct claim, and
-- a single wider index would let two of them fail silently while the third held.
CREATE UNIQUE INDEX IF NOT EXISTS omnivia_idx_governed_version_seals_assembly
    ON omnivia_governed_version_seals (workspace_id, assembly_id);
CREATE UNIQUE INDEX IF NOT EXISTS omnivia_idx_governed_version_seals_version
    ON omnivia_governed_version_seals (workspace_id, governed_record_version_id);
CREATE UNIQUE INDEX IF NOT EXISTS omnivia_idx_record_supersessions_assembly
    ON omnivia_record_supersessions (workspace_id, assembly_id);
CREATE UNIQUE INDEX IF NOT EXISTS omnivia_idx_record_supersessions_source
    ON omnivia_record_supersessions (workspace_id, source_version_id);
CREATE UNIQUE INDEX IF NOT EXISTS omnivia_idx_record_supersessions_target
    ON omnivia_record_supersessions (workspace_id, target_version_id);
CREATE UNIQUE INDEX IF NOT EXISTS omnivia_idx_governed_relation_endpoints_assembly
    ON omnivia_governed_relation_endpoints (workspace_id, assembly_id);
CREATE UNIQUE INDEX IF NOT EXISTS omnivia_idx_governed_extraction_lineage_assembly
    ON omnivia_governed_extraction_lineage (workspace_id, assembly_id);
CREATE UNIQUE INDEX IF NOT EXISTS omnivia_idx_governed_legacy_lineage_assembly
    ON omnivia_governed_legacy_lineage (workspace_id, assembly_id);

-- Material read paths. Each is a question the seal trigger or a later reader
-- actually asks of an append-preserved table that only grows, and without them the
-- answer is a full scan: this record's versions in time order, this assembly's
-- events by action, which links cite one artifact, what one relation points at from
-- either end, and this record's supersession chain.
CREATE INDEX IF NOT EXISTS omnivia_idx_governed_version_assemblies_record
    ON omnivia_governed_version_assemblies
        (workspace_id, governed_record_id, recorded_at_us);
CREATE INDEX IF NOT EXISTS omnivia_idx_governed_provenance_events_action
    ON omnivia_governed_provenance_events (workspace_id, assembly_id, action);
CREATE INDEX IF NOT EXISTS omnivia_idx_governed_version_evidence_links_evidence
    ON omnivia_governed_version_evidence_links (workspace_id, evidence_id);
CREATE INDEX IF NOT EXISTS omnivia_idx_governed_relation_endpoints_source
    ON omnivia_governed_relation_endpoints
        (workspace_id, relation_type, source_record_id, source_version_id);
CREATE INDEX IF NOT EXISTS omnivia_idx_governed_relation_endpoints_target
    ON omnivia_governed_relation_endpoints
        (workspace_id, relation_type, target_record_id, target_version_id);
CREATE INDEX IF NOT EXISTS omnivia_idx_record_supersessions_record
    ON omnivia_record_supersessions (workspace_id, governed_record_id);

-- Twenty-seven statement triggers: nine workspace tables, three statement classes
-- each. The set is exact, because a table can look guarded while one statement class
-- walks past it. With the three catalogue guards created above that is thirty
-- triggers, and this migration creates no other.
--
-- INSERT carries the complete predicate `0005` arrived at -- the connection-local
-- authority function, the guard row, the authoritative workspace state and the lease
-- that agrees with both -- plus the workspace binding, so a row cannot be written
-- into a workspace other than the one this database is. It is evaluated inside the
-- writing transaction, so an owner whose generation was taken over between BEGIN and
-- COMMIT fails on the predicate rather than committing under authority it lost. A
-- stock `sqlite3` client fails on `omnivia_service_writer()` before a row is touched,
-- because that function is connection-local and cannot be created by writing rows.
--
-- Unlike `0008` these bodies carry no `WHEN` gate, for the reason the header gives:
-- the seal predicate is too large to state twice without the two copies becoming a
-- thing a reviewer has to diff. Behaviour is identical either way. The authority
-- clause stays first in every body so a writer with no authority hears about
-- authority rather than about an invariant it was never allowed to reach.
--
-- The six component tables each refuse an insert once their assembly is sealed. That
-- is what makes the seal final rather than merely last: a version cannot grow a new
-- event, link, endpoint or edge after it became authoritative, so what the
-- authoritative view shows is what was checked.
--
-- UPDATE and DELETE carry no predicate at all. There is no condition under which
-- rewriting or removing a governed identity, a version, its lineage, its provenance,
-- its evidence, its endpoints, its supersession or its seal is correct, so there is
-- no condition to write down -- including for the current fenced owner. A correction
-- is a new version.

CREATE TRIGGER IF NOT EXISTS omnivia_guard_governed_records_insert
BEFORE INSERT ON omnivia_governed_records
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_governed_records')
    WHERE omnivia_service_writer() IS NOT 1
       OR NOT EXISTS (
        SELECT 1
        FROM omnivia_mutation_guard g
        JOIN omnivia_workspace_state s ON s.singleton = 1
        JOIN omnivia_workspace_lease l ON l.singleton = 1
        WHERE g.singleton = 1
          AND g.fencing_generation = s.fencing_generation
          AND g.workspace_id       = s.workspace_id
          AND l.fencing_generation = g.fencing_generation
          AND l.workspace_id       = g.workspace_id
          AND l.service_instance_id = g.service_instance_id
          AND l.lifecycle IN ('acquiring', 'held', 'draining')
    )
       OR NEW.workspace_id IS NOT (
        SELECT workspace_id FROM omnivia_workspace_state WHERE singleton = 1
    );
    SELECT RAISE(ABORT, 'omnivia: omnivia_governed_records (workspace_id, governed_record_id) identity is immutable; duplicate insert refused')
    WHERE EXISTS (
        SELECT 1 FROM omnivia_governed_records
        WHERE workspace_id = NEW.workspace_id
          AND governed_record_id = NEW.governed_record_id
    );
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_governed_records_update
BEFORE UPDATE ON omnivia_governed_records
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_governed_records is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_governed_records_delete
BEFORE DELETE ON omnivia_governed_records
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_governed_records is append-only; DELETE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_governed_version_assemblies_insert
BEFORE INSERT ON omnivia_governed_version_assemblies
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_governed_version_assemblies')
    WHERE omnivia_service_writer() IS NOT 1
       OR NOT EXISTS (
        SELECT 1
        FROM omnivia_mutation_guard g
        JOIN omnivia_workspace_state s ON s.singleton = 1
        JOIN omnivia_workspace_lease l ON l.singleton = 1
        WHERE g.singleton = 1
          AND g.fencing_generation = s.fencing_generation
          AND g.workspace_id       = s.workspace_id
          AND l.fencing_generation = g.fencing_generation
          AND l.workspace_id       = g.workspace_id
          AND l.service_instance_id = g.service_instance_id
          AND l.lifecycle IN ('acquiring', 'held', 'draining')
    )
       OR NEW.workspace_id IS NOT (
        SELECT workspace_id FROM omnivia_workspace_state WHERE singleton = 1
    );
    SELECT RAISE(ABORT, 'omnivia: omnivia_governed_version_assemblies.assembly_id identity is immutable; duplicate insert refused')
    WHERE EXISTS (
        SELECT 1 FROM omnivia_governed_version_assemblies
        WHERE workspace_id = NEW.workspace_id AND assembly_id = NEW.assembly_id
    );
    SELECT RAISE(ABORT, 'omnivia: omnivia_governed_version_assemblies.governed_record_version_id identity is immutable; duplicate insert refused')
    WHERE EXISTS (
        SELECT 1 FROM omnivia_governed_version_assemblies
        WHERE workspace_id = NEW.workspace_id
          AND governed_record_version_id = NEW.governed_record_version_id
    );
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_governed_version_assemblies_update
BEFORE UPDATE ON omnivia_governed_version_assemblies
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_governed_version_assemblies is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_governed_version_assemblies_delete
BEFORE DELETE ON omnivia_governed_version_assemblies
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_governed_version_assemblies is append-only; DELETE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_governed_extraction_lineage_insert
BEFORE INSERT ON omnivia_governed_extraction_lineage
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_governed_extraction_lineage')
    WHERE omnivia_service_writer() IS NOT 1
       OR NOT EXISTS (
        SELECT 1
        FROM omnivia_mutation_guard g
        JOIN omnivia_workspace_state s ON s.singleton = 1
        JOIN omnivia_workspace_lease l ON l.singleton = 1
        WHERE g.singleton = 1
          AND g.fencing_generation = s.fencing_generation
          AND g.workspace_id       = s.workspace_id
          AND l.fencing_generation = g.fencing_generation
          AND l.workspace_id       = g.workspace_id
          AND l.service_instance_id = g.service_instance_id
          AND l.lifecycle IN ('acquiring', 'held', 'draining')
    )
       OR NEW.workspace_id IS NOT (
        SELECT workspace_id FROM omnivia_workspace_state WHERE singleton = 1
    );
    SELECT RAISE(ABORT, 'omnivia: omnivia_governed_extraction_lineage cannot be added after the version is sealed')
    WHERE EXISTS (
        SELECT 1 FROM omnivia_governed_version_seals
        WHERE workspace_id = NEW.workspace_id AND assembly_id = NEW.assembly_id
    );
    SELECT RAISE(ABORT, 'omnivia: omnivia_governed_extraction_lineage (workspace_id, assembly_id) identity is immutable; duplicate insert refused')
    WHERE EXISTS (
        SELECT 1 FROM omnivia_governed_extraction_lineage
        WHERE workspace_id = NEW.workspace_id AND assembly_id = NEW.assembly_id
    );
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_governed_extraction_lineage_update
BEFORE UPDATE ON omnivia_governed_extraction_lineage
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_governed_extraction_lineage is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_governed_extraction_lineage_delete
BEFORE DELETE ON omnivia_governed_extraction_lineage
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_governed_extraction_lineage is append-only; DELETE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_governed_legacy_lineage_insert
BEFORE INSERT ON omnivia_governed_legacy_lineage
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_governed_legacy_lineage')
    WHERE omnivia_service_writer() IS NOT 1
       OR NOT EXISTS (
        SELECT 1
        FROM omnivia_mutation_guard g
        JOIN omnivia_workspace_state s ON s.singleton = 1
        JOIN omnivia_workspace_lease l ON l.singleton = 1
        WHERE g.singleton = 1
          AND g.fencing_generation = s.fencing_generation
          AND g.workspace_id       = s.workspace_id
          AND l.fencing_generation = g.fencing_generation
          AND l.workspace_id       = g.workspace_id
          AND l.service_instance_id = g.service_instance_id
          AND l.lifecycle IN ('acquiring', 'held', 'draining')
    )
       OR NEW.workspace_id IS NOT (
        SELECT workspace_id FROM omnivia_workspace_state WHERE singleton = 1
    );
    SELECT RAISE(ABORT, 'omnivia: omnivia_governed_legacy_lineage cannot be added after the version is sealed')
    WHERE EXISTS (
        SELECT 1 FROM omnivia_governed_version_seals
        WHERE workspace_id = NEW.workspace_id AND assembly_id = NEW.assembly_id
    );
    SELECT RAISE(ABORT, 'omnivia: omnivia_governed_legacy_lineage (workspace_id, assembly_id) identity is immutable; duplicate insert refused')
    WHERE EXISTS (
        SELECT 1 FROM omnivia_governed_legacy_lineage
        WHERE workspace_id = NEW.workspace_id AND assembly_id = NEW.assembly_id
    );
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_governed_legacy_lineage_update
BEFORE UPDATE ON omnivia_governed_legacy_lineage
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_governed_legacy_lineage is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_governed_legacy_lineage_delete
BEFORE DELETE ON omnivia_governed_legacy_lineage
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_governed_legacy_lineage is append-only; DELETE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_governed_provenance_events_insert
BEFORE INSERT ON omnivia_governed_provenance_events
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_governed_provenance_events')
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
    SELECT RAISE(ABORT, 'omnivia: provenance event cannot be added after the version is sealed')
    WHERE EXISTS (
        SELECT 1 FROM omnivia_governed_version_seals
        WHERE workspace_id = NEW.workspace_id AND assembly_id = NEW.assembly_id
    );
    SELECT RAISE(ABORT, 'omnivia: provenance event identity is immutable; duplicate insert refused')
    WHERE EXISTS (
        SELECT 1 FROM omnivia_governed_provenance_events
        WHERE workspace_id = NEW.workspace_id
          AND provenance_event_id = NEW.provenance_event_id
    );
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_governed_provenance_events_update
BEFORE UPDATE ON omnivia_governed_provenance_events
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_governed_provenance_events is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_governed_provenance_events_delete
BEFORE DELETE ON omnivia_governed_provenance_events
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_governed_provenance_events is append-only; DELETE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_governed_version_evidence_links_insert
BEFORE INSERT ON omnivia_governed_version_evidence_links
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_governed_version_evidence_links')
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
    SELECT RAISE(ABORT, 'omnivia: evidence link cannot be added after the version is sealed')
    WHERE EXISTS (
        SELECT 1 FROM omnivia_governed_version_seals
        WHERE workspace_id = NEW.workspace_id AND assembly_id = NEW.assembly_id
    );
    SELECT RAISE(ABORT, 'omnivia: duplicate logical evidence link refused')
    WHERE EXISTS (
        SELECT 1
        FROM omnivia_governed_version_evidence_links l
        WHERE l.workspace_id = NEW.workspace_id
          AND l.assembly_id = NEW.assembly_id
          AND l.provenance_event_id = NEW.provenance_event_id
          AND l.evidence_id = NEW.evidence_id
          AND l.normalized_record_id IS NEW.normalized_record_id
          AND l.normalized_span_id IS NEW.normalized_span_id
    );
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_governed_version_evidence_links_update
BEFORE UPDATE ON omnivia_governed_version_evidence_links
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_governed_version_evidence_links is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_governed_version_evidence_links_delete
BEFORE DELETE ON omnivia_governed_version_evidence_links
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_governed_version_evidence_links is append-only; DELETE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_governed_relation_endpoints_insert
BEFORE INSERT ON omnivia_governed_relation_endpoints
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_governed_relation_endpoints')
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
    SELECT RAISE(ABORT, 'omnivia: relation endpoints cannot be added after the version is sealed')
    WHERE EXISTS (
        SELECT 1 FROM omnivia_governed_version_seals
        WHERE workspace_id = NEW.workspace_id AND assembly_id = NEW.assembly_id
    );
    SELECT RAISE(ABORT, 'omnivia: relation endpoint identity is immutable; duplicate insert refused')
    WHERE EXISTS (
        SELECT 1 FROM omnivia_governed_relation_endpoints
        WHERE workspace_id = NEW.workspace_id AND assembly_id = NEW.assembly_id
    );
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_governed_relation_endpoints_update
BEFORE UPDATE ON omnivia_governed_relation_endpoints
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_governed_relation_endpoints is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_governed_relation_endpoints_delete
BEFORE DELETE ON omnivia_governed_relation_endpoints
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_governed_relation_endpoints is append-only; DELETE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_record_supersessions_insert
BEFORE INSERT ON omnivia_record_supersessions
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_record_supersessions')
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
    SELECT RAISE(ABORT, 'omnivia: supersession cannot be added after the version is sealed')
    WHERE EXISTS (
        SELECT 1 FROM omnivia_governed_version_seals
        WHERE workspace_id = NEW.workspace_id AND assembly_id = NEW.assembly_id
    );
    SELECT RAISE(ABORT, 'omnivia: supersession identity is immutable; duplicate insert refused')
    WHERE EXISTS (
        SELECT 1 FROM omnivia_record_supersessions
        WHERE workspace_id = NEW.workspace_id
          AND supersession_id = NEW.supersession_id
    );
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_record_supersessions_update
BEFORE UPDATE ON omnivia_record_supersessions
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_record_supersessions is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_record_supersessions_delete
BEFORE DELETE ON omnivia_record_supersessions
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_record_supersessions is append-only; DELETE is never permitted');
END;

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
                    'reconciliation.supports'
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
               OR (r.relation_type <> 'reconciliation.supports'
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

CREATE TRIGGER IF NOT EXISTS omnivia_guard_governed_version_seals_update
BEFORE UPDATE ON omnivia_governed_version_seals
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_governed_version_seals is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_governed_version_seals_delete
BEFORE DELETE ON omnivia_governed_version_seals
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_governed_version_seals is append-only; DELETE is never permitted');
END;

CREATE VIEW IF NOT EXISTS omnivia_authoritative_governed_versions AS
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
    a.content_json,
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
