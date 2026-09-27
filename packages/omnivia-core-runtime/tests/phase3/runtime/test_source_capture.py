"""Qualification of the service-owned local evidence capture path."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import subprocess
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

needs_walk = pytest.mark.skipif(
    not source_capture._NO_FOLLOW_WALK, reason="host lacks no-follow checkout walk"
)

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


@needs_walk
def test_read_checkout_file_returns_exact_bytes_and_digest(tmp_path: Path) -> None:
    result = _read(_checkout(tmp_path))
    assert result.content == _DATA
    assert result.digest == _digest(_DATA)


@needs_walk
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


@needs_walk
def test_read_checkout_file_refuses_wrong_digest(tmp_path: Path) -> None:
    assert _refused(_checkout(tmp_path), digest=_digest(b"other")) == (
        "source content does not match the expected digest"
    )


@needs_walk
def test_read_checkout_file_refuses_missing(tmp_path: Path) -> None:
    root = _checkout(tmp_path)
    _refused(root, "pkg/missing.py")
    _refused(root, "nodir/a.py")
    _refused(tmp_path / "no-such-root")


@needs_walk
def test_read_checkout_file_refuses_directory_and_fifo(tmp_path: Path) -> None:
    root = _checkout(tmp_path)
    assert _refused(root, "pkg") == "source must be one regular file"
    if hasattr(os, "mkfifo"):
        os.mkfifo(root / "pkg" / "pipe")
        assert _refused(root, "pkg/pipe") == "source must be one regular file"


@needs_walk
def test_read_checkout_file_refuses_oversized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _checkout(tmp_path)
    monkeypatch.setattr(source_capture, "MAX_SOURCE_BYTES", len(_DATA) - 1)
    assert _refused(root) == "source exceeds the capture size limit"


@needs_walk
def test_read_checkout_file_accepts_exactly_the_size_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _checkout(tmp_path)
    monkeypatch.setattr(source_capture, "MAX_SOURCE_BYTES", len(_DATA))
    assert _read(root).content == _DATA


@needs_walk
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
@needs_walk
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


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="no symlinks")
@needs_walk
def test_read_checkout_file_refuses_symlinked_ancestor_of_checkout_root(
    tmp_path: Path,
) -> None:
    """An ancestor symlink, not just the checkout root itself, must be refused: the
    root's own descriptor is opened by walking every one of its components, not by
    opening the absolute path in one call, which would follow every ancestor but the
    last."""
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    root = _checkout(real_parent)
    link_ancestor = tmp_path / "link-ancestor"
    try:
        link_ancestor.symlink_to(real_parent, target_is_directory=True)
    except OSError:
        pytest.skip("cannot create symlinks")
    linked_root = link_ancestor / root.name
    assert _refused(linked_root) == "checkout root is not an accessible directory"


@needs_walk
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


@needs_walk
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


# --- is_trusted_local_checkout_root -------------------------------------------------


def test_trusted_checkout_root_accepts_a_real_directory(tmp_path: Path) -> None:
    root = tmp_path / "real"
    root.mkdir()
    assert source_capture.is_trusted_local_checkout_root(os.fspath(root))


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="no symlinks")
def test_trusted_checkout_root_refuses_a_symlinked_leaf(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("cannot create symlinks")
    assert not source_capture.is_trusted_local_checkout_root(os.fspath(link))


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="no symlinks")
def test_trusted_checkout_root_refuses_a_symlinked_ancestor(tmp_path: Path) -> None:
    """A symlink anywhere above the named leaf is refused, not only at the leaf
    itself: an earlier version checked only the final path's own `lstat`, which a
    symlinked parent directory passed even though later path-based operations
    would follow it.
    """
    real_parent = tmp_path / "real-parent"
    (real_parent / "checkout").mkdir(parents=True)
    linked_parent = tmp_path / "linked-parent"
    try:
        linked_parent.symlink_to(real_parent, target_is_directory=True)
    except OSError:
        pytest.skip("cannot create symlinks")
    checkout_root = linked_parent / "checkout"
    assert not source_capture.is_trusted_local_checkout_root(os.fspath(checkout_root))


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="no symlinks")
def test_trusted_checkout_root_refuses_a_symlinked_ancestor_without_descriptor_support(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The `lstat`-walk fallback used on a host without descriptor support still
    refuses an ancestor symlink, rather than only checking the leaf.
    """
    real_parent = tmp_path / "real-parent-2"
    (real_parent / "checkout").mkdir(parents=True)
    linked_parent = tmp_path / "linked-parent-2"
    try:
        linked_parent.symlink_to(real_parent, target_is_directory=True)
    except OSError:
        pytest.skip("cannot create symlinks")
    checkout_root = linked_parent / "checkout"
    monkeypatch.setattr(source_capture, "_NO_FOLLOW_WALK", False)
    assert not source_capture.is_trusted_local_checkout_root(os.fspath(checkout_root))


def test_trusted_checkout_root_refuses_missing_and_relative(tmp_path: Path) -> None:
    (tmp_path / "checkout").mkdir()
    assert not source_capture.is_trusted_local_checkout_root(
        os.fspath(tmp_path) + "/./checkout"
    )
    assert not source_capture.is_trusted_local_checkout_root("/" + "a" * 513)
    assert not source_capture.is_trusted_local_checkout_root(
        os.fspath(tmp_path / "does-not-exist")
    )
    assert not source_capture.is_trusted_local_checkout_root("relative/path")
    assert not source_capture.is_trusted_local_checkout_root(
        os.fspath(tmp_path / ".." / "escape")
    )


# --- capture_working_tree_manifest -------------------------------------------------

_GIT_ENV = {
    **os.environ,
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
}


def _git_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "a.py").write_bytes(_DATA)
    for args in (
        ["init", "-q"],
        ["add", "."],
        ["-c", "commit.gpgsign=false", "commit", "-q", "-m", "init"],
    ):
        subprocess.run(["git", *args], cwd=root, env=_GIT_ENV, check=True)
    return root


def _manifest(root: Path) -> source_capture.WorkingTreeManifest:
    return source_capture.capture_working_tree_manifest(checkout_root=root)


@needs_walk
def test_capture_clean_then_dirty_tracked_file(tmp_path: Path) -> None:
    root = _git_repo(tmp_path)
    clean = _manifest(root)
    assert clean.complete and not clean.omissions
    assert re.fullmatch(r"sha1:[0-9a-f]{40}|sha256:[0-9a-f]{64}", clean.base_commit_id)
    (file,) = clean.files
    assert (file.path, file.mode, file.tracked) == ("pkg/a.py", "100644", True)
    assert file.digest == _digest(_DATA) and file.content == _DATA
    assert clean.to_dict()["snapshot_kind"] == "working_tree"

    (root / "pkg" / "a.py").write_bytes(b"changed\n")
    dirty = _manifest(root)
    assert dirty.complete and dirty.base_commit_id == clean.base_commit_id
    assert dirty.files[0].content == b"changed\n"
    assert dirty.manifest_digest != clean.manifest_digest


@needs_walk
def test_capture_untracked_file_and_digest_determinism(tmp_path: Path) -> None:
    root = _git_repo(tmp_path)
    (root / "New.txt").write_bytes(b"new")
    (root / ".gitignore").write_bytes(b"ignored.txt\n")
    (root / "ignored.txt").write_bytes(b"x")
    first, second = _manifest(root), _manifest(root)
    assert first.manifest_digest == second.manifest_digest
    assert [(f.path, f.tracked) for f in first.files] == [
        (".gitignore", False),
        ("New.txt", False),
        ("pkg/a.py", True),
    ]
    assert first.complete


@needs_walk
@pytest.mark.skipif(not hasattr(os, "symlink"), reason="no symlinks")
def test_capture_symlink_is_omitted_and_incomplete(tmp_path: Path) -> None:
    root = _git_repo(tmp_path)
    os.symlink("pkg/a.py", root / "link")
    manifest = _manifest(root)
    assert not manifest.complete
    assert [f.path for f in manifest.files] == ["pkg/a.py"]
    assert [(o.path, o.reason) for o in manifest.omissions] == [("link", "symlink")]


@needs_walk
def test_capture_missing_tracked_file_is_incomplete(tmp_path: Path) -> None:
    root = _git_repo(tmp_path)
    (root / "pkg" / "a.py").unlink()
    manifest = _manifest(root)
    assert not manifest.complete and not manifest.files
    assert manifest.omissions[0].reason == "missing_or_unsupported"


@needs_walk
def test_capture_moving_file_is_bounded_and_incomplete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _git_repo(tmp_path)
    reads = 0
    real = source_capture._read_checkout

    def moving(
        checkout_root: Path,
        path: str,
        expected: str | None,
        expected_identity: tuple[int, int] | None = None,
    ) -> source_capture.CheckoutFile:
        nonlocal reads
        reads += 1
        (root / path).write_bytes(b"moving %d\n" % reads)
        return real(checkout_root, path, expected, expected_identity)

    monkeypatch.setattr(source_capture, "_read_checkout", moving)
    manifest = _manifest(root)
    assert not manifest.complete
    assert manifest.attempts == source_capture.MAX_CAPTURE_ATTEMPTS
    assert reads == 2 * source_capture.MAX_CAPTURE_ATTEMPTS
    assert {o.reason for o in manifest.omissions} >= {
        "changed_during_capture",
        "unstable_checkout",
    }


@needs_walk
def test_capture_malicious_portable_path_is_omitted(tmp_path: Path) -> None:
    root = _git_repo(tmp_path)
    bad = root / "bad\\name.txt"  # backslash is outside the portable domain
    bad.write_bytes(b"x")
    manifest = _manifest(root)
    assert not manifest.complete
    assert [f.path for f in manifest.files] == ["pkg/a.py"]
    assert [(o.path, o.reason) for o in manifest.omissions] == [
        (None, "path_not_portable")
    ]


def test_capture_refuses_unsupported_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(source_capture, "_NO_FOLLOW_WALK", False)
    with pytest.raises(SourceCaptureRefused):
        _manifest(tmp_path)


@needs_walk
def test_capture_refuses_non_repository(tmp_path: Path) -> None:
    with pytest.raises(SourceCaptureRefused):
        _manifest(_checkout(tmp_path))


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="no symlinks")
@needs_walk
def test_capture_refuses_symlinked_ancestor_of_checkout_root(tmp_path: Path) -> None:
    """The same ancestor-symlink refusal `read_checkout_file` gets must hold for
    working-tree capture: the root descriptor is opened by the same no-follow walk
    before Git or the reader ever sees it."""
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    root = _git_repo(real_parent)
    link_ancestor = tmp_path / "link-ancestor"
    try:
        link_ancestor.symlink_to(real_parent, target_is_directory=True)
    except OSError:
        pytest.skip("cannot create symlinks")
    linked_root = link_ancestor / root.name
    with pytest.raises(SourceCaptureRefused):
        _manifest(linked_root)


@needs_walk
def test_capture_root_rebind_during_enumeration_is_refused_before_publishing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A path rename that swaps `checkout_root` for a different repository while Git
    enumeration is in flight must not let that replacement's bytes reach a manifest:
    every git query in one enumeration is pinned to the descriptor opened before the
    swap, and the identity re-check once enumeration is done must then refuse rather
    than silently capture whatever now sits at that path."""
    root = _git_repo(tmp_path)
    replacement = tmp_path / "replacement"
    (replacement / "pkg").mkdir(parents=True)
    (replacement / "pkg" / "a.py").write_bytes(b"REPLACEMENT CONTENT\n")
    for args in (
        ["init", "-q"],
        ["add", "."],
        ["-c", "commit.gpgsign=false", "commit", "-q", "-m", "init"],
    ):
        subprocess.run(["git", *args], cwd=replacement, env=_GIT_ENV, check=True)
    moved_original = tmp_path / "repo-original"

    real_git = source_capture._git
    calls = 0

    def rebinding(root_fd: int, *args: str) -> bytes:
        nonlocal calls
        calls += 1
        result = real_git(root_fd, *args)
        if calls == 1:
            root.rename(moved_original)
            replacement.rename(root)
        return result

    monkeypatch.setattr(source_capture, "_git", rebinding)
    with pytest.raises(
        SourceCaptureRefused, match="changed identity during capture"
    ):
        _manifest(root)


@needs_walk
def test_capture_root_swapped_for_dirty_clone_between_enumeration_and_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A different clone, sharing HEAD and the tracked-file list but carrying dirty
    replacement bytes, swapped in only for the read between the first enumeration and
    the capture pass, must not let those bytes reach the manifest -- even though the
    swap is reverted immediately afterwards, well before the trailing enumeration that
    a before/after comparison alone would see as unchanged. One identity is pinned for
    the whole attempt, and it is the read itself that must refuse a root that does not
    match it, not the enumeration surrounding it."""
    root = _git_repo(tmp_path)
    evil = tmp_path / "evil-clone"
    subprocess.run(
        ["git", "clone", "-q", str(root), str(evil)], env=_GIT_ENV, check=True
    )
    (evil / "pkg" / "a.py").write_bytes(b"DIRTY REPLACEMENT BYTES\n")
    moved_original = tmp_path / "repo-original"

    real_open_root = source_capture._open_checkout_root
    calls = 0

    def swapping(
        components: tuple[bytes, ...],
        expected_identity: tuple[int, int] | None = None,
    ) -> int:
        nonlocal calls
        calls += 1
        if calls % 3 == 0:
            root.rename(moved_original)
            evil.rename(root)
            try:
                return real_open_root(components, expected_identity)
            finally:
                root.rename(evil)
                moved_original.rename(root)
        return real_open_root(components, expected_identity)

    monkeypatch.setattr(source_capture, "_open_checkout_root", swapping)
    manifest = _manifest(root)

    assert not manifest.complete
    assert manifest.attempts == source_capture.MAX_CAPTURE_ATTEMPTS
    assert not manifest.files
    assert all(b"DIRTY REPLACEMENT" not in f.content for f in manifest.files)
    assert {o.reason for o in manifest.omissions} >= {
        "changed_during_capture",
        "unstable_checkout",
    }
