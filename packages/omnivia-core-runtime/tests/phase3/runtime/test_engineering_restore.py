"""Engineering records survive a verified backup/restore round trip (plan PR-H2).

AC-063's storage half: one fully migrated workspace carries a recorded source
stream, an evidence-linked observation and a closed continuity session with
two checkpoints; a verified backup is taken, restored into a fresh location,
reopened, and every engineering identity survives with its exact digests --
checkpoints by `(workspace_id, checkpoint_id)` sequence and digest, sessions by
state, repositories and snapshots by row, and the observation still reachable
through the production surface on the restored database.

Nothing here re-runs migrations on the restored bytes beyond what reopening
already does: the restore is byte-for-byte from the verified backup, which is
the property the release evidence rests on.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any

import test_engineering_source_coverage as sc
from omnivia_core_runtime.storage.backup import (
    backup_database,
    new_attempt_id,
    restore_backup,
    verify_backup,
)
from test_blobs_staged_sources_and_evidence_migration import take_ownership

from omnivia_core.contracts.v1 import MutationPrecondition

WORKSPACE_ID = sc.WORKSPACE_ID


def _checkpoint_rows(connection: Any) -> list[tuple[Any, ...]]:
    return connection.execute(
        "SELECT workspace_id, session_id, checkpoint_id, sequence, content_digest "
        "FROM omnivia_engineering_checkpoints WHERE workspace_id = ? "
        "ORDER BY session_id, sequence",
        (WORKSPACE_ID,),
    ).fetchall()


def _session_rows(connection: Any) -> list[tuple[Any, ...]]:
    return connection.execute(
        "SELECT workspace_id, session_id, state FROM omnivia_engineering_sessions "
        "WHERE workspace_id = ? ORDER BY session_id",
        (WORKSPACE_ID,),
    ).fetchall()


def _source_rows(connection: Any) -> list[tuple[Any, ...]]:
    return connection.execute(
        "SELECT repository_id, snapshot_id, capture_status FROM omnivia_engineering_snapshots "
        "WHERE workspace_id = ? ORDER BY repository_id, snapshot_id",
        (WORKSPACE_ID,),
    ).fetchall()


def test_engineering_records_survive_a_verified_backup_restore(tmp_path: Path) -> None:
    ws = sc.Workspace(tmp_path)
    try:
        # One recorded source stream and one evidence-linked observation.
        ws.record(sc._source(1, "esnap-r1", {"src/auth.py": sc.AUTH_V1}))
        observed = ws.observe(sc._observation(sc._manifest("esnap-r1")))

        # One continuity session, two checkpoints, closed with its final one.
        registered = ws.ok(
            "continuity.session.register",
            {
                "schema_version": "engineering.1",
                "checkout_hint": "/home/dev/app",
                "host_session_ref": "restore-conv-1",
            },
        )
        session_id = registered["session"]["session_id"]
        first = ws.ok(
            "continuity.checkpoint.append",
            {
                "session_id": session_id,
                "payload": {
                    "objective": "Investigate the session-restoration failure",
                    "checkpoint_kind": "periodic",
                    "unresolved_work": ["Why does restore fail?"],
                },
            },
            mutation_precondition=MutationPrecondition(record_version="seq-0"),
        )
        second = ws.ok(
            "continuity.checkpoint.append",
            {
                "session_id": session_id,
                "payload": {
                    "objective": "Wrap up the investigation",
                    "checkpoint_kind": "session_close",
                },
                "expected_parent_sequence": 1,
            },
            mutation_precondition=MutationPrecondition(record_version="seq-1"),
        )
        closed = ws.ok(
            "continuity.session.close",
            {"session_id": session_id, "expected_sequence": 2},
            mutation_precondition=MutationPrecondition(record_version="seq-2"),
        )
        assert closed["state"] == "closed"

        # The pre-restore facts the restore must reproduce exactly.
        original = ws.holder.connection
        checkpoints = _checkpoint_rows(original)
        sessions = _session_rows(original)
        snapshots = _source_rows(original)
        assert len(checkpoints) == 2
        assert sessions == [(WORKSPACE_ID, session_id, "closed")]
        assert snapshots, "the recorded snapshot row must exist"
        digest_first = first["receipt"]["content_digest"]
        digest_second = second["receipt"]["content_digest"]
        assert digest_first.startswith("sha256:") and digest_second.startswith("sha256:")

        # The online backup API needs no writer holding the lease: release the
        # workspace connection the way a quiesced service would before backup.
        ws.holder.connection.close()

        # Backup, verify, restore into a fresh location.
        attempt = new_attempt_id()
        backup = backup_database(ws.holder.path, tmp_path / "backups" / f"{attempt}.sqlite")
        verified = verify_backup(ws.holder.path, backup, attempt)
        assert verified.verified is True
        restored_path = restore_backup(backup, tmp_path / "restored" / "workspace.sqlite")

        # Reopen the restored bytes and compare row for row.
        restored = take_ownership(restored_path)
        try:
            assert _checkpoint_rows(restored.connection) == checkpoints
            assert _session_rows(restored.connection) == sessions
            assert _source_rows(restored.connection) == snapshots

            # The observation is still discoverable through the real surface,
            # at the same exact version.
            surface = sc._surface(restored)
            response = surface.dispatch(
                sc.s0.envelope_for(
                    sc.get_operation_metadata("memory.get"),
                    operation_input={
                        "record_id": observed["record_id"],
                        "view": "candidates",
                    },
                    purpose=sc._PURPOSES["memory.get"],
                    workspace_id=WORKSPACE_ID,
                    request_id="req-restore-1",
                    correlation_id="cor-restore-1",
                    trace_id="trc-restore-1",
                )
            )
            result = response.to_wire()["result"]
            identity = result["record"]["provenance"]["identity"]
            assert identity["record_id"] == observed["record_id"]
            assert identity["version"] == observed["version"]
        finally:
            restored.connection.close()
    finally:
        with contextlib.suppress(Exception):
            ws.holder.connection.close()
