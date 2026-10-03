"""Pure semantic rules for managed Skills manifests and their dependency closure (C17).

Structural decoding lives in :mod:`generated`; this module is the layer a JSON Schema cannot be:
what makes a manifest *inert*, what makes its identity, and how a bounded closure of pinned
dependencies resolves. It is standard-library-only, writes no SQL and knows no table, so the
registry's storage, the operation handlers and any other consumer of a manifest apply one rule.

*A manifest grants nothing.* The field set is closed. It names a skill, a version, a
description, inert instruction text, references by digest, dependencies by pinned manifest id,
the roles the skill is compatible with, and the capability identifiers it requires to be
present. There is no member through which a manifest could state a permission, a tool, a
budget, a path, a network right, a credential, an escalation or a sandbox. A manifest that
tries is refused outright rather than stripped, and the common authority-bearing names are
refused under their own code so the refusal says why. This matters here and not only in the
schema because the generated decoders are tolerant by design: they drop an unknown member, so
a hostile one would otherwise vanish without a trace. `required_capabilities` is a statement of
what must be present before execution. It is a requirement, never a grant: what a Run may do
is the bound role's envelope and nothing in this document.

*Identity is content.* A version's `manifest_id` is `skill-` followed by the SHA-256 of the
RFC 8785 canonical bytes of the normalised manifest. Normalisation sorts the unordered
members, so the same skill spelled in a different order is the same manifest, and it refuses a
repeated member rather than choosing between them.

*Dependencies are pinned and the closure is bounded.* A dependency names the exact manifest
id it needs. Resolution walks them to at most :data:`MAX_DEPENDENCY_DEPTH` levels and at most
:data:`MAX_CLOSURE` distinct manifests, refuses a cycle, refuses two different manifests of one
skill in the same closure, and refuses a member the role is not compatible with. A cycle cannot
be published, because an id is a hash of content that contains it; resolution still detects
one, so a closure over a tampered store fails closed instead of looping.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Final

from omnivia_core.contracts.v1.canonical_json import canonical_bytes, canonicalize
from omnivia_core.contracts.v1.compatibility import ContractSemanticError
from omnivia_core.contracts.v1.generated import is_content_checksum, is_identifier

#: The members a manifest has, and the only ones.
SKILL_MANIFEST_FIELDS: Final[tuple[str, ...]] = (
    "skill_name",
    "version",
    "description",
    "instructions",
    "references",
    "dependencies",
    "compatible_roles",
    "required_capabilities",
)
SKILL_REFERENCE_FIELDS: Final[tuple[str, ...]] = ("name", "content_digest")
SKILL_DEPENDENCY_FIELDS: Final[tuple[str, ...]] = ("skill_name", "manifest_id")

#: Names refused under their own code. Any other unknown member is refused too, as malformed;
#: this list exists so a refusal for a member that *tries to grant something* says so.
SKILL_AUTHORITY_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "approval",
        "approvals",
        "authority",
        "budget",
        "budgets",
        "capabilities",
        "command",
        "commands",
        "credential",
        "credentials",
        "env",
        "environment",
        "escalation",
        "exec",
        "filesystem",
        "grant",
        "grants",
        "hooks",
        "mcp",
        "network",
        "paths",
        "permission",
        "permissions",
        "policy",
        "roles",
        "sandbox",
        "scopes",
        "secret",
        "secrets",
        "shell",
        "tool",
        "tool_access",
        "tools",
        "allowed_tools",
    }
)

MAX_DESCRIPTION_CHARS: Final = 1024
MAX_INSTRUCTIONS_CHARS: Final = 16384
MAX_MANIFEST_CHARS: Final = 131072
MAX_REFERENCES: Final = 16
MAX_DEPENDENCIES: Final = 16
MAX_COMPATIBLE_ROLES: Final = 16
MAX_REQUIRED_CAPABILITIES: Final = 16
#: Dependency edges walked from a selected skill, and distinct manifests in one closure.
MAX_DEPENDENCY_DEPTH: Final = 8
MAX_CLOSURE: Final = 32
#: Skills named in one role's selection, roles named in one Run, evidence references on a step.
MAX_SELECTIONS_PER_ROLE: Final = 16
MAX_RUN_ROLES: Final = 8
MAX_EVIDENCE_REFS: Final = 8

SKILL_VERSION_PATTERN: Final = (
    r"^(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})$"
)
SKILL_MANIFEST_ID_PATTERN: Final = r"^skill-[0-9a-f]{64}$"
_VERSION: Final = re.compile(SKILL_VERSION_PATTERN)
_MANIFEST_ID: Final = re.compile(SKILL_MANIFEST_ID_PATTERN)
#: C0 controls other than tab, newline and carriage return, DEL, C1 controls, bidirectional
#: overrides and isolates, and zero-width characters that could hide content from a reviewer.
_HOSTILE: Final = re.compile(
    r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff]"
)

SKILL_SELECTION_EXPLICIT: Final = "explicit"
SKILL_SELECTION_HIGHEST_COMPATIBLE: Final = "highest_compatible"
SKILL_SELECTION_DEPENDENCY: Final = "dependency"
SKILL_SELECTIONS: Final[tuple[str, ...]] = (
    SKILL_SELECTION_EXPLICIT,
    SKILL_SELECTION_HIGHEST_COMPATIBLE,
    SKILL_SELECTION_DEPENDENCY,
)

CODE_AUTHORITY_FIELD: Final = "authority_field_refused"
CODE_MALFORMED: Final = "manifest_malformed"


class SkillManifestError(ContractSemanticError):
    """A manifest that is not an inert, well-formed manifest. `code` says which rule."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def is_skill_manifest_id(value: object) -> bool:
    """Whether `value` is spelled as a manifest id. It says nothing about what it names."""
    return isinstance(value, str) and _MANIFEST_ID.fullmatch(value) is not None


def skill_version_key(version: str) -> tuple[int, int, int]:
    """A version as the integers it orders by. A malformed version is a refusal."""
    match = _VERSION.fullmatch(version) if isinstance(version, str) else None
    if match is None:
        raise SkillManifestError(CODE_MALFORMED, "version is not a skill version")
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def untrusted_text(
    value: object, label: str, *, maximum: int, allow_empty: bool
) -> str:
    """Accept imported text only if it is bounded and hides nothing from a reviewer."""
    if not isinstance(value, str):
        raise SkillManifestError(CODE_MALFORMED, f"{label} must be text")
    if not value and not allow_empty:
        raise SkillManifestError(CODE_MALFORMED, f"{label} must not be empty")
    if len(value) > maximum:
        raise SkillManifestError(
            CODE_MALFORMED, f"{label} is longer than {maximum} characters"
        )
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise SkillManifestError(CODE_MALFORMED, f"{label} is not valid text") from None
    if _HOSTILE.search(value) is not None:
        raise SkillManifestError(
            CODE_MALFORMED,
            f"{label} carries control, bidirectional or zero-width characters",
        )
    return value


def _closed(value: object, fields: tuple[str, ...], label: str) -> Mapping[str, Any]:
    """A mapping with exactly `fields`; an authority-bearing name is refused under its own code."""
    if not isinstance(value, Mapping):
        raise SkillManifestError(CODE_MALFORMED, f"{label} must be an object")
    names = [name for name in value if isinstance(name, str)]
    if len(names) != len(value):
        raise SkillManifestError(
            CODE_MALFORMED, f"{label} has a member that is not named"
        )
    for name in sorted(names):
        if name.lower() in SKILL_AUTHORITY_FIELDS:
            raise SkillManifestError(
                CODE_AUTHORITY_FIELD,
                f"{label} may not declare {name!r}: a skill grants nothing, so permissions, "
                "tools, budgets, paths, network, credentials, escalation and sandbox "
                "settings are not fields of a manifest",
            )
    unknown = sorted(set(names) - set(fields))
    if unknown:
        raise SkillManifestError(
            CODE_MALFORMED, f"{label} has undeclared members: {unknown}"
        )
    missing = [name for name in fields if name not in value]
    if missing:
        raise SkillManifestError(
            CODE_MALFORMED, f"{label} is missing members: {missing}"
        )
    return value


def _array(value: object, label: str, maximum: int) -> Sequence[Any]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise SkillManifestError(CODE_MALFORMED, f"{label} must be an array")
    if len(value) > maximum:
        raise SkillManifestError(
            CODE_MALFORMED, f"{label} has more than {maximum} members"
        )
    return value


def _identifiers(
    value: object, label: str, maximum: int, *, at_least: int
) -> list[str]:
    items = _array(value, label, maximum)
    if len(items) < at_least:
        raise SkillManifestError(
            CODE_MALFORMED, f"{label} needs at least {at_least} member"
        )
    for item in items:
        if not is_identifier(item):
            raise SkillManifestError(
                CODE_MALFORMED, f"{label} has a malformed identifier"
            )
    if len(set(items)) != len(items):
        raise SkillManifestError(CODE_MALFORMED, f"{label} repeats a member")
    return sorted(items)


def validate_skill_manifest(raw: object) -> dict[str, Any]:
    """The manifest in normal form, or a :class:`SkillManifestError` saying why not.

    Total over any input: a value of the wrong type is a refusal, never a `TypeError`. The
    result has sorted unordered members, so it is the one spelling its identity is taken over.
    """
    body = _closed(raw, SKILL_MANIFEST_FIELDS, "the manifest")
    if not is_identifier(body["skill_name"]):
        raise SkillManifestError(CODE_MALFORMED, "skill_name is malformed")
    version = body["version"]
    skill_version_key(version)
    description = untrusted_text(
        body["description"],
        "description",
        maximum=MAX_DESCRIPTION_CHARS,
        allow_empty=False,
    )
    instructions = untrusted_text(
        body["instructions"],
        "instructions",
        maximum=MAX_INSTRUCTIONS_CHARS,
        allow_empty=False,
    )
    references = []
    for item in _array(body["references"], "references", MAX_REFERENCES):
        member = _closed(item, SKILL_REFERENCE_FIELDS, "a reference")
        if not is_identifier(member["name"]) or not is_content_checksum(
            member["content_digest"]
        ):
            raise SkillManifestError(CODE_MALFORMED, "a reference is malformed")
        references.append(
            {"name": member["name"], "content_digest": member["content_digest"]}
        )
    dependencies = []
    for item in _array(body["dependencies"], "dependencies", MAX_DEPENDENCIES):
        member = _closed(item, SKILL_DEPENDENCY_FIELDS, "a dependency")
        if not is_identifier(member["skill_name"]) or not is_skill_manifest_id(
            member["manifest_id"]
        ):
            raise SkillManifestError(CODE_MALFORMED, "a dependency is malformed")
        if member["skill_name"] == body["skill_name"]:
            raise SkillManifestError(CODE_MALFORMED, "a skill cannot depend on itself")
        dependencies.append(
            {"skill_name": member["skill_name"], "manifest_id": member["manifest_id"]}
        )
    for members, key, label in (
        (references, "name", "references"),
        (dependencies, "skill_name", "dependencies"),
    ):
        if len({member[key] for member in members}) != len(members):
            raise SkillManifestError(CODE_MALFORMED, f"{label} repeats a member")
    manifest: dict[str, Any] = {
        "skill_name": body["skill_name"],
        "version": version,
        "description": description,
        "instructions": instructions,
        "references": sorted(references, key=lambda member: member["name"]),
        "dependencies": sorted(dependencies, key=lambda member: member["skill_name"]),
        "compatible_roles": _identifiers(
            body["compatible_roles"],
            "compatible_roles",
            MAX_COMPATIBLE_ROLES,
            at_least=1,
        ),
        "required_capabilities": _identifiers(
            body["required_capabilities"],
            "required_capabilities",
            MAX_REQUIRED_CAPABILITIES,
            at_least=0,
        ),
    }
    if len(canonicalize(manifest)) > MAX_MANIFEST_CHARS:
        raise SkillManifestError(CODE_MALFORMED, "the manifest is too large")
    return manifest


def canonical_skill_manifest(manifest: Mapping[str, Any]) -> str:
    """The canonical text of a manifest, validated and normalised first."""
    return canonicalize(validate_skill_manifest(manifest))


def skill_manifest_id(manifest: Mapping[str, Any]) -> str:
    """`skill-` and the SHA-256 of the canonical manifest: the version's immutable identity."""
    return (
        "skill-"
        + sha256(canonical_bytes(validate_skill_manifest(manifest))).hexdigest()
    )


def skill_manifest_from_canonical(text: str, manifest_id: str) -> dict[str, Any]:
    """A stored manifest, believed only after it is recomputed.

    The text must be the canonical spelling of a valid manifest and must hash to `manifest_id`,
    so a row edited outside the database reads as corrupt rather than as another manifest.
    """
    try:
        manifest = validate_skill_manifest(json.loads(text))
    except ValueError as error:
        if isinstance(error, SkillManifestError):
            raise
        raise SkillManifestError(
            CODE_MALFORMED, "the stored manifest is not JSON"
        ) from error
    if canonicalize(manifest) != text or skill_manifest_id(manifest) != manifest_id:
        raise SkillManifestError(
            CODE_MALFORMED, "the stored manifest does not hash to its id"
        )
    return manifest


# --- dependency closure ----------------------------------------------------------------


class SkillClosureError(ContractSemanticError):
    """A closure that cannot be resolved. `code` names the rule that refused it."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


CODE_DEPENDENCY_CYCLE: Final = "dependency_cycle"
CODE_DEPENDENCY_CONFLICT: Final = "dependency_conflict"
CODE_DEPENDENCY_DEPTH: Final = "dependency_depth_exceeded"
CODE_CLOSURE_TOO_LARGE: Final = "closure_too_large"
CODE_DEPENDENCY_MISSING: Final = "dependency_missing"
CODE_ROLE_INCOMPATIBLE: Final = "role_incompatible"


@dataclass(frozen=True, slots=True)
class ClosureEntry:
    """One manifest of a closure, and why it is there."""

    manifest_id: str
    skill_name: str
    version: str
    selection: str


def resolve_closure(
    roots: Iterable[tuple[str, str]],
    *,
    role_id: str | None,
    load: Callable[[str], Mapping[str, Any] | None],
) -> tuple[ClosureEntry, ...]:
    """The bounded closure of pinned dependencies under `roots`, dependencies first.

    `roots` is `(manifest_id, selection)` in the caller's order. `load` answers the validated
    manifest for an id, or `None` for one that was never published. The order is
    deterministic: a manifest follows everything it depends on, dependencies are visited in
    skill-name order, and roots keep their given order, so one input has one output.

    Refuses a cycle, a pinned dependency that is not published, a chain deeper than
    :data:`MAX_DEPENDENCY_DEPTH`, more than :data:`MAX_CLOSURE` distinct manifests, two
    different manifests of one skill, and any member the role is not compatible with. Each
    refusal is a :class:`SkillClosureError` with its own code. A `role_id` of `None` checks
    the bounds and the pins without a role, which is what publication needs: a version is
    published for every role it declares, and compatibility is judged when a role resolves it.
    """
    ordered: list[ClosureEntry] = []
    by_id: dict[str, ClosureEntry] = {}
    by_name: dict[str, str] = {}
    done: set[str] = set()

    def visit(manifest_id: str, selection: str, path: tuple[str, ...]) -> None:
        if manifest_id in path:
            raise SkillClosureError(
                CODE_DEPENDENCY_CYCLE, "the dependencies form a cycle"
            )
        if len(path) > MAX_DEPENDENCY_DEPTH:
            raise SkillClosureError(
                CODE_DEPENDENCY_DEPTH,
                f"dependencies run more than {MAX_DEPENDENCY_DEPTH} levels deep",
            )
        manifest = load(manifest_id)
        if manifest is None:
            raise SkillClosureError(
                CODE_DEPENDENCY_MISSING,
                "a pinned manifest is not published in this workspace",
            )
        name = manifest["skill_name"]
        held = by_name.get(name)
        if held is not None and held != manifest_id:
            raise SkillClosureError(
                CODE_DEPENDENCY_CONFLICT,
                f"the closure needs two different manifests of skill {name!r}",
            )
        if role_id is not None and role_id not in manifest["compatible_roles"]:
            raise SkillClosureError(
                CODE_ROLE_INCOMPATIBLE,
                f"skill {name!r} is not compatible with the role",
            )
        if manifest_id in done:
            # Reached again through another route: it keeps the strongest reason it was first
            # reached for, so a root that is also someone's dependency stays a root.
            if selection != SKILL_SELECTION_DEPENDENCY and by_id[
                manifest_id
            ].selection == (SKILL_SELECTION_DEPENDENCY):
                held_entry = by_id[manifest_id]
                updated = ClosureEntry(
                    held_entry.manifest_id,
                    held_entry.skill_name,
                    held_entry.version,
                    selection,
                )
                ordered[ordered.index(held_entry)] = updated
                by_id[manifest_id] = updated
            return
        by_name[name] = manifest_id
        for dependency in sorted(
            manifest["dependencies"], key=lambda item: item["skill_name"]
        ):
            visit(
                dependency["manifest_id"],
                SKILL_SELECTION_DEPENDENCY,
                (*path, manifest_id),
            )
        if len(by_id) >= MAX_CLOSURE:
            raise SkillClosureError(
                CODE_CLOSURE_TOO_LARGE,
                f"the closure has more than {MAX_CLOSURE} manifests",
            )
        entry = ClosureEntry(manifest_id, name, manifest["version"], selection)
        by_id[manifest_id] = entry
        ordered.append(entry)
        done.add(manifest_id)

    for manifest_id, selection in roots:
        visit(manifest_id, selection, ())
    return tuple(ordered)


def closure_digest(entries: Iterable[ClosureEntry]) -> str:
    """The `sha256:` digest of a closure's exact manifest ids, in order."""
    ids = [entry.manifest_id for entry in entries]
    return "sha256:" + sha256(canonical_bytes(ids)).hexdigest()


__all__ = [
    "CODE_AUTHORITY_FIELD",
    "CODE_CLOSURE_TOO_LARGE",
    "CODE_DEPENDENCY_CONFLICT",
    "CODE_DEPENDENCY_CYCLE",
    "CODE_DEPENDENCY_DEPTH",
    "CODE_DEPENDENCY_MISSING",
    "CODE_MALFORMED",
    "CODE_ROLE_INCOMPATIBLE",
    "MAX_CLOSURE",
    "MAX_COMPATIBLE_ROLES",
    "MAX_DEPENDENCIES",
    "MAX_DEPENDENCY_DEPTH",
    "MAX_DESCRIPTION_CHARS",
    "MAX_EVIDENCE_REFS",
    "MAX_INSTRUCTIONS_CHARS",
    "MAX_MANIFEST_CHARS",
    "MAX_REFERENCES",
    "MAX_REQUIRED_CAPABILITIES",
    "MAX_RUN_ROLES",
    "MAX_SELECTIONS_PER_ROLE",
    "SKILL_AUTHORITY_FIELDS",
    "SKILL_DEPENDENCY_FIELDS",
    "SKILL_MANIFEST_FIELDS",
    "SKILL_MANIFEST_ID_PATTERN",
    "SKILL_REFERENCE_FIELDS",
    "SKILL_SELECTIONS",
    "SKILL_SELECTION_DEPENDENCY",
    "SKILL_SELECTION_EXPLICIT",
    "SKILL_SELECTION_HIGHEST_COMPATIBLE",
    "SKILL_VERSION_PATTERN",
    "ClosureEntry",
    "SkillClosureError",
    "SkillManifestError",
    "canonical_skill_manifest",
    "closure_digest",
    "is_skill_manifest_id",
    "resolve_closure",
    "skill_manifest_from_canonical",
    "skill_manifest_id",
    "skill_version_key",
    "untrusted_text",
    "validate_skill_manifest",
]
