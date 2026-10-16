"""The packaged document runtime: layout, manifest, relocation and resolution.

Two halves, deliberately separate. The manifest half is pure bookkeeping over
synthetic payloads and runs anywhere: it is where omission, tampering, an extra
field, an extra file, a link and a path escape are each shown to be fatal. The
relocation half needs real Mach-O files, so it compiles two of its own with the
host toolchain and is skipped where there is none -- a payload whose load
commands were never rewritten is not something a stand-in file can stand in for.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

import pytest
from omnivia_core_runtime.distribution import packaged_runtime as payload

needs_mach_o = pytest.mark.skipif(
    sys.platform != "darwin" or shutil.which("cc") is None,
    reason="relocation needs macOS and a host C toolchain",
)

LANGUAGES = ("eng", "osd")


def _payload_root(candidate: Path) -> Path:
    root = payload.runtime_root(candidate)
    for directory in payload.PAYLOAD_DIRS:
        (root / directory).mkdir(parents=True, exist_ok=True)
    return root


def _declare(candidate: Path, role: str, parts: tuple[str, ...], body: bytes) -> dict:
    """Write one synthetic packaged file and the declaration that matches it."""
    target = payload.runtime_root(candidate).joinpath(*parts)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(body)
    return {
        "identity": "synthetic",
        "path": "/".join(
            (f"{payload.BUNDLE_NAME}.app", "Contents", "Resources", payload.RUNTIME_DIR, *parts)
        ),
        "role": role,
        "sha256": hashlib.sha256(body).hexdigest(),
        "size": len(body),
    }


@pytest.fixture
def candidate(tmp_path: Path) -> Path:
    """A synthetic candidate holding one complete declarable payload."""
    root = tmp_path / "candidate"
    _payload_root(root)
    return root


def _complete(candidate: Path) -> list[dict]:
    declared = [
        _declare(candidate, payload.ROLE_PANDOC, (payload.BIN_DIR, "pandoc"), b"pandoc"),
        _declare(
            candidate, payload.ROLE_TESSERACT, (payload.BIN_DIR, "tesseract"), b"tess"
        ),
        _declare(
            candidate,
            f"{payload.LIBRARY_ROLE}libthing.1.dylib",
            (payload.LIB_DIR, "libthing.1.dylib"),
            b"lib",
        ),
        _declare(
            candidate,
            f"{payload.NOTICE_ROLE}NOTICE.txt",
            (payload.NOTICE_DIR, "NOTICE.txt"),
            b"notice",
        ),
    ]
    for language in LANGUAGES:
        declared.append(
            _declare(
                candidate,
                f"{payload.MODEL_ROLE}{language}",
                (payload.TESSDATA_DIR, f"{language}{payload.MODEL_SUFFIX}"),
                f"model-{language}".encode(),
            )
        )
    return declared


def _write_manifest(candidate: Path, declared: object) -> None:
    (candidate / payload.MANIFEST_NAME).write_text(
        json.dumps({"resources": declared}, indent=2, sort_keys=True) + "\n",
        encoding="ascii",
    )


# ---------------------------------------------------------------------------
# The manifest section, checked in both directions
# ---------------------------------------------------------------------------


def test_a_complete_payload_verifies_and_resolves_absolute_paths(
    candidate: Path,
) -> None:
    declared = _complete(candidate)

    assert payload.resource_findings(candidate, declared) == []
    verified = payload.verified_runtime(candidate, declared)

    root = payload.runtime_root(candidate)
    assert verified.root == root
    assert verified.pandoc == root / "bin" / "pandoc"
    assert verified.tesseract == root / "bin" / "tesseract"
    assert verified.tessdata_dir == root / "tessdata"
    # The models carry the size and digest the manifest pinned, which is what
    # lets a selection be identified without reading the directory again.
    assert sorted(verified.models) == ["eng", "osd"]
    assert all(size > 0 and len(digest) == 64 for size, digest in verified.models.values())
    # Every resolved path is absolute and inside the payload: no bare name, and
    # nothing a caller or a document could have supplied.
    for resolved in (verified.pandoc, verified.tesseract, verified.tessdata_dir):
        assert resolved.is_absolute() and root in resolved.parents


def test_an_empty_declaration_over_no_payload_is_a_coherent_candidate(
    tmp_path: Path,
) -> None:
    """A companion-only candidate packages no runtime and is refused nothing."""
    bare = tmp_path / "bare"
    bare.mkdir()

    assert payload.resource_findings(bare, []) == []
    with pytest.raises(payload.PackagedRuntimeRefused):
        payload.verified_runtime(bare, [])


def test_a_tampered_packaged_byte_is_refused(candidate: Path) -> None:
    declared = _complete(candidate)
    model = payload.runtime_root(candidate) / "tessdata" / "eng.traineddata"
    model.write_bytes(b"model-eng"[:-1] + b"X")

    assert payload.resource_findings(candidate, declared) == [
        "packaged resource digest differs: model:eng"
    ]


def test_a_resized_packaged_file_is_refused_before_it_is_digested(
    candidate: Path,
) -> None:
    declared = _complete(candidate)
    (payload.runtime_root(candidate) / "bin" / "pandoc").write_bytes(b"pandoc-longer")

    assert payload.resource_findings(candidate, declared) == [
        "packaged resource size differs: pandoc"
    ]


def test_an_omitted_declaration_is_refused_from_both_directions(
    candidate: Path,
) -> None:
    """The file is still there, so leaving it out of the manifest is a finding."""
    declared = [
        entry for entry in _complete(candidate) if entry["role"] != payload.ROLE_PANDOC
    ]

    findings = payload.resource_findings(candidate, declared)
    assert "packaged runtime declares no pandoc" in findings
    assert any(finding.startswith("undeclared file") for finding in findings)


def test_a_declared_resource_whose_file_is_absent_is_refused(candidate: Path) -> None:
    declared = _complete(candidate)
    (payload.runtime_root(candidate) / "bin" / "tesseract").unlink()

    assert payload.resource_findings(candidate, declared) == [
        "packaged resource missing: tesseract"
    ]


def test_a_missing_model_or_notice_is_refused(candidate: Path) -> None:
    declared = [
        entry
        for entry in _complete(candidate)
        if entry["role"] not in {"model:osd", "notice:NOTICE.txt"}
    ]
    (payload.runtime_root(candidate) / "tessdata" / "osd.traineddata").unlink()
    (payload.runtime_root(candidate) / "licenses" / "NOTICE.txt").unlink()

    findings = payload.resource_findings(candidate, declared)
    assert "packaged runtime declares no osd model" in findings
    assert "packaged runtime declares no licence notice" in findings


def test_an_extra_declared_field_is_refused_rather_than_ignored(
    candidate: Path,
) -> None:
    declared = _complete(candidate)
    declared[0] = {**declared[0], "signed": True}

    findings = payload.resource_findings(candidate, declared)
    assert "packaged resource field not bound by this schema: entry 0.signed" in findings
    # And the entry declares nothing after that: a malformed declaration leaves
    # its file undeclared and its required role unfilled, both of which are
    # findings of their own.
    assert "packaged runtime declares no pandoc" in findings


def test_a_missing_declared_field_is_refused(candidate: Path) -> None:
    declared = _complete(candidate)
    declared[0] = {
        key: value for key, value in declared[0].items() if key != "sha256"
    }

    assert (
        "packaged resource field missing: entry 0.sha256"
        in payload.resource_findings(candidate, declared)
    )


def test_an_unmanaged_role_is_refused(candidate: Path) -> None:
    declared = _complete(candidate)
    declared.append({**declared[0], "role": "helper"})

    assert "packaged resource role not managed: entry 6" in payload.resource_findings(
        candidate, declared
    )


def test_a_role_declared_twice_is_refused(candidate: Path) -> None:
    declared = _complete(candidate)
    declared.append(dict(declared[0]))

    assert "packaged resource declared twice: pandoc" in payload.resource_findings(
        candidate, declared
    )


@pytest.mark.parametrize(
    "path",
    [
        "OmniVia Core.app/Contents/Resources/omnivia-runtime/../../../../etc/passwd",
        "/etc/passwd",
        "OmniVia Core.app/Contents/Resources/omnivia-runtime/bin/../bin/pandoc",
        "etc/passwd",
    ],
)
def test_a_declared_path_outside_its_role_is_unrepresentable(
    candidate: Path, path: str
) -> None:
    """A role has exactly one accepted path, so an escape cannot be declared.

    The check is not "does this path contain ``..``" but "is this path the one
    this role lives at", which is what makes every other spelling -- absolute,
    relative, normalised-equivalent -- a finding without enumerating them.
    """
    declared = _complete(candidate)
    declared[0] = {**declared[0], "path": path}

    assert "packaged resource path mismatch: pandoc" in payload.resource_findings(
        candidate, declared
    )


def test_a_symlink_standing_in_for_a_packaged_file_is_refused(
    candidate: Path, tmp_path: Path
) -> None:
    declared = _complete(candidate)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.write_bytes(b"pandoc")
    target = payload.runtime_root(candidate) / "bin" / "pandoc"
    target.unlink()
    target.symlink_to(elsewhere)

    # The bytes behind the link are the declared bytes, and it is still refused:
    # what a digest was taken over has to be what the loader opens.
    assert payload.resource_findings(candidate, declared) == [
        "packaged resource is not a regular file: pandoc"
    ]


def test_an_undeclared_file_or_directory_inside_the_payload_is_refused(
    candidate: Path,
) -> None:
    declared = _complete(candidate)
    (payload.runtime_root(candidate) / "bin" / "extra").write_bytes(b"extra")
    (payload.runtime_root(candidate) / "stash").mkdir()

    findings = payload.resource_findings(candidate, declared)
    assert "undeclared directory in the packaged runtime: stash" in findings
    assert any("undeclared file in the packaged runtime" in item for item in findings)


def test_a_declaration_that_is_not_a_bounded_list_is_refused(candidate: Path) -> None:
    assert payload.resource_findings(candidate, {"pandoc": "x"}) == [
        "manifest declares no packaged resource set"
    ]
    assert payload.resource_findings(
        candidate, [{}] * (payload.MAX_RESOURCES + 1)
    ) == [f"packaged resource set exceeds {payload.MAX_RESOURCES} entries"]


# ---------------------------------------------------------------------------
# Resolution from verified installation state
# ---------------------------------------------------------------------------


def test_the_installation_resolves_its_own_payload_and_nothing_else(
    tmp_path: Path,
) -> None:
    installation = tmp_path / "installation-state"
    candidate = installation / payload.INSTALLED_CANDIDATE_DIR
    _payload_root(candidate)
    _write_manifest(candidate, _complete(candidate))

    verified = payload.installed_runtime(installation)

    assert verified is not None
    assert verified.root == payload.runtime_root(candidate)
    # No installation root is no payload, which is the fail-closed default.
    assert payload.installed_runtime(None) is None
    assert payload.installed_runtime(tmp_path / "absent") is None


@pytest.mark.parametrize(
    "break_it",
    [
        pytest.param(
            lambda candidate: (candidate / payload.MANIFEST_NAME).write_text("{"),
            id="manifest does not decode",
        ),
        pytest.param(
            lambda candidate: (candidate / payload.MANIFEST_NAME).unlink(),
            id="manifest absent",
        ),
        pytest.param(
            lambda candidate: (
                payload.runtime_root(candidate) / "bin" / "pandoc"
            ).write_bytes(b"other"),
            id="a packaged byte moved",
        ),
        pytest.param(
            lambda candidate: shutil.rmtree(payload.runtime_root(candidate) / "lib"),
            id="a packaged file is gone",
        ),
    ],
)
def test_resolution_fails_closed_on_anything_that_is_not_exactly_declared(
    tmp_path: Path, break_it: object
) -> None:
    installation = tmp_path / "installation-state"
    candidate = installation / payload.INSTALLED_CANDIDATE_DIR
    _payload_root(candidate)
    _write_manifest(candidate, _complete(candidate))
    assert payload.installed_runtime(installation) is not None

    break_it(candidate)  # type: ignore[operator]

    assert payload.installed_runtime(installation) is None


def test_a_candidate_root_that_is_a_link_or_relative_is_refused(
    tmp_path: Path,
) -> None:
    candidate = tmp_path / "candidate"
    _payload_root(candidate)
    _write_manifest(candidate, _complete(candidate))
    linked = tmp_path / "linked"
    linked.symlink_to(candidate)

    with pytest.raises(payload.PackagedRuntimeRefused):
        payload.verify_candidate_runtime(linked)
    with pytest.raises(payload.PackagedRuntimeRefused):
        payload.verify_candidate_runtime(Path("candidate"))


# ---------------------------------------------------------------------------
# Relocation, over Mach-O files this test builds
# ---------------------------------------------------------------------------


def _build_mach_o(tmp_path: Path, *, name: str = "libthing") -> tuple[Path, Path]:
    """One dylib and one executable that loads it by absolute install name.

    The absolute install name is the point: it is what a package manager's own
    build leaves behind, and what relocation has to replace.
    """
    source = tmp_path / "source"
    source.mkdir(parents=True)
    (source / "thing.c").write_text("int thing(void) { return 7; }\n")
    (source / "main.c").write_text(
        "#include <stdio.h>\nint thing(void);\n"
        'int main(void) { printf("thing %d\\n", thing()); return 0; }\n'
    )
    library = tmp_path / "prefix" / "lib" / f"{name}.1.dylib"
    library.parent.mkdir(parents=True)
    subprocess.run(
        [
            "cc",
            "-dynamiclib",
            "-install_name",
            str(library),
            str(source / "thing.c"),
            "-o",
            str(library),
        ],
        check=True,
        capture_output=True,
    )
    executable = tmp_path / "prefix" / "bin" / "probe"
    executable.parent.mkdir(parents=True)
    subprocess.run(
        ["cc", str(source / "main.c"), str(library), "-o", str(executable)],
        check=True,
        capture_output=True,
    )
    return executable, library


def _notices(tmp_path: Path) -> Mapping[str, Path]:
    notice = tmp_path / "NOTICE.txt"
    notice.write_text("synthetic notice\n")
    return {notice.name: notice}


def _models(tmp_path: Path) -> Mapping[str, Path]:
    models = {}
    for language in LANGUAGES:
        model = tmp_path / f"{language}{payload.MODEL_SUFFIX}"
        model.write_bytes(f"model-{language}".encode())
        models[language] = model
    return models


@needs_mach_o
def test_staging_relocates_the_closure_and_declares_the_bytes_that_run(
    tmp_path: Path,
) -> None:
    executable, library = _build_mach_o(tmp_path)
    candidate = tmp_path / "candidate"

    declared = payload.stage_packaged_runtime(
        candidate,
        pandoc=executable,
        tesseract=executable,
        models=_models(tmp_path),
        notices=_notices(tmp_path),
    )

    # The manifest the packager wrote verifies against the bytes it left behind,
    # which is only true because the digests were taken after relocation.
    entries = [entry.to_manifest() for entry in declared]
    assert payload.resource_findings(candidate, entries) == []
    root = payload.runtime_root(candidate)
    assert {entry.role for entry in declared} >= {
        payload.ROLE_PANDOC,
        payload.ROLE_TESSERACT,
        "library:libthing.1.dylib",
        "model:eng",
        "model:osd",
        "notice:NOTICE.txt",
    }
    # Nothing was linked: every staged file is a regular file of its own.
    for entry in declared:
        staged = candidate / entry.path
        assert staged.is_file() and not staged.is_symlink()
    # The dependency is now inside the payload, and the original prefix is gone
    # from the load commands entirely.
    listed = subprocess.run(
        ["otool", "-L", str(root / "bin" / "pandoc")],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "@loader_path/../lib/libthing.1.dylib" in listed
    assert str(library) not in listed
    for line in listed.splitlines()[1:]:
        reference = line.strip().split(" (")[0]
        assert reference.startswith(
            (*payload.SYSTEM_LIBRARY_PREFIXES, "@loader_path/")
        )
    # And no search path survives, so nothing is resolved by lookup at all.
    for name in ("bin/pandoc", "lib/libthing.1.dylib"):
        commands = subprocess.run(
            ["otool", "-l", str(root / name)],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        assert "LC_RPATH" not in commands
    # The staged executable's identity is what the staged executable reports.
    pandoc = next(entry for entry in declared if entry.role == payload.ROLE_PANDOC)
    assert pandoc.identity == "thing 7"


@needs_mach_o
def test_the_relocated_payload_runs_from_an_empty_environment(tmp_path: Path) -> None:
    """The dependency's original prefix is removed, and it still runs."""
    executable, library = _build_mach_o(tmp_path)
    candidate = tmp_path / "candidate"
    payload.stage_packaged_runtime(
        candidate,
        pandoc=executable,
        tesseract=executable,
        models=_models(tmp_path),
        notices=_notices(tmp_path),
    )
    shutil.rmtree(library.parent.parent)

    completed = subprocess.run(
        [str(payload.runtime_root(candidate) / "bin" / "pandoc")],
        check=False,
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin"},
    )

    assert completed.returncode == 0
    assert completed.stdout.strip() == "thing 7"


@needs_mach_o
def test_two_different_builds_of_one_library_name_are_refused(tmp_path: Path) -> None:
    """A collision is detected by digest, not papered over by last-write-wins.

    Staging either build would leave one executable loading bytes it was not
    linked against, so the whole payload is refused instead.
    """
    first, _library = _build_mach_o(tmp_path / "first")
    second, other = _build_mach_o(tmp_path / "second")
    # Same leaf name, different bytes.
    other.write_bytes(other.read_bytes() + b"\x00")
    subprocess.run(
        ["codesign", "--force", "--sign", "-", str(other)],
        check=False,
        capture_output=True,
    )

    with pytest.raises(payload.PackagedRuntimeRefused, match="two different builds"):
        payload.stage_packaged_runtime(
            tmp_path / "candidate",
            pandoc=first,
            tesseract=second,
            models=_models(tmp_path),
            notices=_notices(tmp_path),
        )


@needs_mach_o
def test_staging_refuses_a_linked_or_relative_input(tmp_path: Path) -> None:
    executable, _library = _build_mach_o(tmp_path)
    linked = tmp_path / "linked-pandoc"
    linked.symlink_to(executable)

    with pytest.raises(payload.PackagedRuntimeRefused):
        payload.stage_packaged_runtime(
            tmp_path / "linked-candidate",
            pandoc=linked,
            tesseract=executable,
            models=_models(tmp_path),
            notices=_notices(tmp_path),
        )
    with pytest.raises(payload.PackagedRuntimeRefused):
        payload.stage_packaged_runtime(
            tmp_path / "relative-candidate",
            pandoc=Path(os.path.relpath(executable)),
            tesseract=executable,
            models=_models(tmp_path),
            notices=_notices(tmp_path),
        )


def test_staging_refuses_an_incomplete_model_or_notice_set(tmp_path: Path) -> None:
    source = tmp_path / "pandoc"
    source.write_bytes(b"not a mach-o")

    with pytest.raises(payload.PackagedRuntimeRefused, match="packaged models missing"):
        payload.stage_packaged_runtime(
            tmp_path / "a",
            pandoc=source,
            tesseract=source,
            models={"eng": source},
            notices=_notices(tmp_path),
        )
    with pytest.raises(payload.PackagedRuntimeRefused, match="licence notices"):
        payload.stage_packaged_runtime(
            tmp_path / "b",
            pandoc=source,
            tesseract=source,
            models=_models(tmp_path),
            notices={},
        )


# ---------------------------------------------------------------------------
# The production join: bundled identities, and what happens when they move
# ---------------------------------------------------------------------------


def _installed(tmp_path: Path) -> Path:
    installation = tmp_path / "installation-state"
    candidate = installation / payload.INSTALLED_CANDIDATE_DIR
    _payload_root(candidate)
    _write_manifest(candidate, _complete(candidate))
    return installation


def test_the_default_selection_is_served_from_the_packaged_bytes(
    tmp_path: Path,
) -> None:
    """DOC-13: English and the orientation data come from the payload itself.

    No managed cache exists under this installation root, which is the point: a
    default-English capture is admitted, frozen and served without one, because
    the bundled models belong to the installation rather than to the cache.
    """
    from omnivia_core_runtime.service.processing_settings import (
        DEFAULT_SETTINGS,
        RESOURCES_FROZEN,
        admitted_resources,
        installation_catalogue,
        managed_resources,
        packaged_identities,
        packaged_runtime,
    )

    installation = _installed(tmp_path)
    authority = installation_catalogue(installation)
    assert authority is not None
    runtime = packaged_runtime(authority)
    identities = packaged_identities(runtime)

    admitted = admitted_resources(
        managed_resources(authority),
        pin="capture-pin",
        languages=DEFAULT_SETTINGS.languages,
        packaged=identities,
    )

    assert admitted.kind == RESOURCES_FROZEN
    assert {identity.language for identity in admitted.selection} == {"eng", "osd"}
    with authority.frozen_resources(
        operation_id="capture-pin",
        admitted=admitted,
        requested=DEFAULT_SETTINGS.languages,
    ) as resources:
        assert resources.available
        assert resources.languages == ("eng",)
        assert resources.tessdata_dir == str(runtime.tessdata_dir)
        assert resources.tesseract_path == str(runtime.tesseract)
        # Two models read and pinned: the recognition language and the
        # orientation data beside it.
        assert len(resources.model_hashes) == 2


def test_a_replaced_payload_is_an_unverified_model_and_not_a_pause(
    tmp_path: Path,
) -> None:
    """A capture admitted against bundled bytes that have moved settles.

    No pin was ever taken for packaged bytes, so asking the managed cache to
    re-assert one would raise `CaptureNotExecutable` and pause the obligation
    for ever. What is actually true is a resource condition -- these are not the
    bytes this capture was admitted against -- which is recorded against the
    extraction and recovered by reinstalling.
    """
    from omnivia_core_runtime.service.ocr_resources import REASON_UNVERIFIED
    from omnivia_core_runtime.service.processing_settings import (
        DEFAULT_SETTINGS,
        CaptureNotExecutable,
        admitted_resources,
        installation_catalogue,
        managed_resources,
        packaged_identities,
        packaged_runtime,
    )

    installation = _installed(tmp_path)
    authority = installation_catalogue(installation)
    assert authority is not None
    admitted = admitted_resources(
        managed_resources(authority),
        pin="capture-pin",
        languages=DEFAULT_SETTINGS.languages,
        packaged=packaged_identities(packaged_runtime(authority)),
    )
    # The installation is reinstalled with different model bytes, which is a
    # different identity: same language, same version token, other digest.
    candidate = installation / payload.INSTALLED_CANDIDATE_DIR
    shutil.rmtree(payload.runtime_root(candidate))
    _payload_root(candidate)
    declared = _complete(candidate)
    model = payload.runtime_root(candidate) / "tessdata" / "eng.traineddata"
    model.write_bytes(b"replaced-eng")
    for entry in declared:
        if entry["role"] == "model:eng":
            entry["sha256"] = hashlib.sha256(b"replaced-eng").hexdigest()
            entry["size"] = len(b"replaced-eng")
    _write_manifest(candidate, declared)
    replaced = installation_catalogue(installation)
    assert replaced is not None

    with replaced.frozen_resources(
        operation_id="capture-pin",
        admitted=admitted,
        requested=DEFAULT_SETTINGS.languages,
    ) as resources:
        assert not resources.available
        assert resources.reason == REASON_UNVERIFIED
        assert resources.unavailable_models == ("eng",)
    # And with no payload at all the answer is the same condition, not a pause.
    (candidate / payload.MANIFEST_NAME).unlink()
    gone = installation_catalogue(installation)
    assert gone is not None
    try:
        with gone.frozen_resources(
            operation_id="capture-pin",
            admitted=admitted,
            requested=DEFAULT_SETTINGS.languages,
        ) as resources:
            assert not resources.available
            assert resources.reason == REASON_UNVERIFIED
    except CaptureNotExecutable as pause:  # pragma: no cover - the defect this guards
        raise AssertionError("an absent payload paused instead of settling") from pause


def test_a_packaged_admission_resolves_to_the_profile_it_executes_under(
    tmp_path: Path,
) -> None:
    """The digest the row records is the digest the execution recomputes.

    `_admitted_digest_holds` pauses an obligation whose frozen decision this
    build resolves to a different profile than its admission recorded, and the
    profile is the input to the operation key. So a bundled selection whose
    admission-time digest and execution-time digest disagreed would pause every
    default-English capture for ever, silently. Both are computed here from the
    same row: admission over the frozen identities alone, execution over the
    resources the payload actually served.
    """
    from omnivia_core_runtime.service.document_processing import (
        APPROVED_PROFILE_REVISION,
        DEFAULT_PROFILE_REF,
        admitted_profile_digest,
        portable_profile,
        resolve_settings,
    )
    from omnivia_core_runtime.service.processing_settings import (
        DEFAULT_SETTINGS,
        admitted_resources,
        installation_catalogue,
        managed_resources,
        packaged_identities,
        packaged_runtime,
    )
    from omnivia_core_runtime.storage import document_intake as store

    installation = _installed(tmp_path)
    authority = installation_catalogue(installation)
    assert authority is not None
    admitted = admitted_resources(
        managed_resources(authority),
        pin="capture-pin",
        languages=DEFAULT_SETTINGS.languages,
        packaged=packaged_identities(packaged_runtime(authority)),
    )

    recorded = admitted_profile_digest(
        media_type="image/png",
        profile_ref=DEFAULT_PROFILE_REF,
        revision=APPROVED_PROFILE_REVISION,
        settings=DEFAULT_SETTINGS,
        admitted=admitted,
    )
    with authority.frozen_resources(
        operation_id="capture-pin",
        admitted=admitted,
        requested=DEFAULT_SETTINGS.languages,
    ) as resources:
        executed = store.profile_digest(
            portable_profile(
                media_type="image/png",
                profile_ref=DEFAULT_PROFILE_REF,
                revision=APPROVED_PROFILE_REVISION,
                settings=resolve_settings(
                    media_type="image/png",
                    profile_ref=DEFAULT_PROFILE_REF,
                    settings=DEFAULT_SETTINGS,
                    resources=resources,
                    runtime=packaged_runtime(authority),
                ),
            )
        )

    assert executed == recorded
