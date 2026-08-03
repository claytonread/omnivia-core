"""Host-owned semantic admission and registration boundary.

``BoundHost`` is the integration object the actual Host Adapter keeps and calls.
Its operation catalogue, contribution registry and independently verified
Release facts are captured once at construction; hosted code supplies only a
wire request or contribution declaration to each operation.

The class is intentionally constructible because Core defines the contract that
host integrations implement. A separately constructed instance owns separate
state and grants no authority in the authentic Host Adapter. This is an
ownership and integration boundary, not a claim of cryptographic provenance or
same-process isolation from Python code that already holds the authentic object.

Global wire decoding remains structural. Only :meth:`BoundHost.admit_request`
checks an operation against the catalogue captured by that particular host.
Registration validation is followed by an atomic update of that host's target
registry; decision-shaped values returned elsewhere cannot update it.
"""

from __future__ import annotations

import dataclasses
import threading
from typing import Any, SupportsIndex

from omnivia_core.host_contract.v1 import codec, registration
from omnivia_core.host_contract.v1.compatibility import parse_contract_version
from omnivia_core.host_contract.v1.generated import (
    HostContractDecodeError,
    HostRequest,
    ModuleContributionDeclaration,
)

__all__ = ["BoundHost"]


class BoundHost:
    """One long-lived Host Adapter admission and activation boundary.

    Configuration is snapshotted so later mutation or replacement of the
    caller's configuration records cannot change this instance. Copying,
    pickling and uninitialised reconstruction are refused. Those controls avoid
    accidental boundary duplication; authentic ownership still comes from the
    Host Adapter retaining and invoking this exact instance.
    """

    __slots__ = ("__catalogue", "__evidence", "__lock", "__registry")

    def __init__(
        self,
        *,
        operation_catalogue: codec.OperationCatalogue,
        contribution_registry: registration.ContributionRegistry,
        registration_evidence: tuple[registration.RegistrationEvidence, ...],
    ) -> None:
        catalogue = _snapshot_catalogue(operation_catalogue)
        registry = _snapshot_registry(contribution_registry)
        if type(registration_evidence) is not tuple:
            raise HostContractDecodeError("BoundHost: invalid registration verification inventory")
        evidence = tuple(_snapshot_evidence(item) for item in registration_evidence)
        verified_release_ids = [
            item.verified_module_release_id
            for item in evidence
            if type(item) is registration.RegistrationEvidence
            and type(item.verified_module_release_id) is str
        ]
        if len(verified_release_ids) != len(set(verified_release_ids)):
            raise HostContractDecodeError("BoundHost: duplicate registration verification")
        self.__catalogue = catalogue
        self.__registry = registry
        self.__evidence = evidence
        self.__lock = threading.RLock()

    def __init_subclass__(cls, **kwargs: Any) -> None:
        del kwargs
        raise TypeError("bound hosts cannot be subclassed")

    def __copy__(self) -> BoundHost:
        raise TypeError("bound hosts cannot be copied")

    def __deepcopy__(self, memo: object) -> BoundHost:
        del memo
        raise TypeError("bound hosts cannot be copied")

    def __reduce__(self) -> tuple[object, ...]:
        raise TypeError("bound hosts cannot be serialized")

    def __reduce_ex__(self, protocol: SupportsIndex) -> tuple[object, ...]:
        del protocol
        raise TypeError("bound hosts cannot be serialized")

    def admit_request(self, payload: object) -> HostRequest:
        """Decode and semantically admit one request against this host's catalogue."""
        try:
            catalogue = self.__catalogue
        except AttributeError:
            raise HostContractDecodeError("BoundHost: bound host state is unavailable") from None
        if type(self) is not BoundHost or type(catalogue) is not codec.OperationCatalogue:
            raise HostContractDecodeError("BoundHost: bound host state is unavailable")

        request = codec.decode_host_request(payload)
        published = next(
            (
                item
                for item in catalogue.operations
                if item.namespace == request.namespace and item.operation_id == request.operation
            ),
            None,
        )
        if published is None:
            raise HostContractDecodeError("HostRequest.operation: unsupported_contract_value")
        minor = parse_contract_version(request.contract_version)[1]
        if minor < published.minimum_contract_minor:
            raise HostContractDecodeError("HostRequest.operation: unsupported_contract_value")
        if published.idempotency_required and request.idempotency_key is None:
            raise HostContractDecodeError(
                "HostRequest.idempotencyKey: required by published operation"
            )
        return request

    def register(
        self, declaration: ModuleContributionDeclaration
    ) -> registration.ContributionRegistrationResult:
        """Validate and atomically activate one declaration against captured state."""
        try:
            lock = self.__lock
            evidence_inventory = self.__evidence
        except AttributeError:
            return _unavailable_registration()
        if type(self) is not BoundHost:
            return _unavailable_registration()

        with lock:
            snapshot = registration._snapshot_declaration(declaration)
            if snapshot is None:
                return _invalid_declaration()
            registry = self.__registry
            if any(_is_malformed_evidence(item) for item in evidence_inventory):
                return _malformed_evidence()
            evidence = _evidence_for(snapshot, evidence_inventory)
            if evidence is None:
                return _missing_evidence()
            result = registration._evaluate_registration(snapshot, registry, evidence)
            if result.status != registration.REGISTRATION_STATUS_ACCEPTED:
                return result
            activated_targets = frozenset(
                item.target_id for item in snapshot.contributions
            )
            self.__registry = dataclasses.replace(
                registry,
                registered_target_ids=(
                    registry.registered_target_ids | activated_targets
                ),
            )
            return result


def _snapshot_catalogue(value: codec.OperationCatalogue) -> codec.OperationCatalogue:
    if type(value) is not codec.OperationCatalogue:
        raise HostContractDecodeError("BoundHost: invalid operation catalogue")
    value.validate()
    return codec.OperationCatalogue(
        operations=tuple(
            codec.PublishedOperation(
                namespace=item.namespace,
                operation_id=item.operation_id,
                request_schema_id=item.request_schema_id,
                result_schema_id=item.result_schema_id,
                effect_class=item.effect_class,
                idempotency_required=item.idempotency_required,
                required_capabilities=tuple(item.required_capabilities),
                minimum_contract_minor=item.minimum_contract_minor,
            )
            for item in value.operations
        )
    )


def _snapshot_registry(
    value: registration.ContributionRegistry,
) -> registration.ContributionRegistry:
    if type(value) is not registration.ContributionRegistry:
        raise HostContractDecodeError("BoundHost: invalid contribution registry")
    findings = registration._registry_findings(value)
    if findings:
        raise HostContractDecodeError("BoundHost: invalid contribution registry")
    return registration.ContributionRegistry(
        reserved_target_ids=frozenset(value.reserved_target_ids),
        registered_target_ids=frozenset(value.registered_target_ids),
        available_payload_schema_ids=frozenset(value.available_payload_schema_ids),
        available_capabilities=frozenset(value.available_capabilities),
        declared_entitlements=frozenset(value.declared_entitlements),
        host_target=value.host_target,
        require_publisher_verification=value.require_publisher_verification,
        trusted_publisher_ids=frozenset(value.trusted_publisher_ids),
        trusted_signing_key_ids=frozenset(value.trusted_signing_key_ids),
    )


def _snapshot_evidence(value: object) -> object:
    if type(value) is not registration.RegistrationEvidence:
        return value
    if registration._evidence_findings(value):
        return dataclasses.replace(value)
    identity = type(value.release_identity).from_wire(value.release_identity.to_wire())
    return registration.RegistrationEvidence(
        release_identity=identity,
        verified_module_id=value.verified_module_id,
        verified_module_release_id=value.verified_module_release_id,
        recomputed_manifest_sha256=value.recomputed_manifest_sha256,
        recomputed_payload_sha256=value.recomputed_payload_sha256,
        publisher_verified=value.publisher_verified,
        verified_publisher_id=value.verified_publisher_id,
        signing_key_verified=value.signing_key_verified,
        verified_signing_key_id=value.verified_signing_key_id,
        host_contract_version=value.host_contract_version,
    )


def _is_malformed_evidence(value: object) -> bool:
    if type(value) is not registration.RegistrationEvidence:
        return True
    return bool(registration._evidence_findings(value))


def _evidence_for(
    declaration: object, inventory: tuple[object, ...]
) -> registration.RegistrationEvidence | None:
    """Select the evidence entry that verified *this* release, or select nothing.

    Selection is an exact match on ``verified_module_release_id`` and has no
    fallback. A one-entry inventory is not a licence to hand that entry to a
    declaration naming some other release: that would make the host's own
    verification record the proof for a release it never verified, and would
    leave the mismatch to be noticed downstream by
    :func:`~omnivia_core.host_contract.v1.registration._identity_rejections`.
    Failing here instead keeps the unverified release out of every later
    comparison, whatever the inventory happens to hold.
    """
    if (
        type(declaration) is not ModuleContributionDeclaration
        or type(declaration.module_release_id) is not str
    ):
        return None
    release_id = declaration.module_release_id
    for item in inventory:
        if (
            type(item) is registration.RegistrationEvidence
            and type(item.verified_module_release_id) is str
            and item.verified_module_release_id == release_id
        ):
            return item
    return None


def _unavailable_registration() -> registration.ContributionRegistrationResult:
    return registration.ContributionRegistrationResult(
        status=registration.REGISTRATION_STATUS_REJECTED,
        module_release_id="",
        activated=(),
        rejections=(
            registration.RegistrationRejection(
                code="malformed_registration_evidence",
                detail="field=boundHost",
            ),
        ),
    )


def _malformed_evidence() -> registration.ContributionRegistrationResult:
    return registration.ContributionRegistrationResult(
        status=registration.REGISTRATION_STATUS_REJECTED,
        module_release_id="",
        activated=(),
        rejections=(
            registration.RegistrationRejection(
                code="malformed_registration_evidence",
                detail="field=registrationEvidence",
            ),
        ),
    )


def _invalid_declaration() -> registration.ContributionRegistrationResult:
    return registration.ContributionRegistrationResult(
        status=registration.REGISTRATION_STATUS_REJECTED,
        module_release_id="",
        activated=(),
        rejections=(
            registration.RegistrationRejection(
                code="invalid_declaration",
                detail="field=declaration",
            ),
        ),
    )


def _missing_evidence() -> registration.ContributionRegistrationResult:
    return registration.ContributionRegistrationResult(
        status=registration.REGISTRATION_STATUS_REJECTED,
        module_release_id="",
        activated=(),
        rejections=(
            registration.RegistrationRejection(
                code="unverified_release_identity",
                detail="field=moduleReleaseId",
            ),
        ),
    )
