"""Module contribution registration for the Host Contract v1.

Registration validates identity, Release integrity, target compatibility,
entitlement declaration, schema availability and conflicts *before* activation.
Three governed rules shape the whole module:

- **all-or-nothing** -- a declaration activates completely or not at all.
  Best-effort partial activation was rejected because installation outcomes
  would then depend on registration order and leave ambiguous ownership;
- **priority never decides** -- ``priority`` orders contributions that were all
  accepted. It cannot resolve an identity or ownership conflict, so a higher
  priority never wins a duplicate ``targetId``;
- **nothing activates on its own say-so** -- a declaration describes itself, so
  it is evidence of nothing. Activation requires host-captured
  :class:`RegistrationEvidence`: verified module identity, a verified Release
  Package identity whose digests the host recomputed for itself, verified
  publisher and signing identity where the host requires them, and the Host
  Contract version the host actually speaks. Absent, mismatched or malformed
  proof rejects. There is no two-argument call and no permissive default.

The long-lived :class:`~omnivia_core.host_contract.v1.bound.BoundHost` captures
the :class:`ContributionRegistry` and independently established
:class:`RegistrationEvidence` during host integration. A hosted caller supplies
only its declaration to that bound service. These configuration records remain
importable because Core is also the host-integration contract; constructing a
different service does not alter or impersonate the service the actual Host
Adapter owns. This is an ownership boundary, not cryptographic provenance.

Standard library only. Nothing here may depend on runtime, storage, HTTP, MCP,
CLI, Platform, Dev, or a validation framework.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Final

from omnivia_core.host_contract.v1.compatibility import (
    ContractNegotiationError,
    ContractVersionError,
    parse_contract_version,
    require_supported_version,
)
from omnivia_core.host_contract.v1.generated import (
    HOST_CONTRACT_MAJOR,
    SHA256_PATTERN,
    TARGETS,
    Contribution,
    HostContractDecodeError,
    ModuleContributionDeclaration,
    ReleasePackageIdentity,
)

__all__ = [
    "REGISTRATION_EVIDENCE_CODES",
    "REGISTRATION_STATUSES",
    "REGISTRATION_STATUS_ACCEPTED",
    "REGISTRATION_STATUS_REJECTED",
    "REJECTION_CODES",
    "ContributionRegistrationResult",
    "RegistrationRejection",
]

#: The two registration outcomes. The governed specification names ``rejected``
#: explicitly; ``accepted`` is its complement, the state in which the whole
#: declaration proceeds to activation. Neither is a wire status.
REGISTRATION_STATUS_ACCEPTED: Final[str] = "accepted"
REGISTRATION_STATUS_REJECTED: Final[str] = "rejected"
REGISTRATION_STATUSES: Final[tuple[str, ...]] = (
    REGISTRATION_STATUS_ACCEPTED,
    REGISTRATION_STATUS_REJECTED,
)

#: Every reason a declaration can be refused for a *conflict*, in the order the
#: specification lists them: duplicate identifiers, duplicate ``targetId``,
#: reserved shell IDs, incompatible or absent payload schemas, undeclared
#: entitlements, and missing required capabilities.
REJECTION_CODES: Final[tuple[str, ...]] = (
    "duplicate_contribution_id",
    "duplicate_target_id",
    "reserved_target_id",
    "payload_schema_unavailable",
    "missing_required_capability",
    "undeclared_entitlement",
)

#: Every reason the *pre-activation proof* can fail. These are this
#: implementation's codes rather than governed vocabulary, and are kept apart
#: from :data:`REJECTION_CODES` so a proof failure is never read as a conflict.
REGISTRATION_EVIDENCE_CODES: Final[tuple[str, ...]] = (
    "malformed_registration_evidence",
    "invalid_declaration",
    "unverified_module_identity",
    "unverified_release_identity",
    "unverified_release_integrity",
    "unverified_publisher",
    "incompatible_host_target",
    "incompatible_contract_version",
)

_SHA256_RE: Final[re.Pattern[str]] = re.compile(SHA256_PATTERN)


@dataclass(frozen=True, slots=True)
class ContributionRegistry:
    """What the host currently offers a registering Module release.

    ``available_payload_schema_ids`` also carries the incompatible-schema-major
    rule: a payload schema identifier encodes its own major (``...route.v1``
    against ``...route.v2``), so a contribution declaring a major the host does
    not publish is simply declaring an identifier the host does not have.

    ``host_target`` is the target activation is being attempted on. It must be
    one the verified Release Package actually publishes an entry for, which is
    why compatibility is decided against the verified identity rather than
    against the declaration.

    Publisher trust is stated, not assumed. ``require_publisher_verification``
    left on means the host will not activate a Release whose publisher and
    signing identity it did not check; a conformance host that genuinely does
    not require it has to say so.
    """

    reserved_target_ids: frozenset[str] = frozenset()
    registered_target_ids: frozenset[str] = frozenset()
    available_payload_schema_ids: frozenset[str] = frozenset()
    available_capabilities: frozenset[str] = frozenset()
    declared_entitlements: frozenset[str] = frozenset()
    host_target: str = ""
    require_publisher_verification: bool = True
    trusted_publisher_ids: frozenset[str] = frozenset()
    trusted_signing_key_ids: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class RegistrationEvidence:
    """What the host established for itself before activation was considered.

    Every field is a *verified* fact, not a claim being forwarded: an identity
    the host resolved, a digest it recomputed over the package it holds, a
    signature it checked, the contract version it speaks. Registration compares
    the declaration against these, so a Module that renames its own release or
    invents a module ID contradicts the evidence instead of redefining it.

    This is deliberately not a wire record. It has no schema entry, no
    ``from_wire`` and no transport, so it can only be built by the host doing
    the verifying.
    """

    release_identity: ReleasePackageIdentity
    verified_module_id: str
    verified_module_release_id: str
    recomputed_manifest_sha256: str
    recomputed_payload_sha256: str
    publisher_verified: bool
    verified_publisher_id: str
    signing_key_verified: bool
    verified_signing_key_id: str
    host_contract_version: str


@dataclass(frozen=True, slots=True)
class RegistrationRejection:
    """One reason a declaration was refused.

    ``contribution_id`` carries only safe positional metadata such as
    ``contributions[0]``. It is ``None`` for a declaration-level reason. Caller
    identifiers are deliberately never retained in rejection diagnostics.
    """

    code: str
    detail: str
    contribution_id: str | None = None


@dataclass(frozen=True, slots=True)
class ContributionRegistrationResult:
    """The outcome of registering one declaration.

    ``activated`` is empty whenever ``status`` is ``rejected``: there is no
    state in which some of a declaration's contributions are live and others
    are not.
    """

    status: str
    module_release_id: str
    activated: tuple[str, ...] = ()
    rejections: tuple[RegistrationRejection, ...] = field(default_factory=tuple)


def _snapshot_contribution(value: object) -> Contribution | None:
    """Detach one contribution without invoking caller-controlled operations."""
    if type(value) is not Contribution:
        return None

    contribution_id = value.contribution_id
    kind = value.kind
    target_id = value.target_id
    required_capabilities = value.required_capabilities
    optional_capabilities = value.optional_capabilities
    payload_schema_id = value.payload_schema_id
    priority = value.priority

    if (
        type(contribution_id) is not str
        or type(kind) is not str
        or type(target_id) is not str
        or type(required_capabilities) is not tuple
        or any(type(item) is not str for item in required_capabilities)
        or type(optional_capabilities) is not tuple
        or any(type(item) is not str for item in optional_capabilities)
        or type(payload_schema_id) is not str
        or (priority is not None and type(priority) is not int)
    ):
        return None

    return Contribution(
        contribution_id=contribution_id,
        kind=kind,
        target_id=target_id,
        required_capabilities=tuple(required_capabilities),
        optional_capabilities=tuple(optional_capabilities),
        payload_schema_id=payload_schema_id,
        priority=priority,
    )


def _snapshot_declaration(value: object) -> ModuleContributionDeclaration | None:
    """Return one recursively validated snapshot of caller-owned declaration data.

    Every attribute is captured once. Exact immutable scalar and container types
    are established before validation can perform equality, hashing or regular
    expression operations, so hostile subclasses and mutable substitutions are
    rejected without executing caller code.
    """
    if type(value) is not ModuleContributionDeclaration:
        return None

    declaration_version = value.declaration_version
    module_id = value.module_id
    module_release_id = value.module_release_id
    raw_contributions = value.contributions
    required_entitlements = value.required_entitlements

    if (
        type(declaration_version) is not str
        or type(module_id) is not str
        or type(module_release_id) is not str
        or type(raw_contributions) is not tuple
        or type(required_entitlements) is not tuple
        or any(type(item) is not str for item in required_entitlements)
    ):
        return None

    contributions: list[Contribution] = []
    for item in raw_contributions:
        snapshot = _snapshot_contribution(item)
        if snapshot is None:
            return None
        contributions.append(snapshot)

    declaration = ModuleContributionDeclaration(
        declaration_version=declaration_version,
        module_id=module_id,
        module_release_id=module_release_id,
        contributions=tuple(contributions),
        required_entitlements=tuple(required_entitlements),
    )
    try:
        declaration.validate()
    except HostContractDecodeError:
        return None
    return declaration


def _rejected(
    module_release_id: str, rejections: list[RegistrationRejection]
) -> ContributionRegistrationResult:
    del module_release_id
    return ContributionRegistrationResult(
        status=REGISTRATION_STATUS_REJECTED,
        module_release_id="",
        activated=(),
        rejections=tuple(rejections),
    )


def _diagnostic_rejection(
    code: str, field_name: str, *, contribution_index: int | None = None
) -> RegistrationRejection:
    """Build a rejection from fixed code and field/index metadata only."""
    return RegistrationRejection(
        code=code,
        detail=f"field={field_name}",
        contribution_id=(
            None if contribution_index is None else f"contributions[{contribution_index}]"
        ),
    )


def _registry_findings(registry: ContributionRegistry) -> list[str]:
    if type(registry) is not ContributionRegistry:
        return ["registry is not host registry state"]
    for name in (
        "reserved_target_ids",
        "registered_target_ids",
        "available_payload_schema_ids",
        "available_capabilities",
        "declared_entitlements",
        "trusted_publisher_ids",
        "trusted_signing_key_ids",
    ):
        values = getattr(registry, name)
        if type(values) is not frozenset or any(type(value) is not str for value in values):
            return ["registry contains malformed host state"]
    if type(registry.host_target) is not str or type(
        registry.require_publisher_verification
    ) is not bool:
        return ["registry contains malformed host state"]
    return []


def _evidence_findings(evidence: RegistrationEvidence) -> list[str]:
    """Return one finding per part of the evidence that is not usable as proof.

    Proof that is itself malformed proves nothing, so this runs before any
    comparison against the declaration. A digest that is not a digest, an
    identity that is not a string, or a Release identity that does not satisfy
    its own governed record all fail here rather than being compared.
    """
    findings: list[str] = []
    if type(evidence) is not RegistrationEvidence:
        return ["evidence is not host registration evidence"]
    if type(evidence.release_identity) is not ReleasePackageIdentity:
        findings.append("release_identity is not a ReleasePackageIdentity")
    else:
        try:
            evidence.release_identity.validate()
        except HostContractDecodeError:
            findings.append("release_identity is not a valid Release Package identity")
    for name in (
        "verified_module_id",
        "verified_module_release_id",
        "verified_publisher_id",
        "verified_signing_key_id",
        "host_contract_version",
    ):
        if type(getattr(evidence, name)) is not str or not getattr(evidence, name):
            findings.append(f"{name} is not a non-empty string")
    for name in ("publisher_verified", "signing_key_verified"):
        if type(getattr(evidence, name)) is not bool:
            findings.append(f"{name} is not a boolean")
    for name in ("recomputed_manifest_sha256", "recomputed_payload_sha256"):
        value = getattr(evidence, name)
        if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
            findings.append(f"{name} is not a SHA-256 digest")
    return findings


def _identity_rejections(
    declaration: ModuleContributionDeclaration, evidence: RegistrationEvidence
) -> list[RegistrationRejection]:
    identity = evidence.release_identity
    rejections: list[RegistrationRejection] = []
    if declaration.module_id != evidence.verified_module_id:
        rejections.append(_diagnostic_rejection("unverified_module_identity", "moduleId"))
    if declaration.module_release_id != evidence.verified_module_release_id:
        rejections.append(
            _diagnostic_rejection("unverified_release_identity", "moduleReleaseId")
        )
    elif identity.release_id != evidence.verified_module_release_id:
        rejections.append(
            _diagnostic_rejection(
                "unverified_release_identity", "registrationEvidence.releaseIdentity.releaseId"
            )
        )
    if evidence.recomputed_manifest_sha256 != identity.manifest_sha256:
        rejections.append(
            _diagnostic_rejection(
                "unverified_release_integrity",
                "registrationEvidence.releaseIdentity.manifestSha256",
            )
        )
    if evidence.recomputed_payload_sha256 != identity.payload_sha256:
        rejections.append(
            _diagnostic_rejection(
                "unverified_release_integrity",
                "registrationEvidence.releaseIdentity.payloadSha256",
            )
        )
    return rejections


def _publisher_rejections(
    registry: ContributionRegistry, evidence: RegistrationEvidence
) -> list[RegistrationRejection]:
    if not registry.require_publisher_verification:
        return []
    identity = evidence.release_identity
    reasons: list[str] = []
    if not evidence.publisher_verified:
        reasons.append("publisher identity was not verified")
    if not evidence.signing_key_verified:
        reasons.append("the release signature was not checked")
    if evidence.verified_publisher_id != identity.publisher_id:
        reasons.append("the verified publisher is not the one the package names")
    if evidence.verified_signing_key_id != identity.signing_key_id:
        reasons.append("the verified signing key is not the one the package names")
    if registry.trusted_publisher_ids and evidence.verified_publisher_id not in (
        registry.trusted_publisher_ids
    ):
        reasons.append("the publisher is not trusted by this host")
    if registry.trusted_signing_key_ids and evidence.verified_signing_key_id not in (
        registry.trusted_signing_key_ids
    ):
        reasons.append("the signing key is not trusted by this host")
    if not reasons:
        return []
    return [_diagnostic_rejection("unverified_publisher", "registrationEvidence.publisher")]


def _compatibility_rejections(
    declaration: ModuleContributionDeclaration,
    registry: ContributionRegistry,
    evidence: RegistrationEvidence,
) -> list[RegistrationRejection]:
    rejections: list[RegistrationRejection] = []
    entries = {entry.target for entry in evidence.release_identity.target_entries}
    if registry.host_target not in TARGETS:
        rejections.append(
            _diagnostic_rejection("incompatible_host_target", "host.target")
        )
    elif registry.host_target not in entries:
        rejections.append(
            _diagnostic_rejection(
                "incompatible_host_target", "registrationEvidence.releaseIdentity.targetEntries"
            )
        )
    for field_name, version in (
        ("registrationEvidence.hostContractVersion", evidence.host_contract_version),
        ("declarationVersion", declaration.declaration_version),
    ):
        try:
            require_supported_version(version)
            major = parse_contract_version(version)[0]
        except (ContractNegotiationError, ContractVersionError):
            rejections.append(
                _diagnostic_rejection("incompatible_contract_version", field_name)
            )
            continue
        if major != HOST_CONTRACT_MAJOR:  # pragma: no cover - the schema pins the major
            rejections.append(
                _diagnostic_rejection("incompatible_contract_version", field_name)
            )
    return rejections


def _conflict_rejections(
    declaration: ModuleContributionDeclaration, registry: ContributionRegistry
) -> list[RegistrationRejection]:
    rejections: list[RegistrationRejection] = []

    for entitlement_index, entitlement in enumerate(declaration.required_entitlements):
        if entitlement in registry.declared_entitlements:
            continue
        rejections.append(
            _diagnostic_rejection(
                "undeclared_entitlement",
                f"requiredEntitlements[{entitlement_index}]",
            )
        )

    seen_contribution_ids: set[str] = set()
    seen_target_ids: set[str] = set()
    for contribution_index, contribution in enumerate(declaration.contributions):
        if contribution.contribution_id in seen_contribution_ids:
            rejections.append(
                _diagnostic_rejection(
                    "duplicate_contribution_id",
                    f"contributions[{contribution_index}].contributionId",
                    contribution_index=contribution_index,
                )
            )
        seen_contribution_ids.add(contribution.contribution_id)

        target_id = contribution.target_id
        if target_id in seen_target_ids or target_id in registry.registered_target_ids:
            rejections.append(
                _diagnostic_rejection(
                    "duplicate_target_id",
                    f"contributions[{contribution_index}].targetId",
                    contribution_index=contribution_index,
                )
            )
        seen_target_ids.add(target_id)

        if target_id in registry.reserved_target_ids:
            rejections.append(
                _diagnostic_rejection(
                    "reserved_target_id",
                    f"contributions[{contribution_index}].targetId",
                    contribution_index=contribution_index,
                )
            )

        if contribution.payload_schema_id not in registry.available_payload_schema_ids:
            rejections.append(
                _diagnostic_rejection(
                    "payload_schema_unavailable",
                    f"contributions[{contribution_index}].payloadSchemaId",
                    contribution_index=contribution_index,
                )
            )

        for capability_index, capability in enumerate(contribution.required_capabilities):
            if capability in registry.available_capabilities:
                continue
            rejections.append(
                _diagnostic_rejection(
                    "missing_required_capability",
                    (
                        f"contributions[{contribution_index}]"
                        f".requiredCapabilities[{capability_index}]"
                    ),
                    contribution_index=contribution_index,
                )
            )
    return rejections


def _evaluate_registration(
    declaration: ModuleContributionDeclaration,
    registry: ContributionRegistry,
    evidence: RegistrationEvidence,
) -> ContributionRegistrationResult:
    """Pure validation used by a bound host before it atomically activates.

    Returning ``accepted`` here is not activation and grants no authority. Only
    the bound service owns and updates the authentic target registry.
    """
    release_id = (
        declaration.module_release_id
        if type(declaration) is ModuleContributionDeclaration
        and type(declaration.module_release_id) is str
        else ""
    )
    malformed = [*_registry_findings(registry), *_evidence_findings(evidence)]
    if malformed:
        return _rejected(
            release_id,
            [
                _diagnostic_rejection(
                    "malformed_registration_evidence", "registrationEvidence"
                )
            ],
        )

    if type(declaration) is not ModuleContributionDeclaration:
        return _rejected(
            release_id,
            [
                RegistrationRejection(
                    code="invalid_declaration",
                    detail="field=declaration",
                )
            ],
        )
    try:
        declaration.validate()
    except HostContractDecodeError:
        return _rejected(
            release_id,
            [_diagnostic_rejection("invalid_declaration", "declaration")],
        )

    rejections = [
        *_identity_rejections(declaration, evidence),
        *_publisher_rejections(registry, evidence),
        *_compatibility_rejections(declaration, registry, evidence),
        *_conflict_rejections(declaration, registry),
    ]
    if rejections:
        return _rejected(declaration.module_release_id, rejections)
    return ContributionRegistrationResult(
        status=REGISTRATION_STATUS_ACCEPTED,
        module_release_id=declaration.module_release_id,
        activated=tuple(item.contribution_id for item in declaration.contributions),
        rejections=(),
    )
