-- Engineering Memory legacy-import identity is workspace-local and immutable.
--
-- Migration 0009 makes each assembly unique, but it permits two assemblies to
-- claim the same legacy source version.  Import idempotency is defined by that
-- source identity, so enforce it in authoritative storage as well as in the
-- importer's bounded preflight check.
CREATE UNIQUE INDEX IF NOT EXISTS omnivia_governed_legacy_identity_uq
    ON omnivia_governed_legacy_lineage (
        workspace_id,
        legacy_source_id,
        legacy_source_version
    );
