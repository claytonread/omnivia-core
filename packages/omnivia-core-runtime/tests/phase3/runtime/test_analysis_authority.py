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
records that it ran. A guard fixture fails any test that let that equality run. The
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
    ServiceBinding,
    authorize_application_request,
)
from omnivia_core_runtime.service.operations import OperationContext
from omnivia_core_runtime.storage.dataset_state import (
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

#: Every equality the hostile `str` subclass was asked to run, across one test.
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
    ],
)
def test_legacy_none_and_wrong_shapes_refuse(change: Callable[[OperationContext], OperationContext]) -> None:
    resolver = _Resolver()
    context = change(_valid_context())
    with pytest.raises(AnalysisUseAuthorityRefused) as raised:
        _resolve(context, resolver)
    _assert_plain_refusal(raised.value)
    assert not resolver.queries


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
    assert {f.name for f in fields(query)} == {
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
        "subject_digest",
        "use_class",
        "evaluation_instant",
    }
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


def test_the_query_is_immutable_after_it_is_built() -> None:
    resolver = _Resolver()
    _resolve(_valid_context(), resolver)
    query = resolver.queries[0]
    with pytest.raises(dataclasses.FrozenInstanceError):
        query.observed_authority_epoch = EPOCH_CURRENT  # type: ignore[misc]


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
