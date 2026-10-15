"""The managed OCR resource lifecycle: install, remove and reconcile.

The properties proved are the ones a later change could silently break, and they
are the design's rather than the implementation's.

For verification (§18.4, DOC-13): that a payload becomes usable only after its
length *and* digest equal the declared identity exactly, that a payload longer
than its declaration is refused while it is still being read rather than after it
has filled staging, and that a refusal leaves no resource, no staging and no
change to the state document.

For capacity (§17.3, END-02): that the cap is exactly 512 MiB, that installed
bytes are accounted exactly, and that installing up to the cap is accepted.

For eviction (§18.4, END-02): that an installation which does not fit reclaims
*any* verified unpinned unleased set rather than only the revisions it
supersedes; that the victims are the shortest prefix of the cache's own
stable-key order, so the same cache always evicts the same sets whatever order
it was built in and never one set more than the shortfall needed; that a pinned
set, a leased set and a set whose bytes no longer match are each left alone and
that the last of those is not counted as room either; that a shortfall no prefix
covers defers the installation with the bytes it needed and the bytes it could
actually have had, deleting nothing; that a payload which fails verification and
a state write which fails leave every chosen victim installed with its bytes;
and that repeating an install of something already installed reclaims nothing.

For pins (§18.4, END-02): that a pinned identity cannot be removed and is never
dropped by reconciliation even when its bytes have gone, and that a pin on one
identity does not protect another.

For selection leases (§18.4, END-02): that a selection held for an extraction's
length cannot be removed, is not dropped by reconciliation and is discarded by
neither sweep nor the pruning bound; that the hold lives on disk rather than in
the object that took it, so another process sees it; that two holds of one
selection are two leases; that a hold nobody holds any more is discarded rather
than left blocking removal for good; and that a refused resolution takes no hold.

And that lease *discovery* fails closed: a ``leases/`` that cannot be enumerated,
a held projection that cannot be inspected and a payload that cannot be probed
each refuse removal and reconciliation with nothing mutated, rather than reading
as "nothing is held" -- while the pruning bound, which runs after a resolution
has already succeeded, preserves every projection instead of failing it.

For the path boundary (§15.1): that a relative, traversing, symlinked,
non-regular or inside-the-cache payload is refused, and that no field of an
identity can reach the filesystem as anything but a fixed-width hexadecimal name.

For crash recovery (§18.4): that bytes are published before the state document
records them, so an interrupted install leaves unreferenced bytes rather than an
entry claiming bytes that never arrived; that an interrupted removal leaves bytes
rather than a lying entry; that reconciliation sweeps abandoned staging and
unreferenced directories, quarantines an entry whose bytes went missing or
stopped matching rather than continuing to count it installed, and leaves a
healthy cache byte-for-byte alone.

And throughout: that a state document from a newer build is refused by every
operation and left exactly as it stands, because an older build must not
reconcile away what a newer one recorded.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
from itertools import permutations
from pathlib import Path

import pytest
from omnivia_core_runtime.service import ocr_resource_lifecycle as lifecycle_module
from omnivia_core_runtime.service.ocr_resource_lifecycle import (
    MANAGED_LEASES_DIRNAME,
    MANAGED_QUARANTINE_DIRNAME,
    MANAGED_RESOURCES_DIRNAME,
    MANAGED_SELECTIONS_DIRNAME,
    MANAGED_STAGING_DIRNAME,
    MAX_PROJECTED_MODELS,
    MAX_PUBLISHED_SELECTIONS,
    MODEL_SUFFIX,
    OCR_RESOURCES_DIR,
    REFUSAL_CAPACITY,
    REFUSAL_DIVERGENT,
    REFUSAL_NOT_INSTALLED,
    REFUSAL_PAYLOAD,
    REFUSAL_PINNED,
    REFUSAL_UNAVAILABLE,
    REFUSAL_UNPINNED,
    REFUSAL_VERIFICATION,
    ManagedOcrResources,
    OcrResourceLifecycleRefused,
    directory_name,
)
from omnivia_core_runtime.service.ocr_resource_store import (
    DEFAULT_OCR_RESOURCE_CAPACITY_BYTES,
    OcrResourcePin,
    OcrResourceSetIdentity,
    OcrResourceStoreError,
    OcrResourceStoreState,
    OcrResourceStoreVersionUnsupported,
)

BODY = b"trained-model-bytes" * 64
OTHER_BODY = b"another-model-entirely" * 64
#: A third installed resource, for the assertions that need every identity in a
#: claim to be genuinely installed so the only thing wrong is the claim.
THIRD_BODY = b"a-third-model-again" * 64


def _digest(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _identity(body: bytes = BODY, **overrides: object) -> OcrResourceSetIdentity:
    fields: dict[str, object] = {
        "language": "eng",
        "engine_version": "5.5.3",
        "model_version": "tessdata_fast-4.1.0",
        "byte_size": len(body),
        "sha256": _digest(body),
    }
    fields.update(overrides)
    return OcrResourceSetIdentity(**fields)  # type: ignore[arg-type]


def _managed(tmp_path: Path, **overrides: object) -> ManagedOcrResources:
    return ManagedOcrResources(tmp_path / "managed", **overrides)  # type: ignore[arg-type]


def _payload(tmp_path: Path, body: bytes = BODY, name: str = "payload.bin") -> Path:
    path = tmp_path / "downloads" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return path


def _installed(tmp_path: Path, **overrides: object) -> tuple[ManagedOcrResources, OcrResourceSetIdentity]:
    """One managed cache with exactly one verified resource installed."""
    managed = _managed(tmp_path, **overrides)
    identity = _identity()
    managed.install(_payload(tmp_path), identity)
    return managed, identity


# --- install


def test_install_publishes_the_declared_bytes_under_the_identity(tmp_path: Path) -> None:
    managed, identity = _installed(tmp_path)
    published = managed.payload_path(identity)
    assert published.read_bytes() == BODY
    assert published.is_file() and not published.is_symlink()
    assert published.parent == managed.resource_directory(identity)


def test_the_published_file_is_named_for_the_recognition_language(tmp_path: Path) -> None:
    managed, identity = _installed(tmp_path)
    assert managed.payload_path(identity).name == f"eng{MODEL_SUFFIX}"


def test_install_reports_the_outcome_and_the_remaining_capacity(tmp_path: Path) -> None:
    managed = _managed(tmp_path)
    identity = _identity()
    outcome = managed.install(_payload(tmp_path), identity)
    assert outcome.newly_installed is True
    assert outcome.identity == identity
    assert outcome.installed_bytes == len(BODY)
    assert outcome.available_bytes == DEFAULT_OCR_RESOURCE_CAPACITY_BYTES - len(BODY)
    assert outcome.payload_path == managed.payload_path(identity)


def test_install_records_the_identity_in_the_state_document(tmp_path: Path) -> None:
    managed, identity = _installed(tmp_path)
    assert managed.store.load().entries == (identity,)


def test_install_does_not_copy_the_payload_away(tmp_path: Path) -> None:
    managed = _managed(tmp_path)
    payload = _payload(tmp_path)
    managed.install(payload, _identity())
    assert payload.read_bytes() == BODY


def test_install_leaves_no_staging_behind(tmp_path: Path) -> None:
    managed, _ = _installed(tmp_path)
    assert list(managed.staging_root.iterdir()) == []


def test_the_managed_tree_sits_under_the_one_root(tmp_path: Path) -> None:
    managed = _managed(tmp_path)
    assert managed.resources_root == managed.root / MANAGED_RESOURCES_DIRNAME
    assert managed.staging_root == managed.root / MANAGED_STAGING_DIRNAME
    assert managed.quarantine_root == managed.root / MANAGED_QUARANTINE_DIRNAME
    assert managed.store.root == managed.root


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_the_managed_tree_and_the_payload_are_owner_only(tmp_path: Path) -> None:
    managed, identity = _installed(tmp_path)
    for directory in (managed.root, managed.resources_root, managed.resource_directory(identity)):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700, directory
    assert stat.S_IMODE(managed.payload_path(identity).stat().st_mode) == 0o600


def test_two_model_revisions_of_one_language_coexist(tmp_path: Path) -> None:
    managed = _managed(tmp_path)
    first = _identity()
    second = _identity(OTHER_BODY, model_version="tessdata_fast-4.2.0")
    managed.install(_payload(tmp_path, BODY, "first.bin"), first)
    managed.install(_payload(tmp_path, OTHER_BODY, "second.bin"), second)
    assert managed.payload_path(first).read_bytes() == BODY
    assert managed.payload_path(second).read_bytes() == OTHER_BODY
    assert set(managed.store.load().entries) == {first, second}


def test_a_second_language_does_not_disturb_the_first(tmp_path: Path) -> None:
    managed = _managed(tmp_path)
    first = _identity()
    second = _identity(OTHER_BODY, language="deu")
    managed.install(_payload(tmp_path, BODY, "a.bin"), first)
    managed.install(_payload(tmp_path, OTHER_BODY, "b.bin"), second)
    assert managed.payload_path(first).read_bytes() == BODY
    assert managed.payload_path(second).name == f"deu{MODEL_SUFFIX}"


def test_install_refuses_something_that_is_not_an_identity(tmp_path: Path) -> None:
    managed = _managed(tmp_path)
    with pytest.raises(OcrResourceStoreError):
        managed.install(_payload(tmp_path), "eng")  # type: ignore[arg-type]


# --- idempotency


def test_repeating_an_install_publishes_nothing_and_changes_no_state(tmp_path: Path) -> None:
    managed, identity = _installed(tmp_path)
    before = managed.store.state_path.read_bytes()
    outcome = managed.install(_payload(tmp_path), identity)
    assert outcome.newly_installed is False
    assert outcome.installed_bytes == len(BODY)
    assert managed.store.state_path.read_bytes() == before
    assert managed.payload_path(identity).read_bytes() == BODY


def test_repeating_an_install_counts_the_resource_once(tmp_path: Path) -> None:
    managed, identity = _installed(tmp_path)
    managed.install(_payload(tmp_path), identity)
    assert managed.inventory().installed_bytes == len(BODY)
    assert len(managed.inventory().resources) == 1


def test_an_entry_whose_bytes_are_gone_refuses_rather_than_republishing(
    tmp_path: Path,
) -> None:
    managed, identity = _installed(tmp_path)
    managed.payload_path(identity).unlink()
    with pytest.raises(OcrResourceLifecycleRefused) as refused:
        managed.install(_payload(tmp_path), identity)
    assert refused.value.code == REFUSAL_DIVERGENT
    assert managed.store.load().entries == (identity,)


# --- verification


def test_a_wrong_digest_is_refused(tmp_path: Path) -> None:
    managed = _managed(tmp_path)
    with pytest.raises(OcrResourceLifecycleRefused) as refused:
        managed.install(_payload(tmp_path), _identity(sha256="b" * 64))
    assert refused.value.code == REFUSAL_VERIFICATION


def test_a_payload_shorter_than_its_declaration_is_refused(tmp_path: Path) -> None:
    managed = _managed(tmp_path)
    with pytest.raises(OcrResourceLifecycleRefused) as refused:
        managed.install(_payload(tmp_path), _identity(byte_size=len(BODY) + 1))
    assert refused.value.code == REFUSAL_VERIFICATION
    assert str(len(BODY)) in str(refused.value)


def test_a_payload_longer_than_its_declaration_is_refused(tmp_path: Path) -> None:
    managed = _managed(tmp_path)
    with pytest.raises(OcrResourceLifecycleRefused) as refused:
        managed.install(_payload(tmp_path), _identity(byte_size=len(BODY) - 1))
    assert refused.value.code == REFUSAL_VERIFICATION
    assert "larger than the declared" in str(refused.value)


def test_an_oversized_payload_is_refused_without_staging_all_of_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The declared size bounds the copy, so the read stops at the bound."""
    monkeypatch.setattr(lifecycle_module, "_COPY_BLOCK", 8)
    body = b"z" * 4096
    managed = _managed(tmp_path)
    with pytest.raises(OcrResourceLifecycleRefused) as refused:
        managed.install(_payload(tmp_path, body), _identity(body, byte_size=16))
    assert refused.value.code == REFUSAL_VERIFICATION
    assert list(managed.staging_root.iterdir()) == []


def test_a_refused_payload_leaves_no_resource_and_no_staging(tmp_path: Path) -> None:
    managed = _managed(tmp_path)
    identity = _identity(sha256="c" * 64)
    with pytest.raises(OcrResourceLifecycleRefused):
        managed.install(_payload(tmp_path), identity)
    assert not managed.resource_directory(identity).exists()
    assert list(managed.staging_root.iterdir()) == []
    assert managed.store.load().entries == ()


def test_a_refused_install_preserves_an_earlier_resource(tmp_path: Path) -> None:
    managed, installed = _installed(tmp_path)
    before = managed.store.state_path.read_bytes()
    with pytest.raises(OcrResourceLifecycleRefused):
        managed.install(
            _payload(tmp_path, OTHER_BODY, "bad.bin"),
            _identity(OTHER_BODY, language="deu", sha256="d" * 64),
        )
    assert managed.store.state_path.read_bytes() == before
    assert managed.payload_path(installed).read_bytes() == BODY


def test_the_same_language_is_never_served_from_another_versions_bytes(
    tmp_path: Path,
) -> None:
    """No substitution: a mismatched payload is refused, not quietly accepted."""
    managed, _ = _installed(tmp_path)
    wanted = _identity(OTHER_BODY, model_version="tessdata_fast-4.2.0")
    with pytest.raises(OcrResourceLifecycleRefused) as refused:
        managed.install(_payload(tmp_path, BODY, "wrong.bin"), wanted)
    assert refused.value.code == REFUSAL_VERIFICATION
    assert not managed.resource_directory(wanted).exists()


# --- capacity


def test_the_capacity_is_the_designs_512_mib(tmp_path: Path) -> None:
    assert DEFAULT_OCR_RESOURCE_CAPACITY_BYTES == 536_870_912
    assert _managed(tmp_path).capacity_bytes == 536_870_912


def test_installed_bytes_are_accounted_exactly(tmp_path: Path) -> None:
    managed = _managed(tmp_path)
    managed.install(_payload(tmp_path, BODY, "a.bin"), _identity())
    managed.install(
        _payload(tmp_path, OTHER_BODY, "b.bin"), _identity(OTHER_BODY, language="deu")
    )
    inventory = managed.inventory()
    assert inventory.installed_bytes == len(BODY) + len(OTHER_BODY)
    assert inventory.available_bytes == 536_870_912 - len(BODY) - len(OTHER_BODY)


def test_installing_exactly_up_to_the_cap_is_accepted(tmp_path: Path) -> None:
    """The real 512 MiB cap, reached arithmetically rather than with real bytes."""
    managed = _managed(tmp_path)
    body = b"q" * 10
    bulk = _identity(language="deu", byte_size=536_870_912 - 10, sha256="e" * 64)
    managed.store.save(OcrResourceStoreState(entries=(bulk,)))
    outcome = managed.install(_payload(tmp_path, body), _identity(body))
    assert outcome.newly_installed is True
    assert outcome.installed_bytes == 536_870_912
    assert outcome.available_bytes == 0


def test_one_byte_past_the_cap_is_refused_with_the_capacity_it_needed(
    tmp_path: Path,
) -> None:
    managed = _managed(tmp_path)
    body = b"q" * 11
    bulk = _identity(language="deu", byte_size=536_870_912 - 10, sha256="e" * 64)
    managed.store.save(OcrResourceStoreState(entries=(bulk,)))
    with pytest.raises(OcrResourceLifecycleRefused) as refused:
        managed.install(_payload(tmp_path, body), _identity(body))
    assert refused.value.code == REFUSAL_CAPACITY
    assert refused.value.required_bytes == 11
    assert refused.value.available_bytes == 10


def test_a_capacity_refusal_evicts_nothing(tmp_path: Path) -> None:
    """A deferral deletes nothing, even from an eligible candidate.

    The installed set is verified, unpinned and unleased, so it is a legitimate
    victim -- it is simply not a big enough one. An installation that cannot be
    made to fit leaves the cache exactly as it found it rather than reclaiming
    what it could and failing anyway.
    """
    managed, installed = _installed(tmp_path, capacity_bytes=len(BODY))
    before = managed.store.state_path.read_bytes()
    with pytest.raises(OcrResourceLifecycleRefused) as refused:
        managed.install(
            _payload(tmp_path, OTHER_BODY, "b.bin"),
            _identity(OTHER_BODY, language="deu"),
        )
    assert refused.value.code == REFUSAL_CAPACITY
    assert managed.store.state_path.read_bytes() == before
    assert managed.payload_path(installed).read_bytes() == BODY


def test_a_capacity_refusal_copies_nothing(tmp_path: Path) -> None:
    managed = _managed(tmp_path, capacity_bytes=len(BODY) - 1)
    identity = _identity()
    with pytest.raises(OcrResourceLifecycleRefused):
        managed.install(_payload(tmp_path), identity)
    assert not managed.staging_root.exists()
    assert not managed.resource_directory(identity).exists()


def test_removing_a_resource_frees_its_capacity(tmp_path: Path) -> None:
    managed, identity = _installed(tmp_path, capacity_bytes=len(BODY))
    managed.remove(identity)
    other = _identity(OTHER_BODY, language="deu")
    managed = ManagedOcrResources(managed.root, capacity_bytes=len(OTHER_BODY))
    managed.install(_payload(tmp_path, OTHER_BODY, "b.bin"), other)
    assert managed.inventory().installed_bytes == len(OTHER_BODY)


@pytest.mark.parametrize("capacity", [0, -1, True, "512", 1.5, None])
def test_a_malformed_capacity_is_refused(tmp_path: Path, capacity: object) -> None:
    with pytest.raises(OcrResourceStoreError):
        _managed(tmp_path, capacity_bytes=capacity)


# --- eviction


def _body(marker: str, size: int) -> bytes:
    """Exactly `size` bytes, distinct for every marker these tests use."""
    return (marker.encode("ascii") * size)[:size]


def _filled(
    tmp_path: Path, capacity: int, sizes: dict[str, int]
) -> tuple[ManagedOcrResources, dict[str, OcrResourceSetIdentity]]:
    """A cache of `capacity` bytes holding one verified set per named language.

    The languages are what the stable key sorts by -- it begins with the language
    token -- so a test names its expected victims by choosing their letters.
    """
    managed = _managed(tmp_path, capacity_bytes=capacity)
    installed: dict[str, OcrResourceSetIdentity] = {}
    for language, size in sizes.items():
        body = _body(language, size)
        identity = _identity(body, language=language)
        managed.install(_payload(tmp_path, body, f"{language}.bin"), identity)
        installed[language] = identity
    return managed, installed


def test_an_install_evicts_another_languages_resource_to_fit(tmp_path: Path) -> None:
    """§18.4's test is *verified and unpinned*, not *the same language*.

    Nothing about `deu` supersedes it or is superseded by `eng`; it is simply the
    unheld room the cache has. Reclaiming only the revisions an installation
    supersedes would defer this install with a cache full of resources nothing
    was using.
    """
    managed, installed = _filled(tmp_path, 400, {"deu": 300})
    body = _body("eng", 300)
    arriving = _identity(body, language="eng")

    outcome = managed.install(_payload(tmp_path, body, "arriving.bin"), arriving)

    assert outcome.newly_installed is True
    assert outcome.evicted == (installed["deu"],)
    assert managed.store.load().entries == (arriving,)
    assert managed.payload_path(arriving).read_bytes() == body
    assert not managed.resource_directory(installed["deu"]).exists()
    assert outcome.installed_bytes == 300
    assert outcome.available_bytes == 100


def test_an_install_takes_every_victim_the_shortfall_needs(tmp_path: Path) -> None:
    """More than one, when one is not enough -- and no more than that."""
    managed, installed = _filled(tmp_path, 400, {"ara": 100, "deu": 100, "eng": 100})
    body = _body("spa", 250)
    arriving = _identity(body, language="spa")

    outcome = managed.install(_payload(tmp_path, body, "spa.bin"), arriving)

    assert outcome.evicted == (installed["ara"], installed["deu"])
    assert managed.store.load().entries == (installed["eng"], arriving)
    assert managed.payload_path(installed["eng"]).read_bytes() == _body("eng", 100)
    assert outcome.installed_bytes == 350
    assert outcome.available_bytes == 50


def test_the_victims_are_the_shortest_prefix_of_the_caches_own_order(
    tmp_path: Path,
) -> None:
    """Stable-key order, taken until the shortfall is covered. Nothing cleverer.

    `spa` alone would cover it in a single victim and leave more room, which is
    what a size-aware policy would choose; there is no size-aware policy, because
    the design names no preference and the state document records nothing to
    build one from. What there is instead is an order every process reading this
    cache computes identically -- so the same cache evicts the same two sets
    whatever order an operator happened to install them in -- and a stop at the
    first candidate that covers the shortfall, so the third is never taken.
    """
    sizes = {"ara": 50, "deu": 50, "spa": 300}
    for index, order in enumerate(permutations(sizes)):
        managed, installed = _filled(
            tmp_path / f"run-{index}", 500, {name: sizes[name] for name in order}
        )
        body = _body("eng", 180)
        arriving = _identity(body, language="eng")

        outcome = managed.install(
            _payload(tmp_path, body, f"eng-{index}.bin"), arriving
        )

        assert outcome.evicted == (installed["ara"], installed["deu"]), order
        assert managed.store.load().entries == (arriving, installed["spa"]), order
        assert outcome.installed_bytes == 480


def test_a_pinned_resource_of_another_language_is_never_evicted(
    tmp_path: Path,
) -> None:
    """The pin is what makes the identity unevictable, and it is not cancelled.

    `deu` is the only candidate and the cache is exactly full without it, so the
    installation is deferred -- the cap is not exceeded to avoid that, and the
    claim the pin records is still there afterwards.
    """
    managed, installed = _filled(tmp_path, 400, {"deu": 300})
    managed.pin("op-queued", (installed["deu"],))
    body = _body("eng", 300)

    with pytest.raises(OcrResourceLifecycleRefused) as refused:
        managed.install(
            _payload(tmp_path, body, "eng.bin"), _identity(body, language="eng")
        )

    assert refused.value.code == REFUSAL_CAPACITY
    assert refused.value.required_bytes == 300
    assert refused.value.available_bytes == 100
    assert managed.payload_path(installed["deu"]).read_bytes() == _body("deu", 300)
    assert [pin.operation_id for pin in managed.store.load().pins] == ["op-queued"]


def test_a_leased_resource_is_never_evicted_to_fit_an_installation(
    tmp_path: Path,
) -> None:
    """An extraction holding these exact bytes outranks an install that wants them.

    And the hold is the whole of the obstruction: the same installation fits the
    moment the lease ends, which is what makes this a hold rather than a veto.
    """
    managed, installed = _filled(tmp_path, 400, {"deu": 300})
    body = _body("eng", 300)
    arriving = _identity(body, language="eng")

    with managed.leased_selection([installed["deu"]]) as directory:
        with pytest.raises(OcrResourceLifecycleRefused) as refused:
            managed.install(_payload(tmp_path, body, "eng.bin"), arriving)
        assert refused.value.code == REFUSAL_CAPACITY
        assert refused.value.available_bytes == 100
        assert directory.is_dir()
        assert managed.payload_path(installed["deu"]).read_bytes() == _body("deu", 300)

    outcome = managed.install(_payload(tmp_path, body, "eng.bin"), arriving)
    assert outcome.evicted == (installed["deu"],)


def test_a_divergent_resource_is_neither_evicted_nor_counted_as_room(
    tmp_path: Path,
) -> None:
    """Bytes that stopped matching their digest are evidence, not capacity.

    Deleting them would destroy exactly what reconciliation quarantines for
    inspection, and counting them as reclaimable would promise room their bytes
    cannot supply -- so the deferral reports only the free capacity.
    """
    managed, installed = _filled(tmp_path, 400, {"deu": 300})
    tampered = _body("x", 300)
    managed.payload_path(installed["deu"]).write_bytes(tampered)
    body = _body("eng", 300)

    with pytest.raises(OcrResourceLifecycleRefused) as refused:
        managed.install(
            _payload(tmp_path, body, "eng.bin"), _identity(body, language="eng")
        )

    assert refused.value.code == REFUSAL_CAPACITY
    assert refused.value.required_bytes == 300
    assert refused.value.available_bytes == 100
    assert managed.store.load().entries == (installed["deu"],)
    assert managed.payload_path(installed["deu"]).read_bytes() == tampered


def test_a_deferral_reports_the_bytes_it_could_actually_have_had(
    tmp_path: Path,
) -> None:
    """Free capacity *plus* what was verified as reclaimable, not either alone.

    An operator told "100 bytes available" would free one more resource and try
    again; the honest figure is that even reclaiming everything unheld leaves
    this installation short.
    """
    managed, installed = _filled(tmp_path, 400, {"deu": 300})
    body = _body("eng", 420)

    with pytest.raises(OcrResourceLifecycleRefused) as refused:
        managed.install(
            _payload(tmp_path, body, "eng.bin"), _identity(body, language="eng")
        )

    assert refused.value.required_bytes == 420
    assert refused.value.available_bytes == 400
    assert managed.store.load().entries == (installed["deu"],)


def test_a_payload_that_fails_verification_leaves_its_victims_installed(
    tmp_path: Path,
) -> None:
    """The victims are chosen before the copy and dropped only after it is published."""
    managed, installed = _filled(tmp_path, 400, {"deu": 300})
    body = _body("eng", 300)
    arriving = _identity(body, language="eng")
    before = managed.store.state_path.read_bytes()

    with pytest.raises(OcrResourceLifecycleRefused) as refused:
        managed.install(_payload(tmp_path, _body("z", 300), "wrong.bin"), arriving)

    assert refused.value.code == REFUSAL_VERIFICATION
    assert managed.store.state_path.read_bytes() == before
    assert managed.payload_path(installed["deu"]).read_bytes() == _body("deu", 300)
    assert not managed.resource_directory(arriving).exists()


def test_a_failed_state_write_leaves_its_victims_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One write carries both halves, so a failed one applies neither.

    The arriving publication is undone and every victim is still installed with
    its bytes -- never a cache that reclaimed the room and then lost the resource
    it reclaimed it for.
    """
    managed, installed = _filled(tmp_path, 400, {"deu": 300})
    body = _body("eng", 300)
    arriving = _identity(body, language="eng")

    def _refuse(_state: object) -> None:
        raise OcrResourceStoreError("the state document could not be written")

    monkeypatch.setattr(managed.store, "save", _refuse)
    with pytest.raises(OcrResourceStoreError):
        managed.install(_payload(tmp_path, body, "eng.bin"), arriving)
    monkeypatch.undo()

    assert managed.store.load().entries == (installed["deu"],)
    assert managed.payload_path(installed["deu"]).read_bytes() == _body("deu", 300)
    assert not managed.resource_directory(arriving).exists()
    assert managed.inventory().installed_bytes == 300


def test_repeating_an_install_of_a_full_cache_evicts_nothing(tmp_path: Path) -> None:
    """Idempotence survives the cap: the exact identity is already there."""
    managed, installed = _filled(tmp_path, 400, {"deu": 300, "eng": 100})
    before = managed.store.state_path.read_bytes()

    outcome = managed.install(
        _payload(tmp_path, _body("deu", 300), "deu-again.bin"), installed["deu"]
    )

    assert outcome.newly_installed is False
    assert outcome.evicted == ()
    assert managed.store.state_path.read_bytes() == before
    assert managed.payload_path(installed["eng"]).read_bytes() == _body("eng", 100)


# --- refused payload paths


def test_a_relative_payload_is_refused(tmp_path: Path) -> None:
    managed = _managed(tmp_path)
    with pytest.raises(OcrResourceLifecycleRefused) as refused:
        managed.install(Path("payload.bin"), _identity())
    assert refused.value.code == REFUSAL_PAYLOAD
    assert "absolute" in str(refused.value)


def test_a_traversing_payload_is_refused(tmp_path: Path) -> None:
    managed = _managed(tmp_path)
    payload = _payload(tmp_path)
    traversing = payload.parent / ".." / "downloads" / payload.name
    with pytest.raises(OcrResourceLifecycleRefused) as refused:
        managed.install(traversing, _identity())
    assert refused.value.code == REFUSAL_PAYLOAD
    assert "traversal" in str(refused.value)


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink creation")
def test_a_symlinked_payload_is_refused(tmp_path: Path) -> None:
    managed = _managed(tmp_path)
    real = _payload(tmp_path)
    link = tmp_path / "link.bin"
    link.symlink_to(real)
    with pytest.raises(OcrResourceLifecycleRefused) as refused:
        managed.install(link, _identity())
    assert refused.value.code == REFUSAL_PAYLOAD
    assert "symlink" in str(refused.value)
    assert real.read_bytes() == BODY


def test_a_directory_payload_is_refused(tmp_path: Path) -> None:
    managed = _managed(tmp_path)
    directory = tmp_path / "a-directory"
    directory.mkdir()
    with pytest.raises(OcrResourceLifecycleRefused) as refused:
        managed.install(directory, _identity())
    assert refused.value.code == REFUSAL_PAYLOAD


def test_a_missing_payload_is_refused(tmp_path: Path) -> None:
    managed = _managed(tmp_path)
    with pytest.raises(OcrResourceLifecycleRefused) as refused:
        managed.install(tmp_path / "nothing-here.bin", _identity())
    assert refused.value.code == REFUSAL_PAYLOAD


def test_a_payload_inside_the_managed_root_is_refused(tmp_path: Path) -> None:
    managed, identity = _installed(tmp_path)
    with pytest.raises(OcrResourceLifecycleRefused) as refused:
        managed.install(
            managed.payload_path(identity),
            _identity(language="deu"),
        )
    assert refused.value.code == REFUSAL_PAYLOAD
    assert "managed root" in str(refused.value)


def test_a_payload_that_is_not_a_path_is_refused(tmp_path: Path) -> None:
    managed = _managed(tmp_path)
    with pytest.raises(OcrResourceLifecycleRefused) as refused:
        managed.install("/tmp/payload.bin", _identity())  # type: ignore[arg-type]
    assert refused.value.code == REFUSAL_PAYLOAD


def test_a_relative_managed_root_is_refused() -> None:
    with pytest.raises(OcrResourceStoreError) as refused:
        ManagedOcrResources(Path("managed"))
    assert "absolute" in str(refused.value)


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink creation")
def test_a_symlinked_managed_root_is_refused(tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    link = tmp_path / "managed"
    link.symlink_to(elsewhere)
    with pytest.raises(OcrResourceStoreError) as refused:
        ManagedOcrResources(link)
    assert "symlink" in str(refused.value)


def test_no_identity_field_can_reach_the_filesystem_as_a_path(tmp_path: Path) -> None:
    managed = _managed(tmp_path)
    identity = _identity(language="chi_tra", model_version="4.1.0+rc1")
    name = managed.resource_directory(identity).name
    assert re.fullmatch(r"[0-9a-f]{64}", name)
    assert managed.resource_directory(identity).parent == managed.resources_root


def test_every_distinct_identity_gets_a_distinct_directory(tmp_path: Path) -> None:
    base = _identity()
    variants = [
        _identity(language="deu"),
        _identity(engine_version="5.5.4"),
        _identity(model_version="tessdata_fast-4.2.0"),
        _identity(byte_size=len(BODY) + 1),
        _identity(sha256="f" * 64),
    ]
    names = {directory_name(identity) for identity in (base, *variants)}
    assert len(names) == 6


# --- removal and pins


def test_removal_drops_the_entry_and_the_bytes(tmp_path: Path) -> None:
    managed, identity = _installed(tmp_path)
    managed.remove(identity)
    assert managed.store.load().entries == ()
    assert not managed.resource_directory(identity).exists()
    assert managed.inventory().installed_bytes == 0


def test_removal_keeps_every_other_resource(tmp_path: Path) -> None:
    managed = _managed(tmp_path)
    first = _identity()
    second = _identity(OTHER_BODY, language="deu")
    managed.install(_payload(tmp_path, BODY, "a.bin"), first)
    managed.install(_payload(tmp_path, OTHER_BODY, "b.bin"), second)
    managed.remove(first)
    assert managed.store.load().entries == (second,)
    assert managed.payload_path(second).read_bytes() == OTHER_BODY


def test_removing_an_unknown_identity_is_refused(tmp_path: Path) -> None:
    managed, _ = _installed(tmp_path)
    with pytest.raises(OcrResourceLifecycleRefused) as refused:
        managed.remove(_identity(language="deu"))
    assert refused.value.code == REFUSAL_NOT_INSTALLED


def test_removing_an_identity_twice_is_refused_the_second_time(tmp_path: Path) -> None:
    managed, identity = _installed(tmp_path)
    managed.remove(identity)
    with pytest.raises(OcrResourceLifecycleRefused) as refused:
        managed.remove(identity)
    assert refused.value.code == REFUSAL_NOT_INSTALLED


def test_a_pinned_resource_cannot_be_removed(tmp_path: Path) -> None:
    managed, identity = _installed(tmp_path)
    state = managed.store.load()
    managed.store.save(
        OcrResourceStoreState(
            entries=state.entries, pins=(OcrResourcePin("op-7", identity),)
        )
    )
    with pytest.raises(OcrResourceLifecycleRefused) as refused:
        managed.remove(identity)
    assert refused.value.code == REFUSAL_PINNED
    assert "op-7" in str(refused.value)
    assert managed.payload_path(identity).read_bytes() == BODY
    assert managed.store.load().entries == (identity,)


def test_a_pin_on_another_identity_does_not_block_removal(tmp_path: Path) -> None:
    managed = _managed(tmp_path)
    first = _identity()
    second = _identity(OTHER_BODY, language="deu")
    managed.install(_payload(tmp_path, BODY, "a.bin"), first)
    managed.install(_payload(tmp_path, OTHER_BODY, "b.bin"), second)
    managed.store.save(
        OcrResourceStoreState(
            entries=managed.store.load().entries,
            pins=(OcrResourcePin("op-1", second),),
        )
    )
    managed.remove(first)
    assert managed.store.load().entries == (second,)


def test_removal_refuses_something_that_is_not_an_identity(tmp_path: Path) -> None:
    managed, _ = _installed(tmp_path)
    with pytest.raises(OcrResourceStoreError):
        managed.remove("eng")  # type: ignore[arg-type]


# --- inventory determinism


def test_the_inventory_follows_the_sorted_stable_key_order(tmp_path: Path) -> None:
    managed = _managed(tmp_path)
    identities = [
        _identity(language="zul", sha256="a" * 64, byte_size=11),
        _identity(language="deu", sha256="b" * 64, byte_size=12),
        _identity(language="eng", sha256="c" * 64, byte_size=13),
    ]
    managed.store.save(OcrResourceStoreState(entries=tuple(identities)))
    reported = [resource.identity for resource in managed.inventory().resources]
    assert reported == sorted(identities, key=lambda value: value.stable_key)
    assert [resource.identity for resource in managed.inventory().resources] == reported


def test_the_inventory_reports_pins_sorted_and_deduplicated(tmp_path: Path) -> None:
    managed, identity = _installed(tmp_path)
    managed.store.save(
        OcrResourceStoreState(
            entries=(identity,),
            pins=(
                OcrResourcePin("op-c", identity),
                OcrResourcePin("op-a", identity),
                OcrResourcePin("op-b", identity),
            ),
        )
    )
    resource = managed.inventory().resources[0]
    assert resource.pinned_by == ("op-a", "op-b", "op-c")
    assert resource.pinned is True


def test_the_inventory_reports_missing_bytes_without_claiming_them(
    tmp_path: Path,
) -> None:
    managed, identity = _installed(tmp_path)
    managed.payload_path(identity).unlink()
    resource = managed.inventory().resources[0]
    assert resource.bytes_present is False


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink creation")
def test_a_symlink_standing_in_for_a_payload_is_not_present(tmp_path: Path) -> None:
    managed, identity = _installed(tmp_path)
    published = managed.payload_path(identity)
    published.unlink()
    published.symlink_to(_payload(tmp_path, BODY, "decoy.bin"))
    assert managed.inventory().resources[0].bytes_present is False


def test_the_inventory_creates_nothing(tmp_path: Path) -> None:
    managed = _managed(tmp_path)
    inventory = managed.inventory()
    assert inventory.resources == ()
    assert inventory.installed_bytes == 0
    assert not managed.root.exists()


# --- reconciliation


def test_reconcile_leaves_a_healthy_cache_byte_for_byte_alone(tmp_path: Path) -> None:
    managed, identity = _installed(tmp_path)
    before = managed.store.state_path.read_bytes()
    outcome = managed.reconcile()
    assert outcome.changed is False
    assert outcome == type(outcome)()
    assert managed.store.state_path.read_bytes() == before
    assert managed.payload_path(identity).read_bytes() == BODY


def test_reconcile_discards_abandoned_staging(tmp_path: Path) -> None:
    managed, _ = _installed(tmp_path)
    abandoned = managed.staging_root / "0123456789abcdef"
    abandoned.mkdir()
    (abandoned / f"eng{MODEL_SUFFIX}").write_bytes(b"half a payload")
    outcome = managed.reconcile()
    assert outcome.discarded_staging == ("0123456789abcdef",)
    assert not abandoned.exists()
    assert outcome.changed is True


def test_reconcile_discards_an_unreferenced_resource_directory(tmp_path: Path) -> None:
    """The crash window between publishing bytes and recording them."""
    managed, identity = _installed(tmp_path)
    orphan = _identity(OTHER_BODY, language="deu")
    orphan_directory = managed.resource_directory(orphan)
    orphan_directory.mkdir()
    (orphan_directory / f"deu{MODEL_SUFFIX}").write_bytes(OTHER_BODY)
    outcome = managed.reconcile()
    assert outcome.discarded_unreferenced == (directory_name(orphan),)
    assert not orphan_directory.exists()
    assert managed.payload_path(identity).read_bytes() == BODY


def test_an_interrupted_install_leaves_bytes_that_reconciliation_sweeps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    managed = _managed(tmp_path)
    identity = _identity()

    def _refuse(_state: object) -> None:
        raise OcrResourceStoreError("the state document could not be written")

    monkeypatch.setattr(managed.store, "save", _refuse)
    with pytest.raises(OcrResourceStoreError):
        managed.install(_payload(tmp_path), identity)
    monkeypatch.undo()
    # The publication was undone, so there is nothing usable and nothing claimed.
    assert not managed.resource_directory(identity).exists()
    assert managed.store.load().entries == ()
    assert managed.reconcile().changed is False


def test_an_interrupted_install_never_leaves_a_claimed_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bytes lead and state follows, so a crash cannot overstate what is installed."""
    managed, installed = _installed(tmp_path)
    identity = _identity(OTHER_BODY, language="deu")

    def _refuse(_state: object) -> None:
        raise OcrResourceStoreError("interrupted")

    monkeypatch.setattr(managed.store, "save", _refuse)
    with pytest.raises(OcrResourceStoreError):
        managed.install(_payload(tmp_path, OTHER_BODY, "b.bin"), identity)
    monkeypatch.undo()
    state = managed.store.load()
    assert state.entries == (installed,)
    assert managed.inventory().installed_bytes == len(BODY)


def test_an_interrupted_removal_leaves_bytes_for_reconciliation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    managed, identity = _installed(tmp_path)

    def _refuse(_path: Path) -> None:
        raise OSError("the bytes could not be removed")

    monkeypatch.setattr(lifecycle_module, "_discard", _refuse)
    managed.remove(identity)
    monkeypatch.undo()
    # The entry is gone, so nothing claims the bytes; they are merely unreferenced.
    assert managed.store.load().entries == ()
    assert managed.payload_path(identity).read_bytes() == BODY
    outcome = managed.reconcile()
    assert outcome.discarded_unreferenced == (directory_name(identity),)
    assert not managed.resource_directory(identity).exists()


def test_reconcile_quarantines_tampered_bytes_and_stops_claiming_them(
    tmp_path: Path,
) -> None:
    managed, identity = _installed(tmp_path)
    managed.payload_path(identity).write_bytes(b"t" * len(BODY))
    outcome = managed.reconcile()
    assert outcome.quarantined == (identity,)
    assert outcome.unrecoverable_pinned == ()
    assert managed.store.load().entries == ()
    assert managed.inventory().installed_bytes == 0
    assert not managed.resource_directory(identity).exists()


def test_quarantined_bytes_are_preserved_for_inspection(tmp_path: Path) -> None:
    managed, identity = _installed(tmp_path)
    managed.payload_path(identity).write_bytes(b"t" * len(BODY))
    managed.reconcile()
    quarantined = (
        managed.quarantine_root / directory_name(identity) / f"eng{MODEL_SUFFIX}"
    )
    assert quarantined.read_bytes() == b"t" * len(BODY)


def test_reconcile_quarantines_an_entry_whose_bytes_went_missing(
    tmp_path: Path,
) -> None:
    managed, identity = _installed(tmp_path)
    managed.payload_path(identity).unlink()
    outcome = managed.reconcile()
    assert outcome.quarantined == (identity,)
    assert managed.store.load().entries == ()


def test_quarantine_keeps_an_earlier_specimen(tmp_path: Path) -> None:
    managed, identity = _installed(tmp_path)
    existing = managed.quarantine_root / directory_name(identity)
    existing.mkdir(parents=True)
    (existing / "earlier.bin").write_bytes(b"the first specimen")
    managed.payload_path(identity).write_bytes(b"t" * len(BODY))
    managed.reconcile()
    assert (existing / "earlier.bin").read_bytes() == b"the first specimen"
    assert not managed.resource_directory(identity).exists()


def test_reconcile_never_drops_a_pinned_entry_and_reports_it(tmp_path: Path) -> None:
    managed, identity = _installed(tmp_path)
    managed.store.save(
        OcrResourceStoreState(
            entries=(identity,), pins=(OcrResourcePin("op-9", identity),)
        )
    )
    managed.payload_path(identity).unlink()
    outcome = managed.reconcile()
    assert outcome.unrecoverable_pinned == (identity,)
    assert outcome.quarantined == ()
    assert managed.store.load().entries == (identity,)
    assert managed.store.load().pins[0].operation_id == "op-9"


def test_reconcile_is_idempotent(tmp_path: Path) -> None:
    managed, identity = _installed(tmp_path)
    managed.payload_path(identity).write_bytes(b"t" * len(BODY))
    first = managed.reconcile()
    assert first.changed is True
    second = managed.reconcile()
    assert second.changed is False
    assert managed.store.load().entries == ()


def test_reconcile_keeps_a_healthy_resource_while_quarantining_a_broken_one(
    tmp_path: Path,
) -> None:
    managed = _managed(tmp_path)
    healthy = _identity()
    broken = _identity(OTHER_BODY, language="deu")
    managed.install(_payload(tmp_path, BODY, "a.bin"), healthy)
    managed.install(_payload(tmp_path, OTHER_BODY, "b.bin"), broken)
    managed.payload_path(broken).unlink()
    outcome = managed.reconcile()
    assert outcome.quarantined == (broken,)
    assert managed.store.load().entries == (healthy,)
    assert managed.payload_path(healthy).read_bytes() == BODY


def test_reconcile_on_an_absent_cache_does_nothing(tmp_path: Path) -> None:
    managed = _managed(tmp_path)
    outcome = managed.reconcile()
    assert outcome.changed is False
    assert not managed.root.exists()


def test_a_reinstall_after_quarantine_succeeds(tmp_path: Path) -> None:
    managed, identity = _installed(tmp_path)
    managed.payload_path(identity).unlink()
    managed.reconcile()
    outcome = managed.install(_payload(tmp_path), identity)
    assert outcome.newly_installed is True
    assert managed.payload_path(identity).read_bytes() == BODY


# --- a newer schema is never rewritten


def _write_future_state(managed: ManagedOcrResources) -> bytes:
    managed.root.mkdir(parents=True, exist_ok=True)
    payload = (
        b'{"schemaVersion":99,"entries":[],"pins":[],"somethingNewer":true}\n'
    )
    managed.store.state_path.write_bytes(payload)
    return payload


def test_a_newer_schema_refuses_an_install_and_is_left_intact(tmp_path: Path) -> None:
    managed = _managed(tmp_path)
    payload = _write_future_state(managed)
    identity = _identity()
    with pytest.raises(OcrResourceStoreVersionUnsupported):
        managed.install(_payload(tmp_path), identity)
    assert managed.store.state_path.read_bytes() == payload
    assert not managed.resource_directory(identity).exists()


def test_a_newer_schema_refuses_a_removal(tmp_path: Path) -> None:
    managed = _managed(tmp_path)
    payload = _write_future_state(managed)
    with pytest.raises(OcrResourceStoreVersionUnsupported):
        managed.remove(_identity())
    assert managed.store.state_path.read_bytes() == payload


def test_a_newer_schema_refuses_the_inventory(tmp_path: Path) -> None:
    managed = _managed(tmp_path)
    payload = _write_future_state(managed)
    with pytest.raises(OcrResourceStoreVersionUnsupported):
        managed.inventory()
    assert managed.store.state_path.read_bytes() == payload


def test_a_newer_schema_refuses_reconciliation_and_sweeps_nothing(
    tmp_path: Path,
) -> None:
    managed = _managed(tmp_path)
    payload = _write_future_state(managed)
    staging = managed.staging_root / "abcdef0123456789"
    staging.mkdir(parents=True)
    (staging / "half.bin").write_bytes(b"half")
    with pytest.raises(OcrResourceStoreVersionUnsupported):
        managed.reconcile()
    assert managed.store.state_path.read_bytes() == payload
    assert (staging / "half.bin").read_bytes() == b"half"


# --- selection projection


def _multilingual(
    tmp_path: Path, *languages: str
) -> tuple[ManagedOcrResources, list[OcrResourceSetIdentity]]:
    """Several languages, each installed as its own identity in its own directory."""
    managed = _managed(tmp_path)
    identities = []
    for index, language in enumerate(languages or ("deu", "eng", "fra")):
        body = BODY + bytes([index])
        identity = _identity(body, language=language)
        managed.install(_payload(tmp_path, body, name=f"{language}.bin"), identity)
        identities.append(identity)
    return managed, identities


@pytest.mark.parametrize("count", [2, 3])
def test_a_selection_of_several_languages_is_served_from_one_directory(
    tmp_path: Path, count: int
) -> None:
    """The repair: separate identities, separate directories, one tessdata directory.

    The engine takes a single `--tessdata-dir`, so until the selection was projected
    a two- or three-language order could not be handed to it at all. Each model is
    present under its own recognition-language name and its bytes are the installed
    bytes -- the same inode, which is what makes "without pretending they share the
    first language's directory" literal rather than a copy that could drift.
    """
    managed, identities = _multilingual(tmp_path)
    selection = identities[:count]

    directory = managed.project_selection(selection)

    assert directory.parent == managed.selections_root
    assert sorted(child.name for child in directory.iterdir()) == sorted(
        f"{identity.language}{MODEL_SUFFIX}" for identity in selection
    )
    for identity in selection:
        projected = directory / f"{identity.language}{MODEL_SUFFIX}"
        assert stat.S_ISREG(projected.lstat().st_mode)
        assert projected.lstat().st_ino == managed.payload_path(identity).lstat().st_ino
        assert hashlib.sha256(projected.read_bytes()).hexdigest() == identity.sha256
    # Still one directory per identity underneath: the projection adds a view, it
    # does not flatten the layout two model revisions of one language depend on.
    assert len({managed.resource_directory(i) for i in selection}) == count


def test_the_order_of_a_selection_is_part_of_its_identity(tmp_path: Path) -> None:
    """`deu+eng` and `eng+deu` are different recognition requests, so different names."""
    managed, identities = _multilingual(tmp_path)
    forward = managed.project_selection([identities[0], identities[1]])
    reversed_ = managed.project_selection([identities[1], identities[0]])
    assert forward != reversed_


def test_projecting_the_same_selection_again_leaves_it_in_place(tmp_path: Path) -> None:
    """Idempotent, and specifically not a rebuild: a concurrent engine invocation may
    be reading the directory, so the cheap path must not unlink and republish it.
    """
    managed, identities = _multilingual(tmp_path)
    first = managed.project_selection(identities[:2])
    before = first.lstat().st_ino

    again = managed.project_selection(identities[:2])

    assert again == first
    assert again.lstat().st_ino == before


def test_a_projection_is_rebuilt_when_a_member_is_no_longer_the_payload(
    tmp_path: Path,
) -> None:
    """A member that is a file of its own rather than a link to the payload is stale."""
    managed, identities = _multilingual(tmp_path)
    directory = managed.project_selection(identities[:2])
    member = directory / f"{identities[0].language}{MODEL_SUFFIX}"
    member.unlink()
    member.write_bytes(b"not the installed bytes")

    rebuilt = managed.project_selection(identities[:2])

    assert rebuilt == directory
    assert member.lstat().st_ino == managed.payload_path(identities[0]).lstat().st_ino


def test_one_uninstalled_language_refuses_the_whole_projection(tmp_path: Path) -> None:
    """Nothing is substituted and nothing is served partially: a selection the cache
    cannot hold entirely has no directory at all.
    """
    managed, identities = _multilingual(tmp_path)
    absent = _identity(OTHER_BODY, language="jpn")
    with pytest.raises(OcrResourceLifecycleRefused) as refused:
        managed.project_selection([identities[1], absent])
    assert refused.value.code == REFUSAL_NOT_INSTALLED
    assert not managed.selections_root.exists()


def test_divergent_bytes_refuse_the_projection_rather_than_linking_them(
    tmp_path: Path,
) -> None:
    """Digest verification is preserved: the projection links bytes it has just
    re-digested against the declared identity, so bytes that changed underneath an
    installed entry are refused here and not handed to the engine.
    """
    managed, identities = _multilingual(tmp_path)
    managed.payload_path(identities[0]).write_bytes(OTHER_BODY)
    with pytest.raises(OcrResourceLifecycleRefused) as refused:
        managed.project_selection(identities[:2])
    assert refused.value.code == REFUSAL_DIVERGENT


def test_a_projection_of_an_absent_cache_is_refused_not_created(tmp_path: Path) -> None:
    managed = _managed(tmp_path)
    with pytest.raises(OcrResourceLifecycleRefused) as refused:
        managed.project_selection([_identity()])
    assert refused.value.code == REFUSAL_NOT_INSTALLED
    assert not managed.root.exists()


@pytest.mark.parametrize(
    "selection",
    [
        pytest.param([], id="empty"),
        pytest.param(["eng"], id="not-an-identity"),
    ],
)
def test_a_selection_that_is_not_one_is_a_usage_fault(
    tmp_path: Path, selection: list[object]
) -> None:
    managed, _identities = _multilingual(tmp_path)
    with pytest.raises(OcrResourceStoreError):
        managed.project_selection(selection)  # type: ignore[arg-type]


def test_one_identity_per_language_is_a_usage_fault_not_a_refusal(tmp_path: Path) -> None:
    """Two revisions of one language cannot both be `eng.traineddata`, so the
    selection is not expressible rather than merely unavailable.
    """
    managed = _managed(tmp_path)
    first = _identity()
    second = _identity(OTHER_BODY, model_version="tessdata_fast-4.2.0")
    managed.install(_payload(tmp_path), first)
    managed.install(_payload(tmp_path, OTHER_BODY, name="other.bin"), second)
    with pytest.raises(OcrResourceStoreError):
        managed.project_selection([first, second])


def test_more_than_the_allowed_models_is_a_usage_fault(tmp_path: Path) -> None:
    managed = _managed(tmp_path)
    identities = []
    for index, language in enumerate(("deu", "eng", "fra", "ita", "spa")):
        body = BODY + bytes([index])
        identity = _identity(body, language=language)
        managed.install(_payload(tmp_path, body, name=f"{language}.bin"), identity)
        identities.append(identity)
    assert len(identities) > MAX_PROJECTED_MODELS
    with pytest.raises(OcrResourceStoreError):
        managed.project_selection(identities)


def test_published_projections_are_bounded(tmp_path: Path) -> None:
    """Otherwise the set grows by one every time an operator changes the order and
    never shrinks. The projection just published is never the one evicted.
    """
    managed, identities = _multilingual(tmp_path)
    orders = [
        [identities[i], identities[j]]
        for i in range(3)
        for j in range(3)
        if i != j
    ] + [identities[:3], identities[::-1], [identities[1]], [identities[2]]]
    assert len(orders) > MAX_PUBLISHED_SELECTIONS
    for order in orders:
        latest = managed.project_selection(order)
    published = sorted(child.name for child in managed.selections_root.iterdir())
    assert len(published) <= MAX_PUBLISHED_SELECTIONS
    assert latest.name in published


def test_removing_a_language_discards_the_projections_linking_it(tmp_path: Path) -> None:
    """A projection holds a *hard link*, so one left behind would keep the removed
    inode alive after its accounting entry was gone and let the engine still be
    pointed at a language the operator removed.
    """
    managed, identities = _multilingual(tmp_path)
    directory = managed.project_selection(identities[:2])
    assert directory.is_dir()

    managed.remove(identities[0])

    assert not directory.exists()
    assert not managed.payload_path(identities[0]).exists()


def test_reconciliation_discards_the_projections_too(tmp_path: Path) -> None:
    """They are derived from entries this pass may drop, and they would keep
    quarantined inodes alive.
    """
    managed, identities = _multilingual(tmp_path)
    directory = managed.project_selection(identities[:2])
    managed.payload_path(identities[0]).write_bytes(OTHER_BODY)

    managed.reconcile()

    assert not directory.exists()


# --- selection leases


def _lease_names(managed: ManagedOcrResources) -> list[str]:
    try:
        return sorted(child.name for child in managed.leases_root.iterdir())
    except FileNotFoundError:
        return []


def test_a_leased_identity_refuses_removal_until_the_lease_ends(
    tmp_path: Path,
) -> None:
    """The window the lease closes. Between resolving a selection and invoking the
    engine over it an operator could remove one of its languages, and the invocation
    would then open a directory that is no longer there.

    Refused with the same code a §18.4 pin refuses with, because the operator's
    recovery is the same one: wait for the operation to finish. And the refusal lasts
    exactly as long as the hold -- the identity is removable again afterwards, so this
    is a hold rather than a new permanent veto.
    """
    managed, identities = _multilingual(tmp_path)

    with managed.leased_selection(identities[:2]) as directory:
        for identity in identities[:2]:
            with pytest.raises(OcrResourceLifecycleRefused) as refused:
                managed.remove(identity)
            assert refused.value.code == REFUSAL_PINNED
        assert directory.is_dir()
        # The third language was never resolved, so nothing holds it.
        managed.remove(identities[2])

    managed.remove(identities[0])
    assert not managed.payload_path(identities[0]).exists()


def test_a_hold_is_the_lock_and_not_the_object_that_took_it(tmp_path: Path) -> None:
    """The cross-process property. Another process is another
    :class:`ManagedOcrResources` over the same root with none of this one's state, so
    a hold only excludes it if the hold lives on disk. It does: the lease file and the
    OS lock over it, which is what the second instance reads.
    """
    managed, identities = _multilingual(tmp_path)
    other = ManagedOcrResources(managed.root)

    with managed.leased_selection(identities[:2]) as directory:
        with pytest.raises(OcrResourceLifecycleRefused) as refused:
            other.remove(identities[0])
        assert refused.value.code == REFUSAL_PINNED
        assert other.reconcile().changed is False
        assert directory.is_dir()

    other.remove(identities[0])


def test_neither_sweep_nor_the_pruning_bound_discards_a_held_projection(
    tmp_path: Path,
) -> None:
    """All three of the ways a projection used to be able to vanish underneath a
    resolution, run while one is held: the removal sweep, the reconciliation sweep and
    the bound on how many projections are kept.
    """
    managed, identities = _multilingual(tmp_path, "deu", "eng", "fra", "ita")
    leased = [identities[0], identities[1]]
    others = identities[:3]

    with managed.leased_selection(leased) as directory:
        # A removal of something else sweeps the unheld projections and keeps this one.
        unheld = managed.project_selection([identities[3]])
        managed.remove(identities[3])
        assert not unheld.exists()
        assert directory.is_dir()
        # A reconciliation sweeps them again, and still keeps this one.
        managed.project_selection([identities[1], identities[0]])
        managed.reconcile()
        assert directory.is_dir()
        # And enough further projections to drive the bound past its limit. The bound
        # has to actually bite, or this would pass with the pruning switched off.
        orders = [
            list(order)
            for size in (1, 2, 3)
            for order in permutations(others, size)
            if list(order) != leased
        ]
        assert len(orders) > MAX_PUBLISHED_SELECTIONS
        for order in orders:
            managed.project_selection(order)
        published = {child.name for child in managed.selections_root.iterdir()}
        assert len(published) <= MAX_PUBLISHED_SELECTIONS + 1
        assert directory.name in published
        assert directory.is_dir()
        assert sorted(child.name for child in directory.iterdir()) == [
            f"{identities[0].language}{MODEL_SUFFIX}",
            f"{identities[1].language}{MODEL_SUFFIX}",
        ]


def test_reconciliation_keeps_an_entry_a_live_lease_still_links(
    tmp_path: Path,
) -> None:
    """A leased entry whose bytes stopped matching is §18.4's unrecoverable case, the
    same as a pinned one: the pass reports it rather than dropping the entry out from
    under the operation holding it.
    """
    managed, identities = _multilingual(tmp_path)

    with managed.leased_selection(identities[:1]):
        # Rewritten in place, so the inode the projection links is the same one.
        managed.payload_path(identities[0]).write_bytes(OTHER_BODY)
        outcome = managed.reconcile()

    assert outcome.quarantined == ()
    assert outcome.unrecoverable_pinned == (identities[0],)
    assert identities[0] in managed.store.load().entries


def test_releasing_the_lease_leaves_nothing_holding_anything(tmp_path: Path) -> None:
    """The release is the whole of the cleanup, after success and after failure alike:
    the lease file goes, and nothing is left claiming a hold.
    """
    managed, identities = _multilingual(tmp_path)

    with managed.leased_selection(identities[:2]):
        assert len(_lease_names(managed)) == 1
    assert _lease_names(managed) == []

    with pytest.raises(RuntimeError), managed.leased_selection(identities[:2]):
        assert len(_lease_names(managed)) == 1
        raise RuntimeError("the extraction failed")
    assert _lease_names(managed) == []
    managed.remove(identities[0])


def test_two_holds_of_one_selection_are_two_leases(tmp_path: Path) -> None:
    """Two extractions may want the same recognition order at once, and the end of
    one must not release the other's hold.
    """
    managed, identities = _multilingual(tmp_path)

    with managed.leased_selection(identities[:2]) as first:
        with managed.leased_selection(identities[:2]) as second:
            assert second == first
            assert len(_lease_names(managed)) == 2
        with pytest.raises(OcrResourceLifecycleRefused) as refused:
            managed.remove(identities[0])
        assert refused.value.code == REFUSAL_PINNED
        assert first.is_dir()


def test_a_lease_whose_holder_has_gone_holds_nothing(tmp_path: Path) -> None:
    """The reason the hold is a lock rather than a record. A record of a claim has to
    be released by whoever took it, so a service that crashed mid-extraction would
    leave one behind for good and its languages could never be removed again. A lock
    nobody holds is a lease nobody holds, and the next operation discards it.
    """
    managed, identities = _multilingual(tmp_path)
    directory = managed.project_selection(identities[:2])
    abandoned = managed.leases_root / f"{directory.name}-{'0' * 32}"
    abandoned.parent.mkdir(parents=True, exist_ok=True)
    abandoned.write_bytes(b"")

    managed.remove(identities[0])

    assert not abandoned.exists()
    assert not directory.exists()


def test_a_lease_of_a_selection_the_cache_cannot_serve_holds_nothing(
    tmp_path: Path,
) -> None:
    """It refuses exactly what a projection refuses, and for the same reasons:
    nothing substituted, no smaller selection, and no hold taken on the strength of
    a resolution that did not happen.
    """
    managed, identities = _multilingual(tmp_path)
    absent = _identity(OTHER_BODY, language="jpn")

    with (
        pytest.raises(OcrResourceLifecycleRefused) as refused,
        managed.leased_selection([identities[0], absent]),
    ):
        pass  # pragma: no cover - the lease is refused before the block runs
    assert refused.value.code == REFUSAL_NOT_INSTALLED

    managed.payload_path(identities[1]).write_bytes(OTHER_BODY)
    with (
        pytest.raises(OcrResourceLifecycleRefused) as diverged,
        managed.leased_selection(identities[1:2]),
    ):
        pass  # pragma: no cover - the lease is refused before the block runs
    assert diverged.value.code == REFUSAL_DIVERGENT
    assert _lease_names(managed) == []


def test_leases_that_cannot_be_enumerated_refuse_rather_than_read_as_none(
    tmp_path: Path,
) -> None:
    """The fail-closed claim the discovery makes, held to. An enumeration failure
    used to return the empty set, which is exactly the answer "no extraction holds
    anything" -- so removal and reconciliation would act on bytes they could not
    prove were free.

    A regular file where ``leases/`` belongs is the enumerable version of that
    failure. Both administrative operations must refuse and change nothing, while
    the resolution path -- whose pruning is a tidy-up after an answer it has
    already produced -- must still succeed, with everything preserved.
    """
    managed, identities = _multilingual(tmp_path)
    directory = managed.project_selection(identities[:2])
    abandoned_staging = managed.staging_root / ("0" * 32)
    abandoned_staging.mkdir(parents=True)
    managed.leases_root.write_bytes(b"")

    with pytest.raises(OcrResourceLifecycleRefused) as refused:
        managed.remove(identities[0])
    assert refused.value.code == REFUSAL_UNAVAILABLE
    with pytest.raises(OcrResourceLifecycleRefused) as swept:
        managed.reconcile()
    assert swept.value.code == REFUSAL_UNAVAILABLE

    # Neither touched the state document, the bytes, the projections -- nor, in
    # reconciliation's case, the staging it would otherwise have swept first.
    assert managed.store.load().entries == tuple(identities)
    assert managed.payload_path(identities[0]).exists()
    assert directory.is_dir()
    assert abandoned_staging.is_dir()

    # The bound is the one caller that must not fail: enough further projections
    # to drive it past its limit, every one of them resolved and kept.
    orders = [
        list(order)
        for size in (1, 2, 3)
        for order in permutations(identities, size)
        if list(order) != list(identities[:2])
    ]
    assert len(orders) > MAX_PUBLISHED_SELECTIONS
    for order in orders:
        assert managed.project_selection(order).is_dir()
    published = {child.name for child in managed.selections_root.iterdir()}
    assert len(published) == len(orders) + 1
    assert directory.name in published


def test_a_live_projection_that_cannot_be_inspected_refuses_mutation(
    tmp_path: Path,
) -> None:
    """The other half of the discovery: a lease names a projection, and what that
    hold covers is read from the projection's own members.

    A member that cannot be inspected used to be skipped, so a projection this
    call could not read covered nothing and an extraction's languages were
    removable underneath it. The identity being removed here is not even one of
    the leased ones -- the refusal is about not knowing, not about this identity.
    """
    managed, identities = _multilingual(tmp_path)

    with managed.leased_selection(identities[:2]) as directory:
        shutil.rmtree(directory)
        directory.write_bytes(b"")

        with pytest.raises(OcrResourceLifecycleRefused) as refused:
            managed.remove(identities[2])
        assert refused.value.code == REFUSAL_UNAVAILABLE
        with pytest.raises(OcrResourceLifecycleRefused) as swept:
            managed.reconcile()
        assert swept.value.code == REFUSAL_UNAVAILABLE

    assert managed.store.load().entries == tuple(identities)
    assert managed.payload_path(identities[2]).exists()


def test_a_payload_that_cannot_be_probed_refuses_the_reconciliation(
    tmp_path: Path,
) -> None:
    """Whether a hold covers an identity is an ``lstat`` of its payload, and the
    quarantine decision turns on the answer.

    Absent is a real answer -- nothing links an inode that is not there, and
    quarantining it is the ordinary repair. Unreadable is not: here the resource
    directory is a regular file, so the payload cannot be reached at all, and the
    pass refuses with the entry and the other entries left exactly as they stand.
    """
    managed, identities = _multilingual(tmp_path)
    broken = managed.resource_directory(identities[1])
    shutil.rmtree(broken)
    broken.write_bytes(b"")

    with pytest.raises(OcrResourceLifecycleRefused) as refused:
        managed.reconcile()
    assert refused.value.code == REFUSAL_UNAVAILABLE

    assert managed.store.load().entries == tuple(identities)
    assert not managed.quarantine_root.exists()

    # And the ordinary absence still reconciles: the entry is quarantined, not
    # refused, because an inode that is not there is held by nobody.
    broken.unlink()
    outcome = managed.reconcile()
    assert outcome.quarantined == (identities[1],)


# --- layout constants


def test_the_installation_directory_name_is_stable() -> None:
    assert OCR_RESOURCES_DIR == "ocr-resources"
    assert MANAGED_RESOURCES_DIRNAME == "resources"
    assert MANAGED_SELECTIONS_DIRNAME == "selections"
    assert MANAGED_LEASES_DIRNAME == "leases"
    assert MANAGED_STAGING_DIRNAME == "staging"
    assert MANAGED_QUARANTINE_DIRNAME == "quarantine"


# --- the pin writer: durable claims a queued operation holds (§18.4, END-02)


def test_a_pin_claims_the_exact_identities_and_a_release_takes_it_back(
    tmp_path: Path,
) -> None:
    """The writer the state document has always had enforcement for.

    A pinned identity is immovable until the operation holding it is resolved, and
    resolving it is the *only* thing that frees it: the removal in between is
    refused with the code an operator recovers from by waiting.
    """
    managed, identity = _installed(tmp_path)

    managed.pin("op-queued", (identity,))

    assert [pin.operation_id for pin in managed.store.load().pins] == ["op-queued"]
    assert managed.inventory().resources[0].pinned_by == ("op-queued",)
    with pytest.raises(OcrResourceLifecycleRefused) as refused:
        managed.remove(identity)
    assert refused.value.code == REFUSAL_PINNED

    managed.release("op-queued")

    assert managed.store.load().pins == ()
    managed.remove(identity)
    assert managed.store.load().entries == ()


def test_a_pin_refuses_an_identity_that_is_not_installed(tmp_path: Path) -> None:
    """A claim on bytes nothing installed is refused rather than recorded.

    A pin that could name an uninstalled identity would be a claim the cache
    cannot keep, and the operation holding it would go on to resolve nothing.
    """
    managed, identity = _installed(tmp_path)
    absent = _identity(OTHER_BODY)

    with pytest.raises(OcrResourceLifecycleRefused) as refused:
        managed.pin("op-absent", (absent,))

    assert refused.value.code == REFUSAL_NOT_INSTALLED
    assert refused.value.identities == (absent,)
    assert managed.store.load().pins == ()
    # The installed one was not pinned either: the claim is all or nothing.
    managed.remove(identity)


def test_a_pin_refuses_bytes_that_have_already_diverged(tmp_path: Path) -> None:
    """The state document says installed and the payload says otherwise.

    Pinning it would hold an entry whose bytes no resolution could verify, so the
    operator is sent to reconciliation instead -- the same answer every other
    operation gives for a divergent entry.
    """
    managed, identity = _installed(tmp_path)
    managed.payload_path(identity).write_bytes(BODY[:-1])

    with pytest.raises(OcrResourceLifecycleRefused) as refused:
        managed.pin("op-divergent", (identity,))

    assert refused.value.code == REFUSAL_DIVERGENT
    assert refused.value.identities == (identity,)
    assert managed.store.load().pins == ()


def test_pinning_again_replaces_one_operations_claim_rather_than_adding(
    tmp_path: Path,
) -> None:
    """An operation's pins are exactly one selection, so the second call replaces.

    That is what lets execution re-assert its own claim without knowing whether
    admission's survived: pinning the same selection writes the same document.
    """
    managed = _managed(tmp_path)
    first = _identity()
    second = _identity(OTHER_BODY, language="deu")
    managed.install(_payload(tmp_path, BODY, "first.bin"), first)
    managed.install(_payload(tmp_path, OTHER_BODY, "second.bin"), second)

    managed.pin("op-1", (first, second))
    managed.pin("op-1", (first, second))
    assert [pin.identity for pin in managed.store.load().pins] == sorted(
        (first, second), key=lambda identity: identity.stable_key
    )

    managed.pin("op-1", (second,))

    assert [pin.identity for pin in managed.store.load().pins] == [second]
    # Replaced, not added to: the identity dropped from the claim is removable.
    managed.remove(first)


def test_a_release_is_quiet_about_an_operation_that_holds_nothing(
    tmp_path: Path,
) -> None:
    """Text-only captures pin nothing, and their settlement still releases.

    The caller releasing after a terminal disposition cannot know whether it ever
    pinned, and must not have to branch on it.
    """
    managed, identity = _installed(tmp_path)
    managed.pin("op-other", (identity,))

    managed.release("op-nothing")

    assert [pin.operation_id for pin in managed.store.load().pins] == ["op-other"]


def test_releasing_against_an_absent_cache_is_not_an_error(tmp_path: Path) -> None:
    """An installation whose resources were taken away has no pins to drop."""
    managed = _managed(tmp_path)

    managed.release("op-anything")

    assert not managed.root.exists()


def test_pinning_an_absent_cache_refuses_rather_than_creating_one(
    tmp_path: Path,
) -> None:
    """A read-shaped operation must never be the thing that brings a cache into
    existence, and a claim on a cache that is not there can only be refused.
    """
    managed = _managed(tmp_path)

    with pytest.raises(OcrResourceLifecycleRefused) as refused:
        managed.pin("op-absent-cache", (_identity(),))

    assert refused.value.code == REFUSAL_NOT_INSTALLED
    assert not managed.root.exists()


def test_reclaim_drops_only_the_pins_under_its_prefix_that_are_not_live(
    tmp_path: Path,
) -> None:
    """Crash recovery, and the scope that makes it safe.

    One installation's cache may serve more than one workspace, so a sweep
    computed from one workspace's obligations is told the prefix its own pins
    carry. A pin under that prefix that no live operation holds is abandoned --
    the admission rolled back, or the release was lost -- and a pin outside it is
    not even considered, however stale it looks from here.
    """
    managed, identity = _installed(tmp_path)
    managed.pin("mine-live", (identity,))
    managed.pin("mine-abandoned", (identity,))
    managed.pin("theirs-live", (identity,))

    dropped = managed.reclaim("mine-", {"mine-live"})

    assert dropped == ("mine-abandoned",)
    assert [pin.operation_id for pin in managed.store.load().pins] == [
        "mine-live",
        "theirs-live",
    ]
    # Still pinned by the two that were kept, so nothing became removable.
    with pytest.raises(OcrResourceLifecycleRefused):
        managed.remove(identity)

    assert managed.reclaim("mine-", {"mine-live"}) == ()
    assert managed.reclaim("theirs-", set()) == ("theirs-live",)
    managed.release("mine-live")
    managed.remove(identity)


def test_reclaim_answers_an_absent_cache_without_creating_one(tmp_path: Path) -> None:
    managed = _managed(tmp_path)

    assert managed.reclaim("mine-", set()) == ()
    assert not managed.root.exists()


@pytest.mark.parametrize("prefix", ["", "x" * 65, None, 7])
def test_a_malformed_reclaim_prefix_is_refused(
    tmp_path: Path, prefix: object
) -> None:
    """Bounded and non-empty, and deliberately not checked as a *token*: a prefix
    is the beginning of one, so it may end on the separator a token may not.
    """
    managed, _identity_installed = _installed(tmp_path)

    with pytest.raises(OcrResourceStoreError):
        managed.reclaim(prefix, set())  # type: ignore[arg-type]


def test_an_assertion_accepts_the_exact_claim_and_writes_nothing(
    tmp_path: Path,
) -> None:
    """What execution asks, and the one thing it must not do while asking.

    A claim over exactly the selection is confirmed; a claim over a subset or a
    superset is a claim about another selection and is refused. Either way the
    state document is byte-identical afterwards: the assertion is a read, so a
    missing claim cannot be repaired by the operation that needed it.
    """
    managed = _managed(tmp_path)
    first = _identity()
    second = _identity(OTHER_BODY, language="deu")
    third = _identity(THIRD_BODY, language="fra")
    managed.install(_payload(tmp_path, BODY, "first.bin"), first)
    managed.install(_payload(tmp_path, OTHER_BODY, "second.bin"), second)
    managed.install(_payload(tmp_path, THIRD_BODY, "third.bin"), third)
    managed.pin("op-1", (first, second))
    before = managed.store.state_path.read_bytes()

    # Order is not part of it: a pin names one identity per row.
    managed.assert_pinned("op-1", (second, first))

    # Every one of these is installed and verifiable, so the only thing wrong is
    # the claim -- which is exactly what this code is for.
    for case, operation, selection in (
        ("subset", "op-1", (first,)),
        ("superset", "op-1", (first, second, third)),
        ("another selection", "op-1", (third,)),
        ("another operation", "op-2", (first, second)),
    ):
        with pytest.raises(OcrResourceLifecycleRefused) as refused:
            managed.assert_pinned(operation, selection)
        assert refused.value.code == REFUSAL_UNPINNED, case
    assert managed.store.state_path.read_bytes() == before


def test_an_assertion_answers_the_resource_question_before_the_claim(
    tmp_path: Path,
) -> None:
    """Bytes that are gone are a resource condition, however the pin looks.

    The two refusals are recovered from differently -- a language to install, or
    an operator to ask about a claim that vanished -- and an obligation whose
    admitted bytes are simply not installed has the first problem whether or not
    it still holds them. So that question is asked first, and
    :data:`REFUSAL_UNPINNED` is reserved for bytes that are present and unheld.
    """
    managed, identity = _installed(tmp_path)
    absent = _identity(OTHER_BODY)

    with pytest.raises(OcrResourceLifecycleRefused) as refused:
        managed.assert_pinned("op-1", (absent,))
    assert refused.value.code == REFUSAL_NOT_INSTALLED

    managed.payload_path(identity).write_bytes(BODY[:-1])
    with pytest.raises(OcrResourceLifecycleRefused) as diverged:
        managed.assert_pinned("op-1", (identity,))
    assert diverged.value.code == REFUSAL_DIVERGENT


def test_an_assertion_against_an_absent_cache_refuses_rather_than_creating_one(
    tmp_path: Path,
) -> None:
    """A read-shaped operation never brings a cache into existence."""
    managed = _managed(tmp_path)

    with pytest.raises(OcrResourceLifecycleRefused) as refused:
        managed.assert_pinned("op-1", (_identity(),))

    assert refused.value.code == REFUSAL_NOT_INSTALLED
    assert not managed.root.exists()


def test_a_pinned_selection_holds_the_projection_and_keeps_the_claim(
    tmp_path: Path,
) -> None:
    """The execution end: check the claim, then project and hold.

    Both claims are live inside the block -- the durable pin and the lease -- and
    only the lease ends with it. The pin outlives the invocation because the
    obligation it belongs to has not been settled yet, and settling it is a
    decision this module does not get to make. The pin is the admission's, taken
    before the block, because this end only ever reads it.
    """
    managed, identity = _installed(tmp_path)
    managed.pin("op-executing", (identity,))

    with managed.pinned_selection("op-executing", (identity,)) as directory:
        assert (directory / f"{identity.language}{MODEL_SUFFIX}").read_bytes() == BODY
        assert [pin.operation_id for pin in managed.store.load().pins] == [
            "op-executing"
        ]
        assert len(list(managed.leases_root.iterdir())) == 1
        with pytest.raises(OcrResourceLifecycleRefused) as refused:
            managed.remove(identity)
        assert refused.value.code == REFUSAL_PINNED

    assert list(managed.leases_root.iterdir()) == []
    with pytest.raises(OcrResourceLifecycleRefused):
        managed.remove(identity)
    managed.release("op-executing")
    managed.remove(identity)


def test_a_pinned_selection_refuses_an_identity_that_has_been_removed(
    tmp_path: Path,
) -> None:
    """Never a substitution. The pin is gone and so are the bytes, and another
    model revision of the same language being installed changes nothing: the exact
    identity is what was asked for and it is not there.
    """
    managed, identity = _installed(tmp_path)
    managed.release("op-executing")
    managed.remove(identity)
    replacement = _identity(OTHER_BODY, model_version="tessdata_fast-4.2.0")
    managed.install(_payload(tmp_path, OTHER_BODY, "replacement.bin"), replacement)

    with (
        pytest.raises(OcrResourceLifecycleRefused) as refused,
        managed.pinned_selection("op-executing", (identity,)),
    ):
        raise AssertionError("a removed identity was served")

    assert refused.value.code == REFUSAL_NOT_INSTALLED
    assert refused.value.identities == (identity,)


def test_a_pinned_resource_is_never_evicted_to_fit_an_installation(
    tmp_path: Path,
) -> None:
    """§18.4's capacity rule with a pin in the way: defer, never evict.

    The cache is exactly full and the only entry is pinned, so there is no unpinned
    byte to reclaim. The installation is deferred with **Language storage is full**
    and the capacity it needed -- the cap is not exceeded, the pinned bytes are
    still there, and the pin was not cancelled to make room.
    """
    managed, identity = _installed(tmp_path, capacity_bytes=len(BODY))
    managed.pin("op-queued", (identity,))

    with pytest.raises(OcrResourceLifecycleRefused) as refused:
        managed.install(
            _payload(tmp_path, OTHER_BODY, "other.bin"),
            _identity(OTHER_BODY, language="deu"),
        )

    assert refused.value.code == REFUSAL_CAPACITY
    assert refused.value.required_bytes == len(OTHER_BODY)
    assert refused.value.available_bytes == 0
    inventory = managed.inventory()
    assert inventory.installed_bytes == len(BODY)
    assert inventory.installed_bytes <= inventory.capacity_bytes
    assert [resource.identity for resource in inventory.resources] == [identity]
    assert managed.payload_path(identity).read_bytes() == BODY
    assert [pin.operation_id for pin in managed.store.load().pins] == ["op-queued"]
