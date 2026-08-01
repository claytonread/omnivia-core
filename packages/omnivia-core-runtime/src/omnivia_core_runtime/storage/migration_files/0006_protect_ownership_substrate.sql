-- Substrate tables refuse writers this runtime did not create (T-0629F, SRB-11).
--
-- The substrate carries no guard triggers on purpose: a trigger on
-- `omnivia_mutation_guard` requiring an open guard would make opening one impossible.
-- That reasoning is sound and it was over-applied. "Cannot require a guard" is not
-- "cannot require anything": the substrate can still require the writer to be this
-- runtime, which is a different question and has no such circularity.
--
-- Without it, guarded data tables failed closed for a stock `sqlite3` client while
-- the rows that *govern* them stayed open: `UPDATE omnivia_workspace_lease SET
-- lifecycle = 'released'` from outside committed happily, and so did edits to the
-- guard row and the migration ledger. The weaker half of the boundary was the half
-- that decides what the stronger half permits.
--
-- The predicate is only the connection-authority check. Anything more would
-- reintroduce the circularity: `acquire_lease` and `open_guard` are what establish
-- authority, so they cannot be gated on holding it.

CREATE TRIGGER IF NOT EXISTS omnivia_substrate_omnivia_migration_attempts_insert
BEFORE INSERT ON omnivia_migration_attempts
WHEN omnivia_service_writer() IS NOT 1
BEGIN
    SELECT RAISE(ABORT, 'omnivia: INSERT on omnivia_migration_attempts from outside the runtime');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_substrate_omnivia_migration_attempts_update
BEFORE UPDATE ON omnivia_migration_attempts
WHEN omnivia_service_writer() IS NOT 1
BEGIN
    SELECT RAISE(ABORT, 'omnivia: UPDATE on omnivia_migration_attempts from outside the runtime');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_substrate_omnivia_migration_attempts_delete
BEFORE DELETE ON omnivia_migration_attempts
WHEN omnivia_service_writer() IS NOT 1
BEGIN
    SELECT RAISE(ABORT, 'omnivia: DELETE on omnivia_migration_attempts from outside the runtime');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_substrate_omnivia_mutation_guard_insert
BEFORE INSERT ON omnivia_mutation_guard
WHEN omnivia_service_writer() IS NOT 1
BEGIN
    SELECT RAISE(ABORT, 'omnivia: INSERT on omnivia_mutation_guard from outside the runtime');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_substrate_omnivia_mutation_guard_update
BEFORE UPDATE ON omnivia_mutation_guard
WHEN omnivia_service_writer() IS NOT 1
BEGIN
    SELECT RAISE(ABORT, 'omnivia: UPDATE on omnivia_mutation_guard from outside the runtime');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_substrate_omnivia_mutation_guard_delete
BEFORE DELETE ON omnivia_mutation_guard
WHEN omnivia_service_writer() IS NOT 1
BEGIN
    SELECT RAISE(ABORT, 'omnivia: DELETE on omnivia_mutation_guard from outside the runtime');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_substrate_omnivia_schema_migrations_insert
BEFORE INSERT ON omnivia_schema_migrations
WHEN omnivia_service_writer() IS NOT 1
BEGIN
    SELECT RAISE(ABORT, 'omnivia: INSERT on omnivia_schema_migrations from outside the runtime');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_substrate_omnivia_schema_migrations_update
BEFORE UPDATE ON omnivia_schema_migrations
WHEN omnivia_service_writer() IS NOT 1
BEGIN
    SELECT RAISE(ABORT, 'omnivia: UPDATE on omnivia_schema_migrations from outside the runtime');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_substrate_omnivia_schema_migrations_delete
BEFORE DELETE ON omnivia_schema_migrations
WHEN omnivia_service_writer() IS NOT 1
BEGIN
    SELECT RAISE(ABORT, 'omnivia: DELETE on omnivia_schema_migrations from outside the runtime');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_substrate_omnivia_workspace_lease_insert
BEFORE INSERT ON omnivia_workspace_lease
WHEN omnivia_service_writer() IS NOT 1
BEGIN
    SELECT RAISE(ABORT, 'omnivia: INSERT on omnivia_workspace_lease from outside the runtime');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_substrate_omnivia_workspace_lease_update
BEFORE UPDATE ON omnivia_workspace_lease
WHEN omnivia_service_writer() IS NOT 1
BEGIN
    SELECT RAISE(ABORT, 'omnivia: UPDATE on omnivia_workspace_lease from outside the runtime');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_substrate_omnivia_workspace_lease_delete
BEFORE DELETE ON omnivia_workspace_lease
WHEN omnivia_service_writer() IS NOT 1
BEGIN
    SELECT RAISE(ABORT, 'omnivia: DELETE on omnivia_workspace_lease from outside the runtime');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_substrate_omnivia_workspace_open_events_insert
BEFORE INSERT ON omnivia_workspace_open_events
WHEN omnivia_service_writer() IS NOT 1
BEGIN
    SELECT RAISE(ABORT, 'omnivia: INSERT on omnivia_workspace_open_events from outside the runtime');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_substrate_omnivia_workspace_open_events_update
BEFORE UPDATE ON omnivia_workspace_open_events
WHEN omnivia_service_writer() IS NOT 1
BEGIN
    SELECT RAISE(ABORT, 'omnivia: UPDATE on omnivia_workspace_open_events from outside the runtime');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_substrate_omnivia_workspace_open_events_delete
BEFORE DELETE ON omnivia_workspace_open_events
WHEN omnivia_service_writer() IS NOT 1
BEGIN
    SELECT RAISE(ABORT, 'omnivia: DELETE on omnivia_workspace_open_events from outside the runtime');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_substrate_omnivia_workspace_state_insert
BEFORE INSERT ON omnivia_workspace_state
WHEN omnivia_service_writer() IS NOT 1
BEGIN
    SELECT RAISE(ABORT, 'omnivia: INSERT on omnivia_workspace_state from outside the runtime');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_substrate_omnivia_workspace_state_update
BEFORE UPDATE ON omnivia_workspace_state
WHEN omnivia_service_writer() IS NOT 1
BEGIN
    SELECT RAISE(ABORT, 'omnivia: UPDATE on omnivia_workspace_state from outside the runtime');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_substrate_omnivia_workspace_state_delete
BEFORE DELETE ON omnivia_workspace_state
WHEN omnivia_service_writer() IS NOT 1
BEGIN
    SELECT RAISE(ABORT, 'omnivia: DELETE on omnivia_workspace_state from outside the runtime');
END;
