#!/usr/bin/env python3
"""Child: wait at a barrier, then attempt the lock exactly once.

The barrier guarantees the holder already has the lock before this attempt runs,
so a failure to acquire is real exclusion rather than a lost race.
"""
from __future__ import annotations

import sys
from pathlib import Path

from omnivia_core_runtime.ownership.locks import LockRole, create_lock

from phase2.harness import Barrier, emit


def main() -> int:
    lock_path = Path(sys.argv[1])
    barrier_dir = Path(sys.argv[2])
    name = sys.argv[3]

    barrier = Barrier(barrier_dir, participants=2)
    if not barrier.wait_for_release():
        emit({"child": name, "error": "barrier never released"})
        return 4

    lock = create_lock(lock_path, LockRole.LIFETIME_STORAGE, {"child": name})
    acquired = lock.acquire()
    if acquired:
        lock.release()
    emit({"child": name, "acquired": acquired})
    return 0


if __name__ == "__main__":
    sys.exit(main())
