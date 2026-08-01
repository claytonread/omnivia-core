"""Multi-process adversarial harness for Phase 2.

Real OS processes, not threads. Threads share the SQLite connection cache and the
GIL and cannot demonstrate file-level exclusion; FM-12 in particular needs a
process that does not import OmniVia at all, because the claim is about the stock
SQLite VFS rather than about the runtime's own guards.

Racing cases use a filesystem barrier rather than a sleep. A sleep proves nothing
about simultaneity — it only makes the race likely — whereas a barrier guarantees
both processes were at the start line before either proceeded.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

CHILDREN = Path(__file__).parent / "children"
DEFAULT_TIMEOUT = 30.0


@dataclass(frozen=True)
class ChildResult:
    """Outcome of one child process."""

    name: str
    returncode: int
    stdout: str
    stderr: str

    @property
    def report(self) -> dict[str, object]:
        """The child's structured result line.

        Children print exactly one JSON line so the parent asserts on what the
        child observed rather than on what the parent guessed. A missing line is
        reported as such instead of silently becoming an empty dict.
        """
        for line in reversed(self.stdout.splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    value = json.loads(line)
                except ValueError:
                    continue
                if isinstance(value, dict):
                    return value
        return {"error": "no structured result", "stdout": self.stdout, "stderr": self.stderr}

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def child_environment(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Environment giving a child the same isolated import path as the tests.

    The venv holds an editable install of `omnivia_memory` pointing at the primary
    checkout, so PYTHONPATH must shadow it or a child would import a different
    worktree's source than the test intends.
    """
    repo = Path(__file__).resolve().parents[5]
    paths = [
        str(repo / "src"),
        str(repo / "services" / "omnivia-memory" / "src"),
        str(repo / "packages" / "omnivia-core-runtime" / "src"),
        str(repo / "packages" / "omnivia-core-runtime" / "tests"),
    ]
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(paths)
    if extra:
        environment.update(extra)
    return environment


def spawn(
    child: str,
    *args: str,
    env: dict[str, str] | None = None,
) -> subprocess.Popen[str]:
    """Launch a child script detached from the test process."""
    script = CHILDREN / child
    if not script.is_file():
        raise FileNotFoundError(f"no such child script: {script}")
    return subprocess.Popen(
        [sys.executable, str(script), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=child_environment(env),
    )


def wait(
    process: subprocess.Popen[str], name: str, timeout: float = DEFAULT_TIMEOUT
) -> ChildResult:
    """Wait for a child, killing it if it overruns so a hang cannot wedge CI."""
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        stdout, stderr = process.communicate()
        return ChildResult(name=name, returncode=-9, stdout=stdout or "", stderr="timeout")
    return ChildResult(
        name=name, returncode=process.returncode, stdout=stdout or "", stderr=stderr or ""
    )


def run_child(
    child: str, *args: str, env: dict[str, str] | None = None, timeout: float = DEFAULT_TIMEOUT
) -> ChildResult:
    """Spawn and wait, for cases that do not need a rendezvous."""
    return wait(spawn(child, *args, env=env), child, timeout=timeout)


class Barrier:
    """Filesystem rendezvous for N processes.

    Each participant declares arrival by creating a file and then waits until every
    participant has arrived. Deterministic, cross-process, and needs no shared
    memory or sockets.
    """

    def __init__(self, directory: Path, participants: int) -> None:
        self.directory = directory
        self.participants = participants
        directory.mkdir(parents=True, exist_ok=True)

    def arrive(self, name: str) -> None:
        (self.directory / f"{name}.arrived").write_text("1", encoding="utf-8")

    def arrived(self) -> int:
        return len(list(self.directory.glob("*.arrived")))

    def wait(self, timeout: float = DEFAULT_TIMEOUT, count: int | None = None) -> bool:
        """Wait until `count` participants have arrived (default: all of them).

        A count is sometimes narrower than the full rendezvous: an exclusion test
        needs to know the *holder* is in place before it starts the contender, and
        waiting for both would deadlock because the contender has not been spawned.
        """
        required = self.participants if count is None else count
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.arrived() >= required:
                return True
            time.sleep(0.005)
        return False

    def release(self) -> None:
        """Signal that every participant may proceed."""
        (self.directory / "go").write_text("1", encoding="utf-8")

    def wait_for_release(self, timeout: float = DEFAULT_TIMEOUT) -> bool:
        deadline = time.monotonic() + timeout
        gate = self.directory / "go"
        while time.monotonic() < deadline:
            if gate.exists():
                return True
            time.sleep(0.005)
        return False


def emit(payload: dict[str, object]) -> None:
    """Child-side: print the single structured result line."""
    sys.stdout.write(json.dumps(payload, sort_keys=True) + "\n")
    sys.stdout.flush()


__all__ = [
    "CHILDREN",
    "DEFAULT_TIMEOUT",
    "Barrier",
    "ChildResult",
    "child_environment",
    "emit",
    "run_child",
    "spawn",
    "wait",
]
