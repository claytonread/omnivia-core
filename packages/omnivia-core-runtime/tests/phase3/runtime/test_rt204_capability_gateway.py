"""RT-204 acceptance for the capability gateway and its binding resolver.

Every test here is a rule, not a scenario, because the seam under test is pure: no
database, no adapter, no registry, no clock. The binding inventory, the persisted
records and the instant are all arguments, so each rule can be isolated by changing one
field and nothing else.

*Fail closed on every branch.* The gateway returns an `AuthorizedInvocation` only by
proving every one of its checks. Each test below removes exactly one proof and asserts a
refusal, so a check that stops being load-bearing fails here rather than silently
widening authority.

*Discovery is not authority, and neither is order.* A discovered binding is excluded
before selection, so it can never be activated and can never make an approved binding
look ambiguous. Among approved bindings the highest satisfying version wins whatever
order the inventory is in, and two distinct bindings tying at that version are refused
as ambiguous rather than resolved by position.

*A resolver's answer is checked, never trusted.* A resolver is an argument, so the
gateway re-applies the eligibility rules to whatever comes back: a rogue resolver
returning an unapproved, discovered, misplaced or more consequential binding is refused.

*The ambiguities the accepted contract leaves open are asserted as open.* No accepted
contract says which effect classes require an approval, gives an `Approval` a field
naming a grant, or makes evidence mandatory for a class of action. The gateway therefore
requires an approval to *authorize* when one is supplied and requires the evidence a
proposal *names* to be the runtime's own retained record -- and asserts, rather than
assumes, that it invents neither requirement where the contract is silent.

No public wire surface is touched: `ActionProposal` has no schema and no codec, and the
refusal codes are the application contract's existing error vocabulary.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from omnivia_core_runtime.service.capability_gateway import (
    ActionProposal,
    AuthorizedInvocation,
    BindingCandidate,
    CapabilityGatewayRefusal,
    DeterministicBindingResolver,
    RuntimeAuthority,
    authorize_invocation,
)

from omnivia_core.contracts.v1 import (
    APPROVAL_DECISION_APPROVED,
    APPROVAL_DECISION_REJECTED,
    ERROR_CODE_AUTHORIZATION_DENIED,
    ERROR_CODE_CAPABILITY_NOT_GRANTED,
    ERROR_CODE_CONFLICT,
    ERROR_CODE_INTERNAL_NON_RECOVERABLE,
    ERROR_CODE_INVALID_PURPOSE,
    ERROR_CODE_INVALID_REQUEST,
    Approval,
    CapabilityGrant,
    EvidenceItem,
    ExternalReference,
    PolicySnapshot,
)

WORKSPACE = "ws-rt204"
RUN = "run-rt204"
INSTALLATION = "install-rt204"
CAPABILITY = "mail.send"
DIGEST = "sha256:" + "a" * 64
NOW = datetime(2026, 8, 23, 12, 0, 0, tzinfo=UTC)


def _stamp(offset_minutes: int = 0) -> str:
    moment = NOW + timedelta(minutes=offset_minutes)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _policy(
    *, granted: tuple[str, ...] = (CAPABILITY,), discovered: tuple[str, ...] = ()
) -> PolicySnapshot:
    return PolicySnapshot(
        workspace_id=WORKSPACE,
        policy_snapshot_id="policy-1",
        run_id=RUN,
        revision=1,
        pinned_at=_stamp(-60),
        granted_capabilities=granted,
        discovered_capabilities=discovered,
        decision_reason="policy.pinned",
        audit_reference="audit-policy-1",
    )


def _grant(**overrides: object) -> CapabilityGrant:
    grant = CapabilityGrant(
        workspace_id=WORKSPACE,
        capability_grant_id="grant-1",
        run_id=RUN,
        capability_id=CAPABILITY,
        policy_snapshot_id="policy-1",
        granted_at=_stamp(-30),
        scopes=("mail:send", "mail:read"),
        purpose="operations.support",
        expires_at=_stamp(30),
    )
    return replace(grant, **overrides)  # type: ignore[arg-type]


def _evidence(**overrides: object) -> EvidenceItem:
    item = EvidenceItem(
        workspace_id=WORKSPACE,
        evidence_item_id="evidence-1",
        run_id=RUN,
        evidence_kind="tool_output",
        source=ExternalReference(
            source_kind="runtime", source_id="runtime-1", workspace_id=WORKSPACE
        ),
        content_checksum="sha256:" + "b" * 64,
        captured_at=_stamp(-10),
        authoritative=True,
        retained=True,
    )
    return replace(item, **overrides)  # type: ignore[arg-type]


def _approval(**overrides: object) -> Approval:
    approval = Approval(
        workspace_id=WORKSPACE,
        approval_id="approval-1",
        run_id=RUN,
        wait_id="wait-1",
        requested_at=_stamp(-20),
        approver_role="operator",
        expires_at=_stamp(20),
        decision=APPROVAL_DECISION_APPROVED,
        decided_at=_stamp(-15),
        decided_by="person-1",
        audit_reference="audit-approval-1",
    )
    return replace(approval, **overrides)  # type: ignore[arg-type]


def _authority(**overrides: object) -> RuntimeAuthority:
    authority = RuntimeAuthority(
        workspace_id=WORKSPACE,
        run_id=RUN,
        installation_id=INSTALLATION,
        run_status="running",
        policy=_policy(),
        grant=_grant(),
        permitted_effect_classes=frozenset({"consequential_external", "read"}),
    )
    return replace(authority, **overrides)  # type: ignore[arg-type]


def _proposal(**overrides: object) -> ActionProposal:
    proposal = ActionProposal(
        workspace_id=WORKSPACE,
        run_id=RUN,
        capability_id=CAPABILITY,
        minimum_version="1.0",
        effect_class="consequential_external",
        purpose="operations.support",
        requested_scopes=("mail:send",),
        idempotency_key="idem-1",
        request_digest=DIGEST,
    )
    return replace(proposal, **overrides)  # type: ignore[arg-type]


def _candidate(**overrides: object) -> BindingCandidate:
    candidate = BindingCandidate(
        binding_id="binding-1",
        source_id="module-mail",
        capability_id=CAPABILITY,
        version="1.2",
        effect_class="consequential_external",
        scopes=("mail:send", "mail:read"),
        workspace_id=WORKSPACE,
        installation_id=INSTALLATION,
        approved=True,
        healthy=True,
        trusted=True,
        discovery_only=False,
    )
    return replace(candidate, **overrides)  # type: ignore[arg-type]


def _resolver(*candidates: BindingCandidate) -> DeterministicBindingResolver:
    return DeterministicBindingResolver(candidates=candidates or (_candidate(),))


def _authorize(
    *,
    proposal: ActionProposal | None = None,
    authority: RuntimeAuthority | None = None,
    resolver: DeterministicBindingResolver | None = None,
    now: datetime = NOW,
) -> AuthorizedInvocation:
    return authorize_invocation(
        proposal if proposal is not None else _proposal(),
        authority=authority if authority is not None else _authority(),
        resolver=resolver if resolver is not None else _resolver(),
        now=now,
    )


def _refused(**kwargs: object) -> CapabilityGatewayRefusal:
    with pytest.raises(CapabilityGatewayRefusal) as raised:
        _authorize(**kwargs)  # type: ignore[arg-type]
    return raised.value


# --- the resolver --------------------------------------------------------------


def test_resolver_selects_the_highest_satisfying_version_whatever_the_order() -> None:
    """Selection is by version, not by position in the inventory."""
    low = _candidate(binding_id="binding-low", version="1.0")
    high = _candidate(binding_id="binding-high", version="2.1")
    middle = _candidate(binding_id="binding-mid", version="1.9")
    forwards = DeterministicBindingResolver(candidates=(low, middle, high))
    backwards = DeterministicBindingResolver(candidates=(high, middle, low))
    authority = _authority()
    proposal = _proposal()
    assert forwards.resolve(proposal, authority=authority) == high
    assert backwards.resolve(proposal, authority=authority) == high


def test_resolver_refuses_two_distinct_bindings_at_the_same_highest_version() -> None:
    """A tie is an ambiguity: preferring either would make authority depend on order."""
    resolver = _resolver(
        _candidate(binding_id="binding-a", source_id="module-a"),
        _candidate(binding_id="binding-b", source_id="module-b"),
    )
    with pytest.raises(CapabilityGatewayRefusal) as raised:
        resolver.resolve(_proposal(), authority=_authority())
    assert raised.value.code == ERROR_CODE_CONFLICT


def test_resolver_collapses_the_same_binding_listed_twice() -> None:
    """One binding recorded twice is one binding, not an ambiguity."""
    candidate = _candidate()
    resolver = _resolver(candidate, candidate)
    assert resolver.resolve(_proposal(), authority=_authority()) == candidate


def test_resolver_never_activates_a_discovered_binding() -> None:
    """Discovery is not authority: a discovered binding is excluded before selection."""
    resolver = _resolver(_candidate(binding_id="binding-found", discovery_only=True))
    with pytest.raises(CapabilityGatewayRefusal) as raised:
        resolver.resolve(_proposal(), authority=_authority())
    assert raised.value.code == ERROR_CODE_CAPABILITY_NOT_GRANTED


def test_a_discovered_binding_cannot_make_an_approved_one_ambiguous() -> None:
    """Excluded before selection, so it is not a tie-breaker and not a tie."""
    approved = _candidate(binding_id="binding-approved")
    discovered = _candidate(
        binding_id="binding-found", source_id="module-other", discovery_only=True
    )
    resolver = _resolver(approved, discovered)
    assert resolver.resolve(_proposal(), authority=_authority()) == approved


@pytest.mark.parametrize(
    "excluded",
    [
        pytest.param({"approved": False}, id="unapproved"),
        pytest.param({"healthy": False}, id="unhealthy"),
        pytest.param({"trusted": False}, id="untrusted"),
        pytest.param({"discovery_only": True}, id="discovery_only"),
        pytest.param({"workspace_id": "ws-other"}, id="other_workspace"),
        pytest.param({"installation_id": "install-other"}, id="other_installation"),
        pytest.param({"capability_id": "mail.receive"}, id="other_capability"),
        pytest.param({"version": "0.9"}, id="below_the_version_floor"),
    ],
)
def test_resolver_excludes_every_candidate_that_is_not_usable(
    excluded: dict[str, object],
) -> None:
    """Approved, healthy, trusted, correctly placed, at or above the floor -- or nothing."""
    resolver = _resolver(_candidate(**excluded))
    with pytest.raises(CapabilityGatewayRefusal) as raised:
        resolver.resolve(_proposal(), authority=_authority())
    assert raised.value.code == ERROR_CODE_CAPABILITY_NOT_GRANTED


@pytest.mark.parametrize(
    "malformed",
    [
        pytest.param({"binding_id": "-not-an-identifier"}, id="malformed_identifier"),
        pytest.param({"binding_id": "binding\x071"}, id="control_character"),
        pytest.param({"binding_id": "binding-1\n"}, id="trailing_newline"),
        pytest.param({"version": "1"}, id="malformed_version"),
        pytest.param({"version": "1.0.0"}, id="release_version_is_another_domain"),
        pytest.param({"scopes": ("Mail:Send",)}, id="malformed_scope"),
        pytest.param({"scopes": ["mail:send"]}, id="scopes_not_a_tuple"),
        pytest.param({"effect_class": "destructive"}, id="unknown_effect_class"),
        pytest.param({"approved": 1}, id="flag_not_a_boolean"),
        pytest.param({"binding_id": "a" * 129}, id="unbounded_value"),
    ],
)
def test_resolver_refuses_a_malformed_inventory(malformed: dict[str, object]) -> None:
    """A malformed inventory row is this build's fault, and stops resolution outright."""
    resolver = _resolver(_candidate(**malformed))
    with pytest.raises(CapabilityGatewayRefusal) as raised:
        resolver.resolve(_proposal(), authority=_authority())
    assert raised.value.code == ERROR_CODE_INTERNAL_NON_RECOVERABLE


@pytest.mark.parametrize(
    "secret",
    [
        pytest.param("AKIAIOSFODNN7EXAMPLE", id="aws_access_key_id"),
        pytest.param(
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r",
            id="json_web_token",
        ),
        pytest.param("xoxb-12345678-abcdefghijkl", id="slack_token"),
    ],
)
def test_resolver_refuses_a_credential_shaped_value(secret: str) -> None:
    """A canonical value domain is exactly where a credential hides, so it is refused.

    Each of these is admitted by `Identifier`'s own character class. Left alone it would
    be copied into an `AuthorizedInvocation` and republished by whatever records one.
    """
    resolver = _resolver(_candidate(source_id=secret))
    with pytest.raises(CapabilityGatewayRefusal) as raised:
        resolver.resolve(_proposal(), authority=_authority())
    assert raised.value.code == ERROR_CODE_INTERNAL_NON_RECOVERABLE
    assert secret not in raised.value.message


def test_resolver_refuses_an_empty_inventory() -> None:
    resolver = DeterministicBindingResolver()
    with pytest.raises(CapabilityGatewayRefusal) as raised:
        resolver.resolve(_proposal(), authority=_authority())
    assert raised.value.code == ERROR_CODE_CAPABILITY_NOT_GRANTED


# --- successful authorization --------------------------------------------------


def test_authorizes_and_states_exactly_what_may_be_invoked() -> None:
    """The one success path, and the whole of what it hands on."""
    authority = _authority(approval=_approval(), evidence=(_evidence(),))
    invocation = _authorize(
        proposal=_proposal(required_evidence_ids=("evidence-1",)), authority=authority
    )
    assert invocation == AuthorizedInvocation(
        workspace_id=WORKSPACE,
        run_id=RUN,
        capability_id=CAPABILITY,
        binding_id="binding-1",
        binding_source_id="module-mail",
        binding_version="1.2",
        effect_class="consequential_external",
        scopes=("mail:send",),
        purpose="operations.support",
        capability_grant_id="grant-1",
        policy_snapshot_id="policy-1",
        idempotency_key="idem-1",
        request_digest=DIGEST,
        evidence_item_ids=("evidence-1",),
        authorized_at=NOW,
        approval_id="approval-1",
    )


def test_the_invocation_carries_no_adapter_handle() -> None:
    """Authority is what an invocation carries; the means of acting is dispatch's."""
    fields = set(AuthorizedInvocation.__dataclass_fields__)
    assert not fields & {"handle", "endpoint", "adapter", "credential", "transport"}


# --- proposal shape and bounds -------------------------------------------------


def test_a_proposal_of_the_wrong_type_is_refused_as_malformed() -> None:
    refusal = _refused(proposal="mail.send")
    assert refusal.code == ERROR_CODE_INVALID_REQUEST


@pytest.mark.parametrize(
    "uncanonical",
    [
        pytest.param({"capability_id": "Mail.Send"}, id="malformed_capability_id"),
        pytest.param({"capability_id": "mail"}, id="capability_id_below_min_length"),
        pytest.param({"minimum_version": "1.0.0"}, id="wrong_version_domain"),
        pytest.param({"purpose": "operations\x07support"}, id="control_character"),
        pytest.param({"purpose": "operations.support\n"}, id="trailing_newline"),
        pytest.param({"requested_scopes": ()}, id="no_scope_requested"),
        pytest.param({"requested_scopes": "mail:send"}, id="scopes_not_a_tuple"),
        pytest.param({"request_digest": "sha256:beef"}, id="malformed_digest"),
        pytest.param({"idempotency_key": "idem 1"}, id="malformed_idempotency_key"),
        pytest.param({"run_id": "a" * 129}, id="unbounded_value"),
        pytest.param(
            {"required_evidence_ids": ("evidence 1",)}, id="malformed_evidence_id"
        ),
    ],
)
def test_a_proposal_outside_a_value_domain_is_refused(
    uncanonical: dict[str, object],
) -> None:
    """Typed is not the same as canonical, and every field is held to its own domain."""
    refusal = _refused(proposal=_proposal(**uncanonical))
    assert refusal.code == ERROR_CODE_INVALID_REQUEST


def test_a_secret_bearing_proposal_is_refused_without_repeating_it() -> None:
    secret = "ghp_" + "A" * 36
    refusal = _refused(proposal=_proposal(idempotency_key=secret))
    assert refusal.code == ERROR_CODE_INVALID_REQUEST
    assert secret not in refusal.message


@pytest.mark.parametrize(
    "unbounded",
    [
        pytest.param(
            {"requested_scopes": tuple(f"mail:s{index}" for index in range(65))},
            id="too_many_scopes",
        ),
        pytest.param(
            {"required_evidence_ids": tuple(f"evidence-{i}" for i in range(65))},
            id="too_many_evidence_ids",
        ),
    ],
)
def test_an_unbounded_list_is_refused(unbounded: dict[str, object]) -> None:
    """Cardinality is part of the domain: a caller does not choose how much is carried."""
    refusal = _refused(proposal=_proposal(**unbounded))
    assert refusal.code == ERROR_CODE_INVALID_REQUEST


@pytest.mark.parametrize(
    "instant",
    [
        pytest.param(NOW.replace(tzinfo=None), id="naive"),
        pytest.param("2026-08-23T12:00:00Z", id="not_a_datetime"),
    ],
)
def test_an_unusable_instant_is_refused(instant: object) -> None:
    """A grant window cannot be decided against a clock in an unknown zone."""
    refusal = _refused(now=instant)
    assert refusal.code == ERROR_CODE_INVALID_REQUEST


# --- authority and run status --------------------------------------------------


@pytest.mark.parametrize(
    "unusable",
    [
        pytest.param({"policy": "policy-1"}, id="policy_not_a_snapshot"),
        pytest.param({"grant": None}, id="grant_absent"),
        pytest.param({"evidence": [None]}, id="evidence_not_a_tuple_of_items"),
        pytest.param({"run_id": "run rt204"}, id="malformed_run_id"),
        pytest.param(
            {"permitted_effect_classes": frozenset({"destructive"})},
            id="effect_class_outside_the_vocabulary",
        ),
        pytest.param(
            {"permitted_effect_classes": {"consequential_external"}},
            id="allowlist_not_a_frozenset",
        ),
    ],
)
def test_unusable_authority_is_reported_as_service_state(
    unusable: dict[str, object],
) -> None:
    """This build's fault is not reported as a grant the caller lacks."""
    refusal = _refused(authority=_authority(**unusable))
    assert refusal.code == ERROR_CODE_INTERNAL_NON_RECOVERABLE


def test_authority_of_the_wrong_type_is_reported_as_service_state() -> None:
    refusal = _refused(authority=_grant())
    assert refusal.code == ERROR_CODE_INTERNAL_NON_RECOVERABLE


@pytest.mark.parametrize(
    "mismatch",
    [
        pytest.param({"run_id": "run-other"}, id="another_run"),
        pytest.param({"workspace_id": "ws-other"}, id="another_workspace"),
    ],
)
def test_authority_for_another_run_authorizes_nothing(
    mismatch: dict[str, object],
) -> None:
    refusal = _refused(proposal=_proposal(**mismatch))
    assert refusal.code == ERROR_CODE_AUTHORIZATION_DENIED


@pytest.mark.parametrize(
    "status",
    [
        "admitted",
        "waiting",
        "succeeded",
        "failed",
        "cancelled",
        "uncertain",
        "sprinting",
    ],
)
def test_only_a_running_run_may_act(status: str) -> None:
    """An unrecognized status grants nothing either -- it is read as no permission."""
    refusal = _refused(authority=_authority(run_status=status))
    assert refusal.code == ERROR_CODE_AUTHORIZATION_DENIED


# --- effect class ---------------------------------------------------------------


def test_an_effect_class_this_build_does_not_know_is_refused() -> None:
    refusal = _refused(proposal=_proposal(effect_class="destructive"))
    assert refusal.code == ERROR_CODE_INVALID_REQUEST


def test_an_effect_class_the_run_may_not_act_in_is_denied() -> None:
    authority = _authority(permitted_effect_classes=frozenset({"read"}))
    refusal = _refused(authority=authority)
    assert refusal.code == ERROR_CODE_AUTHORIZATION_DENIED


def test_an_authority_with_no_permitted_effect_class_refuses_everything() -> None:
    """The default is the refusing value: under-specified authority permits nothing."""
    authority = RuntimeAuthority(
        workspace_id=WORKSPACE,
        run_id=RUN,
        installation_id=INSTALLATION,
        run_status="running",
        policy=_policy(),
        grant=_grant(),
    )
    refusal = _refused(authority=authority)
    assert refusal.code == ERROR_CODE_AUTHORIZATION_DENIED


# --- the grant ------------------------------------------------------------------


def test_a_capability_only_discovered_is_not_a_grant() -> None:
    """Discovery is not authority, decided by the contract's own grant validator."""
    authority = _authority(policy=_policy(granted=(), discovered=(CAPABILITY,)))
    refusal = _refused(authority=authority)
    assert refusal.code == ERROR_CODE_CAPABILITY_NOT_GRANTED
    assert CAPABILITY not in refusal.message


def test_a_grant_for_another_capability_does_not_authorize_this_one() -> None:
    authority = _authority(
        policy=_policy(granted=(CAPABILITY, "mail.receive")),
        grant=_grant(capability_id="mail.receive"),
    )
    refusal = _refused(authority=authority)
    assert refusal.code == ERROR_CODE_CAPABILITY_NOT_GRANTED


def test_a_grant_naming_another_policy_snapshot_does_not_authorize() -> None:
    authority = _authority(grant=_grant(policy_snapshot_id="policy-2"))
    refusal = _refused(authority=authority)
    assert refusal.code == ERROR_CODE_CAPABILITY_NOT_GRANTED


@pytest.mark.parametrize(
    "instant",
    [
        pytest.param(NOW + timedelta(minutes=31), id="after_expiry"),
        pytest.param(NOW + timedelta(minutes=30), id="at_expiry"),
        pytest.param(NOW - timedelta(minutes=31), id="before_the_grant_was_issued"),
    ],
)
def test_a_grant_outside_its_window_does_not_authorize(instant: datetime) -> None:
    """Both ends of the window: expired, and not yet in force."""
    refusal = _refused(now=instant)
    assert refusal.code == ERROR_CODE_CAPABILITY_NOT_GRANTED


def test_a_grant_with_no_expiry_stays_in_force() -> None:
    """`expires_at` is optional in the accepted contract, and absence is not expiry."""
    authority = _authority(grant=_grant(expires_at=None))
    invocation = _authorize(authority=authority, now=NOW + timedelta(days=365))
    assert invocation.capability_grant_id == "grant-1"


# --- the resolved binding -------------------------------------------------------


class _RogueResolver:
    """A resolver that answers with whatever it was handed, however unusable."""

    def __init__(self, answer: object) -> None:
        self._answer = answer

    def resolve(self, proposal: object, *, authority: object) -> BindingCandidate:
        return self._answer  # type: ignore[return-value]


@pytest.mark.parametrize(
    "returned",
    [
        pytest.param({"approved": False}, id="unapproved"),
        pytest.param({"discovery_only": True}, id="discovered"),
        pytest.param({"healthy": False}, id="unhealthy"),
        pytest.param({"trusted": False}, id="untrusted"),
        pytest.param({"workspace_id": "ws-other"}, id="another_workspace"),
        pytest.param({"installation_id": "install-other"}, id="another_installation"),
        pytest.param({"capability_id": "mail.receive"}, id="another_capability"),
        pytest.param({"version": "0.9"}, id="below_the_version_floor"),
        pytest.param({"effect_class": "read"}, id="another_effect_class"),
    ],
)
def test_a_resolver_cannot_widen_authority_by_what_it_returns(
    returned: dict[str, object],
) -> None:
    """A resolver is an argument, so its answer is re-checked rather than trusted."""
    resolver = _RogueResolver(_candidate(**returned))
    with pytest.raises(CapabilityGatewayRefusal) as raised:
        authorize_invocation(
            _proposal(), authority=_authority(), resolver=resolver, now=NOW
        )
    assert raised.value.code == ERROR_CODE_AUTHORIZATION_DENIED


def test_a_resolver_answering_with_something_else_entirely_is_refused() -> None:
    resolver = _RogueResolver("binding-1")
    with pytest.raises(CapabilityGatewayRefusal) as raised:
        authorize_invocation(
            _proposal(), authority=_authority(), resolver=resolver, now=NOW
        )
    assert raised.value.code == ERROR_CODE_INTERNAL_NON_RECOVERABLE


# --- scopes and purpose ---------------------------------------------------------


def test_a_scope_the_grant_does_not_carry_is_denied() -> None:
    proposal = _proposal(requested_scopes=("mail:send", "mail:delete"))
    resolver = _resolver(_candidate(scopes=("mail:send", "mail:delete")))
    refusal = _refused(proposal=proposal, resolver=resolver)
    assert refusal.code == ERROR_CODE_AUTHORIZATION_DENIED


def test_a_scope_the_binding_does_not_carry_is_denied() -> None:
    """Both halves are required: a grant is not a binding's competence."""
    proposal = _proposal(requested_scopes=("mail:read",))
    resolver = _resolver(_candidate(scopes=("mail:send",)))
    refusal = _refused(proposal=proposal, resolver=resolver)
    assert refusal.code == ERROR_CODE_AUTHORIZATION_DENIED


def test_a_purpose_the_grant_was_not_issued_for_is_refused() -> None:
    """A grant states one purpose, so this is equality rather than membership."""
    refusal = _refused(proposal=_proposal(purpose="operations.audit"))
    assert refusal.code == ERROR_CODE_INVALID_PURPOSE


# --- approval -------------------------------------------------------------------


@pytest.mark.parametrize(
    "approval",
    [
        pytest.param(_approval(decision=APPROVAL_DECISION_REJECTED), id="rejected"),
        pytest.param(
            _approval(
                decision=None, decided_at=None, decided_by=None, audit_reference=None
            ),
            id="still_pending",
        ),
        pytest.param(
            _approval(decided_at=_stamp(25)), id="decided_after_the_request_expired"
        ),
        pytest.param(_approval(run_id="run-other"), id="for_another_run"),
        pytest.param(_approval(workspace_id="ws-other"), id="for_another_workspace"),
        pytest.param(_approval(decided_by="person 1"), id="malformed_decider"),
    ],
)
def test_an_approval_that_does_not_authorize_denies_the_action(
    approval: Approval,
) -> None:
    refusal = _refused(authority=_authority(approval=approval))
    assert refusal.code == ERROR_CODE_AUTHORIZATION_DENIED


def test_an_approved_decision_is_carried_into_the_invocation() -> None:
    invocation = _authorize(authority=_authority(approval=_approval()))
    assert invocation.approval_id == "approval-1"


def test_no_effect_class_requires_an_approval_this_contract_does_not_state() -> None:
    """The ambiguity, asserted as an ambiguity rather than closed by invention.

    No accepted contract says which effect classes require an approval, and an
    `Approval` has no field naming a grant or an action. So a consequential action with
    no approval supplied is authorized on the grant, the binding and the scopes alone,
    and `approval_id` is `None` -- the gateway states what it checked rather than
    implying an approval it never saw.
    """
    invocation = _authorize(proposal=_proposal(effect_class="consequential_external"))
    assert invocation.approval_id is None
    assert invocation.effect_class == "consequential_external"


# --- evidence -------------------------------------------------------------------


@pytest.mark.parametrize(
    "evidence",
    [
        pytest.param((), id="not_held_at_all"),
        pytest.param((_evidence(evidence_item_id="evidence-2"),), id="another_item"),
        pytest.param((_evidence(retained=False),), id="not_retained"),
        pytest.param((_evidence(authoritative=False),), id="not_authoritative"),
        pytest.param(
            (
                _evidence(
                    authoritative=False,
                    source=ExternalReference(
                        source_kind="external_log",
                        source_id="log-1",
                        workspace_id=WORKSPACE,
                    ),
                ),
            ),
            id="subordinate_source",
        ),
        pytest.param((_evidence(captured_at="yesterday"),), id="malformed_item"),
    ],
)
def test_required_evidence_must_be_the_runtimes_own_retained_record(
    evidence: tuple[EvidenceItem, ...],
) -> None:
    """An external log corroborates the runtime's record; it never replaces it."""
    refusal = _refused(
        proposal=_proposal(required_evidence_ids=("evidence-1",)),
        authority=_authority(evidence=evidence),
    )
    assert refusal.code == ERROR_CODE_AUTHORIZATION_DENIED


def test_two_evidence_items_with_one_id_are_unusable_service_state() -> None:
    """Which of two records with one id was meant is not a question to answer by order."""
    duplicated = (_evidence(), _evidence(content_checksum="sha256:" + "c" * 64))
    refusal = _refused(
        proposal=_proposal(required_evidence_ids=("evidence-1",)),
        authority=_authority(evidence=duplicated),
    )
    assert refusal.code == ERROR_CODE_INTERNAL_NON_RECOVERABLE


def test_evidence_is_required_only_where_the_proposal_names_it() -> None:
    """The second ambiguity, asserted as one.

    No accepted contract makes evidence mandatory for any class of action, so the
    gateway requires exactly what a proposal names and nothing more -- and reports what
    it proved, so a caller naming none cannot be read as having proved any.
    """
    invocation = _authorize(authority=_authority(evidence=(_evidence(),)))
    assert invocation.evidence_item_ids == ()


# --- refusals say nothing -------------------------------------------------------


def test_a_refusal_repeats_no_value_it_was_handed() -> None:
    """A refusal is rendered into logs, audit records and a wire error; it names a category."""
    secret = "sk_live_" + "0" * 24
    refusal = _refused(
        proposal=_proposal(purpose="operations.audit", idempotency_key="idem-1"),
        authority=_authority(grant=_grant(purpose="operations.support")),
    )
    assert refusal.code == ERROR_CODE_INVALID_PURPOSE
    for value in (secret, "operations.audit", "operations.support", CAPABILITY, RUN):
        assert value not in refusal.message
    assert refusal.as_api_error().code == ERROR_CODE_INVALID_PURPOSE
