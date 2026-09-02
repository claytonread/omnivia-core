"""T-0688 IP-04 acceptance for the call-time dispatch authority.

Every test is a rule. The seam is pure -- `now`, the permission resolver and the audit
sink are arguments -- so each rule is isolated by changing exactly one field and
asserting the refusal that field is supposed to cause.

*Possession is not permission.* The same unchanged `AuthorizedInvocation` is dispatched
twice against a resolver whose answer changes in between, and the second call refuses:
there is no cache, so a revocation lands on the very next call.

*Local mode is not a weaker mode.* Authenticated local IPC and the authenticated Cloud
service transport are asserted to produce equivalent permits and equivalent audit
records, differing only in the transport kind each is bound to.

*Nothing leaks.* Credential-shaped and malformed caller values are asserted absent from
both the refusal's representation and every audit record, and the permit is asserted to
carry only the bounded non-secret facts its contract names.
"""

from __future__ import annotations

from dataclasses import fields, replace
from datetime import UTC, datetime, timedelta

import pytest
from omnivia_core_runtime.service.capability_gateway import AuthorizedInvocation
from omnivia_core_runtime.service.gateway_dispatch_authority import (
    ALLOW_REASON,
    MAX_PERMISSION_VALIDITY,
    REFUSE_AMBIENT_DATABASE_AUTHORITY,
    REFUSE_AMBIENT_SECRET_AUTHORITY,
    REFUSE_AUDIT_UNAVAILABLE,
    REFUSE_DESTINATION_NOT_CANONICAL,
    REFUSE_INSTANT_NOT_USABLE,
    REFUSE_MALFORMED_CONTEXT,
    REFUSE_MALFORMED_INVOCATION,
    REFUSE_PERMISSION_MALFORMED,
    REFUSE_PERMISSION_MISMATCH,
    REFUSE_PERMISSION_NOT_ALLOWED,
    REFUSE_PERMISSION_OUT_OF_WINDOW,
    REFUSE_PERMISSION_REVOKED,
    REFUSE_PERMISSION_UNAVAILABLE,
    REFUSE_PERMISSION_WINDOW_NOT_SHORT,
    REFUSE_TRANSPORT_NOT_AUTHENTICATED,
    TRANSPORT_CLOUD_SERVICE,
    TRANSPORT_LOCAL_IPC,
    CurrentPermission,
    DispatchAuditRecord,
    DispatchContext,
    DispatchPermit,
    GatewayDispatchRefusal,
    authorize_dispatch,
    canonical_https_origin,
)

NOW = datetime(2026, 8, 31, 12, 0, 0, tzinfo=UTC)
ORIGIN = "https://api.example.com"
CAPABILITY = "mail.send"

#: A value that is canonical for `Identifier` and is also somebody's live token. It is
#: fed in wherever a caller value could reach a record, and asserted never to arrive.
SECRET = "ghp_" + "A" * 36


def _stamp(offset_minutes: float) -> str:
    return (NOW + timedelta(minutes=offset_minutes)).isoformat()


def _invocation(**overrides: object) -> AuthorizedInvocation:
    invocation = AuthorizedInvocation(
        workspace_id="ws-t0688",
        run_id="run-1",
        capability_id=CAPABILITY,
        binding_id="binding-1",
        binding_source_id="source-1",
        binding_version="1.2.0",
        effect_class="external_write",
        scopes=("mail:send",),
        purpose="operations.support",
        capability_grant_id="grant-1",
        policy_snapshot_id="policy-1",
        idempotency_key="idem-1",
        request_digest="sha256:" + "a" * 64,
        evidence_item_ids=("evidence-1",),
        authorized_at=NOW - timedelta(minutes=1),
    )
    return replace(invocation, **overrides)  # type: ignore[arg-type]


def _context(**overrides: object) -> DispatchContext:
    context = DispatchContext(
        process_id="process-1",
        worker_id="worker-1",
        tenant_id="tenant-1",
        project_id="project-1",
        run_id="run-1",
        attempt_id="attempt-1",
        transport_kind=TRANSPORT_LOCAL_IPC,
        destination=ORIGIN,
        ambient_database_authority=False,
        ambient_secret_authority=False,
    )
    return replace(context, **overrides)  # type: ignore[arg-type]


def _permission(**overrides: object) -> CurrentPermission:
    permission = CurrentPermission(
        decision_id="decision-1",
        permission_revision="revision-1",
        process_id="process-1",
        worker_id="worker-1",
        tenant_id="tenant-1",
        project_id="project-1",
        run_id="run-1",
        attempt_id="attempt-1",
        transport_kind=TRANSPORT_LOCAL_IPC,
        destination_origin=ORIGIN,
        capability_id=CAPABILITY,
        capability_grant_id="grant-1",
        policy_snapshot_id="policy-1",
        binding_id="binding-1",
        not_before=_stamp(-1),
        not_after=_stamp(1),
        allowed=True,
        revoked=False,
    )
    return replace(permission, **overrides)  # type: ignore[arg-type]


class _Resolver:
    """Answers with the permissions it was handed, one per call, and counts the calls."""

    def __init__(self, *answers: CurrentPermission | Exception) -> None:
        self._answers = list(answers)
        self.calls: list[tuple[DispatchContext, AuthorizedInvocation]] = []

    def resolve(
        self, context: DispatchContext, invocation: AuthorizedInvocation
    ) -> CurrentPermission:
        self.calls.append((context, invocation))
        answer = self._answers[min(len(self.calls) - 1, len(self._answers) - 1)]
        if isinstance(answer, Exception):
            raise answer
        return answer


class _Sink:
    """Collects records, or fails on every write when constructed to."""

    def __init__(self, *, fails: bool = False) -> None:
        self.records: list[DispatchAuditRecord] = []
        self._fails = fails

    def record(self, entry: DispatchAuditRecord) -> None:
        if self._fails:
            raise RuntimeError("audit sink is down: " + SECRET)
        self.records.append(entry)


#: "not supplied", so that `None` can itself be passed in as a malformed value.
_DEFAULT: object = object()


def _dispatch(
    *,
    invocation: object = _DEFAULT,
    context: object = _DEFAULT,
    permission: object = _DEFAULT,
    sink: _Sink | None = None,
    now: object = NOW,
) -> tuple[DispatchPermit, _Sink, _Resolver]:
    resolver = _Resolver(  # type: ignore[arg-type]
        _permission() if permission is _DEFAULT else permission
    )
    sink = sink if sink is not None else _Sink()
    permit = authorize_dispatch(
        _invocation() if invocation is _DEFAULT else invocation,  # type: ignore[arg-type]
        context=_context() if context is _DEFAULT else context,  # type: ignore[arg-type]
        permission_resolver=resolver,
        audit_sink=sink,
        now=now,  # type: ignore[arg-type]
    )
    return permit, sink, resolver


def _refusal(**kwargs: object) -> tuple[GatewayDispatchRefusal, _Sink, _Resolver]:
    sink = kwargs.get("sink") or _Sink()
    kwargs["sink"] = sink
    with pytest.raises(GatewayDispatchRefusal) as raised:
        _dispatch(**kwargs)  # type: ignore[arg-type]
    return raised.value, sink, _Resolver()


# --- the allow path, both authenticated transports ------------------------------


@pytest.mark.parametrize("transport", [TRANSPORT_LOCAL_IPC, TRANSPORT_CLOUD_SERVICE])
def test_authenticated_transports_permit_dispatch(transport: str) -> None:
    permit, sink, resolver = _dispatch(
        context=_context(transport_kind=transport),
        permission=_permission(transport_kind=transport),
    )

    assert permit.decision_id == "decision-1"
    assert permit.process_id == "process-1"
    assert permit.worker_id == "worker-1"
    assert permit.tenant_id == "tenant-1"
    assert permit.project_id == "project-1"
    assert permit.run_id == "run-1"
    assert permit.attempt_id == "attempt-1"
    assert permit.capability_id == CAPABILITY
    assert permit.binding_id == "binding-1"
    assert permit.destination_origin == ORIGIN
    assert permit.permission_revision == "revision-1"
    assert permit.authorized_at == NOW
    assert permit.expires_at == datetime.fromisoformat(_stamp(1))
    assert len(resolver.calls) == 1
    assert [record.allowed for record in sink.records] == [True]
    assert sink.records[0].reason == ALLOW_REASON
    assert sink.records[0].transport_kind == transport


def test_local_and_cloud_permits_are_equivalent() -> None:
    """The transport kind is a bound fact, never a discount: same permit either way."""
    local, local_sink, _ = _dispatch()
    cloud, cloud_sink, _ = _dispatch(
        context=_context(transport_kind=TRANSPORT_CLOUD_SERVICE),
        permission=_permission(transport_kind=TRANSPORT_CLOUD_SERVICE),
    )

    assert local == cloud
    assert replace(local_sink.records[0], transport_kind=None) == replace(
        cloud_sink.records[0], transport_kind=None
    )


# --- the decision must be about exactly this call -------------------------------

#: One field of the current decision per case, and the call it would then be about.
_MISMATCHES: tuple[tuple[str, str], ...] = (
    ("process_id", "process-2"),
    ("worker_id", "worker-2"),
    ("tenant_id", "tenant-2"),
    ("project_id", "project-2"),
    ("run_id", "run-2"),
    ("attempt_id", "attempt-2"),
    ("transport_kind", TRANSPORT_CLOUD_SERVICE),
    ("destination_origin", "https://other.example.com"),
    ("capability_id", "mail.read"),
    ("capability_grant_id", "grant-2"),
    ("policy_snapshot_id", "policy-2"),
    ("binding_id", "binding-2"),
)


@pytest.mark.parametrize(
    ("field", "value"), _MISMATCHES, ids=[f for f, _ in _MISMATCHES]
)
def test_any_mismatched_identity_refuses(field: str, value: str) -> None:
    refusal, sink, _ = _refusal(permission=_permission(**{field: value}))

    assert refusal.reason == REFUSE_PERMISSION_MISMATCH
    assert [record.allowed for record in sink.records] == [False]


def test_permission_naming_another_run_than_the_invocation_refuses() -> None:
    """The context and the decision can agree and still be about another authority."""
    refusal, _, _ = _refusal(
        context=_context(run_id="run-2"),
        permission=_permission(run_id="run-2"),
    )

    assert refusal.reason == REFUSE_PERMISSION_MISMATCH


# --- resolved once, every call --------------------------------------------------


def test_resolver_is_consulted_exactly_once_per_dispatch() -> None:
    _, _, resolver = _dispatch()

    assert len(resolver.calls) == 1
    assert resolver.calls[0][0] == _context()


def test_revocation_lands_on_the_next_call_for_the_same_invocation() -> None:
    """Possession is not permission: the invocation is unchanged, the answer is not."""
    invocation = _invocation()
    context = _context()
    resolver = _Resolver(_permission(), _permission(revoked=True))
    sink = _Sink()

    permit = authorize_dispatch(
        invocation,
        context=context,
        permission_resolver=resolver,
        audit_sink=sink,
        now=NOW,
    )
    assert permit.decision_id == "decision-1"

    with pytest.raises(GatewayDispatchRefusal) as raised:
        authorize_dispatch(
            invocation,
            context=context,
            permission_resolver=resolver,
            audit_sink=sink,
            now=NOW,
        )

    assert raised.value.reason == REFUSE_PERMISSION_REVOKED
    assert len(resolver.calls) == 2
    assert [record.allowed for record in sink.records] == [True, False]


# --- the decision's own state and window ----------------------------------------


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"allowed": False}, REFUSE_PERMISSION_NOT_ALLOWED),
        ({"revoked": True}, REFUSE_PERMISSION_REVOKED),
        ({"revoked": True, "allowed": False}, REFUSE_PERMISSION_REVOKED),
        ({"not_before": "not-an-instant"}, REFUSE_PERMISSION_MALFORMED),
        ({"not_after": "not-an-instant"}, REFUSE_PERMISSION_MALFORMED),
        ({"not_before": "2026-08-31T12:00:00"}, REFUSE_PERMISSION_MALFORMED),
        ({"not_after": _stamp(-1)}, REFUSE_PERMISSION_MALFORMED),
        (
            {"not_before": _stamp(1), "not_after": _stamp(2)},
            REFUSE_PERMISSION_OUT_OF_WINDOW,
        ),
        (
            {"not_before": _stamp(-2), "not_after": _stamp(-1)},
            REFUSE_PERMISSION_OUT_OF_WINDOW,
        ),
        (
            {"not_before": _stamp(0), "not_after": _stamp(5.001)},
            REFUSE_PERMISSION_WINDOW_NOT_SHORT,
        ),
        (
            {"not_before": _stamp(-30), "not_after": _stamp(30)},
            REFUSE_PERMISSION_WINDOW_NOT_SHORT,
        ),
    ],
    ids=[
        "denied",
        "revoked",
        "revoked-beats-denied",
        "not-before-malformed",
        "not-after-malformed",
        "naive-not-before",
        "inverted-window",
        "not-yet-valid",
        "expired",
        "just-over-five-minutes",
        "hour-long-window",
    ],
)
def test_permission_state_and_window_rules(
    overrides: dict[str, object], reason: str
) -> None:
    refusal, sink, _ = _refusal(permission=_permission(**overrides))

    assert refusal.reason == reason
    assert [record.allowed for record in sink.records] == [False]


def test_window_of_exactly_the_maximum_is_permitted() -> None:
    """Five minutes is the bound, not the first refusal."""
    permit, _, _ = _dispatch(
        permission=_permission(not_before=_stamp(0), not_after=_stamp(5))
    )

    assert permit.expires_at - datetime.fromisoformat(_stamp(0)) == (
        MAX_PERMISSION_VALIDITY
    )


def test_now_equal_to_not_after_is_outside_the_window() -> None:
    """The window is half-open: the instant it ends is the instant it stops permitting."""
    refusal, _, _ = _refusal(
        permission=_permission(not_before=_stamp(-1), not_after=_stamp(0))
    )

    assert refusal.reason == REFUSE_PERMISSION_OUT_OF_WINDOW


# --- local checks come first ----------------------------------------------------


@pytest.mark.parametrize(
    "transport",
    ["", "unauthenticated_local_ipc", "http", TRANSPORT_LOCAL_IPC.upper(), "unknown"],
)
def test_unauthenticated_transport_refuses_before_the_resolver(transport: str) -> None:
    resolver = _Resolver(_permission())
    sink = _Sink()

    with pytest.raises(GatewayDispatchRefusal) as raised:
        authorize_dispatch(
            _invocation(),
            context=_context(transport_kind=transport),
            permission_resolver=resolver,
            audit_sink=sink,
            now=NOW,
        )

    assert raised.value.reason == REFUSE_TRANSPORT_NOT_AUTHENTICATED
    assert resolver.calls == []
    assert sink.records[0].transport_kind is None


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"ambient_database_authority": True}, REFUSE_AMBIENT_DATABASE_AUTHORITY),
        ({"ambient_secret_authority": True}, REFUSE_AMBIENT_SECRET_AUTHORITY),
    ],
    ids=["database", "secret"],
)
def test_ambient_authority_refuses_before_the_resolver(
    overrides: dict[str, object], reason: str
) -> None:
    resolver = _Resolver(_permission())

    with pytest.raises(GatewayDispatchRefusal) as raised:
        authorize_dispatch(
            _invocation(),
            context=_context(**overrides),
            permission_resolver=resolver,
            audit_sink=_Sink(),
            now=NOW,
        )

    assert raised.value.reason == reason
    assert resolver.calls == []


def test_ambient_authority_defaults_to_refusing() -> None:
    """Silence is not a claim: an under-specified context dispatches nothing."""
    context = DispatchContext(
        process_id="process-1",
        worker_id="worker-1",
        tenant_id="tenant-1",
        project_id="project-1",
        run_id="run-1",
        attempt_id="attempt-1",
        transport_kind=TRANSPORT_LOCAL_IPC,
        destination=ORIGIN,
    )
    resolver = _Resolver(_permission())

    with pytest.raises(GatewayDispatchRefusal) as raised:
        authorize_dispatch(
            _invocation(),
            context=context,
            permission_resolver=resolver,
            audit_sink=_Sink(),
            now=NOW,
        )

    assert raised.value.reason == REFUSE_AMBIENT_DATABASE_AUTHORITY
    assert resolver.calls == []


# --- destination is a canonical HTTPS origin and nothing more -------------------


@pytest.mark.parametrize(
    "destination",
    [
        "https://api.example.com",
        "https://api.example.com:8443",
        "https://127.0.0.1:8443",
    ],
)
def test_canonical_origins_are_accepted(destination: str) -> None:
    assert canonical_https_origin(destination) == destination

    permit, _, _ = _dispatch(
        context=_context(destination=destination),
        permission=_permission(destination_origin=destination),
    )
    assert permit.destination_origin == destination


@pytest.mark.parametrize(
    "destination",
    [
        "http://api.example.com",
        "ftp://api.example.com",
        "//api.example.com",
        "api.example.com",
        "https://user:pass@api.example.com",
        "https://user@api.example.com",
        "https://api.example.com/",
        "https://api.example.com/v1/send",
        "https://api.example.com?a=b",
        "https://api.example.com#frag",
        "https://API.example.com",
        "https://api.EXAMPLE.com",
        "https://api.example.com:",
        "https://api.example.com:00443",
        "https://api.example.com:notaport",
        "https://",
        "https://:443",
        "",
        " https://api.example.com",
        "https://api.example.com ",
        "https://api.example.com\n",
        "https://[::1",
        "https://" + "a" * 300 + ".example.com",
    ],
)
def test_noncanonical_destinations_are_refused(destination: str) -> None:
    assert canonical_https_origin(destination) is None

    refusal, sink, _ = _refusal(context=_context(destination=destination))
    assert refusal.reason == REFUSE_DESTINATION_NOT_CANONICAL
    assert sink.records[0].destination_origin is None


@pytest.mark.parametrize(
    "destination", [None, 443, b"https://api.example.com", object()]
)
def test_non_string_destinations_are_refused(destination: object) -> None:
    assert canonical_https_origin(destination) is None

    refusal, _, _ = _refusal(context=_context(destination=destination))
    assert refusal.reason == REFUSE_MALFORMED_CONTEXT


def test_destination_matching_a_different_origin_refuses() -> None:
    refusal, _, _ = _refusal(
        context=_context(destination="https://other.example.com"),
        permission=_permission(destination_origin=ORIGIN),
    )

    assert refusal.reason == REFUSE_PERMISSION_MISMATCH


def test_permission_naming_a_noncanonical_origin_is_malformed() -> None:
    refusal, _, _ = _refusal(
        permission=_permission(destination_origin="https://api.example.com/")
    )

    assert refusal.reason == REFUSE_PERMISSION_MALFORMED


# --- fail closed on malformed inputs and unknown vocabularies -------------------


@pytest.mark.parametrize(
    "now",
    [
        None,
        "2026-08-31T12:00:00+00:00",
        datetime(2026, 8, 31, 12, 0, 0),  # noqa: DTZ001 - naive is the case under test
        NOW.date(),
        0,
    ],
    ids=["none", "string", "naive", "date", "epoch"],
)
def test_unusable_now_refuses(now: object) -> None:
    resolver = _Resolver(_permission())

    with pytest.raises(GatewayDispatchRefusal) as raised:
        authorize_dispatch(
            _invocation(),
            context=_context(),
            permission_resolver=resolver,
            audit_sink=_Sink(),
            now=now,  # type: ignore[arg-type]
        )

    assert raised.value.reason == REFUSE_INSTANT_NOT_USABLE
    assert resolver.calls == []


@pytest.mark.parametrize(
    "overrides",
    [
        {"process_id": ""},
        {"worker_id": "worker 1"},
        {"tenant_id": "tenant\n1"},
        {"project_id": None},
        {"run_id": 1},
        {"attempt_id": "attempt/1"},
        {"attempt_id": SECRET},
        {"transport_kind": None},
        {"ambient_database_authority": 0},
        {"ambient_secret_authority": "false"},
    ],
    ids=[
        "empty",
        "space",
        "newline",
        "none",
        "non-string",
        "slash",
        "credential-shaped",
        "non-string-transport",
        "int-for-database-flag",
        "string-for-secret-flag",
    ],
)
def test_malformed_context_refuses(overrides: dict[str, object]) -> None:
    resolver = _Resolver(_permission())

    with pytest.raises(GatewayDispatchRefusal) as raised:
        authorize_dispatch(
            _invocation(),
            context=_context(**overrides),
            permission_resolver=resolver,
            audit_sink=_Sink(),
            now=NOW,
        )

    assert raised.value.reason == REFUSE_MALFORMED_CONTEXT
    assert resolver.calls == []


@pytest.mark.parametrize("context", [None, "context", {"run_id": "run-1"}, object()])
def test_non_context_refuses_and_records_nothing_of_it(context: object) -> None:
    refusal, sink, _ = _refusal(context=context)

    assert refusal.reason == REFUSE_MALFORMED_CONTEXT
    record = sink.records[0]
    assert record.process_id is None
    assert record.run_id is None
    assert record.transport_kind is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"capability_id": "mail"},
        {"capability_id": ""},
        {"capability_id": None},
        {"run_id": "run 1"},
        {"binding_id": ""},
        {"capability_grant_id": None},
        {"policy_snapshot_id": "policy\n1"},
        {"binding_id": SECRET},
    ],
    ids=[
        "capability-without-namespace",
        "empty-capability",
        "none-capability",
        "space-in-run",
        "empty-binding",
        "none-grant",
        "newline-in-policy",
        "credential-shaped-binding",
    ],
)
def test_malformed_invocation_refuses(overrides: dict[str, object]) -> None:
    resolver = _Resolver(_permission())

    with pytest.raises(GatewayDispatchRefusal) as raised:
        authorize_dispatch(
            _invocation(**overrides),
            context=_context(),
            permission_resolver=resolver,
            audit_sink=_Sink(),
            now=NOW,
        )

    assert raised.value.reason == REFUSE_MALFORMED_INVOCATION
    assert resolver.calls == []


@pytest.mark.parametrize("invocation", [None, "invocation", 1, object()])
def test_non_invocation_refuses(invocation: object) -> None:
    refusal, sink, _ = _refusal(invocation=invocation)

    assert refusal.reason == REFUSE_MALFORMED_INVOCATION
    assert sink.records[0].capability_id is None
    assert sink.records[0].binding_id is None


@pytest.mark.parametrize(
    "permission",
    [
        None,
        "permission",
        _permission(decision_id=""),
        _permission(permission_revision=None),
        _permission(process_id="process 1"),
        _permission(capability_id="mail"),
        _permission(capability_grant_id=SECRET),
        _permission(transport_kind="unknown_transport"),
        _permission(transport_kind=""),
        _permission(destination_origin="api.example.com"),
        _permission(allowed=1),
        _permission(revoked=0),
    ],
    ids=[
        "none",
        "not-a-permission",
        "empty-decision-id",
        "none-revision",
        "space-in-process",
        "capability-without-namespace",
        "credential-shaped-grant",
        "unknown-transport",
        "empty-transport",
        "noncanonical-origin",
        "int-for-allowed",
        "int-for-revoked",
    ],
)
def test_malformed_permission_refuses(permission: object) -> None:
    refusal, sink, _ = _refusal(permission=permission)

    assert refusal.reason == REFUSE_PERMISSION_MALFORMED
    # Nothing of a decision this seam could not prove is quoted into the record.
    assert sink.records[0].decision_id is None
    assert sink.records[0].permission_revision is None


def test_resolver_failure_is_not_permission() -> None:
    refusal, sink, _ = _refusal(permission=RuntimeError("resolver exploded: " + SECRET))

    assert refusal.reason == REFUSE_PERMISSION_UNAVAILABLE
    assert sink.records[0].destination_origin == ORIGIN


# --- audit is the record of record ----------------------------------------------


def test_allowed_decision_reaches_audit_with_bounded_facts() -> None:
    _, sink, _ = _dispatch()

    assert sink.records == [
        DispatchAuditRecord(
            allowed=True,
            reason=ALLOW_REASON,
            transport_kind=TRANSPORT_LOCAL_IPC,
            process_id="process-1",
            worker_id="worker-1",
            tenant_id="tenant-1",
            project_id="project-1",
            run_id="run-1",
            attempt_id="attempt-1",
            capability_id=CAPABILITY,
            binding_id="binding-1",
            destination_origin=ORIGIN,
            decision_id="decision-1",
            permission_revision="revision-1",
        )
    ]


def test_refused_decision_reaches_audit_and_can_be_traced_to_its_revision() -> None:
    refusal, sink, _ = _refusal(permission=_permission(revoked=True))

    assert sink.records == [
        DispatchAuditRecord(
            allowed=False,
            reason=REFUSE_PERMISSION_REVOKED,
            transport_kind=TRANSPORT_LOCAL_IPC,
            process_id="process-1",
            worker_id="worker-1",
            tenant_id="tenant-1",
            project_id="project-1",
            run_id="run-1",
            attempt_id="attempt-1",
            capability_id=CAPABILITY,
            binding_id="binding-1",
            destination_origin=ORIGIN,
            decision_id="decision-1",
            permission_revision="revision-1",
        )
    ]
    assert refusal.reason == REFUSE_PERMISSION_REVOKED


def test_no_caller_value_reaches_a_refusal_or_a_record() -> None:
    """Malformed and credential-shaped values, from every direction at once."""
    sink = _Sink()
    poisoned_context = _context(attempt_id=SECRET, destination="https://" + SECRET)

    with pytest.raises(GatewayDispatchRefusal) as raised:
        authorize_dispatch(
            _invocation(binding_id=SECRET),
            context=poisoned_context,
            permission_resolver=_Resolver(RuntimeError(SECRET)),
            audit_sink=sink,
            now=NOW,
        )

    rendered = (
        repr(raised.value)
        + str(raised.value)
        + repr(raised.value.args)
        + repr(raised.value.__context__)
        + repr(sink.records)
    )
    assert SECRET not in rendered
    assert "resolver" not in rendered
    assert raised.value.reason == REFUSE_MALFORMED_CONTEXT


def test_resolver_exception_text_is_not_chained_onto_the_refusal() -> None:
    refusal, _, _ = _refusal(permission=RuntimeError(SECRET))

    assert refusal.__context__ is None
    assert refusal.__cause__ is None
    assert SECRET not in repr(refusal)


@pytest.mark.parametrize(
    "case",
    ["allow", "deny"],
)
def test_audit_sink_failure_is_the_only_refusal_and_yields_no_permit(case: str) -> None:
    sink = _Sink(fails=True)
    permission = _permission() if case == "allow" else _permission(allowed=False)

    with pytest.raises(GatewayDispatchRefusal) as raised:
        authorize_dispatch(
            _invocation(),
            context=_context(),
            permission_resolver=_Resolver(permission),
            audit_sink=sink,
            now=NOW,
        )

    assert raised.value.reason == REFUSE_AUDIT_UNAVAILABLE
    assert sink.records == []
    assert SECRET not in repr(raised.value)


# --- what a permit is, and what it must never carry -----------------------------


def test_permit_and_audit_record_are_immutable() -> None:
    permit, sink, _ = _dispatch()

    with pytest.raises(AttributeError):
        permit.destination_origin = "https://elsewhere.example.com"  # type: ignore[misc]
    with pytest.raises(AttributeError):
        sink.records[0].allowed = False  # type: ignore[misc]


def test_permit_carries_only_bounded_non_secret_facts() -> None:
    """No adapter, endpoint credential, database handle, secret or request data."""
    assert {field.name for field in fields(DispatchPermit)} == {
        "decision_id",
        "process_id",
        "worker_id",
        "tenant_id",
        "project_id",
        "run_id",
        "attempt_id",
        "capability_id",
        "binding_id",
        "destination_origin",
        "permission_revision",
        "authorized_at",
        "expires_at",
    }

    invocation = _invocation()
    permit, _, _ = _dispatch(invocation=invocation)
    rendered = repr(permit)
    for leak in (
        invocation.request_digest,
        invocation.idempotency_key,
        invocation.scopes[0],
        invocation.purpose,
        invocation.effect_class,
    ):
        assert leak not in rendered
