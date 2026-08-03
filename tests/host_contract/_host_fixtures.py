"""Host-integration helpers for Host Contract tests.

Each helper returns a separate bound host. Tests keep the returned instance and
supply only hosted request/declaration input to its operations.
"""

from __future__ import annotations

from omnivia_core.host_contract.v1 import bound, codec, registration


def bound_host(
    catalogue: codec.OperationCatalogue | None = None,
    registry: registration.ContributionRegistry | None = None,
    evidence: tuple[registration.RegistrationEvidence, ...] = (),
) -> bound.BoundHost:
    return bound.BoundHost(
        operation_catalogue=catalogue or codec.OperationCatalogue(operations=()),
        contribution_registry=registry or registration.ContributionRegistry(host_target="desktop"),
        registration_evidence=evidence,
    )
