"""Qualification of the service-owned local evidence capture path."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path

import pytest
from omnivia_core_runtime.service import source_capture
from omnivia_core_runtime.service.runner import ServiceRunner, ServiceSettings
from omnivia_core_runtime.service.source_capture import (
    MAX_SOURCE_BYTES,
    SourceCaptureRefused,
    capture_local_source,
    publish_blob,
    read_checkout_file,
)
from omnivia_core_runtime.service.versions import SERVER_VERSION
from omnivia_core_runtime.service.workspace_init import (
    WorkspaceInitStatus,
    initialise_workspace,
)
from omnivia_core_runtime.workspace.blob_publication import BlobPublicationRefused


def _workspace(tmp_path: Path) -> tuple[Path, Path]:
    workspace = tmp_path / "workspace"
    installation = tmp_path / "installation-state"
    result = initialise_workspace(
        workspace_root=workspace,
        installation_root=installation,
        core_version=SERVER_VERSION,
    )
    assert result.status in {
        WorkspaceInitStatus.INITIALISED,
        WorkspaceInitStatus.ALREADY_INITIALISED,
    }
    return workspace, installation


def _capture(
    workspace: Path, installation: Path, source: Path, source_id: str = "source-1"
):
    return capture_local_source(
        workspace_root=workspace,
        installation_root=installation,
        source_path=source,
        source_id=source_id,
        media_type="text/plain",
        core_version=SERVER_VERSION,
    )


def test_capture_publishes_blob_and_fenced_evidence_idempotently(
    tmp_path: Path,
) -> None:
    workspace, installation = _workspace(tmp_path)
    source = tmp_path / "source.txt"
    content = b"standalone evidence\n"
    source.write_bytes(content)

    first = _capture(workspace, installation, source)
    assert first.status == "captured"
    assert first.evidence_id is not None
    assert first.content_digest is not None
    assert first.content_length_bytes == len(content)
    rendered = json.dumps(first.to_dict(), sort_keys=True)
    assert str(source) not in rendered
    assert content.decode().strip() not in rendered

    blob = workspace / "blobs" / "sha256" / first.content_digest.removeprefix("sha256:")
    assert blob.read_bytes() == content

    second = _capture(workspace, installation, source)
    assert second.status == "already_captured"
    assert second.evidence_id == first.evidence_id

    connection = sqlite3.connect(workspace / "workspace.sqlite")
    try:
        artifact = connection.execute(
            "SELECT source_kind, source_native_id, source_locator, "
            "source_retrieved_at_us, blob_content_digest, media_type, "
            "original_metadata_json, parser_status, ingestion_status "
            "FROM omnivia_evidence_artifacts"
        ).fetchone()
        assert artifact is not None
        assert artifact[:6] == (
            "document",
            "source-1",
            None,
            None,
            first.content_digest,
            "text/plain",
        )
        assert str(source) not in str(artifact[6])
        assert artifact[7:] == ("not_parsed", "ingested")
        assert connection.execute(
            "SELECT COUNT(*) FROM omnivia_blob_objects"
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT COUNT(*) FROM omnivia_staged_sources"
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT action, actor_kind, source_native_id "
            "FROM omnivia_evidence_provenance_events"
        ).fetchone() == ("captured", "service", "source-1")
    finally:
        connection.close()


def test_capture_refuses_source_identity_rebinding(tmp_path: Path) -> None:
    workspace, installation = _workspace(tmp_path)
    source = tmp_path / "source.txt"
    source.write_text("first", encoding="utf-8")
    accepted = _capture(workspace, installation, source)
    source.write_text("different", encoding="utf-8")

    with pytest.raises(
        SourceCaptureRefused, match="source identity already names different evidence"
    ):
        _capture(workspace, installation, source)

    connection = sqlite3.connect(workspace / "workspace.sqlite")
    try:
        assert connection.execute(
            "SELECT evidence_id, blob_content_digest FROM omnivia_evidence_artifacts"
        ).fetchone() == (accepted.evidence_id, accepted.content_digest)
        assert connection.execute(
            "SELECT COUNT(*) FROM omnivia_evidence_artifacts"
        ).fetchone() == (1,)
    finally:
        connection.close()


def test_capture_refuses_nonregular_and_oversized_sources(tmp_path: Path) -> None:
    workspace, installation = _workspace(tmp_path)
    directory = tmp_path / "directory"
    directory.mkdir()
    with pytest.raises(SourceCaptureRefused, match="one regular file"):
        _capture(workspace, installation, directory)

    oversized = tmp_path / "oversized.bin"
    with oversized.open("wb") as handle:
        handle.truncate(MAX_SOURCE_BYTES + 1)
    with pytest.raises(SourceCaptureRefused, match="capture size limit"):
        _capture(workspace, installation, oversized)


def test_legacy_publish_blob_facade_raises_source_capture_refused(
    tmp_path: Path,
) -> None:
    """The legacy facade wraps the provider-neutral primitive, it does not subclass it:
    a `BlobPublicationRefused` reaching this module's `publish_blob` must still be
    catchable as `SourceCaptureRefused`, so it is translated rather than inherited."""
    blobs_root = tmp_path / "blobs"
    blobs_root.mkdir()
    content = b"legacy facade bytes\n"
    mismatched_digest = "sha256:" + "a" * 64

    with pytest.raises(SourceCaptureRefused, match="does not match"):
        publish_blob(blobs_root, mismatched_digest, content)

    assert not issubclass(SourceCaptureRefused, BlobPublicationRefused)


def test_capture_refuses_while_live_service_owns_workspace(tmp_path: Path) -> None:
    workspace, installation = _workspace(tmp_path)
    source = tmp_path / "source.txt"
    source.write_text("held", encoding="utf-8")
    owner = ServiceRunner(
        ServiceSettings(
            workspace_root=workspace,
            installation_root=installation,
            core_version=SERVER_VERSION,
            endpoint=None,
        )
    )
    report = owner.start()
    assert report.ready
    try:
        with pytest.raises(
            SourceCaptureRefused,
            match="another service holds the lifetime storage lock",
        ):
            _capture(workspace, installation, source)
    finally:
        owner.stop()


# --- read_checkout_file -----------------------------------------------------------------

_DATA = b"def f():\n    return 1\n"


def _digest(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def _checkout(tmp_path: Path) -> Path:
    root = tmp_path / "checkout"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "a.py").write_bytes(_DATA)
    return root


def _read(root: Path, path: str = "pkg/a.py", digest: str | None = None):
    return read_checkout_file(
        checkout_root=root,
        relative_path=path,
        expected_digest=digest or _digest(_DATA),
    )


def _refused(root: Path, path: str = "pkg/a.py", digest: str | None = None) -> str:
    with pytest.raises(SourceCaptureRefused) as raised:
        _read(root, path, digest)
    message = str(raised.value)
    assert str(root) not in message and (not path or path not in message)
    assert raised.value.__cause__ is None
    return message


def test_read_checkout_file_returns_exact_bytes_and_digest(tmp_path: Path) -> None:
    result = _read(_checkout(tmp_path))
    assert result.content == _DATA
    assert result.digest == _digest(_DATA)


def test_read_checkout_file_keeps_exact_case_and_unicode(tmp_path: Path) -> None:
    root = _checkout(tmp_path)
    (root / "pkg" / "Ünï.PY").write_bytes(b"x")
    assert _read(root, "pkg/Ünï.PY", _digest(b"x")).content == b"x"
    if not (root / "pkg" / "A.py").exists():  # case-sensitive filesystem only
        assert _refused(root, "pkg/A.py")


@pytest.mark.parametrize(
    "path",
    [
        "../x",
        "pkg/../pkg/a.py",
        "/etc/passwd",
        "pkg//a.py",
        "./pkg/a.py",
        "pkg\\a.py",
        "C:/x",
        "",
        "pkg/a\x00.py",
    ],
)
def test_read_checkout_file_refuses_unportable_paths(tmp_path: Path, path: str) -> None:
    assert _refused(_checkout(tmp_path), path) == (
        "repository path is outside the accepted portable domain"
    )


@pytest.mark.parametrize(
    "digest", ["", "sha256:abc", "sha256:" + "A" * 64, "md5:" + "a" * 64]
)
def test_read_checkout_file_refuses_malformed_digest(
    tmp_path: Path, digest: str
) -> None:
    root = _checkout(tmp_path)
    with pytest.raises(SourceCaptureRefused):
        read_checkout_file(
            checkout_root=root, relative_path="pkg/a.py", expected_digest=digest
        )


def test_read_checkout_file_refuses_wrong_digest(tmp_path: Path) -> None:
    assert _refused(_checkout(tmp_path), digest=_digest(b"other")) == (
        "source content does not match the expected digest"
    )


def test_read_checkout_file_refuses_missing(tmp_path: Path) -> None:
    root = _checkout(tmp_path)
    _refused(root, "pkg/missing.py")
    _refused(root, "nodir/a.py")
    _refused(tmp_path / "no-such-root")


def test_read_checkout_file_refuses_directory_and_fifo(tmp_path: Path) -> None:
    root = _checkout(tmp_path)
    assert _refused(root, "pkg") == "source must be one regular file"
    if hasattr(os, "mkfifo"):
        os.mkfifo(root / "pkg" / "pipe")
        assert _refused(root, "pkg/pipe") == "source must be one regular file"


def test_read_checkout_file_refuses_oversized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _checkout(tmp_path)
    monkeypatch.setattr(source_capture, "MAX_SOURCE_BYTES", len(_DATA) - 1)
    assert _refused(root) == "source exceeds the capture size limit"


def test_read_checkout_file_accepts_exactly_the_size_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _checkout(tmp_path)
    monkeypatch.setattr(source_capture, "MAX_SOURCE_BYTES", len(_DATA))
    assert _read(root).content == _DATA


def test_read_checkout_file_refuses_growth_past_limit_during_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _checkout(tmp_path)
    monkeypatch.setattr(source_capture, "MAX_SOURCE_BYTES", len(_DATA))
    real_read = os.read

    done: list[bool] = []

    def growing(fd: int, n: int) -> bytes:
        data = real_read(fd, n)
        if not done:
            done.append(True)
            with (root / "pkg" / "a.py").open("ab") as handle:
                handle.write(b"#")
        return data

    monkeypatch.setattr(source_capture.os, "read", growing)
    assert _refused(root) == "source exceeds the capture size limit"


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="no symlinks")
def test_read_checkout_file_refuses_symlinks(tmp_path: Path) -> None:
    root = _checkout(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "a.py").write_bytes(_DATA)
    try:
        (root / "leaf.py").symlink_to(root / "pkg" / "a.py")
        (root / "linkdir").symlink_to(outside, target_is_directory=True)
        link_root = tmp_path / "link-root"
        link_root.symlink_to(root, target_is_directory=True)
    except OSError:
        pytest.skip("cannot create symlinks")
    assert (
        _refused(root, "leaf.py")
        == "source cannot be opened as a file inside the checkout"
    )
    assert _refused(root, "linkdir/a.py") == (
        "source cannot be opened as a file inside the checkout"
    )
    assert _refused(link_root) == "checkout root is not an accessible directory"


def test_read_checkout_file_refuses_path_rebound_mid_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _checkout(tmp_path)
    real_read = os.read
    done: list[bool] = []

    def rebinding(fd: int, n: int) -> bytes:
        data = real_read(fd, n)
        if not done:
            done.append(True)
            (root / "pkg").rename(root / "old")
            (root / "pkg").mkdir()
            (root / "pkg" / "a.py").write_bytes(_DATA)
        return data

    monkeypatch.setattr(source_capture.os, "read", rebinding)
    assert _refused(root) == "source changed while it was being read"


def test_read_checkout_file_refuses_file_modified_mid_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _checkout(tmp_path)
    real_read = os.read
    done: list[bool] = []

    def modifying(fd: int, n: int) -> bytes:
        data = real_read(fd, n)
        if not done:
            done.append(True)
            with (root / "pkg" / "a.py").open("ab") as handle:
                handle.write(b"#")
        return data

    monkeypatch.setattr(source_capture.os, "read", modifying)
    assert _refused(root) == "source changed while it was being read"


def test_read_checkout_file_refuses_unsupported_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(source_capture, "_NO_FOLLOW_WALK", False)
    assert _refused(_checkout(tmp_path)) == (
        "this host cannot read a checkout without following links"
    )
