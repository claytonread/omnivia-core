"""`omnivia-core-service --ocr-resources`: the owner-local OCR resource surface.

The owner-local half of §14.5's language-resource flow, exercised through the
packaged entry point rather than the lifecycle object, because the routing, the
output contract and the refusal classification are what the native settings
surface will consume.

What is proved here: that the action vocabulary is closed, that the mode is
mutually exclusive with the other maintenance modes, that the versioned result
document is the whole of stdout and renders identically for the same cache, that
every lifecycle refusal arrives with its stable code and exit 1 while a usage
fault exits 2 having written no document at all, and that the managed cache is
installation-local -- this mode owns no workspace and starts nothing.

And the boundary that matters most: these commands are **not** an operation.
Nothing here is in the frozen catalogue, on the CLI application surface or in any
MCP exposure manifest, so there is no name a model can call. That is asserted
rather than asserted-in-prose, in the same shape
``test_legacy_engineering_import`` asserts it for ``--import-legacy``.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from omnivia_core_cli.surface import APPLICATION_COMMANDS
from omnivia_core_mcp.manifest import PROFILES, exposure_manifest
from omnivia_core_runtime.service import main as main_module
from omnivia_core_runtime.service.main import (
    _OCR_ACTION_OPTIONS,
    OCR_RESOURCE_ACTIONS,
    OCR_RESOURCE_DOCUMENT_VERSION,
    build_parser,
    main,
)
from omnivia_core_runtime.service.ocr_resource_lifecycle import (
    MODEL_SUFFIX,
    OCR_RESOURCES_DIR,
    REFUSAL_BUSY,
    REFUSAL_CAPACITY,
    REFUSAL_NOT_INSTALLED,
    REFUSAL_PAYLOAD,
    REFUSAL_PINNED,
    REFUSAL_VERIFICATION,
    ManagedOcrResources,
)
from omnivia_core_runtime.service.ocr_resource_store import (
    OcrResourcePin,
    OcrResourceSetIdentity,
    OcrResourceStoreState,
)

from omnivia_core.contracts.v1 import OPERATION_CATALOGUE

BODY = b"trained-model-bytes" * 64
DIGEST = hashlib.sha256(BODY).hexdigest()


class _Admin:
    """One installation state root and the entry point that administers it."""

    def __init__(self, tmp_path: Path) -> None:
        self.workspace = tmp_path / "ws"
        self.installation = tmp_path / "inst"
        self.installation.mkdir(parents=True)
        self.downloads = tmp_path / "downloads"
        self.downloads.mkdir(parents=True)

    @property
    def managed_root(self) -> Path:
        return self.installation / OCR_RESOURCES_DIR

    def managed(self) -> ManagedOcrResources:
        return ManagedOcrResources(self.managed_root)

    def payload(self, body: bytes = BODY, name: str = "payload.bin") -> Path:
        path = self.downloads / name
        path.write_bytes(body)
        return path

    def run(
        self, capsys: pytest.CaptureFixture[str], *arguments: str
    ) -> tuple[int, str, str]:
        code = main(
            [
                "--workspace",
                str(self.workspace),
                "--installation-state",
                str(self.installation),
                *arguments,
            ]
        )
        captured = capsys.readouterr()
        return code, captured.out, captured.err

    def install(
        self,
        capsys: pytest.CaptureFixture[str],
        payload: Path,
        *,
        language: str = "eng",
        byte_size: int = len(BODY),
        sha256: str = DIGEST,
        model_version: str = "tessdata_fast-4.1.0",
    ) -> tuple[int, str, str]:
        return self.run(
            capsys,
            "--ocr-resources",
            "install",
            "--ocr-payload",
            str(payload),
            *_identity_arguments(
                language=language,
                byte_size=byte_size,
                sha256=sha256,
                model_version=model_version,
            ),
        )


def _identity_arguments(
    *,
    language: str = "eng",
    engine_version: str = "5.5.3",
    model_version: str = "tessdata_fast-4.1.0",
    byte_size: int = len(BODY),
    sha256: str = DIGEST,
) -> list[str]:
    return [
        "--ocr-language",
        language,
        "--ocr-engine-version",
        engine_version,
        "--ocr-model-version",
        model_version,
        "--ocr-byte-size",
        str(byte_size),
        "--ocr-sha256",
        sha256,
    ]


def _identity(**overrides: object) -> OcrResourceSetIdentity:
    fields: dict[str, object] = {
        "language": "eng",
        "engine_version": "5.5.3",
        "model_version": "tessdata_fast-4.1.0",
        "byte_size": len(BODY),
        "sha256": DIGEST,
    }
    fields.update(overrides)
    return OcrResourceSetIdentity(**fields)  # type: ignore[arg-type]


# --- the output contract


def test_status_reports_an_empty_cache(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    admin = _Admin(tmp_path)
    code, out, err = admin.run(capsys, "--ocr-resources", "status")
    assert code == 0
    assert err == ""
    document = json.loads(out)
    assert document == {
        "ocr_resource_result_version": OCR_RESOURCE_DOCUMENT_VERSION,
        "action": "status",
        "status": "accepted",
        "capacityBytes": 536_870_912,
        "installedBytes": 0,
        "availableBytes": 536_870_912,
        "resources": [],
    }


def test_the_result_document_is_the_whole_of_stdout(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    admin = _Admin(tmp_path)
    _, out, _ = admin.run(capsys, "--ocr-resources", "status")
    assert out.endswith("\n")
    assert out.count("\n") == 1
    json.loads(out)


def test_the_same_cache_renders_identically_twice(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    admin = _Admin(tmp_path)
    admin.install(capsys, admin.payload())
    _, first, _ = admin.run(capsys, "--ocr-resources", "status")
    _, second, _ = admin.run(capsys, "--ocr-resources", "status")
    assert first == second


def test_the_document_keys_are_sorted(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    admin = _Admin(tmp_path)
    _, out, _ = admin.run(capsys, "--ocr-resources", "status")
    keys = list(json.loads(out))
    assert keys == sorted(keys)


# --- install, status, remove


def test_install_verifies_and_publishes_the_payload(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    admin = _Admin(tmp_path)
    code, out, err = admin.install(capsys, admin.payload())
    assert code == 0
    assert err == ""
    document = json.loads(out)
    assert document["status"] == "accepted"
    assert document["newlyInstalled"] is True
    assert document["installedBytes"] == len(BODY)
    assert document["availableBytes"] == 536_870_912 - len(BODY)
    assert document["identity"] == {
        "language": "eng",
        "engineVersion": "5.5.3",
        "modelVersion": "tessdata_fast-4.1.0",
        "byteSize": len(BODY),
        "sha256": DIGEST,
    }
    assert admin.managed().payload_path(_identity()).read_bytes() == BODY


def test_repeating_an_install_reports_that_nothing_changed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    admin = _Admin(tmp_path)
    admin.install(capsys, admin.payload())
    code, out, _ = admin.install(capsys, admin.payload())
    assert code == 0
    assert json.loads(out)["newlyInstalled"] is False


def test_status_lists_the_installed_resource_with_its_pins(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    admin = _Admin(tmp_path)
    admin.install(capsys, admin.payload())
    managed = admin.managed()
    managed.store.save(
        OcrResourceStoreState(
            entries=(_identity(),), pins=(OcrResourcePin("op-4", _identity()),)
        )
    )
    code, out, _ = admin.run(capsys, "--ocr-resources", "status")
    assert code == 0
    resources = json.loads(out)["resources"]
    assert resources == [
        {
            "identity": {
                "language": "eng",
                "engineVersion": "5.5.3",
                "modelVersion": "tessdata_fast-4.1.0",
                "byteSize": len(BODY),
                "sha256": DIGEST,
            },
            "pinned": True,
            "pinnedBy": ["op-4"],
            "bytesPresent": True,
        }
    ]


def test_remove_removes_the_exact_identity(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    admin = _Admin(tmp_path)
    admin.install(capsys, admin.payload())
    code, out, err = admin.run(
        capsys, "--ocr-resources", "remove", *_identity_arguments()
    )
    assert code == 0
    assert err == ""
    document = json.loads(out)
    assert document["removed"] is True
    assert document["installedBytes"] == 0
    assert not admin.managed().resource_directory(_identity()).exists()


def test_reconcile_reports_its_repairs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    admin = _Admin(tmp_path)
    admin.install(capsys, admin.payload())
    managed = admin.managed()
    abandoned = managed.staging_root / "fedcba9876543210"
    abandoned.mkdir(parents=True, exist_ok=True)
    (abandoned / "half.bin").write_bytes(b"half")
    code, out, err = admin.run(capsys, "--ocr-resources", "reconcile")
    assert code == 0
    assert err == ""
    document = json.loads(out)
    assert document["discardedStaging"] == ["fedcba9876543210"]
    assert document["changed"] is True
    assert document["installedBytes"] == len(BODY)


def test_reconcile_reports_a_quarantined_entry(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    admin = _Admin(tmp_path)
    admin.install(capsys, admin.payload())
    admin.managed().payload_path(_identity()).write_bytes(b"t" * len(BODY))
    code, out, _ = admin.run(capsys, "--ocr-resources", "reconcile")
    assert code == 0
    document = json.loads(out)
    assert document["quarantined"] == [
        {
            "language": "eng",
            "engineVersion": "5.5.3",
            "modelVersion": "tessdata_fast-4.1.0",
            "byteSize": len(BODY),
            "sha256": DIGEST,
        }
    ]
    assert document["installedBytes"] == 0


# --- refusal classification, exit 1


def test_a_digest_mismatch_is_classified_and_reported(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    admin = _Admin(tmp_path)
    code, out, err = admin.install(capsys, admin.payload(), sha256="b" * 64)
    assert code == 1
    document = json.loads(out)
    assert document["status"] == "refused"
    assert document["code"] == REFUSAL_VERIFICATION
    assert document["action"] == "install"
    assert err.startswith(f"{REFUSAL_VERIFICATION}: ")


def test_a_size_mismatch_is_classified(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    admin = _Admin(tmp_path)
    code, out, _ = admin.install(capsys, admin.payload(), byte_size=len(BODY) + 1)
    assert code == 1
    assert json.loads(out)["code"] == REFUSAL_VERIFICATION


def test_a_full_cache_refuses_with_language_storage_full(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    admin = _Admin(tmp_path)
    bulk = _identity(language="deu", byte_size=536_870_912, sha256="e" * 64)
    admin.managed().store.save(OcrResourceStoreState(entries=(bulk,)))
    code, out, err = admin.install(capsys, admin.payload())
    assert code == 1
    assert json.loads(out)["code"] == REFUSAL_CAPACITY
    assert REFUSAL_CAPACITY in err
    assert not admin.managed().resource_directory(_identity()).exists()


def test_a_pinned_resource_refuses_removal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    admin = _Admin(tmp_path)
    admin.install(capsys, admin.payload())
    admin.managed().store.save(
        OcrResourceStoreState(
            entries=(_identity(),), pins=(OcrResourcePin("op-2", _identity()),)
        )
    )
    code, out, err = admin.run(
        capsys, "--ocr-resources", "remove", *_identity_arguments()
    )
    assert code == 1
    assert json.loads(out)["code"] == REFUSAL_PINNED
    assert "op-2" in err
    assert admin.managed().payload_path(_identity()).read_bytes() == BODY


def test_removing_an_unknown_identity_is_classified(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    admin = _Admin(tmp_path)
    code, out, _ = admin.run(
        capsys, "--ocr-resources", "remove", *_identity_arguments(language="deu")
    )
    assert code == 1
    assert json.loads(out)["code"] == REFUSAL_NOT_INSTALLED


def test_a_relative_payload_is_classified(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    admin = _Admin(tmp_path)
    code, out, _ = admin.run(
        capsys,
        "--ocr-resources",
        "install",
        "--ocr-payload",
        "payload.bin",
        *_identity_arguments(),
    )
    assert code == 1
    assert json.loads(out)["code"] == REFUSAL_PAYLOAD


# --- usage faults, exit 2 and no document


@pytest.mark.parametrize("action", ["install", "remove"])
def test_a_partial_identity_is_a_usage_fault(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], action: str
) -> None:
    admin = _Admin(tmp_path)
    code, out, err = admin.run(
        capsys, "--ocr-resources", action, "--ocr-language", "eng"
    )
    assert code == 2
    assert out == ""
    assert "--ocr-engine-version" in err
    assert "--ocr-sha256" in err


def test_install_without_a_payload_is_a_usage_fault(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    admin = _Admin(tmp_path)
    code, out, err = admin.run(
        capsys, "--ocr-resources", "install", *_identity_arguments()
    )
    assert code == 2
    assert out == ""
    assert "--ocr-payload" in err


def test_a_malformed_digest_is_a_usage_fault(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    admin = _Admin(tmp_path)
    code, out, err = admin.install(capsys, admin.payload(), sha256="NOT-A-DIGEST")
    assert code == 2
    assert out == ""
    assert "sha256" in err


def test_a_malformed_language_is_a_usage_fault(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    admin = _Admin(tmp_path)
    code, out, _ = admin.install(capsys, admin.payload(), language="../eng")
    assert code == 2
    assert out == ""


def test_a_relative_installation_state_is_a_usage_fault(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = main(
        [
            "--workspace",
            "ws",
            "--installation-state",
            "inst",
            "--ocr-resources",
            "status",
        ]
    )
    captured = capsys.readouterr()
    assert code == 2
    assert captured.out == ""
    assert "absolute" in captured.err


def test_an_unknown_action_is_an_argparse_usage_error(tmp_path: Path) -> None:
    admin = _Admin(tmp_path)
    with pytest.raises(SystemExit) as exit_code:
        main(
            [
                "--workspace",
                str(admin.workspace),
                "--installation-state",
                str(admin.installation),
                "--ocr-resources",
                "evict",
            ]
        )
    assert exit_code.value.code == 2


def test_choosing_two_maintenance_modes_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    admin = _Admin(tmp_path)
    code, out, err = admin.run(
        capsys,
        "--ocr-resources",
        "status",
        "--import-legacy",
        str(admin.payload()),
    )
    assert code == 2
    assert out == ""
    assert "--ocr-resources" in err
    assert "--import-legacy" in err


# --- routing and ownership


def test_the_action_vocabulary_is_closed() -> None:
    assert OCR_RESOURCE_ACTIONS == ("status", "install", "remove", "reconcile")
    assert "evict" not in OCR_RESOURCE_ACTIONS


def test_the_mode_is_documented_in_the_parser_help() -> None:
    help_text = build_parser().format_help()
    for flag in (
        "--ocr-resources",
        "--ocr-payload",
        "--ocr-language",
        "--ocr-engine-version",
        "--ocr-model-version",
        "--ocr-byte-size",
        "--ocr-sha256",
    ):
        assert flag in help_text, flag


def test_the_managed_cache_is_installation_local(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    admin = _Admin(tmp_path)
    admin.install(capsys, admin.payload())
    assert admin.managed_root.is_dir()
    assert admin.managed_root.parent == admin.installation


def test_the_mode_owns_no_workspace_and_starts_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    admin = _Admin(tmp_path)
    code, _, _ = admin.install(capsys, admin.payload())
    assert code == 0
    assert not admin.workspace.exists()


# --- the boundary: administration is not an operation


def test_the_ocr_administration_is_not_an_operation_on_any_surface() -> None:
    surfaces = {
        "catalogue": {entry.name for entry in OPERATION_CATALOGUE},
        "mcp": {
            exposed.operation
            for profile in PROFILES
            for exposed in exposure_manifest(profile)
        },
        "cli": {command.operation for command in APPLICATION_COMMANDS},
    }
    for surface, operations in surfaces.items():
        assert not [name for name in operations if "ocr" in name.lower()], surface
    assert "--ocr-resources" in build_parser().format_help()


def test_no_mcp_tool_can_name_the_resource_administration() -> None:
    for profile in PROFILES:
        for exposed in exposure_manifest(profile):
            assert "ocr" not in exposed.tool_name.lower()
            assert "resource" not in exposed.tool_name.lower()


# --- options the chosen action does not read


def test_every_action_declares_which_options_it_reads() -> None:
    """Closed on both sides: no action without a rule, no rule without an action.

    A new action added without declaring its options would otherwise inherit no
    entry at all and fail with a `KeyError` on first use, and an option admitted
    for an action that no longer exists would never be refused for any.
    """
    assert set(_OCR_ACTION_OPTIONS) == set(OCR_RESOURCE_ACTIONS)
    assert _OCR_ACTION_OPTIONS["status"] == ()
    assert _OCR_ACTION_OPTIONS["reconcile"] == ()
    assert "ocr_payload" not in _OCR_ACTION_OPTIONS["remove"]
    assert "ocr_payload" in _OCR_ACTION_OPTIONS["install"]


@pytest.mark.parametrize("action", ["status", "reconcile"])
def test_an_identity_handed_to_an_inventory_action_is_a_usage_fault(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], action: str
) -> None:
    """`status` and `reconcile` act on the whole cache, never on one identity.

    Silently ignoring the identity was the defect: a caller asking for the status
    *of one resource set* received the status of every one of them and no sign
    that its request had not been understood, and the same five flags handed to
    `reconcile` looked like a scoped repair.
    """
    admin = _Admin(tmp_path)
    code, out, err = admin.run(
        capsys, "--ocr-resources", action, *_identity_arguments()
    )
    assert code == 2
    assert out == ""
    assert f"--ocr-resources {action}" in err
    assert "--ocr-language" in err
    assert "--ocr-sha256" in err
    # Nothing was read, written or created in the meantime.
    assert not admin.managed_root.exists()


@pytest.mark.parametrize("action", ["status", "reconcile", "remove"])
def test_a_payload_handed_to_an_action_that_reads_none_is_a_usage_fault(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], action: str
) -> None:
    """Only `install` reads a payload. A removal that accepted one would look as
    though the named file had been consulted before the bytes went."""
    admin = _Admin(tmp_path)
    identity = _identity_arguments() if action == "remove" else []
    code, out, err = admin.run(
        capsys,
        "--ocr-resources",
        action,
        "--ocr-payload",
        str(admin.payload()),
        *identity,
    )
    assert code == 2
    assert out == ""
    assert "--ocr-payload" in err


def test_a_single_stray_identity_field_is_enough_to_refuse(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """One flag, not only the full five: a partial one is just as ignored."""
    admin = _Admin(tmp_path)
    code, _, err = admin.run(
        capsys, "--ocr-resources", "status", "--ocr-byte-size", "17"
    )
    assert code == 2
    assert "--ocr-byte-size" in err
    assert "--ocr-language" not in err, "only what was actually supplied is named"


def test_the_admitted_options_are_still_accepted(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The guard refuses the irrelevant without narrowing the relevant."""
    admin = _Admin(tmp_path)
    code, _, err = admin.install(capsys, admin.payload())
    assert code == 0, err
    code, _, err = admin.run(
        capsys, "--ocr-resources", "remove", *_identity_arguments()
    )
    assert code == 0, err
    for action in ("status", "reconcile"):
        code, _, err = admin.run(capsys, "--ocr-resources", action)
        assert code == 0, err


# --- one administrator at a time


def test_a_concurrently_held_cache_is_refused_with_its_code(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second administrator is told the cache is busy, not left waiting.

    The surface's contract for it is the ordinary one: the versioned document on
    stdout carries the lifecycle's stable code, so the native settings surface
    branches on `ocr_resource_busy` rather than on a sentence, and exit 1 marks it
    a refusal rather than a usage fault.

    The wait is shortened through the constructor the mode uses, because the
    default is a thirty-second bound and this test is about the classification
    rather than about the length of the bound.
    """
    admin = _Admin(tmp_path)
    monkeypatch.setattr(
        main_module,
        "ManagedOcrResources",
        lambda root: ManagedOcrResources(root, lock_wait_seconds=0.2),
    )
    holder = ManagedOcrResources(admin.managed_root)
    with holder.coordinated():
        code, out, err = admin.run(capsys, "--ocr-resources", "status")

    assert code == 1
    document = json.loads(out)
    assert document["status"] == "refused"
    assert document["code"] == REFUSAL_BUSY
    assert REFUSAL_BUSY in err


def test_an_inventory_action_answers_an_absent_cache_without_reading_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`status` and `reconcile` render an absent cache from the decision itself.

    Neither may establish the cache, so neither takes the coordination lock -- and
    therefore neither may read the root afterwards either, because an install
    arriving in that instant would be rendered halfway into existence. The whole
    document comes from the empty inventory and a pass that repaired nothing, and
    nothing under the managed root is created, lock file included.
    """
    admin = _Admin(tmp_path)
    documents: dict[str, dict[str, Any]] = {}
    for action in ("status", "reconcile"):
        code, out, err = admin.run(capsys, "--ocr-resources", action)
        assert code == 0, err
        assert not admin.managed_root.exists(), action
        documents[action] = json.loads(out)

    for action, document in documents.items():
        assert document["action"] == action
        assert document["status"] == "accepted"
        assert document["resources"] == []
        assert document["installedBytes"] == 0
        assert document["availableBytes"] == document["capacityBytes"]
    assert documents["reconcile"]["changed"] is False
    assert documents["reconcile"]["discardedStaging"] == []
    assert documents["reconcile"]["discardedUnreferenced"] == []
    assert documents["reconcile"]["quarantined"] == []
    assert documents["reconcile"]["unrecoverablePinned"] == []


# --- the one literal shared with the selection path


def test_the_model_suffix_matches_the_selection_path() -> None:
    """The published name has to be the name the engine's model directory uses."""
    from omnivia_core_runtime.service import ocr_resources

    assert MODEL_SUFFIX == ocr_resources._MODEL_SUFFIX
