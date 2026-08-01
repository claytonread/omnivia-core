"""Filesystem safety checks for workspace paths (T-0629A).

Everything here fails closed. A path that cannot be proven safe is refused rather
than accepted with a warning, because the caller is about to write a database into
it.
"""

from __future__ import annotations

from pathlib import Path


class WorkspacePathError(Exception):
    """A workspace path is unsafe or escapes its root."""

    def __init__(self, message: str, path: Path) -> None:
        super().__init__(f"{message}: {path}")
        self.path = path


def contains_traversal(relative: str) -> bool:
    """Whether a workspace-relative path contains a traversal or is absolute."""
    if relative == "":
        return True
    candidate = Path(relative)
    if candidate.is_absolute():
        return True
    return any(part == ".." for part in candidate.parts)


def resolve_within(root: Path, relative: str) -> Path:
    """Resolve `relative` under `root`, refusing anything that escapes it.

    Containment is checked after resolution, not before: `a/../../b` only reveals
    itself as an escape once normalised.
    """
    if contains_traversal(relative):
        raise WorkspacePathError("path traversal is not permitted", Path(relative))

    root_resolved = root.resolve()
    target = (root_resolved / relative).resolve()
    if target != root_resolved and root_resolved not in target.parents:
        raise WorkspacePathError("path escapes the workspace root", target)
    return target


def assert_no_unsafe_symlink(root: Path, path: Path) -> None:
    """Refuse a symlink that points outside the workspace root.

    A symlink *inside* the workspace pointing *inside* the workspace is allowed —
    that is an ordinary relocation. One pointing outside is refused, because it
    would let a copied workspace reach back into the machine it came from.
    """
    if not path.is_symlink():
        return
    root_resolved = root.resolve()
    try:
        target = path.resolve(strict=False)
    except OSError as exc:  # pragma: no cover - platform dependent
        raise WorkspacePathError(f"symlink cannot be resolved ({exc})", path) from exc
    if target != root_resolved and root_resolved not in target.parents:
        raise WorkspacePathError("symlink escapes the workspace root", path)


def scan_for_unsafe_symlinks(root: Path) -> list[Path]:
    """Every symlink under `root` whose target escapes `root`."""
    offenders: list[Path] = []
    root_resolved = root.resolve()
    for path in sorted(root.rglob("*")):
        if not path.is_symlink():
            continue
        try:
            target = path.resolve(strict=False)
        except OSError:  # pragma: no cover - platform dependent
            offenders.append(path)
            continue
        if target != root_resolved and root_resolved not in target.parents:
            offenders.append(path)
    return offenders


def fsync_directory(directory: Path) -> None:
    """Flush a directory entry so a rename is durable.

    Renaming a file makes the new name visible, but the *directory entry* is not
    durable until the directory itself is synced. Without this, a crash can leave
    the manifest missing entirely even though the rename appeared to succeed.

    Directory fsync is not available on Windows; the failure is swallowed there
    because there is no equivalent operation, not because durability is optional.
    """
    import os

    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:  # pragma: no cover - platform dependent
        return
    try:
        os.fsync(fd)
    except OSError:  # pragma: no cover - Windows has no directory fsync
        pass
    finally:
        os.close(fd)


__all__ = [
    "WorkspacePathError",
    "assert_no_unsafe_symlink",
    "contains_traversal",
    "fsync_directory",
    "resolve_within",
    "scan_for_unsafe_symlinks",
]
