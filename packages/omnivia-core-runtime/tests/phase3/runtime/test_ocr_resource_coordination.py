"""Two administrators at once: the managed OCR cache under real concurrency.

Every operation on the managed cache spans two things -- resource bytes and the
state document -- so a snapshot of either alone is not a view of the cache. The
properties proved here are the ones that fail when nothing serialises them, and
each is stated as an outcome an unserialised build cannot produce:

- two simultaneous installs of *different* resources both survive, rather than the
  second writing back a state document that never knew about the first (§18.4's
  state document never overstating or understating what is installed);
- simultaneous installs against a cache with room for one of them leave one
  entry, because the second reclaims the first's bytes rather than adding to
  them -- and when a pin puts the shortfall out of reclaiming reach, the second
  is deferred with **Language storage is full** instead. Either way the 512 MiB
  cap is never silently exceeded by two callers each spending the same remaining
  capacity (§17.3, §18.4, END-02);
- simultaneous installs of the *same* identity are deterministic: one publishes,
  the other reports that nothing changed, and neither destroys the other's
  publication nor reports the divergence refusal;
- an install overlapping a removal leaves both decisions in the state document
  rather than whichever process wrote last;
- a reconciliation cannot sweep a live installation's staging, and that
  installation then completes normally;
- a holder that is killed releases the lock on the spot and leaves its abandoned
  staging exactly where the next reconciliation finds it (§18.4's crash window);
- a cache that springs into existence immediately after a passive read decided it
  was absent is reported as the moment before it, never as a tree caught halfway
  into existence -- and in particular a reconciliation pass does not sweep the
  staging of the install that created it;
- two threads sharing one `ManagedOcrResources` exclude each other, which the OS
  lock alone cannot do because both implementations are per-descriptor.

The multi-process cases use the repository's one adversarial harness: real OS
processes released from a filesystem barrier, because the locks are per-descriptor
and threads sharing this process's would prove nothing. Each child's locked
section is stretched by a one-byte copy block over a 64 KiB payload, so the two
children are genuinely inside their installs at the same time rather than merely
permitted to be.

The remaining cases are in-process on purpose: two `ManagedOcrResources` over one
root open two descriptors, which contend exactly as two processes do, and that is
the cheapest way to pin the bounded refusal, the lock file's own properties and
the re-entrancy the administrative surface depends on. The two-thread cases have
to be in-process, because sharing one instance is the whole hazard they prove is
closed; they are released by events rather than by sleeping, so they are
deterministic rather than merely likely.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from omnivia_core_runtime.ownership.locks import LockRole
from omnivia_core_runtime.service import ocr_resource_lifecycle as lifecycle_module
from omnivia_core_runtime.service.ocr_resource_lifecycle import (
    DEFAULT_LOCK_WAIT_SECONDS,
    MANAGED_LOCK_FILENAME,
    REFUSAL_BUSY,
    REFUSAL_CAPACITY,
    REFUSAL_UNAVAILABLE,
    ManagedOcrResources,
    OcrResourceLifecycleRefused,
)
from omnivia_core_runtime.service.ocr_resource_store import (
    OcrResourceSetIdentity,
    OcrResourceStoreError,
)
from phase2.harness import Barrier, ChildResult, spawn, wait

#: Big enough that a one-byte copy block makes the locked section of an install
#: last far longer than the barrier release that starts both children.
BODY = b"managed-model-" * 4681
OTHER_BODY = b"different-model" * 4369

#: A small set installed and pinned before a race, so the room the children
#: contend for is exactly what the other child has not taken.
PINNED_BODY = b"pinned-model" * 2

#: How long a contender is given to refuse, and the ceiling a bounded refusal has
#: to come in under. Generously wider than the wait itself: the assertion is that
#: the wait is *bounded*, not that the scheduler is punctual.
SHORT_WAIT = 0.25
BOUND = 20.0


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


def _identity_fields(identity: OcrResourceSetIdentity) -> dict[str, object]:
    return {
        "language": identity.language,
        "engineVersion": identity.engine_version,
        "modelVersion": identity.model_version,
        "byteSize": identity.byte_size,
        "sha256": identity.sha256,
    }


def _payload(tmp_path: Path, body: bytes, name: str) -> Path:
    path = tmp_path / "downloads" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return path


class _Race:
    """One managed root, and children released onto it together."""

    def __init__(self, tmp_path: Path, participants: int = 2) -> None:
        self.tmp_path = tmp_path
        self.root = tmp_path / "managed"
        self.barrier_dir = tmp_path / "barrier"
        self.resume = tmp_path / "resume"
        self.barrier = Barrier(self.barrier_dir, participants=participants)
        self.specs = tmp_path / "specs"
        self.specs.mkdir(parents=True, exist_ok=True)
        self._index = 0

    def managed(self, **overrides: object) -> ManagedOcrResources:
        return ManagedOcrResources(self.root, **overrides)  # type: ignore[arg-type]

    def spec(self, name: str, **fields: object) -> Path:
        self._index += 1
        path = self.specs / f"{name}-{self._index}.json"
        document: dict[str, object] = {
            "root": str(self.root),
            "barrier": str(self.barrier_dir),
            "resume": str(self.resume),
            "participants": self.barrier.participants,
            "name": name,
            # One byte at a time, so the locked section of an install is long
            # enough that an unserialised build would really interleave.
            "copyBlock": 1,
        }
        document.update(fields)
        path.write_text(json.dumps(document), encoding="utf-8")
        return path

    def start(self, name: str, **fields: object) -> Any:
        return spawn("ocr_resource_action.py", str(self.spec(name, **fields)))

    def release_both(self, *processes: Any, names: tuple[str, ...]) -> list[ChildResult]:
        """Wait for every child at the start line, release them, collect results."""
        assert self.barrier.wait(timeout=BOUND), "both children must reach the barrier"
        self.barrier.release()
        return [
            wait(process, name, timeout=BOUND)
            for process, name in zip(processes, names, strict=True)
        ]

    def resume_now(self) -> None:
        self.resume.write_text("1", encoding="utf-8")


def _reports(results: list[ChildResult]) -> list[dict[str, object]]:
    for result in results:
        assert result.returncode == 0, f"{result.name}: {result.stderr}"
    return [result.report for result in results]


# --- two processes, one cache


def test_two_simultaneous_distinct_installs_both_survive(tmp_path: Path) -> None:
    """Neither install writes back a state document that forgot the other.

    This is the last-writer-wins case: both children load the state document
    before either has written one, so without serialisation the second overwrites
    the first's entry with a document derived from a snapshot that never had it.
    """
    race = _Race(tmp_path)
    first = _identity(BODY)
    second = _identity(OTHER_BODY, language="deu")
    one = race.start(
        "one",
        action="install",
        payload=str(_payload(tmp_path, BODY, "a.bin")),
        identity=_identity_fields(first),
    )
    two = race.start(
        "two",
        action="install",
        payload=str(_payload(tmp_path, OTHER_BODY, "b.bin")),
        identity=_identity_fields(second),
    )
    reports = _reports(race.release_both(one, two, names=("one", "two")))

    assert all(report.get("ok") for report in reports), reports
    assert all(report.get("newlyInstalled") for report in reports), reports

    managed = race.managed()
    assert set(managed.store.load().entries) == {first, second}
    inventory = managed.inventory()
    assert inventory.installed_bytes == len(BODY) + len(OTHER_BODY)
    assert managed.payload_path(first).read_bytes() == BODY
    assert managed.payload_path(second).read_bytes() == OTHER_BODY


def test_simultaneous_installs_cannot_both_spend_the_same_capacity(
    tmp_path: Path,
) -> None:
    """Room for one: the second reclaims the first's bytes rather than adding to them.

    Unserialised, both children read the same remaining capacity, neither sees
    anything to evict and both install against it -- which is precisely §18.4's
    "never silently exceed the cap" being broken, and would leave two entries
    summing to nearly twice the cap. Serialised, whichever runs second finds the
    first's entry, reclaims exactly it, and the cache ends with one.

    The eviction is the evidence that the second one saw the first's reservation,
    and it is the same evidence the refusal used to be before an installation
    could reclaim anything: either way the second child acted on a cache the
    first had already changed.
    """
    race = _Race(tmp_path)
    first = _identity(BODY)
    second = _identity(OTHER_BODY, language="deu")
    capacity = max(len(BODY), len(OTHER_BODY))
    one = race.start(
        "one",
        action="install",
        capacity=capacity,
        payload=str(_payload(tmp_path, BODY, "a.bin")),
        identity=_identity_fields(first),
    )
    two = race.start(
        "two",
        action="install",
        capacity=capacity,
        payload=str(_payload(tmp_path, OTHER_BODY, "b.bin")),
        identity=_identity_fields(second),
    )
    reports = _reports(race.release_both(one, two, names=("one", "two")))

    assert all(report.get("ok") for report in reports), reports
    evicting = [report for report in reports if report.get("evicted")]
    assert len(evicting) == 1, reports
    loser, winner = (first, second) if evicting[0]["child"] == "two" else (second, first)
    assert evicting[0]["evicted"] == [loser.stable_key], reports

    managed = race.managed(capacity_bytes=capacity)
    assert managed.store.load().entries == (winner,)
    assert managed.inventory().installed_bytes <= capacity
    assert not managed.resource_directory(loser).exists()


def test_an_overlap_eviction_cannot_absorb_still_defers_rather_than_overflows(
    tmp_path: Path,
) -> None:
    """Room for the smaller one only, and a pinned entry holding the rest.

    The pinned set is unevictable, so the only room either child can reclaim is
    the other child's entry -- and that is one byte short of what `two` needs.
    So `two` is deferred with **Language storage is full** whichever order they
    run in: it refuses outright when it goes first, and refuses against `one`'s
    entry when it goes second, which is the ordering an unserialised build would
    have let through by spending room `one` had already taken.
    """
    race = _Race(tmp_path)
    first = _identity(BODY)
    second = _identity(OTHER_BODY, language="deu")
    sitting = _identity(PINNED_BODY, language="fra")
    capacity = len(PINNED_BODY) + len(BODY)
    managed = race.managed(capacity_bytes=capacity)
    managed.install(_payload(tmp_path, PINNED_BODY, "pinned.bin"), sitting)
    managed.pin("op-queued", (sitting,))

    one = race.start(
        "one",
        action="install",
        capacity=capacity,
        payload=str(_payload(tmp_path, BODY, "a.bin")),
        identity=_identity_fields(first),
    )
    two = race.start(
        "two",
        action="install",
        capacity=capacity,
        payload=str(_payload(tmp_path, OTHER_BODY, "b.bin")),
        identity=_identity_fields(second),
    )
    reports = _reports(race.release_both(one, two, names=("one", "two")))

    accepted = [report for report in reports if report.get("ok")]
    refused = [report for report in reports if not report.get("ok")]
    assert len(accepted) == 1, reports
    assert len(refused) == 1, reports
    assert refused[0].get("code") == REFUSAL_CAPACITY, refused
    assert accepted[0].get("evicted") == [], reports

    managed = race.managed(capacity_bytes=capacity)
    assert set(managed.store.load().entries) == {sitting, first}
    assert managed.inventory().installed_bytes <= capacity
    assert managed.payload_path(sitting).read_bytes() == PINNED_BODY


def test_simultaneous_installs_of_one_identity_are_deterministic(
    tmp_path: Path,
) -> None:
    """One publishes, the other reports that nothing changed. Never divergent.

    Unserialised, the loser discards the winner's published directory on its way
    to republishing it -- which is what makes the state document claim an entry
    whose bytes are briefly gone, and what the divergence refusal then reports to
    an operator who did nothing wrong.
    """
    race = _Race(tmp_path)
    identity = _identity(BODY)
    one = race.start(
        "one",
        action="install",
        payload=str(_payload(tmp_path, BODY, "a.bin")),
        identity=_identity_fields(identity),
    )
    two = race.start(
        "two",
        action="install",
        payload=str(_payload(tmp_path, BODY, "b.bin")),
        identity=_identity_fields(identity),
    )
    reports = _reports(race.release_both(one, two, names=("one", "two")))

    assert all(report.get("ok") for report in reports), reports
    published = [report for report in reports if report.get("newlyInstalled")]
    assert len(published) == 1, reports

    managed = race.managed()
    assert managed.store.load().entries == (identity,)
    assert managed.payload_path(identity).read_bytes() == BODY
    assert managed.inventory().installed_bytes == len(BODY)


def test_an_install_overlapping_a_removal_keeps_both_decisions(
    tmp_path: Path,
) -> None:
    """Whichever ran first, the cache ends up holding exactly the new resource.

    Both orders converge: remove-then-install leaves the installed one, and
    install-then-remove leaves the same. Unserialised, one of the two state
    writes is derived from a snapshot taken before the other and silently undoes
    it -- losing either the installation or the removal.
    """
    race = _Race(tmp_path)
    existing = _identity(OTHER_BODY, language="deu")
    arriving = _identity(BODY)
    race.managed().install(_payload(tmp_path, OTHER_BODY, "existing.bin"), existing)

    installer = race.start(
        "installer",
        action="install",
        payload=str(_payload(tmp_path, BODY, "arriving.bin")),
        identity=_identity_fields(arriving),
    )
    remover = race.start(
        "remover", action="remove", identity=_identity_fields(existing)
    )
    reports = _reports(
        race.release_both(installer, remover, names=("installer", "remover"))
    )

    assert all(report.get("ok") for report in reports), reports
    managed = race.managed()
    assert managed.store.load().entries == (arriving,)
    assert managed.payload_path(arriving).read_bytes() == BODY
    assert not managed.resource_directory(existing).exists()
    assert managed.inventory().installed_bytes == len(BODY)


def test_reconcile_cannot_sweep_a_live_installations_staging(tmp_path: Path) -> None:
    """A real install, paused inside staging, is not something a sweep may eat.

    The child is stopped inside the private staging step, so it holds the
    coordination lock with verified bytes on disk and no state entry -- exactly
    what reconciliation is otherwise entitled to discard as abandoned. The
    reconciliation is refused as busy, the staged bytes are still there, and the
    install then completes normally.
    """
    race = _Race(tmp_path, participants=1)
    identity = _identity(BODY)
    installer = race.start(
        "installer",
        action="install",
        pauseStage=True,
        payload=str(_payload(tmp_path, BODY, "a.bin")),
        identity=_identity_fields(identity),
    )
    try:
        assert race.barrier.wait(timeout=BOUND, count=1), "installer never arrived"
        race.barrier.release()
        staged = race.barrier_dir / "installer.staged.arrived"
        deadline = time.monotonic() + BOUND
        while time.monotonic() < deadline and not staged.exists():
            time.sleep(0.005)
        assert staged.exists(), "the installer never reached its staging pause"

        managed = race.managed(lock_wait_seconds=SHORT_WAIT)
        before = sorted(child.name for child in managed.staging_root.iterdir())
        assert len(before) == 1, before
        with pytest.raises(OcrResourceLifecycleRefused) as refused:
            managed.reconcile()
        assert refused.value.code == REFUSAL_BUSY
        assert sorted(child.name for child in managed.staging_root.iterdir()) == before
        assert (managed.staging_root / before[0] / f"eng{lifecycle_module.MODEL_SUFFIX}").is_file()
    finally:
        race.resume_now()
        result = wait(installer, "installer", timeout=BOUND)

    assert result.report.get("ok") is True, result.report
    managed = race.managed()
    assert managed.store.load().entries == (identity,)
    assert managed.payload_path(identity).read_bytes() == BODY
    assert list(managed.staging_root.iterdir()) == []


def test_a_killed_holder_releases_the_lock_and_leaves_reconcilable_staging(
    tmp_path: Path,
) -> None:
    """The crash case: no lease to expire, nothing to clean up by hand.

    Both halves matter. The lock is an OS lock on an open descriptor, so killing
    the holder releases it rather than leaving the cache administrable only after
    a manual repair; and the staging it abandoned is still on disk, so the next
    reconciliation is what cleans up rather than the lock's own bookkeeping.
    """
    race = _Race(tmp_path, participants=1)
    holder = race.start("holder", action="hold", stagingName="abandoned")
    marker = race.barrier_dir / "holder.holding.arrived"
    deadline = time.monotonic() + BOUND
    while time.monotonic() < deadline and not marker.exists():
        time.sleep(0.005)
    assert marker.exists(), "the holder never took the lock"

    holder.kill()
    holder.wait(timeout=BOUND)

    managed = race.managed(lock_wait_seconds=SHORT_WAIT)
    assert (managed.staging_root / "abandoned" / "half.bin").read_bytes() == b"half"
    outcome = managed.reconcile()
    assert outcome.discarded_staging == ("abandoned",)
    assert not (managed.staging_root / "abandoned").exists()
    # And the cache is immediately usable again, which is the whole claim.
    identity = _identity(BODY)
    managed.install(_payload(tmp_path, BODY, "a.bin"), identity)
    assert managed.store.load().entries == (identity,)


# --- the bounded refusal


def _held(tmp_path: Path) -> tuple[ManagedOcrResources, ManagedOcrResources]:
    """A cache whose lock one instance holds, and a contender with a short wait.

    Two instances over one root open two descriptors, and both lock
    implementations are per-descriptor, so this contends exactly as two processes
    do -- which is what `test_lock_context_manager_raises_when_unavailable` relies
    on for the storage lock.
    """
    root = tmp_path / "managed"
    holder = ManagedOcrResources(root)
    contender = ManagedOcrResources(root, lock_wait_seconds=SHORT_WAIT)
    return holder, contender


@pytest.mark.parametrize("operation", ["install", "remove", "reconcile", "inventory"])
def test_every_operation_refuses_rather_than_waiting_without_end(
    tmp_path: Path, operation: str
) -> None:
    """Install, remove, reconcile and the status read are all coordinated.

    The read is in the list deliberately: a status call that reported a snapshot
    taken while another process was mid-publication would claim a resource was
    absent that is in fact being installed, which is worse than saying so.
    """
    holder, contender = _held(tmp_path)
    identity = _identity(BODY)
    holder.install(_payload(tmp_path, BODY, "a.bin"), identity)

    actions = {
        "install": lambda: contender.install(
            _payload(tmp_path, OTHER_BODY, "b.bin"),
            _identity(OTHER_BODY, language="deu"),
        ),
        "remove": lambda: contender.remove(identity),
        "reconcile": contender.reconcile,
        "inventory": contender.inventory,
    }
    with holder.coordinated():
        started = time.monotonic()
        with pytest.raises(OcrResourceLifecycleRefused) as refused:
            actions[operation]()
        elapsed = time.monotonic() - started

    assert refused.value.code == REFUSAL_BUSY
    assert elapsed < BOUND, f"{operation} waited {elapsed}s"
    # Nothing was changed by the refusal.
    assert holder.store.load().entries == (identity,)
    assert holder.payload_path(identity).read_bytes() == BODY


def test_the_refusal_names_the_wait_it_gave_up_after(tmp_path: Path) -> None:
    holder, contender = _held(tmp_path)
    with holder.coordinated(), pytest.raises(OcrResourceLifecycleRefused) as refused:
        contender.inventory()
    assert str(SHORT_WAIT) in str(refused.value)
    assert "nothing was changed" in str(refused.value)


def test_the_default_wait_is_bounded_and_stated(tmp_path: Path) -> None:
    """A default, not an unbounded block: `blocking` is never asked for."""
    assert 0 < DEFAULT_LOCK_WAIT_SECONDS < 600
    assert ManagedOcrResources(tmp_path / "m").lock_wait_seconds == (
        DEFAULT_LOCK_WAIT_SECONDS
    )


@pytest.mark.parametrize("wait_seconds", [-1, -0.5, True, "30", None, object()])
def test_a_malformed_lock_wait_is_refused(
    tmp_path: Path, wait_seconds: object
) -> None:
    with pytest.raises(OcrResourceStoreError):
        ManagedOcrResources(tmp_path / "m", lock_wait_seconds=wait_seconds)  # type: ignore[arg-type]


def test_a_zero_wait_is_one_attempt_rather_than_an_error(tmp_path: Path) -> None:
    """Zero is a legitimate choice: try once, refuse, report."""
    holder = ManagedOcrResources(tmp_path / "managed")
    impatient = ManagedOcrResources(tmp_path / "managed", lock_wait_seconds=0)
    with holder.coordinated(), pytest.raises(OcrResourceLifecycleRefused) as refused:
        impatient.inventory()
    assert refused.value.code == REFUSAL_BUSY


# --- the lock file itself


def test_the_lock_sits_at_one_stable_owner_private_path(tmp_path: Path) -> None:
    """A per-call name would exclude nobody, and a loose mode would exclude nobody
    that matters."""
    managed = ManagedOcrResources(tmp_path / "managed")
    assert managed.lock_path == managed.root / MANAGED_LOCK_FILENAME
    assert MANAGED_LOCK_FILENAME == "coordination.lock"
    managed.install(_payload(tmp_path, BODY, "a.bin"), _identity(BODY))
    assert managed.lock_path.is_file()
    if os.name != "nt":
        assert stat.S_IMODE(managed.lock_path.stat().st_mode) == 0o600
    # Repeating the operation reuses it rather than accumulating names.
    managed.inventory()
    assert [
        child.name
        for child in managed.root.iterdir()
        if child.name.startswith("coordination")
    ] == [MANAGED_LOCK_FILENAME]


def test_reconciliation_never_sweeps_its_own_lock(tmp_path: Path) -> None:
    """The lock is at the root, not under `staging/` or `resources/`.

    Placed inside either it would be swept by the pass that holds it, which on
    POSIX would leave a reconciliation holding a lock no later contender can even
    find -- mutual exclusion silently gone.
    """
    managed = ManagedOcrResources(tmp_path / "managed")
    managed.install(_payload(tmp_path, BODY, "a.bin"), _identity(BODY))
    managed.reconcile()
    assert managed.lock_path.is_file()
    assert managed.lock_path.parent == managed.root


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink creation")
def test_a_symlink_at_the_lock_path_fails_closed(tmp_path: Path) -> None:
    """Fail closed rather than lock -- and write -- through a redirected name."""
    managed = ManagedOcrResources(tmp_path / "managed")
    managed.root.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.write_bytes(b"must stay unchanged")
    managed.lock_path.symlink_to(outside)

    with pytest.raises(OcrResourceLifecycleRefused) as refused:
        managed.install(_payload(tmp_path, BODY, "a.bin"), _identity(BODY))
    assert refused.value.code == REFUSAL_UNAVAILABLE
    assert outside.read_bytes() == b"must stay unchanged"
    assert managed.lock_path.is_symlink()


def test_a_directory_at_the_lock_path_fails_closed(tmp_path: Path) -> None:
    managed = ManagedOcrResources(tmp_path / "managed")
    managed.lock_path.mkdir(parents=True)
    with pytest.raises(OcrResourceLifecycleRefused) as refused:
        managed.inventory()
    assert refused.value.code == REFUSAL_UNAVAILABLE


@pytest.mark.skipif(os.name == "nt", reason="POSIX FIFO creation")
def test_a_fifo_at_the_lock_path_fails_closed(tmp_path: Path) -> None:
    """A directory is refused by the open mode; a FIFO is not, and is lockable.

    Which would make mutual exclusion appear to work while the thing being held
    open is not a file at all -- so the regular-file test is explicit, exactly as
    it is for a payload.
    """
    managed = ManagedOcrResources(tmp_path / "managed")
    managed.root.mkdir(parents=True)
    os.mkfifo(managed.lock_path)
    with pytest.raises(OcrResourceLifecycleRefused) as refused:
        managed.inventory()
    assert refused.value.code == REFUSAL_UNAVAILABLE
    assert "regular file" in str(refused.value)


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink creation")
def test_a_managed_root_that_became_a_symlink_fails_closed(tmp_path: Path) -> None:
    """The constructor's check is not the last word: the root can change after."""
    managed = ManagedOcrResources(tmp_path / "managed")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    managed.root.symlink_to(elsewhere)
    with pytest.raises(OcrResourceLifecycleRefused) as refused:
        managed.install(_payload(tmp_path, BODY, "a.bin"), _identity(BODY))
    assert refused.value.code == REFUSAL_UNAVAILABLE
    assert list(elsewhere.iterdir()) == []


def test_the_lock_is_taken_under_the_resource_coordination_role(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not lifetime storage ownership, and not the launcher's bootstrap mutex.

    The role is asserted at the call rather than inferred from the lock file,
    because a payload proves nothing about what a lock grants -- and misusing the
    lifetime storage role here would have claimed write authority over a workspace
    this administration does not own.
    """
    roles: list[LockRole] = []
    real = lifecycle_module.create_lock

    def recording(path: Path, role: LockRole, *args: Any, **keywords: Any) -> Any:
        roles.append(role)
        return real(path, role, *args, **keywords)

    monkeypatch.setattr(lifecycle_module, "create_lock", recording)
    managed = ManagedOcrResources(tmp_path / "managed")
    managed.install(_payload(tmp_path, BODY, "a.bin"), _identity(BODY))

    assert roles == [LockRole.RESOURCE_COORDINATION]
    assert not roles[0].grants_write_authority


# --- re-entrancy and the absent cache


def test_coordination_is_re_entrant_within_one_cache(tmp_path: Path) -> None:
    """The administrative surface runs an action and then renders the inventory.

    Those two have to be one view of one moment, and an inner block must not
    release what the outer one still needs -- which an OS lock has no depth of its
    own to prevent.
    """
    managed = ManagedOcrResources(tmp_path / "managed", lock_wait_seconds=SHORT_WAIT)
    identity = _identity(BODY)
    with managed.coordinated():
        managed.install(_payload(tmp_path, BODY, "a.bin"), identity)
        inventory = managed.inventory()
        assert inventory.installed_bytes == len(BODY)
        # Still held: a contender must still be excluded from inside the block.
        other = ManagedOcrResources(managed.root, lock_wait_seconds=SHORT_WAIT)
        with pytest.raises(OcrResourceLifecycleRefused) as refused:
            other.inventory()
        assert refused.value.code == REFUSAL_BUSY
    # And released exactly once, at the end of the outermost block.
    assert ManagedOcrResources(managed.root).inventory().installed_bytes == len(BODY)


def test_a_reading_pass_over_an_absent_cache_still_creates_nothing(
    tmp_path: Path,
) -> None:
    """A read must not be the thing that brings the store into existence.

    The inventory and the reconciliation pass do nothing at all to a cache that is
    not there, so taking the lock would be the one act that created it -- lock file
    included. They run unlocked in that case, which is safe precisely because they
    touch nothing: an install racing them creates the root and completes, and
    reporting the moment before it is accurate rather than incoherent.
    """
    managed = ManagedOcrResources(tmp_path / "managed")
    assert managed.inventory().resources == ()
    assert managed.reconcile().changed is False
    assert not managed.root.exists()
    assert not managed.lock_path.exists()


class _MutatesAfterTheDecision(ManagedOcrResources):
    """A cache that is installed into the instant it is found absent.

    The deterministic stand-in for the install that wins this race: `_absent` *is*
    the decision, so overriding it lands the concurrent work in the one window
    that matters -- after the real answer was computed, before the caller can act
    on it -- with no threads, no sleeps and no scheduler to trust. The window is
    the whole hazard: a caller that went on to read the root after this would see
    whatever the mutation had got to by then.
    """

    mutate: Callable[[], None]
    decisions: int = 0

    def _absent(self) -> bool:
        absent = super()._absent()
        self.decisions += 1
        if absent and self.decisions == 1:
            self.mutate()
        return absent


def test_a_read_racing_an_install_reports_the_moment_it_decided_on(
    tmp_path: Path,
) -> None:
    """The absence decision is a whole snapshot, not the start of one.

    An install creates the root and completes between the decision and the
    result. The read is entitled to report either side of it, and reports the
    before -- what it may not do is go and look, because looking lands at some
    third moment that is neither.
    """
    root = tmp_path / "managed"
    identity = _identity(BODY)
    payload = _payload(tmp_path, BODY, "a.bin")

    managed = _MutatesAfterTheDecision(root)
    managed.mutate = lambda: ManagedOcrResources(root).install(payload, identity)

    inventory = managed.inventory()

    assert managed.decisions == 1
    assert inventory.resources == ()
    assert inventory.installed_bytes == 0
    assert inventory.available_bytes == inventory.capacity_bytes
    # The install really did land, and the read simply did not look at it: the
    # result is the coherent *before*, and the cache is the coherent *after*.
    after = ManagedOcrResources(root).inventory()
    assert [resource.identity for resource in after.resources] == [identity]
    assert after.resources[0].bytes_present


def test_a_reconciliation_cannot_sweep_the_install_that_created_the_cache(
    tmp_path: Path,
) -> None:
    """The destructive half of the same race, and the reason absence must decide.

    What lands after the decision here is the shape a live install has mid-flight:
    the root, private staging holding its verified bytes, and no state entry for
    them yet. A pass that read the root after deciding it was absent would find
    exactly that and be entitled to discard it as abandoned -- destroying another
    administrator's work and reporting a change it had no authority to make. The
    no-change answer is the before snapshot, and the staging survives.
    """
    root = tmp_path / "managed"
    staged = root / "staging" / "0123456789abcdef" / f"eng{lifecycle_module.MODEL_SUFFIX}"
    unreferenced = root / "resources" / "deadbeef" / "deu.traineddata"

    def arriving_install() -> None:
        for path in (staged, unreferenced):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(BODY)

    managed = _MutatesAfterTheDecision(root)
    managed.mutate = arriving_install

    outcome = managed.reconcile()

    assert managed.decisions == 1
    assert outcome.changed is False
    assert outcome.discarded_staging == ()
    assert outcome.discarded_unreferenced == ()
    assert staged.read_bytes() == BODY
    assert unreferenced.read_bytes() == BODY
    # And the pass that does hold the lock is still free to sweep them.
    later = ManagedOcrResources(root).reconcile()
    assert later.discarded_staging == ("0123456789abcdef",)
    assert later.discarded_unreferenced == ("deadbeef",)


# --- two threads, one instance


class _PausingInstall(ManagedOcrResources):
    """An install that stops with the lock held and its bytes already staged.

    Not a sleep somewhere convenient: this puts the holding thread inside the real
    install, holding the real coordination, which is the state a second thread
    sharing this instance must not be able to act beside.
    """

    inside: threading.Event
    release: threading.Event

    def _stage(
        self, source: Path, identity: OcrResourceSetIdentity, staging: Path
    ) -> Path:
        staged = super()._stage(source, identity, staging)
        self.inside.set()
        assert self.release.wait(BOUND), "the parent never released the install"
        return staged


def _paused_install(
    root: Path, payload: Path, identity: OcrResourceSetIdentity
) -> tuple[_PausingInstall, threading.Thread, list[BaseException]]:
    """Start an install in another thread and return once it is holding.

    One instance, deliberately: the OS lock is per-descriptor, so two threads of
    one `ManagedOcrResources` share the very descriptor that is supposed to
    exclude them and the kernel grants the second one the lock the first holds.
    Only the process-local half can serialise this pair.
    """
    managed = _PausingInstall(root, lock_wait_seconds=SHORT_WAIT)
    managed.inside = threading.Event()
    managed.release = threading.Event()
    failures: list[BaseException] = []

    def run() -> None:
        try:
            managed.install(payload, identity)
        except BaseException as failure:  # noqa: BLE001 - reported to the parent
            failures.append(failure)
            managed.inside.set()

    worker = threading.Thread(target=run, name="paused-install")
    worker.start()
    assert managed.inside.wait(BOUND), "the install never reached its staging pause"
    assert not failures, failures
    return managed, worker, failures


@pytest.mark.parametrize("operation", ["inventory", "install", "remove", "reconcile"])
def test_a_second_thread_of_one_instance_is_refused_not_admitted(
    tmp_path: Path, operation: str
) -> None:
    """Read/mutation and mutation/mutation overlap on one shared instance.

    The hazard this closes is a bypass rather than a wait: an instance-global
    depth counter reads nonzero to *any* thread, so the second one took the
    re-entrant path and ran its read-decide-write beside a live install without
    holding anything. It is now refused within its bounded wait, and the paused
    install then completes untouched.
    """
    root = tmp_path / "managed"
    installed = _identity(OTHER_BODY, language="deu")
    ManagedOcrResources(root).install(
        _payload(tmp_path, OTHER_BODY, "installed.bin"), installed
    )
    arriving = _identity(BODY)
    managed, worker, failures = _paused_install(
        root, _payload(tmp_path, BODY, "arriving.bin"), arriving
    )

    actions: dict[str, Callable[[], object]] = {
        "inventory": managed.inventory,
        "install": lambda: managed.install(
            _payload(tmp_path, OTHER_BODY, "second.bin"), installed
        ),
        "remove": lambda: managed.remove(installed),
        "reconcile": managed.reconcile,
    }
    try:
        started = time.monotonic()
        with pytest.raises(OcrResourceLifecycleRefused) as refused:
            actions[operation]()
        elapsed = time.monotonic() - started
    finally:
        managed.release.set()
        worker.join(BOUND)

    assert not failures, failures
    assert refused.value.code == REFUSAL_BUSY
    assert elapsed < BOUND, f"{operation} waited {elapsed}s"
    assert "nothing was changed" in str(refused.value)
    # The install the second thread could not disturb finished normally.
    state = ManagedOcrResources(root).store.load()
    assert set(state.entries) == {installed, arriving}
    assert ManagedOcrResources(root).payload_path(arriving).read_bytes() == BODY


def test_the_two_thread_refusal_costs_the_wait_that_was_asked_for(
    tmp_path: Path,
) -> None:
    """One deadline across both locks, not one wait each.

    A contender waits for the process-local mutex and then for the OS lock, so a
    naive implementation charges a thread twice its configured wait before it is
    told the cache is busy.
    """
    root = tmp_path / "managed"
    identity = _identity(BODY)
    managed, worker, failures = _paused_install(
        root, _payload(tmp_path, BODY, "a.bin"), identity
    )
    try:
        started = time.monotonic()
        with pytest.raises(OcrResourceLifecycleRefused):
            managed.inventory()
        elapsed = time.monotonic() - started
    finally:
        managed.release.set()
        worker.join(BOUND)

    assert not failures, failures
    # Generous against a slow scheduler, tight enough to fail a doubled wait.
    assert elapsed < SHORT_WAIT * 4, elapsed


def test_re_entrancy_belongs_to_the_owning_thread_alone(tmp_path: Path) -> None:
    """The depth the administrative surface relies on is not a global bypass.

    Stated separately from the operations above because it is the mechanism rather
    than a symptom: inside one thread's coordinated block the depth is nonzero,
    and a second thread asking for the same instance must still be excluded by it.
    """
    managed = ManagedOcrResources(tmp_path / "managed", lock_wait_seconds=SHORT_WAIT)
    refusals: list[str] = []

    def contend() -> None:
        try:
            with managed.coordinated():
                refusals.append("admitted")
        except OcrResourceLifecycleRefused as refused:
            refusals.append(refused.code)

    with managed.coordinated() as coordinated:
        assert coordinated is True
        with managed.coordinated() as nested:
            assert nested is True, "the owning thread re-enters"
        other = threading.Thread(target=contend, name="contender")
        other.start()
        other.join(BOUND)
        assert not other.is_alive(), "the contender never gave up"

    assert refusals == [REFUSAL_BUSY]
