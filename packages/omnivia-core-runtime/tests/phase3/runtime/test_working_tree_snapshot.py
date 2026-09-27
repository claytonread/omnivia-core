"""Service-owned persistence of a working-tree manifest as an authoritative snapshot."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from omnivia_core_runtime.ownership.fencing import fenced_transaction
from omnivia_core_runtime.service import source_capture
from omnivia_core_runtime.service.runner import ServiceRunner, ServiceSettings
from omnivia_core_runtime.service.source_capture import (
    SourceCaptureRefused,
    capture_working_tree_snapshot,
)
from omnivia_core_runtime.service.versions import SERVER_VERSION
from omnivia_core_runtime.service.workspace_init import initialise_workspace
from omnivia_core_runtime.storage import repository_identity

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

    (root / "a.py").write_bytes(b"different\n")
    with pytest.raises(SourceCaptureRefused, match="different content"):
        env.snapshot("repo-1", root)
    assert env.rows("omnivia_engineering_snapshots") == 1
    # The changed tree is a distinct snapshot under its own identity.
    other = env.snapshot("repo-1", root, "snap-2")
    assert other.status == "captured" and other.manifest_digest != first.manifest_digest


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
    assert {"path": "link", "reason": "symlink"} in manifest["omissions"]


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
