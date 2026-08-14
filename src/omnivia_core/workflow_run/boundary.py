"""Shared vocabulary for the Workflow Run ingress boundary (V06-8, A8-N1).

The terms both halves of this lane need, and nothing else. The loader
(:mod:`~omnivia_core.workflow_run.conformance`) reads the accepted corpus into
them; the input side (:mod:`~omnivia_core.workflow_run.ingress`) derives
observations in them. Neither imports the other, so a structured input recipe
cannot be written towards an expectation.

Every operation fact here is *derived* from
:data:`~omnivia_core.contracts.v1.generated.OPERATION_CATALOGUE` rather than
restated. A parallel list of the twenty operations, their capabilities or their
scopes would be a second catalogue that can drift from the frozen one, and a
conformance layer holding a stale copy would pass a boundary that no longer
exists.

This module adds no operation, capability, error code or envelope field. It
implements no Workflow Runtime, no host adapter and no run-state storage: it
names the boundary those things sit on the far side of.

Standard library only.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from enum import Enum
from types import MappingProxyType
from typing import Any, Final

from omnivia_core.contracts.v1.generated import (
    OPERATION_CATALOGUE,
    OperationMetadata,
    RequestMetadata,
)


class PrincipalKind(Enum):
    """The kind of caller principal a run's ingress acts as."""

    AGENT = "agent"
    HUMAN = "human"


class Disposition(Enum):
    """How one modelled observation ended."""

    ACCEPTED = "accepted"
    REJECTED = "rejected"
    NO_OP = "no_op"
    INDETERMINATE_UNTIL_RECONCILED = "indeterminate_until_reconciled"


class MutationAuthority(Enum):
    """Under whose authority a canonical change was made, if any."""

    NONE = "none"
    VALIDATED_HUMAN_PRINCIPAL = "validated_human_principal"


class BranchId(Enum):
    """The two legitimate outcomes an unreconciled cancellation resolves to."""

    COMMITTED_ONCE = "committed_once"
    NOT_COMMITTED = "not_committed"


#: The frozen catalogue, by name. Held as the entries the catalogue holds.
CATALOGUE: Final[Mapping[str, OperationMetadata]] = MappingProxyType(
    {operation.name: operation for operation in OPERATION_CATALOGUE}
)

GOVERNANCE_CAPABILITY: Final = "knowledge.govern"
JOB_CONTROL_CAPABILITY: Final = "job.control"
INSTALLATION_SCOPE_KIND: Final = "installation"
NO_SIDE_EFFECT: Final = "none"

#: Every operation the catalogue marks as changing state.
MUTATING_OPERATIONS: Final[frozenset[str]] = frozenset(
    name
    for name, operation in CATALOGUE.items()
    if operation.scope.side_effect != NO_SIDE_EFFECT
)

#: The operations that can record a governance decision, by their capability.
GOVERNANCE_OPERATIONS: Final[frozenset[str]] = frozenset(
    name
    for name, operation in CATALOGUE.items()
    if operation.required_capability.id == GOVERNANCE_CAPABILITY
)

#: Core durable-job control. Derived here rather than imported from the agent
#: host package: A9 consumes this lane's vocabulary, not the other way round,
#: and a one-line derivation from the catalogue beats a cross-lane import.
JOB_CONTROL_OPERATIONS: Final[frozenset[str]] = frozenset(
    name
    for name, operation in CATALOGUE.items()
    if operation.required_capability.id == JOB_CONTROL_CAPABILITY
)

INSTALLATION_SCOPED_OPERATIONS: Final[frozenset[str]] = frozenset(
    name
    for name, operation in CATALOGUE.items()
    if operation.scope.scope_kind == INSTALLATION_SCOPE_KIND
)

#: Boundary document §5.3: Core exposes no operation that creates, reads,
#: lists, cancels or replays workflow-run state. Named so the claim is checked
#: against the catalogue instead of asserted in prose.
FORBIDDEN_RUN_STATE_OPERATIONS: Final[frozenset[str]] = frozenset(
    {
        "workflow_run.create",
        "workflow_run.get",
        "workflow_run.list",
        "workflow_run.cancel",
        "workflow_run.replay",
    }
)

#: The five identities that participate in run ingress (RUN-R-06). Only the
#: last two are Core-authoritative; the first three are runtime-owned, opaque
#: to Core and never authorization inputs.
IDENTITY_ROLES: Final[tuple[str, ...]] = (
    "run",
    "session",
    "agent",
    "caller_principal",
    "workspace",
)

#: The runtime-owned identifiers that must never appear in `RequestMetadata`.
#: `RequestMetadata` sets `unevaluatedProperties: false` and defines none of
#: them, so run lineage rides in payload provenance instead (RUN-D-01).
RUN_IDENTITY_FIELDS: Final[tuple[str, ...]] = ("agent_id", "run_id", "session_id")

#: The `SourceKind` open code run lineage is carried under (RUN-R-07).
RUN_LINEAGE_SOURCE_KIND: Final = "workflow_run"

#: The frozen `Identifier` bound. Truncating an over-long run id to fit is
#: forbidden, because a truncated id forges lineage (RUN-V-22).
IDENTIFIER_OCTET_BOUND: Final = 128

#: `RequestMetadata`'s own field names, read from the frozen dataclass.
REQUEST_METADATA_FIELDS: Final[tuple[str, ...]] = tuple(
    field.name for field in dataclasses.fields(RequestMetadata)
)


def metadata_run_identity_fields(metadata: Mapping[str, Any]) -> tuple[str, ...]:
    """Return the run-identity keys `metadata` carries, which must be none."""
    return tuple(name for name in RUN_IDENTITY_FIELDS if name in metadata)


__all__ = [
    "CATALOGUE",
    "FORBIDDEN_RUN_STATE_OPERATIONS",
    "GOVERNANCE_CAPABILITY",
    "GOVERNANCE_OPERATIONS",
    "IDENTIFIER_OCTET_BOUND",
    "IDENTITY_ROLES",
    "INSTALLATION_SCOPED_OPERATIONS",
    "INSTALLATION_SCOPE_KIND",
    "JOB_CONTROL_CAPABILITY",
    "JOB_CONTROL_OPERATIONS",
    "MUTATING_OPERATIONS",
    "NO_SIDE_EFFECT",
    "REQUEST_METADATA_FIELDS",
    "RUN_IDENTITY_FIELDS",
    "RUN_LINEAGE_SOURCE_KIND",
    "BranchId",
    "Disposition",
    "MutationAuthority",
    "PrincipalKind",
    "metadata_run_identity_fields",
]
