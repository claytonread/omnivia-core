"""Portable Engineering Memory restore and erasure (AC-063; EM-28, EM-29).

AC-063: "Export/restore a workspace, then revoke/remove evidence referenced by a
checkpoint and cache. History and stable identities survive authorised restore;
credentials/local leases/paths are absent; removal blocks derived/cache access
immediately."

One production workspace carries an evidence-linked accepted observation, a derived
preview projection, a closed and a still-active continuity session whose checkpoints
reference that record and evidence, and an installation checkout bound to a local path
with a snapshot capture header, stream origin and producer rows that name it. It is
exported as a portable artifact (distinct from `test_engineering_restore.py`'s
byte-faithful verified backup): the checkout mapping and every row naming an
installation or checkout are *excluded* (not placeholdered), the lifecycle history is
exactly the history that existed (nothing fabricated for the revocation), and the
artifact has no foreign-key violation. It is restored into a fresh location and the
restored workspace is driven through the production application surface: identities
and checkpoint lineage survive, no exported session is live authority, and revoking
the evidence blocks search, context-pack, citation follow-up and projection reads
immediately -- while the projection row is still in the database.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import test_blobs_staged_sources_and_evidence_migration as m2
import test_engineering_source_coverage as sc
from omnivia_core_runtime.ownership.fencing import (
    assert_guards_intact,
    fenced_transaction,
)
from omnivia_core_runtime.storage import portable
from omnivia_core_runtime.storage.backup import backup_database
from omnivia_core_runtime.storage.connection import (
    SERVICE_WRITER_FUNCTION,
    OpenMode,
    fingerprint_schema,
    foreign_key_check,
    open_database,
)
from omnivia_core_runtime.storage.inventory import capture_inventory
from omnivia_core_runtime.storage.migrations import canonical_schema_fingerprint
from omnivia_core_runtime.storage.portable import (
    DATABASE_NAME,
    MANIFEST_NAME,
    PORTABLE_FORMAT,
    PORTABLE_FORMAT_VERSION,
    PortableArtifactError,
    export_portable,
    restore_portable,
    verify_portable,
)

from omnivia_core.contracts.v1 import MutationPrecondition

WORKSPACE_ID = sc.WORKSPACE_ID
PRINCIPAL = sc.PRINCIPAL
EXPORTED_AT_US = 1_800_000_000_000_000
LOCAL_PATH = "/home/dev/app"
CHECKOUT_PATH = "/home/dev/checkout-of-app"
HOST_REF = "portable-host-conversation-secret"
TARGET = {"repository_id": sc.REPOSITORY, "snapshot_id": "esnap-a"}
BUILD = {
    "query": "provider",
    "targets": [TARGET],
    "profile": "implement",
    "applicability_mode": "current_safe",
}


class _Reopened(sc.Workspace):
    """The production surface over an already-owned (restored) workspace."""

    def __init__(self, holder: Any) -> None:
        self.holder = holder
        self.surface = sc._surface(holder)
        self._requests = 0
        self._continuity_bindings = {}


def _rows(connection: Any, sql: str) -> list[tuple[Any, ...]]:
    return [tuple(row) for row in connection.execute(sql).fetchall()]


_CHECKPOINTS = (
    "SELECT workspace_id, checkpoint_id, session_id, sequence, parent_checkpoint_id, "
    "checkpoint_kind, payload_json, content_digest FROM omnivia_engineering_checkpoints "
    "ORDER BY session_id, sequence"
)
_SESSIONS = (
    "SELECT session_id, principal_id, binding_generation, state, last_checkpoint_sequence, "
    "last_checkpoint_id FROM omnivia_engineering_sessions ORDER BY session_id"
)
_LIFECYCLE = (
    "SELECT workspace_id, session_id, event_sequence, event_type, binding_generation, state, "
    "lease_expires_at_us, settled_at_us, prior_session_id, audit_ref "
    "FROM omnivia_engineering_session_lifecycle ORDER BY session_id, event_sequence"
)
_EXCLUDED = (
    "omnivia_engineering_source_producer_queue",
    "omnivia_engineering_source_producer_state",
    "omnivia_engineering_source_stream_origins",
    "omnivia_engineering_snapshot_captures",
    "omnivia_engineering_checkouts",
    "omnivia_engineering_session_authority",
    "omnivia_engineering_selector_attestations",
    "omnivia_engineering_handoff_grant_revocations",
    "omnivia_engineering_handoff_grants",
    "omnivia_workspace_lease",
    "omnivia_mutation_guard",
    "omnivia_workspace_open_events",
)
#: Tables the scrub rewrites or empties; every other table must survive byte for byte.
_REWRITTEN = (
    *_EXCLUDED,
    "omnivia_engineering_sessions",
    "omnivia_engineering_session_lifecycle",
    "workspaces",
    "sources",
)
_IDENTITIES = (
    "SELECT governed_record_id, governed_record_version_id, content_digest "
    "FROM omnivia_authoritative_governed_version_metadata ORDER BY governed_record_version_id"
)
_PROJECTION = "SELECT COUNT(*) FROM omnivia_engineering_preview_projection"


class _Seeded:
    """The pre-export facts the restore must reproduce."""

    def __init__(self, ws: sc.Workspace) -> None:
        m2.write(ws.holder, m2.EVIDENCE, evidence_id="evd-open", source_native_id="doc-open")
        ws.record(sc._source(1, "esnap-a", sc.FILES_A))
        open_source = {**sc.EVIDENCE_SOURCE, "source_id": "doc-open"}
        self.accepted = sc._accept(
            ws, ws.observe(sc._observation(sc._manifest(), source=open_source))
        )

        def append(session_id: str, objective: str, version: str, **extra: Any) -> str:
            payload = {
                "objective": objective,
                "checkpoint_kind": "periodic",
                "accepted_record_refs": [self.accepted],
                "observations": [
                    {
                        "statement": "The provider decision rests on the open evidence.",
                        "evidence_refs": ["evd-open"],
                        "support": "claimed",
                    }
                ],
                "unresolved_work": ["Why does restore fail?"],
            }
            result = ws.ok(
                "continuity.checkpoint.append",
                {"session_id": session_id, "payload": payload, **extra},
                mutation_precondition=MutationPrecondition(record_version=version),
            )
            return str(result["receipt"]["checkpoint_id"])

        def register(host_ref: str) -> str:
            return str(
                ws.ok(
                    "continuity.session.register",
                    {
                        "schema_version": "engineering.1",
                        "checkout_hint": LOCAL_PATH,
                        "host_session_ref": host_ref,
                    },
                )["session"]["session_id"]
            )

        # Session one: two linked checkpoints, then closed.
        self.closed_session = register("portable-host-conversation-one")
        self.first = append(self.closed_session, "Investigate the failure", "seq-0")
        self.second = append(
            self.closed_session,
            "Wrap up",
            "seq-1",
            expected_parent_sequence=1,
            parent_checkpoint_id=self.first,
        )
        ws.ok(
            "continuity.session.close",
            {"session_id": self.closed_session, "expected_sequence": 2},
            mutation_precondition=MutationPrecondition(record_version="seq-2"),
        )
        # Session two stays active, with a live registration response in hand.
        self.live_session = register(HOST_REF)
        self.live = append(self.live_session, "Continue the investigation", "seq-0")
        self.live_binding = ws.binding_for(PRINCIPAL)

        h = ws.holder
        audit_ref = h.connection.execute(
            "SELECT audit_ref FROM omnivia_engineering_sessions LIMIT 1"
        ).fetchone()[0]
        with fenced_transaction(
            h.connection, h.identity, workspace_id=WORKSPACE_ID, fencing_generation=h.generation
        ):
            h.connection.execute(
                "INSERT INTO omnivia_engineering_checkouts (workspace_id, checkout_id, "
                "repository_id, installation_id, checkout_hint, registered_at_us, "
                "last_seen_at_us, audit_ref) VALUES (?, 'echeckout-1', ?, 'inst-local', ?, "
                "1, 1, ?)",
                (WORKSPACE_ID, sc.REPOSITORY, CHECKOUT_PATH, audit_ref),
            )

        connection = h.connection
        self.checkpoints = _rows(connection, _CHECKPOINTS)
        self.lifecycle = _rows(connection, _LIFECYCLE)
        self.sessions = _rows(connection, _SESSIONS)
        self.identities = _rows(connection, _IDENTITIES)
        self.projection_rows = int(connection.execute(_PROJECTION).fetchone()[0])
        # What production actually stored: the host reference is not the caller's string
        # but a derived association correlation, and the sessions hold live authority.
        self.correlations = [
            str(row[0])
            for row in connection.execute(
                "SELECT host_session_ref FROM omnivia_engineering_sessions"
            )
        ]
        self.association_keys = [
            str(row[0])
            for row in connection.execute(
                "SELECT DISTINCT association_key FROM omnivia_engineering_session_lifecycle "
                "WHERE association_key IS NOT NULL"
            )
        ]
        assert len(self.correlations) == 2 and self.association_keys
        assert all(ref.startswith("core-association.v1:") for ref in self.correlations)
        assert connection.execute(
            "SELECT COUNT(*) FROM omnivia_engineering_session_authority "
            "WHERE state = 'active'"
        ).fetchone()[0] == 1
        assert len(self.checkpoints) == 3 and self.projection_rows >= 1


@contextlib.contextmanager
def _lifted(path: Path) -> Iterator[sqlite3.Connection]:
    """A raw connection with the guard triggers lifted, restored on exit (a test seam)."""
    connection = sqlite3.connect(path, isolation_level=None)
    connection.create_function(SERVICE_WRITER_FUNCTION, 0, lambda: 1)
    try:
        triggers = connection.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'trigger'"
        ).fetchall()
        for name, _ in triggers:
            connection.execute(f'DROP TRIGGER "{name}"')
        yield connection
        for _, sql in triggers:
            connection.execute(sql)
    finally:
        connection.close()


def _seed_installation_rows(path: Path) -> None:
    """Rows that name the installation checkout: capture header, origin, producer rows.

    Inserted with the guard triggers lifted -- their own insert predicates need a full
    capture flow -- and proved foreign-key clean, so the export's own foreign-key proof
    is meaningful: these are exactly the rows whose parent (the checkout) is removed.
    """
    with _lifted(path) as connection:
        audit_ref = connection.execute(
            "SELECT audit_ref FROM omnivia_engineering_sessions LIMIT 1"
        ).fetchone()[0]
        repository, digest, status, captured = connection.execute(
            "SELECT repository_id, manifest_digest, capture_status, captured_at_us "
            "FROM omnivia_engineering_snapshots WHERE snapshot_id = 'esnap-a'"
        ).fetchone()
        stream = connection.execute(
            "SELECT stream_id FROM omnivia_engineering_source_events WHERE snapshot_id = 'esnap-a'"
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO omnivia_engineering_snapshot_captures VALUES "
            "(?, 'esnap-a', ?, 'inst-local', 'echeckout-1', 'evd-open', ?, ?, 0, ?, ?, ?)",
            (WORKSPACE_ID, repository, digest, digest, status, captured, audit_ref),
        )
        connection.execute(
            "INSERT INTO omnivia_engineering_source_stream_origins VALUES "
            "(?, ?, ?, 'inst-local', 'echeckout-1', 1, ?)",
            (WORKSPACE_ID, stream, repository, audit_ref),
        )
        connection.execute(
            "INSERT INTO omnivia_engineering_source_producer_queue VALUES "
            "(?, 'inst-local', 'esnap-a', ?, 'echeckout-1', ?, NULL, NULL, 'pending', 1, 0, 1, "
            "NULL, NULL)",
            (WORKSPACE_ID, repository, stream),
        )
        connection.execute(
            "INSERT INTO omnivia_engineering_source_producer_state VALUES "
            "(?, 'inst-local', 'recovery', NULL, NULL, NULL, NULL, NULL, NULL, NULL, 0, 1)",
            (WORKSPACE_ID,),
        )
        assert foreign_key_check(connection) == []
        for table in _EXCLUDED[:5]:
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 1, table


@pytest.fixture
def exported(tmp_path: Path) -> Any:
    ws = sc.Workspace(tmp_path)
    try:
        seeded = _Seeded(ws)
        ws.holder.connection.close()
        _seed_installation_rows(ws.holder.path)
        artifact = tmp_path / "artifact"
        manifest = export_portable(ws.holder.path, artifact, exported_at_us=EXPORTED_AT_US)
        yield ws, seeded, artifact, manifest
    finally:
        with contextlib.suppress(Exception):
            ws.holder.connection.close()


def test_the_artifact_has_an_exact_contract_and_no_installation_local_data(
    exported: Any, tmp_path: Path
) -> None:
    ws, seeded, artifact, manifest = exported
    secrets = [*seeded.correlations, *seeded.association_keys]

    assert sorted(entry.name for entry in artifact.iterdir()) == sorted([DATABASE_NAME, MANIFEST_NAME])
    assert manifest["format"] == PORTABLE_FORMAT and manifest["format_version"] == (
        PORTABLE_FORMAT_VERSION
    )
    assert manifest["workspace_id"] == WORKSPACE_ID
    assert manifest["schema_fingerprint"] == "sha256:" + canonical_schema_fingerprint().digest
    assert manifest["sessions_revoked"] == 1
    assert json.loads((artifact / MANIFEST_NAME).read_text()) == manifest
    assert verify_portable(artifact) == manifest

    # Neither the path, the host correlation, the checkout hint nor the installation and
    # checkout identities are anywhere in the bytes -- not merely unlinked, not left
    # in page slack.
    for name in (DATABASE_NAME, MANIFEST_NAME):
        content = (artifact / name).read_bytes()
        for secret in (LOCAL_PATH, CHECKOUT_PATH, HOST_REF, "inst-local", "echeckout-1", *secrets):
            assert secret.encode() not in content, (name, secret)

    source = open_database(ws.holder.path, OpenMode.READ_ONLY)
    try:
        source_inventory = {t.name: t for t in capture_inventory(source).tables}
        source_lifecycle = _rows(source, _LIFECYCLE)
    finally:
        source.close()
    assert source_inventory["omnivia_engineering_checkouts"].row_count == 1

    connection = open_database(artifact / DATABASE_NAME, OpenMode.READ_ONLY)
    try:
        # Excluded outright: no placeholder checkout, no capture, origin or producer row.
        for table in _EXCLUDED:
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, table
        # Retained history is the source's, row for row and digest for digest: every table
        # the scrub does not rewrite, including repositories, snapshots, the snapshot file
        # index, source streams and events, evidence and audit.
        artifact_inventory = {t.name: t for t in capture_inventory(connection).tables}
        kept = [name for name in source_inventory if name not in _REWRITTEN]
        assert {"omnivia_engineering_snapshots", "omnivia_engineering_snapshot_files",
                "omnivia_engineering_source_events", "omnivia_engineering_repositories",
                "omnivia_evidence_artifacts"} <= set(kept)
        for name in kept:
            assert artifact_inventory[name].row_count == source_inventory[name].row_count, name
            assert artifact_inventory[name].content_checksum == (
                source_inventory[name].content_checksum
            ), name
        for name in ("omnivia_engineering_snapshots", "omnivia_engineering_source_events"):
            assert artifact_inventory[name].row_count > 0, name
        # The lifecycle history is exactly what existed: no event was fabricated for the
        # still-active session, and only the association key (live binding) was cleared.
        assert _rows(connection, _LIFECYCLE) == source_lifecycle == seeded.lifecycle
        assert foreign_key_check(connection) == []
        assert _rows(
            connection,
            "SELECT state, host_session_ref, checkout_hint FROM omnivia_engineering_sessions "
            "ORDER BY state",
        ) == [("closed", None, None), ("revoked", None, None)]
        # The schema and its guard triggers are exactly the canonical ones.
        assert fingerprint_schema(connection) == canonical_schema_fingerprint()
        assert_guards_intact(connection)
    finally:
        connection.close()

    # A verified backup is a different artifact: byte-faithful, installation-local, and
    # still carrying exactly what the portable export removes.
    backup = backup_database(ws.holder.path, tmp_path / "backup.sqlite").read_bytes()
    for secret in (LOCAL_PATH, CHECKOUT_PATH, *secrets):
        assert secret.encode() in backup, secret

    # Exporting only read the source: its checkout mapping and capture rows are intact.
    still = open_database(ws.holder.path, OpenMode.READ_ONLY)
    try:
        for table in _EXCLUDED[:5]:
            assert still.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 1, table
    finally:
        still.close()

    # The artifact is deterministic content: exporting the same source again yields the
    # same checksums, and the source itself was only read.
    again = export_portable(ws.holder.path, tmp_path / "again", exported_at_us=EXPORTED_AT_US)
    assert again == manifest
    assert verify_portable(tmp_path / "again") == manifest


def test_portable_round_trip_then_revocation_blocks_derived_access_immediately(
    exported: Any, tmp_path: Path
) -> None:
    _ws, seeded, artifact, _manifest = exported
    restored_path = restore_portable(artifact, tmp_path / "restored" / "workspace.sqlite")
    holder = m2.take_ownership(restored_path)
    try:
        connection = holder.connection

        # Stable identities, checkpoint lineage and digests survive, row for row.
        assert _rows(connection, _CHECKPOINTS) == seeded.checkpoints
        assert _rows(
            connection,
            "SELECT parent_checkpoint_id FROM omnivia_engineering_checkpoints "
            "WHERE checkpoint_id = '" + seeded.second + "'",
        ) == [(seeded.first,)]
        assert _rows(connection, _IDENTITIES) == seeded.identities
        sessions = _rows(connection, _SESSIONS)
        assert [(row[0], row[1], row[2]) for row in sessions] == [
            (row[0], row[1], row[2]) for row in seeded.sessions
        ]
        assert {row[0]: row[3] for row in sessions} == {
            seeded.closed_session: "closed",
            seeded.live_session: "revoked",
        }
        assert fingerprint_schema(connection) == canonical_schema_fingerprint()
        assert_guards_intact(connection)

        # No lease or authority came across: the restoring owner holds its own lease.
        assert connection.execute(
            "SELECT COUNT(*) FROM omnivia_engineering_session_authority"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT service_instance_id FROM omnivia_workspace_lease"
        ).fetchone()[0] == holder.identity.service_instance_id
        # The history is the exported history: nothing was appended for the revocation,
        # and the live session's last event is still the one it had before export.
        assert _rows(connection, _LIFECYCLE) == seeded.lifecycle
        assert [row[3] for row in seeded.lifecycle if row[1] == seeded.live_session][-1] != (
            "revoked"
        )
        assert foreign_key_check(connection) == []
        for table in _EXCLUDED[:5]:
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, table

        restored = _Reopened(holder)
        reader = sc._reader()

        # The restored workspace is writable, through a newly registered session -- the
        # only way back in, and a new session rather than a resumed one.
        fresh = restored.ok(
            "continuity.session.register", {"schema_version": "engineering.1"}
        )["session"]["session_id"]
        assert fresh not in (seeded.closed_session, seeded.live_session)
        restored.ok(
            "continuity.checkpoint.append",
            {
                "session_id": fresh,
                "payload": {"objective": "Start again", "checkpoint_kind": "periodic"},
            },
            mutation_precondition=MutationPrecondition(record_version="seq-0"),
        )
        written = _rows(connection, _CHECKPOINTS)
        assert len(written) == len(seeded.checkpoints) + 1
        assert set(seeded.checkpoints) <= set(written)

        # The exported session is not authority: its pre-export binding appends nothing,
        # closes nothing, and cannot be re-presented as a live registration.
        restored._continuity_bindings[PRINCIPAL] = seeded.live_binding
        append = {
            "session_id": seeded.live_session,
            "payload": {"objective": "Resume from a portable export", "checkpoint_kind": "periodic"},
        }
        for operation, payload in (
            ("continuity.checkpoint.append", append),
            ("continuity.session.close", {"session_id": seeded.live_session, "expected_sequence": 1}),
        ):
            code, message, _retry = restored.refused(
                operation,
                payload,
                mutation_precondition=MutationPrecondition(record_version="seq-1"),
            )
            assert (code, message) == (
                "conflict",
                "this continuity request conflicts with the session's current state",
            ), operation
        assert _rows(connection, _CHECKPOINTS) == written

        # Before revocation the derived reads serve the restored record to the reader.
        assert restored.matched("esnap-a", view="accepted", session=reader) == [
            seeded.accepted["record_id"]
        ]
        pack = restored.ok("engineering.context.build", BUILD, session=reader)["pack"]
        assert [c["record_ref"] for c in pack["citations"]] == [seeded.accepted]
        anchor = {"anchor": seeded.accepted}
        assert restored.ok("engineering.expand", anchor, session=reader)["nodes"] == [
            seeded.accepted
        ]
        projected = int(connection.execute(_PROJECTION).fetchone()[0])
        assert projected == seeded.projection_rows

        # Revocation: the evidence the checkpoint and the record cite gains the
        # restricted label. Nothing below runs a cleanup, and the derived row is still
        # in the database -- the block is on access, not on deletion.
        m2.write(
            holder,
            m2.LABELS,
            label_event_id="lbl-open",
            evidence_id="evd-open",
            label_sequence=1,
        )
        assert int(connection.execute(_PROJECTION).fetchone()[0]) == projected

        assert restored.matched("esnap-a", view="accepted", session=reader) == []
        assert restored.matched("esnap-a", view="candidates", session=reader) == []
        revoked = restored.ok("engineering.context.build", BUILD, session=reader)["pack"]
        assert revoked["sections"] == [] and revoked["citations"] == []
        followed = restored.refused("engineering.expand", anchor, session=reader)
        assert followed[0] == "not_found"
        assert seeded.accepted["record_id"] not in json.dumps(followed)

        # The checkpoint that cites the evidence is untouched history, and the owner,
        # who still holds the label, still reaches the record: revocation is access.
        assert _rows(connection, _CHECKPOINTS) == written
        assert restored.matched("esnap-a", view="accepted") == [seeded.accepted["record_id"]]
    finally:
        with contextlib.suppress(Exception):
            holder.connection.close()


def _rewrite_manifest(artifact: Path, **changes: Any) -> None:
    """Change manifest fields and recompute its own checksum, so only they fail."""
    from omnivia_core_runtime.storage import portable

    path = artifact / MANIFEST_NAME
    manifest = json.loads(path.read_text())
    manifest.update(changes)
    manifest["manifest_checksum"] = portable._manifest_checksum(manifest)
    path.write_text(json.dumps(manifest))


def _forge_live_authority(artifact: Path, statement: str) -> None:
    """A hostile artifact whose manifest is internally consistent: the guard triggers
    are lifted for one statement, the schema restored, and the content checksum and
    manifest checksum recomputed -- only the inertness proof can refuse it."""
    connection = sqlite3.connect(artifact / DATABASE_NAME, isolation_level=None)
    connection.create_function(SERVICE_WRITER_FUNCTION, 0, lambda: 1)
    try:
        triggers = connection.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'trigger'"
        ).fetchall()
        for name, _ in triggers:
            connection.execute(f'DROP TRIGGER "{name}"')
        connection.execute(statement)
        for _, sql in triggers:
            connection.execute(sql)
        inventory = capture_inventory(connection)
    finally:
        connection.close()
    _rewrite_manifest(
        artifact, content_checksum="sha256:" + inventory.content_checksum, total_rows=inventory.total_rows
    )


def _copy(artifact: Path, target: Path) -> Path:
    shutil.copytree(artifact, target)
    return target


def test_corrupt_or_incomplete_artifacts_fail_closed(exported: Any, tmp_path: Path) -> None:
    _ws, _seeded, artifact, manifest = exported
    destination = tmp_path / "never" / "workspace.sqlite"

    def refused(broken: Path, reason: str) -> None:
        with pytest.raises(PortableArtifactError, match=reason):
            verify_portable(broken)
        with pytest.raises(PortableArtifactError, match=reason):
            restore_portable(broken, destination)
        assert not destination.exists()

    missing = tmp_path / "missing"
    refused(missing, "no portable artifact")

    no_manifest = _copy(artifact, tmp_path / "no-manifest")
    (no_manifest / MANIFEST_NAME).unlink()
    refused(no_manifest, "not its two files")

    no_database = _copy(artifact, tmp_path / "no-database")
    (no_database / DATABASE_NAME).unlink()
    refused(no_database, "not its two files")

    extra = _copy(artifact, tmp_path / "extra")
    (extra / "workspace.sqlite-wal").write_bytes(b"stale frames")
    refused(extra, "not its two files")

    truncated = _copy(artifact, tmp_path / "truncated")
    data = (truncated / DATABASE_NAME).read_bytes()
    (truncated / DATABASE_NAME).write_bytes(data[: len(data) // 2])
    refused(truncated, "unusable|integrity|content")

    garbage = _copy(artifact, tmp_path / "garbage")
    (garbage / MANIFEST_NAME).write_text("{not json")
    refused(garbage, "unusable")

    tampered = _copy(artifact, tmp_path / "tampered")  # stale manifest checksum
    manifest_file = tampered / MANIFEST_NAME
    manifest_file.write_text(
        json.dumps({**manifest, "content_checksum": "sha256:" + "0" * 64})
    )
    refused(tampered, "manifest checksum")

    manifest_changes: dict[str, tuple[dict[str, Any], str]] = {
        "future-version": ({"format_version": PORTABLE_FORMAT_VERSION + 1}, "format and version"),
        "other-format": ({"format": "omnivia.something-else"}, "format and version"),
        "wrong-checksum": ({"content_checksum": "sha256:" + "0" * 64}, "content does not match"),
        "wrong-rows": ({"total_rows": 1}, "content does not match"),
        "other-workspace": ({"workspace_id": "ws-other"}, "different workspace"),
        "other-schema": (
            {"schema_fingerprint": "sha256:" + "0" * 64},
            "current workspace schema",
        ),
        "extra-field": ({"installation_secret": "x"}, "exactly the v1 fields"),
        # Strict grammar and types: booleans are not integers, checksums are exactly
        # `sha256:` and 64 lowercase hex, identifiers are bounded and well formed.
        "bool-version": ({"format_version": True}, "malformed field"),
        "bool-time": ({"exported_at_us": True}, "malformed field"),
        "zero-time": ({"exported_at_us": 0}, "malformed field"),
        "float-time": ({"exported_at_us": 1.5}, "malformed field"),
        "bool-rows": ({"total_rows": True}, "malformed field"),
        "negative-rows": ({"total_rows": -1}, "malformed field"),
        "bool-sessions": ({"sessions_revoked": False}, "malformed field"),
        "negative-sessions": ({"sessions_revoked": -1}, "malformed field"),
        "string-rows": ({"total_rows": "7"}, "malformed field"),
        "bare-content-checksum": ({"content_checksum": "0" * 64}, "malformed field"),
        "upper-content-checksum": ({"content_checksum": "sha256:" + "A" * 64}, "malformed field"),
        "short-content-checksum": ({"content_checksum": "sha256:" + "0" * 63}, "malformed field"),
        "long-schema-fingerprint": (
            {"schema_fingerprint": "sha256:" + "0" * 65},
            "malformed field",
        ),
        "null-schema-fingerprint": ({"schema_fingerprint": None}, "malformed field"),
        "empty-workspace": ({"workspace_id": ""}, "malformed field"),
        "spaced-workspace": ({"workspace_id": "ws other"}, "malformed field"),
        "long-workspace": ({"workspace_id": "w" * 129}, "malformed field"),
        "newline-workspace": ({"workspace_id": WORKSPACE_ID + "\n"}, "malformed field"),
        "numeric-workspace": ({"workspace_id": 7}, "malformed field"),
        "numeric-format": ({"format": 1}, "malformed field"),
    }
    for name, (change, reason) in manifest_changes.items():
        broken = _copy(artifact, tmp_path / name)
        _rewrite_manifest(broken, **change)
        refused(broken, reason)

    for name, statement in {
        "live-session": "UPDATE omnivia_engineering_sessions SET state = 'active' "
        "WHERE state = 'revoked'",
        "host-correlation": "UPDATE omnivia_engineering_sessions SET host_session_ref = 'host'",
        "checkout-hint": "UPDATE omnivia_engineering_sessions SET checkout_hint = '/home/dev/x'",
        "checkout-mapping": (
            "INSERT INTO omnivia_engineering_checkouts (workspace_id, checkout_id, "
            "repository_id, installation_id, checkout_hint, registered_at_us, "
            "last_seen_at_us, audit_ref) SELECT r.workspace_id, 'echeckout-9', r.repository_id, "
            "'inst-x', 'portable:echeckout-9', 1, 1, "
            "(SELECT audit_ref FROM omnivia_engineering_sessions LIMIT 1) "
            "FROM omnivia_engineering_repositories r LIMIT 1"
        ),
        "association-key": "UPDATE omnivia_engineering_session_lifecycle SET "
        "association_key = 'sha256:' || printf('%064d', 1) WHERE event_sequence = 1",
    }.items():
        forged = _copy(artifact, tmp_path / name)
        _forge_live_authority(forged, statement)
        refused(forged, "portable artifact carries")

    # A consistent checksum over a database with a dangling foreign key is refused too.
    dangling = _copy(artifact, tmp_path / "dangling")
    _forge_live_authority(dangling, "DELETE FROM omnivia_engineering_repositories")
    refused(dangling, "foreign-key violations")

    short = _copy(artifact, tmp_path / "short-manifest-checksum")
    (short / MANIFEST_NAME).write_text(json.dumps({**manifest, "manifest_checksum": "sha256:abc"}))
    refused(short, "checksum is not a sha256")

    # Raw JSON primitives the grammar must not admit.
    text = (artifact / MANIFEST_NAME).read_text()
    for name, forged_text in {
        "nan": text.replace(f'"total_rows": {manifest["total_rows"]}', '"total_rows": NaN'),
        "infinity": text.replace(
            f'"exported_at_us": {manifest["exported_at_us"]}', '"exported_at_us": Infinity'
        ),
        "duplicate-key": text.replace(
            '"format_version": 1', '"format_version": 1, "format_version": 1'
        ),
        "oversized": text + " " * 8192,
        "not-utf8": "\udcff",
    }.items():
        assert forged_text != text or name == "oversized", name
        broken = _copy(artifact, tmp_path / f"json-{name}")
        data = forged_text.encode("utf-8", errors="surrogateescape")
        (broken / MANIFEST_NAME).write_bytes(data)
        refused(broken, "unusable|too large")

    # An existing destination -- file, directory or dangling link -- is never replaced.
    with pytest.raises(PortableArtifactError):
        export_portable(_ws.holder.path, artifact, exported_at_us=EXPORTED_AT_US)
    restored = restore_portable(artifact, tmp_path / "ok" / "workspace.sqlite")
    with pytest.raises(PortableArtifactError):
        restore_portable(artifact, restored)
    assert verify_portable(artifact) == manifest


def test_symbolic_links_are_never_followed(exported: Any, tmp_path: Path) -> None:
    _ws, _seeded, artifact, _manifest = exported
    destination = tmp_path / "never" / "workspace.sqlite"

    def link(target: Path, name: Path) -> Path:
        try:
            os.symlink(target, name, target_is_directory=target.is_dir())
        except (OSError, NotImplementedError):
            pytest.skip("this host cannot create symbolic links")
        return name

    def refused(broken: Path, reason: str) -> None:
        with pytest.raises(PortableArtifactError, match=reason):
            verify_portable(broken)
        with pytest.raises(PortableArtifactError, match=reason):
            restore_portable(broken, destination)
        assert not destination.exists()

    refused(link(artifact, tmp_path / "linked-root"), "not a plain directory")

    for member in (DATABASE_NAME, MANIFEST_NAME):
        broken = _copy(artifact, tmp_path / f"linked-{member}")
        real = tmp_path / f"real-{member}"
        (broken / member).rename(real)
        link(real, broken / member)
        refused(broken, "not a plain file")

    not_a_directory = tmp_path / "file-root"
    not_a_directory.write_bytes(b"x")
    refused(not_a_directory, "not a plain directory")

    # A destination that is a dangling link still counts as present.
    dangling = link(tmp_path / "nowhere", tmp_path / "dangling-destination")
    with pytest.raises(PortableArtifactError):
        export_portable(_ws.holder.path, dangling, exported_at_us=EXPORTED_AT_US)
    with pytest.raises(PortableArtifactError):
        restore_portable(artifact, dangling)
    assert os.path.islink(dangling)


def _leftovers(parent: Path) -> list[str]:
    return sorted(p.name for p in parent.iterdir() if p.name.startswith(".portable-"))


def test_export_and_restore_never_clobber_or_delete_what_they_did_not_create(
    exported: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws, _seeded, artifact, manifest = exported
    real_verify = portable.verify_portable

    # A sibling that used to be the deterministic staging name is someone else's.
    bystander = tmp_path / ".racing.export"
    bystander.mkdir()
    (bystander / "keep").write_bytes(b"unrelated")

    # Export: the destination appears while the artifact is being built.
    racing = tmp_path / "racing"

    def verify_then_race(path: Path) -> dict[str, Any]:
        result = real_verify(path)
        racing.mkdir()
        (racing / "theirs").write_bytes(b"unrelated")
        return result

    monkeypatch.setattr(portable, "verify_portable", verify_then_race)
    with pytest.raises(PortableArtifactError, match="refusing to overwrite"):
        export_portable(ws.holder.path, racing, exported_at_us=EXPORTED_AT_US)
    monkeypatch.setattr(portable, "verify_portable", real_verify)
    assert [p.name for p in racing.iterdir()] == ["theirs"]
    assert (racing / "theirs").read_bytes() == b"unrelated"
    assert (bystander / "keep").read_bytes() == b"unrelated"
    assert _leftovers(tmp_path) == []

    # A publish that fails midway removes only what this call created.
    real_link = os.link
    calls: list[str] = []

    def failing_link(src: Any, dst: Any, **kwargs: Any) -> None:
        calls.append(str(dst))
        if len(calls) == 2:
            raise OSError("link refused")
        real_link(src, dst, **kwargs)

    monkeypatch.setattr(os, "link", failing_link)
    with pytest.raises(OSError, match="link refused"):
        export_portable(ws.holder.path, tmp_path / "halfway", exported_at_us=EXPORTED_AT_US)
    monkeypatch.setattr(os, "link", real_link)
    assert not (tmp_path / "halfway").exists()
    assert _leftovers(tmp_path) == []

    # Restore: the destination appears after the copy is verified, before it is published.
    destination = tmp_path / "restore" / "workspace.sqlite"
    destination.parent.mkdir()
    real_verify_database = portable._verify_database

    def verify_then_race_database(path: Path, expected: dict[str, Any]) -> None:
        real_verify_database(path, expected)
        if path.parent.name.startswith(".portable-restore-"):
            destination.write_bytes(b"someone else's database")

    monkeypatch.setattr(portable, "_verify_database", verify_then_race_database)
    with pytest.raises(PortableArtifactError, match="refusing to restore"):
        restore_portable(artifact, destination)
    monkeypatch.setattr(portable, "_verify_database", real_verify_database)
    assert destination.read_bytes() == b"someone else's database"
    assert _leftovers(destination.parent) == []

    # A stale WAL sidecar beside the destination is refused, not deleted.
    wal = tmp_path / "restore" / "stale.sqlite-wal"
    wal.write_bytes(b"stale frames")
    with pytest.raises(PortableArtifactError, match="refusing to restore"):
        restore_portable(artifact, tmp_path / "restore" / "stale.sqlite")
    assert wal.read_bytes() == b"stale frames"
    assert not (tmp_path / "restore" / "stale.sqlite").exists()

    # A restore whose copy fails verification publishes nothing.
    def corrupt(path: Path, expected: dict[str, Any]) -> None:
        raise PortableArtifactError("restored database does not match")

    monkeypatch.setattr(portable, "_verify_database", corrupt)
    with pytest.raises(PortableArtifactError, match="does not match"):
        restore_portable(artifact, tmp_path / "restore" / "bad.sqlite")
    monkeypatch.setattr(portable, "_verify_database", real_verify_database)
    assert not (tmp_path / "restore" / "bad.sqlite").exists()
    assert _leftovers(tmp_path / "restore") == []

    # Neither failure damaged the artifact.
    assert verify_portable(artifact) == manifest
