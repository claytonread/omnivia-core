#!/usr/bin/env python3
"""Child: acquire a lock, report, hold until told to release.

Used by the two-process exclusion cases. Holding rather than exiting is essential:
a lock released on exit would let the second process succeed and the test would
prove nothing.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

from omnivia_core_runtime.ownership.locks import LockRole, create_lock

from phase2.harness import Barrier, emit


def main() -> int:
    lock_path = Path(sys.argv[1])
    barrier_dir = Path(sys.argv[2])
    name = sys.argv[3]
    hold_seconds = float(sys.argv[4]) if len(sys.argv) > 4 else 5.0

    barrier = Barrier(barrier_dir, participants=2)
    lock = create_lock(lock_path, LockRole.LIFETIME_STORAGE, {"child": name})

    acquired = lock.acquire()
    emit({"child": name, "acquired": acquired, "pid": lock.read_payload() or {}})
    barrier.arrive(name)

    if acquired:
        deadline = time.monotonic() + hold_seconds
        released = barrier_dir / "release"
        while time.monotonic() < deadline and not released.exists():
            time.sleep(0.01)
        lock.release()
    return 0 if acquired else 3


if __name__ == "__main__":
    sys.exit(main())
