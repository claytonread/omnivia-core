"""Independent completeness/safety suite for the local filesystem SourceConnectorSpi.

Deliberately does not call any adapter-private helper: expected relative paths
and identities are derived here with plain stdlib enumeration and hashing, so
this suite catches the adapter drifting from its own claims rather than
re-confirming whatever the adapter happens to compute.
"""

from __future__ import annotations

import base64
import hashlib
import os
import sys
from pathlib import Path

import pytest

from omnivia_core.connector.filesystem import FilesystemSourceConnector
from omnivia_core.connector.host import validate_batch
from omnivia_core.connector.spi import (
    SPI_OPERATIONS,
    Batch,
    ConnectorRefused,
    CredentialHandle,
    CursorBinding,
    CursorState,
    Deadline,
    DeletionSignal,
    IdentityStability,
    Observation,
    PollContext,
    PollLimits,
    SourceConnectorSpi,
)
from omnivia_core.contracts.v1.generated import (
    ERROR_CODE_CANCELLED,
    ERROR_CODE_INVALID_REQUEST,
    ERROR_CODE_SIZE_LIMIT_EXCEEDED,
)

NOW_US = 1785000000000000


def _resolver_never_called(handle):  # pragma: no cover - only invoked on failure
    raise AssertionError("resolve_credential must never be called by a filesystem poll")


def make_ctx(**overrides: object) -> PollContext:
    values: dict[str, object] = {
        "workspace_id": "workspace-alpha",
        "run_id": "run-0001",
        "attempt_ordinal": 1,
        "granted_scopes": ("source.fs",),
        "credential_handle": CredentialHandle(reference="handle-0001"),
        "resolve_credential": _resolver_never_called,
        "limits": PollLimits(
            max_batch_items=4096,
            max_item_metadata_bytes=1,
            max_run_bytes=67_108_864,
            poll_deadline_ms=30_000,
        ),
        "deadline": Deadline(expires_at_us=NOW_US + 1_000_000),
        "cancellation": lambda: False,
    }
    values.update(overrides)
    return PollContext(**values)  # type: ignore[arg-type]


def native_id(relative_path: str) -> str:
    return "fs-" + hashlib.sha256(relative_path.encode("utf-8")).hexdigest()


def expected_paths(root: Path) -> list[str]:
    """Independently enumerate accepted-extension regular files under root."""
    accepted = {".txt", ".md", ".markdown", ".json"}
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [
            d for d in dirnames if not (Path(dirpath) / d).is_symlink()
        ]
        for name in filenames:
            p = Path(dirpath) / name
            if p.is_symlink() or not p.is_file():
                continue
            if p.suffix.lower() not in accepted:
                continue
            out.append(p.relative_to(root).as_posix())
    return sorted(out)


def run_poll(connector: FilesystemSourceConnector, cursor: CursorState | None):
    return next(iter(connector.poll(make_ctx(), cursor)))


def admit(batch: Batch, descriptor) -> Batch:
    """Push a batch through host-side validation, as the real coordinator would."""
    binding = CursorBinding(workspace_id="workspace-alpha", connector_id=descriptor.connector_id)
    record = _make_cursor_record(binding, batch.successor_cursor)
    verdict = validate_batch(
        batch,
        make_ctx(),
        record,
        descriptor=descriptor,
        now_us=NOW_US,
    )
    return verdict


def _make_cursor_record(binding, state):
    from omnivia_core.connector.spi import CursorRecord

    return CursorRecord(binding=binding, state=state)


# --- 1. describe / four-operation protocol / export -------------------------


def test_describe_and_four_operation_protocol(tmp_path: Path) -> None:
    connector = FilesystemSourceConnector(root=tmp_path)
    assert isinstance(connector, SourceConnectorSpi)
    assert SPI_OPERATIONS == ("describe", "migrate_cursor", "probe", "poll")
    for name in SPI_OPERATIONS:
        assert callable(getattr(connector, name))
    for name in ("write", "delete", "schedule", "open_workspace", "get_credential"):
        assert not hasattr(connector, name)

    descriptor = connector.describe()
    assert descriptor.connector_id == "local.filesystem"
    assert descriptor.identity_stability is IdentityStability.LOCATOR_DERIVED
    assert descriptor.supported_state_versions == (1,)


def test_filesystem_connector_is_deliberately_exported() -> None:
    from omnivia_core.connector import filesystem as module

    assert "FilesystemSourceConnector" in module.__all__


# --- 2. initial scan determinism -------------------------------------------


def test_initial_scan_order_media_types_content_checksum(tmp_path: Path) -> None:
    (tmp_path / "b").mkdir()
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "one.txt").write_text("hello", encoding="utf-8")
    (tmp_path / "b" / "two.md").write_text("# title", encoding="utf-8")
    (tmp_path / "root.json").write_text('{"x": 1}', encoding="utf-8")
    (tmp_path / "note.markdown").write_text("notes", encoding="utf-8")

    connector = FilesystemSourceConnector(root=tmp_path)
    batch = run_poll(connector, None)

    want = expected_paths(tmp_path)
    got = [obs.source_locator for obs in batch.observations]
    assert got == want

    media = {obs.source_locator: obs.media_type for obs in batch.observations}
    assert media["a/one.txt"] == "text/plain"
    assert media["b/two.md"] == "text/markdown"
    assert media["root.json"] == "application/json"
    assert media["note.markdown"] == "text/markdown"

    for obs in batch.observations:
        assert obs.source_native_id == native_id(obs.source_locator)
        assert obs.metadata_bytes == 0
        assert obs.deletion_signal is DeletionSignal.NONE
        raw = (tmp_path / obs.source_locator).read_bytes()
        assert obs.content == raw
        assert obs.content_checksum == "sha256:" + hashlib.sha256(raw).hexdigest()
        obs.content.decode("utf-8")  # never raises: admitted content is valid UTF-8


# --- 3. deterministic replay/resume and restart with only the cursor -------


def test_replay_with_same_cursor_is_deterministic(tmp_path: Path) -> None:
    (tmp_path / "one.txt").write_text("hello", encoding="utf-8")
    connector = FilesystemSourceConnector(root=tmp_path)
    first = run_poll(connector, None)
    second = run_poll(connector, None)
    assert first.observations == second.observations
    assert first.successor_cursor.payload == second.successor_cursor.payload


def test_resume_with_only_the_cursor_reconstructs_full_state(tmp_path: Path) -> None:
    (tmp_path / "one.txt").write_text("hello", encoding="utf-8")
    connector = FilesystemSourceConnector(root=tmp_path)
    first = run_poll(connector, None)

    # A brand-new connector instance, seeded with nothing but the cursor.
    restarted = FilesystemSourceConnector(root=tmp_path)
    (tmp_path / "two.txt").write_text("world", encoding="utf-8")
    second = run_poll(restarted, first.successor_cursor)

    locators = {obs.source_locator for obs in second.observations}
    assert locators == {"one.txt", "two.txt"}  # full rescan every poll
    assert all(obs.deletion_signal is DeletionSignal.NONE for obs in second.observations)
    assert second.successor_cursor.witness_seq == first.successor_cursor.witness_seq + 1


# --- 4. deletion, empty tree, rename = delete+create ------------------------


def test_empty_tree_produces_no_observations(tmp_path: Path) -> None:
    connector = FilesystemSourceConnector(root=tmp_path)
    batch = run_poll(connector, None)
    assert batch.observations == ()
    assert batch.item_failures == ()


def test_explicit_deletion_is_reported(tmp_path: Path) -> None:
    target = tmp_path / "gone.txt"
    target.write_text("bye", encoding="utf-8")
    connector = FilesystemSourceConnector(root=tmp_path)
    first = run_poll(connector, None)
    target.unlink()
    second = run_poll(connector, first.successor_cursor)

    assert len(second.observations) == 1
    deletion = second.observations[0]
    assert deletion.source_locator == "gone.txt"
    assert deletion.source_native_id == native_id("gone.txt")
    assert deletion.deletion_signal is DeletionSignal.EXPLICIT_DELETE
    assert deletion.content is None
    assert deletion.content_checksum is None


def test_rename_is_locator_derived_delete_and_create(tmp_path: Path) -> None:
    old = tmp_path / "old.txt"
    old.write_text("same bytes", encoding="utf-8")
    connector = FilesystemSourceConnector(root=tmp_path)
    first = run_poll(connector, None)

    old.rename(tmp_path / "new.txt")
    second = run_poll(connector, first.successor_cursor)

    by_locator = {obs.source_locator: obs for obs in second.observations}
    assert by_locator["old.txt"].deletion_signal is DeletionSignal.EXPLICIT_DELETE
    assert by_locator["new.txt"].deletion_signal is DeletionSignal.NONE
    assert by_locator["old.txt"].source_native_id != by_locator["new.txt"].source_native_id


# --- 5. symlinks never followed ---------------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks require elevated rights on Windows")
def test_symlink_root_is_refused(tmp_path: Path) -> None:
    real_root = tmp_path / "real"
    real_root.mkdir()
    (real_root / "a.txt").write_text("x", encoding="utf-8")
    link_root = tmp_path / "link"
    link_root.symlink_to(real_root, target_is_directory=True)

    connector = FilesystemSourceConnector(root=link_root)
    health = connector.probe(make_ctx())
    assert health.state.name == "UNAVAILABLE"
    with pytest.raises(ConnectorRefused) as excinfo:
        run_poll(connector, None)
    assert excinfo.value.error == ERROR_CODE_INVALID_REQUEST


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks require elevated rights on Windows")
def test_root_replaced_by_symlink_after_construction_is_refused(tmp_path: Path) -> None:
    configured = tmp_path / "configured"
    configured.mkdir()
    connector = FilesystemSourceConnector(root=configured)
    configured.rename(tmp_path / "former-root")
    configured.symlink_to(tmp_path / "former-root", target_is_directory=True)

    assert connector.probe(make_ctx()).state.name == "UNAVAILABLE"
    with pytest.raises(ConnectorRefused) as excinfo:
        run_poll(connector, None)
    assert excinfo.value.error == ERROR_CODE_INVALID_REQUEST


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks require elevated rights on Windows")
def test_symlinked_file_is_reported_as_a_failure_never_read(tmp_path: Path) -> None:
    real = tmp_path / "real.txt"
    real.write_text("secret", encoding="utf-8")
    link = tmp_path / "link.txt"
    link.symlink_to(real)

    connector = FilesystemSourceConnector(root=tmp_path)
    batch = run_poll(connector, None)

    locators = {obs.source_locator for obs in batch.observations}
    assert "link.txt" not in locators
    assert "real.txt" in locators
    failed = {f.source_native_id for f in batch.item_failures}
    assert native_id("link.txt") in failed


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks require elevated rights on Windows")
def test_symlinked_directory_is_never_descended_into(tmp_path: Path) -> None:
    real_dir = tmp_path / "real_dir"
    real_dir.mkdir()
    (real_dir / "inside.txt").write_text("x", encoding="utf-8")
    link_dir = tmp_path / "link_dir"
    link_dir.symlink_to(real_dir, target_is_directory=True)

    connector = FilesystemSourceConnector(root=tmp_path)
    batch = run_poll(connector, None)

    locators = {obs.source_locator for obs in batch.observations}
    assert not any(loc.startswith("link_dir/") for loc in locators)
    failed = {f.source_native_id for f in batch.item_failures}
    assert native_id("link_dir") in failed


# --- 6. rejects / limits -----------------------------------------------------


def test_unsupported_extension_is_an_item_failure(tmp_path: Path) -> None:
    (tmp_path / "image.png").write_bytes(b"\x89PNG\r\n")
    connector = FilesystemSourceConnector(root=tmp_path)
    batch = run_poll(connector, None)
    assert batch.observations == ()
    assert len(batch.item_failures) == 1
    assert batch.item_failures[0].source_native_id == native_id("image.png")


def test_binary_content_with_nul_byte_is_an_item_failure(tmp_path: Path) -> None:
    (tmp_path / "bad.txt").write_bytes(b"hello\x00world")
    connector = FilesystemSourceConnector(root=tmp_path)
    batch = run_poll(connector, None)
    assert batch.observations == ()
    assert len(batch.item_failures) == 1
    assert batch.item_failures[0].error == "filesystem_item_nul_byte"


def test_invalid_utf8_content_is_an_item_failure(tmp_path: Path) -> None:
    (tmp_path / "bad.txt").write_bytes(b"\xff\xfe\xfd")
    connector = FilesystemSourceConnector(root=tmp_path)
    batch = run_poll(connector, None)
    assert batch.observations == ()
    assert batch.item_failures[0].error == "filesystem_item_invalid_utf8"


def test_oversized_item_is_an_item_failure(tmp_path: Path) -> None:
    (tmp_path / "big.txt").write_bytes(b"a" * 100)
    connector = FilesystemSourceConnector(root=tmp_path, max_item_bytes=10)
    batch = run_poll(connector, None)
    assert batch.observations == ()
    assert batch.item_failures[0].error == ERROR_CODE_SIZE_LIMIT_EXCEEDED


def test_overlong_path_is_an_item_failure(tmp_path: Path) -> None:
    long_dir = tmp_path / ("d" * 200)
    long_dir.mkdir()
    (long_dir / (("f" * 200) + ".txt")).write_text("x", encoding="utf-8")
    connector = FilesystemSourceConnector(root=tmp_path, max_path_length=50)
    batch = run_poll(connector, None)
    assert batch.observations == ()
    assert len(batch.item_failures) == 1
    assert batch.item_failures[0].error == "filesystem_item_path_too_long"


def test_file_count_ceiling_refuses_the_poll(tmp_path: Path) -> None:
    for i in range(5):
        (tmp_path / f"{i}.txt").write_text("x", encoding="utf-8")
    connector = FilesystemSourceConnector(root=tmp_path, max_files=3)
    with pytest.raises(ConnectorRefused) as excinfo:
        run_poll(connector, None)
    assert excinfo.value.error == ERROR_CODE_SIZE_LIMIT_EXCEEDED


def test_directory_enumeration_error_refuses_instead_of_inventing_deletions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    connector = FilesystemSourceConnector(root=tmp_path)

    def broken_walk(root, *, followlinks, onerror):
        del root, followlinks
        onerror(OSError("unreadable"))
        return ()

    monkeypatch.setattr(os, "walk", broken_walk)
    with pytest.raises(ConnectorRefused) as excinfo:
        run_poll(connector, None)
    assert excinfo.value.error == "filesystem_item_unreadable"


def test_tree_bytes_ceiling_refuses_the_poll(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_bytes(b"a" * 100)
    (tmp_path / "b.txt").write_bytes(b"b" * 100)
    connector = FilesystemSourceConnector(root=tmp_path, max_tree_bytes=150)
    with pytest.raises(ConnectorRefused) as excinfo:
        run_poll(connector, None)
    assert excinfo.value.error == ERROR_CODE_SIZE_LIMIT_EXCEEDED


def test_cursor_snapshot_ceiling_refuses_the_poll(tmp_path: Path) -> None:
    # Enough distinct paths that the encoded snapshot exceeds the 4096-byte
    # cursor payload ceiling.
    for i in range(400):
        (tmp_path / f"file-{i:04d}-with-a-longer-name.txt").write_text(
            "x", encoding="utf-8"
        )
    connector = FilesystemSourceConnector(root=tmp_path, max_files=10_000)
    with pytest.raises(ConnectorRefused) as excinfo:
        run_poll(connector, None)
    assert excinfo.value.error == ERROR_CODE_SIZE_LIMIT_EXCEEDED


# --- 7. malformed/unsupported cursor -----------------------------------------


def test_unsupported_state_version_refuses_poll(tmp_path: Path) -> None:
    connector = FilesystemSourceConnector(root=tmp_path)
    bad_cursor = CursorState(state_version=2, payload=b"", witness_seq=0)
    with pytest.raises(ConnectorRefused):
        run_poll(connector, bad_cursor)


def test_malformed_cursor_payload_refuses_poll(tmp_path: Path) -> None:
    connector = FilesystemSourceConnector(root=tmp_path)
    # The cursor envelope is valid base64url, but its decoded snapshot is not
    # UTF-8, so the adapter must refuse it rather than guess or resynchronise.
    non_utf8 = base64.urlsafe_b64encode(b"\xff\xfe").rstrip(b"=")
    truly_bad = CursorState(state_version=1, payload=non_utf8, witness_seq=0)
    with pytest.raises(ConnectorRefused):
        run_poll(connector, truly_bad)


def test_migrate_cursor_is_refused_unmigratable(tmp_path: Path) -> None:
    connector = FilesystemSourceConnector(root=tmp_path)
    cursor = CursorState(state_version=1, payload=b"", witness_seq=0)
    with pytest.raises(ConnectorRefused):
        connector.migrate_cursor(cursor)


# --- 8. cancellation and credential resolver never called -------------------


def test_cancellation_before_scan_raises_and_never_scans(tmp_path: Path) -> None:
    (tmp_path / "one.txt").write_text("x", encoding="utf-8")
    connector = FilesystemSourceConnector(root=tmp_path)
    with pytest.raises(ConnectorRefused) as excinfo:
        next(iter(connector.poll(make_ctx(cancellation=lambda: True), None)))
    assert excinfo.value.error == ERROR_CODE_CANCELLED


def test_credential_resolver_is_never_invoked(tmp_path: Path) -> None:
    (tmp_path / "one.txt").write_text("x", encoding="utf-8")
    connector = FilesystemSourceConnector(root=tmp_path)
    # make_ctx wires resolve_credential to a function that raises if called;
    # a successful poll here is itself the assertion.
    batch = next(iter(connector.poll(make_ctx(), None)))
    assert len(batch.observations) == 1


# --- 9. host rejects missing/mismatched checksum and content on deletion ---


def _descriptor(connector: FilesystemSourceConnector):
    return connector.describe()


def test_host_rejects_content_with_missing_checksum(tmp_path: Path) -> None:
    connector = FilesystemSourceConnector(root=tmp_path)
    descriptor = _descriptor(connector)
    bad = Observation(
        source_native_id=native_id("a.txt"),
        source_locator="a.txt",
        observed_at_us=0,
        metadata_bytes=0,
        content=b"hello",
        content_checksum=None,
        media_type="text/plain",
    )
    successor = CursorState(state_version=1, payload=b"", witness_seq=0)
    batch = Batch(observations=(bad,), successor_cursor=successor)
    with pytest.raises(ConnectorRefused) as excinfo:
        admit(batch, descriptor)
    assert excinfo.value.error == ERROR_CODE_INVALID_REQUEST


def test_host_rejects_content_with_mismatched_checksum(tmp_path: Path) -> None:
    connector = FilesystemSourceConnector(root=tmp_path)
    descriptor = _descriptor(connector)
    bad = Observation(
        source_native_id=native_id("a.txt"),
        source_locator="a.txt",
        observed_at_us=0,
        metadata_bytes=0,
        content=b"hello",
        content_checksum="sha256:" + ("0" * 64),
        media_type="text/plain",
    )
    successor = CursorState(state_version=1, payload=b"", witness_seq=0)
    batch = Batch(observations=(bad,), successor_cursor=successor)
    with pytest.raises(ConnectorRefused) as excinfo:
        admit(batch, descriptor)
    assert excinfo.value.error == ERROR_CODE_INVALID_REQUEST


def test_observation_construction_rejects_content_on_deletion(tmp_path: Path) -> None:
    from omnivia_core.connector.models import ConnectorContractError

    with pytest.raises(ConnectorContractError):
        Observation(
            source_native_id=native_id("a.txt"),
            source_locator="a.txt",
            observed_at_us=0,
            metadata_bytes=0,
            content=b"hello",
            content_checksum="sha256:" + hashlib.sha256(b"hello").hexdigest(),
            deletion_signal=DeletionSignal.EXPLICIT_DELETE,
        )


# --- 10. failed/unresolved files never falsely create or delete -------------


def test_unsupported_new_file_never_becomes_a_later_deletion(tmp_path: Path) -> None:
    bad = tmp_path / "image.png"
    bad.write_bytes(b"\x89PNG")
    connector = FilesystemSourceConnector(root=tmp_path)
    first = run_poll(connector, None)
    assert first.observations == ()

    second = run_poll(connector, first.successor_cursor)
    # image.png was never admitted, so its later continued presence (or even
    # removal) must not surface as a deletion observation.
    assert second.observations == ()


@pytest.mark.skipif(sys.platform == "win32", reason="chmod semantics differ on Windows")
def test_previously_admitted_but_temporarily_unreadable_file_is_not_falsely_deleted(
    tmp_path: Path,
) -> None:
    target = tmp_path / "flaky.txt"
    target.write_text("hello", encoding="utf-8")
    connector = FilesystemSourceConnector(root=tmp_path)
    first = run_poll(connector, None)
    assert [o.source_locator for o in first.observations] == ["flaky.txt"]

    original_mode = target.stat().st_mode
    target.chmod(0o000)
    try:
        second = run_poll(connector, first.successor_cursor)
    finally:
        target.chmod(original_mode)

    # Unreadable this poll: still present on disk, so it is never diffed away
    # as a deletion of a previously admitted file.
    assert all(o.source_locator != "flaky.txt" for o in second.observations)

    third = run_poll(connector, second.successor_cursor)
    assert [o.source_locator for o in third.observations] == ["flaky.txt"]
