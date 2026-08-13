"""Qualification of the service-owned local evidence capture path."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from omnivia_core_runtime.service.runner import ServiceRunner, ServiceSettings
from omnivia_core_runtime.service.source_capture import (
    MAX_SOURCE_BYTES,
    SourceCaptureRefused,
    capture_local_source,
)
from omnivia_core_runtime.service.versions import SERVER_VERSION
from omnivia_core_runtime.service.workspace_init import (
    WorkspaceInitStatus,
    initialise_workspace,
)


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
            SourceCaptureRefused, match="another service holds the lifetime storage lock"
        ):
            _capture(workspace, installation, source)
    finally:
        owner.stop()
