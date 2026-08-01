"""Atomic manifest storage and zero-write inspection (T-0629A).

Writing the manifest is the one operation that must never leave a half-state: a
torn `workspace.json` makes the workspace unopenable and its identity unknowable.
The write is therefore temp-file → fsync → atomic rename → directory fsync, which
gives a reader either the complete old manifest or the complete new one and never
anything between them.

Inspection is strictly read-only. ADR-037 restricts direct-storage operation to
ephemeral, read-only or explicitly exclusive maintenance modes, and a read-only
open that quietly writes would break that guarantee.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from omnivia_core.workspace.compatibility import (
    CompatibilityOutcome,
    evaluate_compatibility,
)
from omnivia_core.workspace.manifest import (
    WorkspaceManifest,
    validate_manifest,
    validate_raw_manifest,
)
from omnivia_core_runtime.workspace.filesystem import fsync_directory
from omnivia_core_runtime.workspace.layout import WorkspaceLayout


class ManifestStoreError(Exception):
    """The manifest could not be read, parsed or validated."""


@dataclass(frozen=True)
class WorkspaceInspection:
    """The result of a zero-write inspection."""

    manifest: WorkspaceManifest
    layout_problems: tuple[str, ...]
    integrity_ok: bool
    compatibility: CompatibilityOutcome

    @property
    def ok(self) -> bool:
        return (
            not self.layout_problems
            and self.integrity_ok
            and self.compatibility.compatible
        )


def write_manifest(layout: WorkspaceLayout, manifest: WorkspaceManifest) -> Path:
    """Atomically write the manifest, refreshing its integrity block.

    The integrity block is always recomputed here rather than trusted from the
    caller, so a manifest on disk cannot carry a checksum that does not describe
    it.
    """
    sealed = manifest.with_integrity()
    result = validate_manifest(sealed)
    if not result.valid:
        raise ManifestStoreError(
            "refusing to write an invalid manifest: " + "; ".join(result.errors)
        )

    layout.root.mkdir(parents=True, exist_ok=True)
    target = layout.manifest_path
    # Same directory, so the rename is guaranteed to be within one filesystem.
    temporary = target.with_name(f".{target.name}.tmp")

    payload = sealed.canonical_bytes()
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)

    os.replace(temporary, target)
    fsync_directory(layout.root)
    return target


def read_manifest(layout: WorkspaceLayout) -> WorkspaceManifest:
    """Read and parse the manifest without writing anything."""
    path = layout.manifest_path
    if not path.is_file():
        raise ManifestStoreError(f"no workspace manifest at {path}")
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ManifestStoreError(f"manifest is unreadable: {exc}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ManifestStoreError(f"manifest is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ManifestStoreError("manifest must be a JSON object")

    # Before construction, not after: `from_dict` keeps only the keys it knows, so a
    # rejected field would already be gone by the time a model-level check ran.
    raw_result = validate_raw_manifest(data)
    if not raw_result.valid:
        raise ManifestStoreError(
            "refusing an invalid manifest document: " + "; ".join(raw_result.errors)
        )

    try:
        manifest = WorkspaceManifest.from_dict(data)
    except (KeyError, TypeError, ValueError) as exc:
        raise ManifestStoreError(f"manifest is missing required fields: {exc}") from exc

    # Every key the document *does* carry must survive parsing unchanged. Closed keys
    # stop the known way for raw and model to diverge; this stops the rest -- a
    # coerced type, a dropped list entry, anything that makes the bytes on disk mean
    # something other than the model they produced. Integrity is then a claim about
    # the stored document rather than about the reader's interpretation of it.
    #
    # Compared key by key rather than whole-dict. `to_dict` always emits all nine
    # keys, so an equality test silently promoted every optional field to required
    # and refused documents this project's own schema calls valid -- including one
    # carrying exactly the four fields the schema marks `required`. That passed the
    # suite only because `write_manifest` happens to emit every key, so nothing
    # hand-written or produced by another version could be opened at all.
    disagreeing = _disagreeing_paths(data, manifest.to_dict())
    if disagreeing:
        raise ManifestStoreError(
            "manifest does not reconstruct exactly; the stored document and the "
            f"parsed manifest disagree on: {', '.join(disagreeing)}"
        )
    return manifest


def _disagreeing_paths(raw: Any, reconstructed: Any, prefix: str = "") -> list[str]:
    """Paths where the stored document and the parsed manifest differ.

    Only keys the document actually carries are compared, at every level. The models
    serialise every field, including the optional ones they left as `None`, so a
    whole-value equality test would report a document that simply omitted an optional
    key as disagreeing -- which is how an earlier version of this check made all nine
    top-level keys, and `compatibility.max_core_version_exclusive` below them,
    mandatory on disk.

    What it still catches is what it is for: a value that changed on the way through
    `from_dict` -- a coerced type, a dropped list entry, a nested field rewritten.
    """
    if isinstance(raw, dict) and isinstance(reconstructed, dict):
        differences: list[str] = []
        for key in sorted(raw):
            path = f"{prefix}.{key}" if prefix else str(key)
            if key not in reconstructed:
                differences.append(path)
                continue
            differences.extend(_disagreeing_paths(raw[key], reconstructed[key], path))
        return differences

    if isinstance(raw, list) and isinstance(reconstructed, list):
        if len(raw) != len(reconstructed):
            return [f"{prefix}[]"]
        differences = []
        for index, (left, right) in enumerate(zip(raw, reconstructed, strict=True)):
            differences.extend(_disagreeing_paths(left, right, f"{prefix}[{index}]"))
        return differences

    return [] if raw == reconstructed else [prefix or "<document>"]


def inspect_workspace(
    layout: WorkspaceLayout,
    core_version: str,
    *,
    require_database: bool = False,
) -> WorkspaceInspection:
    """Inspect a workspace read-only.

    Performs no write of any kind: no manifest rewrite, no lock file, no database
    open. Compatibility is evaluated from the manifest alone, before any lock or
    lease could be taken, which is what ADR-037 requires.
    """
    manifest = read_manifest(layout)
    problems = tuple(layout.validate(require_database=require_database))
    return WorkspaceInspection(
        manifest=manifest,
        layout_problems=problems,
        integrity_ok=manifest.integrity_matches(),
        compatibility=evaluate_compatibility(manifest, core_version),
    )


def create_workspace(
    root: Path,
    manifest: WorkspaceManifest,
) -> tuple[WorkspaceLayout, Path]:
    """Create the five-path layout and write the manifest atomically."""
    layout = WorkspaceLayout(root=root)
    layout.create_directories()
    path = write_manifest(layout, manifest)
    return layout, path


__all__ = [
    "ManifestStoreError",
    "WorkspaceInspection",
    "create_workspace",
    "inspect_workspace",
    "read_manifest",
    "write_manifest",
]
