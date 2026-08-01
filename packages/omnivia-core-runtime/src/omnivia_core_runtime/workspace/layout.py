"""The portable five-path workspace layout (T-0629A).

```text
workspace/
├── workspace.json
├── workspace.sqlite
├── blobs/
├── indexes/
└── locks/
```

Nothing else belongs in a portable workspace. Mutable installation-local state —
backups and runtime scratch — lives outside it, so copying the workspace copies
data and not the machine's opinion about that data.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from omnivia_core_runtime.workspace.filesystem import (
    WorkspacePathError,
    scan_for_unsafe_symlinks,
)

MANIFEST_NAME = "workspace.json"
DATABASE_NAME = "workspace.sqlite"
BLOBS_DIR = "blobs"
INDEXES_DIR = "indexes"
LOCKS_DIR = "locks"

#: The complete set of permitted top-level entries.
PERMITTED_ENTRIES = frozenset({MANIFEST_NAME, DATABASE_NAME, BLOBS_DIR, INDEXES_DIR, LOCKS_DIR})

#: Directories created when a workspace is initialised.
REQUIRED_DIRECTORIES = (BLOBS_DIR, INDEXES_DIR, LOCKS_DIR)


@dataclass(frozen=True)
class WorkspaceLayout:
    """Resolved paths for one workspace.

    `root` is machine-local and is never serialised into the manifest. The
    workspace's identity comes from the manifest, which is why moving the
    directory does not change identity.
    """

    root: Path

    @property
    def manifest_path(self) -> Path:
        return self.root / MANIFEST_NAME

    @property
    def database_path(self) -> Path:
        return self.root / DATABASE_NAME

    @property
    def blobs_path(self) -> Path:
        return self.root / BLOBS_DIR

    @property
    def indexes_path(self) -> Path:
        return self.root / INDEXES_DIR

    @property
    def locks_path(self) -> Path:
        return self.root / LOCKS_DIR

    def exists(self) -> bool:
        """Whether this looks like an initialised workspace."""
        return self.manifest_path.is_file()

    def create_directories(self) -> None:
        """Create the root and the three required subdirectories."""
        self.root.mkdir(parents=True, exist_ok=True)
        for name in REQUIRED_DIRECTORIES:
            (self.root / name).mkdir(exist_ok=True)

    def unexpected_entries(self) -> list[str]:
        """Top-level entries that are not part of the portable layout.

        SQLite sidecars are tolerated: `-wal` and `-shm` are created by WAL mode
        and are legitimately present while a database is open. Treating them as
        foreign would make every open workspace look malformed.
        """
        if not self.root.is_dir():
            return []
        tolerated = {f"{DATABASE_NAME}-wal", f"{DATABASE_NAME}-shm", f"{DATABASE_NAME}-journal"}
        found: list[str] = []
        for entry in sorted(self.root.iterdir()):
            if entry.name in PERMITTED_ENTRIES or entry.name in tolerated:
                continue
            found.append(entry.name)
        return found

    def missing_entries(self) -> list[str]:
        """Required layout entries that are absent."""
        missing: list[str] = []
        if not self.manifest_path.is_file():
            missing.append(MANIFEST_NAME)
        for name in REQUIRED_DIRECTORIES:
            if not (self.root / name).is_dir():
                missing.append(name)
        return missing

    def validate(self, *, require_database: bool = False) -> list[str]:
        """Structural problems with this workspace directory.

        An empty list means the layout is exactly the portable five-path form. The
        database is optional by default because a workspace exists as a valid
        portable directory from the moment its manifest is written, before any
        database has been bootstrapped.
        """
        problems: list[str] = []
        if not self.root.is_dir():
            return [f"workspace root does not exist: {self.root}"]

        problems.extend(f"missing required entry: {name}" for name in self.missing_entries())
        problems.extend(
            f"unexpected entry in portable workspace: {name}"
            for name in self.unexpected_entries()
        )
        if require_database and not self.database_path.is_file():
            problems.append(f"missing required entry: {DATABASE_NAME}")

        for offender in scan_for_unsafe_symlinks(self.root):
            problems.append(f"symlink escapes the workspace root: {offender.name}")
        return problems

    def assert_valid(self, *, require_database: bool = False) -> None:
        """Raise if the layout is not the portable five-path form."""
        problems = self.validate(require_database=require_database)
        if problems:
            raise WorkspacePathError("; ".join(problems), self.root)


__all__ = [
    "BLOBS_DIR",
    "DATABASE_NAME",
    "INDEXES_DIR",
    "LOCKS_DIR",
    "MANIFEST_NAME",
    "PERMITTED_ENTRIES",
    "REQUIRED_DIRECTORIES",
    "WorkspaceLayout",
]
