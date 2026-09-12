-- Dedicated installed-MCP principals, their exact rights and their authoring intent
-- (Gate B, R004 section 9.1-9.3).
--
-- Two tables and one parent index, added to the installation catalogue rather than to
-- any workspace, for the same reason `0001` gave: a workspace is portable and this is
-- not. Which host was configured, under which principal, with which credential verifier
-- and which rights, is a fact about *this machine's installation*. Copying a workspace
-- must not carry an MCP credential's authority with it.
--
--   omnivia_installation_mcp_setups   one active-or-revoked setup per supported host
--   omnivia_installation_mcp_grants   the exact rights that setup holds, one per row
--
-- Credential. No secret has a column here and none ever may. What is stored is a random
-- per-setup salt and `sha256:<salt||secret>`, which verifies a presented bearer and
-- produces nothing usable to whoever reads this file. `credential_reference` beside it is
-- an opaque public name -- it appears in status output and in host configuration, it is
-- not the credential, and nothing authenticates with it.
--
-- Rights. `omnivia_installation_mcp_grants` is one row per granted operation, scope,
-- purpose or capability rather than one JSON document per setup. A row cannot hold a
-- wildcard -- the CHECK refuses `*` and `?` outright -- and no reader has to agree with
-- any writer about how a policy document is spelled before it can tell what was granted.
-- R004 section 9.1 forbids a wildcard operation, scope, capability, workspace or purpose
-- grant, and this is that rule as a constraint rather than as a convention.
--
-- Rotation and revocation. `setup_generation` increases on every change and the grant
-- rows carry the generation they were issued under, so a policy read is `WHERE
-- setup_generation = <the setup's current one>` and yesterday's rights are historical
-- rather than live. The UPDATE trigger requires a strictly greater generation *and* a
-- different credential digest on every change, so no path through this schema can
-- re-provision or revoke a host while leaving the previous bearer able to authenticate.
-- The grant table is append-only, so what was granted before is still readable evidence.
--
-- Authoring intent. `authoring_intent` and `profile` are locked to each other by CHECK:
-- the authoring profile exists only with explicit intent recorded, and intent is
-- meaningless without it. R004 section 9.3's rule -- that `mutation_enabled: true` in a
-- public file is a ceiling and never an authorisation -- has its floor here, and the
-- floor is a stored fact a file edit cannot reach.
--
-- The same conventions as `0001`: every comment sits between statements, never inside
-- one, so the applied schema matches the canonical fingerprint byte for byte; timestamps
-- are signed 64-bit UTC microseconds with `typeof(...) = 'integer'`; digests are
-- `sha256:<64 lowercase hex>`; text is bounded at 128 characters; and every write
-- predicate names the connection-local writer function and the current owner's exact
-- installation id and fencing generation.

-- `0001` declared its composite parent keys by name, but the workspace inventory only
-- needed a single-column primary key at the time. A setup's workspace binding is a
-- composite reference -- this workspace, in *this* installation -- so the parent key it
-- requires is declared here, by name, so it reaches the canonical fingerprint.
CREATE UNIQUE INDEX IF NOT EXISTS omnivia_idx_installation_workspaces_id
    ON omnivia_installation_workspaces (workspace_id, installation_id);

-- One dedicated MCP setup per supported host. The row is the whole durable authority: a
-- server that holds a credential has exactly the rights this row and its grants say it
-- has, read fresh, with no cached session able to outlive a revocation.
--
-- `principal_id` is minted by the service and is required to carry the `mcp-` prefix, so
-- the service worker identity, an installation secret and a human's own identifier are
-- refused by the database rather than by the care of whoever calls it -- R004 section
-- 9.1's "MUST NOT reuse" as a constraint.
--
-- `workspace_id` is a composite foreign key into the authorised inventory, so a setup
-- cannot name a workspace this installation never created. A caller that could bind a
-- principal to an unknown workspace id would be minting authority over something the
-- catalogue never authorised.
CREATE TABLE IF NOT EXISTS omnivia_installation_mcp_setups (
    setup_id             TEXT    NOT NULL PRIMARY KEY,
    installation_id      TEXT    NOT NULL,
    host                 TEXT    NOT NULL,
    workspace_id         TEXT    NOT NULL,
    principal_id         TEXT    NOT NULL,
    profile              TEXT    NOT NULL,
    authoring_intent     INTEGER NOT NULL,
    credential_reference TEXT    NOT NULL,
    credential_salt      TEXT    NOT NULL,
    credential_digest    TEXT    NOT NULL,
    status               TEXT    NOT NULL,
    setup_generation     INTEGER NOT NULL,
    fencing_generation   INTEGER NOT NULL,
    created_at_us        INTEGER NOT NULL,
    updated_at_us        INTEGER NOT NULL,
    revoked_at_us        INTEGER,

    CHECK (length(setup_id)        BETWEEN 1 AND 128),
    CHECK (length(installation_id) BETWEEN 1 AND 128),
    CHECK (host IN ('claude-code', 'codex')),
    CHECK (length(workspace_id)    BETWEEN 1 AND 128),
    CHECK (length(principal_id) BETWEEN 5 AND 128 AND principal_id GLOB 'mcp-*'),
    CHECK (profile IN ('restricted', 'authoring')),
    CHECK (typeof(authoring_intent) = 'integer' AND authoring_intent IN (0, 1)),
    CHECK ((profile = 'authoring') = (authoring_intent = 1)),
    CHECK (
        length(credential_reference) BETWEEN 1 AND 128
        AND credential_reference NOT GLOB '*[*?]*'
    ),
    CHECK (length(credential_salt) = 32 AND credential_salt NOT GLOB '*[^0-9a-f]*'),
    CHECK (
        length(credential_digest) = 71
        AND substr(credential_digest, 1, 7) = 'sha256:'
        AND substr(credential_digest, 8) NOT GLOB '*[^0-9a-f]*'
    ),
    CHECK (status IN ('active', 'revoked')),
    CHECK (
        (status = 'active' AND revoked_at_us IS NULL)
        OR (
            status = 'revoked'
            AND typeof(revoked_at_us) = 'integer'
            AND revoked_at_us > 0
        )
    ),
    CHECK (typeof(setup_generation) = 'integer' AND setup_generation > 0),
    CHECK (typeof(fencing_generation) = 'integer' AND fencing_generation > 0),
    CHECK (typeof(created_at_us) = 'integer' AND created_at_us > 0),
    CHECK (typeof(updated_at_us) = 'integer' AND updated_at_us > 0),

    FOREIGN KEY (workspace_id, installation_id)
        REFERENCES omnivia_installation_workspaces (workspace_id, installation_id)
);

-- The exact rights one setup generation holds, one right per row.
--
-- `grant_version` is present for a capability and absent for everything else, stated as
-- one CHECK over both columns so the two cannot disagree: a capability without its
-- minimum version cannot be compared against a floor, and a scope with a version would
-- be describing something scopes do not have.
--
-- Append-only, and scoped by `setup_generation`. Rotation issues a new generation's rows
-- and leaves the previous generation's in place as evidence of what was granted; nothing
-- here is edited, and nothing here is removed.
CREATE TABLE IF NOT EXISTS omnivia_installation_mcp_grants (
    grant_row_id       TEXT    NOT NULL PRIMARY KEY,
    installation_id    TEXT    NOT NULL,
    setup_id           TEXT    NOT NULL,
    setup_generation   INTEGER NOT NULL,
    grant_kind         TEXT    NOT NULL,
    grant_value        TEXT    NOT NULL,
    grant_version      TEXT,
    fencing_generation INTEGER NOT NULL,
    granted_at_us      INTEGER NOT NULL,

    CHECK (length(grant_row_id)    BETWEEN 1 AND 128),
    CHECK (length(installation_id) BETWEEN 1 AND 128),
    CHECK (length(setup_id)        BETWEEN 1 AND 128),
    CHECK (typeof(setup_generation) = 'integer' AND setup_generation > 0),
    CHECK (grant_kind IN ('operation', 'scope', 'purpose', 'capability')),
    CHECK (length(grant_value) BETWEEN 1 AND 128 AND grant_value NOT GLOB '*[*?]*'),
    CHECK (
        (grant_kind = 'capability' AND grant_version IS NOT NULL
         AND length(grant_version) BETWEEN 1 AND 32
         AND grant_version NOT GLOB '*[*?]*')
        OR (grant_kind <> 'capability' AND grant_version IS NULL)
    ),
    CHECK (typeof(fencing_generation) = 'integer' AND fencing_generation > 0),
    CHECK (typeof(granted_at_us) = 'integer' AND granted_at_us > 0),

    FOREIGN KEY (setup_id, installation_id)
        REFERENCES omnivia_installation_mcp_setups (setup_id, installation_id)
);

-- The parent key the grant table's composite foreign key needs, declared by name so it
-- reaches the fingerprint -- an implicit `sqlite_autoindex_*` would not.
CREATE UNIQUE INDEX IF NOT EXISTS omnivia_idx_installation_mcp_setups_id
    ON omnivia_installation_mcp_setups (setup_id, installation_id);

-- One setup per host, one principal, one credential reference. Each is a uniqueness the
-- database holds rather than one the service remembers to hold: two setups for a single
-- host would be two live credentials nobody could revoke as a unit, and a reused
-- principal or reference would make one host's authority readable as another's.
CREATE UNIQUE INDEX IF NOT EXISTS omnivia_idx_installation_mcp_setups_host
    ON omnivia_installation_mcp_setups (installation_id, host);
CREATE UNIQUE INDEX IF NOT EXISTS omnivia_idx_installation_mcp_setups_principal
    ON omnivia_installation_mcp_setups (installation_id, principal_id);
CREATE UNIQUE INDEX IF NOT EXISTS omnivia_idx_installation_mcp_setups_reference
    ON omnivia_installation_mcp_setups (installation_id, credential_reference);

-- One right is granted once per setup generation. A repeated row would make "the exact
-- rights" a multiset nobody could compare against the profile it was supposed to be.
CREATE UNIQUE INDEX IF NOT EXISTS omnivia_idx_installation_mcp_grants_right
    ON omnivia_installation_mcp_grants
       (setup_id, setup_generation, grant_kind, grant_value);

-- Reading a setup's live policy is always "this setup, at this generation".
CREATE INDEX IF NOT EXISTS omnivia_idx_installation_mcp_grants_generation
    ON omnivia_installation_mcp_grants (setup_id, setup_generation);

-- A setup is created by the current owner, under its exact installation and fencing
-- generation, and only ever in its first generation: a row that could be born at
-- generation seven could be a resurrection of authority somebody revoked.
CREATE TRIGGER IF NOT EXISTS omnivia_guard_installation_mcp_setups_insert
BEFORE INSERT ON omnivia_installation_mcp_setups
WHEN omnivia_installation_writer() IS NOT 1
   OR NOT EXISTS (
    SELECT 1 FROM omnivia_installation_state s
    WHERE s.singleton = 1
      AND s.owner_instance_id IS NOT NULL
      AND s.installation_id = NEW.installation_id
      AND s.fencing_generation = NEW.fencing_generation
)
   OR NEW.setup_generation IS NOT 1
   OR NEW.status IS NOT 'active'
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_installation_mcp_setups');
END;

-- The one mutable row in this migration, mutable along exactly one path.
--
-- What identifies the setup is frozen: its id, its installation and the host it was
-- configured for. Everything else -- the workspace, the principal, the profile, the
-- intent, the credential and the status -- may change, and may change only together with
-- two things. The generation must strictly increase, so every change makes the previous
-- generation's grant rows historical in the same statement that changes the row. And the
-- credential digest must differ, so no re-provision and no revocation can leave the
-- previous bearer able to authenticate -- which is what "rotation invalidates the old
-- credential" has to mean if it is to mean anything.
--
-- Revoking twice is refused here rather than treated as a second revocation: it would
-- burn a generation and rewrite a revocation timestamp for no change at all. The service
-- answers a repeated revoke from the already-revoked row instead.
CREATE TRIGGER IF NOT EXISTS omnivia_guard_installation_mcp_setups_update
BEFORE UPDATE ON omnivia_installation_mcp_setups
WHEN omnivia_installation_writer() IS NOT 1
   OR NOT EXISTS (
    SELECT 1 FROM omnivia_installation_state s
    WHERE s.singleton = 1
      AND s.owner_instance_id IS NOT NULL
      AND s.installation_id = NEW.installation_id
      AND s.fencing_generation = NEW.fencing_generation
)
   OR NEW.setup_id IS NOT OLD.setup_id
   OR NEW.installation_id IS NOT OLD.installation_id
   OR NEW.host IS NOT OLD.host
   OR NEW.created_at_us IS NOT OLD.created_at_us
   OR NEW.setup_generation <= OLD.setup_generation
   OR NEW.credential_digest IS OLD.credential_digest
   OR (OLD.status = 'revoked' AND NEW.status = 'revoked')
BEGIN
    SELECT RAISE(ABORT, 'omnivia: refused UPDATE on omnivia_installation_mcp_setups');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_installation_mcp_setups_delete
BEFORE DELETE ON omnivia_installation_mcp_setups
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_installation_mcp_setups are never deleted');
END;

-- A grant row is written by the current owner, for a setup in this installation, and at
-- exactly that setup's current generation. The generation agreement is the load-bearing
-- half: without it a writer could add a right to a generation that is already live, or
-- backdate one into a generation that has already been read.
CREATE TRIGGER IF NOT EXISTS omnivia_guard_installation_mcp_grants_insert
BEFORE INSERT ON omnivia_installation_mcp_grants
WHEN omnivia_installation_writer() IS NOT 1
   OR NOT EXISTS (
    SELECT 1 FROM omnivia_installation_state s
    WHERE s.singleton = 1
      AND s.owner_instance_id IS NOT NULL
      AND s.installation_id = NEW.installation_id
      AND s.fencing_generation = NEW.fencing_generation
)
   OR NOT EXISTS (
    SELECT 1 FROM omnivia_installation_mcp_setups m
    WHERE m.setup_id = NEW.setup_id
      AND m.installation_id = NEW.installation_id
      AND m.setup_generation = NEW.setup_generation
      AND m.status = 'active'
)
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_installation_mcp_grants');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_installation_mcp_grants_update
BEFORE UPDATE ON omnivia_installation_mcp_grants
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_installation_mcp_grants is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_installation_mcp_grants_delete
BEFORE DELETE ON omnivia_installation_mcp_grants
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_installation_mcp_grants is append-only; DELETE is never permitted');
END;
