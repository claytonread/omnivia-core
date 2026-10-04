"""Analysis-use authority seam for result-use evaluation (SPEC-CORE-DATA-001 §13.3).

Before an analytical result is used, the current authority for that use must be
resolved from the server's own state, against exactly one pinned subject and one
pinned dataset state. This module builds that question and checks the answer. It
does not decide the use: the decision belongs to the shared result-use evaluator,
which this module never calls.

The subject is taken from an authorized operation context only where its consumed
pairs agree by value. Legacy `None` authority fields refuse rather than widen. The
dataset observation is flattened into the query, and `observed_authority_epoch` is
recorded as evidence only: the current epoch is whatever the resolver answers.

Every refusal is one fixed reason with no caller or resolver text in it. A failing
resolver is caught and dropped before the refusal is raised, so nothing it quoted
reaches `__context__`.

Local and in-process: the resolver is an argument, and nothing here touches storage,
the network, the clock or credentials.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final, Protocol

from omnivia_core.contracts.v1 import (
    CapabilityRef,
    ContentChecksum,
    GrantedAuthority,
    Identifier,
    Purpose,
    RequestEnvelope,
    Scope,
    is_capability_id,
    is_content_checksum,
    is_contract_version,
    is_identifier,
    is_operation_name,
    is_purpose,
    is_scope,
    is_workspace_id,
)
from omnivia_core.contracts.v1.semantics_result_use import (
    USE_ACTION_INPUT,
    USE_CURRENT_PUBLICATION,
    USE_EXPLORATION,
    USE_HISTORICAL_DISPLAY,
)
from omnivia_core_runtime.service.authorization import AuthorizedApplicationContext
from omnivia_core_runtime.service.operations import OperationContext
from omnivia_core_runtime.storage.dataset_state import (
    DatasetStateObservation,
    DatasetStateRecord,
)

#: Every result-use class the shared evaluator admits. Deliberately the full set,
#: `action_input` included, not the narrower admitted-analysis list.
_USE_CLASSES: Final = (
    USE_EXPLORATION,
    USE_HISTORICAL_DISPLAY,
    USE_CURRENT_PUBLICATION,
    USE_ACTION_INPUT,
)

#: The one refusal reason. A fixed literal: no caller or resolver value is ever
#: interpolated into it.
REFUSE_ANALYSIS_USE_AUTHORITY: Final = "analysis_use_authority_unavailable"


class AnalysisUseAuthorityRefused(Exception):
    """The single refusal this seam raises, carrying only its fixed reason."""

    def __init__(self) -> None:
        super().__init__(REFUSE_ANALYSIS_USE_AUTHORITY)
        self.reason = REFUSE_ANALYSIS_USE_AUTHORITY


@dataclass(frozen=True, slots=True)
class AnalysisUseAuthoritySubject:
    """The operation, workspace, authority, scopes and purpose a use is bound to."""

    operation: str
    workspace_id: str
    authority: GrantedAuthority
    scopes: tuple[Scope, ...]
    purpose: Purpose


@dataclass(frozen=True, slots=True)
class AnalysisUseAuthorityQuery:
    """One fully validated question: a subject bound to one dataset state and use."""

    subject: AnalysisUseAuthoritySubject
    dataset_id: str
    dataset_revision: str
    dataset_incarnation: str
    state_generation: int
    manifest_id: str | None
    manifest_revision: str | None
    manifest_digest: ContentChecksum | None
    scope_digest: ContentChecksum
    observed_authority_epoch: str
    subject_digest: Identifier
    use_class: str
    evaluation_instant: datetime


@dataclass(frozen=True, slots=True)
class AnalysisUseAuthoritySnapshot:
    """The resolver's answer to one query, with the current authority epoch.

    False permission and policy flags are valid facts, not refusals.
    """

    query: AnalysisUseAuthorityQuery
    authority_epoch: str
    evidence_access_permitted: bool
    policy_permits_partial_or_stale: bool
    policy_ref: str
    policy_digest: ContentChecksum


class AnalysisUseAuthorityResolver(Protocol):
    """Answers one query from current server state."""

    def resolve(self, query: AnalysisUseAuthorityQuery) -> AnalysisUseAuthoritySnapshot:
        """Return the current authority for `query`."""
        ...


def analysis_use_authority_subject_from_context(
    context: OperationContext,
) -> AnalysisUseAuthoritySubject:
    """Build the subject from an authorized context whose consumed pairs agree.

    Only the operation, principal, workspace, authority, scopes and purpose are
    read. Every other envelope field is ignored.
    """
    if type(context) is not OperationContext:
        raise AnalysisUseAuthorityRefused()
    request = context.request
    authorization = context.authorization
    authority = context.authority
    scopes = context.scopes
    purpose = context.purpose
    if (
        type(request) is not RequestEnvelope
        or type(authorization) is not AuthorizedApplicationContext
        or type(authority) is not GrantedAuthority
        or type(scopes) is not tuple
        or type(purpose) is not str
    ):
        raise AnalysisUseAuthorityRefused()
    # Both sides are proven canonical before any `==` runs, so no equality here
    # dispatches to a subclass or a spoofed value.
    if not (
        _canonical_str(request.operation, is_operation_name)
        and _canonical_str(authorization.operation, is_operation_name)
        and _canonical_str(context.principal, is_identifier)
        and _canonical_str(authorization.principal_id, is_identifier)
        and _canonical_str(context.workspace_id, is_workspace_id)
        and _canonical_str(authorization.workspace_id, is_workspace_id)
        and _valid_authority(authority)
        and _valid_authority(authorization.authority)
        and _canonical_tuple(scopes, is_scope)
        and _canonical_tuple(authorization.scopes, is_scope)
        and _canonical_str(purpose, is_purpose)
        and _canonical_str(authorization.purpose, is_purpose)
        and request.operation == authorization.operation
        and context.principal == authorization.principal_id
        and context.workspace_id == authorization.workspace_id
        and authority == authorization.authority
        and scopes == authorization.scopes
        and purpose == authorization.purpose
    ):
        raise AnalysisUseAuthorityRefused()
    return AnalysisUseAuthoritySubject(
        operation=request.operation,
        workspace_id=context.workspace_id,
        authority=authority,
        scopes=scopes,
        purpose=purpose,
    )


def resolve_analysis_use_authority_for_subject(
    subject: AnalysisUseAuthoritySubject,
    *,
    dataset: DatasetStateRecord,
    subject_digest: Identifier,
    use_class: str,
    evaluation_instant: datetime,
    resolver: AnalysisUseAuthorityResolver,
) -> AnalysisUseAuthoritySnapshot:
    """Validate everything locally, ask the resolver once, and check its answer."""
    query = _build_query(
        subject,
        dataset=dataset,
        subject_digest=subject_digest,
        use_class=use_class,
        evaluation_instant=evaluation_instant,
    )
    snapshot: AnalysisUseAuthoritySnapshot | None = None
    try:
        snapshot = resolver.resolve(query)
        answered = _answers(snapshot, query)
    except Exception:  # noqa: BLE001 - any failure to answer is a refusal
        answered = False
    if snapshot is None or not answered:
        raise AnalysisUseAuthorityRefused()
    return snapshot


def resolve_analysis_use_authority(
    context: OperationContext,
    *,
    dataset: DatasetStateRecord,
    subject_digest: Identifier,
    use_class: str,
    evaluation_instant: datetime,
    resolver: AnalysisUseAuthorityResolver,
) -> AnalysisUseAuthoritySnapshot:
    """Convert the context to its subject, then resolve it as above."""
    return resolve_analysis_use_authority_for_subject(
        analysis_use_authority_subject_from_context(context),
        dataset=dataset,
        subject_digest=subject_digest,
        use_class=use_class,
        evaluation_instant=evaluation_instant,
        resolver=resolver,
    )


def _build_query(
    subject: AnalysisUseAuthoritySubject,
    *,
    dataset: DatasetStateRecord,
    subject_digest: Identifier,
    use_class: str,
    evaluation_instant: datetime,
) -> AnalysisUseAuthorityQuery:
    if not (_valid_subject(subject) and type(dataset) is DatasetStateRecord):
        raise AnalysisUseAuthorityRefused()
    observation = dataset.observation
    if type(observation) is not DatasetStateObservation:
        raise AnalysisUseAuthorityRefused()
    manifest = (
        observation.manifest_id,
        observation.manifest_revision,
        observation.manifest_digest,
    )
    manifest_absent = all(value is None for value in manifest)
    manifest_present = (
        _canonical_str(manifest[0], is_identifier)
        and _canonical_str(manifest[1], is_identifier)
        and _canonical_str(manifest[2], is_content_checksum)
    )
    instant = _utc(evaluation_instant)
    if not (
        _canonical_str(dataset.workspace_id, is_workspace_id)
        and dataset.workspace_id == subject.workspace_id
        and type(dataset.state_generation) is int
        and dataset.state_generation >= 1
        and _canonical_str(observation.dataset_id, is_identifier)
        and _canonical_str(observation.dataset_revision, is_identifier)
        and _canonical_str(observation.dataset_incarnation, is_identifier)
        and (manifest_absent or manifest_present)
        and _canonical_str(observation.scope_digest, is_content_checksum)
        and _canonical_str(observation.observed_authority_epoch, is_identifier)
        and _canonical_str(subject_digest, is_identifier)
        and type(use_class) is str
        and use_class in _USE_CLASSES
        and instant is not None
    ):
        raise AnalysisUseAuthorityRefused()
    return AnalysisUseAuthorityQuery(
        subject=subject,
        dataset_id=observation.dataset_id,
        dataset_revision=observation.dataset_revision,
        dataset_incarnation=observation.dataset_incarnation,
        state_generation=dataset.state_generation,
        manifest_id=manifest[0],
        manifest_revision=manifest[1],
        manifest_digest=manifest[2],
        scope_digest=observation.scope_digest,
        observed_authority_epoch=observation.observed_authority_epoch,
        subject_digest=subject_digest,
        use_class=use_class,
        evaluation_instant=instant,
    )


def _canonical_str(value: object, check: Callable[[object], bool]) -> bool:
    # The exact type is proven before `check` runs, so a str subclass never reaches
    # the validator or any later comparison.
    return type(value) is str and check(value)


def _canonical_tuple(value: object, check: Callable[[object], bool]) -> bool:
    return type(value) is tuple and all(_canonical_str(item, check) for item in value)


def _valid_subject(subject: object) -> bool:
    return (
        type(subject) is AnalysisUseAuthoritySubject
        and _canonical_str(subject.operation, is_operation_name)
        and _canonical_str(subject.workspace_id, is_workspace_id)
        and _valid_authority(subject.authority)
        and _canonical_tuple(subject.scopes, is_scope)
        and _canonical_str(subject.purpose, is_purpose)
    )


def _valid_authority(authority: object) -> bool:
    return (
        type(authority) is GrantedAuthority
        and _canonical_str(authority.principal_id, is_identifier)
        and _canonical_tuple(authority.roles, is_identifier)
        and type(authority.capabilities) is tuple
        and all(_valid_capability(item) for item in authority.capabilities)
    )


def _valid_capability(item: object) -> bool:
    return (
        type(item) is CapabilityRef
        and _canonical_str(item.id, is_capability_id)
        and _canonical_str(item.version, is_contract_version)
    )


def _utc(value: object) -> datetime | None:
    if type(value) is not datetime:
        return None
    # A hostile tzinfo can raise from either call. The failure is dropped here, so
    # no timezone text reaches the refusal's cause or context.
    try:
        normalized = None if value.utcoffset() is None else value.astimezone(UTC)
    except Exception:  # noqa: BLE001 - any timezone failure is a refusal
        normalized = None
    if type(normalized) is not datetime or normalized.tzinfo is not UTC:
        return None
    return normalized


def _answers(snapshot: object, query: AnalysisUseAuthorityQuery) -> bool:
    # Identity, not `==`: a resolver-supplied query may carry spoofed equality.
    return (
        type(snapshot) is AnalysisUseAuthoritySnapshot
        and type(snapshot.query) is AnalysisUseAuthorityQuery
        and snapshot.query is query
        and _canonical_str(snapshot.authority_epoch, is_identifier)
        and type(snapshot.evidence_access_permitted) is bool
        and type(snapshot.policy_permits_partial_or_stale) is bool
        and _canonical_str(snapshot.policy_ref, is_identifier)
        and _canonical_str(snapshot.policy_digest, is_content_checksum)
    )
