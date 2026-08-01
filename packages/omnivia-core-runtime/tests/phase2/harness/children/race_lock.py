#!/usr/bin/env python3
"""Child: race for a lock at a synchronised start, and report the outcome.

Both racers arrive, then wait for release, then attempt simultaneously. Exactly one
must win.
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
    barrier.arrive(name)
    if not barrier.wait_for_release():
        emit({"child": name, "error": "barrier never released"})
        return 4

    lock = create_lock(lock_path, LockRole.LIFETIME_STORAGE, {"child": name})
    acquired = lock.acquire()
    emit({"child": name, "acquired": acquired})
    if acquired:
        # Hold briefly so the loser's attempt genuinely overlaps.
        import time

        time.sleep(0.4)
        lock.release()
    return 0


if __name__ == "__main__":
    sys.exit(main())
