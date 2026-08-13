from __future__ import annotations

import json
from pathlib import Path

import pytest
from omnivia_core_runtime.distribution.shared_runtime import (
    COMPANION_BUNDLE_ID,
    DistributionError,
    SharedRuntimeInstallation,
    canonical_macos_paths,
)

DIGEST_A = "a" * 64
DIGEST_B = "b" * 64
CORE_CONSUMER = "c" * 64
PLATFORM_CONSUMER = "d" * 64


def _payload(root: Path, name: str) -> Path:
    payload = root / name
    payload.mkdir()
    (payload / "omnivia").write_text(name, encoding="utf-8")
    return payload


def _installation(tmp_path: Path) -> SharedRuntimeInstallation:
    return SharedRuntimeInstallation(tmp_path / "Core")


def test_canonical_paths_and_bundle_identity_are_frozen() -> None:
    core, companion = canonical_macos_paths(Path("/Users/founder"))
    assert core == Path("/Users/founder/Library/Application Support/OmniVia/Core")
    assert companion == Path("/Users/founder/Applications/OmniVia Core.app")
    assert COMPANION_BUNDLE_ID == "com.omnivia.core.status"


def test_core_only_platform_only_and_both_installed_converge(tmp_path: Path) -> None:
    installation = _installation(tmp_path)
    installation.install_candidate(
        _payload(tmp_path, "candidate-a"), release_version="0.6.5", payload_digest=DIGEST_A
    )

    selected = installation.register_consumer(
        consumer_id="standalone-core",
        consumer_payload_digest=CORE_CONSUMER,
        minimum_core_version="0.6.5",
    )
    assert selected.payload_digest == DIGEST_A
    installation.unregister_consumer("standalone-core")
    assert installation.active() == selected

    selected = installation.register_consumer(
        consumer_id="com.omnivia.platform",
        consumer_payload_digest=PLATFORM_CONSUMER,
        minimum_core_version="0.6.5",
    )
    assert selected.payload_digest == DIGEST_A
    installation.register_consumer(
        consumer_id="standalone-core",
        consumer_payload_digest=CORE_CONSUMER,
        minimum_core_version="0.6.5",
    )
    assert {receipt.consumer_id for receipt in installation.list_receipts()} == {
        "standalone-core",
        "com.omnivia.platform",
    }
    assert installation.active() == selected


def test_selection_is_highest_compatible_and_tracks_previous_known_good(tmp_path: Path) -> None:
    installation = _installation(tmp_path)
    first = installation.install_candidate(
        _payload(tmp_path, "candidate-a"), release_version="0.6.5", payload_digest=DIGEST_A
    )
    installation.register_consumer(
        consumer_id="standalone-core",
        consumer_payload_digest=CORE_CONSUMER,
        minimum_core_version="0.6.5",
    )
    second = installation.install_candidate(
        _payload(tmp_path, "candidate-b"), release_version="0.6.6", payload_digest=DIGEST_B
    )
    assert installation.reconcile() == second
    assert installation.active() == second
    assert installation.previous_known_good() == first


def test_uninstalling_one_consumer_never_breaks_the_other(tmp_path: Path) -> None:
    installation = _installation(tmp_path)
    selected = installation.install_candidate(
        _payload(tmp_path, "candidate-a"), release_version="0.6.5", payload_digest=DIGEST_A
    )
    for consumer, digest in (
        ("standalone-core", CORE_CONSUMER),
        ("com.omnivia.platform", PLATFORM_CONSUMER),
    ):
        installation.register_consumer(
            consumer_id=consumer,
            consumer_payload_digest=digest,
            minimum_core_version="0.6.5",
        )
    assert installation.unregister_consumer("com.omnivia.platform") == selected
    assert [receipt.consumer_id for receipt in installation.list_receipts()] == [
        "standalone-core"
    ]
    assert installation.active() == selected
    assert (installation.root / selected.relative_path).is_dir()


def test_incompatible_registration_is_refused_without_a_receipt(tmp_path: Path) -> None:
    installation = _installation(tmp_path)
    installation.install_candidate(
        _payload(tmp_path, "candidate-a"), release_version="0.6.5", payload_digest=DIGEST_A
    )
    with pytest.raises(DistributionError, match="no compatible shared runtime"):
        installation.register_consumer(
            consumer_id="com.omnivia.platform",
            consumer_payload_digest=PLATFORM_CONSUMER,
            minimum_core_version="0.7.0",
        )
    assert installation.list_receipts() == []
    assert installation.active() is None


def test_reconcile_repairs_an_interrupted_active_selection(tmp_path: Path) -> None:
    installation = _installation(tmp_path)
    candidate = installation.install_candidate(
        _payload(tmp_path, "candidate-a"), release_version="0.6.5", payload_digest=DIGEST_A
    )
    installation.initialise()
    receipt = {
        "consumer_id": "standalone-core",
        "consumer_payload_digest": CORE_CONSUMER,
        "minimum_core_version": "0.6.5",
        "schema_version": 1,
    }
    (installation.receipts / "standalone-core.json").write_text(
        json.dumps(receipt), encoding="utf-8"
    )
    assert installation.active() is None
    assert installation.reconcile() == candidate
    assert installation.active() == candidate


def test_payload_symlinks_and_record_tampering_fail_closed(tmp_path: Path) -> None:
    installation = _installation(tmp_path)
    payload = _payload(tmp_path, "candidate-a")
    (payload / "escape").symlink_to(tmp_path / "outside")
    with pytest.raises(DistributionError, match="payload refused"):
        installation.install_candidate(
            payload, release_version="0.6.5", payload_digest=DIGEST_A
        )

    clean = _payload(tmp_path, "candidate-b")
    installation.install_candidate(clean, release_version="0.6.5", payload_digest=DIGEST_A)
    index = installation.candidates / f"{DIGEST_A}.json"
    document = json.loads(index.read_text(encoding="utf-8"))
    document["relative_path"] = "../../outside"
    index.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(DistributionError, match="candidate refused"):
        installation.register_consumer(
            consumer_id="standalone-core",
            consumer_payload_digest=CORE_CONSUMER,
            minimum_core_version="0.6.5",
        )


def test_garbage_collection_is_only_a_report_and_protects_active_previous(tmp_path: Path) -> None:
    installation = _installation(tmp_path)
    first = installation.install_candidate(
        _payload(tmp_path, "candidate-a"), release_version="0.6.5", payload_digest=DIGEST_A
    )
    second = installation.install_candidate(
        _payload(tmp_path, "candidate-b"), release_version="0.6.6", payload_digest=DIGEST_B
    )
    installation.register_consumer(
        consumer_id="standalone-core",
        consumer_payload_digest=CORE_CONSUMER,
        minimum_core_version="0.6.5",
    )
    assert installation.active() == second
    assert installation.garbage_collectable_candidates() == (first,)
    assert (installation.root / first.relative_path).is_dir()
