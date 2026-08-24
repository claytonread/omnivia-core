"""Runtime-specific integrity evidence (RT-701).

Generic backup and inventory machinery proves a SQLite file copied and verified
cleanly. It says nothing about whether the canonical Agent Runtime records the file
holds are consistent with themselves: a restored file can pass `PRAGMA
integrity_check` and a byte-for-byte inventory match while still, say, holding a
projection that a full replay of its own canonical events would not reproduce. This
module is the narrow seam that checks the runtime-specific claims RT-701 needs
evidence for, on top of -- never instead of -- the generic checks `backup` and
`inventory` already provide.

:func:`check_runtime_integrity` fails closed: each of its checks raises
:class:`RuntimeIntegrityError` rather than returning a problem a caller could ignore,
so a corrupted or drifted database is refused rather than silently reported on.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from omnivia_core_runtime.storage.agent_runtime import read_run, read_workspace_run_ids
from omnivia_core_runtime.storage.connection import (
    StorageError,
    fingerprint_schema,
    foreign_key_check,
    integrity_check,
)
from omnivia_core_runtime.storage.migrations import (
    applied_migrations,
    canonical_schema_fingerprint,
    load_migrations,
)
from omnivia_core_runtime.storage.projections.runtime_run_summary import (
    runtime_run_summary_projection_digest,
    runtime_run_summary_replay_digest,
)


class RuntimeIntegrityError(StorageError):
    """A runtime database failed one of the RT-701 integrity checks."""


@dataclass(frozen=True, slots=True)
class RuntimeIntegrityReport:
    """What one open connection can honestly report about its own runtime state.

    Deliberately excludes anything about which service instance or fencing
    generation produced it: two reports built from logically equal data -- the
    pre-destruction workspace and its restored replacement, taken under two
    different successor identities -- must compare equal, which is exactly what a
    restored-equality proof needs.
    """

    run_count: int
    run_ids: tuple[str, ...]
    schema_digest: str
    migration_ledger: tuple[tuple[int, str], ...]
    projection_digest: str


def check_runtime_integrity(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
) -> RuntimeIntegrityReport:
    """Prove the runtime state this connection holds is internally consistent.

    Five checks, in order, each failing closed:

    * `PRAGMA integrity_check` -- the page-level structure is sound.
    * `PRAGMA foreign_key_check` -- declared references are not broken.
    * The applied migration ledger's checksums match the packaged migrations that
      pin them, and the live schema matches the packaged canonical fingerprint.
    * A full replay of every canonical event reproduces the exact digest the
      incrementally maintained projection already holds -- proving replay equality,
      not merely that a projection row exists. This is deliberately non-mutating:
      drift is refused without repairing the projection in place.
    * Every run's canonical documents decode and verify through `read_run`, the same
      path any real reader uses, rather than being trusted as raw rows.
    """
    problems = integrity_check(connection)
    if problems:
        raise RuntimeIntegrityError(
            f"runtime database failed its integrity check: {problems}"
        )

    references = foreign_key_check(connection)
    if references:
        raise RuntimeIntegrityError(
            f"runtime database failed its foreign-key check: {references}"
        )

    schema = fingerprint_schema(connection)
    expected_schema = canonical_schema_fingerprint()
    if not schema.matches(expected_schema):
        raise RuntimeIntegrityError(
            "runtime database schema does not match the packaged canonical schema: "
            f"live {schema.digest[:12]}…, packaged {expected_schema.digest[:12]}…"
        )

    pinned = {migration.version: migration.checksum for migration in load_migrations()}
    ledger = applied_migrations(connection)
    if set(ledger) != set(pinned):
        missing = sorted(set(pinned) - set(ledger))
        extra = sorted(set(ledger) - set(pinned))
        raise RuntimeIntegrityError(
            f"runtime migration ledger is not current: missing {missing}, extra {extra}"
        )
    for version, checksum in ledger.items():
        expected = pinned[version]
        if expected != checksum:
            raise RuntimeIntegrityError(
                f"migration {version} has changed since it was applied: recorded "
                f"{checksum[:12]}…, packaged {expected[:12]}…"
            )

    live_digest = runtime_run_summary_projection_digest(
        connection, workspace_id=workspace_id
    )
    try:
        replay = runtime_run_summary_replay_digest(
            connection, workspace_id=workspace_id
        )
    except StorageError as error:
        raise RuntimeIntegrityError(
            "runtime Run summary projection cannot be replayed from canonical events"
        ) from error
    if replay.build_digest != live_digest:
        raise RuntimeIntegrityError(
            "a full replay of the canonical event stream does not reproduce the "
            "live Run summary projection"
        )

    run_ids = read_workspace_run_ids(connection, workspace_id=workspace_id)
    for run_id in run_ids:
        try:
            snapshot = read_run(connection, workspace_id=workspace_id, run_id=run_id)
        except StorageError as error:
            raise RuntimeIntegrityError(
                f"run {run_id!r} failed canonical runtime document verification"
            ) from error
        if snapshot is None:
            raise RuntimeIntegrityError(f"run {run_id!r} vanished while re-reading it")

    return RuntimeIntegrityReport(
        run_count=len(run_ids),
        run_ids=run_ids,
        schema_digest=schema.digest,
        migration_ledger=tuple(sorted(ledger.items())),
        projection_digest=live_digest,
    )


__all__ = [
    "RuntimeIntegrityError",
    "RuntimeIntegrityReport",
    "check_runtime_integrity",
]
