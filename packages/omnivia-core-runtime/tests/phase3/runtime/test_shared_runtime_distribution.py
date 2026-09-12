from __future__ import annotations

import json
import os
import shutil
import threading
from pathlib import Path

import pytest
from _signed_payload import NOW, OTHER_KEY_ID, anchor, write_payload
from omnivia_core_runtime.distribution import shared_runtime
from omnivia_core_runtime.distribution.shared_runtime import (
    COMPANION_BUNDLE_ID,
    CandidateRecord,
    DistributionError,
    SharedRuntimeInstallation,
    canonical_macos_paths,
)
from omnivia_core_runtime.distribution.trusted_runtime import (
    RUNTIME_MANIFEST_NAME,
    RuntimeRefusal,
    RuntimeResolutionError,
    resolve_runtime,
    unharden_tree,
)

CORE_CONSUMER = "c" * 64
PLATFORM_CONSUMER = "d" * 64

POSIX_ONLY = pytest.mark.skipif(os.name == "nt", reason="POSIX mode and owner policy")


def _payload(
    root: Path, name: str, *, release_version: str = "0.6.5", **kwargs: object
) -> Path:
    """One signed source payload, as a release would hand it to the installer."""
    payload = root / name
    write_payload(payload, release_version=release_version, marker=name, **kwargs)  # type: ignore[arg-type]
    return payload


def _installation(tmp_path: Path) -> SharedRuntimeInstallation:
    return SharedRuntimeInstallation(tmp_path / "Core")


def _install(
    installation: SharedRuntimeInstallation, source: Path, **guards: object
) -> CandidateRecord:
    return installation.install_candidate(
        source,
        trust_anchors=[anchor()],
        verification_time=NOW,
        **guards,  # type: ignore[arg-type]
    )


def _installed_payloads(installation: SharedRuntimeInstallation) -> list[str]:
    """The candidate payload directories actually on disk, by identity."""
    return sorted(
        path.name
        for release in installation.runtimes.iterdir()
        if release.is_dir()
        for path in release.iterdir()
        if path.is_dir()
    )


def _staging_trees(installation: SharedRuntimeInstallation) -> list[str]:
    """Leftover staging directories.

    The empty `runtimes/<version>/` a failed install leaves behind is deliberately
    not one: removing it would race a concurrent install publishing a sibling
    payload into the same version, and an empty directory selects nothing.
    """
    return sorted(path.name for path in installation.runtimes.glob(".staging.*"))


@pytest.fixture(autouse=True)
def _release_hardened_trees(tmp_path: Path) -> object:
    """Give the hardened payloads their write bits back so the tree can be removed."""
    yield
    unharden_tree(tmp_path)


def test_canonical_paths_and_bundle_identity_are_frozen() -> None:
    core, companion = canonical_macos_paths(Path("/Users/founder"))
    assert core == Path("/Users/founder/Library/Application Support/OmniVia/Core")
    assert companion == Path("/Users/founder/Applications/OmniVia Core.app")
    assert COMPANION_BUNDLE_ID == "com.omnivia.core.status"


def test_core_only_platform_only_and_both_installed_converge(tmp_path: Path) -> None:
    installation = _installation(tmp_path)
    installed = _install(installation, _payload(tmp_path, "candidate-a"))

    selected = installation.register_consumer(
        consumer_id="standalone-core",
        consumer_payload_digest=CORE_CONSUMER,
        minimum_core_version="0.6.5",
    )
    assert selected.payload_digest == installed.payload_digest
    installation.unregister_consumer("standalone-core")
    assert installation.active() == selected

    selected = installation.register_consumer(
        consumer_id="com.omnivia.platform",
        consumer_payload_digest=PLATFORM_CONSUMER,
        minimum_core_version="0.6.5",
    )
    assert selected.payload_digest == installed.payload_digest
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


def test_selection_is_highest_compatible_and_tracks_previous_known_good(
    tmp_path: Path,
) -> None:
    installation = _installation(tmp_path)
    first = _install(installation, _payload(tmp_path, "candidate-a"))
    installation.register_consumer(
        consumer_id="standalone-core",
        consumer_payload_digest=CORE_CONSUMER,
        minimum_core_version="0.6.5",
    )
    second = _install(
        installation, _payload(tmp_path, "candidate-b", release_version="0.6.6")
    )
    assert installation.reconcile() == second
    assert installation.active() == second
    assert installation.previous_known_good() == first


def test_uninstalling_one_consumer_never_breaks_the_other(tmp_path: Path) -> None:
    installation = _installation(tmp_path)
    selected = _install(installation, _payload(tmp_path, "candidate-a"))
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
    _install(installation, _payload(tmp_path, "candidate-a"))
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
    candidate = _install(installation, _payload(tmp_path, "candidate-a"))
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
        _install(installation, payload)

    clean = _payload(tmp_path, "candidate-b")
    record = _install(installation, clean)
    index = installation.candidates / f"{record.payload_digest}.json"
    document = json.loads(index.read_text(encoding="utf-8"))
    document["relative_path"] = "../../outside"
    index.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(DistributionError, match="candidate refused"):
        installation.register_consumer(
            consumer_id="standalone-core",
            consumer_payload_digest=CORE_CONSUMER,
            minimum_core_version="0.6.5",
        )


def test_garbage_collection_is_only_a_report_and_protects_active_previous(
    tmp_path: Path,
) -> None:
    installation = _installation(tmp_path)
    first = _install(installation, _payload(tmp_path, "candidate-a"))
    second = _install(
        installation, _payload(tmp_path, "candidate-b", release_version="0.6.6")
    )
    installation.register_consumer(
        consumer_id="standalone-core",
        consumer_payload_digest=CORE_CONSUMER,
        minimum_core_version="0.6.5",
    )
    assert installation.active() == second
    assert installation.garbage_collectable_candidates() == (first,)
    assert (installation.root / first.relative_path).is_dir()


# --------------------------------------------------------------------------
# Installation computes the identity it publishes
# --------------------------------------------------------------------------


def test_the_installed_identity_is_computed_from_the_signed_manifest(
    tmp_path: Path,
) -> None:
    """The directory name is derived, not supplied. That inversion is the change.

    The previous signature took a `payload_digest` and placed the tree at
    `runtimes/<version>/<that digest>/` without ever computing it, so an installer
    that lied produced a candidate whose name said one thing and whose contents were
    another -- and every later consumer inherited the claim.
    """
    installation = _installation(tmp_path)
    source = _payload(tmp_path, "candidate-a")
    expected = json.loads((source / RUNTIME_MANIFEST_NAME).read_text(encoding="utf-8"))

    record = _install(installation, source)
    assert record.payload_digest == expected["payload_identity"].removeprefix("sha256:")
    assert record.release_version == expected["release_version"]
    assert record.relative_path == f"runtimes/0.6.5/{record.payload_digest}"
    assert (installation.root / record.relative_path / RUNTIME_MANIFEST_NAME).is_file()


def test_a_legacy_digest_guard_is_an_equality_check_and_never_the_identity(
    tmp_path: Path,
) -> None:
    installation = _installation(tmp_path)
    source = _payload(tmp_path, "candidate-a")

    with pytest.raises(DistributionError, match="candidate refused"):
        _install(installation, source, payload_digest="a" * 64)
    with pytest.raises(DistributionError, match="candidate refused"):
        _install(installation, source, release_version="9.9.9")
    assert list(installation.candidates.glob("*.json")) == []

    computed = _install(installation, source)
    # Passed correctly, the guard is simply satisfied.
    assert (
        _install(installation, source, payload_digest=computed.payload_digest)
        == computed
    )
    assert _install(installation, source, release_version="0.6.5") == computed


def test_an_unsigned_or_tampered_payload_is_refused_and_leaves_nothing_behind(
    tmp_path: Path,
) -> None:
    installation = _installation(tmp_path)
    installation.initialise()

    unsigned = _payload(tmp_path, "unsigned")
    (unsigned / RUNTIME_MANIFEST_NAME).unlink()
    with pytest.raises(RuntimeResolutionError) as raised:
        _install(installation, unsigned)
    assert raised.value.refusal is RuntimeRefusal.METADATA_INVALID

    foreign = _payload(tmp_path, "foreign", key_id=OTHER_KEY_ID)
    with pytest.raises(RuntimeResolutionError) as untrusted:
        _install(installation, foreign)
    assert untrusted.value.refusal is RuntimeRefusal.UNTRUSTED

    tampered = _payload(tmp_path, "tampered")
    member = tampered / "lib" / "omnivia" / "release.txt"
    member.write_bytes(member.read_bytes().replace(b"payload", b"paylOad"))
    with pytest.raises(RuntimeResolutionError) as changed:
        _install(installation, tampered)
    assert changed.value.refusal is RuntimeRefusal.TAMPERED

    # No index, no candidate directory and no staging tree.
    assert list(installation.candidates.iterdir()) == []
    assert _installed_payloads(installation) == []
    assert _staging_trees(installation) == []


@POSIX_ONLY
def test_a_published_candidate_is_hardened_before_its_index_appears(
    tmp_path: Path,
) -> None:
    """Hardening finishes before the candidate index is published.

    The destination can exist without an index while hardening finishes, but selection
    cannot name it in that state. Resolution also rechecks the mode policy, so a
    writable candidate is never returned as trusted or launched.
    """
    installation = _installation(tmp_path)
    source = _payload(tmp_path, "candidate-a")
    for path in source.rglob("*"):
        if path.is_file():
            path.chmod(0o755)

    record = _install(installation, source)
    published = installation.root / record.relative_path
    assert (installation.candidates / f"{record.payload_digest}.json").is_file()
    assert published.stat().st_mode & 0o222 == 0
    for path in published.rglob("*"):
        assert path.lstat().st_mode & 0o222 == 0, path
    assert (published / "lib" / "omnivia" / "release.txt").stat().st_mode & 0o111 == 0
    assert (published / "bin" / "omnivia-core-service").stat().st_mode & 0o100


def test_a_failed_publication_publishes_no_index_and_removes_its_staging_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A move failure leaves only this call's staging tree to clean up.

    Nothing reached a content-derived destination and no candidate index was
    published, so the failed call leaves no installation state behind.
    """
    installation = _installation(tmp_path)
    source = _payload(tmp_path, "candidate-a")

    def _explode(*args: object, **kwargs: object) -> None:
        raise OSError("no space left on device")

    monkeypatch.setattr(shared_runtime, "_rename_no_replace", _explode)
    with pytest.raises(OSError, match="no space left"):
        _install(installation, source)

    assert list(installation.candidates.iterdir()) == []
    assert _installed_payloads(installation) == []
    assert _staging_trees(installation) == []


def test_reinstalling_the_same_payload_is_idempotent_and_leaves_no_staging(
    tmp_path: Path,
) -> None:
    installation = _installation(tmp_path)
    source = _payload(tmp_path, "candidate-a")
    first = _install(installation, source)
    second = _install(installation, source)
    assert first == second
    assert _installed_payloads(installation) == [first.payload_digest]
    assert _staging_trees(installation) == []


def test_a_payload_directory_without_its_index_is_repaired_rather_than_bricked(
    tmp_path: Path,
) -> None:
    """The state a crash between the move and the index write leaves behind.

    It used to be terminal: the conflict branch keyed on the *directory* existing and
    then read the index, so every later install of that identity answered
    "distribution record refused" and no shipped operation could clear it. The
    payload is verified against its own signed manifest on the way through, so
    adopting it is not trusting anything unverified -- the missing record is simply
    written.
    """
    installation = _installation(tmp_path)
    source = _payload(tmp_path, "candidate-a")
    record = _install(installation, source)
    index = installation.candidates / f"{record.payload_digest}.json"
    index.unlink()

    assert _install(installation, source) == record
    assert index.is_file()
    assert _installed_payloads(installation) == [record.payload_digest]
    assert _staging_trees(installation) == []


def test_a_conflicting_payload_never_overwrites_the_installed_one(
    tmp_path: Path,
) -> None:
    """Same identity, different bytes is impossible; same *directory*, different
    record is what a conflict actually looks like, and it is refused."""
    installation = _installation(tmp_path)
    source = _payload(tmp_path, "candidate-a")
    record = _install(installation, source)

    index = installation.candidates / f"{record.payload_digest}.json"
    document = json.loads(index.read_text(encoding="utf-8"))
    document["release_version"] = "0.6.6"
    document["relative_path"] = f"runtimes/0.6.6/{record.payload_digest}"
    index.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(DistributionError):
        _install(installation, source)
    assert (installation.root / record.relative_path / RUNTIME_MANIFEST_NAME).is_file()


def test_a_corrupt_orphan_payload_directory_is_never_adopted_or_overwritten(
    tmp_path: Path,
) -> None:
    """A content-derived directory name is a claim about bytes, never proof of them.

    An unindexed destination -- what a crash between the move and the index write
    leaves, and the window a converging installer sits in -- used to be adopted on
    `destination.is_dir()` alone, hardened, and then published under this call's
    record. Whatever had rotted inside it silently became the installed payload.

    It is verified against the identity and release this call computed now, and a
    tree that fails is left exactly where it is: overwriting it destroys the only
    evidence of what happened, and deleting it races whichever installer may be
    publishing it.
    """
    installation = _installation(tmp_path)
    source = _payload(tmp_path, "candidate-a")
    record = _install(installation, source)
    index = installation.candidates / f"{record.payload_digest}.json"
    index.unlink()

    member = (
        installation.root / record.relative_path / "lib" / "omnivia" / "release.txt"
    )
    member.chmod(0o600)
    member.write_bytes(b"not the signed bytes\n")

    with pytest.raises(DistributionError, match="candidate conflict"):
        _install(installation, source)

    assert not index.exists()
    assert member.read_bytes() == b"not the signed bytes\n"
    assert _installed_payloads(installation) == [record.payload_digest]
    assert _staging_trees(installation) == []


def test_an_empty_directory_at_the_destination_is_refused_and_left_in_place(
    tmp_path: Path,
) -> None:
    """The move was the third way something existing got overwritten.

    Adoption was reached only from a *failed* replacing rename, and on POSIX renaming a
    directory onto an **empty** one does not fail: `rename(2)` removes it. So an
    empty directory at the content-derived name -- an interrupted removal, a restored
    backup -- was silently deleted and taken over on POSIX, while the identical call
    refused on Windows. The atomic no-clobber move now refuses that occupant without
    a check-then-rename window, and an empty directory has no manifest to verify, so
    it remains a conflict: refused and still there afterwards.
    """
    installation = _installation(tmp_path)
    installation.initialise()
    source = _payload(tmp_path, "candidate-a")
    manifest = json.loads((source / RUNTIME_MANIFEST_NAME).read_text(encoding="utf-8"))
    destination = (
        installation.runtimes
        / manifest["release_version"]
        / manifest["payload_identity"].removeprefix("sha256:")
    )
    destination.mkdir(parents=True)

    with pytest.raises(DistributionError, match="candidate conflict"):
        _install(installation, source)

    assert destination.is_dir()
    assert list(destination.iterdir()) == []
    assert list(installation.candidates.iterdir()) == []
    assert _staging_trees(installation) == []


def test_a_published_candidate_whose_bytes_rotted_is_refused_rather_than_reused(
    tmp_path: Path,
) -> None:
    """The indexed half of the same defect.

    `index.exists()` returned the stored record after `destination.is_dir()` and an
    index comparison, so reinstalling over a payload whose bytes had been rewritten
    answered "already installed" and the caller went on to select it.
    """
    installation = _installation(tmp_path)
    source = _payload(tmp_path, "candidate-a")
    record = _install(installation, source)

    member = (
        installation.root / record.relative_path / "lib" / "omnivia" / "release.txt"
    )
    member.chmod(0o600)
    member.write_bytes(b"not the signed bytes\n")

    with pytest.raises(DistributionError, match="candidate conflict"):
        _install(installation, source)
    assert member.read_bytes() == b"not the signed bytes\n"
    assert _staging_trees(installation) == []


def test_a_publication_failure_never_deletes_a_tree_another_installer_published(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The moved tree is not this call's to delete, and cleanup used to delete it.

    The old `finally` removed the destination whenever the rename had succeeded and
    the index write had not, reasoning that nothing could be reading a tree no index
    named. **The index is shared.** A second installer of this same identity
    converges on exactly that tree and publishes it, so the cleanup removed a payload
    another process had already told its consumers about and left a record naming
    nothing. What survives a local failure now is an unindexed -- or, as here,
    someone else's indexed -- verified tree, which the next run reconciles.

    Deterministic rather than threaded: the second installer runs *inside* the first
    one's hardening step, which is precisely the window, and the first then fails.
    """
    installation = _installation(tmp_path)
    source = _payload(tmp_path, "candidate-a")
    real_harden = shared_runtime.harden_payload
    reentered: list[bool] = []
    converged: list[CandidateRecord] = []

    def _lose_the_race(root: Path, inventory: object) -> None:
        real_harden(root, inventory)  # type: ignore[arg-type]
        if reentered:
            return
        reentered.append(True)
        converged.append(_install(installation, source))
        raise OSError("no space left on device")

    monkeypatch.setattr(shared_runtime, "harden_payload", _lose_the_race)
    with pytest.raises(OSError, match="no space left"):
        _install(installation, source)

    record = converged[0]
    assert (installation.candidates / f"{record.payload_digest}.json").is_file()
    assert _installed_payloads(installation) == [record.payload_digest]
    assert (installation.root / record.relative_path / RUNTIME_MANIFEST_NAME).is_file()
    assert _staging_trees(installation) == []
    # And what survived is the real payload: a later install adopts rather than
    # rebuilds it, which is only sound because the adoption is a full verification.
    assert _install(installation, source) == record


# --------------------------------------------------------------------------
# Installation and resolution meet
# --------------------------------------------------------------------------


def test_the_active_selection_resolves_to_the_pair_it_installed(tmp_path: Path) -> None:
    installation = _installation(tmp_path)
    record = _install(installation, _payload(tmp_path, "candidate-a"))
    installation.register_consumer(
        consumer_id="com.omnivia.platform",
        consumer_payload_digest=PLATFORM_CONSUMER,
        minimum_core_version="0.6.5",
    )

    verified = resolve_runtime(
        installation_root=installation.root,
        trust_anchors=[anchor()],
        verification_time=NOW,
    )
    assert verified.payload_identity == f"sha256:{record.payload_digest}"
    assert verified.runtime_root == installation.root / record.relative_path
    assert verified.cli_path.is_file() and verified.service_path.is_file()
    assert verified.cli_path.parent == verified.service_path.parent


def test_resolution_refuses_after_the_installed_pair_is_edited(tmp_path: Path) -> None:
    """Revalidate at use: the installed tree was authentic when it was published."""
    installation = _installation(tmp_path)
    record = _install(installation, _payload(tmp_path, "candidate-a"))
    installation.register_consumer(
        consumer_id="com.omnivia.platform",
        consumer_payload_digest=PLATFORM_CONSUMER,
        minimum_core_version="0.6.5",
    )
    service = installation.root / record.relative_path / "bin" / "omnivia-core-service"
    service.chmod(0o700)
    service.write_bytes(
        service.read_bytes().replace(b"omnivia-core-service", b"0mnivia-core-svc")
    )

    with pytest.raises(RuntimeResolutionError) as raised:
        resolve_runtime(
            installation_root=installation.root,
            trust_anchors=[anchor()],
            verification_time=NOW,
        )
    assert raised.value.refusal in {
        RuntimeRefusal.TAMPERED,
        RuntimeRefusal.LAYOUT_INVALID,
    }


def test_an_active_record_naming_a_removed_candidate_is_a_bounded_retry(
    tmp_path: Path,
) -> None:
    installation = _installation(tmp_path)
    record = _install(installation, _payload(tmp_path, "candidate-a"))
    installation.register_consumer(
        consumer_id="com.omnivia.platform",
        consumer_payload_digest=PLATFORM_CONSUMER,
        minimum_core_version="0.6.5",
    )
    published = installation.root / record.relative_path
    unharden_tree(published)
    shutil.rmtree(published)

    with pytest.raises(RuntimeResolutionError) as raised:
        resolve_runtime(
            installation_root=installation.root,
            trust_anchors=[anchor()],
            verification_time=NOW,
        )
    assert raised.value.refusal is RuntimeRefusal.BUSY


def test_no_active_record_is_not_installed_rather_than_untrusted(
    tmp_path: Path,
) -> None:
    installation = _installation(tmp_path)
    _install(installation, _payload(tmp_path, "candidate-a"))
    with pytest.raises(RuntimeResolutionError) as raised:
        resolve_runtime(
            installation_root=installation.root,
            trust_anchors=[anchor()],
            verification_time=NOW,
        )
    assert raised.value.refusal is RuntimeRefusal.NOT_INSTALLED


def test_concurrent_installations_converge_and_never_publish_a_half_written_candidate(
    tmp_path: Path,
) -> None:
    """Four installers, one payload, launched together.

    The candidate directory is named for a digest of its own contents, so
    concurrent installs of one payload are installing identical bytes and there is
    nothing to arbitrate -- but only one atomic no-clobber move can win, and before this the
    losers raised a bare `OSError` from a race they could not have avoided.

    What is asserted is the invariant rather than the mechanism: every caller comes
    back with the same record, exactly one payload directory exists, no staging tree
    is left, and what was published verifies completely. A half-written candidate
    would fail the last of those.
    """
    installation = _installation(tmp_path)
    installation.initialise()
    source = _payload(tmp_path, "candidate-a")

    results: list[CandidateRecord] = []
    failures: list[BaseException] = []
    barrier = threading.Barrier(4)

    def install() -> None:
        try:
            barrier.wait(timeout=30)
            results.append(_install(installation, source))
        except BaseException as failure:  # noqa: BLE001 - reported, not swallowed
            failures.append(failure)

    threads = [threading.Thread(target=install) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert not failures, failures
    assert len({record.payload_digest for record in results}) == 1
    assert len(results) == 4
    assert _installed_payloads(installation) == [results[0].payload_digest]
    assert _staging_trees(installation) == []

    installation.register_consumer(
        consumer_id="standalone-core",
        consumer_payload_digest=CORE_CONSUMER,
        minimum_core_version="0.6.5",
    )
    verified = resolve_runtime(
        installation_root=installation.root,
        trust_anchors=[anchor()],
        verification_time=NOW,
    )
    assert verified.payload_identity == f"sha256:{results[0].payload_digest}"
