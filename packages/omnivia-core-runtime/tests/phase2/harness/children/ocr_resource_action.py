#!/usr/bin/env python3
"""Child: run one managed OCR resource action, released from a barrier.

Lives in the one multi-process harness this repository has rather than beside the
phase 3 tests that drive it, because the claim being proved is the same kind the
harness exists for: that real OS processes exclude each other. Threads cannot
demonstrate it -- `flock` and the Windows byte-range lock are per-descriptor and a
thread would share this process's, so two threads would sail through a lock that
genuinely excludes two processes.

The whole specification arrives as one JSON document so the parent can describe a
slow install, a tight lock wait or a paused staging without this script growing a
flag per case. One structured result line comes back, as every child here does.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from omnivia_core_runtime.service import ocr_resource_lifecycle as lifecycle_module
from omnivia_core_runtime.service.ocr_resource_lifecycle import (
    ManagedOcrResources,
    OcrResourceLifecycleRefused,
)
from omnivia_core_runtime.service.ocr_resource_store import (
    OcrResourceSetIdentity,
    OcrResourceStoreError,
)

from phase2.harness import Barrier, emit

#: Longest a paused child waits to be resumed before giving up, so a parent that
#: fails its assertion before releasing cannot leave a process behind.
_RESUME_TIMEOUT = 60.0


def _wait_for(path: Path, timeout: float = _RESUME_TIMEOUT) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.005)
    return False


def _identity(fields: dict[str, object]) -> OcrResourceSetIdentity:
    return OcrResourceSetIdentity(
        language=str(fields["language"]),
        engine_version=str(fields["engineVersion"]),
        model_version=str(fields["modelVersion"]),
        byte_size=int(str(fields["byteSize"])),
        sha256=str(fields["sha256"]),
    )


def _managed(spec: dict[str, object], barrier: Barrier) -> ManagedOcrResources:
    """The cache this child administers, pausing inside staging when asked.

    The pause is an override of the private staging step rather than a sleep
    anywhere convenient: it puts this child inside the real install, holding the
    real coordination lock, with real staging bytes on disk -- which is the state
    a parent's reconciliation must not be able to sweep.
    """
    keywords: dict[str, object] = {}
    if spec.get("capacity") is not None:
        keywords["capacity_bytes"] = int(str(spec["capacity"]))
    if spec.get("lockWait") is not None:
        keywords["lock_wait_seconds"] = float(str(spec["lockWait"]))
    root = Path(str(spec["root"]))
    if not spec.get("pauseStage"):
        return ManagedOcrResources(root, **keywords)  # type: ignore[arg-type]

    name = str(spec["name"])
    resume = Path(str(spec["resume"]))

    class _PausingResources(ManagedOcrResources):
        def _stage(
            self, source: Path, identity: OcrResourceSetIdentity, staging: Path
        ) -> Path:
            staged = super()._stage(source, identity, staging)
            barrier.arrive(f"{name}.staged")
            _wait_for(resume)
            return staged

    return _PausingResources(root, **keywords)  # type: ignore[arg-type]


def _perform(spec: dict[str, object], managed: ManagedOcrResources) -> dict[str, object]:
    action = str(spec["action"])
    if action == "status":
        inventory = managed.inventory()
        return {
            "installedBytes": inventory.installed_bytes,
            "availableBytes": inventory.available_bytes,
            "installed": len(inventory.resources),
        }
    if action == "reconcile":
        outcome = managed.reconcile()
        return {
            "discardedStaging": list(outcome.discarded_staging),
            "discardedUnreferenced": list(outcome.discarded_unreferenced),
            "changed": outcome.changed,
        }
    if action == "remove":
        managed.remove(_identity(dict(spec["identity"])))  # type: ignore[arg-type]
        return {"removed": True}
    installed = managed.install(
        Path(str(spec["payload"])),
        _identity(dict(spec["identity"])),  # type: ignore[arg-type]
    )
    return {
        "newlyInstalled": installed.newly_installed,
        "installedBytes": installed.installed_bytes,
        "availableBytes": installed.available_bytes,
        # By stable key, because a parent comparing two children's reports needs
        # something it can write down rather than five fields per victim.
        "evicted": [entry.stable_key for entry in installed.evicted],
    }


def _hold(spec: dict[str, object], managed: ManagedOcrResources, barrier: Barrier) -> None:
    """Hold the coordination lock with staging on disk, then wait.

    The shape a live installation has when it is interrupted: the lock held, a
    staging directory with partial bytes in it, and no state entry. A parent kills
    this child to prove that both the lock and the staging survive the way a crash
    leaves them.
    """
    name = str(spec["name"])
    with managed.coordinated():
        staging = managed.staging_root / str(spec.get("stagingName", "abandoned"))
        staging.mkdir(parents=True, exist_ok=True)
        (staging / "half.bin").write_bytes(b"half")
        emit({"child": name, "ok": True, "holding": True})
        barrier.arrive(f"{name}.holding")
        _wait_for(Path(str(spec["resume"])))


def main() -> int:
    spec = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    name = str(spec["name"])
    if spec.get("copyBlock") is not None:
        # A deliberately tiny copy block stretches the locked section of an
        # install far past the moment both children leave the barrier, so an
        # unserialised build really would overlap rather than merely be allowed to.
        lifecycle_module._COPY_BLOCK = int(str(spec["copyBlock"]))

    barrier = Barrier(Path(str(spec["barrier"])), participants=int(str(spec["participants"])))
    managed = _managed(spec, barrier)

    if str(spec["action"]) == "hold":
        _hold(spec, managed, barrier)
        return 0

    barrier.arrive(name)
    if not barrier.wait_for_release():
        emit({"child": name, "ok": False, "error": "barrier never released"})
        return 4

    started = time.monotonic()
    try:
        result = _perform(spec, managed)
    except OcrResourceLifecycleRefused as refused:
        emit(
            {
                "child": name,
                "ok": False,
                "code": refused.code,
                "elapsed": time.monotonic() - started,
            }
        )
        return 0
    except OcrResourceStoreError as failure:
        emit({"child": name, "ok": False, "code": "store_error", "reason": str(failure)})
        return 0
    emit({"child": name, "ok": True, "elapsed": time.monotonic() - started, **result})
    return 0


if __name__ == "__main__":
    sys.exit(main())
