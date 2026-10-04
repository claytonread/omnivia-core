"""Analysis-use authority seam (SPEC-CORE-DATA-001 §13.3): conversion and resolution.

Every context, grant, dataset observation and resolver answer here is a real
production type, and the valid context comes from `authorize_application_request`
rather than a hand-built authority. The properties proved are the ones a later
change could silently break: that the subject is taken only from the six consumed
bindings and only where they agree by exact value, that a dataset observation is
flattened into one query without loss, that the resolver is asked exactly once with
exactly that query and must echo it back by identity, and that every refusal is the
one fixed reason with no caller, resolver or timezone text reachable from it.

Hostile values are built from a `str` subclass whose equality answers True and
records that it ran. A guard fixture fails any test that let that equality, or any
other recorded hostile hook, run. The resolver is also shown unable to rebind the
query it was handed by writing through `object.__setattr__` while it runs. The
payloads are never printed or compared, so a leak shows up as an absent assertion
rather than as a message that repeats the secret.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from dataclasses import fields, replace
from datetime import UTC, date, datetime, timedelta, timezone, tzinfo
from typing import Any, cast

import pytest
from omnivia_core_runtime.analysis import authority as authority_module
from omnivia_core_runtime.analysis.authority import (
    REFUSE_ANALYSIS_USE_AUTHORITY,
    AnalysisUseAuthorityQuery,
    AnalysisUseAuthorityRefused,
    AnalysisUseAuthoritySnapshot,
    AnalysisUseAuthoritySubject,
    analysis_use_authority_subject_from_context,
    resolve_analysis_use_authority,
    resolve_analysis_use_authority_for_subject,
)
from omnivia_core_runtime.service.authorization import (
    AuthenticatedSession,
    AuthorizedApplicationContext,
    ServiceBinding,
    authorize_application_request,
)
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

from omnivia_core.contracts.v1 import (
    CONTRACT_VERSION,
    CapabilityRef,
    CapabilityRequirement,
    ClientIdentity,
    GrantedAuthority,
    RequestEnvelope,
    RequestMetadata,
)
from omnivia_core.contracts.v1.semantics_result_use import (
    USE_ACTION_INPUT,
    USE_CURRENT_PUBLICATION,
    USE_EXPLORATION,
    USE_HISTORICAL_DISPLAY,
)

#: The raw payload every hostile input carries. Not a valid identifier, so a leak of
#: it cannot be mistaken for an accepted value.
SENTINEL = "do not echo: 5b1d"

#: Every hostile equality, hash or attribute hook that ran, across one test.
EQUALITY_CALLS: list[str] = []

OPERATION = "memory.get"
OTHER_OPERATION = "analysis.start"
PRINCIPAL = "principal-1"
OTHER_PRINCIPAL = "principal-2"
WORKSPACE = "ws-0001"
OTHER_WORKSPACE = "ws-0002"
INSTALLATION = "inst-0001"
PURPOSE = "operations.read"
OTHER_PURPOSE = "operations.write"
SCOPE = "memory:read"
OTHER_SCOPE = "memory:write"
BINDING = ServiceBinding(installation_id=INSTALLATION)
CLIENT = ClientIdentity(id="test-client", version="0.1.0")
SUPPORTED = (CapabilityRef(id="memory.read", version="1.4"),)

DATASET_ID = "dataset-1"
SUBJECT_DIGEST = "subject-1"
SCOPE_DIGEST = "sha256:" + "5" * 64
MANIFEST_ID = "manifest-1"
MANIFEST_REVISION = "manifest-rev-1"
MANIFEST_DIGEST = "sha256:" + "6" * 64
POLICY_REF = "policy-1"
POLICY_DIGEST = "sha256:" + "7" * 64
EPOCH_OBSERVED = "epoch-observed-1"
EPOCH_CURRENT = "epoch-current-2"
INSTANT = datetime(2026, 10, 4, 1, 0, tzinfo=UTC)
REFUSAL = "analysis_use_authority_unavailable"


class _Spoof(str):
    """A `str` whose equality answers True and records that it was asked."""

    def __eq__(self, other: object) -> bool:
        EQUALITY_CALLS.append("eq")
        return True

    def __ne__(self, other: object) -> bool:
        EQUALITY_CALLS.append("ne")
        return False

    def __hash__(self) -> int:
        return str.__hash__(self)


class _Ctx(OperationContext):
    """An `OperationContext` subclass, to show the exact-type gate."""


class _CapabilitySubclass(CapabilityRef):
    """A `CapabilityRef` subclass with valid values, to show the exact-type gate."""


class _SubjectSubclass(AnalysisUseAuthoritySubject):
    """A subject subclass with valid values, to show the exact-type gate."""


class _AuthoritySubclass(GrantedAuthority):
    """A `GrantedAuthority` subclass with valid values, to show the exact-type gate."""


class _IntSubclass(int):
    """An `int` subclass that compares equal to the same plain int."""


class _SnapshotSubclass(AnalysisUseAuthoritySnapshot):
    """A snapshot subclass with valid values, to show the exact-type gate."""


class _QuerySubclass(AnalysisUseAuthorityQuery):
    """A query copy whose equality is hostile."""

    def __eq__(self, other: object) -> bool:
        EQUALITY_CALLS.append("query-eq")
        return True


class _Instant(datetime):
    """A `datetime` subclass carrying a valid aware instant."""


class _FixedZero(tzinfo):
    """A valid, non-UTC zone with a zero offset, which must still normalize to UTC."""

    def utcoffset(self, dt: datetime | None) -> timedelta:
        return timedelta(0)

    def dst(self, dt: datetime | None) -> timedelta:
        return timedelta(0)

    def tzname(self, dt: datetime | None) -> str:
        return "ZERO"


class _HostileZone(tzinfo):
    """A zone whose offset fails: it raises, returns the wrong type, is out of range,
    or fails only on its second read, which is inside normalization."""

    def __init__(self, fault: str) -> None:
        self._fault = fault
        self._reads = 0

    def utcoffset(self, dt: datetime | None) -> Any:
        self._reads += 1
        if self._fault == "raises":
            raise RuntimeError(SENTINEL)
        if self._fault == "raises_on_second_read" and self._reads > 1:
            raise RuntimeError(SENTINEL)
        if self._fault == "wrong_type":
            return cast(Any, SENTINEL)
        if self._fault == "out_of_range":
            return timedelta(hours=24)
        return timedelta(0)

    def dst(self, dt: datetime | None) -> timedelta | None:
        return None

    def tzname(self, dt: datetime | None) -> str:
        return "HOSTILE"


@pytest.fixture(autouse=True)
def _no_hostile_equality_ran() -> Any:
    EQUALITY_CALLS.clear()
    yield
    assert EQUALITY_CALLS == []


# ---------------------------------------------------------------------------
# Builders: a real authorized context and a real dataset observation.
# ---------------------------------------------------------------------------


def _valid_request() -> RequestEnvelope:
    metadata = RequestMetadata(
        request_id="req-1",
        correlation_id="corr-1",
        trace_id="trace-1",
        api_version=CONTRACT_VERSION,
        client=CLIENT,
        scopes=(SCOPE,),
        purpose=PURPOSE,
        required_capabilities=(
            CapabilityRequirement(id="memory.read", minimum_version="1.0", required=True),
        ),
        workspace_id=WORKSPACE,
    )
    return RequestEnvelope(operation=OPERATION, metadata=metadata, input={})


def _session() -> AuthenticatedSession:
    return AuthenticatedSession(
        principal_id=PRINCIPAL,
        roles=frozenset({"reader", "writer"}),
        installations=frozenset({INSTALLATION}),
        workspaces=frozenset({WORKSPACE}),
        operations=frozenset({OPERATION}),
        scopes=frozenset({SCOPE}),
        purposes=frozenset({PURPOSE}),
        capabilities=(CapabilityRef(id="memory.read", version="1.4"),),
    )


def _valid_context() -> OperationContext:
    """A context the production authorizer produced, with its authority pass-through."""
    request = _valid_request()
    authorized = authorize_application_request(
        request,
        session=_session(),
        binding=BINDING,
        supported_capabilities=SUPPORTED,
    )
    return OperationContext(
        request=request,
        principal=authorized.principal_id,
        workspace_id=WORKSPACE,
        granted_operations=frozenset({OPERATION}),
        authority=authorized.authority,
        scopes=authorized.scopes,
        purpose=authorized.purpose,
        authorization=authorized,
    )


def _observation(**overrides: Any) -> DatasetStateObservation:
    fields_: dict[str, Any] = {
        "dataset_id": DATASET_ID,
        "dataset_revision": "rev-1",
        "dataset_incarnation": "inc-1",
        "initial_readiness": "ready",
        "completeness": "complete",
        "continuity": "verified",
        "operational_health": "healthy",
        "schema_compatibility": "compatible",
        "content_observation": "nonempty",
        "evidence_availability": "available",
        "observed_authority_epoch": EPOCH_OBSERVED,
        "scope_digest": SCOPE_DIGEST,
        "coverage": {"scope_digest": SCOPE_DIGEST, "proof_kind": "complete_enumeration"},
        "source_observation": {"source_ref": {"id": "source-erp"}},
        "verified_at_us": 1_790_000_000_000_000,
        "freshness_deadline_at_us": None,
        "manifest_id": MANIFEST_ID,
        "manifest_revision": MANIFEST_REVISION,
        "manifest_digest": MANIFEST_DIGEST,
    }
    fields_.update(overrides)
    return DatasetStateObservation(**fields_)


def _record(**overrides: Any) -> DatasetStateRecord:
    fields_: dict[str, Any] = {
        "workspace_id": WORKSPACE,
        "state_generation": 3,
        "observation": _observation(),
        "coverage_digest": "sha256:" + "8" * 64,
        "source_observation_digest": "sha256:" + "9" * 64,
        "recorded_at_us": 1_790_000_000_000_001,
        "audit_ref": "audit-1",
    }
    fields_.update(overrides)
    return DatasetStateRecord(**fields_)


def _snapshot(query: AnalysisUseAuthorityQuery, **overrides: Any) -> AnalysisUseAuthoritySnapshot:
    fields_: dict[str, Any] = {
        "query": query,
        "authority_epoch": EPOCH_CURRENT,
        "evidence_access_permitted": True,
        "freshness_ok": True,
        "policy_permits_partial_or_stale": False,
        "policy_ref": POLICY_REF,
        "policy_digest": POLICY_DIGEST,
    }
    fields_.update(overrides)
    return AnalysisUseAuthoritySnapshot(**fields_)


def _echo(query: AnalysisUseAuthorityQuery) -> AnalysisUseAuthoritySnapshot:
    return _snapshot(query)


def _raise(query: AnalysisUseAuthorityQuery) -> Any:
    raise RuntimeError(SENTINEL)


class _Resolver:
    """Records every query it is asked, and answers with `answer`."""

    def __init__(self, answer: Callable[[AnalysisUseAuthorityQuery], Any] = _echo) -> None:
        self.queries: list[AnalysisUseAuthorityQuery] = []
        self._answer = answer

    def resolve(self, query: AnalysisUseAuthorityQuery) -> Any:
        self.queries.append(query)
        return self._answer(query)


def _resolve(
    context: Any,
    resolver: _Resolver,
    *,
    dataset: Any = None,
    subject_digest: Any = SUBJECT_DIGEST,
    use_class: Any = USE_CURRENT_PUBLICATION,
    instant: Any = INSTANT,
) -> AnalysisUseAuthoritySnapshot:
    return resolve_analysis_use_authority(
        context,
        dataset=dataset if dataset is not None else _record(),
        subject_digest=subject_digest,
        use_class=use_class,
        evaluation_instant=instant,
        resolver=resolver,
    )


def _expected_subject(context: OperationContext) -> AnalysisUseAuthoritySubject:
    return AnalysisUseAuthoritySubject(
        operation=OPERATION,
        workspace_id=WORKSPACE,
        authority=GrantedAuthority(
            principal_id=PRINCIPAL,
            roles=context.authority.roles if context.authority else (),
            capabilities=(CapabilityRef(id="memory.read", version="1.4"),),
        ),
        scopes=(SCOPE,),
        purpose=PURPOSE,
    )


def _assert_plain_refusal(error: AnalysisUseAuthorityRefused, *secrets: str) -> None:
    """The refusal is the exact type, the fixed reason, and nothing chained or quoted."""
    assert type(error) is AnalysisUseAuthorityRefused
    assert error.reason == REFUSAL == REFUSE_ANALYSIS_USE_AUTHORITY
    assert error.args == (REFUSAL,)
    assert error.__cause__ is None
    assert error.__context__ is None
    rendered = "\n".join((str(error), repr(error), repr(error.args)))
    for secret in (SENTINEL, *secrets):
        assert secret not in rendered


def _with_metadata(context: OperationContext, **fields_: Any) -> OperationContext:
    request = replace(
        context.request,
        metadata=replace(context.request.metadata, **fields_),
    )
    return replace(context, request=request)


def _with_authorization(context: OperationContext, **fields_: Any) -> OperationContext:
    """Change only the authorization side, leaving the context's own authority alone."""
    return replace(context, authorization=replace(context.authorization, **fields_))


def _with_authority(context: OperationContext, **fields_: Any) -> OperationContext:
    """Change the authorization and the context's authority to the same value."""
    authorization = replace(context.authorization, **fields_)
    return replace(context, authorization=authorization, authority=authorization.authority)


def _with_capabilities(context: OperationContext, *caps: Any) -> OperationContext:
    return _with_authority(context, capabilities=tuple(caps))


# ---------------------------------------------------------------------------
# 1. A valid context yields the exact subject and consumes exactly six bindings.
# ---------------------------------------------------------------------------


def test_valid_context_yields_the_exact_subject() -> None:
    context = _valid_context()
    subject = analysis_use_authority_subject_from_context(context)
    assert type(subject) is AnalysisUseAuthoritySubject
    assert subject == _expected_subject(context)
    assert subject.operation == OPERATION
    assert subject.workspace_id == WORKSPACE
    assert subject.authority is context.authority
    assert subject.scopes == (SCOPE,)
    assert subject.purpose == PURPOSE


@pytest.mark.parametrize(
    "change",
    [
        pytest.param(lambda c: _with_metadata(c, request_id="req-other"), id="request-id"),
        pytest.param(lambda c: _with_metadata(c, correlation_id="corr-other"), id="correlation-id"),
        pytest.param(lambda c: _with_metadata(c, trace_id="trace-other"), id="trace-id"),
        pytest.param(lambda c: _with_metadata(c, deadline_ms=5_000), id="deadline"),
        pytest.param(lambda c: _with_metadata(c, client=ClientIdentity(id="other", version="9.9.9")), id="client"),
        pytest.param(lambda c: _with_metadata(c, workspace_id=OTHER_WORKSPACE), id="metadata-workspace"),
        pytest.param(lambda c: _with_metadata(c, api_version="1.0"), id="api-version"),
        pytest.param(lambda c: _with_authorization(c, request_id="req-other"), id="authorization-request-id"),
        pytest.param(lambda c: _with_authorization(c, correlation_id="corr-other"), id="authorization-correlation"),
        pytest.param(lambda c: _with_authorization(c, trace_id="trace-other"), id="authorization-trace"),
        pytest.param(lambda c: _with_authorization(c, client=ClientIdentity(id="other", version="9.9.9")), id="authorization-client"),
        pytest.param(lambda c: _with_authorization(c, installation_id="inst-other"), id="installation"),
        pytest.param(lambda c: _with_authorization(c, deadline_ms=5_000), id="authorization-deadline"),
        pytest.param(lambda c: replace(c, granted_operations=frozenset({"x.y"})), id="granted-operations"),
        pytest.param(lambda c: replace(c, service=object()), id="service"),
    ],
)
def test_fields_outside_the_six_bindings_do_not_reach_the_subject(change: Callable[[OperationContext], OperationContext]) -> None:
    baseline = analysis_use_authority_subject_from_context(_valid_context())
    changed = analysis_use_authority_subject_from_context(change(_valid_context()))
    assert changed == baseline


def test_each_consumed_binding_is_reflected_when_both_sides_move_together() -> None:
    base = _valid_context()
    other_roles = ("auditor",)
    moved = replace(
        base,
        workspace_id=OTHER_WORKSPACE,
        scopes=(OTHER_SCOPE,),
        purpose=OTHER_PURPOSE,
        authority=GrantedAuthority(
            principal_id=PRINCIPAL,
            roles=other_roles,
            capabilities=_authority_of(base).capabilities,
        ),
        request=replace(base.request, operation=OTHER_OPERATION),
        authorization=replace(
            base.authorization,
            workspace_id=OTHER_WORKSPACE,
            scopes=(OTHER_SCOPE,),
            purpose=OTHER_PURPOSE,
            roles=other_roles,
            operation=OTHER_OPERATION,
        ),
    )
    subject = analysis_use_authority_subject_from_context(moved)
    assert subject.operation == OTHER_OPERATION
    assert subject.workspace_id == OTHER_WORKSPACE
    assert subject.scopes == (OTHER_SCOPE,)
    assert subject.purpose == OTHER_PURPOSE
    assert subject.authority.roles == other_roles


def test_principal_is_consumed_through_the_authority_it_names() -> None:
    base = _valid_context()
    moved = replace(
        base,
        principal=OTHER_PRINCIPAL,
        authority=GrantedAuthority(
            principal_id=OTHER_PRINCIPAL,
            roles=_authority_of(base).roles,
            capabilities=_authority_of(base).capabilities,
        ),
        authorization=replace(base.authorization, principal_id=OTHER_PRINCIPAL),
    )
    subject = analysis_use_authority_subject_from_context(moved)
    assert subject.authority.principal_id == OTHER_PRINCIPAL


# ---------------------------------------------------------------------------
# 2. Each consumed binding mismatch refuses; legacy None and wrong shapes refuse.
# ---------------------------------------------------------------------------

#: One mismatching value per consumed binding, applied to exactly one side.
MISMATCHES: dict[str, tuple[Callable[[OperationContext], OperationContext], Callable[[OperationContext], OperationContext]]] = {
    "operation": (
        lambda c: replace(c, request=replace(c.request, operation=OTHER_OPERATION)),
        lambda c: _with_authorization(c, operation=OTHER_OPERATION),
    ),
    "principal": (
        lambda c: replace(c, principal=OTHER_PRINCIPAL),
        lambda c: _with_authorization(c, principal_id=OTHER_PRINCIPAL),
    ),
    "workspace": (
        lambda c: replace(c, workspace_id=OTHER_WORKSPACE),
        lambda c: _with_authorization(c, workspace_id=OTHER_WORKSPACE),
    ),
    "authority": (
        lambda c: replace(c, authority=GrantedAuthority(principal_id=PRINCIPAL, roles=("auditor",), capabilities=c.authorization.capabilities)),
        lambda c: _with_authorization(c, roles=("auditor",)),
    ),
    "scopes": (
        lambda c: replace(c, scopes=(OTHER_SCOPE,)),
        lambda c: _with_authorization(c, scopes=(OTHER_SCOPE,)),
    ),
    "purpose": (
        lambda c: replace(c, purpose=OTHER_PURPOSE),
        lambda c: _with_authorization(c, purpose=OTHER_PURPOSE),
    ),
}


@pytest.mark.parametrize("binding", sorted(MISMATCHES))
@pytest.mark.parametrize("side", [0, 1], ids=["context-side", "authorization-side"])
def test_a_one_sided_mismatch_on_any_binding_refuses(binding: str, side: int) -> None:
    context = MISMATCHES[binding][side](_valid_context())
    resolver = _Resolver()
    with pytest.raises(AnalysisUseAuthorityRefused) as raised:
        analysis_use_authority_subject_from_context(context)
    _assert_plain_refusal(raised.value)
    with pytest.raises(AnalysisUseAuthorityRefused) as full:
        _resolve(context, resolver)
    _assert_plain_refusal(full.value)
    assert not resolver.queries


@pytest.mark.parametrize(
    "change",
    [
        pytest.param(lambda c: replace(c, authority=None), id="authority-none"),
        pytest.param(lambda c: replace(c, scopes=None), id="scopes-none"),
        pytest.param(lambda c: replace(c, purpose=None), id="purpose-none"),
        pytest.param(lambda c: replace(c, authorization=None), id="authorization-none"),
        pytest.param(lambda c: replace(c, scopes=[SCOPE]), id="scopes-list"),
        pytest.param(lambda c: replace(c, scopes=frozenset({SCOPE})), id="scopes-frozenset"),
        pytest.param(lambda c: replace(c, authority=_authority_subclass(c)), id="authority-subclass"),
        pytest.param(lambda c: replace(c, purpose=cast(Any, 1)), id="purpose-not-str"),
        pytest.param(lambda c: replace(c, authorization=cast(Any, object())), id="authorization-foreign"),
        pytest.param(lambda c: _with_metadata_request_none(c), id="request-none"),
        pytest.param(lambda c: replace(c, request=cast(Any, object())), id="request-foreign"),
        pytest.param(lambda c: replace(c, request=_copy_as(_RequestSubclass, c.request)), id="request-subclass"),
        pytest.param(lambda c: replace(c, authorization=_copy_as(_AuthorizationSubclass, c.authorization)), id="authorization-subclass"),
        pytest.param(lambda c: replace(c, authority=cast(Any, object())), id="authority-foreign"),
        pytest.param(lambda c: replace(c, scopes=_ScopesSubclass(c.scopes or ())), id="scopes-subclass"),
        pytest.param(lambda c: replace(c, purpose=None, authorization=replace(c.authorization, purpose=cast(Any, 1))), id="purpose-int-both-sides"),
    ],
)
def test_legacy_none_and_wrong_shapes_refuse(change: Callable[[OperationContext], OperationContext]) -> None:
    resolver = _Resolver()
    context = change(_valid_context())
    with pytest.raises(AnalysisUseAuthorityRefused) as direct:
        analysis_use_authority_subject_from_context(context)
    _assert_plain_refusal(direct.value)
    with pytest.raises(AnalysisUseAuthorityRefused) as raised:
        _resolve(context, resolver)
    _assert_plain_refusal(raised.value)
    assert not resolver.queries


class _RequestSubclass(RequestEnvelope):
    """A `RequestEnvelope` subclass with valid values, to show the exact-type gate."""


class _AuthorizationSubclass(AuthorizedApplicationContext):
    """An authorization subclass with valid values, to show the exact-type gate."""


class _ScopesSubclass(tuple):  # type: ignore[type-arg]
    """A tuple subclass holding valid scopes, to show the exact-type gate."""


def _copy_as(cls: type[Any], value: Any) -> Any:
    return cls(**{f.name: getattr(value, f.name) for f in fields(value)})


def _authority_subclass(context: OperationContext) -> GrantedAuthority:
    class _Authority(GrantedAuthority):
        pass

    authority = context.authority
    assert authority is not None
    return _Authority(
        principal_id=authority.principal_id,
        roles=authority.roles,
        capabilities=authority.capabilities,
    )


def _with_metadata_request_none(context: OperationContext) -> OperationContext:
    return replace(context, request=cast(Any, None))


def test_a_context_subclass_or_non_context_refuses() -> None:
    base = _valid_context()
    subclass = _Ctx(**{f.name: getattr(base, f.name) for f in fields(base)})
    for candidate in (subclass, cast(Any, object())):
        with pytest.raises(AnalysisUseAuthorityRefused) as raised:
            analysis_use_authority_subject_from_context(candidate)
        _assert_plain_refusal(raised.value)


# ---------------------------------------------------------------------------
# 3. Exact-type security: hostile str subclasses refuse without equality or leaks.
# ---------------------------------------------------------------------------

#: Each case swaps one consumed value for a hostile copy of its own text, on one side.
SPOOFS: dict[str, tuple[Callable[[OperationContext], OperationContext], Callable[[OperationContext], OperationContext]]] = {
    "operation": (
        lambda c: replace(c, request=replace(c.request, operation=_Spoof(OPERATION))),
        lambda c: _with_authorization(c, operation=_Spoof(OPERATION)),
    ),
    "principal": (
        lambda c: replace(c, principal=_Spoof(PRINCIPAL)),
        lambda c: _with_authorization(c, principal_id=_Spoof(PRINCIPAL)),
    ),
    "workspace": (
        lambda c: replace(c, workspace_id=_Spoof(WORKSPACE)),
        lambda c: _with_authorization(c, workspace_id=_Spoof(WORKSPACE)),
    ),
    "purpose": (
        lambda c: replace(c, purpose=_Spoof(PURPOSE)),
        lambda c: _with_authorization(c, purpose=_Spoof(PURPOSE)),
    ),
    "scope": (
        lambda c: replace(c, scopes=(_Spoof(SCOPE),)),
        lambda c: _with_authorization(c, scopes=(_Spoof(SCOPE),)),
    ),
    "authority-principal": (
        lambda c: replace(c, authority=replace(_authority_of(c), principal_id=_Spoof(PRINCIPAL))),
        lambda c: _with_authorization(c, principal_id=_Spoof(PRINCIPAL)),
    ),
    "authority-role": (
        lambda c: replace(c, authority=replace(_authority_of(c), roles=(_Spoof("auditor"),))),
        lambda c: _with_authorization(c, roles=(_Spoof("auditor"),)),
    ),
    "authority-capability-id": (
        lambda c: replace(c, authority=replace(_authority_of(c), capabilities=(CapabilityRef(id=_Spoof("memory.read"), version="1.4"),))),
        lambda c: _with_authorization(c, capabilities=(CapabilityRef(id=_Spoof("memory.read"), version="1.4"),)),
    ),
    "authority-capability-version": (
        lambda c: replace(c, authority=replace(_authority_of(c), capabilities=(CapabilityRef(id="memory.read", version=_Spoof("1.4")),))),
        lambda c: _with_authorization(c, capabilities=(CapabilityRef(id="memory.read", version=_Spoof("1.4")),)),
    ),
}


def _authority_of(context: OperationContext) -> GrantedAuthority:
    authority = context.authority
    assert authority is not None
    return authority


@pytest.mark.parametrize("binding", sorted(SPOOFS))
@pytest.mark.parametrize("side", [0, 1], ids=["context-side", "authorization-side"])
def test_a_hostile_str_on_either_side_refuses_without_equality(binding: str, side: int) -> None:
    context = SPOOFS[binding][side](_valid_context())
    with pytest.raises(AnalysisUseAuthorityRefused) as raised:
        analysis_use_authority_subject_from_context(context)
    _assert_plain_refusal(raised.value)
    assert EQUALITY_CALLS == []


def test_a_hostile_str_on_the_dataset_workspace_refuses_without_equality() -> None:
    resolver = _Resolver()
    dataset = _record(workspace_id=_Spoof(WORKSPACE))
    with pytest.raises(AnalysisUseAuthorityRefused) as raised:
        _resolve(_valid_context(), resolver, dataset=dataset)
    _assert_plain_refusal(raised.value)
    assert not resolver.queries
    assert EQUALITY_CALLS == []


def test_a_dataset_workspace_that_differs_from_the_subject_refuses() -> None:
    resolver = _Resolver()
    with pytest.raises(AnalysisUseAuthorityRefused) as raised:
        _resolve(_valid_context(), resolver, dataset=_record(workspace_id=OTHER_WORKSPACE))
    _assert_plain_refusal(raised.value)
    assert not resolver.queries


# ---------------------------------------------------------------------------
# 4. A valid dataset state is flattened exactly into the query.
# ---------------------------------------------------------------------------


def test_a_valid_dataset_state_is_flattened_exactly_into_the_query() -> None:
    resolver = _Resolver()
    context = _valid_context()
    snapshot = _resolve(context, resolver, use_class=USE_CURRENT_PUBLICATION)
    assert len(resolver.queries) == 1
    query = resolver.queries[0]
    assert type(query) is AnalysisUseAuthorityQuery
    assert snapshot.query is query
    assert tuple(f.name for f in fields(query)) == QUERY_FIELD_ORDER
    assert query.subject == _expected_subject(context)
    assert query.dataset_id == DATASET_ID
    assert query.dataset_revision == "rev-1"
    assert query.dataset_incarnation == "inc-1"
    assert query.state_generation == 3
    assert query.manifest_id == MANIFEST_ID
    assert query.manifest_revision == MANIFEST_REVISION
    assert query.manifest_digest == MANIFEST_DIGEST
    assert query.scope_digest == SCOPE_DIGEST
    assert query.observed_authority_epoch == EPOCH_OBSERVED
    assert query.initial_readiness == "ready"
    assert query.completeness == "complete"
    assert query.continuity == "verified"
    assert query.schema_compatibility == "compatible"
    assert query.evidence_availability == "available"
    assert query.freshness_deadline_at_us is None
    assert query.verified_at_us == 1_790_000_000_000_000
    assert query.coverage_digest == "sha256:" + "8" * 64
    assert query.source_observation_digest == "sha256:" + "9" * 64
    assert query.recorded_at_us == 1_790_000_000_000_001
    assert query.subject_digest == SUBJECT_DIGEST
    assert query.use_class == USE_CURRENT_PUBLICATION
    assert query.evaluation_instant == INSTANT
    assert query.evaluation_instant.tzinfo is UTC


# ---------------------------------------------------------------------------
# 5. Manifest shapes, identifiers, digests, generations, instants and use classes.
# ---------------------------------------------------------------------------


def test_manifest_all_absent_passes() -> None:
    resolver = _Resolver()
    dataset = _record(observation=_observation(manifest_id=None, manifest_revision=None, manifest_digest=None))
    _resolve(_valid_context(), resolver, dataset=dataset)
    query = resolver.queries[0]
    assert (query.manifest_id, query.manifest_revision, query.manifest_digest) == (None, None, None)


def test_manifest_all_present_passes() -> None:
    resolver = _Resolver()
    _resolve(_valid_context(), resolver)
    query = resolver.queries[0]
    assert (query.manifest_id, query.manifest_revision, query.manifest_digest) == (
        MANIFEST_ID,
        MANIFEST_REVISION,
        MANIFEST_DIGEST,
    )


@pytest.mark.parametrize(
    "absent",
    [
        pytest.param(("manifest_id",), id="only-id-absent"),
        pytest.param(("manifest_revision",), id="only-revision-absent"),
        pytest.param(("manifest_digest",), id="only-digest-absent"),
        pytest.param(("manifest_id", "manifest_revision"), id="id-and-revision-absent"),
        pytest.param(("manifest_id", "manifest_digest"), id="id-and-digest-absent"),
        pytest.param(("manifest_revision", "manifest_digest"), id="revision-and-digest-absent"),
    ],
)
def test_a_mixed_manifest_shape_refuses(absent: tuple[str, ...]) -> None:
    resolver = _Resolver()
    observation = _observation(**{name: None for name in absent})
    with pytest.raises(AnalysisUseAuthorityRefused) as raised:
        _resolve(_valid_context(), resolver, dataset=_record(observation=observation))
    _assert_plain_refusal(raised.value)
    assert not resolver.queries


@pytest.mark.parametrize(
    "field_name, bad",
    [
        pytest.param("manifest_id", "bad id", id="id-space"),
        pytest.param("manifest_id", "", id="id-empty"),
        pytest.param("manifest_id", "m" * 129, id="id-too-long"),
        pytest.param("manifest_id", MANIFEST_ID + "\n", id="id-trailing-newline"),
        pytest.param("manifest_id", _Spoof(MANIFEST_ID), id="id-spoof"),
        pytest.param("manifest_revision", "rev/1", id="revision-slash"),
        pytest.param("manifest_revision", 7, id="revision-int"),
        pytest.param("manifest_digest", "sha256:" + "A" * 64, id="digest-uppercase"),
        pytest.param("manifest_digest", "sha256:" + "a" * 63, id="digest-short"),
        pytest.param("manifest_digest", "sha1:" + "a" * 64, id="digest-algorithm"),
        pytest.param("manifest_digest", "sha256:" + "a" * 64 + "\n", id="digest-trailing-newline"),
        pytest.param("manifest_digest", _Spoof(MANIFEST_DIGEST), id="digest-spoof"),
    ],
)
def test_an_invalid_manifest_field_refuses(field_name: str, bad: Any) -> None:
    resolver = _Resolver()
    observation = _observation(**{field_name: bad})
    with pytest.raises(AnalysisUseAuthorityRefused) as raised:
        _resolve(_valid_context(), resolver, dataset=_record(observation=observation))
    _assert_plain_refusal(raised.value)
    assert not resolver.queries
    assert EQUALITY_CALLS == []


@pytest.mark.parametrize(
    "field_name, bad",
    [
        pytest.param("dataset_id", "bad id", id="dataset-id-space"),
        pytest.param("dataset_id", None, id="dataset-id-none"),
        pytest.param("dataset_id", _Spoof(DATASET_ID), id="dataset-id-spoof"),
        pytest.param("dataset_revision", "", id="revision-empty"),
        pytest.param("dataset_incarnation", "inc\t1", id="incarnation-tab"),
        pytest.param("observed_authority_epoch", "epoch 1", id="observed-epoch-space"),
        pytest.param("observed_authority_epoch", _Spoof(EPOCH_OBSERVED), id="observed-epoch-spoof"),
        pytest.param("scope_digest", "sha256:" + "5" * 63, id="scope-digest-short"),
        pytest.param("scope_digest", "5" * 64, id="scope-digest-bare"),
    ],
)
def test_an_invalid_observation_field_refuses(field_name: str, bad: Any) -> None:
    resolver = _Resolver()
    observation = _observation(**{field_name: bad})
    with pytest.raises(AnalysisUseAuthorityRefused) as raised:
        _resolve(_valid_context(), resolver, dataset=_record(observation=observation))
    _assert_plain_refusal(raised.value)
    assert not resolver.queries
    assert EQUALITY_CALLS == []


@pytest.mark.parametrize(
    "generation",
    [
        pytest.param(True, id="true"),
        pytest.param(False, id="false"),
        pytest.param(0, id="zero"),
        pytest.param(-1, id="negative"),
        pytest.param(1.0, id="float"),
        pytest.param("1", id="string"),
        pytest.param(None, id="none"),
    ],
)
def test_an_invalid_state_generation_refuses(generation: Any) -> None:
    resolver = _Resolver()
    with pytest.raises(AnalysisUseAuthorityRefused) as raised:
        _resolve(_valid_context(), resolver, dataset=_record(state_generation=generation))
    _assert_plain_refusal(raised.value)
    assert not resolver.queries


def test_the_smallest_valid_state_generation_passes() -> None:
    resolver = _Resolver()
    _resolve(_valid_context(), resolver, dataset=_record(state_generation=1))
    assert resolver.queries[0].state_generation == 1


@pytest.mark.parametrize(
    "instant",
    [
        pytest.param(datetime(2026, 10, 4, 1, 0), id="naive"),  # noqa: DTZ001 - the refusal input
        pytest.param(None, id="none"),
        pytest.param(1_790_000_000, id="epoch-int"),
        pytest.param("2026-10-04T01:00:00+00:00", id="iso-string"),
        pytest.param(date(2026, 10, 4), id="date"),
        pytest.param(_Instant(2026, 10, 4, 1, 0, tzinfo=UTC), id="datetime-subclass"),
    ],
)
def test_an_invalid_evaluation_instant_refuses(instant: Any) -> None:
    resolver = _Resolver()
    with pytest.raises(AnalysisUseAuthorityRefused) as raised:
        _resolve(_valid_context(), resolver, instant=instant)
    _assert_plain_refusal(raised.value)
    assert not resolver.queries


@pytest.mark.parametrize(
    "use_class",
    [
        pytest.param("explore", id="unknown-word"),
        pytest.param("Exploration", id="wrong-case"),
        pytest.param("exploration ", id="trailing-space"),
        pytest.param("certified", id="unlisted"),
        pytest.param("action-input", id="hyphenated"),
        pytest.param("", id="empty"),
        pytest.param(None, id="none"),
        pytest.param(1, id="int"),
        pytest.param(_Spoof(USE_EXPLORATION), id="spoof-of-a-real-class"),
    ],
)
def test_an_invalid_use_class_refuses(use_class: Any) -> None:
    resolver = _Resolver()
    with pytest.raises(AnalysisUseAuthorityRefused) as raised:
        _resolve(_valid_context(), resolver, use_class=use_class)
    _assert_plain_refusal(raised.value)
    assert not resolver.queries
    assert EQUALITY_CALLS == []


@pytest.mark.parametrize(
    "subject_digest",
    [
        pytest.param("", id="empty"),
        pytest.param("subject 1", id="space"),
        pytest.param("s" * 129, id="too-long"),
        pytest.param(None, id="none"),
        pytest.param(_Spoof(SUBJECT_DIGEST), id="spoof"),
    ],
)
def test_an_invalid_subject_digest_refuses(subject_digest: Any) -> None:
    resolver = _Resolver()
    with pytest.raises(AnalysisUseAuthorityRefused) as raised:
        _resolve(_valid_context(), resolver, subject_digest=subject_digest)
    _assert_plain_refusal(raised.value)
    assert not resolver.queries
    assert EQUALITY_CALLS == []


@pytest.mark.parametrize(
    "use_class",
    [
        pytest.param(USE_EXPLORATION, id="exploration"),
        pytest.param(USE_HISTORICAL_DISPLAY, id="historical-display"),
        pytest.param(USE_CURRENT_PUBLICATION, id="current-publication"),
        pytest.param(USE_ACTION_INPUT, id="action-input"),
    ],
)
def test_every_use_class_passes(use_class: str) -> None:
    resolver = _Resolver()
    snapshot = _resolve(_valid_context(), resolver, use_class=use_class)
    assert resolver.queries[0].use_class == use_class
    assert snapshot.query is resolver.queries[0]


# ---------------------------------------------------------------------------
# 6b. DatasetState evidence: vocabularies, digests, bounded instants, the deadline
# and the freshness flag. Every value is exact-typed before any hook can run.
# ---------------------------------------------------------------------------

#: The query's fields in declaration order. Pinned, so a reorder is a visible change.
QUERY_FIELD_ORDER = (
    "subject",
    "dataset_id",
    "dataset_revision",
    "dataset_incarnation",
    "state_generation",
    "manifest_id",
    "manifest_revision",
    "manifest_digest",
    "scope_digest",
    "observed_authority_epoch",
    "initial_readiness",
    "completeness",
    "continuity",
    "schema_compatibility",
    "evidence_availability",
    "freshness_deadline_at_us",
    "verified_at_us",
    "coverage_digest",
    "source_observation_digest",
    "recorded_at_us",
    "subject_digest",
    "use_class",
    "evaluation_instant",
)

#: Every (field, word) pair the observation vocabularies admit.
VALID_EVIDENCE_WORDS = [
    ("initial_readiness", "not_started"),
    ("initial_readiness", "initialising"),
    ("initial_readiness", "catching_up"),
    ("initial_readiness", "ready"),
    ("initial_readiness", "blocked"),
    ("completeness", "complete"),
    ("completeness", "partial"),
    ("completeness", "unknown"),
    ("continuity", "verified"),
    ("continuity", "gap_detected"),
    ("continuity", "unknown"),
    ("continuity", "not_applicable"),
    ("schema_compatibility", "compatible"),
    ("schema_compatibility", "requires_review"),
    ("schema_compatibility", "incompatible"),
    ("schema_compatibility", "unknown"),
    ("evidence_availability", "available"),
    ("evidence_availability", "limited"),
    ("evidence_availability", "unavailable"),
]

#: Integers that no microsecond field may carry: outside `1..2**63-1`, or not exact `int`.
NOT_A_MICROSECOND = [
    pytest.param(0, id="zero"),
    pytest.param(-1, id="negative"),
    pytest.param(2**63, id="overflow"),
    pytest.param(True, id="true"),
    pytest.param(False, id="false"),
    pytest.param(1.0, id="float"),
    pytest.param("1790000000000000", id="string"),
    pytest.param(_IntSubclass(1_790_000_000_000_000), id="int-subclass"),
]


def _refuse_on(dataset: DatasetStateRecord) -> None:
    """Assert the seam refuses `dataset` plainly, before the resolver is ever asked."""
    resolver = _Resolver()
    with pytest.raises(AnalysisUseAuthorityRefused) as raised:
        _resolve(_valid_context(), resolver, dataset=dataset)
    _assert_plain_refusal(raised.value)
    assert not resolver.queries
    assert EQUALITY_CALLS == []


def test_the_valid_word_list_covers_each_vocabulary_exactly() -> None:
    vocabularies = {
        "initial_readiness": INITIAL_READINESS,
        "completeness": COMPLETENESS,
        "continuity": CONTINUITY,
        "schema_compatibility": SCHEMA_COMPATIBILITY,
        "evidence_availability": EVIDENCE_AVAILABILITY,
    }
    for field_name, vocabulary in vocabularies.items():
        assert {word for name, word in VALID_EVIDENCE_WORDS if name == field_name} == vocabulary


@pytest.mark.parametrize(("field_name", "word"), VALID_EVIDENCE_WORDS)
def test_every_listed_evidence_word_is_carried_through_exactly(field_name: str, word: str) -> None:
    resolver = _Resolver()
    _resolve(_valid_context(), resolver, dataset=_record(observation=_observation(**{field_name: word})))
    assert getattr(resolver.queries[0], field_name) == word
    assert type(getattr(resolver.queries[0], field_name)) is str


@pytest.mark.parametrize(
    ("field_name", "bad"),
    [
        pytest.param("initial_readiness", "Ready", id="readiness-case"),
        pytest.param("initial_readiness", "ready ", id="readiness-trailing-space"),
        pytest.param("initial_readiness", "done", id="readiness-unlisted"),
        pytest.param("initial_readiness", None, id="readiness-none"),
        pytest.param("initial_readiness", _Spoof("ready"), id="readiness-spoof"),
        pytest.param("completeness", "Complete", id="completeness-case"),
        pytest.param("completeness", _Spoof("complete"), id="completeness-spoof"),
        pytest.param("continuity", "gap-detected", id="continuity-hyphenated"),
        pytest.param("continuity", 0, id="continuity-int"),
        pytest.param("continuity", _Spoof("verified"), id="continuity-spoof"),
        pytest.param("schema_compatibility", "compatible_ish", id="schema-unlisted"),
        pytest.param("schema_compatibility", None, id="schema-none"),
        pytest.param("schema_compatibility", _Spoof("compatible"), id="schema-spoof"),
        pytest.param("evidence_availability", "full", id="evidence-unlisted"),
        pytest.param("evidence_availability", b"available", id="evidence-bytes"),
        pytest.param("evidence_availability", _Spoof("available"), id="evidence-spoof"),
    ],
)
def test_an_evidence_word_outside_its_vocabulary_refuses(field_name: str, bad: Any) -> None:
    _refuse_on(_record(observation=_observation(**{field_name: bad})))


@pytest.mark.parametrize("field_name", ["coverage_digest", "source_observation_digest"])
@pytest.mark.parametrize(
    "bad",
    [
        pytest.param("sha256:" + "A" * 64, id="uppercase"),
        pytest.param("sha256:" + "a" * 63, id="short"),
        pytest.param("sha256:" + "a" * 65, id="long"),
        pytest.param("sha1:" + "a" * 64, id="algorithm"),
        pytest.param("a" * 64, id="bare-hex"),
        pytest.param("sha256:" + "a" * 64 + "\n", id="trailing-newline"),
        pytest.param(None, id="none"),
        pytest.param(_Spoof("sha256:" + "a" * 64), id="spoof"),
    ],
)
def test_an_invalid_evidence_digest_refuses(field_name: str, bad: Any) -> None:
    _refuse_on(_record(**{field_name: bad}))


@pytest.mark.parametrize("bad", [*NOT_A_MICROSECOND, pytest.param(None, id="none")])
def test_an_invalid_verified_instant_refuses(bad: Any) -> None:
    _refuse_on(_record(observation=_observation(verified_at_us=bad)))


@pytest.mark.parametrize("bad", [*NOT_A_MICROSECOND, pytest.param(None, id="none")])
def test_an_invalid_recorded_instant_refuses(bad: Any) -> None:
    _refuse_on(_record(recorded_at_us=bad))


@pytest.mark.parametrize("bad", NOT_A_MICROSECOND)
def test_an_invalid_freshness_deadline_refuses(bad: Any) -> None:
    _refuse_on(_record(observation=_observation(freshness_deadline_at_us=bad)))


@pytest.mark.parametrize(
    "deadline",
    [
        pytest.param(None, id="absent"),
        pytest.param(1, id="smallest"),
        pytest.param(1_790_000_000_500_000, id="typical"),
        pytest.param(2**63 - 1, id="largest"),
    ],
)
def test_a_freshness_deadline_that_is_none_or_bounded_is_carried_through(deadline: int | None) -> None:
    resolver = _Resolver()
    _resolve(_valid_context(), resolver, dataset=_record(observation=_observation(freshness_deadline_at_us=deadline)))
    assert resolver.queries[0].freshness_deadline_at_us == deadline
    assert type(resolver.queries[0].freshness_deadline_at_us) is type(deadline)


@pytest.mark.parametrize("bound", [1, 2**63 - 1])
def test_the_microsecond_bounds_are_inclusive_for_both_instants(bound: int) -> None:
    resolver = _Resolver()
    dataset = _record(observation=_observation(verified_at_us=bound), recorded_at_us=bound)
    _resolve(_valid_context(), resolver, dataset=dataset)
    query = resolver.queries[0]
    assert (query.verified_at_us, query.recorded_at_us) == (bound, bound)


@pytest.mark.parametrize("fresh", [True, False])
def test_freshness_true_and_false_are_both_accepted(fresh: bool) -> None:
    resolver = _Resolver(lambda q: _snapshot(q, freshness_ok=fresh))
    snapshot = _resolve(_valid_context(), resolver)
    assert snapshot.freshness_ok is fresh
    assert type(snapshot.freshness_ok) is bool


@pytest.mark.parametrize(
    "freshness",
    [
        pytest.param(None, id="none"),
        pytest.param(1, id="one"),
        pytest.param(0, id="zero"),
        pytest.param("True", id="string"),
        pytest.param(_IntSubclass(1), id="int-subclass"),
    ],
)
def test_a_non_bool_freshness_flag_refuses(freshness: Any) -> None:
    resolver = _Resolver(lambda q: _snapshot(q, freshness_ok=freshness))
    with pytest.raises(AnalysisUseAuthorityRefused) as raised:
        _resolve(_valid_context(), resolver)
    _assert_plain_refusal(raised.value)
    assert EQUALITY_CALLS == []


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(_IntSubclass(1_790_000_000_500_000), id="int-subclass"),
        pytest.param(True, id="true"),
        pytest.param(1.0, id="float"),
    ],
)
def test_a_deadline_written_in_during_resolve_as_a_non_exact_int_refuses(value: Any) -> None:
    resolver, targets = _mutating_resolver(_at_query, {"freshness_deadline_at_us": value})
    with pytest.raises(AnalysisUseAuthorityRefused) as raised:
        _resolve_subject(_rich_subject(), resolver)
    _assert_plain_refusal(raised.value)
    assert len(resolver.queries) == 1
    assert targets[0].freshness_deadline_at_us is value
    assert EQUALITY_CALLS == []


# ---------------------------------------------------------------------------
# 7. The resolver is asked exactly once, and must echo the query by identity.
# ---------------------------------------------------------------------------


def test_the_resolver_is_invoked_exactly_once_with_the_built_query() -> None:
    resolver = _Resolver()
    snapshot = _resolve(_valid_context(), resolver)
    assert len(resolver.queries) == 1
    assert snapshot.query is resolver.queries[0]


def test_an_equal_copied_query_refuses() -> None:
    resolver = _Resolver(lambda query: _snapshot(replace(query)))
    with pytest.raises(AnalysisUseAuthorityRefused) as raised:
        _resolve(_valid_context(), resolver)
    _assert_plain_refusal(raised.value)
    assert len(resolver.queries) == 1


def test_a_snapshot_for_a_different_query_refuses() -> None:
    other = _record(state_generation=4)
    first = _resolver_query_for(_valid_context(), other)
    resolver = _Resolver(lambda query: _snapshot(first))
    with pytest.raises(AnalysisUseAuthorityRefused) as raised:
        _resolve(_valid_context(), resolver)
    _assert_plain_refusal(raised.value)
    assert len(resolver.queries) == 1


def _resolver_query_for(context: OperationContext, dataset: DatasetStateRecord) -> AnalysisUseAuthorityQuery:
    captured = _Resolver()
    _resolve(context, captured, dataset=dataset)
    return captured.queries[0]


# ---------------------------------------------------------------------------
# 8. Resolver failure and malformed answers collapse to the one fixed refusal.
# ---------------------------------------------------------------------------


def _raise_key(query: AnalysisUseAuthorityQuery) -> Any:
    raise KeyError(SENTINEL)


def _raise_value(query: AnalysisUseAuthorityQuery) -> Any:
    raise ValueError(SENTINEL)


def _raise_refusal(query: AnalysisUseAuthorityQuery) -> Any:
    raise AnalysisUseAuthorityRefused()


@pytest.mark.parametrize(
    "answer",
    [
        pytest.param(_raise, id="runtime-error"),
        pytest.param(_raise_key, id="key-error"),
        pytest.param(_raise_value, id="value-error"),
        pytest.param(_raise_refusal, id="inner-refusal"),
    ],
)
def test_a_failing_resolver_becomes_only_the_fixed_refusal(answer: Callable[[AnalysisUseAuthorityQuery], Any]) -> None:
    resolver = _Resolver(answer)
    with pytest.raises(AnalysisUseAuthorityRefused) as raised:
        _resolve(_valid_context(), resolver)
    _assert_plain_refusal(raised.value)
    assert len(resolver.queries) == 1


@pytest.mark.parametrize(
    "answer",
    [
        pytest.param(lambda q: None, id="none"),
        pytest.param(lambda q: {"authority_epoch": EPOCH_CURRENT}, id="mapping"),
        pytest.param(lambda q: _SnapshotSubclass(**{f.name: getattr(_snapshot(q), f.name) for f in fields(_snapshot(q))}), id="snapshot-subclass"),
        pytest.param(lambda q: _snapshot(replace(q)), id="query-copy"),
        pytest.param(lambda q: _snapshot(_QuerySubclass(**{f.name: getattr(q, f.name) for f in fields(q)})), id="query-subclass"),
        pytest.param(lambda q: _snapshot(q, authority_epoch=SENTINEL), id="epoch-invalid-with-payload"),
        pytest.param(lambda q: _snapshot(q, authority_epoch=""), id="epoch-empty"),
        pytest.param(lambda q: _snapshot(q, authority_epoch=_Spoof(EPOCH_CURRENT)), id="epoch-spoof"),
        pytest.param(lambda q: _snapshot(q, authority_epoch=None), id="epoch-none"),
        pytest.param(lambda q: _snapshot(q, evidence_access_permitted=1), id="evidence-int"),
        pytest.param(lambda q: _snapshot(q, evidence_access_permitted=None), id="evidence-none"),
        pytest.param(lambda q: _snapshot(q, evidence_access_permitted="true"), id="evidence-string"),
        pytest.param(lambda q: _snapshot(q, policy_permits_partial_or_stale=0), id="policy-flag-int"),
        pytest.param(lambda q: _snapshot(q, policy_ref=SENTINEL), id="policy-ref-invalid"),
        pytest.param(lambda q: _snapshot(q, policy_ref=""), id="policy-ref-empty"),
        pytest.param(lambda q: _snapshot(q, policy_ref=_Spoof(POLICY_REF)), id="policy-ref-spoof"),
        pytest.param(lambda q: _snapshot(q, policy_digest="sha256:" + "A" * 64), id="policy-digest-uppercase"),
        pytest.param(lambda q: _snapshot(q, policy_digest=SENTINEL), id="policy-digest-invalid"),
        pytest.param(lambda q: _snapshot(q, policy_digest=_Spoof(POLICY_DIGEST)), id="policy-digest-spoof"),
    ],
)
def test_a_malformed_resolver_answer_becomes_only_the_fixed_refusal(answer: Callable[[AnalysisUseAuthorityQuery], Any]) -> None:
    resolver = _Resolver(answer)
    with pytest.raises(AnalysisUseAuthorityRefused) as raised:
        _resolve(_valid_context(), resolver)
    _assert_plain_refusal(raised.value)
    assert len(resolver.queries) == 1
    assert EQUALITY_CALLS == []


# ---------------------------------------------------------------------------
# 9. A resolver-controlled copied query with spoofed equality is never accepted.
# ---------------------------------------------------------------------------


def test_a_copied_query_with_hostile_fields_refuses_without_equality() -> None:
    def answer(query: AnalysisUseAuthorityQuery) -> Any:
        copy = replace(query, dataset_id=_Spoof(DATASET_ID), use_class=_Spoof(USE_EXPLORATION))
        return _snapshot(copy)

    resolver = _Resolver(answer)
    with pytest.raises(AnalysisUseAuthorityRefused) as raised:
        _resolve(_valid_context(), resolver)
    _assert_plain_refusal(raised.value)
    assert EQUALITY_CALLS == []


def test_a_query_subclass_with_hostile_equality_is_never_accepted() -> None:
    def answer(query: AnalysisUseAuthorityQuery) -> Any:
        hostile = _QuerySubclass(**{f.name: getattr(query, f.name) for f in fields(query)})
        return _snapshot(hostile)

    resolver = _Resolver(answer)
    with pytest.raises(AnalysisUseAuthorityRefused) as raised:
        _resolve(_valid_context(), resolver)
    _assert_plain_refusal(raised.value)
    assert EQUALITY_CALLS == []


# ---------------------------------------------------------------------------
# 10. Timezone handling: hostile zones refuse; aware instants normalize to UTC.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fault",
    [
        pytest.param("raises", id="offset-raises"),
        pytest.param("raises_on_second_read", id="normalization-raises"),
        pytest.param("wrong_type", id="offset-wrong-type"),
        pytest.param("out_of_range", id="offset-out-of-range"),
    ],
)
def test_a_hostile_timezone_refuses_without_leaking(fault: str) -> None:
    resolver = _Resolver()
    instant = datetime(2026, 10, 4, 1, 0, tzinfo=_HostileZone(fault))
    with pytest.raises(AnalysisUseAuthorityRefused) as raised:
        _resolve(_valid_context(), resolver, instant=instant)
    _assert_plain_refusal(raised.value)
    assert not resolver.queries


def test_a_naive_instant_refuses() -> None:
    resolver = _Resolver()
    with pytest.raises(AnalysisUseAuthorityRefused) as raised:
        _resolve(_valid_context(), resolver, instant=datetime(2026, 10, 4, 1, 0))  # noqa: DTZ001 - the refusal input
    _assert_plain_refusal(raised.value)
    assert not resolver.queries


@pytest.mark.parametrize(
    "instant, expected",
    [
        pytest.param(datetime(2026, 10, 4, 1, 0, tzinfo=UTC), datetime(2026, 10, 4, 1, 0, tzinfo=UTC), id="utc"),
        pytest.param(datetime(2026, 10, 4, 3, 0, tzinfo=timezone(timedelta(hours=2))), datetime(2026, 10, 4, 1, 0, tzinfo=UTC), id="plus-two"),
        pytest.param(datetime(2026, 10, 3, 20, 0, tzinfo=timezone(timedelta(hours=-5))), datetime(2026, 10, 4, 1, 0, tzinfo=UTC), id="minus-five-crosses-midnight"),
        pytest.param(datetime(2026, 10, 4, 6, 30, tzinfo=timezone(timedelta(hours=5, minutes=30))), datetime(2026, 10, 4, 1, 0, tzinfo=UTC), id="plus-five-thirty"),
        pytest.param(datetime(2026, 10, 4, 1, 0, tzinfo=_FixedZero()), datetime(2026, 10, 4, 1, 0, tzinfo=UTC), id="custom-zero-offset"),
    ],
)
def test_an_aware_instant_normalizes_to_exact_utc(instant: datetime, expected: datetime) -> None:
    resolver = _Resolver()
    _resolve(_valid_context(), resolver, instant=instant)
    normalized = resolver.queries[0].evaluation_instant
    assert normalized == expected
    assert normalized.tzinfo is UTC
    assert normalized.utcoffset() == timedelta(0)


# ---------------------------------------------------------------------------
# 11. Every GrantedAuthority member is validated canonically, not just by type.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "capability_id",
    [
        pytest.param("BAD", id="uppercase"),
        pytest.param("memory", id="no-dot"),
        pytest.param("memory.read!", id="punctuation"),
        pytest.param("memory..read", id="empty-segment"),
        pytest.param("memory.read\n", id="trailing-newline"),
        pytest.param("m" * 129, id="too-long"),
        pytest.param("", id="empty"),
        pytest.param(SENTINEL, id="payload"),
    ],
)
def test_an_invalid_capability_id_refuses_even_with_an_exact_capability_ref(capability_id: str) -> None:
    resolver = _Resolver()
    context = _with_capabilities(_valid_context(), CapabilityRef(id=capability_id, version="1.4"))
    with pytest.raises(AnalysisUseAuthorityRefused) as raised:
        _resolve(context, resolver)
    _assert_plain_refusal(raised.value)
    assert not resolver.queries


@pytest.mark.parametrize(
    "version",
    [
        pytest.param("1.x", id="letter"),
        pytest.param("1", id="single-part"),
        pytest.param("01.2", id="leading-zero"),
        pytest.param("1.4.0", id="three-part"),
        pytest.param("v1.4", id="prefixed"),
        pytest.param("1." + "4" * 40, id="too-long"),
        pytest.param("", id="empty"),
        pytest.param("1.4\n", id="trailing-newline"),
        pytest.param(SENTINEL, id="payload"),
    ],
)
def test_an_invalid_capability_contract_version_refuses_even_with_an_exact_capability_ref(version: str) -> None:
    resolver = _Resolver()
    context = _with_capabilities(_valid_context(), CapabilityRef(id="memory.read", version=version))
    with pytest.raises(AnalysisUseAuthorityRefused) as raised:
        _resolve(context, resolver)
    _assert_plain_refusal(raised.value)
    assert not resolver.queries


@pytest.mark.parametrize(
    "role",
    [
        pytest.param("BAD ROLE", id="space"),
        pytest.param("", id="empty"),
        pytest.param("r" * 129, id="too-long"),
        pytest.param(SENTINEL, id="payload"),
    ],
)
def test_an_invalid_role_refuses(role: str) -> None:
    resolver = _Resolver()
    context = _with_authority(_valid_context(), roles=(role,))
    with pytest.raises(AnalysisUseAuthorityRefused) as raised:
        _resolve(context, resolver)
    _assert_plain_refusal(raised.value)
    assert not resolver.queries


@pytest.mark.parametrize(
    "principal",
    [
        pytest.param("bad principal", id="space"),
        pytest.param("", id="empty"),
        pytest.param(SENTINEL, id="payload"),
    ],
)
def test_an_invalid_principal_refuses(principal: str) -> None:
    resolver = _Resolver()
    context = replace(_with_authority(_valid_context(), principal_id=principal), principal=principal)
    with pytest.raises(AnalysisUseAuthorityRefused) as raised:
        _resolve(context, resolver)
    _assert_plain_refusal(raised.value)
    assert not resolver.queries


@pytest.mark.parametrize(
    "authority_change",
    [
        pytest.param(lambda c: replace(c, authority=replace(_authority_of(c), roles=cast(Any, ["reader"]))), id="roles-list"),
        pytest.param(lambda c: replace(c, authority=replace(_authority_of(c), capabilities=cast(Any, [CapabilityRef(id="memory.read", version="1.4")]))), id="capabilities-list"),
        pytest.param(lambda c: _with_capabilities(c, _CapabilitySubclass(id="memory.read", version="1.4")), id="capability-subclass-valid-values"),
        pytest.param(lambda c: _with_capabilities(c, {"id": "memory.read", "version": "1.4"}), id="capability-mapping"),
    ],
)
def test_authority_members_of_the_wrong_shape_refuse(authority_change: Callable[[OperationContext], OperationContext]) -> None:
    resolver = _Resolver()
    context = authority_change(_valid_context())
    with pytest.raises(AnalysisUseAuthorityRefused) as raised:
        _resolve(context, resolver)
    _assert_plain_refusal(raised.value)
    assert not resolver.queries


def test_a_valid_capability_pair_on_both_sides_is_accepted() -> None:
    resolver = _Resolver()
    context = _with_capabilities(_valid_context(), CapabilityRef(id="memory.read", version="1.4"))
    _resolve(context, resolver)
    assert len(resolver.queries) == 1


def test_an_empty_but_valid_authority_is_a_real_answer() -> None:
    resolver = _Resolver()
    context = _with_authority(_valid_context(), roles=(), capabilities=())
    _resolve(context, resolver)
    assert resolver.queries[0].subject.authority.roles == ()
    assert resolver.queries[0].subject.authority.capabilities == ()


# ---------------------------------------------------------------------------
# 12. Evidence flags are facts; policy and epoch references are checked.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("evidence", [True, False])
@pytest.mark.parametrize("partial", [True, False])
def test_evidence_and_policy_flags_are_facts_in_either_state(evidence: bool, partial: bool) -> None:
    resolver = _Resolver(lambda q: _snapshot(q, evidence_access_permitted=evidence, policy_permits_partial_or_stale=partial))
    snapshot = _resolve(_valid_context(), resolver)
    assert snapshot.evidence_access_permitted is evidence
    assert snapshot.policy_permits_partial_or_stale is partial


@pytest.mark.parametrize(
    "authority_epoch",
    [
        pytest.param("epoch 2", id="space"),
        pytest.param("", id="empty"),
        pytest.param("e" * 129, id="too-long"),
        pytest.param(None, id="none"),
        pytest.param(_Spoof(EPOCH_CURRENT), id="spoof"),
    ],
)
def test_an_invalid_current_epoch_refuses(authority_epoch: Any) -> None:
    resolver = _Resolver(lambda q: _snapshot(q, authority_epoch=authority_epoch))
    with pytest.raises(AnalysisUseAuthorityRefused) as raised:
        _resolve(_valid_context(), resolver)
    _assert_plain_refusal(raised.value)
    assert EQUALITY_CALLS == []


# ---------------------------------------------------------------------------
# 13. The observed epoch in the query and the resolver's epoch are never conflated.
# ---------------------------------------------------------------------------


def test_the_observed_and_current_epochs_are_kept_separate() -> None:
    resolver = _Resolver(lambda q: _snapshot(q, authority_epoch=EPOCH_CURRENT))
    snapshot = _resolve(_valid_context(), resolver)
    query = resolver.queries[0]
    assert query.observed_authority_epoch == EPOCH_OBSERVED
    assert snapshot.authority_epoch == EPOCH_CURRENT
    assert query.observed_authority_epoch != snapshot.authority_epoch
    assert "authority_epoch" not in {f.name for f in fields(query)}


def test_an_answer_that_matches_the_observed_epoch_is_still_its_own_fact() -> None:
    resolver = _Resolver(lambda q: _snapshot(q, authority_epoch=EPOCH_OBSERVED))
    snapshot = _resolve(_valid_context(), resolver)
    assert snapshot.authority_epoch == resolver.queries[0].observed_authority_epoch == EPOCH_OBSERVED


# ---------------------------------------------------------------------------
# The subject-level entry point checks a hand-built subject just as strictly.
# ---------------------------------------------------------------------------


def _subject(**overrides: Any) -> AnalysisUseAuthoritySubject:
    return replace(analysis_use_authority_subject_from_context(_valid_context()), **overrides)


def _resolve_subject(subject: Any, resolver: _Resolver) -> AnalysisUseAuthoritySnapshot:
    return resolve_analysis_use_authority_for_subject(
        subject,
        dataset=_record(),
        subject_digest=SUBJECT_DIGEST,
        use_class=USE_CURRENT_PUBLICATION,
        evaluation_instant=INSTANT,
        resolver=resolver,
    )


def test_the_subject_level_path_builds_the_same_query_as_the_context_path() -> None:
    by_context = _Resolver()
    _resolve(_valid_context(), by_context)
    by_subject = _Resolver()
    _resolve_subject(analysis_use_authority_subject_from_context(_valid_context()), by_subject)
    assert by_subject.queries == by_context.queries


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"operation": "Memory.Get"}, id="operation-uppercase"),
        pytest.param({"operation": ""}, id="operation-empty"),
        pytest.param({"operation": _Spoof(OPERATION)}, id="operation-spoof"),
        pytest.param({"workspace_id": "ws 1"}, id="workspace-space"),
        pytest.param({"workspace_id": _Spoof(WORKSPACE)}, id="workspace-spoof"),
        pytest.param({"purpose": "Operations.Read"}, id="purpose-uppercase"),
        pytest.param({"purpose": _Spoof(PURPOSE)}, id="purpose-spoof"),
        pytest.param({"scopes": ("Memory:Read",)}, id="scope-uppercase"),
        pytest.param({"scopes": [SCOPE]}, id="scopes-list"),
        pytest.param({"scopes": (_Spoof(SCOPE),)}, id="scope-spoof"),
        pytest.param({"authority": None}, id="authority-none"),
        pytest.param(
            {"authority": GrantedAuthority(principal_id="bad principal", roles=("reader",), capabilities=())},
            id="authority-principal-invalid",
        ),
        pytest.param(
            {
                "authority": GrantedAuthority(
                    principal_id=PRINCIPAL,
                    roles=("reader",),
                    capabilities=(CapabilityRef(id="BAD", version="1.4"),),
                )
            },
            id="authority-capability-invalid",
        ),
    ],
)
def test_a_hand_built_subject_is_validated_as_strictly(overrides: dict[str, Any]) -> None:
    resolver = _Resolver()
    with pytest.raises(AnalysisUseAuthorityRefused) as raised:
        _resolve_subject(_subject(**overrides), resolver)
    _assert_plain_refusal(raised.value)
    assert not resolver.queries
    assert EQUALITY_CALLS == []


@pytest.mark.parametrize(
    "subject",
    [
        pytest.param({"operation": OPERATION}, id="mapping"),
        pytest.param(None, id="none"),
        pytest.param("subject-subclass", id="subject-subclass"),
    ],
)
def test_a_subject_that_is_not_an_exact_subject_refuses(subject: Any) -> None:
    if subject == "subject-subclass":
        subject = _SubjectSubclass(**{f.name: getattr(_subject(), f.name) for f in fields(_subject())})
    resolver = _Resolver()
    with pytest.raises(AnalysisUseAuthorityRefused) as raised:
        _resolve_subject(subject, resolver)
    _assert_plain_refusal(raised.value)
    assert not resolver.queries


# ---------------------------------------------------------------------------
# 14. The resolver cannot rebind the query it was handed, even by mutating it.
#
# Frozen and slotted dataclasses stop ordinary assignment only. `object.__setattr__`
# still writes through, so each matrix below rewrites one field of the very query
# object the resolver was given and echoes that same object back. Identity alone
# would accept it; the deep binding taken before the resolver ran must refuse it.
# ---------------------------------------------------------------------------


def _rich_subject() -> AnalysisUseAuthoritySubject:
    """Two roles, scopes and capabilities, so a non-first item can be rewritten.

    Built fresh on every call: the mutation tests write into these very objects.
    """
    return _subject(
        scopes=(SCOPE, OTHER_SCOPE),
        authority=GrantedAuthority(
            principal_id=PRINCIPAL,
            roles=("reader", "writer"),
            capabilities=(
                CapabilityRef(id="memory.read", version="1.4"),
                CapabilityRef(id="memory.write", version="1.5"),
            ),
        ),
    )


OTHER_SUBJECT = AnalysisUseAuthoritySubject(
    operation=OTHER_OPERATION,
    workspace_id=WORKSPACE,
    authority=GrantedAuthority(principal_id=PRINCIPAL, roles=("reader",), capabilities=()),
    scopes=(SCOPE,),
    purpose=PURPOSE,
)

#: One individually canonical replacement per field, in declaration order.
QUERY_MUTATIONS: dict[str, Any] = {
    "subject": OTHER_SUBJECT,
    "dataset_id": "dataset-2",
    "dataset_revision": "rev-2",
    "dataset_incarnation": "inc-2",
    "state_generation": 4,
    "manifest_id": "manifest-2",
    "manifest_revision": "manifest-rev-2",
    "manifest_digest": "sha256:" + "a" * 64,
    "scope_digest": "sha256:" + "b" * 64,
    "observed_authority_epoch": "epoch-observed-9",
    "initial_readiness": "blocked",
    "completeness": "partial",
    "continuity": "gap_detected",
    "schema_compatibility": "requires_review",
    "evidence_availability": "limited",
    "freshness_deadline_at_us": 1_790_000_000_500_000,
    "verified_at_us": 1_790_000_000_000_500,
    "coverage_digest": "sha256:" + "c" * 64,
    "source_observation_digest": "sha256:" + "d" * 64,
    "recorded_at_us": 1_790_000_000_000_002,
    "subject_digest": "subject-2",
    "use_class": USE_EXPLORATION,
    "evaluation_instant": INSTANT + timedelta(hours=1),
}
SUBJECT_MUTATIONS: dict[str, Any] = {
    "operation": OTHER_OPERATION,
    "workspace_id": OTHER_WORKSPACE,
    "authority": GrantedAuthority(principal_id=OTHER_PRINCIPAL, roles=("reader",), capabilities=()),
    "scopes": (SCOPE, "memory:delete"),
    "purpose": OTHER_PURPOSE,
}
AUTHORITY_MUTATIONS: dict[str, Any] = {
    "principal_id": OTHER_PRINCIPAL,
    "roles": ("reader", "auditor"),
    "capabilities": (
        CapabilityRef(id="memory.read", version="1.4"),
        CapabilityRef(id="memory.write", version="1.6"),
    ),
}
#: Applied to the second capability, never the first.
CAPABILITY_MUTATIONS: dict[str, Any] = {
    "id": "memory.delete",
    "version": "1.6",
}
#: Each one valid exact-UTC instant differs from `INSTANT` in exactly one component.
INSTANT_COMPONENT_MUTATIONS: dict[str, datetime] = {
    "year": INSTANT.replace(year=2027),
    "month": INSTANT.replace(month=11),
    "day": INSTANT.replace(day=5),
    "hour": INSTANT.replace(hour=2),
    "minute": INSTANT.replace(minute=1),
    "second": INSTANT.replace(second=1),
    "microsecond": INSTANT.replace(microsecond=1),
    "fold": INSTANT.replace(fold=1),
}
_READ, _WRITE = CapabilityRef(id="memory.read", version="1.4"), CapabilityRef(id="memory.write", version="1.5")
#: Structural rewrites of the `_rich_subject` capability pair, items otherwise valid.
CAPABILITIES_REORDERED = (_WRITE, _READ)
CAPABILITIES_TRUNCATED = (_READ,)
CAPABILITIES_EXTENDED = (_READ, _WRITE, CapabilityRef(id="memory.delete", version="1.5"))


def test_each_instant_mutation_changes_exactly_one_component() -> None:
    components = ("year", "month", "day", "hour", "minute", "second", "microsecond", "fold")
    assert tuple(INSTANT_COMPONENT_MUTATIONS) == components
    for name, instant in INSTANT_COMPONENT_MUTATIONS.items():
        assert instant.tzinfo is UTC
        assert [c for c in components if getattr(instant, c) != getattr(INSTANT, c)] == [name]


def _at_query(query: AnalysisUseAuthorityQuery) -> object:
    return query


def _at_subject(query: AnalysisUseAuthorityQuery) -> object:
    return query.subject


def _at_authority(query: AnalysisUseAuthorityQuery) -> object:
    return query.subject.authority


def _at_first_capability(query: AnalysisUseAuthorityQuery) -> object:
    return query.subject.authority.capabilities[0]


def _at_second_capability(query: AnalysisUseAuthorityQuery) -> object:
    return query.subject.authority.capabilities[1]


@pytest.mark.parametrize(
    "cls, matrix",
    [
        pytest.param(AnalysisUseAuthorityQuery, QUERY_MUTATIONS, id="query"),
        pytest.param(AnalysisUseAuthoritySubject, SUBJECT_MUTATIONS, id="subject"),
        pytest.param(GrantedAuthority, AUTHORITY_MUTATIONS, id="authority"),
        pytest.param(CapabilityRef, CAPABILITY_MUTATIONS, id="capability"),
    ],
)
def test_each_mutation_matrix_covers_every_field_in_order(cls: type[Any], matrix: dict[str, Any]) -> None:
    assert tuple(matrix) == tuple(f.name for f in fields(cls))


MUTATIONS = [
    *(pytest.param(_at_query, {name: value}, id=f"query-{name}") for name, value in QUERY_MUTATIONS.items()),
    pytest.param(_at_query, dict.fromkeys(("manifest_id", "manifest_revision", "manifest_digest")), id="query-manifest-all-none"),
    pytest.param(_at_query, {"manifest_revision": None}, id="query-manifest-mixed"),
    pytest.param(_at_query, {"manifest_id": "bad id"}, id="query-manifest-malformed"),
    *(
        pytest.param(_at_query, {"evaluation_instant": instant}, id=f"query-instant-{component}")
        for component, instant in INSTANT_COMPONENT_MUTATIONS.items()
    ),
    *(pytest.param(_at_subject, {name: value}, id=f"subject-{name}") for name, value in SUBJECT_MUTATIONS.items()),
    pytest.param(_at_subject, {"scopes": (OTHER_SCOPE, SCOPE)}, id="subject-scopes-reordered"),
    pytest.param(_at_subject, {"scopes": (SCOPE,)}, id="subject-scopes-truncated"),
    *(pytest.param(_at_authority, {name: value}, id=f"authority-{name}") for name, value in AUTHORITY_MUTATIONS.items()),
    pytest.param(_at_authority, {"roles": ("writer", "reader")}, id="authority-roles-reordered"),
    pytest.param(_at_authority, {"roles": ("reader", "writer", "auditor")}, id="authority-roles-extended"),
    pytest.param(_at_authority, {"capabilities": CAPABILITIES_REORDERED}, id="authority-capabilities-reordered"),
    pytest.param(_at_authority, {"capabilities": CAPABILITIES_TRUNCATED}, id="authority-capabilities-truncated"),
    pytest.param(_at_authority, {"capabilities": CAPABILITIES_EXTENDED}, id="authority-capabilities-extended"),
    *(pytest.param(_at_second_capability, {name: value}, id=f"capability-{name}") for name, value in CAPABILITY_MUTATIONS.items()),
]


def _mutating_resolver(locate: Callable[[AnalysisUseAuthorityQuery], object], changes: dict[str, Any]) -> tuple[_Resolver, list[object]]:
    """A resolver that rewrites `changes` into the object `locate` finds, then echoes the query."""
    targets: list[object] = []

    def answer(query: AnalysisUseAuthorityQuery) -> AnalysisUseAuthoritySnapshot:
        target = locate(query)
        for name, value in changes.items():
            object.__setattr__(target, name, value)
        targets.append(target)
        return _snapshot(query)

    return _Resolver(answer), targets


@pytest.mark.parametrize("locate, changes", MUTATIONS)
def test_mutating_the_echoed_query_during_resolve_refuses(
    locate: Callable[[AnalysisUseAuthorityQuery], object], changes: dict[str, Any]
) -> None:
    resolver, targets = _mutating_resolver(locate, changes)
    with pytest.raises(AnalysisUseAuthorityRefused) as raised:
        _resolve_subject(_rich_subject(), resolver)
    _assert_plain_refusal(raised.value)
    assert len(resolver.queries) == 1
    # The write really landed on the object the resolver was handed.
    assert len(targets) == 1
    for name, value in changes.items():
        assert getattr(targets[0], name) is value


class _Hostile(str):
    """A `str` whose equality and hash record a call and raise a secret."""

    def __eq__(self, other: object) -> bool:
        EQUALITY_CALLS.append("hostile-eq")
        raise RuntimeError(SENTINEL)

    def __ne__(self, other: object) -> bool:
        EQUALITY_CALLS.append("hostile-ne")
        raise RuntimeError(SENTINEL)

    def __hash__(self) -> int:
        EQUALITY_CALLS.append("hostile-hash")
        raise RuntimeError(SENTINEL)


def _hostile_leaf(
    locate: Callable[[AnalysisUseAuthorityQuery], object], name: str, current: Any, index: int | None = None
) -> Any:
    """One matrix row: a fresh `_Hostile` copy of `current`, or of its item at `index`."""

    def changes() -> dict[str, Any]:
        if index is None:
            return {name: _Hostile(current)}
        items = list(current)
        items[index] = _Hostile(items[index])
        return {name: tuple(items)}

    place = "" if index is None else f"-{('first', 'second')[index]}"
    return pytest.param(locate, changes, id=f"{locate.__name__[4:]}-{name}{place}")


#: Every exact-string leaf the binding reads, each rewritten to an equal-valued `_Hostile`.
#: `_rich_subject` and `_record` supply the current values.
HOSTILE_LEAVES = [
    _hostile_leaf(_at_query, "dataset_id", DATASET_ID),
    _hostile_leaf(_at_query, "dataset_revision", "rev-1"),
    _hostile_leaf(_at_query, "dataset_incarnation", "inc-1"),
    _hostile_leaf(_at_query, "manifest_id", MANIFEST_ID),
    _hostile_leaf(_at_query, "manifest_revision", MANIFEST_REVISION),
    _hostile_leaf(_at_query, "manifest_digest", MANIFEST_DIGEST),
    _hostile_leaf(_at_query, "scope_digest", SCOPE_DIGEST),
    _hostile_leaf(_at_query, "observed_authority_epoch", EPOCH_OBSERVED),
    _hostile_leaf(_at_query, "subject_digest", SUBJECT_DIGEST),
    _hostile_leaf(_at_query, "use_class", USE_CURRENT_PUBLICATION),
    _hostile_leaf(_at_query, "initial_readiness", "ready"),
    _hostile_leaf(_at_query, "completeness", "complete"),
    _hostile_leaf(_at_query, "continuity", "verified"),
    _hostile_leaf(_at_query, "schema_compatibility", "compatible"),
    _hostile_leaf(_at_query, "evidence_availability", "available"),
    _hostile_leaf(_at_query, "coverage_digest", "sha256:" + "8" * 64),
    _hostile_leaf(_at_query, "source_observation_digest", "sha256:" + "9" * 64),
    _hostile_leaf(_at_subject, "operation", OPERATION),
    _hostile_leaf(_at_subject, "workspace_id", WORKSPACE),
    _hostile_leaf(_at_subject, "purpose", PURPOSE),
    _hostile_leaf(_at_subject, "scopes", (SCOPE, OTHER_SCOPE), 0),
    _hostile_leaf(_at_subject, "scopes", (SCOPE, OTHER_SCOPE), 1),
    _hostile_leaf(_at_authority, "principal_id", PRINCIPAL),
    _hostile_leaf(_at_authority, "roles", ("reader", "writer"), 0),
    _hostile_leaf(_at_authority, "roles", ("reader", "writer"), 1),
    _hostile_leaf(_at_first_capability, "id", "memory.read"),
    _hostile_leaf(_at_first_capability, "version", "1.4"),
    _hostile_leaf(_at_second_capability, "id", "memory.write"),
    _hostile_leaf(_at_second_capability, "version", "1.5"),
]


@pytest.mark.parametrize("locate, changes", HOSTILE_LEAVES)
def test_a_hostile_str_written_in_during_resolve_refuses_without_any_hook(
    locate: Callable[[AnalysisUseAuthorityQuery], object], changes: Callable[[], dict[str, Any]]
) -> None:
    written = changes()
    resolver, targets = _mutating_resolver(locate, written)
    with pytest.raises(AnalysisUseAuthorityRefused) as raised:
        _resolve_subject(_rich_subject(), resolver)
    _assert_plain_refusal(raised.value)
    # Refused after the resolver ran, not before it: it ran once, and the write landed
    # on the exact target, so a pre-resolver refusal cannot satisfy this.
    assert len(resolver.queries) == 1
    assert len(targets) == 1
    for name, value in written.items():
        assert getattr(targets[0], name) is value
    assert EQUALITY_CALLS == []


def _flat(value: Any) -> Any:
    """Field values only, with no type or `__eq__` of any dataclass, tuple or datetime in play."""
    if dataclasses.is_dataclass(value):
        return tuple(_flat(getattr(value, f.name)) for f in fields(value))
    if isinstance(value, tuple):
        return tuple(_flat(item) for item in value)
    return value


def _first_capability_wrong_type(capabilities: tuple[CapabilityRef, ...]) -> tuple[CapabilityRef, ...]:
    return (_copy_as(_CapabilitySubclass, capabilities[0]), capabilities[1])


def _second_capability_wrong_type(capabilities: tuple[CapabilityRef, ...]) -> tuple[CapabilityRef, ...]:
    return (capabilities[0], _copy_as(_CapabilitySubclass, capabilities[1]))


def _instant_wrong_type(i: datetime) -> datetime:
    return _Instant(
        i.year, i.month, i.day, i.hour, i.minute, i.second, i.microsecond, tzinfo=i.tzinfo, fold=i.fold
    )


#: (id, locate, field, equal-valued wrong-exact-type copy of the field, state generation).
SUBSTITUTIONS = [
    pytest.param(_at_query, "state_generation", _IntSubclass, 3, id="state-generation-int"),
    pytest.param(_at_query, "state_generation", bool, 1, id="state-generation-bool"),
    pytest.param(_at_query, "subject", lambda s: _copy_as(_SubjectSubclass, s), 3, id="subject"),
    pytest.param(_at_subject, "authority", lambda a: _copy_as(_AuthoritySubclass, a), 3, id="subject-authority"),
    pytest.param(_at_authority, "capabilities", _first_capability_wrong_type, 3, id="first-capability"),
    pytest.param(_at_authority, "capabilities", _second_capability_wrong_type, 3, id="second-capability"),
    pytest.param(_at_authority, "roles", _ScopesSubclass, 3, id="roles"),
    pytest.param(_at_subject, "scopes", _ScopesSubclass, 3, id="scopes"),
    pytest.param(_at_authority, "capabilities", _ScopesSubclass, 3, id="capabilities"),
    pytest.param(_at_query, "evaluation_instant", _instant_wrong_type, 3, id="evaluation-instant"),
    pytest.param(_at_query, "verified_at_us", _IntSubclass, 3, id="verified-at-us-int"),
    pytest.param(_at_query, "recorded_at_us", _IntSubclass, 3, id="recorded-at-us-int"),
]


@pytest.mark.parametrize("locate, name, substitute, generation", SUBSTITUTIONS)
def test_an_equal_valued_wrong_exact_type_written_in_during_resolve_refuses(
    locate: Callable[[AnalysisUseAuthorityQuery], object],
    name: str,
    substitute: Callable[[Any], Any],
    generation: int,
) -> None:
    swaps: list[tuple[Any, Any]] = []

    def answer(query: AnalysisUseAuthorityQuery) -> AnalysisUseAuthoritySnapshot:
        target = locate(query)
        before = getattr(target, name)
        after = substitute(before)
        object.__setattr__(target, name, after)
        swaps.append((before, after))
        return _snapshot(query)

    resolver = _Resolver(answer)
    with pytest.raises(AnalysisUseAuthorityRefused) as raised:
        resolve_analysis_use_authority_for_subject(
            _rich_subject(),
            dataset=_record(state_generation=generation),
            subject_digest=SUBJECT_DIGEST,
            use_class=USE_CURRENT_PUBLICATION,
            evaluation_instant=INSTANT,
            resolver=resolver,
        )
    _assert_plain_refusal(raised.value)
    # The query passed the first bind and reached the resolver, so this is the second.
    assert len(resolver.queries) == 1
    ((before, after),) = swaps
    assert after is not before
    # Nothing but an exact type differs: every field value is equal, so value inequality
    # cannot be what refused it.
    assert _flat(after) == _flat(before)
    assert type(after) is not type(before) or any(
        type(a) is not type(b) for a, b in zip(after, before, strict=True)
    )


class _RecordingZone(tzinfo):
    """A zone that records and raises from every method a comparison could reach."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def utcoffset(self, dt: datetime | None) -> timedelta:
        self.calls.append("utcoffset")
        raise RuntimeError(SENTINEL)

    def dst(self, dt: datetime | None) -> timedelta:
        self.calls.append("dst")
        raise RuntimeError(SENTINEL)

    def tzname(self, dt: datetime | None) -> str:
        self.calls.append("tzname")
        raise RuntimeError(SENTINEL)

    def fromutc(self, dt: datetime) -> datetime:
        self.calls.append("fromutc")
        raise RuntimeError(SENTINEL)

    def __eq__(self, other: object) -> bool:
        self.calls.append("eq")
        raise RuntimeError(SENTINEL)

    def __ne__(self, other: object) -> bool:
        self.calls.append("ne")
        raise RuntimeError(SENTINEL)

    def __hash__(self) -> int:
        self.calls.append("hash")
        raise RuntimeError(SENTINEL)


def test_an_instant_with_a_hostile_zone_written_in_during_resolve_refuses_without_any_hook() -> None:
    zone = _RecordingZone()
    # The same wall-clock fields as the bound instant: only the zone differs.
    instant = datetime(INSTANT.year, INSTANT.month, INSTANT.day, INSTANT.hour, tzinfo=zone)
    resolver, _ = _mutating_resolver(_at_query, {"evaluation_instant": instant})
    with pytest.raises(AnalysisUseAuthorityRefused) as raised:
        _resolve_subject(_rich_subject(), resolver)
    _assert_plain_refusal(raised.value)
    # No offset, name, conversion, equality or hash hook: the zone is checked by identity.
    assert zone.calls == []


class _RewritingZone(_FixedZero):
    """A valid zero-offset zone that runs `rewrite` from inside normalization."""

    def __init__(self, rewrite: Callable[[], None]) -> None:
        self.rewrite = rewrite

    def utcoffset(self, dt: datetime | None) -> timedelta:
        self.rewrite()
        return timedelta(0)


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        pytest.param("roles", ("auditor", "writer"), id="roles"),
        pytest.param("capabilities", (CapabilityRef(id="memory.read", version="9.9"),), id="capabilities"),
    ],
)
def test_an_authority_rewritten_by_the_timezone_hook_refuses_before_the_resolver(
    field_name: str, value: Any
) -> None:
    subject = _rich_subject()
    zone = _RewritingZone(lambda: object.__setattr__(subject.authority, field_name, value))
    instant = datetime(2026, 10, 4, 1, 0, tzinfo=zone)
    resolver = _Resolver()
    with pytest.raises(AnalysisUseAuthorityRefused) as raised:
        resolve_analysis_use_authority_for_subject(
            subject,
            dataset=_record(),
            subject_digest=SUBJECT_DIGEST,
            use_class=USE_CURRENT_PUBLICATION,
            evaluation_instant=instant,
            resolver=resolver,
        )
    _assert_plain_refusal(raised.value)
    assert getattr(subject.authority, field_name) == value  # the hook did run
    assert not resolver.queries


#: The observation values the query carries under the same name.
OBSERVATION_QUERY_FIELDS = (
    "dataset_id",
    "dataset_revision",
    "dataset_incarnation",
    "manifest_id",
    "manifest_revision",
    "manifest_digest",
    "scope_digest",
    "observed_authority_epoch",
    "initial_readiness",
    "completeness",
    "continuity",
    "schema_compatibility",
    "evidence_availability",
    "freshness_deadline_at_us",
    "verified_at_us",
)
#: The record values the query carries under the same name.
RECORD_QUERY_FIELDS = ("coverage_digest", "source_observation_digest", "recorded_at_us")
OTHER_GENERATION = 4


def _dataset_values(record: DatasetStateRecord) -> dict[str, Any]:
    """Every dataset-derived query value, read straight off the record."""
    values: dict[str, Any] = {"state_generation": record.state_generation}
    values.update({name: getattr(record, name) for name in RECORD_QUERY_FIELDS})
    values.update({name: getattr(record.observation, name) for name in OBSERVATION_QUERY_FIELDS})
    return values


def _rewrite_generation(record: DatasetStateRecord) -> None:
    object.__setattr__(record, "state_generation", OTHER_GENERATION)


def _rewrite_record_field(name: str) -> Callable[[DatasetStateRecord], None]:
    def rewrite(record: DatasetStateRecord) -> None:
        object.__setattr__(record, name, QUERY_MUTATIONS[name])

    return rewrite


def _rewrite_observation_field(name: str) -> Callable[[DatasetStateRecord], None]:
    def rewrite(record: DatasetStateRecord) -> None:
        object.__setattr__(record.observation, name, QUERY_MUTATIONS[name])

    return rewrite


def _rewrite_observation(record: DatasetStateRecord) -> None:
    values = {name: QUERY_MUTATIONS[name] for name in OBSERVATION_QUERY_FIELDS}
    coverage = {"scope_digest": values["scope_digest"], "proof_kind": "complete_enumeration"}
    object.__setattr__(record, "observation", _observation(coverage=coverage, **values))


#: (rewrite, names whose record value changes, whether the observation object is replaced).
DATASET_REWRITES = [
    pytest.param(_rewrite_generation, {"state_generation"}, False, id="state-generation"),
    *(
        pytest.param(_rewrite_observation_field(name), {name}, False, id=f"observation-{name}")
        for name in OBSERVATION_QUERY_FIELDS
    ),
    *(pytest.param(_rewrite_record_field(name), {name}, False, id=f"record-{name}") for name in RECORD_QUERY_FIELDS),
    pytest.param(_rewrite_observation, set(OBSERVATION_QUERY_FIELDS), True, id="observation-replaced"),
]


@pytest.mark.parametrize(("rewrite", "changed", "replaced"), DATASET_REWRITES)
def test_a_dataset_rewritten_by_the_timezone_hook_does_not_reach_the_query(
    rewrite: Callable[[DatasetStateRecord], None], changed: set[str], replaced: bool
) -> None:
    """Dataset values are held before `_utc`, so a still-canonical rewrite inside it is not read."""
    record = _record()
    observation = record.observation
    before = _dataset_values(record)
    zone = _RewritingZone(lambda: rewrite(record))
    issued: list[AnalysisUseAuthoritySnapshot] = []

    def answer(query: AnalysisUseAuthorityQuery) -> AnalysisUseAuthoritySnapshot:
        issued.append(_snapshot(query))
        return issued[0]

    resolver = _Resolver(answer)
    result = resolve_analysis_use_authority_for_subject(
        _rich_subject(),
        dataset=record,
        subject_digest=SUBJECT_DIGEST,
        use_class=USE_CURRENT_PUBLICATION,
        evaluation_instant=datetime(2026, 10, 4, 1, 0, tzinfo=zone),
        resolver=resolver,
    )
    # The hook ran and moved exactly the intended values, on the exact intended object.
    after = _dataset_values(record)
    assert {name for name in before if after[name] != before[name]} == changed
    assert (record.observation is not observation) is replaced
    assert len(resolver.queries) == 1
    query = resolver.queries[0]
    assert {name: getattr(query, name) for name in before} == before
    assert result is issued[0]
    assert result.query is query


def test_an_unchanged_query_returns_the_exact_resolver_issued_snapshot() -> None:
    issued: list[AnalysisUseAuthoritySnapshot] = []

    def answer(query: AnalysisUseAuthorityQuery) -> AnalysisUseAuthoritySnapshot:
        issued.append(_snapshot(query))
        return issued[0]

    resolver = _Resolver(answer)
    result = _resolve_subject(_rich_subject(), resolver)
    assert len(resolver.queries) == 1
    assert result is issued[0]
    assert result.query is resolver.queries[0]


def test_a_query_that_fails_to_bind_refuses_before_the_resolver(monkeypatch: pytest.MonkeyPatch) -> None:
    """The binding is the last check before the resolver, not only the one after it."""
    build = authority_module._build_query

    def build_then_corrupt(*args: Any, **kwargs: Any) -> AnalysisUseAuthorityQuery:
        query = build(*args, **kwargs)
        object.__setattr__(query, "dataset_id", _Hostile(DATASET_ID))
        return query

    monkeypatch.setattr(authority_module, "_build_query", build_then_corrupt)
    resolver = _Resolver()
    with pytest.raises(AnalysisUseAuthorityRefused) as raised:
        _resolve_subject(_rich_subject(), resolver)
    _assert_plain_refusal(raised.value)
    assert not resolver.queries
    assert EQUALITY_CALLS == []


def test_the_seam_values_are_frozen_slotted_dataclasses() -> None:
    """Shape only: `object.__setattr__` still writes through, as the matrices show."""
    snapshot = _resolve(_valid_context(), _Resolver())
    for value in (snapshot.query.subject, snapshot.query, snapshot):
        cls: type[Any] = type(value)
        names = tuple(f.name for f in fields(cls))
        assert dataclasses.is_dataclass(value)
        assert cls.__dataclass_params__.frozen
        assert cls.__slots__ == names
        assert not hasattr(value, "__dict__")
        for name in names:
            with pytest.raises(dataclasses.FrozenInstanceError):
                setattr(value, name, getattr(value, name))


# ---------------------------------------------------------------------------
# 15. A dataset or observation of the wrong exact type refuses before the resolver.
# ---------------------------------------------------------------------------


class _RecordSubclass(DatasetStateRecord):
    """A `DatasetStateRecord` subclass with valid values, to show the exact-type gate."""


class _ObservationSubclass(DatasetStateObservation):
    """A `DatasetStateObservation` subclass with valid values, to show the exact-type gate."""


class _NoAttributes:
    """Records and raises on any attribute read."""

    def __getattribute__(self, name: str) -> Any:
        EQUALITY_CALLS.append("getattr")
        raise RuntimeError(SENTINEL)


@pytest.mark.parametrize(
    "dataset",
    [
        pytest.param(lambda: _copy_as(_RecordSubclass, _record()), id="record-subclass"),
        pytest.param(lambda: {"workspace_id": WORKSPACE, "observation": _observation()}, id="record-mapping"),
        pytest.param(object, id="record-object"),
        pytest.param(lambda: None, id="record-none"),
        pytest.param(_NoAttributes, id="record-no-attribute-access"),
        pytest.param(lambda: _record(observation=_copy_as(_ObservationSubclass, _observation())), id="observation-subclass"),
        pytest.param(lambda: _record(observation={"dataset_id": DATASET_ID}), id="observation-mapping"),
        pytest.param(lambda: _record(observation=object()), id="observation-object"),
        pytest.param(lambda: _record(observation=None), id="observation-none"),
        pytest.param(lambda: _record(observation=_NoAttributes()), id="observation-no-attribute-access"),
    ],
)
def test_a_dataset_or_observation_of_the_wrong_exact_type_refuses(dataset: Callable[[], Any]) -> None:
    resolver = _Resolver()
    with pytest.raises(AnalysisUseAuthorityRefused) as raised:
        resolve_analysis_use_authority_for_subject(
            _rich_subject(),
            dataset=dataset(),
            subject_digest=SUBJECT_DIGEST,
            use_class=USE_CURRENT_PUBLICATION,
            evaluation_instant=INSTANT,
            resolver=resolver,
        )
    _assert_plain_refusal(raised.value)
    assert not resolver.queries
    assert EQUALITY_CALLS == []
