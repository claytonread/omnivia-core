"""Pure workspace compatibility evaluation (T-0629A, ADR-037).

Compatibility is decided from declared values alone: no filesystem access, no
lock, no lease. ADR-037 requires an incompatible manifest to fail *before* lease
acquisition, which is only possible if the decision is pure.

The outcome is a typed value, not a bool. A caller needs to distinguish "this Core
is too old for this workspace" from "this workspace format is unknown" because the
remedies differ — upgrade Core versus refuse the workspace entirely — and a bool
throws that away.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from omnivia_core.workspace.manifest import MANIFEST_VERSION, WorkspaceManifest

#: Workspace format versions this Core build can open.
SUPPORTED_WORKSPACE_FORMATS = frozenset({"1"})


class CompatibilityStatus(str, Enum):
    """Why a workspace may or may not be opened by this Core build."""

    COMPATIBLE = "compatible"
    MANIFEST_VERSION_UNSUPPORTED = "manifest_version_unsupported"
    WORKSPACE_FORMAT_UNSUPPORTED = "workspace_format_unsupported"
    CORE_TOO_OLD = "core_too_old"
    CORE_TOO_NEW = "core_too_new"
    MANIFEST_MALFORMED = "manifest_malformed"


@dataclass(frozen=True)
class CompatibilityOutcome:
    """Typed compatibility decision.

    `writable` is deliberately separate from `readable`: a workspace whose format
    this Core understands but whose Core window excludes this build may still be
    safely inspected read-only, and refusing to even read it would make diagnosis
    harder than it needs to be.
    """

    status: CompatibilityStatus
    reason: str
    readable: bool
    writable: bool
    workspace_format_version: str | None = None
    core_version: str | None = None

    @property
    def compatible(self) -> bool:
        return self.status is CompatibilityStatus.COMPATIBLE


def parse_version(value: str) -> tuple[tuple[int, ...], str]:
    """Split a version into its numeric core and its pre-release suffix.

    The suffix is separated before the core is parsed. Parsing `1.0.0-rc.1` by
    splitting on `.` alone would yield a four-component core and rank the release
    candidate *above* `1.0.0`, which is backwards.

    A malformed segment ends the core rather than raising: a version that cannot be
    fully parsed must still be comparable, because this decides a compatibility
    gate and a crash there is worse than a conservative answer.
    """
    core_text = value
    suffix = ""
    for separator in ("-", "+"):
        index = core_text.find(separator)
        if index != -1:
            suffix = core_text[index + 1 :]
            core_text = core_text[:index]
            break

    parts: list[int] = []
    for segment in core_text.split("."):
        head = ""
        for char in segment:
            if not char.isdigit():
                break
            head += char
        if head == "":
            break
        parts.append(int(head))
    return tuple(parts), suffix


def compare_versions(left: str, right: str) -> int:
    """Compare two dotted versions, shorter cores zero-extended.

    Pre-release precedence follows SemVer: `1.0.0-rc.1` is **lower** than `1.0.0`.
    That makes the compatibility gate fail closed — a release candidate does not
    satisfy a `min_core_version` of its own release version, because it may not
    carry everything that release promises.
    """
    (a_core, a_pre), (b_core, b_pre) = parse_version(left), parse_version(right)

    width = max(len(a_core), len(b_core))
    for index in range(width):
        x = a_core[index] if index < len(a_core) else 0
        y = b_core[index] if index < len(b_core) else 0
        if x != y:
            return -1 if x < y else 1

    if a_pre == b_pre:
        return 0
    # Absent suffix outranks any pre-release.
    if a_pre == "":
        return 1
    if b_pre == "":
        return -1
    return -1 if a_pre < b_pre else 1


def evaluate_compatibility(
    manifest: WorkspaceManifest,
    core_version: str,
    *,
    supported_formats: frozenset[str] = SUPPORTED_WORKSPACE_FORMATS,
) -> CompatibilityOutcome:
    """Decide whether `core_version` may open the workspace `manifest` describes."""
    compatibility = manifest.compatibility
    workspace_format = compatibility.workspace_format_version

    if manifest.manifest_version != MANIFEST_VERSION:
        return CompatibilityOutcome(
            status=CompatibilityStatus.MANIFEST_VERSION_UNSUPPORTED,
            reason=(
                f"manifest_version {manifest.manifest_version!r} is not supported; "
                f"this Core understands {MANIFEST_VERSION!r}"
            ),
            readable=False,
            writable=False,
            workspace_format_version=workspace_format,
            core_version=core_version,
        )

    if not workspace_format or not compatibility.min_core_version:
        return CompatibilityOutcome(
            status=CompatibilityStatus.MANIFEST_MALFORMED,
            reason="compatibility block is missing a required version value",
            readable=False,
            writable=False,
            workspace_format_version=workspace_format or None,
            core_version=core_version,
        )

    if workspace_format not in supported_formats:
        return CompatibilityOutcome(
            status=CompatibilityStatus.WORKSPACE_FORMAT_UNSUPPORTED,
            reason=(
                f"workspace format {workspace_format!r} is not supported by this Core "
                f"build; supported: {sorted(supported_formats)}"
            ),
            readable=False,
            writable=False,
            workspace_format_version=workspace_format,
            core_version=core_version,
        )

    if compare_versions(core_version, compatibility.min_core_version) < 0:
        return CompatibilityOutcome(
            status=CompatibilityStatus.CORE_TOO_OLD,
            reason=(
                f"workspace requires Core >= {compatibility.min_core_version}, "
                f"this build is {core_version}"
            ),
            # The format is understood, so read-only inspection stays available.
            readable=True,
            writable=False,
            workspace_format_version=workspace_format,
            core_version=core_version,
        )

    upper = compatibility.max_core_version_exclusive
    if upper is not None and compare_versions(core_version, upper) >= 0:
        return CompatibilityOutcome(
            status=CompatibilityStatus.CORE_TOO_NEW,
            reason=(
                f"workspace excludes Core >= {upper}, this build is {core_version}"
            ),
            readable=True,
            writable=False,
            workspace_format_version=workspace_format,
            core_version=core_version,
        )

    return CompatibilityOutcome(
        status=CompatibilityStatus.COMPATIBLE,
        reason="workspace format and Core version window are satisfied",
        readable=True,
        writable=True,
        workspace_format_version=workspace_format,
        core_version=core_version,
    )


__all__ = [
    "SUPPORTED_WORKSPACE_FORMATS",
    "CompatibilityOutcome",
    "CompatibilityStatus",
    "compare_versions",
    "evaluate_compatibility",
    "parse_version",
]
