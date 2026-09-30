"""Service-owned persistence of a working-tree manifest as an authoritative snapshot."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import threading
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
import test_engineering_captured_source_coverage as captured_schema
from omnivia_core_runtime.ownership.fencing import fenced_transaction, guarded_tables
from omnivia_core_runtime.ownership.identity import FakeClock
from omnivia_core_runtime.service import (
    engineering_source_capture_execution,
    source_capture,
)
from omnivia_core_runtime.service.application import (
    build_engineering_application_dispatcher,
)
from omnivia_core_runtime.service.authorization import Grant
from omnivia_core_runtime.service.dispatch import Dispatcher
from omnivia_core_runtime.service.engineering_source_capture_execution import (
    EngineeringSourceCaptureExecutor,
)
from omnivia_core_runtime.service.operations import SERVICE_OPERATIONS
from omnivia_core_runtime.service.runner import ServiceRunner, ServiceSettings
from omnivia_core_runtime.service.source_capture import (
    SourceCaptureRefused,
    capture_working_tree_manifest,
    capture_working_tree_snapshot,
    capture_working_tree_snapshot_owned,
)
from omnivia_core_runtime.service.versions import SERVER_VERSION
from omnivia_core_runtime.service.workspace_init import initialise_workspace
from omnivia_core_runtime.storage import (
    engineering_invalidation,
    engineering_source,
    engineering_source_producer,
    repository_identity,
)
from omnivia_core_runtime.storage import (
    migrations as migrations_module,
)
from omnivia_core_runtime.storage.connection import OpenMode, StorageError
from omnivia_core_runtime.storage.migrations import (
    applied_migrations,
    apply_pending_migrations,
    canonical_schema_fingerprint,
    canonical_schema_tables,
)

from omnivia_core.contracts.v1 import (
    CONTRACT_VERSION,
    CapabilityRequirement,
    ClientIdentity,
    ErrorResponseEnvelope,
    RequestEnvelope,
    RequestMetadata,
    SuccessResponseEnvelope,
)

pytestmark = pytest.mark.skipif(
    not source_capture._NO_FOLLOW_WALK, reason="host lacks no-follow checkout walk"
)

_GIT_ENV = {
    **os.environ,
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
}


def _repo(tmp_path: Path, name: str = "repo") -> Path:
    root = tmp_path / name
    root.mkdir()
    (root / "a.py").write_bytes(b"print('a')\n")
    for args in (
        ["init", "-q"],
        ["add", "."],
        ["-c", "commit.gpgsign=false", "commit", "-q", "-m", "init"],
    ):
        subprocess.run(["git", *args], cwd=root, env=_GIT_ENV, check=True)
    return root


class _Env:
    def __init__(self, tmp_path: Path) -> None:
        self.workspace = tmp_path / "workspace"
        self.installation = tmp_path / "installation-state"
        initialise_workspace(
            workspace_root=self.workspace,
            installation_root=self.installation,
            core_version=SERVER_VERSION,
        )

    def _runner(self) -> ServiceRunner:
        runner = ServiceRunner(
            ServiceSettings(
                workspace_root=self.workspace,
                installation_root=self.installation,
                core_version=SERVER_VERSION,
                endpoint=None,
            )
        )
        assert runner.start().ready
        return runner

    def register(self, repository_id: str, checkout: Path | None) -> None:
        """Pre-register through a service-owned fenced transaction."""
        runner = self._runner()
        try:
            assert runner.connection and runner.identity and runner.generation
            ws = str(runner.workspace_id)
            audit = f"aud-reg-{repository_id}"
            with fenced_transaction(
                runner.connection,
                runner.identity,
                workspace_id=ws,
                fencing_generation=runner.generation,
            ) as c:
                c.execute(
                    "INSERT INTO omnivia_application_audit_events (audit_ref, "
                    "workspace_id, principal_id, operation, purpose, request_id, "
                    "correlation_id, trace_id, granted_authority_json, outcome_class, "
                    "error_code, recorded_at_us) VALUES (?, ?, 'p', 'o', 'p', 'r', "
                    "'c', 't', '{}', 'succeeded', NULL, 1)",
                    (audit, ws),
                )
                settlement = SimpleNamespace(audit_ref=audit)
                repository_identity.register_repository(
                    c,
                    settlement,
                    workspace_id=ws,
                    repository_id=repository_id,
                    display_name="repo",
                    provider_hint=None,
                    registered_at_us=1,
                )
                if checkout is not None:
                    repository_identity.register_checkout(
                        c,
                        settlement,
                        workspace_id=ws,
                        checkout_id=f"co-{repository_id}",
                        repository_id=repository_id,
                        installation_id=runner.identity.installation_id,
                        checkout_hint=os.fspath(checkout),
                        registered_at_us=1,
                    )
        finally:
            runner.stop()

    def snapshot(self, repository_id: str, checkout: Path, snapshot_id: str = "snap-1"):
        return capture_working_tree_snapshot(
            workspace_root=self.workspace,
            installation_root=self.installation,
            repository_id=repository_id,
            checkout_root=checkout,
            snapshot_id=snapshot_id,
            core_version=SERVER_VERSION,
        )

    def commit(
        self,
        payload: dict[str, object],
        *,
        key: str,
        request_id: str,
    ) -> SuccessResponseEnvelope | ErrorResponseEnvelope:
        runner = self._runner()
        try:
            assert runner.workspace_id is not None and runner.identity is not None
            principal = "local-user"
            fallback = Dispatcher.for_service_operations(
                Grant(
                    principal=principal,
                    workspaces=frozenset({runner.workspace_id}),
                    operations=frozenset(SERVICE_OPERATIONS),
                ),
                None,
            )
            dispatcher = build_engineering_application_dispatcher(
                service=runner,
                principal_id=principal,
                installation_id=runner.identity.installation_id,
                workspace_id=runner.workspace_id,
                fallback=fallback,
            )
            return dispatcher.dispatch(
                RequestEnvelope(
                    operation="engineering.source.capture.commit",
                    metadata=RequestMetadata(
                        request_id=request_id,
                        correlation_id=f"cor-{request_id}",
                        trace_id=f"trc-{request_id}",
                        api_version=CONTRACT_VERSION,
                        client=ClientIdentity(id="capture-test", version="1.0.0"),
                        workspace_id=runner.workspace_id,
                        scopes=("engineering:source",),
                        purpose="engineering_source",
                        required_capabilities=(
                            CapabilityRequirement(
                                id="engineering.source",
                                minimum_version="1.0",
                                required=True,
                            ),
                        ),
                        idempotency_key=key,
                        mutation_precondition=None,
                        principal_claim=None,
                    ),
                    input=payload,
                )
            )
        finally:
            runner.stop()

    def rows(self, table: str) -> int:
        connection = sqlite3.connect(self.workspace / "workspace.sqlite")
        try:
            return int(
                connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            )
        finally:
            connection.close()

    def blob(self, digest: str) -> bytes:
        return (
            self.workspace / "blobs" / "sha256" / digest.removeprefix("sha256:")
        ).read_bytes()


def test_snapshot_persists_resolvable_manifest_and_bytes_across_restart(
    tmp_path: Path,
) -> None:
    env = _Env(tmp_path)
    root = _repo(tmp_path)
    env.register("repo-1", root)
    (root / "a.py").write_bytes(b"dirty\n")
    (root / "new.txt").write_bytes(b"untracked\n")

    result = env.snapshot("repo-1", root)
    assert (result.status, result.capture_status, result.file_count) == (
        "captured",
        "complete",
        2,
    )
    assert str(root) not in json.dumps(result.to_dict())

    # A new connection (restart): the row, the evidence and the exact bytes resolve.
    connection = sqlite3.connect(env.workspace / "workspace.sqlite")
    try:
        row = connection.execute(
            "SELECT repository_id, snapshot_kind, manifest_digest, base_commit, "
            "capture_status FROM omnivia_engineering_snapshots WHERE snapshot_id = ?",
            ("snap-1",),
        ).fetchone()
        assert row == (
            "repo-1",
            "working_tree",
            result.manifest_digest,
            None,
            "complete",
        )
        evidence = connection.execute(
            "SELECT evidence_id, blob_content_digest FROM omnivia_evidence_artifacts "
            "WHERE source_native_id = 'working-tree-manifest.snap-1'"
        ).fetchone()
        assert evidence == (result.manifest_evidence_id, result.manifest_digest)
        header = connection.execute(
            "SELECT repository_id, manifest_evidence_id, rich_manifest_digest, "
            "coverage_digest, file_count, capture_status "
            "FROM omnivia_engineering_snapshot_captures WHERE snapshot_id = ?",
            ("snap-1",),
        ).fetchone()
        assert header is not None
        assert header[:3] == (
            "repo-1",
            result.manifest_evidence_id,
            result.manifest_digest,
        )
        assert header[4:] == (2, "complete")
        indexed = dict(
            connection.execute(
                "SELECT path, content_digest FROM omnivia_engineering_snapshot_files "
                "WHERE snapshot_id = ? ORDER BY path",
                ("snap-1",),
            )
        )
        assert set(indexed) == {"a.py", "new.txt"}
        assert header[3] == engineering_source.captured_coverage_digest(indexed)
        for digest in (result.manifest_digest, hashlib.sha256(b"dirty\n").hexdigest()):
            digest = digest.removeprefix("sha256:")
            assert connection.execute(
                "SELECT 1 FROM omnivia_blob_objects WHERE content_digest = ?",
                (f"sha256:{digest}",),
            ).fetchone()
    finally:
        connection.close()

    manifest_bytes = env.blob(result.manifest_digest)
    assert "sha256:" + hashlib.sha256(manifest_bytes).hexdigest() == (
        result.manifest_digest
    )
    manifest = json.loads(manifest_bytes)
    assert manifest["snapshot_kind"] == "working_tree"
    assert manifest["base"]["kind"] == "commit" and manifest["complete"] is True
    files = {f["path"]: f for f in manifest["files"]}
    assert files["a.py"]["tracked"] and not files["new.txt"]["tracked"]
    for path, content in (("a.py", b"dirty\n"), ("new.txt", b"untracked\n")):
        assert env.blob(files[path]["content_digest"]) == content


def test_snapshot_retry_is_idempotent_and_conflict_refuses(tmp_path: Path) -> None:
    env = _Env(tmp_path)
    root = _repo(tmp_path)
    env.register("repo-1", root)
    first = env.snapshot("repo-1", root)
    again = env.snapshot("repo-1", root)
    assert again.status == "already_captured"
    assert (again.snapshot_id, again.manifest_digest, again.manifest_evidence_id) == (
        first.snapshot_id,
        first.manifest_digest,
        first.manifest_evidence_id,
    )
    assert env.rows("omnivia_engineering_snapshots") == 1
    assert env.rows("omnivia_engineering_snapshot_captures") == 1
    assert env.rows("omnivia_engineering_snapshot_files") == first.file_count

    (root / "a.py").write_bytes(b"different\n")
    with pytest.raises(SourceCaptureRefused, match="different content"):
        env.snapshot("repo-1", root)
    assert env.rows("omnivia_engineering_snapshots") == 1
    # The changed tree is a distinct snapshot under its own identity.
    other = env.snapshot("repo-1", root, "snap-2")
    assert other.status == "captured" and other.manifest_digest != first.manifest_digest


def test_conflicting_retry_refuses_before_publishing_any_blob(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _Env(tmp_path)
    root = _repo(tmp_path)
    env.register("repo-1", root)
    env.snapshot("repo-1", root)
    blob_dir = env.workspace / "blobs" / "sha256"
    before = sorted(p.name for p in blob_dir.iterdir())

    def unexpected(*_args: object) -> Path:
        raise AssertionError("publish_blob called for a conflicting retry")

    monkeypatch.setattr(source_capture, "publish_blob", unexpected)
    (root / "a.py").write_bytes(b"different\n")
    with pytest.raises(SourceCaptureRefused, match="different content"):
        env.snapshot("repo-1", root)
    assert sorted(p.name for p in blob_dir.iterdir()) == before


def test_snapshot_refuses_unregistered_and_mismatched_checkout(tmp_path: Path) -> None:
    env = _Env(tmp_path)
    root = _repo(tmp_path)
    other = _repo(tmp_path, "other")

    with pytest.raises(SourceCaptureRefused, match="not registered"):
        env.snapshot("repo-1", root)

    env.register("repo-1", root)
    env.register("repo-2", None)  # registered, but no checkout bound
    with pytest.raises(SourceCaptureRefused, match="not bound"):
        env.snapshot("repo-1", other)  # a checkout nobody registered
    with pytest.raises(SourceCaptureRefused, match="not bound"):
        env.snapshot("repo-2", root)  # bound to a different repository
    assert env.rows("omnivia_engineering_snapshots") == 0
    assert env.rows("omnivia_evidence_artifacts") == 0


def test_snapshot_preserves_incomplete_coverage(tmp_path: Path) -> None:
    env = _Env(tmp_path)
    root = _repo(tmp_path)
    (root / "link").symlink_to("a.py")
    env.register("repo-1", root)
    result = env.snapshot("repo-1", root)
    assert result.capture_status == "incomplete"
    manifest = json.loads(env.blob(result.manifest_digest))
    assert manifest["complete"] is False
    assert {
        "path": "link",
        "reason": "missing_or_unsupported",
    } in manifest["omissions"]


def test_failed_publication_leaves_no_acknowledged_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _Env(tmp_path)
    root = _repo(tmp_path)
    env.register("repo-1", root)
    calls = 0
    real = source_capture.publish_blob

    def flaky(blobs_root: Path, digest: str, content: bytes) -> Path:
        nonlocal calls
        calls += 1
        if calls == 2:  # the manifest, after the file blob was published
            raise SourceCaptureRefused("publication failed")
        return real(blobs_root, digest, content)

    monkeypatch.setattr(source_capture, "publish_blob", flaky)
    with pytest.raises(SourceCaptureRefused, match="publication failed"):
        env.snapshot("repo-1", root)
    assert env.rows("omnivia_engineering_snapshots") == 0
    assert env.rows("omnivia_evidence_artifacts") == 0
    assert env.rows("omnivia_blob_objects") == 0

    monkeypatch.setattr(source_capture, "publish_blob", real)
    assert env.snapshot("repo-1", root).status == "captured"


def test_captured_commit_triggers_the_invalidation_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _Env(tmp_path)
    root = _repo(tmp_path)
    env.register("repo-1", root)
    capture = env.snapshot("repo-1", root, "captured-trigger")
    calls: list[tuple[str, str, int]] = []

    def drain(
        _connection: object,
        _identity: object,
        *,
        workspace_id: str,
        stream_id: str,
        fencing_generation: int,
        now_us: int,
        **_kwargs: object,
    ) -> None:
        assert now_us > 0
        calls.append((workspace_id, stream_id, fencing_generation))

    monkeypatch.setattr(engineering_invalidation, "drain_invalidation", drain)
    result = env.commit(
        {
            "repository_id": "repo-1",
            "stream_id": "captured-trigger-stream",
            "sequence": 1,
            "snapshot_id": "captured-trigger",
            "expected_manifest_digest": capture.manifest_digest,
        },
        key="capture-trigger-key",
        request_id="capture-trigger",
    )

    assert isinstance(result, SuccessResponseEnvelope)
    assert len(calls) == 1
    assert calls[0][1] == "captured-trigger-stream"
    assert calls[0][2] > 0


def test_captured_commit_replays_and_recovers_a_gap_across_restarts(
    tmp_path: Path,
) -> None:
    env = _Env(tmp_path)
    root = _repo(tmp_path)
    env.register("repo-1", root)
    first = env.snapshot("repo-1", root, "captured-1")
    stream_id = "captured-stream-1"

    def payload(
        sequence: int,
        snapshot_id: str,
        *,
        predecessor: str | None = None,
        expected: str | None = None,
    ) -> dict[str, object]:
        value: dict[str, object] = {
            "repository_id": "repo-1",
            "stream_id": stream_id,
            "sequence": sequence,
            "snapshot_id": snapshot_id,
        }
        if predecessor is not None:
            value["predecessor"] = {
                "sequence": sequence - 1,
                "snapshot_id": predecessor,
            }
        if expected is not None:
            value["expected_manifest_digest"] = expected
        return value

    request = payload(1, "captured-1", expected=first.manifest_digest)
    recorded = env.commit(request, key="capture-key-1", request_id="capture-1")
    assert isinstance(recorded, SuccessResponseEnvelope)
    first_result = recorded.to_wire()["result"]
    assert first_result["disposition"] == "recorded"
    assert first_result["coverage"] == {
        "state": "current",
        "covered_sequence": 1,
        "announced_sequence": 1,
    }

    replay = env.commit(request, key="capture-key-1", request_id="capture-1-replay")
    assert isinstance(replay, SuccessResponseEnvelope)
    assert replay.to_wire()["result"] == first_result
    duplicate = env.commit(request, key="capture-key-1b", request_id="capture-1-dup")
    assert isinstance(duplicate, SuccessResponseEnvelope)
    assert duplicate.to_wire()["result"]["disposition"] == "already_recorded"

    (root / "a.py").write_bytes(b"second\n")
    second = env.snapshot("repo-1", root, "captured-2")
    (root / "a.py").write_bytes(b"third\n")
    third = env.snapshot("repo-1", root, "captured-3")

    pending = env.commit(
        payload(3, "captured-3", predecessor="captured-2"),
        key="capture-key-3",
        request_id="capture-3",
    )
    assert isinstance(pending, SuccessResponseEnvelope)
    assert pending.to_wire()["result"]["coverage"] == {
        "state": "pending",
        "covered_sequence": 1,
        "announced_sequence": 3,
    }

    mismatch = env.commit(
        payload(
            2,
            "captured-2",
            predecessor="captured-1",
            expected="sha256:" + "f" * 64,
        ),
        key="capture-key-2-bad",
        request_id="capture-2-bad",
    )
    assert isinstance(mismatch, ErrorResponseEnvelope)
    assert mismatch.error.code == "mutation_precondition_failed"

    converged = env.commit(
        payload(
            2,
            "captured-2",
            predecessor="captured-1",
            expected=second.manifest_digest,
        ),
        key="capture-key-2",
        request_id="capture-2",
    )
    assert isinstance(converged, SuccessResponseEnvelope)
    assert converged.to_wire()["result"]["coverage"] == {
        "state": "current",
        "covered_sequence": 3,
        "announced_sequence": 3,
    }

    serialized = json.dumps(converged.to_wire()["result"])
    assert str(root) not in serialized
    assert all(
        word not in serialized for word in ("checkout_hint", "files", "manifest_json")
    )
    connection = sqlite3.connect(env.workspace / "workspace.sqlite")
    try:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM omnivia_engineering_source_events "
                "WHERE stream_id = ?",
                (stream_id,),
            ).fetchone()[0]
            == 3
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM omnivia_engineering_source_stream_origins "
                "WHERE stream_id = ?",
                (stream_id,),
            ).fetchone()[0]
            == 1
        )
        resolved = engineering_source.covered_snapshot(
            connection,
            workspace_id=str(
                connection.execute(
                    "SELECT workspace_id FROM omnivia_workspace_state WHERE singleton = 1"
                ).fetchone()[0]
            ),
            snapshot_id="captured-3",
            repository_id="repo-1",
        )
        assert resolved is not None and resolved.representation == "captured_v1"
        assert resolved.manifest_digest == third.manifest_digest
    finally:
        connection.close()


def test_installed_producer_captures_and_commits_a_registered_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _Env(tmp_path)
    root = _repo(tmp_path)
    env.register("repo-1", root)
    runner = env._runner()
    try:
        assert (
            runner.workspace_id is not None
            and runner.identity is not None
            and runner.generation is not None
        )
        principal = "local-user"
        fallback = Dispatcher.for_service_operations(
            Grant(
                principal=principal,
                workspaces=frozenset({runner.workspace_id}),
                operations=frozenset(SERVICE_OPERATIONS),
            ),
            None,
        )
        dispatcher = build_engineering_application_dispatcher(
            service=runner,
            principal_id=principal,
            installation_id=runner.identity.installation_id,
            workspace_id=runner.workspace_id,
            fallback=fallback,
        )
        dispatched: list[RequestEnvelope] = []

        def dispatch(request: RequestEnvelope) -> object:
            dispatched.append(request)
            return dispatcher.dispatch(request)

        renewals: list[bool] = []
        real_renew = runner.renew_lease_if_due

        def observe_renewal(*, gate_already_held: bool = False) -> bool:
            renewals.append(gate_already_held)
            return real_renew(gate_already_held=gate_already_held)

        monkeypatch.setattr(runner, "renew_lease_if_due", observe_renewal)
        executor = EngineeringSourceCaptureExecutor(
            runner=runner,
            application=SimpleNamespace(dispatch=dispatch),
            principal_id=principal,
        )
        outcome = executor.run_pending(budget=1, force=True)

        assert (outcome.inspected, outcome.captured, outcome.committed) == (1, 1, 1)
        assert len(renewals) >= 10 and not any(renewals)
        assert len(dispatched) == 1
        request = dispatched[0]
        assert request.operation == "engineering.source.capture.commit"
        seal = runner.connection.execute(
            "SELECT snapshot_id, rich_manifest_digest "
            "FROM omnivia_engineering_snapshot_captures"
        ).fetchone()
        assert seal is not None
        assert request.input == {
            "repository_id": "repo-1",
            "stream_id": request.input["stream_id"],
            "sequence": 1,
            "snapshot_id": str(seal[0]),
            "expected_manifest_digest": str(seal[1]),
        }
        assert runner.connection.execute(
            "SELECT manifest_format FROM omnivia_engineering_source_events "
            "WHERE snapshot_id = ?",
            (seal[0],),
        ).fetchone() == ("captured_v1",)
        assert all(
            not hasattr(outcome, field)
            for field in ("checkout_hint", "path", "files", "content", "manifest")
        )
    finally:
        runner.stop()


def test_installed_producer_recovers_a_sealed_capture_without_rereading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _Env(tmp_path)
    root = _repo(tmp_path)
    env.register("repo-1", root)
    runner = env._runner()
    try:
        assert runner.workspace_id is not None and runner.identity is not None
        renewals: list[int] = []
        frozen = capture_working_tree_manifest(checkout_root=root)
        sealed = capture_working_tree_snapshot_owned(
            runner,
            repository_id="repo-1",
            checkout_root=root,
            snapshot_id="pending-capture-1",
            manifest=frozen,
            renew_lease=lambda: renewals.append(1),
        )
        assert sealed.status == "captured"
        assert len(renewals) >= 3

        principal = "local-user"
        fallback = Dispatcher.for_service_operations(
            Grant(
                principal=principal,
                workspaces=frozenset({runner.workspace_id}),
                operations=frozenset(SERVICE_OPERATIONS),
            ),
            None,
        )
        dispatcher = build_engineering_application_dispatcher(
            service=runner,
            principal_id=principal,
            installation_id=runner.identity.installation_id,
            workspace_id=runner.workspace_id,
            fallback=fallback,
        )
        executor = EngineeringSourceCaptureExecutor(
            runner=runner, application=dispatcher, principal_id=principal
        )

        def unexpected_capture(
            *, checkout_root: Path, heartbeat: object | None = None
        ) -> object:
            raise AssertionError(f"recovery reread {checkout_root}")

        monkeypatch.setattr(
            engineering_source_capture_execution,
            "capture_working_tree_manifest",
            unexpected_capture,
        )
        outcome = executor.run_pending(budget=1, force=True)
        assert outcome.inspected == outcome.committed == 1
        assert outcome.captured == 0
        assert not hasattr(outcome, "checkout_hint")
        assert (
            runner.connection.execute(
                "SELECT COUNT(*) FROM omnivia_engineering_source_events "
                "WHERE snapshot_id = 'pending-capture-1'"
            ).fetchone()[0]
            == 1
        )

        # The poll interval prevents repeated work immediately after settlement.
        assert executor.run_pending(budget=1).inspected == 0
        assert (
            runner.connection.execute(
                "SELECT COUNT(*) FROM omnivia_engineering_source_events "
                "WHERE snapshot_id = 'pending-capture-1'"
            ).fetchone()[0]
            == 1
        )
    finally:
        runner.stop()


def test_capture_queue_is_atomic_payload_free_and_binds_intent_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = _Env(tmp_path)
    root = _repo(tmp_path)
    env.register("repo-1", root)
    runner = env._runner()
    try:
        assert (
            runner.workspace_id is not None
            and runner.identity is not None
            and runner.generation is not None
        )
        frozen = capture_working_tree_manifest(checkout_root=root)
        snapshot_id = engineering_source_capture_execution._derived(
            "src-snapshot",
            runner.workspace_id,
            "repo-1",
            runner.identity.installation_id,
            "co-repo-1",
            frozen.manifest_digest,
        )
        capture_working_tree_snapshot_owned(
            runner,
            repository_id="repo-1",
            checkout_root=root,
            snapshot_id=snapshot_id,
            manifest=frozen,
            renew_lease=runner.renew_lease_if_due,
        )
        assert runner.connection is not None
        assert runner.connection.execute(
            "SELECT expected_frontier, expected_predecessor_snapshot_id, state, "
            "attempt_count FROM omnivia_engineering_source_producer_queue "
            "WHERE workspace_id = ? AND snapshot_id = ?",
            (runner.workspace_id, snapshot_id),
        ).fetchone() == (None, None, "pending", 0)

        # The live producer may bind an untouched generic seal exactly once.
        capture_working_tree_snapshot_owned(
            runner,
            repository_id="repo-1",
            checkout_root=root,
            snapshot_id=snapshot_id,
            manifest=frozen,
            renew_lease=runner.renew_lease_if_due,
            producer_expected_frontier=0,
        )
        assert runner.connection.execute(
            "SELECT expected_frontier, expected_predecessor_snapshot_id, state, "
            "attempt_count FROM omnivia_engineering_source_producer_queue "
            "WHERE workspace_id = ? AND snapshot_id = ?",
            (runner.workspace_id, snapshot_id),
        ).fetchone() == (0, None, "pending", 0)

        generic_replay = capture_working_tree_snapshot_owned(
            runner,
            repository_id="repo-1",
            checkout_root=root,
            snapshot_id=snapshot_id,
            manifest=frozen,
            renew_lease=runner.renew_lease_if_due,
        )
        assert generic_replay.status == "already_captured"

        principal = "local-user"
        application = build_engineering_application_dispatcher(
            service=runner,
            principal_id=principal,
            installation_id=runner.identity.installation_id,
            workspace_id=runner.workspace_id,
            fallback=Dispatcher.for_service_operations(
                Grant(
                    principal=principal,
                    workspaces=frozenset({runner.workspace_id}),
                    operations=frozenset(SERVICE_OPERATIONS),
                ),
                None,
            ),
        )
        settled = EngineeringSourceCaptureExecutor(
            runner=runner,
            application=application,
            principal_id=principal,
        ).run_pending(budget=1, force=True)
        assert settled.committed == 1
        settled_replay = capture_working_tree_snapshot_owned(
            runner,
            repository_id="repo-1",
            checkout_root=root,
            snapshot_id=snapshot_id,
            manifest=frozen,
            renew_lease=runner.renew_lease_if_due,
        )
        assert settled_replay.status == "already_captured"
        assert runner.connection.execute(
            "SELECT state FROM omnivia_engineering_source_producer_queue "
            "WHERE workspace_id = ? AND snapshot_id = ?",
            (runner.workspace_id, snapshot_id),
        ).fetchone() == ("settled",)
        with (
            pytest.raises(sqlite3.Error, match="identity and intent are immutable"),
            fenced_transaction(
                runner.connection,
                runner.identity,
                workspace_id=runner.workspace_id,
                fencing_generation=runner.generation,
            ),
        ):
            runner.connection.execute(
                "UPDATE omnivia_engineering_source_producer_queue "
                "SET repository_id = 'other-repository' "
                "WHERE workspace_id = ? AND snapshot_id = ?",
                (runner.workspace_id, snapshot_id),
            )
        with (
            pytest.raises(sqlite3.Error, match="transition is invalid"),
            fenced_transaction(
                runner.connection,
                runner.identity,
                workspace_id=runner.workspace_id,
                fencing_generation=runner.generation,
            ),
        ):
            runner.connection.execute(
                "UPDATE omnivia_engineering_source_producer_queue "
                "SET available_at_us = available_at_us + 1 "
                "WHERE workspace_id = ? AND snapshot_id = ?",
                (runner.workspace_id, snapshot_id),
            )
        with (
            pytest.raises(sqlite3.Error, match="completed.*cannot reopen"),
            fenced_transaction(
                runner.connection,
                runner.identity,
                workspace_id=runner.workspace_id,
                fencing_generation=runner.generation,
            ),
        ):
            runner.connection.execute(
                "UPDATE omnivia_engineering_source_producer_state "
                "SET legacy_cursor_snapshot_id = 'rewound-snapshot' "
                "WHERE workspace_id = ? AND installation_id = ?",
                (runner.workspace_id, runner.identity.installation_id),
            )

        with pytest.raises(StorageError, match="intent does not match"):
            capture_working_tree_snapshot_owned(
                runner,
                repository_id="repo-1",
                checkout_root=root,
                snapshot_id=snapshot_id,
                manifest=frozen,
                renew_lease=runner.renew_lease_if_due,
                producer_expected_frontier=1,
                producer_expected_predecessor_snapshot_id="another-snapshot",
            )

        columns = {
            str(row[1])
            for row in runner.connection.execute(
                "PRAGMA table_info(omnivia_engineering_source_producer_queue)"
            ).fetchall()
        }
        assert "checkout_hint" not in columns
        assert os.fspath(root) not in repr(
            runner.connection.execute(
                "SELECT * FROM omnivia_engineering_source_producer_queue"
            ).fetchall()
        )

        def refuse_queue(*_args: object, **_kwargs: object) -> None:
            raise StorageError("injected queue refusal")

        monkeypatch.setattr(
            source_capture, "enqueue_capture_in_transaction", refuse_queue
        )
        (root / "a.py").write_bytes(b"rollback-capture\n")
        rollback_manifest = capture_working_tree_manifest(checkout_root=root)
        with pytest.raises(StorageError, match="injected queue refusal"):
            capture_working_tree_snapshot_owned(
                runner,
                repository_id="repo-1",
                checkout_root=root,
                snapshot_id="captured-queue-rollback",
                manifest=rollback_manifest,
                renew_lease=runner.renew_lease_if_due,
            )
        assert runner.connection.execute(
            "SELECT 1 FROM omnivia_engineering_snapshot_captures "
            "WHERE workspace_id = ? AND snapshot_id = 'captured-queue-rollback'",
            (runner.workspace_id,),
        ).fetchone() is None
    finally:
        runner.stop()

    outside = sqlite3.connect(env.workspace / "workspace.sqlite")
    try:
        with pytest.raises(sqlite3.Error, match="unguarded|no such function"):
            outside.execute(
                "UPDATE omnivia_engineering_source_producer_queue "
                "SET available_at_us = available_at_us + 1 "
                "WHERE snapshot_id = ?",
                (snapshot_id,),
            )
        with pytest.raises(sqlite3.Error, match="durable; DELETE"):
            outside.execute(
                "DELETE FROM omnivia_engineering_source_producer_queue "
                "WHERE snapshot_id = ?",
                (snapshot_id,),
            )
    finally:
        outside.close()


def test_checkout_replay_respects_persisted_retry_time(tmp_path: Path) -> None:
    env = _Env(tmp_path)
    root = _repo(tmp_path)
    env.register("repo-1", root)
    clock = FakeClock(wall=datetime.now(UTC))
    runner = ServiceRunner(
        ServiceSettings(
            workspace_root=env.workspace,
            installation_root=env.installation,
            core_version=SERVER_VERSION,
            endpoint=None,
        ),
        clock=clock,
    )
    assert runner.start().ready

    class RefusingApplication:
        def __init__(self) -> None:
            self.calls = 0

        def dispatch(self, _request: RequestEnvelope) -> object:
            self.calls += 1
            return object()

    application = RefusingApplication()
    try:
        executor = EngineeringSourceCaptureExecutor(
            runner=runner,
            application=application,
            principal_id="local-user",
        )
        first = executor.run_pending(budget=1, force=True)
        assert first.inspected == first.captured == 1
        assert first.committed == 0
        assert application.calls == 1
        assert runner.connection is not None
        retry = runner.connection.execute(
            "SELECT state, available_at_us, last_attempt_at_us "
            "FROM omnivia_engineering_source_producer_queue"
        ).fetchone()
        assert retry is not None and retry[0] == "retry"
        assert int(retry[1]) > int(retry[2])

        # Both a checkout turn and a recovery turn occur before eligibility.
        # Neither may bypass the durable wall-clock delay.
        before_due = [executor.run_pending(budget=1, force=True) for _ in range(2)]
        assert all(result.inspected <= 1 for result in before_due)
        assert application.calls == 1

        clock.advance_wall(2)
        after_due = [executor.run_pending(budget=1, force=True) for _ in range(2)]
        assert all(result.inspected <= 1 for result in after_due)
        assert application.calls == 2
    finally:
        runner.stop()


def test_retrying_generic_seal_binds_exact_intent_and_recovers_after_restart(
    tmp_path: Path,
) -> None:
    env = _Env(tmp_path)
    root = _repo(tmp_path)
    env.register("repo-1", root)
    clock = FakeClock(wall=datetime.now(UTC))

    def new_runner() -> ServiceRunner:
        service = ServiceRunner(
            ServiceSettings(
                workspace_root=env.workspace,
                installation_root=env.installation,
                core_version=SERVER_VERSION,
                endpoint=None,
            ),
            clock=clock,
        )
        assert service.start().ready
        return service

    def new_executor(service: ServiceRunner) -> EngineeringSourceCaptureExecutor:
        assert service.workspace_id is not None and service.identity is not None
        principal = "local-user"
        return EngineeringSourceCaptureExecutor(
            runner=service,
            application=build_engineering_application_dispatcher(
                service=service,
                principal_id=principal,
                installation_id=service.identity.installation_id,
                workspace_id=service.workspace_id,
                fallback=Dispatcher.for_service_operations(
                    Grant(
                        principal=principal,
                        workspaces=frozenset({service.workspace_id}),
                        operations=frozenset(SERVICE_OPERATIONS),
                    ),
                    None,
                ),
            ),
            principal_id=principal,
        )

    runner = new_runner()
    try:
        executor = new_executor(runner)
        first = executor.run_pending(budget=1, force=True)
        assert first == engineering_source_capture_execution.SourceProducerPass(
            inspected=1, captured=1, committed=1
        )
        assert runner.connection is not None and runner.workspace_id is not None
        stream_id, first_snapshot = map(
            str,
            runner.connection.execute(
                "SELECT stream_id, snapshot_id "
                "FROM omnivia_engineering_source_events WHERE sequence = 1"
            ).fetchone(),
        )

        # Consume the checkout lane while the tree is unchanged, leaving recovery
        # as the next durable lane before a generic capture is introduced.
        unchanged = executor.run_pending(budget=1, force=True)
        assert unchanged == engineering_source_capture_execution.SourceProducerPass(
            inspected=1, captured=0, committed=0
        )

        (root / "a.py").write_bytes(b"generic-then-exact\n")
        manifest = capture_working_tree_manifest(checkout_root=root)
        second_snapshot = engineering_source_capture_execution._derived(
            "src-snapshot",
            runner.workspace_id,
            "repo-1",
            runner.identity.installation_id,
            "co-repo-1",
            manifest.manifest_digest,
        )
        generic = capture_working_tree_snapshot_owned(
            runner,
            repository_id="repo-1",
            checkout_root=root,
            snapshot_id=second_snapshot,
            manifest=manifest,
            renew_lease=runner.renew_lease_if_due,
        )
        assert generic.status == "captured"

        # Capture timestamps come from the publication clock; advance the fake
        # scheduler clock beyond that seal before exercising recovery.
        clock.advance_wall(2)
        refused = executor.run_pending(budget=1, force=True)
        assert refused == engineering_source_capture_execution.SourceProducerPass(
            inspected=1, captured=0, committed=0
        )
        retry_before_bind = runner.connection.execute(
            "SELECT state, available_at_us, attempt_count, last_attempt_at_us, "
            "expected_frontier, expected_predecessor_snapshot_id "
            "FROM omnivia_engineering_source_producer_queue "
            "WHERE snapshot_id = ?",
            (second_snapshot,),
        ).fetchone()
        assert retry_before_bind is not None
        assert retry_before_bind[0] == "retry"
        assert retry_before_bind[2] == 1
        assert retry_before_bind[4:] == (None, None)

        # The checkout lane reobserves the content-derived seal and binds the
        # current head, but must preserve the retry deadline and attempt count.
        bound = executor.run_pending(budget=1, force=True)
        assert bound == engineering_source_capture_execution.SourceProducerPass(
            inspected=1, captured=0, committed=0
        )
        retry_after_bind = runner.connection.execute(
            "SELECT state, available_at_us, attempt_count, last_attempt_at_us, "
            "expected_frontier, expected_predecessor_snapshot_id "
            "FROM omnivia_engineering_source_producer_queue "
            "WHERE snapshot_id = ?",
            (second_snapshot,),
        ).fetchone()
        assert retry_after_bind == (*retry_before_bind[:4], 1, first_snapshot)
    finally:
        runner.stop()

    clock.advance_wall(2)
    restarted = new_runner()
    try:
        recovered = new_executor(restarted).run_pending(budget=1, force=True)
        assert recovered == engineering_source_capture_execution.SourceProducerPass(
            inspected=1, captured=0, committed=1
        )
        assert restarted.connection is not None
        assert restarted.connection.execute(
            "SELECT sequence, snapshot_id, predecessor_snapshot_id "
            "FROM omnivia_engineering_source_events "
            "WHERE stream_id = ? ORDER BY sequence",
            (stream_id,),
        ).fetchall() == [
            (1, first_snapshot, None),
            (2, second_snapshot, first_snapshot),
        ]
        assert restarted.connection.execute(
            "SELECT state, expected_frontier, expected_predecessor_snapshot_id "
            "FROM omnivia_engineering_source_producer_queue "
            "WHERE snapshot_id = ?",
            (second_snapshot,),
        ).fetchone() == ("settled", 1, first_snapshot)

        new_executor(restarted).run_pending(budget=1, force=True)
        assert restarted.connection.execute(
            "SELECT COUNT(*) FROM omnivia_engineering_source_events "
            "WHERE stream_id = ? AND snapshot_id = ?",
            (stream_id, second_snapshot),
        ).fetchone() == (1,)
    finally:
        restarted.stop()


def test_retry_cursor_wraps_past_a_moving_poison_item_across_restarts(
    tmp_path: Path,
) -> None:
    env = _Env(tmp_path)
    root = _repo(tmp_path)
    env.register("repo-1", root)

    runner = env._runner()
    try:
        assert (
            runner.connection is not None
            and runner.identity is not None
            and runner.workspace_id is not None
            and runner.generation is not None
        )
        manifest = capture_working_tree_manifest(checkout_root=root)
        for snapshot_id in ("captured-cursor-a", "captured-cursor-z"):
            capture_working_tree_snapshot_owned(
                runner,
                repository_id="repo-1",
                checkout_root=root,
                snapshot_id=snapshot_id,
                manifest=manifest,
                renew_lease=runner.renew_lease_if_due,
            )
        now_us = (
            int(
                runner.connection.execute(
                    "SELECT max(available_at_us) "
                    "FROM omnivia_engineering_source_producer_queue"
                ).fetchone()[0]
            )
            + 1
        )
        with runner.sqlite_gate:
            crashed = engineering_source_producer.take_queue_items(
                runner.connection,
                runner.identity,
                workspace_id=runner.workspace_id,
                installation_id=runner.identity.installation_id,
                fencing_generation=runner.generation,
                now_us=now_us,
                limit=1,
            )
        assert [item.snapshot_id for item in crashed] == ["captured-cursor-a"]
        # Stop after selection and before the effect: the cursor is durable even
        # though this item was not handled.
    finally:
        runner.stop()

    for cycle in range(3):
        poison_runner = env._runner()
        try:
            assert (
                poison_runner.connection is not None
                and poison_runner.identity is not None
                and poison_runner.workspace_id is not None
                and poison_runner.generation is not None
            )
            with poison_runner.sqlite_gate:
                poison = engineering_source_producer.take_queue_items(
                    poison_runner.connection,
                    poison_runner.identity,
                    workspace_id=poison_runner.workspace_id,
                    installation_id=poison_runner.identity.installation_id,
                    fencing_generation=poison_runner.generation,
                    now_us=now_us,
                    limit=1,
                )
                assert [item.snapshot_id for item in poison] == [
                    "captured-cursor-z"
                ]
                engineering_source_producer.mark_queue_retry(
                    poison_runner.connection,
                    poison_runner.identity,
                    workspace_id=poison_runner.workspace_id,
                    installation_id=poison_runner.identity.installation_id,
                    snapshot_id="captured-cursor-z",
                    fencing_generation=poison_runner.generation,
                    now_us=now_us,
                )
                poison_due = int(
                    poison_runner.connection.execute(
                        "SELECT available_at_us "
                        "FROM omnivia_engineering_source_producer_queue "
                        "WHERE snapshot_id = 'captured-cursor-z'"
                    ).fetchone()[0]
                )
                assert poison_runner.connection.execute(
                    "SELECT queue_cursor_available_at_us, "
                    "queue_cursor_snapshot_id "
                    "FROM omnivia_engineering_source_producer_state"
                ).fetchone() == (poison_due, "captured-cursor-z")
        finally:
            poison_runner.stop()

        wrapped_runner = env._runner()
        try:
            assert (
                wrapped_runner.connection is not None
                and wrapped_runner.identity is not None
                and wrapped_runner.workspace_id is not None
                and wrapped_runner.generation is not None
            )
            with wrapped_runner.sqlite_gate:
                wrapped = engineering_source_producer.take_queue_items(
                    wrapped_runner.connection,
                    wrapped_runner.identity,
                    workspace_id=wrapped_runner.workspace_id,
                    installation_id=wrapped_runner.identity.installation_id,
                    fencing_generation=wrapped_runner.generation,
                    now_us=now_us,
                    limit=1,
                )
                assert [item.snapshot_id for item in wrapped] == [
                    "captured-cursor-a"
                ]
                if cycle == 2:
                    engineering_source_producer.mark_queue_settled(
                        wrapped_runner.connection,
                        wrapped_runner.identity,
                        workspace_id=wrapped_runner.workspace_id,
                        installation_id=wrapped_runner.identity.installation_id,
                        snapshot_id="captured-cursor-a",
                        fencing_generation=wrapped_runner.generation,
                        now_us=now_us,
                    )
        finally:
            wrapped_runner.stop()
        now_us = poison_due

    connection = sqlite3.connect(env.workspace / "workspace.sqlite")
    try:
        assert connection.execute(
            "SELECT snapshot_id, state, attempt_count "
            "FROM omnivia_engineering_source_producer_queue ORDER BY snapshot_id"
        ).fetchall() == [
            ("captured-cursor-a", "settled", 1),
            ("captured-cursor-z", "retry", 3),
        ]
    finally:
        connection.close()


def test_producer_skips_an_old_unusable_seal_and_fills_the_contiguous_gap(
    tmp_path: Path,
) -> None:
    env = _Env(tmp_path)
    root = _repo(tmp_path)
    env.register("repo-1", root)

    poison = env.snapshot("repo-1", root, "captured-poison")
    (root / "a.py").write_bytes(b"first\n")
    first = env.snapshot("repo-1", root, "captured-gap-1")
    (root / "a.py").write_bytes(b"second\n")
    env.snapshot("repo-1", root, "captured-gap-2")
    (root / "a.py").write_bytes(b"third\n")
    third = env.snapshot("repo-1", root, "captured-gap-3")

    connection = sqlite3.connect(env.workspace / "workspace.sqlite")
    try:
        workspace_id = str(
            connection.execute(
                "SELECT workspace_id FROM omnivia_workspace_state WHERE singleton = 1"
            ).fetchone()[0]
        )
        installation_id, checkout_id = map(
            str,
            connection.execute(
                "SELECT installation_id, checkout_id "
                "FROM omnivia_engineering_snapshot_captures "
                "WHERE snapshot_id = 'captured-gap-1'"
            ).fetchone(),
        )
    finally:
        connection.close()
    stream_id = engineering_source_capture_execution._derived(
        "src-stream",
        workspace_id,
        "repo-1",
        installation_id,
        checkout_id,
    )

    def payload(
        sequence: int,
        snapshot_id: str,
        manifest_digest: str,
        predecessor: str | None = None,
    ) -> dict[str, object]:
        result: dict[str, object] = {
            "repository_id": "repo-1",
            "stream_id": stream_id,
            "sequence": sequence,
            "snapshot_id": snapshot_id,
            "expected_manifest_digest": manifest_digest,
        }
        if predecessor is not None:
            result["predecessor"] = {
                "sequence": sequence - 1,
                "snapshot_id": predecessor,
            }
        return result

    assert isinstance(
        env.commit(
            payload(1, "captured-gap-1", first.manifest_digest),
            key="gap-first",
            request_id="gap-first",
        ),
        SuccessResponseEnvelope,
    )
    announced = env.commit(
        payload(
            3,
            "captured-gap-3",
            third.manifest_digest,
            predecessor="captured-gap-2",
        ),
        key="gap-third",
        request_id="gap-third",
    )
    assert isinstance(announced, SuccessResponseEnvelope)
    assert announced.result["coverage"]["state"] == "pending"

    runner = env._runner()
    try:
        assert runner.workspace_id is not None and runner.identity is not None
        principal = "local-user"
        fallback = Dispatcher.for_service_operations(
            Grant(
                principal=principal,
                workspaces=frozenset({runner.workspace_id}),
                operations=frozenset(SERVICE_OPERATIONS),
            ),
            None,
        )
        application = build_engineering_application_dispatcher(
            service=runner,
            principal_id=principal,
            installation_id=runner.identity.installation_id,
            workspace_id=runner.workspace_id,
            fallback=fallback,
        )
        executor = EngineeringSourceCaptureExecutor(
            runner=runner, application=application, principal_id=principal
        )
        skipped = executor.run_pending(budget=1, force=True)
        live_turn = executor.run_pending(budget=1, force=True)
        outcome = executor.run_pending(budget=1, force=True)

        assert skipped == engineering_source_capture_execution.SourceProducerPass(
            inspected=1, captured=0, committed=0
        )
        assert live_turn == engineering_source_capture_execution.SourceProducerPass(
            inspected=1, captured=0, committed=0
        )
        assert outcome == engineering_source_capture_execution.SourceProducerPass(
            inspected=1, captured=0, committed=1
        )
        assert runner.connection is not None
        events = runner.connection.execute(
            "SELECT sequence, snapshot_id FROM omnivia_engineering_source_events "
            "WHERE workspace_id = ? AND stream_id = ? ORDER BY sequence",
            (runner.workspace_id, stream_id),
        ).fetchall()
        assert events == [
            (1, "captured-gap-1"),
            (2, "captured-gap-2"),
            (3, "captured-gap-3"),
        ]
        assert runner.connection.execute(
            "SELECT covered_sequence, announced_sequence "
            "FROM omnivia_engineering_source_streams "
            "WHERE workspace_id = ? AND stream_id = ?",
            (runner.workspace_id, stream_id),
        ).fetchone() == (3, 3)
        assert (
            runner.connection.execute(
                "SELECT 1 FROM omnivia_engineering_source_events WHERE snapshot_id = ?",
                (poison.snapshot_id,),
            ).fetchone()
            is None
        )
    finally:
        runner.stop()


def test_producer_never_promotes_a_stale_poison_seal_to_the_stream_head(
    tmp_path: Path,
) -> None:
    env = _Env(tmp_path)
    root = _repo(tmp_path)
    env.register("repo-1", root)

    poison = env.snapshot("repo-1", root, "captured-poison")
    (root / "a.py").write_bytes(b"first\n")
    first = env.snapshot("repo-1", root, "captured-gap-1")
    (root / "a.py").write_bytes(b"second\n")
    env.snapshot("repo-1", root, "captured-gap-2")
    (root / "a.py").write_bytes(b"third\n")
    third = env.snapshot("repo-1", root, "captured-gap-3")

    connection = sqlite3.connect(env.workspace / "workspace.sqlite")
    try:
        workspace_id = str(
            connection.execute(
                "SELECT workspace_id FROM omnivia_workspace_state WHERE singleton = 1"
            ).fetchone()[0]
        )
        installation_id, checkout_id = map(
            str,
            connection.execute(
                "SELECT installation_id, checkout_id "
                "FROM omnivia_engineering_snapshot_captures "
                "WHERE snapshot_id = 'captured-gap-1'"
            ).fetchone(),
        )
    finally:
        connection.close()
    stream_id = engineering_source_capture_execution._derived(
        "src-stream", workspace_id, "repo-1", installation_id, checkout_id
    )

    def payload(
        sequence: int,
        snapshot_id: str,
        manifest_digest: str,
        predecessor: str | None = None,
    ) -> dict[str, object]:
        result: dict[str, object] = {
            "repository_id": "repo-1",
            "stream_id": stream_id,
            "sequence": sequence,
            "snapshot_id": snapshot_id,
            "expected_manifest_digest": manifest_digest,
        }
        if predecessor is not None:
            result["predecessor"] = {
                "sequence": sequence - 1,
                "snapshot_id": predecessor,
            }
        return result

    assert isinstance(
        env.commit(
            payload(1, "captured-gap-1", first.manifest_digest),
            key="restart-first",
            request_id="restart-first",
        ),
        SuccessResponseEnvelope,
    )
    announced = env.commit(
        payload(
            3, "captured-gap-3", third.manifest_digest, predecessor="captured-gap-2"
        ),
        key="restart-third",
        request_id="restart-third",
    )
    assert isinstance(announced, SuccessResponseEnvelope)
    assert announced.result["coverage"]["state"] == "pending"

    def stream_row() -> tuple[int, int]:
        assert runner.connection is not None
        row = runner.connection.execute(
            "SELECT covered_sequence, announced_sequence "
            "FROM omnivia_engineering_source_streams "
            "WHERE workspace_id = ? AND stream_id = ?",
            (runner.workspace_id, stream_id),
        ).fetchone()
        assert row is not None
        return (int(row[0]), int(row[1]))

    runner = env._runner()
    try:
        assert runner.workspace_id is not None and runner.identity is not None
        principal = "local-user"
        fallback = Dispatcher.for_service_operations(
            Grant(
                principal=principal,
                workspaces=frozenset({runner.workspace_id}),
                operations=frozenset(SERVICE_OPERATIONS),
            ),
            None,
        )
        application = build_engineering_application_dispatcher(
            service=runner,
            principal_id=principal,
            installation_id=runner.identity.installation_id,
            workspace_id=runner.workspace_id,
            fallback=fallback,
        )

        # Pass 1: the poison seal is the oldest pending capture and is skipped
        # because it is not the exact named predecessor of the gap.
        pass_one = EngineeringSourceCaptureExecutor(
            runner=runner, application=application, principal_id=principal
        )
        skipped = pass_one.run_pending(budget=1, force=True)
        assert skipped == engineering_source_capture_execution.SourceProducerPass(
            inspected=1, captured=0, committed=0
        )

        # Pass 2 alternates to the live lane, which refuses to capture a new
        # head while the stream has a gap.
        live_turn = pass_one.run_pending(budget=1, force=True)
        assert live_turn == engineering_source_capture_execution.SourceProducerPass(
            inspected=1, captured=0, committed=0
        )

        # Pass 3 returns to recovery. The cursor has advanced past the poison,
        # so the exact sealed predecessor for sequence 2 closes the gap.
        filled = pass_one.run_pending(budget=1, force=True)
        assert filled == engineering_source_capture_execution.SourceProducerPass(
            inspected=1, captured=0, committed=1
        )
        assert stream_row() == (3, 3)

        # The next lane was durably advanced before the recovery effect. A new
        # executor therefore resumes with checkout capture after restart.
        (root / "a.py").write_bytes(b"fourth\n")
        restarted = EngineeringSourceCaptureExecutor(
            runner=runner, application=application, principal_id=principal
        )
        post_restart = restarted.run_pending(budget=1, force=True)
        assert post_restart == engineering_source_capture_execution.SourceProducerPass(
            inspected=1, captured=1, committed=1
        )
        assert (
            runner.connection is not None
            and runner.connection.execute(
                "SELECT 1 FROM omnivia_engineering_source_events "
                "WHERE snapshot_id = ?",
                (poison.snapshot_id,),
            ).fetchone()
            is None
        )
        assert stream_row() == (4, 4)

        assert runner.connection is not None
        events = runner.connection.execute(
            "SELECT sequence, snapshot_id, predecessor_snapshot_id "
            "FROM omnivia_engineering_source_events "
            "WHERE workspace_id = ? AND stream_id = ? ORDER BY sequence",
            (runner.workspace_id, stream_id),
        ).fetchall()
        assert [row[0] for row in events] == [1, 2, 3, 4]
        assert events[3][2] == "captured-gap-3"
        assert events[3][1] != poison.snapshot_id
        assert stream_row() == (4, 4)
    finally:
        runner.stop()


def test_producer_fairly_splits_recovery_and_live_capture_across_restart(
    tmp_path: Path,
) -> None:
    env = _Env(tmp_path)
    root = _repo(tmp_path)
    env.register("repo-1", root)

    first = env.snapshot("repo-1", root, "captured-base-1")
    (root / "a.py").write_bytes(b"poison-a\n")
    poison_a = env.snapshot("repo-1", root, "captured-a-poison")
    (root / "a.py").write_bytes(b"poison-b\n")
    poison_b = env.snapshot("repo-1", root, "captured-b-poison")

    connection = sqlite3.connect(env.workspace / "workspace.sqlite")
    try:
        workspace_id = str(
            connection.execute(
                "SELECT workspace_id FROM omnivia_workspace_state WHERE singleton = 1"
            ).fetchone()[0]
        )
        installation_id, checkout_id = map(
            str,
            connection.execute(
                "SELECT installation_id, checkout_id "
                "FROM omnivia_engineering_snapshot_captures "
                "WHERE snapshot_id = ?",
                (first.snapshot_id,),
            ).fetchone(),
        )
    finally:
        connection.close()
    stream_id = engineering_source_capture_execution._derived(
        "src-stream", workspace_id, "repo-1", installation_id, checkout_id
    )

    def payload(
        sequence: int,
        snapshot_id: str,
        manifest_digest: str,
        predecessor: str | None = None,
    ) -> dict[str, object]:
        result: dict[str, object] = {
            "repository_id": "repo-1",
            "stream_id": stream_id,
            "sequence": sequence,
            "snapshot_id": snapshot_id,
            "expected_manifest_digest": manifest_digest,
        }
        if predecessor is not None:
            result["predecessor"] = {
                "sequence": sequence - 1,
                "snapshot_id": predecessor,
            }
        return result

    assert isinstance(
        env.commit(
            payload(1, first.snapshot_id, first.manifest_digest),
            key="fair-base",
            request_id="fair-base",
        ),
        SuccessResponseEnvelope,
    )

    def application_for(runner: ServiceRunner) -> object:
        assert runner.workspace_id is not None and runner.identity is not None
        principal = "local-user"
        fallback = Dispatcher.for_service_operations(
            Grant(
                principal=principal,
                workspaces=frozenset({runner.workspace_id}),
                operations=frozenset(SERVICE_OPERATIONS),
            ),
            None,
        )
        return build_engineering_application_dispatcher(
            service=runner,
            principal_id=principal,
            installation_id=runner.identity.installation_id,
            workspace_id=runner.workspace_id,
            fallback=fallback,
        )

    # With one unit, the first pass inspects recovery and the next pass must
    # alternate to the changed checkout. The fresh capture, rather than either
    # poison seal, becomes sequence 2.
    (root / "a.py").write_bytes(b"fresh-two\n")
    runner = env._runner()
    try:
        executor = EngineeringSourceCaptureExecutor(
            runner=runner,
            application=application_for(runner),
            principal_id="local-user",
        )
        recovery_turn = executor.run_pending(budget=1, force=True)
        live_turn = executor.run_pending(budget=1, force=True)
        assert recovery_turn == engineering_source_capture_execution.SourceProducerPass(
            inspected=1, captured=0, committed=0
        )
        assert live_turn == engineering_source_capture_execution.SourceProducerPass(
            inspected=1, captured=1, committed=1
        )
        assert recovery_turn.inspected <= 1 and live_turn.inspected <= 1
        assert runner.connection is not None
        first_events = runner.connection.execute(
            "SELECT sequence, snapshot_id, predecessor_snapshot_id "
            "FROM omnivia_engineering_source_events "
            "WHERE workspace_id = ? AND stream_id = ? ORDER BY sequence",
            (runner.workspace_id, stream_id),
        ).fetchall()
        assert [row[0] for row in first_events] == [1, 2]
        assert first_events[1][1] not in {
            poison_a.snapshot_id,
            poison_b.snapshot_id,
        }
        fresh_two_id = str(first_events[1][1])
        assert first_events[1][2] == first.snapshot_id

        # With two unmatched seals still pending, a two-unit pass may spend
        # only one unit on recovery. Its reserved live unit observes another
        # checkout change and appends sequence 3.
        (root / "a.py").write_bytes(b"fresh-three\n")
        split = executor.run_pending(budget=2, force=True)
        assert split == engineering_source_capture_execution.SourceProducerPass(
            inspected=2, captured=1, committed=1
        )
        assert split.inspected <= 2
        first_events = runner.connection.execute(
            "SELECT sequence, snapshot_id, predecessor_snapshot_id "
            "FROM omnivia_engineering_source_events "
            "WHERE workspace_id = ? AND stream_id = ? ORDER BY sequence",
            (runner.workspace_id, stream_id),
        ).fetchall()
        assert [row[0] for row in first_events] == [1, 2, 3]
        fresh_three_id = str(first_events[2][1])
        assert fresh_three_id not in {
            poison_a.snapshot_id,
            poison_b.snapshot_id,
        }
        assert first_events[2][2] == fresh_two_id
    finally:
        runner.stop()

    # The split pass persisted checkout as the next lane. A service restart
    # therefore reaches the changed checkout on its first one-unit pass.
    (root / "a.py").write_bytes(b"fresh-four\n")
    restarted_runner = env._runner()
    try:
        restarted = EngineeringSourceCaptureExecutor(
            runner=restarted_runner,
            application=application_for(restarted_runner),
            principal_id="local-user",
        )
        after_restart_live = restarted.run_pending(budget=1, force=True)
        assert after_restart_live == (
            engineering_source_capture_execution.SourceProducerPass(
                inspected=1, captured=1, committed=1
            )
        )
        recovery_after_live = restarted.run_pending(budget=1, force=True)
        assert recovery_after_live.inspected <= 1
        assert recovery_after_live.captured == 0
        assert recovery_after_live.committed == 0
        assert restarted_runner.connection is not None
        before_gap = restarted_runner.connection.execute(
            "SELECT sequence, snapshot_id, predecessor_snapshot_id "
            "FROM omnivia_engineering_source_events "
            "WHERE workspace_id = ? AND stream_id = ? ORDER BY sequence",
            (restarted_runner.workspace_id, stream_id),
        ).fetchall()
        assert [row[0] for row in before_gap] == [1, 2, 3, 4]
        fresh_four_id = str(before_gap[3][1])
        assert fresh_four_id not in {
            poison_a.snapshot_id,
            poison_b.snapshot_id,
        }
        assert before_gap[3][2] == fresh_three_id
    finally:
        restarted_runner.stop()

    # Build a durable gap after the service stops. Its exact predecessor is
    # later in the same pending order as both poison seals.
    (root / "a.py").write_bytes(b"gap-five\n")
    gap_five = env.snapshot("repo-1", root, "captured-z-gap-5")
    (root / "a.py").write_bytes(b"gap-six\n")
    gap_six = env.snapshot("repo-1", root, "captured-z-gap-6")
    announced = env.commit(
        payload(
            6,
            gap_six.snapshot_id,
            gap_six.manifest_digest,
            predecessor=gap_five.snapshot_id,
        ),
        key="fair-gap-six",
        request_id="fair-gap-six",
    )
    assert isinstance(announced, SuccessResponseEnvelope)
    assert announced.result["coverage"] == {
        "state": "pending",
        "covered_sequence": 4,
        "announced_sequence": 6,
    }
    (root / "a.py").write_bytes(b"fresh-four\n")

    # A second restart keeps the persisted lane and queue cursors. Recovery
    # reaches the exact gap predecessor without rescanning capture history.
    gap_runner = env._runner()
    try:
        gap_executor = EngineeringSourceCaptureExecutor(
            runner=gap_runner,
            application=application_for(gap_runner),
            principal_id="local-user",
        )
        passes = [gap_executor.run_pending(budget=1, force=True) for _ in range(7)]
        assert all(result.inspected <= 1 for result in passes)
        assert any(result.committed == 1 for result in passes)
        assert sum(result.committed for result in passes) == 1

        assert gap_runner.connection is not None
        events = gap_runner.connection.execute(
            "SELECT sequence, snapshot_id, predecessor_snapshot_id "
            "FROM omnivia_engineering_source_events "
            "WHERE workspace_id = ? AND stream_id = ? ORDER BY sequence",
            (gap_runner.workspace_id, stream_id),
        ).fetchall()
        assert [row[0] for row in events] == [1, 2, 3, 4, 5, 6]
        assert events[1][1] == fresh_two_id
        assert events[2][1] == fresh_three_id
        assert events[3][1] == fresh_four_id
        assert events[4] == (5, gap_five.snapshot_id, fresh_four_id)
        assert events[5] == (6, gap_six.snapshot_id, gap_five.snapshot_id)
        assert not {
            poison_a.snapshot_id,
            poison_b.snapshot_id,
        }.intersection(str(row[1]) for row in events)
        assert gap_runner.connection.execute(
            "SELECT covered_sequence, announced_sequence "
            "FROM omnivia_engineering_source_streams "
            "WHERE workspace_id = ? AND stream_id = ?",
            (gap_runner.workspace_id, stream_id),
        ).fetchone() == (6, 6)
    finally:
        gap_runner.stop()


def test_producer_recovers_exact_crash_intent_and_refuses_a_stale_head(
    tmp_path: Path,
) -> None:
    env = _Env(tmp_path)
    root = _repo(tmp_path)
    env.register("repo-1", root)
    base = env.snapshot("repo-1", root, "captured-intent-base")

    connection = sqlite3.connect(env.workspace / "workspace.sqlite")
    try:
        workspace_id = str(
            connection.execute(
                "SELECT workspace_id FROM omnivia_workspace_state WHERE singleton = 1"
            ).fetchone()[0]
        )
        installation_id, checkout_id = map(
            str,
            connection.execute(
                "SELECT installation_id, checkout_id "
                "FROM omnivia_engineering_snapshot_captures WHERE snapshot_id = ?",
                (base.snapshot_id,),
            ).fetchone(),
        )
    finally:
        connection.close()
    stream_id = engineering_source_producer.source_stream_id(
        workspace_id, "repo-1", installation_id, checkout_id
    )

    def payload(
        sequence: int,
        snapshot_id: str,
        manifest_digest: str,
        predecessor: str | None = None,
    ) -> dict[str, object]:
        result: dict[str, object] = {
            "repository_id": "repo-1",
            "stream_id": stream_id,
            "sequence": sequence,
            "snapshot_id": snapshot_id,
            "expected_manifest_digest": manifest_digest,
        }
        if predecessor is not None:
            result["predecessor"] = {
                "sequence": sequence - 1,
                "snapshot_id": predecessor,
            }
        return result

    assert isinstance(
        env.commit(
            payload(1, base.snapshot_id, base.manifest_digest),
            key="intent-base",
            request_id="intent-base",
        ),
        SuccessResponseEnvelope,
    )

    def application_for(runner: ServiceRunner) -> object:
        assert runner.workspace_id is not None and runner.identity is not None
        principal = "local-user"
        fallback = Dispatcher.for_service_operations(
            Grant(
                principal=principal,
                workspaces=frozenset({runner.workspace_id}),
                operations=frozenset(SERVICE_OPERATIONS),
            ),
            None,
        )
        return build_engineering_application_dispatcher(
            service=runner,
            principal_id=principal,
            installation_id=runner.identity.installation_id,
            workspace_id=runner.workspace_id,
            fallback=fallback,
        )

    runner = env._runner()
    try:
        (root / "a.py").write_bytes(b"intent-two\n")
        executor = EngineeringSourceCaptureExecutor(
            runner=runner,
            application=application_for(runner),
            principal_id="local-user",
        )
        appended = executor.run_pending(budget=1, force=True)
        assert appended.committed == 1
        assert runner.connection is not None
        second_id = str(
            runner.connection.execute(
                "SELECT snapshot_id FROM omnivia_engineering_source_events "
                "WHERE workspace_id = ? AND stream_id = ? AND sequence = 2",
                (runner.workspace_id, stream_id),
            ).fetchone()[0]
        )

        # Seal the next head with its exact frontier, then stop before dispatch.
        (root / "a.py").write_bytes(b"intent-three\n")
        frozen = capture_working_tree_manifest(checkout_root=root)
        gap_id = engineering_source_capture_execution._derived(
            "src-snapshot",
            workspace_id,
            "repo-1",
            installation_id,
            checkout_id,
            frozen.manifest_digest,
        )
        capture_working_tree_snapshot_owned(
            runner,
            repository_id="repo-1",
            checkout_root=root,
            snapshot_id=gap_id,
            manifest=frozen,
            renew_lease=runner.renew_lease_if_due,
            producer_expected_frontier=2,
            producer_expected_predecessor_snapshot_id=second_id,
        )
        assert runner.connection.execute(
            "SELECT expected_frontier, expected_predecessor_snapshot_id, state "
            "FROM omnivia_engineering_source_producer_queue "
            "WHERE workspace_id = ? AND snapshot_id = ?",
            (runner.workspace_id, gap_id),
        ).fetchone() == (2, second_id, "pending")
    finally:
        runner.stop()

    # A later event may announce the captured intent as its exact predecessor.
    # Recovery must fill that gap from the persisted plan after a restart.
    (root / "a.py").write_bytes(b"intent-four\n")
    fourth = env.snapshot("repo-1", root, "captured-intent-four")
    announced = env.commit(
        payload(4, fourth.snapshot_id, fourth.manifest_digest, predecessor=gap_id),
        key="intent-four",
        request_id="intent-four",
    )
    assert isinstance(announced, SuccessResponseEnvelope)
    assert announced.result["coverage"]["state"] == "pending"

    checkout_turn = env._runner()
    try:
        executor = EngineeringSourceCaptureExecutor(
            runner=checkout_turn,
            application=application_for(checkout_turn),
            principal_id="local-user",
        )
        refused_live = executor.run_pending(budget=1, force=True)
        assert refused_live == engineering_source_capture_execution.SourceProducerPass(
            inspected=1, captured=0, committed=0
        )
    finally:
        checkout_turn.stop()

    recovery_turn = env._runner()
    try:
        executor = EngineeringSourceCaptureExecutor(
            runner=recovery_turn,
            application=application_for(recovery_turn),
            principal_id="local-user",
        )
        recovered = executor.run_pending(budget=1, force=True)
        assert recovered == engineering_source_capture_execution.SourceProducerPass(
            inspected=1, captured=0, committed=1
        )
        assert recovery_turn.connection is not None
        events = recovery_turn.connection.execute(
            "SELECT sequence, snapshot_id, predecessor_snapshot_id "
            "FROM omnivia_engineering_source_events "
            "WHERE workspace_id = ? AND stream_id = ? ORDER BY sequence",
            (recovery_turn.workspace_id, stream_id),
        ).fetchall()
        assert events == [
            (1, base.snapshot_id, None),
            (2, second_id, base.snapshot_id),
            (3, gap_id, second_id),
            (4, fourth.snapshot_id, gap_id),
        ]

        # A second exact intent cannot be rebound after another append wins.
        (root / "a.py").write_bytes(b"stale-five\n")
        stale_manifest = capture_working_tree_manifest(checkout_root=root)
        stale_id = engineering_source_capture_execution._derived(
            "src-snapshot",
            workspace_id,
            "repo-1",
            installation_id,
            checkout_id,
            stale_manifest.manifest_digest,
        )
        capture_working_tree_snapshot_owned(
            recovery_turn,
            repository_id="repo-1",
            checkout_root=root,
            snapshot_id=stale_id,
            manifest=stale_manifest,
            renew_lease=recovery_turn.renew_lease_if_due,
            producer_expected_frontier=4,
            producer_expected_predecessor_snapshot_id=fourth.snapshot_id,
        )
    finally:
        recovery_turn.stop()

    (root / "a.py").write_bytes(b"winner-five\n")
    winner = env.snapshot("repo-1", root, "captured-winner-five")
    assert isinstance(
        env.commit(
            payload(
                5,
                winner.snapshot_id,
                winner.manifest_digest,
                predecessor=fourth.snapshot_id,
            ),
            key="winner-five",
            request_id="winner-five",
        ),
        SuccessResponseEnvelope,
    )
    (root / "a.py").write_bytes(b"stale-five\n")

    stale_runner = env._runner()
    try:
        stale_executor = EngineeringSourceCaptureExecutor(
            runner=stale_runner,
            application=application_for(stale_runner),
            principal_id="local-user",
        )
        checkout_refusal = stale_executor.run_pending(budget=1, force=True)
        assert checkout_refusal.inspected == 1
        assert checkout_refusal.committed == 0
        retried = stale_executor.run_pending(budget=1, force=True)
        assert retried.inspected == 1
        assert retried.committed == 0
        assert stale_runner.connection is not None
        assert stale_runner.connection.execute(
            "SELECT expected_frontier, expected_predecessor_snapshot_id, state "
            "FROM omnivia_engineering_source_producer_queue "
            "WHERE workspace_id = ? AND snapshot_id = ?",
            (stale_runner.workspace_id, stale_id),
        ).fetchone() == (4, fourth.snapshot_id, "retry")
        assert stale_runner.connection.execute(
            "SELECT 1 FROM omnivia_engineering_source_events "
            "WHERE workspace_id = ? AND snapshot_id = ?",
            (stale_runner.workspace_id, stale_id),
        ).fetchone() is None
    finally:
        stale_runner.stop()


def test_large_capture_history_uses_bounded_durable_keysets(tmp_path: Path) -> None:
    env = _Env(tmp_path)
    root = _repo(tmp_path)
    env.register("repo-1", root)
    runner = env._runner()
    history_size = engineering_source_producer.LEGACY_SEED_BATCH * 2 + 2
    try:
        assert (
            runner.connection is not None
            and runner.identity is not None
            and runner.workspace_id is not None
            and runner.generation is not None
        )
        frozen = capture_working_tree_manifest(checkout_root=root)
        for index in range(history_size):
            capture_working_tree_snapshot_owned(
                runner,
                repository_id="repo-1",
                checkout_root=root,
                snapshot_id=f"captured-history-{index:04d}",
                manifest=frozen,
                renew_lease=runner.renew_lease_if_due,
            )

        now_us = max(
            1, int(runner.clock.wall_time().timestamp() * 1_000_000)
        )
        with runner.sqlite_gate:
            first_seeded = engineering_source_producer.seed_legacy_captures(
                runner.connection,
                runner.identity,
                workspace_id=runner.workspace_id,
                installation_id=runner.identity.installation_id,
                fencing_generation=runner.generation,
                now_us=now_us,
            )
            ceiling = runner.connection.execute(
                "SELECT legacy_seed_through_captured_at_us, "
                "legacy_seed_through_snapshot_id "
                "FROM omnivia_engineering_source_producer_state "
                "WHERE workspace_id = ? AND installation_id = ?",
                (runner.workspace_id, runner.identity.installation_id),
            ).fetchone()
        assert first_seeded == engineering_source_producer.LEGACY_SEED_BATCH
        assert ceiling is not None

        # A capture created after the ceiling enqueues atomically but cannot move
        # the finite historical backfill target.
        late = capture_working_tree_snapshot_owned(
            runner,
            repository_id="repo-1",
            checkout_root=root,
            snapshot_id="captured-history-late",
            manifest=frozen,
            renew_lease=runner.renew_lease_if_due,
        )
        batches = [first_seeded]
        while True:
            with runner.sqlite_gate:
                seeded = engineering_source_producer.seed_legacy_captures(
                    runner.connection,
                    runner.identity,
                    workspace_id=runner.workspace_id,
                    installation_id=runner.identity.installation_id,
                    fencing_generation=runner.generation,
                    now_us=now_us,
                )
            if seeded == 0:
                break
            batches.append(seeded)
        assert batches == [engineering_source_producer.LEGACY_SEED_BATCH] * 2 + [2]

        with runner.sqlite_gate:
            state = runner.connection.execute(
                "SELECT legacy_cursor_captured_at_us, legacy_cursor_snapshot_id, "
                "legacy_seed_through_captured_at_us, "
                "legacy_seed_through_snapshot_id, legacy_seed_complete "
                "FROM omnivia_engineering_source_producer_state "
                "WHERE workspace_id = ? AND installation_id = ?",
                (runner.workspace_id, runner.identity.installation_id),
            ).fetchone()
            late_time = int(
                runner.connection.execute(
                    "SELECT captured_at_us FROM omnivia_engineering_snapshot_captures "
                    "WHERE workspace_id = ? AND snapshot_id = ?",
                    (runner.workspace_id, late.snapshot_id),
                ).fetchone()[0]
            )
            queue_plan = " ".join(
                str(row[3])
                for row in runner.connection.execute(
                    "EXPLAIN QUERY PLAN SELECT repository_id, snapshot_id, "
                    "checkout_id, stream_id, expected_frontier, "
                    "expected_predecessor_snapshot_id, available_at_us "
                    "FROM omnivia_engineering_source_producer_queue "
                    "WHERE workspace_id = ? AND installation_id = ? "
                    "AND state = ? AND available_at_us <= ? "
                    "AND (available_at_us, snapshot_id) > (?, ?) "
                    "ORDER BY available_at_us, snapshot_id LIMIT ?",
                    (
                        runner.workspace_id,
                        runner.identity.installation_id,
                        "pending",
                        now_us,
                        1,
                        "captured-history-0000",
                        7,
                    ),
                ).fetchall()
            ).lower()
            seed_plan = " ".join(
                str(row[3])
                for row in runner.connection.execute(
                    "EXPLAIN QUERY PLAN SELECT captured_at_us, snapshot_id, "
                    "repository_id, checkout_id "
                    "FROM omnivia_engineering_snapshot_captures "
                    "WHERE workspace_id = ? AND installation_id = ? "
                    "AND (captured_at_us, snapshot_id) > (?, ?) "
                    "AND (captured_at_us, snapshot_id) <= (?, ?) "
                    "ORDER BY captured_at_us, snapshot_id LIMIT ?",
                    (
                        runner.workspace_id,
                        runner.identity.installation_id,
                        1,
                        "captured-history-0000",
                        int(ceiling[0]),
                        str(ceiling[1]),
                        engineering_source_producer.LEGACY_SEED_BATCH,
                    ),
                ).fetchall()
            ).lower()
            checkout_plan = " ".join(
                str(row[3])
                for row in runner.connection.execute(
                    "EXPLAIN QUERY PLAN SELECT repository_id, checkout_id, "
                    "checkout_hint FROM omnivia_engineering_checkouts "
                    "WHERE workspace_id = ? AND installation_id = ? "
                    "AND checkout_id > ? ORDER BY checkout_id LIMIT 1",
                    (
                        runner.workspace_id,
                        runner.identity.installation_id,
                        "",
                    ),
                ).fetchall()
            ).lower()
            event_probe_plan = " ".join(
                str(row[3])
                for row in runner.connection.execute(
                    "EXPLAIN QUERY PLAN SELECT stream_id, recorded_at_us, "
                    "manifest_format FROM omnivia_engineering_source_events "
                    "WHERE workspace_id = ? AND snapshot_id = ?",
                    (runner.workspace_id, "captured-history-0000"),
                ).fetchall()
            ).lower()
            first_batch = engineering_source_producer.take_queue_items(
                runner.connection,
                runner.identity,
                workspace_id=runner.workspace_id,
                installation_id=runner.identity.installation_id,
                fencing_generation=runner.generation,
                now_us=now_us,
                limit=7,
            )

        assert state is not None
        assert tuple(state[:2]) == tuple(state[2:4]) == tuple(ceiling)
        assert int(state[4]) == 1
        assert (late_time, late.snapshot_id) > (int(ceiling[0]), str(ceiling[1]))
        assert "using covering index omnivia_idx_engineering_source_producer_eligible" in queue_plan
        assert "using covering index omnivia_idx_engineering_capture_producer_seed" in seed_plan
        assert "using covering index omnivia_idx_engineering_checkout_producer" in checkout_plan
        assert "search omnivia_engineering_source_events" in event_probe_plan
        assert "snapshot_id=?" in event_probe_plan
        assert len(first_batch) == 7
        first_ids = {item.snapshot_id for item in first_batch}
        assert os.fspath(root) not in repr(first_batch)
    finally:
        runner.stop()

    restarted = env._runner()
    try:
        assert (
            restarted.connection is not None
            and restarted.identity is not None
            and restarted.workspace_id is not None
            and restarted.generation is not None
        )
        with restarted.sqlite_gate:
            second_batch = engineering_source_producer.take_queue_items(
                restarted.connection,
                restarted.identity,
                workspace_id=restarted.workspace_id,
                installation_id=restarted.identity.installation_id,
                fencing_generation=restarted.generation,
                now_us=max(
                    1,
                    int(restarted.clock.wall_time().timestamp() * 1_000_000),
                ),
                limit=7,
            )
        assert len(second_batch) == 7
        assert first_ids.isdisjoint(item.snapshot_id for item in second_batch)
        wrapped = False
        for _ in range(3):
            with restarted.sqlite_gate:
                batch = engineering_source_producer.take_queue_items(
                    restarted.connection,
                    restarted.identity,
                    workspace_id=restarted.workspace_id,
                    installation_id=restarted.identity.installation_id,
                    fencing_generation=restarted.generation,
                    now_us=max(
                        1,
                        int(restarted.clock.wall_time().timestamp() * 1_000_000),
                    ),
                    limit=engineering_source_producer.MAX_QUEUE_BATCH,
                )
            assert len(batch) <= engineering_source_producer.MAX_QUEUE_BATCH
            wrapped = wrapped or first_batch[0].snapshot_id in {
                item.snapshot_id for item in batch
            }
        assert wrapped
    finally:
        restarted.stop()


def test_0059_upgrade_seeds_a_preexisting_0058_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    full_catalogue = migrations_module.load_migrations()
    through_0058 = tuple(item for item in full_catalogue if item.version <= 58)
    old: captured_schema.Workspace | None = None
    try:
        with monkeypatch.context() as old_schema:
            old_schema.setattr(
                migrations_module, "load_migrations", lambda: through_0058
            )
            canonical_schema_tables.cache_clear()
            canonical_schema_fingerprint.cache_clear()
            guarded_tables.cache_clear()

            old = captured_schema.Workspace(tmp_path)
            legacy = captured_schema._seal(
                old,
                repository_id="repo-upgrade",
                stream_id=engineering_source_producer.source_stream_id(
                    captured_schema.WORKSPACE_ID,
                    "repo-upgrade",
                    "inst-1",
                    "checkout-upgrade",
                ),
                principal_id=captured_schema.esc.PRINCIPAL,
                checkout_id="checkout-upgrade",
                snapshot_id="captured-before-0059",
                files={},
                base_us=8_000_000,
            )
            assert max(applied_migrations(old.holder.connection)) == 58
            assert old.holder.connection.execute(
                "SELECT 1 FROM sqlite_schema WHERE type = 'table' "
                "AND name = 'omnivia_engineering_source_producer_queue'"
            ).fetchone() is None

        canonical_schema_tables.cache_clear()
        canonical_schema_fingerprint.cache_clear()
        guarded_tables.cache_clear()
        applied = apply_pending_migrations(
            old.holder.connection,
            mode=OpenMode.SERVICE_OWNED,
            service_instance_id=old.holder.identity.service_instance_id,
            fencing_generation=old.holder.generation,
            workspace_id=captured_schema.WORKSPACE_ID,
        )
        assert [item.version for item in applied] == [59, 60]
        assert old.holder.connection.execute(
            "SELECT COUNT(*) FROM omnivia_engineering_source_producer_queue"
        ).fetchone() == (0,)
        assert old.holder.connection.execute(
            "SELECT covered_sequence, processed_sequence, "
            "pending_dependent_record_id, pending_dependent_version "
            "FROM omnivia_engineering_source_streams WHERE workspace_id = ? "
            "AND stream_id = ?",
            (captured_schema.WORKSPACE_ID, legacy.stream_id),
        ).fetchone() == (1, 0, None, None)
        seeded = engineering_source_producer.seed_legacy_captures(
            old.holder.connection,
            old.holder.identity,
            workspace_id=captured_schema.WORKSPACE_ID,
            installation_id=legacy.installation_id,
            fencing_generation=old.holder.generation,
            now_us=legacy.captured_at_us + 100,
        )
        assert seeded == 1
        assert old.holder.connection.execute(
            "SELECT snapshot_id, state, expected_frontier, "
            "expected_predecessor_snapshot_id "
            "FROM omnivia_engineering_source_producer_queue"
        ).fetchone() == (legacy.snapshot_id, "settled", None, None)
    finally:
        canonical_schema_tables.cache_clear()
        canonical_schema_fingerprint.cache_clear()
        guarded_tables.cache_clear()
        if old is not None:
            old.holder.connection.close()


def test_producer_lost_reply_recovers_without_a_duplicate_event(tmp_path: Path) -> None:
    env = _Env(tmp_path)
    root = _repo(tmp_path)
    env.register("repo-1", root)
    runner = env._runner()
    try:
        assert runner.workspace_id is not None and runner.identity is not None
        frozen = capture_working_tree_manifest(checkout_root=root)
        snapshot_id = engineering_source_capture_execution._derived(
            "src-snapshot",
            runner.workspace_id,
            "repo-1",
            runner.identity.installation_id,
            "co-repo-1",
            frozen.manifest_digest,
        )
        capture_working_tree_snapshot_owned(
            runner,
            repository_id="repo-1",
            checkout_root=root,
            snapshot_id=snapshot_id,
            manifest=frozen,
            renew_lease=runner.renew_lease_if_due,
        )
        principal = "local-user"
        fallback = Dispatcher.for_service_operations(
            Grant(
                principal=principal,
                workspaces=frozenset({runner.workspace_id}),
                operations=frozenset(SERVICE_OPERATIONS),
            ),
            None,
        )
        application = build_engineering_application_dispatcher(
            service=runner,
            principal_id=principal,
            installation_id=runner.identity.installation_id,
            workspace_id=runner.workspace_id,
            fallback=fallback,
        )

        class LostReply:
            def dispatch(self, request: RequestEnvelope) -> object:
                application.dispatch(request)
                raise RuntimeError("reply was lost after settlement")

        crashing = EngineeringSourceCaptureExecutor(
            runner=runner, application=LostReply(), principal_id=principal
        )
        with pytest.raises(RuntimeError, match="reply was lost"):
            crashing.run_pending(budget=1, force=True)
        assert runner.connection is not None
        assert runner.connection.execute(
            "SELECT state FROM omnivia_engineering_source_producer_queue "
            "WHERE workspace_id = ? AND snapshot_id = ?",
            (runner.workspace_id, snapshot_id),
        ).fetchone() == ("settled",)

        restarted = EngineeringSourceCaptureExecutor(
            runner=runner, application=application, principal_id=principal
        )
        recovered = restarted.run_pending(budget=1, force=True)
        assert recovered.inspected == 1
        assert recovered.committed == 0
        assert runner.connection is not None
        assert runner.connection.execute(
            "SELECT COUNT(*) FROM omnivia_engineering_source_events "
            "WHERE snapshot_id = ?",
            (snapshot_id,),
        ).fetchone() == (1,)
    finally:
        runner.stop()


def test_producer_releases_the_sqlite_gate_during_filesystem_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _Env(tmp_path)
    root = _repo(tmp_path)
    env.register("repo-1", root)
    runner = env._runner()
    started = threading.Event()
    release = threading.Event()
    frozen = capture_working_tree_manifest(checkout_root=root)
    failures: list[BaseException] = []
    try:
        assert runner.workspace_id is not None and runner.identity is not None
        principal = "local-user"
        fallback = Dispatcher.for_service_operations(
            Grant(
                principal=principal,
                workspaces=frozenset({runner.workspace_id}),
                operations=frozenset(SERVICE_OPERATIONS),
            ),
            None,
        )
        application = build_engineering_application_dispatcher(
            service=runner,
            principal_id=principal,
            installation_id=runner.identity.installation_id,
            workspace_id=runner.workspace_id,
            fallback=fallback,
        )

        def slow_manifest(
            *, checkout_root: Path, heartbeat: object | None = None
        ) -> source_capture.WorkingTreeManifest:
            assert checkout_root == root
            started.set()
            assert release.wait(timeout=5)
            return frozen

        monkeypatch.setattr(
            engineering_source_capture_execution,
            "capture_working_tree_manifest",
            slow_manifest,
        )
        executor = EngineeringSourceCaptureExecutor(
            runner=runner, application=application, principal_id=principal
        )

        def produce() -> None:
            try:
                executor.run_pending(budget=1, force=True)
            except BaseException as error:  # noqa: BLE001 - returned to test thread
                failures.append(error)

        worker = threading.Thread(target=produce)
        worker.start()
        assert started.wait(timeout=5)
        assert runner.sqlite_gate.acquire(timeout=1), (
            "filesystem capture held the shared SQLite gate"
        )
        try:
            assert runner.connection is not None
            assert runner.connection.execute("SELECT 1").fetchone() == (1,)
        finally:
            runner.sqlite_gate.release()
        release.set()
        worker.join(timeout=10)
        assert not worker.is_alive()
        assert not failures
    finally:
        release.set()
        runner.stop()
