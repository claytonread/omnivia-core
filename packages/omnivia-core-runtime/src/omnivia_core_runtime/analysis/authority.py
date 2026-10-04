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
    is_content_checksum,
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
    if not (
        request.operation == authorization.operation
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
    except Exception:  # noqa: BLE001 - any failure to answer is a refusal
        snapshot = None
    if snapshot is None or not _answers(snapshot, query):
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
        is_identifier(manifest[0])
        and is_identifier(manifest[1])
        and is_content_checksum(manifest[2])
    )
    instant = _utc(evaluation_instant)
    if not (
        is_workspace_id(dataset.workspace_id)
        and dataset.workspace_id == subject.workspace_id
        and type(dataset.state_generation) is int
        and dataset.state_generation >= 1
        and is_identifier(observation.dataset_id)
        and is_identifier(observation.dataset_revision)
        and is_identifier(observation.dataset_incarnation)
        and (manifest_absent or manifest_present)
        and is_content_checksum(observation.scope_digest)
        and is_identifier(observation.observed_authority_epoch)
        and is_identifier(subject_digest)
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


def _valid_subject(subject: object) -> bool:
    return (
        type(subject) is AnalysisUseAuthoritySubject
        and is_operation_name(subject.operation)
        and is_workspace_id(subject.workspace_id)
        and _valid_authority(subject.authority)
        and type(subject.scopes) is tuple
        and all(is_scope(scope) for scope in subject.scopes)
        and is_purpose(subject.purpose)
    )


def _valid_authority(authority: object) -> bool:
    return (
        type(authority) is GrantedAuthority
        and is_identifier(authority.principal_id)
        and type(authority.roles) is tuple
        and all(is_identifier(role) for role in authority.roles)
        and type(authority.capabilities) is tuple
        and all(type(item) is CapabilityRef for item in authority.capabilities)
    )


def _utc(value: object) -> datetime | None:
    if type(value) is not datetime or value.utcoffset() is None:
        return None
    return value.astimezone(UTC)


def _answers(snapshot: object, query: AnalysisUseAuthorityQuery) -> bool:
    return (
        type(snapshot) is AnalysisUseAuthoritySnapshot
        and type(snapshot.query) is AnalysisUseAuthorityQuery
        and snapshot.query == query
        and is_identifier(snapshot.authority_epoch)
        and type(snapshot.evidence_access_permitted) is bool
        and type(snapshot.policy_permits_partial_or_stale) is bool
        and is_identifier(snapshot.policy_ref)
        and is_content_checksum(snapshot.policy_digest)
    )
