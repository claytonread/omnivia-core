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

Just before the resolver runs, every query value is bound by hand into a tuple of
exact built-in values. The resolver must echo that query object by identity, and
the binding rebuilt afterwards must equal the first. That closes a synchronous
rewrite of the query, through `object.__setattr__`, during `resolve`. It says
nothing about concurrent writers or about the query once it has been returned.

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
    COMPLETENESS,
    CONTINUITY,
    EVIDENCE_AVAILABILITY,
    INITIAL_READINESS,
    SCHEMA_COMPATIBILITY,
    DatasetStateObservation,
    DatasetStateRecord,
)

#: The largest value a bounded microsecond field may carry: a signed 64-bit integer.
_MAX_US: Final = 2**63 - 1

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
    initial_readiness: str
    completeness: str
    continuity: str
    schema_compatibility: str
    evidence_availability: str
    freshness_deadline_at_us: int | None
    verified_at_us: int
    coverage_digest: ContentChecksum
    source_observation_digest: ContentChecksum
    recorded_at_us: int
    subject_digest: Identifier
    use_class: str
    evaluation_instant: datetime


@dataclass(frozen=True, slots=True)
class AnalysisUseAuthoritySnapshot:
    """The resolver's answer to one query, with the current authority epoch.

    False permission, freshness and policy flags are valid facts, not refusals.
    """

    query: AnalysisUseAuthorityQuery
    authority_epoch: str
    evidence_access_permitted: bool
    freshness_ok: bool
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
    binding = _bind_query(query)
    if binding is None:
        raise AnalysisUseAuthorityRefused()
    snapshot: AnalysisUseAuthoritySnapshot | None = None
    try:
        snapshot = resolver.resolve(query)
        answered = _answers(snapshot, query, binding)
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
    # Every value is read once, validated, and held before `_utc` runs a caller-owned
    # timezone hook. The query is built from these locals, never from a reread.
    workspace_id = dataset.workspace_id
    generation = dataset.state_generation
    dataset_id = observation.dataset_id
    dataset_revision = observation.dataset_revision
    dataset_incarnation = observation.dataset_incarnation
    manifest = (
        observation.manifest_id,
        observation.manifest_revision,
        observation.manifest_digest,
    )
    scope_digest = observation.scope_digest
    epoch = observation.observed_authority_epoch
    initial_readiness = observation.initial_readiness
    completeness = observation.completeness
    continuity = observation.continuity
    schema_compatibility = observation.schema_compatibility
    evidence_availability = observation.evidence_availability
    freshness_deadline = observation.freshness_deadline_at_us
    verified_at = observation.verified_at_us
    coverage_digest = dataset.coverage_digest
    source_observation_digest = dataset.source_observation_digest
    recorded_at = dataset.recorded_at_us
    manifest_absent = all(value is None for value in manifest)
    manifest_present = (
        _canonical_str(manifest[0], is_identifier)
        and _canonical_str(manifest[1], is_identifier)
        and _canonical_str(manifest[2], is_content_checksum)
    )
    if not (
        _canonical_str(workspace_id, is_workspace_id)
        and workspace_id == subject.workspace_id
        and type(generation) is int
        and generation >= 1
        and _canonical_str(dataset_id, is_identifier)
        and _canonical_str(dataset_revision, is_identifier)
        and _canonical_str(dataset_incarnation, is_identifier)
        and (manifest_absent or manifest_present)
        and _canonical_str(scope_digest, is_content_checksum)
        and _canonical_str(epoch, is_identifier)
        and _canonical_str(initial_readiness, INITIAL_READINESS.__contains__)
        and _canonical_str(completeness, COMPLETENESS.__contains__)
        and _canonical_str(continuity, CONTINUITY.__contains__)
        and _canonical_str(schema_compatibility, SCHEMA_COMPATIBILITY.__contains__)
        and _canonical_str(evidence_availability, EVIDENCE_AVAILABILITY.__contains__)
        and (freshness_deadline is None or _bounded_us(freshness_deadline))
        and _bounded_us(verified_at)
        and _canonical_str(coverage_digest, is_content_checksum)
        and _canonical_str(source_observation_digest, is_content_checksum)
        and _bounded_us(recorded_at)
        and _canonical_str(subject_digest, is_identifier)
        and type(use_class) is str
        and use_class in _USE_CLASSES
    ):
        raise AnalysisUseAuthorityRefused()
    subject_binding = _bind_subject(subject)
    instant = _utc(evaluation_instant)
    # The hook may have rewritten the subject. Only exact built-in values compare.
    if instant is None or subject_binding is None or _bind_subject(subject) != subject_binding:
        raise AnalysisUseAuthorityRefused()
    return AnalysisUseAuthorityQuery(
        subject=subject,
        dataset_id=dataset_id,
        dataset_revision=dataset_revision,
        dataset_incarnation=dataset_incarnation,
        state_generation=generation,
        manifest_id=manifest[0],
        manifest_revision=manifest[1],
        manifest_digest=manifest[2],
        scope_digest=scope_digest,
        observed_authority_epoch=epoch,
        initial_readiness=initial_readiness,
        completeness=completeness,
        continuity=continuity,
        schema_compatibility=schema_compatibility,
        evidence_availability=evidence_availability,
        freshness_deadline_at_us=freshness_deadline,
        verified_at_us=verified_at,
        coverage_digest=coverage_digest,
        source_observation_digest=source_observation_digest,
        recorded_at_us=recorded_at,
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


def _bounded_us(value: object) -> bool:
    # Exact `int` only: `bool` and every subclass fail the proof before any comparison.
    return type(value) is int and 1 <= value <= _MAX_US


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


def _bind_query(query: object) -> tuple[object, ...] | None:
    """Every query value as an exact built-in str, int or None, in field order.

    The subject and instant are bound by their own helpers, so the query's own fields
    are the only thing read here. `None` if any value is not of an exact built-in type.
    Comparing two bindings therefore runs only built-in equality: never a subclass
    hook, a dataclass `__eq__` or a timezone method. Frozen dataclasses do not stop
    `object.__setattr__`, so this is what proves the query was not rewritten while the
    resolver held it.
    """
    if type(query) is not AnalysisUseAuthorityQuery:
        return None
    subject = _bind_subject(query.subject)
    instant = _bind_instant(query.evaluation_instant)
    flat = (
        query.dataset_id,
        query.dataset_revision,
        query.dataset_incarnation,
        query.state_generation,
        query.manifest_id,
        query.manifest_revision,
        query.manifest_digest,
        query.scope_digest,
        query.observed_authority_epoch,
        query.initial_readiness,
        query.completeness,
        query.continuity,
        query.schema_compatibility,
        query.evidence_availability,
        query.freshness_deadline_at_us,
        query.verified_at_us,
        query.coverage_digest,
        query.source_observation_digest,
        query.recorded_at_us,
        query.subject_digest,
        query.use_class,
    )
    if subject is None or instant is None or not all(_exact_scalar(value) for value in flat):
        return None
    return (subject, *flat, instant)


def _exact_scalar(value: object) -> bool:
    # Identity tests only: a subclass, `bool`, or any other type is not bound.
    return type(value) is str or type(value) is int or value is None


def _bind_subject(subject: object) -> tuple[object, ...] | None:
    if type(subject) is not AnalysisUseAuthoritySubject:
        return None
    authority = subject.authority
    if type(authority) is not GrantedAuthority:
        return None
    capabilities = authority.capabilities
    if type(capabilities) is not tuple or not all(
        type(item) is CapabilityRef for item in capabilities
    ):
        return None
    # Every capability, not just the first, flattened to its own (id, version) pair.
    pairs = tuple((item.id, item.version) for item in capabilities)
    names = (subject.operation, subject.workspace_id, subject.purpose, authority.principal_id)
    roles = authority.roles
    scopes = subject.scopes
    if not (_strs(names) and _strs(roles) and _strs(scopes) and all(_strs(pair) for pair in pairs)):
        return None
    operation, workspace_id, purpose, principal_id = names
    return (operation, workspace_id, (principal_id, roles, pairs), scopes, purpose)


def _bind_instant(value: object) -> tuple[int, ...] | None:
    # Zone by identity, then the plain integer fields of an exact `datetime`. No
    # offset, name or conversion call can reach a resolver-supplied tzinfo.
    if type(value) is not datetime or value.tzinfo is not UTC:
        return None
    return (
        value.year,
        value.month,
        value.day,
        value.hour,
        value.minute,
        value.second,
        value.microsecond,
        value.fold,
    )


def _strs(values: object) -> bool:
    return type(values) is tuple and all(type(value) is str for value in values)


def _answers(
    snapshot: object, query: AnalysisUseAuthorityQuery, binding: tuple[object, ...]
) -> bool:
    # Identity, not `==`: a resolver-supplied query may carry spoofed equality. The
    # rebuilt binding then shows the resolver did not rewrite the query it echoed.
    return (
        type(snapshot) is AnalysisUseAuthoritySnapshot
        and type(snapshot.query) is AnalysisUseAuthorityQuery
        and snapshot.query is query
        and _bind_query(query) == binding
        and _canonical_str(snapshot.authority_epoch, is_identifier)
        and type(snapshot.evidence_access_permitted) is bool
        and type(snapshot.freshness_ok) is bool
        and type(snapshot.policy_permits_partial_or_stale) is bool
        and _canonical_str(snapshot.policy_ref, is_identifier)
        and _canonical_str(snapshot.policy_digest, is_content_checksum)
    )
