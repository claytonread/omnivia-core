"""Service-owned persistence of a working-tree manifest as an authoritative snapshot."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from omnivia_core_runtime.ownership.fencing import fenced_transaction
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
from omnivia_core_runtime.storage import engineering_source, repository_identity

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
        outcome = executor.run_pending(budget=1, force=True)

        assert skipped == engineering_source_capture_execution.SourceProducerPass(
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

        # Pass 2: the cursor has advanced past the poison, so the exact
        # sealed predecessor for sequence 2 is found and the gap closes.
        filled = pass_one.run_pending(budget=1, force=True)
        assert filled == engineering_source_capture_execution.SourceProducerPass(
            inspected=1, captured=0, committed=1
        )
        assert stream_row() == (3, 3)

        # Restart the executor: in-memory cursors reset, so the poison seal
        # (still unmatched to any event) is the oldest pending capture again.
        restarted = EngineeringSourceCaptureExecutor(
            runner=runner, application=application, principal_id=principal
        )
        pass_three = restarted.run_pending(budget=1, force=True)
        assert pass_three == engineering_source_capture_execution.SourceProducerPass(
            inspected=1, captured=0, committed=0
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
        assert stream_row() == (3, 3)

        # Change the actual registered checkout: a fresh producer capture,
        # tied to the frontier observed just before it, may append sequence 4.
        (root / "a.py").write_bytes(b"fourth\n")
        fresh = EngineeringSourceCaptureExecutor(
            runner=runner, application=application, principal_id=principal
        )
        appended = fresh.run_pending(budget=2, force=True)
        assert appended == engineering_source_capture_execution.SourceProducerPass(
            inspected=2, captured=1, committed=1
        )
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
