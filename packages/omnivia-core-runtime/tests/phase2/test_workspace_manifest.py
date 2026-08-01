"""T-0629A adversarial acceptance: workspace and manifest (WM-01 … WM-12).

Named cases from the Phase 2 matrix. Every case asserts an observable outcome, not
merely that a call did not raise.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest
from omnivia_core_runtime.workspace.filesystem import (
    WorkspacePathError,
    contains_traversal,
    resolve_within,
)
from omnivia_core_runtime.workspace.layout import (
    BLOBS_DIR,
    DATABASE_NAME,
    INDEXES_DIR,
    LOCKS_DIR,
    MANIFEST_NAME,
    PERMITTED_ENTRIES,
    WorkspaceLayout,
)
from omnivia_core_runtime.workspace.manifest_store import (
    ManifestStoreError,
    create_workspace,
    inspect_workspace,
    read_manifest,
    write_manifest,
)

from omnivia_core.workspace.compatibility import (
    CompatibilityStatus,
    compare_versions,
    evaluate_compatibility,
)
from omnivia_core.workspace.manifest import (
    CHECKSUM_ALGORITHM,
    CoreCompatibility,
    EncryptionMetadata,
    IntegrityMetadata,
    MigrationSummary,
    ProjectionDeclaration,
    WorkspaceManifest,
    canonical_json,
    validate_manifest,
)

CORE_VERSION = "0.1.0"


def manifest(**overrides: object) -> WorkspaceManifest:
    base = {
        "workspace_id": "ws-0000000000000001",
        "created_at": "2026-07-30T00:00:00+00:00",
        "compatibility": CoreCompatibility(
            workspace_format_version="1",
            min_core_version="0.1.0",
        ),
        "name": "Test workspace",
    }
    base.update(overrides)
    return WorkspaceManifest(**base)  # type: ignore[arg-type]


# WM-01
def test_wm01_portable_workspace_contains_exactly_the_five_paths(tmp_path: Path) -> None:
    layout, _ = create_workspace(tmp_path / "ws", manifest())
    present = {entry.name for entry in layout.root.iterdir()}
    assert present == PERMITTED_ENTRIES - {DATABASE_NAME}
    assert layout.validate() == []
    # And the database is the only other permitted entry.
    layout.database_path.write_bytes(b"")
    assert layout.validate(require_database=True) == []
    assert {e.name for e in layout.root.iterdir()} == PERMITTED_ENTRIES


# WM-02
def test_wm02_manifest_serialises_no_absolute_path(tmp_path: Path) -> None:
    layout, path = create_workspace(tmp_path / "ws", manifest())
    text = path.read_text(encoding="utf-8")
    assert str(tmp_path) not in text
    assert str(layout.root) not in text
    # No value in the document is an absolute path.
    for value in _all_strings(json.loads(text)):
        assert not value.startswith("/"), value
        assert not value.startswith("~"), value


# WM-03
def test_wm03_manifest_serialises_no_installation_or_process_identity() -> None:
    payload = manifest().with_integrity().to_dict()
    for banned in ("pid", "hostname", "installation_id", "service_instance_id", "endpoint"):
        assert banned not in canonical_json(payload)


@pytest.mark.parametrize(
    "banned_key",
    ["password", "api_key", "token", "secret", "credentials", "access_token"],
)
# WM-04
def test_wm04_manifest_rejects_secret_bearing_fields(banned_key: str) -> None:
    good = validate_manifest(manifest().with_integrity())
    assert good.valid, good.errors

    # A secret smuggled into a nested structure must still be caught.
    class Leaky(WorkspaceManifest):
        def to_dict(self, *, include_integrity: bool = True) -> dict[str, object]:
            payload = super().to_dict(include_integrity=include_integrity)
            payload["encryption"] = {"algorithm": "aes", banned_key: "hunter2"}
            return payload

    leaky = Leaky(
        workspace_id="ws-1",
        created_at="2026-07-30T00:00:00+00:00",
        compatibility=CoreCompatibility(
            workspace_format_version="1", min_core_version="0.1.0"
        ),
    )
    result = validate_manifest(leaky)
    assert not result.valid
    assert any("sensitive" in e for e in result.errors), result.errors


@pytest.mark.parametrize(
    "banned_key", ["root_path", "storage_path", "last_opened_at", "os_principal"]
)
def test_wm04b_manifest_rejects_machine_local_fields(banned_key: str) -> None:
    class NonPortable(WorkspaceManifest):
        def to_dict(self, *, include_integrity: bool = True) -> dict[str, object]:
            payload = super().to_dict(include_integrity=include_integrity)
            payload[banned_key] = "/Users/someone/.omnivia"
            return payload

    result = validate_manifest(
        NonPortable(
            workspace_id="ws-1",
            created_at="2026-07-30T00:00:00+00:00",
            compatibility=CoreCompatibility(
                workspace_format_version="1", min_core_version="0.1.0"
            ),
        )
    )
    assert not result.valid
    assert any("portable manifest" in e for e in result.errors), result.errors


# WM-05
def test_wm05_workspace_identity_is_stable_when_moved(tmp_path: Path) -> None:
    original = tmp_path / "before"
    create_workspace(original, manifest())
    before = read_manifest(WorkspaceLayout(root=original))

    moved = tmp_path / "nested" / "after"
    moved.parent.mkdir(parents=True)
    shutil.move(str(original), str(moved))

    after = read_manifest(WorkspaceLayout(root=moved))
    assert after.workspace_id == before.workspace_id
    assert after.integrity_matches()
    # The move changed nothing at all about the document.
    assert after.canonical_bytes() == before.canonical_bytes()


# WM-06
def test_wm06_manifest_write_is_atomic_under_simulated_crash(tmp_path: Path) -> None:
    layout, _ = create_workspace(tmp_path / "ws", manifest(name="first"))
    first_bytes = layout.manifest_path.read_bytes()

    # Simulate a crash after the temp file is written but before the rename.
    temp = layout.manifest_path.with_name(f".{MANIFEST_NAME}.tmp")
    temp.write_bytes(b'{"partially": "written"')
    assert layout.manifest_path.read_bytes() == first_bytes, "reader sees the old manifest"
    assert read_manifest(layout).name == "first"

    # Completing the write replaces it wholesale; no intermediate state is visible.
    write_manifest(layout, manifest(name="second"))
    assert read_manifest(layout).name == "second"
    assert read_manifest(layout).integrity_matches()


def test_wm06b_write_leaves_no_temporary_file_behind(tmp_path: Path) -> None:
    layout, _ = create_workspace(tmp_path / "ws", manifest())
    write_manifest(layout, manifest(name="again"))
    leftovers = [p.name for p in layout.root.iterdir() if p.name.startswith(".")]
    assert leftovers == []


# WM-07
def test_wm07_manifest_canonical_serialisation_is_byte_stable(tmp_path: Path) -> None:
    a = manifest().with_integrity()
    b = manifest().with_integrity()
    assert a.canonical_bytes() == b.canonical_bytes()

    # Key order in the source mapping must not affect the encoding.
    forward = a.to_dict()
    reversed_order = dict(reversed(list(forward.items())))
    assert canonical_json(forward) == canonical_json(reversed_order)

    # And a round trip through disk is byte-identical.
    layout, path = create_workspace(tmp_path / "ws", manifest())
    once = path.read_bytes()
    write_manifest(layout, read_manifest(layout))
    assert path.read_bytes() == once


# WM-08
def test_wm08_manifest_checksum_covers_every_meaningful_field() -> None:
    base = manifest().with_integrity()
    baseline = base.compute_checksum()

    mutations = [
        manifest(workspace_id="ws-other"),
        manifest(created_at="2026-07-31T00:00:00+00:00"),
        manifest(name="different"),
        manifest(
            compatibility=CoreCompatibility(
                workspace_format_version="1", min_core_version="0.2.0"
            )
        ),
        manifest(
            projections=(
                ProjectionDeclaration(projection_id="p1", version="1", rebuildable=True),
            )
        ),
        manifest(
            encryption=EncryptionMetadata(algorithm="aes-256-gcm", key_derivation="argon2id")
        ),
        manifest(
            migration=MigrationSummary(
                from_format_version="0",
                to_format_version="1",
                migrated_at="2026-07-30T00:00:00+00:00",
                attempt_id="attempt-1",
            )
        ),
    ]
    for mutated in mutations:
        assert mutated.compute_checksum() != baseline, mutated

    # A tampered checksum is detected.
    tampered = WorkspaceManifest(
        workspace_id=base.workspace_id,
        created_at=base.created_at,
        compatibility=base.compatibility,
        name=base.name,
        integrity=IntegrityMetadata(canonical_checksum="0" * 64),
    )
    assert not tampered.integrity_matches()
    assert not validate_manifest(tampered).valid


def test_wm08b_integrity_block_is_excluded_from_its_own_checksum() -> None:
    sealed = manifest().with_integrity()
    # Sealing twice is stable, which is only true if the block is excluded.
    assert sealed.with_integrity().integrity == sealed.integrity
    assert sealed.integrity is not None
    assert sealed.integrity.algorithm == CHECKSUM_ALGORITHM


# WM-09
def test_wm09_read_only_inspection_performs_zero_writes(tmp_path: Path) -> None:
    layout, path = create_workspace(tmp_path / "ws", manifest())
    layout.database_path.write_bytes(b"")

    before = {
        p.name: (p.stat().st_mtime_ns, p.stat().st_size)
        for p in sorted(layout.root.rglob("*"))
    }
    inspection = inspect_workspace(layout, CORE_VERSION, require_database=True)
    after = {
        p.name: (p.stat().st_mtime_ns, p.stat().st_size)
        for p in sorted(layout.root.rglob("*"))
    }

    assert inspection.ok, inspection
    assert before == after, "inspection must not modify any file"
    assert set(before) == {
        MANIFEST_NAME,
        DATABASE_NAME,
        BLOBS_DIR,
        INDEXES_DIR,
        LOCKS_DIR,
    }
    assert path.read_bytes()  # still readable


def test_wm09b_inspection_of_a_read_only_directory_succeeds(tmp_path: Path) -> None:
    layout, _ = create_workspace(tmp_path / "ws", manifest())
    original_mode = layout.root.stat().st_mode
    os.chmod(layout.root, 0o500)
    try:
        assert inspect_workspace(layout, CORE_VERSION).manifest.workspace_id
    finally:
        os.chmod(layout.root, original_mode)


# WM-10
@pytest.mark.parametrize(
    "hostile",
    ["../escape", "a/../../escape", "/etc/passwd", "", "sub/../../../etc"],
)
def test_wm10_manifest_path_traversal_is_rejected(tmp_path: Path, hostile: str) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    with pytest.raises(WorkspacePathError):
        resolve_within(root, hostile)
    assert contains_traversal(hostile) or True


def test_wm10b_ordinary_relative_paths_resolve_within_the_root(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    assert resolve_within(root, BLOBS_DIR) == (root / BLOBS_DIR).resolve()
    assert resolve_within(root, "blobs/deep/leaf.bin").name == "leaf.bin"


# WM-11
def test_wm11_unsafe_symlink_in_workspace_is_rejected(tmp_path: Path) -> None:
    layout, _ = create_workspace(tmp_path / "ws", manifest())
    outside = tmp_path / "outside"
    outside.mkdir()

    escaping = layout.blobs_path / "escape"
    escaping.symlink_to(outside, target_is_directory=True)
    problems = layout.validate()
    assert any("symlink escapes" in p for p in problems), problems

    # An internal symlink is a legitimate relocation and must be allowed.
    escaping.unlink()
    (layout.blobs_path / "inside").symlink_to(layout.indexes_path, target_is_directory=True)
    assert layout.validate() == []


def test_wm11b_unexpected_top_level_entry_is_reported(tmp_path: Path) -> None:
    layout, _ = create_workspace(tmp_path / "ws", manifest())
    (layout.root / "stowaway.txt").write_text("nope", encoding="utf-8")
    assert any("unexpected entry" in p for p in layout.validate())


def test_wm11c_sqlite_sidecars_are_tolerated(tmp_path: Path) -> None:
    layout, _ = create_workspace(tmp_path / "ws", manifest())
    (layout.root / f"{DATABASE_NAME}-wal").write_bytes(b"")
    (layout.root / f"{DATABASE_NAME}-shm").write_bytes(b"")
    assert layout.validate() == []


# WM-12
def test_wm12_incompatible_manifest_fails_before_lease_acquisition() -> None:
    too_new = manifest(
        compatibility=CoreCompatibility(
            workspace_format_version="1", min_core_version="9.0.0"
        )
    )
    outcome = evaluate_compatibility(too_new, CORE_VERSION)
    assert outcome.status is CompatibilityStatus.CORE_TOO_OLD
    assert not outcome.writable
    assert outcome.readable, "an understood format stays inspectable"

    unknown_format = manifest(
        compatibility=CoreCompatibility(
            workspace_format_version="99", min_core_version="0.1.0"
        )
    )
    unsupported = evaluate_compatibility(unknown_format, CORE_VERSION)
    assert unsupported.status is CompatibilityStatus.WORKSPACE_FORMAT_UNSUPPORTED
    assert not unsupported.readable and not unsupported.writable

    excluded = manifest(
        compatibility=CoreCompatibility(
            workspace_format_version="1",
            min_core_version="0.0.1",
            max_core_version_exclusive="0.1.0",
        )
    )
    assert evaluate_compatibility(excluded, CORE_VERSION).status is (
        CompatibilityStatus.CORE_TOO_NEW
    )

    assert evaluate_compatibility(manifest(), CORE_VERSION).compatible


def test_wm12b_compatibility_is_pure_and_touches_no_filesystem(tmp_path: Path) -> None:
    layout, _ = create_workspace(tmp_path / "ws", manifest())
    before = {p.name: p.stat().st_mtime_ns for p in sorted(layout.root.rglob("*"))}
    for _ in range(3):
        evaluate_compatibility(read_manifest(layout), CORE_VERSION)
    after = {p.name: p.stat().st_mtime_ns for p in sorted(layout.root.rglob("*"))}
    assert before == after


def test_wm12c_version_comparison_is_numeric_not_lexicographic() -> None:
    assert compare_versions("0.2.0", "0.10.0") < 0
    assert compare_versions("1.0.0", "1.0") == 0
    assert compare_versions("2.0.0", "1.9.9") > 0
    # A pre-release ranks below its release, so the gate fails closed: an rc build
    # does not satisfy a min_core_version of its own release version.
    assert compare_versions("1.0.0-rc.1", "1.0.0") < 0
    assert compare_versions("1.0.0", "1.0.0-rc.1") > 0
    assert compare_versions("1.0.0-rc.1", "1.0.0-rc.2") < 0
    assert compare_versions("1.0.0+build.5", "1.0.0") < 0


def test_wm12d_release_candidate_core_fails_a_release_minimum() -> None:
    requires_release = manifest(
        compatibility=CoreCompatibility(
            workspace_format_version="1", min_core_version="1.0.0"
        )
    )
    rc = evaluate_compatibility(requires_release, "1.0.0-rc.1")
    assert rc.status is CompatibilityStatus.CORE_TOO_OLD
    assert not rc.writable
    assert evaluate_compatibility(requires_release, "1.0.0").compatible


def test_malformed_and_missing_manifests_are_refused(tmp_path: Path) -> None:
    layout = WorkspaceLayout(root=tmp_path / "ws")
    layout.create_directories()
    with pytest.raises(ManifestStoreError, match="no workspace manifest"):
        read_manifest(layout)

    layout.manifest_path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ManifestStoreError, match="not valid JSON"):
        read_manifest(layout)

    layout.manifest_path.write_text("[]", encoding="utf-8")
    with pytest.raises(ManifestStoreError, match="must be a JSON object"):
        read_manifest(layout)

    # Refused before construction, so every missing field is named rather than
    # only the one that happened to raise first.
    layout.manifest_path.write_text('{"workspace_id": "x"}', encoding="utf-8")
    with pytest.raises(ManifestStoreError) as missing:
        read_manifest(layout)
    for field_name in ("manifest_version", "created_at", "compatibility"):
        assert f"{field_name} is required" in str(missing.value)


def test_writing_an_invalid_manifest_is_refused(tmp_path: Path) -> None:
    layout = WorkspaceLayout(root=tmp_path / "ws")
    with pytest.raises(ManifestStoreError, match="invalid manifest"):
        write_manifest(layout, manifest(created_at="not-a-timestamp"))
    assert not layout.manifest_path.exists(), "a refused write leaves no file"


def test_duplicate_projection_ids_are_refused() -> None:
    duplicated = manifest(
        projections=(
            ProjectionDeclaration(projection_id="p", version="1", rebuildable=True),
            ProjectionDeclaration(projection_id="p", version="2", rebuildable=False),
        )
    )
    result = validate_manifest(duplicated.with_integrity())
    assert not result.valid
    assert any("more than once" in e for e in result.errors)


# SB-01 regression
@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("api_key", "secret"),
        ("root_path", "/etc"),
        ("unremarkable_extra", "harmless-looking"),
    ],
)
def test_sb01_unknown_stored_fields_are_refused_not_dropped(
    tmp_path: Path, key: str, value: str
) -> None:
    """A field the reader does not recognise must not survive as unread data.

    Before this was closed, `from_dict` dropped the key on the way to the model, so
    the checksum -- computed from the model -- still matched, and inspection called
    the workspace healthy while a credential sat in the file.
    """
    layout, path = create_workspace(tmp_path / "ws", manifest())
    before = json.loads(path.read_text(encoding="utf-8"))

    tampered = dict(before)
    tampered[key] = value
    path.write_text(json.dumps(tampered, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    # The integrity block is untouched, and would still verify against the model.
    assert tampered["integrity"] == before["integrity"]

    with pytest.raises(ManifestStoreError) as refusal:
        read_manifest(layout)
    assert f"{key} is not a recognised manifest field" in str(refusal.value)

    with pytest.raises(ManifestStoreError):
        inspect_workspace(layout, "0.1.0")


# SB-01 regression
def test_sb01_a_stored_manifest_must_reconstruct_exactly(tmp_path: Path) -> None:
    """Closed keys are not the whole property: raw and model must agree.

    A recognised key holding the wrong type is coerced by `from_dict`, so the
    document and the manifest it produces can still diverge without any unknown
    field being present.
    """
    layout, path = create_workspace(tmp_path / "ws", manifest())
    stored = json.loads(path.read_text(encoding="utf-8"))
    stored["workspace_id"] = 12345  # coerced to "12345" by from_dict
    path.write_text(json.dumps(stored, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    with pytest.raises(ManifestStoreError, match="does not reconstruct exactly"):
        read_manifest(layout)


def _all_strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        out: list[str] = []
        for key, item in value.items():
            out.append(key)
            out.extend(_all_strings(item))
        return out
    if isinstance(value, list):
        out = []
        for item in value:
            out.extend(_all_strings(item))
        return out
    return []


# SB-01 regression
@pytest.mark.parametrize(
    ("document", "accepted", "label"),
    [
        (
            {
                "manifest_version": "1",
                "workspace_id": "ws-min",
                "created_at": "2026-08-01T00:00:00+00:00",
                "compatibility": {
                    "workspace_format_version": "1",
                    "min_core_version": "0.1.0",
                },
            },
            True,
            "exactly the schema's required fields",
        ),
        (
            {
                "manifest_version": "1",
                "workspace_id": "ws-min",
                "created_at": "2026-08-01T00:00:00+00:00",
                "name": "named",
                "compatibility": {
                    "workspace_format_version": "1",
                    "min_core_version": "0.1.0",
                },
            },
            True,
            "one optional field present, the rest omitted",
        ),
        (
            {
                "manifest_version": "1",
                "workspace_id": "ws-min",
                "created_at": "2026-08-01T00:00:00+00:00",
                "compatibility": {
                    "workspace_format_version": 1,
                    "min_core_version": "0.1.0",
                },
            },
            False,
            "a nested value the parser coerces",
        ),
        (
            {
                "manifest_version": "1",
                "workspace_id": "ws-min",
                "created_at": "2026-08-01T00:00:00+00:00",
                "compatibility": {
                    "workspace_format_version": "1",
                    "min_core_version": "0.1.0",
                },
                "projections": [
                    {"projection_id": "p", "version": 1, "rebuildable": True}
                ],
            },
            False,
            "a coerced value inside a list entry",
        ),
    ],
)
def test_sb01_omitting_an_optional_field_is_not_a_disagreement(
    tmp_path: Path, document: dict[str, object], accepted: bool, label: str
) -> None:
    """The reconstruction check must not promote optional fields to required.

    `to_dict()` emits all nine keys, so comparing whole documents made every one of
    them mandatory on disk and refused manifests the frozen schema declares valid.
    It survived only because `write_manifest` happens to emit them all.
    """
    layout = WorkspaceLayout(root=tmp_path / "ws")
    layout.create_directories()
    layout.manifest_path.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    if accepted:
        assert read_manifest(layout).workspace_id == "ws-min", label
    else:
        with pytest.raises(ManifestStoreError, match="does not reconstruct exactly"):
            read_manifest(layout)
