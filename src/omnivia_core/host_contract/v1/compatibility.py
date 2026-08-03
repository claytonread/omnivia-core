"""Host Contract v1 compatibility, version negotiation and migration.

The approved compatibility-and-migration policy, expressed as pure functions
over the governed records:

- **negotiation** -- Semantic Versioning within one major. Hosts and consumers
  advertise a supported major plus a minimum and maximum minor; negotiation
  selects the highest mutually supported minor. No mutual major fails with
  ``unsupported_contract_version`` before the operation runs, never after it;
- **additive-minor limits** -- a minor release may add optional capabilities,
  but may not make an optional capability required for a target classification
  it already supported, nor drop a supported target;
- **target compatibility** -- a declaration plus what a target actually offers
  produces a structured :class:`~...generated.CompatibilityResult`. A missing
  *required* capability can never be ``compatible``; a missing *optional* one
  degrades only where the declaration states the degradation behaviour;
- **migration** -- ``app-shell-host-context.v0`` is a display-safe fixture
  contract. A bounded adapter may carry its display fields into v1 presentation
  data; it may never synthesize principal, Workspace, permission, entitlement
  or environment authority from them.

Standard library only. Nothing here may depend on runtime, storage, HTTP, MCP,
CLI, Platform, Dev, or a validation framework.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Final, cast

from omnivia_core.host_contract.v1.generated import (
    HOST_CONTRACT_MAJOR,
    HOST_CONTRACT_VERSION,
    IDENTIFIER_PATTERN,
    SEM_VER_PATTERN,
    TARGETS,
    CompatibilityResult,
    HostContractDecodeError,
)

__all__ = [
    "COMPATIBILITY_STAGES",
    "SUPPORTED_CONTRACT",
    "TARGET_CLASSIFICATIONS",
    "UNSUPPORTED_CONTRACT_VALUE",
    "UNSUPPORTED_CONTRACT_VERSION",
    "V0_DISPLAY_CONTRACT",
    "V0_PERMITTED_MAPPING",
    "ContractCompatibilityError",
    "ContractNegotiationError",
    "ContractSupport",
    "ContractVersionError",
    "MigrationPlan",
    "TargetDeclaration",
    "TargetOffering",
    "can_roll_back_to",
    "evaluate_compatibility",
    "is_host_contract_instance",
    "is_same_major",
    "load_migration_plan",
    "migrate_v0_display_context",
    "negotiate_contract_version",
    "parse_contract_version",
    "require_supported_version",
    "validate_additive_minor",
]

#: The error codes the governed policy names for fail-closed refusals.
UNSUPPORTED_CONTRACT_VERSION: Final[str] = "unsupported_contract_version"
UNSUPPORTED_CONTRACT_VALUE: Final[str] = "unsupported_contract_value"

#: Target classification vocabulary. A declaration classifies itself; this is
#: distinct from the runtime ``target`` a request or result reports.
TARGET_CLASSIFICATIONS: Final[tuple[str, ...]] = (
    "universal",
    "desktop",
    "cloud",
    "hybrid",
    "solution_multi_target",
)

#: The lifecycle points at which compatibility is evaluated. Launch re-evaluates
#: policy and health; it never rewrites the immutable Release.
COMPATIBILITY_STAGES: Final[tuple[str, ...]] = (
    "authoring",
    "validation",
    "packaging",
    "installation",
    "launch",
)

_SEMVER_RE: Final[re.Pattern[str]] = re.compile(SEM_VER_PATTERN)
_IDENTIFIER_RE: Final[re.Pattern[str]] = re.compile(IDENTIFIER_PATTERN)


class ContractVersionError(Exception):
    """Raised when a version string or support window is not well formed."""


class ContractCompatibilityError(Exception):
    """Raised when a declaration or migration breaks a governed compatibility rule."""


class ContractNegotiationError(Exception):
    """Raised when two peers cannot agree on a Host Contract version.

    Carries the governed refusal ``code`` so a caller reports the same value
    the policy names rather than re-deriving one from the message.
    """

    def __init__(self, message: str, code: str = UNSUPPORTED_CONTRACT_VERSION) -> None:
        super().__init__(message)
        self.code = code


# --------------------------------------------------------------------------
# Versions
# --------------------------------------------------------------------------


def parse_contract_version(text: str) -> tuple[int, int, int]:
    """Parse a Semantic Version into ``(major, minor, patch)``.

    The canonical schema's own SemVer pattern is the accepted grammar, so a
    pre-release, a build tag, a leading zero or a missing component is refused
    here exactly as it would be at the schema boundary.
    """
    if not isinstance(text, str) or _SEMVER_RE.fullmatch(text) is None:
        raise ContractVersionError(f"{text!r} is not a Semantic Version")
    major, minor, patch = (int(part) for part in text.split("."))
    return major, minor, patch


def is_same_major(version: str) -> bool:
    """Return whether a version shares this build's Host Contract major."""
    return parse_contract_version(version)[0] == HOST_CONTRACT_MAJOR


@dataclass(frozen=True, slots=True)
class ContractSupport:
    """One peer's advertised support: a major plus a minimum and maximum minor."""

    major: int
    minimum_minor: int
    maximum_minor: int

    def validate(self) -> None:
        """Raise unless the window is well formed."""
        components = (self.major, self.minimum_minor, self.maximum_minor)
        if any(type(component) is not int or component < 0 for component in components):
            raise ContractVersionError("a support window uses non-negative components only")
        if self.minimum_minor > self.maximum_minor:
            raise ContractVersionError(
                f"inverted support window: minimum minor {self.minimum_minor} "
                f"exceeds maximum minor {self.maximum_minor}"
            )


#: What this build speaks. Host Contract 1.0.0 is the only approved version.
SUPPORTED_CONTRACT: Final[ContractSupport] = ContractSupport(
    major=HOST_CONTRACT_MAJOR, minimum_minor=0, maximum_minor=0
)


def negotiate_contract_version(host: ContractSupport, consumer: ContractSupport) -> str:
    """Return the highest mutually supported version, or fail before any operation.

    Negotiation never crosses a major: a major change may remove or reinterpret
    fields, operations or enum values, so two peers on different majors have no
    safe common ground to downgrade to.
    """
    host.validate()
    consumer.validate()
    if host.major != consumer.major:
        raise ContractNegotiationError(
            f"no mutual Host Contract major: host speaks {host.major}, consumer speaks {consumer.major}"
        )
    minor = min(host.maximum_minor, consumer.maximum_minor)
    if minor < max(host.minimum_minor, consumer.minimum_minor):
        raise ContractNegotiationError(
            f"no mutually supported minor within major {host.major}: "
            f"host {host.minimum_minor}-{host.maximum_minor}, "
            f"consumer {consumer.minimum_minor}-{consumer.maximum_minor}"
        )
    return f"{host.major}.{minor}.0"


def require_supported_version(version: str) -> str:
    """Return ``version`` when this build can speak it, else fail closed."""
    major, minor, _patch = parse_contract_version(version)
    if major != SUPPORTED_CONTRACT.major:
        raise ContractNegotiationError(
            f"Host Contract {version} is major {major}; this build speaks major "
            f"{SUPPORTED_CONTRACT.major} ({HOST_CONTRACT_VERSION})"
        )
    if not SUPPORTED_CONTRACT.minimum_minor <= minor <= SUPPORTED_CONTRACT.maximum_minor:
        raise ContractNegotiationError(
            f"Host Contract {version} is outside this build's supported minor window "
            f"{SUPPORTED_CONTRACT.minimum_minor}-{SUPPORTED_CONTRACT.maximum_minor}"
        )
    return version


def can_roll_back_to(release_version: str, adapter: ContractSupport) -> bool:
    """Return whether an adapter can serve a previously verified Release.

    Release rollback selects a package that was already verified; the contract
    half of that decision is whether the selected Release and the Host Adapter
    still share a supported major and minor window.
    """
    adapter.validate()
    try:
        major, minor, _patch = parse_contract_version(release_version)
    except ContractVersionError:
        return False
    return major == adapter.major and adapter.minimum_minor <= minor <= adapter.maximum_minor


# --------------------------------------------------------------------------
# Target declarations and compatibility
# --------------------------------------------------------------------------


def _frozen_mapping(value: Mapping[str, str]) -> Mapping[str, str]:
    return MappingProxyType(dict(value))


@dataclass(frozen=True, slots=True)
class TargetDeclaration:
    """What an artefact says it needs, and where it says it runs.

    ``degradation`` maps an *optional* capability to the declared, tested
    behaviour when it is absent. A capability with no entry has no declared
    degradation, so its absence cannot yield ``compatible_with_degradation``.
    Whether that declared behaviour was actually tested is a release-process
    fact this contract records rather than proves.
    """

    supported_targets: tuple[str, ...]
    required_capabilities: tuple[str, ...] = ()
    optional_capabilities: tuple[str, ...] = ()
    degradation: Mapping[str, str] = field(default_factory=dict)
    classification: str = "universal"

    def validate(self) -> None:
        """Raise unless every declared value is in a governed vocabulary."""
        if type(self) is not TargetDeclaration:
            raise ContractCompatibilityError("malformed target declaration")
        if type(self.classification) is not str:
            raise ContractCompatibilityError("malformed target declaration")
        if self.classification not in TARGET_CLASSIFICATIONS:
            raise ContractCompatibilityError(
                f"{UNSUPPORTED_CONTRACT_VALUE}: target classification {self.classification!r}"
            )
        _require_identifier_tuple(self.required_capabilities, "required capabilities")
        _require_identifier_tuple(self.optional_capabilities, "optional capabilities")
        if type(self.supported_targets) is not tuple or any(
            type(target) is not str for target in self.supported_targets
        ):
            raise ContractCompatibilityError("malformed supported targets")
        if len(set(self.supported_targets)) != len(self.supported_targets):
            raise ContractCompatibilityError("supported targets repeat a value")
        for target in self.supported_targets:
            if target not in TARGETS:
                raise ContractCompatibilityError(
                    f"{UNSUPPORTED_CONTRACT_VALUE}: target {target!r}"
                )
        _require_string_mapping(self.degradation, "degradation", values_are_identifiers=False)
        overlap = sorted(set(self.required_capabilities) & set(self.optional_capabilities))
        if overlap:
            raise ContractCompatibilityError(
                f"capabilities declared both required and optional: {overlap}"
            )
        undeclared = sorted(set(self.degradation) - set(self.optional_capabilities))
        if undeclared:
            raise ContractCompatibilityError(
                f"degradation declared for non-optional capabilities: {undeclared}"
            )


@dataclass(frozen=True, slots=True)
class TargetOffering:
    """What one target actually offers, at the moment it is asked.

    Health and policy are part of this rather than of the declaration, which is
    why launch can re-evaluate compatibility without rewriting the immutable
    Release: only the offering changes.
    """

    target: str
    available: frozenset[str] = frozenset()
    after_configuration: Mapping[str, str] = field(default_factory=dict)
    through_companion: Mapping[str, str] = field(default_factory=dict)
    policy_blocked: frozenset[str] = frozenset()

    def validate(self) -> None:
        """Raise unless the offering names a governed target."""
        if type(self) is not TargetOffering or type(self.target) is not str:
            raise ContractCompatibilityError("malformed target offering")
        if self.target not in TARGETS:
            raise ContractCompatibilityError(f"{UNSUPPORTED_CONTRACT_VALUE}: target {self.target!r}")
        _require_identifier_frozenset(self.available, "available capabilities")
        _require_identifier_frozenset(self.policy_blocked, "policy-blocked capabilities")
        _require_string_mapping(
            self.after_configuration, "configuration routes", values_are_identifiers=True
        )
        _require_string_mapping(
            self.through_companion, "companion routes", values_are_identifiers=True
        )


def _is_identifier(value: object) -> bool:
    return (
        type(value) is str
        and len(value) <= 128
        and _IDENTIFIER_RE.fullmatch(value) is not None
    )


def _require_identifier_tuple(value: object, label: str) -> None:
    if type(value) is not tuple or any(not _is_identifier(member) for member in value):
        raise ContractCompatibilityError(f"malformed {label}")
    if len(set(value)) != len(value):
        raise ContractCompatibilityError(f"{label} repeat a value")


def _require_identifier_frozenset(value: object, label: str) -> None:
    if type(value) is not frozenset or any(not _is_identifier(member) for member in value):
        raise ContractCompatibilityError(f"malformed {label}")


def _require_string_mapping(
    value: object, label: str, *, values_are_identifiers: bool
) -> None:
    if type(value) not in (dict, MappingProxyType):
        raise ContractCompatibilityError(f"malformed {label}")
    for key, member in cast(Mapping[object, object], value).items():
        if not _is_identifier(key):
            raise ContractCompatibilityError(f"malformed {label}")
        if values_are_identifiers:
            valid_member = _is_identifier(member)
        else:
            valid_member = type(member) is str and 1 <= len(member) <= 500
        if not valid_member:
            raise ContractCompatibilityError(f"malformed {label}")


def validate_additive_minor(previous: TargetDeclaration, following: TargetDeclaration) -> None:
    """Raise unless ``following`` is a lawful additive-minor successor to ``previous``.

    A minor release adds; it does not tighten. Newly requiring a capability --
    whether it was previously optional or absent entirely -- would break an
    installation that already satisfied the older declaration, and dropping a
    supported target would strand one.
    """
    previous.validate()
    following.validate()
    dropped = [
        target for target in previous.supported_targets if target not in following.supported_targets
    ]
    if dropped:
        raise ContractCompatibilityError(
            f"a minor release may not drop supported target(s) {dropped}"
        )
    if previous.classification != following.classification:
        raise ContractCompatibilityError("a minor release may not change target classification")
    removed_required = sorted(
        set(previous.required_capabilities) - set(following.required_capabilities)
    )
    if removed_required:
        raise ContractCompatibilityError(
            f"a minor release may not remove required capability {removed_required}"
        )
    removed_optional = sorted(
        set(previous.optional_capabilities) - set(following.optional_capabilities)
    )
    if removed_optional:
        raise ContractCompatibilityError(
            f"a minor release may not remove optional capability {removed_optional}"
        )
    changed_degradation = sorted(
        capability
        for capability, behaviour in previous.degradation.items()
        if following.degradation.get(capability) != behaviour
    )
    if changed_degradation:
        raise ContractCompatibilityError(
            f"a minor release may not remove or change degradation behaviour {changed_degradation}"
        )
    newly_required = [
        capability
        for capability in following.required_capabilities
        if capability not in previous.required_capabilities
    ]
    if newly_required:
        raise ContractCompatibilityError(
            f"a minor release may not newly require capability {newly_required}"
        )


def evaluate_compatibility(
    declaration: TargetDeclaration, offering: TargetOffering
) -> CompatibilityResult:
    """Evaluate a declaration against what a target offers.

    Outcomes are decided in a fixed precedence, most restrictive first, so a
    result never reports a lesser problem while a greater one stands:

    1. a policy block on any declared capability -- ``blocked_by_policy``;
    2. the target is not one the declaration supports, or a required capability
       is absent with no configuration or companion route -- ``incompatible``;
    3. a required capability reachable only through a companion --
       ``requires_companion``;
    4. a required capability reachable only after configuration --
       ``compatible_after_configuration``;
    5. a missing optional capability with declared degradation --
       ``compatible_with_degradation``; without one, ``incompatible``;
    6. otherwise ``compatible``.

    ``companions`` and ``requiredConfiguration`` name *routes*, not capabilities,
    and one companion or one settings route routinely supplies several
    capabilities. Both arrays are ``uniqueItems`` in the canonical schema, so a
    route reached by more than one capability is listed once, in the order it was
    first reached. Naming it twice would add nothing and would make the result
    unencodable.
    """
    declaration.validate()
    offering.validate()

    declared = (*declaration.required_capabilities, *declaration.optional_capabilities)
    policy_blocks = tuple(
        capability for capability in declared if capability in offering.policy_blocked
    )

    required_missing: list[str] = []
    required_configuration: list[str] = []
    companions: list[str] = []
    for capability in declaration.required_capabilities:
        if capability in offering.policy_blocked:
            continue
        if capability in offering.available:
            continue
        if capability in offering.through_companion:
            companions.append(offering.through_companion[capability])
            continue
        if capability in offering.after_configuration:
            required_configuration.append(offering.after_configuration[capability])
            continue
        required_missing.append(capability)

    optional_missing = tuple(
        capability
        for capability in declaration.optional_capabilities
        if capability not in offering.available and capability not in offering.policy_blocked
    )
    undeclared_degradation = [
        capability for capability in optional_missing if capability not in declaration.degradation
    ]
    safe_reasons = tuple(
        declaration.degradation[capability]
        for capability in optional_missing
        if capability in declaration.degradation
    )

    if policy_blocks:
        status = "blocked_by_policy"
    elif offering.target not in declaration.supported_targets or required_missing:
        status = "incompatible"
    elif companions:
        status = "requires_companion"
    elif required_configuration:
        status = "compatible_after_configuration"
    elif undeclared_degradation:
        status = "incompatible"
    elif optional_missing:
        status = "compatible_with_degradation"
    else:
        status = "compatible"

    return CompatibilityResult(
        status=status,
        target=offering.target,
        required_missing=tuple(required_missing),
        optional_missing=optional_missing,
        policy_blocks=policy_blocks,
        required_configuration=tuple(dict.fromkeys(required_configuration)),
        companions=tuple(dict.fromkeys(companions)),
        safe_reasons=safe_reasons,
    )


# --------------------------------------------------------------------------
# v0 display-context migration
# --------------------------------------------------------------------------

#: The prior display-safe fixture contract. It is not Host Contract v1 and does
#: not become it by being mapped.
V0_DISPLAY_CONTRACT: Final[str] = "app-shell-host-context.v0"

#: The display fields the governed migration plan permits an adapter to map.
V0_PERMITTED_MAPPING: Final[tuple[str, ...]] = (
    "app_id",
    "app_name",
    "runtime_state",
    "sources",
    "last_updated",
)


@dataclass(frozen=True, slots=True)
class MigrationPlan:
    """A bounded mapping plan from a prior contract into v1 presentation data.

    A plan describes what an adapter may carry across and what it may never
    synthesize. It is not itself a Host Contract instance and must never be
    accepted as one.
    """

    source_contract: str
    target_contract: str
    permitted_mapping: tuple[str, ...]
    prohibited_synthesis: tuple[str, ...]
    result: str


def load_migration_plan(document: object) -> MigrationPlan:
    """Load a migration plan document."""
    if not isinstance(document, Mapping):
        raise ContractCompatibilityError("migration plan: expected an object")
    missing = sorted(
        {"sourceContract", "targetContract", "permittedMapping", "prohibitedSynthesis", "result"}
        - set(document)
    )
    if missing:
        raise ContractCompatibilityError(f"migration plan: missing field(s) {missing}")
    return MigrationPlan(
        source_contract=str(document["sourceContract"]),
        target_contract=str(document["targetContract"]),
        permitted_mapping=tuple(str(name) for name in document["permittedMapping"]),
        prohibited_synthesis=tuple(str(name) for name in document["prohibitedSynthesis"]),
        result=str(document["result"]),
    )


def is_host_contract_instance(document: object) -> bool:
    """Return whether a document decodes as one of the governed v1 records.

    A migration plan answers ``False``: it describes a mapping, it is not a
    value the contract carries. Schema or structural acceptance alone would
    never grant capability authority either way -- this only answers whether
    the document is a Host Contract value at all.
    """
    from omnivia_core.host_contract.v1 import generated

    for name in generated.HOST_CONTRACT_RECORDS:
        try:
            getattr(generated, name).from_wire(document)
        except HostContractDecodeError:
            continue
        return True
    return False


def migrate_v0_display_context(document: Mapping[str, Any]) -> Mapping[str, Any]:
    """Carry a validated v0 display document into v1 presentation data.

    Only the plan's permitted display fields survive; anything else is dropped
    rather than forwarded. A document that carries a prohibited authority
    spelling is refused outright rather than quietly stripped, because an
    adapter presented with one is being asked to launder authority, and that
    attempt should be visible rather than absorbed.

    The output is presentation data. It is not a ShellContext and cannot become
    one: principal, Workspace, permission, entitlement and environment authority
    originate from the trusted v1 context service or not at all.
    """
    from omnivia_core.host_contract.v1.codec import PROHIBITED_PAYLOAD_AUTHORITY_FIELDS

    synthesized = sorted(set(document) & PROHIBITED_PAYLOAD_AUTHORITY_FIELDS)
    if synthesized:
        raise ContractCompatibilityError(
            f"{V0_DISPLAY_CONTRACT}: migration cannot synthesize authority field(s) {synthesized}; "
            "they must originate from the trusted v1 context service"
        )
    migrated = {
        name: _freeze_display_value(document[name])
        for name in V0_PERMITTED_MAPPING
        if name in document
    }
    return MappingProxyType(migrated)


def _freeze_display_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze_display_value(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_display_value(item) for item in value)
    return value
