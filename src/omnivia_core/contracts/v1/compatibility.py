"""Pure version and capability compatibility semantics for Application Contract v1 (ADR-038).

Structural decoding and encoding live in :mod:`generated` and :mod:`codec`.
This module owns the semantics that hold regardless of wire representation:
version-window membership, capability-set intersection, and requirement
resolution. Nothing here parses or produces JSON; callers pass and receive
the generated dataclasses.

Standard library only. Nothing here may depend on runtime, storage, HTTP, MCP,
CLI, Platform, Dev, or a validation framework.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from omnivia_core.contracts.v1.generated import (
    COMPATIBILITY_STATUS_COMPATIBLE,
    COMPATIBILITY_STATUS_INCOMPATIBLE,
    COMPATIBILITY_STATUS_UPGRADE_REQUIRED,
    CONTRACT_VERSION_PATTERN,
    CapabilityRef,
    CapabilityRequirement,
    CapabilitySet,
    ContractDecodeError,
    GrantedAuthority,
    VersionCapabilityEnvelope,
    VersionWindow,
)

__all__ = [
    "CapabilityResolution",
    "ContractSemanticError",
    "capability_key",
    "classify_version_compatibility",
    "compare_contract_versions",
    "duplicate_capability_ids",
    "effective_capabilities",
    "parse_contract_version",
    "resolve_capability_requirements",
    "validate_capability_set",
    "validate_granted_authority",
    "validate_version_capability_envelope",
    "validate_version_window",
    "version_in_window",
]


class ContractSemanticError(ContractDecodeError):
    """Raised when a decoded contract value violates a v1 semantic invariant.

    Structural decoding (:mod:`generated`) only rejects a payload that is the
    wrong shape. This is the layer above it: a payload can be perfectly well
    formed and still assert something that cannot be true, such as an
    ``effective`` capability list that is not actually the intersection of
    ``supported`` and ``granted``, or a version window whose ``minimum``
    exceeds its ``maximum``.

    This is a :class:`ContractDecodeError` (and so, transitively, a
    ``ValueError``): a caller that only wants to catch "this payload was
    unusable" does not need to know whether the rejection happened at the
    structural or the semantic layer.
    """


# ``ContractVersion``'s grammar, taken from the generated constant rather than
# restated here, so this parser and the schema can never drift apart.
_CONTRACT_VERSION_RE = re.compile(CONTRACT_VERSION_PATTERN)


def parse_contract_version(version: str) -> tuple[int, int]:
    """Parse a `major.minor` ContractVersion string into a comparable tuple.

    The accepted spelling is exactly :data:`CONTRACT_VERSION_PATTERN`: two
    decimal components, each either ``0`` or an unpadded positive integer.
    ``ContractVersion`` is only a string on the wire, so this is the layer that
    decides what counts as one, and it must decide it the same way the schema
    does -- hence matching the generated pattern instead of re-deriving the rule.

    Anything outside that grammar is rejected rather than read for whatever
    number Python could find in it. That matters most for the spellings that
    *look* parseable: ``"01.2"`` and ``"1.02"`` are noncanonical forms of a real
    version, and a component written in non-ASCII digits (``"١.٢"``) is one
    ``int`` would accept outright. Tolerating any of them would leave two peers
    holding values they each read as ``(1, 2)`` but neither can match against
    the other's spelling, so the only safe answer is that a noncanonical
    spelling is not a ``ContractVersion`` at all.

    Raises ``ValueError`` on any such input rather than guessing at a value.
    """
    match = _CONTRACT_VERSION_RE.fullmatch(version)
    if match is None:
        raise ValueError(f"{version!r} is not a `major.minor` contract version")
    return int(match.group(1)), int(match.group(2))


def compare_contract_versions(a: str, b: str) -> int:
    """Compare two ContractVersion strings: negative, zero, or positive like ``cmp``."""
    left, right = parse_contract_version(a), parse_contract_version(b)
    return (left > right) - (left < right)


def _require_contract_version(version: str, label: str) -> tuple[int, int]:
    """Parse ``version``, reporting a malformed value as a semantic failure.

    ``ContractVersion`` is only a string on the wire, so structural decoding
    cannot tell ``"1.2"`` apart from ``"9"`` or ``"latest"``. A value that does
    not parse cannot be compared against anything, so every semantic rule that
    depends on it is unenforceable -- that is a rejected payload, not a
    ``ValueError`` escaping from an internal helper.
    """
    try:
        return parse_contract_version(version)
    except ValueError as error:
        raise ContractSemanticError(f"{label}: {error}") from error


def validate_version_window(window: VersionWindow) -> None:
    """Raise unless ``window.minimum`` is at or below ``window.maximum``.

    A reversed window (``minimum`` above ``maximum``) is malformed input, not a
    window that simply matches nothing: passing it through would make every
    membership and classification query silently return an empty answer
    instead of surfacing the caller's mistake. A bound that does not parse as a
    ``ContractVersion`` at all is rejected the same way, and for the same
    reason.
    """
    if _require_contract_version(window.minimum, "version window minimum") > (
        _require_contract_version(window.maximum, "version window maximum")
    ):
        raise ContractSemanticError(
            f"version window is reversed: minimum {window.minimum!r} exceeds "
            f"maximum {window.maximum!r}"
        )


def version_in_window(version: str, window: VersionWindow) -> bool:
    """Return whether ``version`` falls within the inclusive ``[minimum, maximum]`` window."""
    validate_version_window(window)
    parsed = parse_contract_version(version)
    lower = parse_contract_version(window.minimum)
    upper = parse_contract_version(window.maximum)
    return lower <= parsed <= upper


def classify_version_compatibility(version: str, window: VersionWindow) -> str:
    """Classify a requested contract version against a peer's supported window.

    A different major version can never be negotiated (``incompatible``): major
    changes are breaking by definition, so there is no safe way to interpret a
    document written against a major version outside the peer's window. The
    same major version outside the supported minor range needs an upgrade
    (``upgrade_required``). Inside the window is ``compatible``. Deprecations
    are layered on separately by the caller (``compatible_with_deprecations``
    is a caller-side refinement of ``compatible``, not computed here).
    """
    validate_version_window(window)
    requested_major, _ = parse_contract_version(version)
    min_major, _ = parse_contract_version(window.minimum)
    max_major, _ = parse_contract_version(window.maximum)
    if requested_major < min_major or requested_major > max_major:
        return COMPATIBILITY_STATUS_INCOMPATIBLE
    if version_in_window(version, window):
        return COMPATIBILITY_STATUS_COMPATIBLE
    return COMPATIBILITY_STATUS_UPGRADE_REQUIRED


def capability_key(ref: CapabilityRef) -> tuple[str, str]:
    """The ``(id, version)`` identity of a capability reference."""
    return (ref.id, ref.version)


def duplicate_capability_ids(refs: Sequence[CapabilityRef]) -> tuple[str, ...]:
    """Return capability ids that appear more than once, in first-seen order.

    A capability set naming the same id at two versions is ambiguous: nothing
    downstream can tell which version is actually in force, so this must fail
    rather than pick one silently.
    """
    counts: dict[str, int] = {}
    for ref in refs:
        counts[ref.id] = counts.get(ref.id, 0) + 1
    duplicates: list[str] = []
    for ref in refs:
        if counts[ref.id] > 1 and ref.id not in duplicates:
            duplicates.append(ref.id)
    return tuple(duplicates)


def effective_capabilities(
    supported: Sequence[CapabilityRef], granted: Sequence[CapabilityRef]
) -> tuple[CapabilityRef, ...]:
    """Compute the capabilities actually usable: ``supported`` intersected with ``granted``.

    Intersection is by exact ``(id, version)`` pair: a capability granted at a
    version the server does not implement is not effective, and a capability
    the server implements but did not grant is not effective either. Order
    follows ``supported``; the result is never wider than either input.
    """
    granted_keys = {capability_key(ref) for ref in granted}
    return tuple(ref for ref in supported if capability_key(ref) in granted_keys)


@dataclass(frozen=True, slots=True)
class CapabilityResolution:
    """The outcome of resolving a caller's capability requirements against what is effective."""

    unmet_required: tuple[CapabilityRequirement, ...]
    unmet_optional: tuple[CapabilityRequirement, ...]

    @property
    def satisfied(self) -> bool:
        """True when every *required* requirement is met.

        Unmet optional requirements mean the caller degrades; they never fail
        the request on their own.
        """
        return not self.unmet_required


def resolve_capability_requirements(
    requirements: Sequence[CapabilityRequirement],
    effective: Sequence[CapabilityRef],
) -> CapabilityResolution:
    """Resolve requirements against effective capabilities.

    A requirement is met when ``effective`` contains its id at a version with
    the *same major* as ``minimum_version`` and a value at or above it. A major
    version is a breaking change by definition, so an effective version at a
    higher major can never satisfy a requirement pinned to a lower major: there
    is no safe way to assume the newer major still behaves as required. An id
    absent from ``effective`` at every matching-major version is always unmet.
    """
    versions_by_id: dict[str, list[str]] = {}
    for ref in effective:
        versions_by_id.setdefault(ref.id, []).append(ref.version)

    unmet_required: list[CapabilityRequirement] = []
    unmet_optional: list[CapabilityRequirement] = []
    for requirement in requirements:
        versions = versions_by_id.get(requirement.id, ())
        required_major, _ = parse_contract_version(requirement.minimum_version)
        met = any(
            parse_contract_version(version)[0] == required_major
            and compare_contract_versions(version, requirement.minimum_version) >= 0
            for version in versions
        )
        if not met:
            bucket = unmet_required if requirement.required else unmet_optional
            bucket.append(requirement)

    return CapabilityResolution(
        unmet_required=tuple(unmet_required),
        unmet_optional=tuple(unmet_optional),
    )


def validate_capability_set(capabilities: CapabilitySet) -> None:
    """Raise unless ``capabilities`` is an internally consistent ``CapabilitySet``.

    Two invariants are enforced, both required for ``effective`` to mean what
    callers rely on it meaning:

    - none of ``supported``, ``granted``, or ``effective`` names the same
      capability id more than once, since a duplicate id makes it ambiguous
      which version is actually in force;
    - ``effective`` is *exactly* the ``(id, version)`` intersection of
      ``supported`` and ``granted`` -- no wider (a claimed capability neither
      side agrees on) and no narrower (a capability both sides agree on but
      that was silently dropped).
    """
    for field_name, refs in (
        ("supported", capabilities.supported),
        ("granted", capabilities.granted),
        ("effective", capabilities.effective),
    ):
        duplicates = duplicate_capability_ids(refs)
        if duplicates:
            raise ContractSemanticError(
                f"{field_name}: duplicate capability id(s) {duplicates!r}"
            )

    expected = {
        capability_key(ref)
        for ref in effective_capabilities(capabilities.supported, capabilities.granted)
    }
    actual = {capability_key(ref) for ref in capabilities.effective}
    if actual != expected:
        raise ContractSemanticError(
            "effective must equal the exact id+version intersection of supported and granted"
        )


def validate_granted_authority(
    authority: GrantedAuthority, capabilities: CapabilitySet
) -> None:
    """Raise unless trusted authority stays within effective negotiated authority.

    ``GrantedAuthority`` is the only authority statement a client may trust, and
    ``capabilities.effective`` is the whole of what this exchange actually
    negotiated. Authority naming a capability that is not effective is
    therefore claiming more than was granted -- the same authority-widening
    failure :func:`validate_capability_set` guards ``effective`` itself against,
    one level up. Nothing structural stops it: ``authority.capabilities`` and
    ``capabilities.effective`` are independent lists on the wire.

    Identity is the exact ``(id, version)`` pair, as everywhere else in this
    module: authority held at a version that was never made effective is as
    unnegotiated as an id that never appeared at all. Ids must also be unique
    within the authority set, for the same reason they must be in a
    ``CapabilitySet`` -- a repeated id makes it ambiguous which version is in
    force.

    An empty authority capability list is always valid, including on an error or
    incompatible response: a response may legitimately establish no authority at
    all, and claiming nothing can never claim too much.
    """
    duplicates = duplicate_capability_ids(authority.capabilities)
    if duplicates:
        raise ContractSemanticError(
            f"authority capabilities: duplicate capability id(s) {duplicates!r}"
        )

    effective = {capability_key(ref) for ref in capabilities.effective}
    exceeded = tuple(
        capability_key(ref)
        for ref in authority.capabilities
        if capability_key(ref) not in effective
    )
    if exceeded:
        raise ContractSemanticError(
            "authority capabilities must not exceed the effective negotiated "
            f"capabilities: {list(exceeded)!r} not effective"
        )


def validate_version_capability_envelope(envelope: VersionCapabilityEnvelope) -> None:
    """Raise unless ``envelope`` is an internally consistent ``VersionCapabilityEnvelope``.

    This is the one call a caller needs to enforce every semantic invariant
    ``VersionCapabilityEnvelope`` carries.

    An envelope states each contract version twice: once as the version in
    force on this document (``api_version`` / ``workspace_format_version``) and
    once as the version negotiation selected (``compatibility.selected_*``). A
    caller reading one and a caller reading the other must reach the same
    answer, so the two must agree exactly, both must parse as a
    ``ContractVersion``, and the selected version must actually fall inside the
    window the same envelope publishes as supported. An envelope that says
    ``api_version`` ``"9.9"`` while selecting ``"1.2"``, or that selects a
    version its own supported window excludes, is asserting something that
    cannot be true -- rejecting it here is what stops the contradiction from
    reaching a caller that only reads one of the two fields.

    On top of that, the capability set cannot assert wider authority than
    ``supported`` and ``granted`` actually agree on
    (:func:`validate_capability_set`).

    ``compatibility.status`` is deliberately *not* constrained here.
    ``CompatibilityStatus`` is an open vocabulary: a newer peer may negotiate a
    status this build has never seen, and a build that rejected it would break
    forward compatibility for a value the frozen invariants say nothing about.
    """
    negotiated = envelope.compatibility
    for in_force_field, in_force, selected_field, selected, window_field, window in (
        (
            "api_version",
            envelope.api_version,
            "selected_api_version",
            negotiated.selected_api_version,
            "supported_api_versions",
            negotiated.supported_api_versions,
        ),
        (
            "workspace_format_version",
            envelope.workspace_format_version,
            "selected_workspace_version",
            negotiated.selected_workspace_version,
            "supported_workspace_versions",
            negotiated.supported_workspace_versions,
        ),
    ):
        validate_version_window(window)
        _require_contract_version(in_force, in_force_field)
        _require_contract_version(selected, f"compatibility.{selected_field}")
        if in_force != selected:
            raise ContractSemanticError(
                f"{in_force_field} {in_force!r} disagrees with "
                f"compatibility.{selected_field} {selected!r}"
            )
        if not version_in_window(selected, window):
            raise ContractSemanticError(
                f"compatibility.{selected_field} {selected!r} falls outside the declared "
                f"{window_field} window [{window.minimum!r}, {window.maximum!r}]"
            )

    validate_capability_set(envelope.capabilities)
