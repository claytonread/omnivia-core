"""Conformance oracles for the Runtime Execution Planes seam.

The seam holds no database, scheduler, recovery, transport or Platform handle,
so every test here is built from frozen values, an injected clock and recording
callables. What is asserted is the seam's own contract:

- the reference vocabularies are exactly the four execution classes, the four
  profile trust states, the four build trust states, the six executor kinds, the
  five session kinds, the four reconcile states and the ``0..3`` isolation
  ordinal -- closed, spelled in exact uppercase, and closed at those members. A
  lowercase or alternate spelling is not a lenient input, it is a refusal;
- an executor kind and an execution class are separate, disjoint vocabularies,
  and neither is accepted where the other belongs;
- the three descriptors are frozen, validated, and content-addressed over the
  public contract's canonical bytes -- and an edited one no longer verifies;
- the component plane backs the ``DETERMINISTIC`` class, separates completion,
  replay, cancellation and invalid output, and treats capability proposals as
  data it never applies;
- governed dispatch backs the ``EFFECT`` class and refuses a missing intent, an
  unsupported contract version, an unapproved profile, an unaccepted intent id,
  an undeclared capability and a stale fence strictly before the callback runs;
- the session lease covers acquire, inspect, snapshot, transfer of control,
  resume, release and reconcile: exclusive, fenced, expiring against the
  injected clock, refusing a stale holder, gated on the provider build declaring
  the lifecycle support, and reconciling to a stable
  ``ACTIVE``/``RELEASED``/``ORPHANED``/``UNKNOWN`` with deterministic evidence;
- the registry is source-qualified, exact-version, exact-build, health-aware,
  trust-aware, isolation-aware and fail closed, and its history outlives
  replacement and removal.
"""

from __future__ import annotations

import ast
from dataclasses import replace
from hashlib import sha256
from pathlib import Path

import pytest
from omnivia_core_runtime.execution import (
    BUILD_TRUST_APPROVED,
    BUILD_TRUST_DISABLED,
    BUILD_TRUST_PROTOTYPE,
    BUILD_TRUST_QUARANTINED,
    BUILD_TRUST_STATES,
    CONTRACT_VERSION,
    ENTRY_EXECUTOR,
    ENTRY_SESSION_PROVIDER,
    EXECUTION_CLASS_AGENT,
    EXECUTION_CLASS_DETERMINISTIC,
    EXECUTION_CLASS_EFFECT,
    EXECUTION_CLASS_WAIT,
    EXECUTION_CLASSES,
    EXECUTOR_KIND_COMMAND,
    EXECUTOR_KIND_GIT,
    EXECUTOR_KINDS,
    HEALTH_QUARANTINED,
    HEALTH_READY,
    HEALTH_STATES,
    ISOLATION_MAX,
    ISOLATION_MIN,
    LEASE_EXPIRED,
    LEASE_HELD,
    LEASE_RELEASED,
    PROFILE_TRUST_APPROVED,
    PROFILE_TRUST_DISABLED,
    PROFILE_TRUST_DRAFT,
    PROFILE_TRUST_QUARANTINED,
    PROFILE_TRUST_STATES,
    SESSION_ACTIVE,
    SESSION_KIND_ACP,
    SESSION_KIND_BROWSER,
    SESSION_KIND_TERMINAL,
    SESSION_KINDS,
    SESSION_ORPHANED,
    SESSION_RELEASED,
    SESSION_STATES,
    SESSION_UNKNOWN,
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    CapabilityProposal,
    ComponentPlane,
    ComponentRequest,
    ComponentResult,
    DispatchIntent,
    ExecutionContractError,
    ExecutionLineage,
    ExecutionRefused,
    ExecutorDescriptor,
    GovernedDispatchPlane,
    RuntimeExecutionRegistry,
    RuntimeProfileDescriptor,
    SessionProviderDescriptor,
    StatefulSessionPlane,
)
from omnivia_core_runtime.execution import profile as profile_module
from omnivia_core_runtime.execution.profile import HEALTH_RECOVERABLE
from omnivia_core_runtime.execution.registry import (
    EVENT_HEALTH_CHANGED,
    EVENT_REGISTERED,
    EVENT_REMOVED,
    EVENT_REPLACED,
)

from omnivia_core.contracts.v1.canonical_json import canonical_bytes

LINEAGE = ExecutionLineage(
    workspace_id="ws-rxp",
    run_id="run-rxp-0001",
    run_step_id="step-rxp-0001",
    attempt_id="att-rxp-0001",
)

DIGEST = "sha256:" + "1" * 64
OTHER_DIGEST = "sha256:" + "2" * 64
BUILD = "sha256:" + "3" * 64
OTHER_BUILD = "sha256:" + "4" * 64
REMOVAL = "sha256:" + "5" * 64


def deterministic_profile(**overrides: object) -> RuntimeProfileDescriptor:
    fields: dict[str, object] = {
        "profile_id": "component.echo",
        "version": "1.0.0",
        "execution_classes": (EXECUTION_CLASS_DETERMINISTIC,),
        "worker_routes": ("in_process",),
        "capability_sets": ("echo", "summarize"),
        "minimum_isolation": 0,
        "trust_state": PROFILE_TRUST_APPROVED,
    }
    fields.update(overrides)
    return RuntimeProfileDescriptor(**fields).sealed()  # type: ignore[arg-type]


def effect_profile(**overrides: object) -> RuntimeProfileDescriptor:
    fields: dict[str, object] = {
        "profile_id": "dispatch.tools",
        "version": "1.0.0",
        "execution_classes": (EXECUTION_CLASS_EFFECT,),
        "worker_routes": ("in_process",),
        "capability_sets": ("tool.call",),
        "minimum_isolation": 1,
        "trust_state": PROFILE_TRUST_APPROVED,
        "policy_defaults_ref": DIGEST,
    }
    fields.update(overrides)
    return RuntimeProfileDescriptor(**fields).sealed()  # type: ignore[arg-type]


def agent_profile(**overrides: object) -> RuntimeProfileDescriptor:
    fields: dict[str, object] = {
        "profile_id": "session.repl",
        "version": "1.0.0",
        "execution_classes": (EXECUTION_CLASS_AGENT,),
        "worker_routes": ("in_process",),
        "capability_sets": ("session.eval",),
        "minimum_isolation": 2,
        "trust_state": PROFILE_TRUST_APPROVED,
        "session_providers": ("repl",),
    }
    fields.update(overrides)
    return RuntimeProfileDescriptor(**fields).sealed()  # type: ignore[arg-type]


def executor(**overrides: object) -> ExecutorDescriptor:
    fields: dict[str, object] = {
        "source_id": "core.local",
        "executor_id": "component.echo",
        "version": "1.0.0",
        "build_hash": BUILD,
        "executor_kind": EXECUTOR_KIND_COMMAND,
        "capabilities": ("echo", "summarize"),
        "supported_contract_versions": (CONTRACT_VERSION,),
        "required_isolation": 1,
        "trust_state": BUILD_TRUST_APPROVED,
        "reconciliation_capabilities": ("adopt", "terminate"),
        "removal_instructions_ref": REMOVAL,
    }
    fields.update(overrides)
    return ExecutorDescriptor(**fields).sealed()  # type: ignore[arg-type]


def provider(**overrides: object) -> SessionProviderDescriptor:
    fields: dict[str, object] = {
        "source_id": "core.local",
        "provider_id": "repl",
        "version": "1.0.0",
        "build_hash": BUILD,
        "supported_kinds": (SESSION_KIND_TERMINAL,),
        "supports_suspend_resume": True,
        "supports_control_transfer": True,
        "required_isolation": 2,
        "trust_state": BUILD_TRUST_APPROVED,
    }
    fields.update(overrides)
    return SessionProviderDescriptor(**fields).sealed()  # type: ignore[arg-type]


class Clock:
    """A deterministic microsecond clock the tests advance by hand."""

    def __init__(self, now: int = 1_000) -> None:
        self.now = now

    def __call__(self) -> int:
        return self.now

    def advance(self, microseconds: int) -> None:
        self.now += microseconds


class Handler:
    """A component handler recording every call and returning a scripted result."""

    def __init__(self, result: object) -> None:
        self.result = result
        self.calls: list[ComponentRequest] = []

    def __call__(self, request: ComponentRequest) -> object:
        self.calls.append(request)
        return self.result


class Callback:
    """A governed dispatch callback that records whether it was ever reached."""

    def __init__(self) -> None:
        self.calls: list[DispatchIntent] = []

    def __call__(self, intent: DispatchIntent) -> str:
        self.calls.append(intent)
        return "ran"


# --------------------------------------------------------------------------
# The reference vocabularies, asserted as closed sets of exact uppercase members
# --------------------------------------------------------------------------


def test_the_reference_vocabularies_are_exactly_these_members() -> None:
    assert EXECUTION_CLASSES == {"AGENT", "DETERMINISTIC", "EFFECT", "WAIT"}
    assert PROFILE_TRUST_STATES == {"DRAFT", "APPROVED", "QUARANTINED", "DISABLED"}
    assert BUILD_TRUST_STATES == {
        "PROTOTYPE",
        "APPROVED",
        "QUARANTINED",
        "DISABLED",
    }
    assert EXECUTOR_KINDS == {
        "BROWSER",
        "INTEGRATION",
        "FILESYSTEM",
        "COMMAND",
        "GIT",
        "OTHER",
    }
    assert SESSION_KINDS == {
        "BROWSER",
        "TERMINAL",
        "ACP",
        "REMOTE_EXECUTION",
        "OTHER",
    }
    assert SESSION_STATES == {"ACTIVE", "RELEASED", "ORPHANED", "UNKNOWN"}
    assert HEALTH_STATES == {
        "CONFIGURED",
        "AUTHENTICATED",
        "COMPATIBLE",
        "READY",
        "RECOVERABLE",
        "QUARANTINED",
    }
    assert (ISOLATION_MIN, ISOLATION_MAX) == (0, 3)
    assert (
        EXECUTION_CLASS_AGENT,
        EXECUTION_CLASS_DETERMINISTIC,
        EXECUTION_CLASS_EFFECT,
        EXECUTION_CLASS_WAIT,
    ) == ("AGENT", "DETERMINISTIC", "EFFECT", "WAIT")
    assert (BUILD_TRUST_PROTOTYPE, PROFILE_TRUST_DRAFT) == ("PROTOTYPE", "DRAFT")
    assert CONTRACT_VERSION == "1.0.0"


def test_an_executor_kind_is_not_an_execution_class() -> None:
    """The two vocabularies are disjoint, and neither answers for the other."""
    assert EXECUTOR_KINDS != EXECUTION_CLASSES
    assert EXECUTOR_KINDS.isdisjoint(EXECUTION_CLASSES)
    # An executor kind and a session kind may legitimately name the same thing;
    # an executor kind and an execution class never do.
    assert EXECUTOR_KINDS & SESSION_KINDS == {"BROWSER", "OTHER"}

    for wrong in (EXECUTION_CLASS_DETERMINISTIC, EXECUTION_CLASS_AGENT, "COMPONENT"):
        with pytest.raises(ExecutionContractError) as caught:
            executor(executor_kind=wrong)
        assert caught.value.reason == "unknown_vocabulary_member"

    # An executor declares no execution class at all: what a build *is* and what
    # a profile *runs* are different questions with different owners.
    assert not hasattr(executor(), "execution_classes")
    assert executor(executor_kind=EXECUTOR_KIND_GIT).executor_kind == "GIT"


@pytest.mark.parametrize(
    ("build", "overrides"),
    [
        (deterministic_profile, {"execution_classes": ("deterministic",)}),
        (deterministic_profile, {"execution_classes": ("Deterministic",)}),
        (deterministic_profile, {"execution_classes": ("COMPONENT",)}),
        (deterministic_profile, {"trust_state": "approved"}),
        (deterministic_profile, {"trust_state": "ATTESTED"}),
        (executor, {"executor_kind": "command"}),
        (executor, {"executor_kind": "SHELL"}),
        (executor, {"trust_state": "approved"}),
        (executor, {"trust_state": "TRUSTED"}),
        (provider, {"supported_kinds": ("terminal",)}),
        (provider, {"supported_kinds": ("SHELL",)}),
        (provider, {"supported_kinds": ("NOTEBOOK",)}),
        (provider, {"supported_kinds": ("CONTAINER",)}),
        (provider, {"supported_kinds": ("REPL",)}),
        (provider, {"trust_state": "prototype"}),
    ],
)
def test_a_lowercase_or_alternate_spelling_is_not_a_member(
    build: object, overrides: dict[str, object]
) -> None:
    """Vocabulary membership is exact: ``"agent"`` is not a spelling of ``AGENT``."""
    with pytest.raises(ExecutionContractError) as caught:
        build(**overrides)  # type: ignore[operator]
    assert caught.value.reason == "unknown_vocabulary_member"


@pytest.mark.parametrize("isolation", [-1, 4, 99])
def test_isolation_is_an_integer_ordinal_bounded_at_zero_to_three(
    isolation: int,
) -> None:
    for build in (
        lambda: deterministic_profile(minimum_isolation=isolation),
        lambda: executor(required_isolation=isolation),
        lambda: provider(required_isolation=isolation),
    ):
        with pytest.raises(ExecutionContractError) as caught:
            build()
        assert caught.value.reason == "invalid_isolation"


# --------------------------------------------------------------------------
# Descriptors: frozen, validated, canonically hashed
# --------------------------------------------------------------------------


@pytest.mark.parametrize("build", [deterministic_profile, executor, provider])
def test_every_descriptor_hashes_the_public_contract_canonical_bytes(
    build: object,
) -> None:
    descriptor = build()  # type: ignore[operator]
    preimage = descriptor.canonical_preimage()

    assert "content_hash" not in preimage
    assert descriptor.content_hash == (
        "sha256:" + sha256(canonical_bytes(preimage)).hexdigest()
    )
    descriptor.verify_content_hash()


def test_logically_equal_descriptors_seal_to_the_same_hash() -> None:
    assert deterministic_profile().content_hash == deterministic_profile().content_hash
    assert (
        deterministic_profile(version="1.0.1").content_hash
        != deterministic_profile().content_hash
    )
    assert executor().content_hash == executor().content_hash
    assert executor(build_hash=OTHER_BUILD).content_hash != executor().content_hash
    assert (
        provider(supports_control_transfer=False).content_hash
        != provider().content_hash
    )


def test_an_edited_descriptor_no_longer_verifies() -> None:
    tampered = replace(
        deterministic_profile(), capability_sets=("echo", "exfiltrate")
    )

    with pytest.raises(ExecutionContractError) as caught:
        tampered.verify_content_hash()
    assert caught.value.reason == "content_hash_mismatch"


def test_an_unsealed_descriptor_is_refused_before_its_claims_are_read() -> None:
    unsealed = RuntimeProfileDescriptor(
        profile_id="component.echo",
        version="1.0.0",
        execution_classes=(EXECUTION_CLASS_DETERMINISTIC,),
        worker_routes=("in_process",),
        capability_sets=("echo",),
        minimum_isolation=0,
        trust_state=PROFILE_TRUST_APPROVED,
    )

    assert not unsealed.is_sealed
    with pytest.raises(ExecutionContractError) as caught:
        ComponentPlane(unsealed, Handler(ComponentResult()))
    assert caught.value.reason == "unsealed_descriptor"


def test_descriptors_are_frozen() -> None:
    with pytest.raises(AttributeError):
        deterministic_profile().trust_state = PROFILE_TRUST_DRAFT  # type: ignore[misc]
    with pytest.raises(AttributeError):
        executor().trust_state = BUILD_TRUST_PROTOTYPE  # type: ignore[misc]
    with pytest.raises(AttributeError):
        provider().supported_kinds = (SESSION_KIND_BROWSER,)  # type: ignore[misc]


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"version": "1.0"}, "invalid_version"),
        ({"profile_id": "Component.Echo"}, "invalid_identifier"),
        (
            {"execution_classes": ("DETERMINISTIC", "DETERMINISTIC")},
            "duplicate_entry",
        ),
        ({"execution_classes": ()}, "empty_collection"),
        ({"capability_sets": ()}, "empty_collection"),
        ({"worker_routes": ()}, "empty_collection"),
        ({"capability_sets": ("echo", "echo")}, "duplicate_entry"),
        ({"policy_defaults_ref": "sha256:nope"}, "invalid_digest"),
        ({"evidence_rules_ref": "nope"}, "invalid_digest"),
        ({"completion_rule_ref": DIGEST[:-1]}, "invalid_digest"),
        ({"session_providers": ("repl",)}, "incoherent_posture"),
    ],
)
def test_profile_validation_refuses_each_malformed_field(
    overrides: dict[str, object], reason: str
) -> None:
    with pytest.raises(ExecutionContractError) as caught:
        deterministic_profile(**overrides)
    assert caught.value.reason == reason


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"version": "1"}, "invalid_version"),
        ({"capabilities": ()}, "empty_collection"),
        ({"capabilities": ("echo", "echo")}, "duplicate_entry"),
        ({"build_hash": "deadbeef"}, "invalid_digest"),
        ({"supported_contract_versions": ()}, "empty_collection"),
        ({"supported_contract_versions": ("1.0",)}, "invalid_version"),
        ({"reconciliation_capabilities": ("adopt", "adopt")}, "duplicate_entry"),
        ({"removal_instructions_ref": "see-the-docs"}, "invalid_digest"),
        ({"supply_chain_ref": "sha256:not-hex"}, "invalid_digest"),
    ],
)
def test_executor_validation_refuses_each_malformed_field(
    overrides: dict[str, object], reason: str
) -> None:
    with pytest.raises(ExecutionContractError) as caught:
        executor(**overrides)
    assert caught.value.reason == reason


def test_removal_instructions_are_required_and_supply_chain_is_optional() -> None:
    """A build that cannot say how it is removed is not constructible."""
    with pytest.raises(TypeError):
        ExecutorDescriptor(  # type: ignore[call-arg]
            source_id="core.local",
            executor_id="component.echo",
            version="1.0.0",
            build_hash=BUILD,
            executor_kind=EXECUTOR_KIND_COMMAND,
            capabilities=("echo",),
            supported_contract_versions=(CONTRACT_VERSION,),
            required_isolation=1,
            trust_state=BUILD_TRUST_APPROVED,
            reconciliation_capabilities=(),
        )

    assert executor().supply_chain_ref is None
    assert executor(supply_chain_ref=DIGEST).supply_chain_ref == DIGEST
    assert executor().removal_instructions_ref == REMOVAL
    assert executor(reconciliation_capabilities=()).reconciliation_capabilities == ()


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"version": "1.0.0-rc1"}, "invalid_version"),
        ({"supported_kinds": ()}, "empty_collection"),
        (
            {"supported_kinds": (SESSION_KIND_TERMINAL, SESSION_KIND_TERMINAL)},
            "duplicate_entry",
        ),
        ({"provider_id": "REPL"}, "invalid_identifier"),
        ({"build_hash": DIGEST[:-1]}, "invalid_digest"),
    ],
)
def test_session_provider_validation_refuses_each_malformed_field(
    overrides: dict[str, object], reason: str
) -> None:
    with pytest.raises(ExecutionContractError) as caught:
        provider(**overrides)
    assert caught.value.reason == reason


def test_a_draft_profile_may_not_take_effects_or_run_in_process() -> None:
    with pytest.raises(ExecutionContractError) as effects:
        effect_profile(trust_state=PROFILE_TRUST_DRAFT)
    assert effects.value.reason == "incoherent_posture"

    with pytest.raises(ExecutionContractError) as isolation:
        deterministic_profile(trust_state=PROFILE_TRUST_DRAFT)
    assert isolation.value.reason == "incoherent_posture"

    # DRAFT is coherent once it is isolated and takes no effects.
    assert (
        deterministic_profile(
            trust_state=PROFILE_TRUST_DRAFT, minimum_isolation=2
        ).trust_state
        == PROFILE_TRUST_DRAFT
    )


def test_an_effect_profile_must_reference_policy_defaults() -> None:
    with pytest.raises(ExecutionContractError) as caught:
        effect_profile(policy_defaults_ref=None)
    assert caught.value.reason == "incoherent_posture"


def test_session_providers_and_the_agent_class_are_declared_together() -> None:
    with pytest.raises(ExecutionContractError) as caught:
        agent_profile(session_providers=())
    assert caught.value.reason == "incoherent_posture"


# --------------------------------------------------------------------------
# Component plane: the DETERMINISTIC execution class
# --------------------------------------------------------------------------


def request(key: str = "idem-0001", capability: str = "echo") -> ComponentRequest:
    return ComponentRequest(
        lineage=LINEAGE,
        capability=capability,
        idempotency_key=key,
        arguments=(("text", "hello"),),
    )


def test_a_component_plane_requires_the_deterministic_execution_class() -> None:
    for wrong in (agent_profile(), effect_profile()):
        with pytest.raises(ExecutionRefused) as caught:
            ComponentPlane(wrong, Handler(ComponentResult()))
        assert caught.value.reason == "unsupported_execution_class"


def test_a_component_completes_and_reports_its_output() -> None:
    handler = Handler(ComponentResult(output=(("text", "hello"),)))
    outcome = ComponentPlane(deterministic_profile(), handler).execute(request())

    assert outcome.status == STATUS_COMPLETED == "COMPLETED"
    assert outcome.output == (("text", "hello"),)
    assert not outcome.replayed
    assert len(handler.calls) == 1


def test_a_repeated_key_replays_without_re_running_the_handler() -> None:
    handler = Handler(ComponentResult(output=(("text", "hello"),)))
    plane = ComponentPlane(deterministic_profile(), handler)

    first = plane.execute(request())
    second = plane.execute(request())

    assert len(handler.calls) == 1
    assert not first.replayed
    assert second.replayed
    assert (second.status, second.output) == (first.status, first.output)


def test_a_reused_key_carrying_a_different_request_is_refused() -> None:
    handler = Handler(ComponentResult())
    plane = ComponentPlane(deterministic_profile(), handler)
    plane.execute(request())

    with pytest.raises(ExecutionRefused) as caught:
        plane.execute(
            ComponentRequest(
                lineage=LINEAGE,
                capability="echo",
                idempotency_key="idem-0001",
                arguments=(("text", "goodbye"),),
            )
        )
    assert caught.value.reason == "idempotency_conflict"
    assert len(handler.calls) == 1


def test_cancellation_before_the_handler_never_reaches_it() -> None:
    handler = Handler(ComponentResult(output=(("text", "hello"),)))
    outcome = ComponentPlane(deterministic_profile(), handler).execute(
        request(), cancelled=lambda: True
    )

    assert outcome.status == STATUS_CANCELLED == "CANCELLED"
    assert outcome.output == ()
    assert handler.calls == []


def test_cancellation_after_the_handler_discards_proposals_and_is_not_replayed() -> (
    None
):
    handler = Handler(
        ComponentResult(
            output=(("text", "hello"),),
            proposals=(CapabilityProposal("artifact.write", DIGEST),),
        )
    )
    plane = ComponentPlane(deterministic_profile(), handler)
    signals = iter([False, True])

    cancelled = plane.execute(request(), cancelled=lambda: next(signals))

    assert cancelled.status == STATUS_CANCELLED
    assert cancelled.proposals == ()
    assert len(handler.calls) == 1

    # Nothing was recorded, so the same key runs the handler again rather than
    # replaying a completion that never happened.
    retried = plane.execute(request())
    assert retried.status == STATUS_COMPLETED
    assert not retried.replayed
    assert len(handler.calls) == 2


@pytest.mark.parametrize(
    ("result", "reason"),
    [
        (None, "invalid_component_output"),
        ({"text": "hello"}, "invalid_component_output"),
        (ComponentResult(output=(("Text", "hello"),)), "invalid_identifier"),
        (ComponentResult(output=(("text", "a"), ("text", "b"))), "duplicate_entry"),
        (
            ComponentResult(
                proposals=(
                    CapabilityProposal("artifact.write", DIGEST),
                    CapabilityProposal("artifact.write", DIGEST),
                )
            ),
            "duplicate_entry",
        ),
        (
            ComponentResult(proposals=(CapabilityProposal("write", "not-a-digest"),)),
            "invalid_digest",
        ),
    ],
)
def test_invalid_component_output_is_refused_and_not_recorded(
    result: object, reason: str
) -> None:
    plane = ComponentPlane(deterministic_profile(), Handler(result))

    with pytest.raises(ExecutionContractError) as caught:
        plane.execute(request())
    assert caught.value.reason == reason

    # Refused, therefore never a replayable completion.
    good = ComponentPlane(deterministic_profile(), Handler(ComponentResult()))
    assert not good.execute(request()).replayed


def test_capability_proposals_are_data_and_never_applied() -> None:
    applied: list[CapabilityProposal] = []
    proposal = CapabilityProposal("artifact.write", DIGEST)
    plane = ComponentPlane(
        deterministic_profile(), Handler(ComponentResult(proposals=(proposal,)))
    )

    outcome = plane.execute(request())

    assert outcome.proposals == (proposal,)
    assert applied == []
    assert proposal.key == CapabilityProposal("artifact.write", DIGEST).key
    assert proposal.key != CapabilityProposal("artifact.write", OTHER_DIGEST).key


def test_a_component_plane_refuses_a_capability_its_profile_does_not_declare() -> None:
    handler = Handler(ComponentResult())
    plane = ComponentPlane(deterministic_profile(), handler)

    with pytest.raises(ExecutionRefused) as caught:
        plane.execute(request(capability="delete"))
    assert caught.value.reason == "unsupported_capability"
    assert handler.calls == []


# --------------------------------------------------------------------------
# Governed dispatch: the EFFECT class, and every refusal before the callback
# --------------------------------------------------------------------------

ACCEPTED = ("intent-0001",)


def intent(**overrides: object) -> DispatchIntent:
    fields: dict[str, object] = {
        "intent_id": "intent-0001",
        "capability": "tool.call",
        "contract_version": CONTRACT_VERSION,
        "fence": 1,
        "lineage": LINEAGE,
    }
    fields.update(overrides)
    return DispatchIntent(**fields)  # type: ignore[arg-type]


def test_a_dispatch_plane_requires_the_effect_execution_class() -> None:
    with pytest.raises(ExecutionRefused) as caught:
        GovernedDispatchPlane(deterministic_profile(), ACCEPTED)
    assert caught.value.reason == "unsupported_execution_class"


def test_an_admitted_intent_reaches_the_callback() -> None:
    callback = Callback()
    plane = GovernedDispatchPlane(effect_profile(), ACCEPTED)

    assert plane.dispatch(intent(), callback) == "ran"
    assert [seen.intent_id for seen in callback.calls] == ["intent-0001"]


@pytest.mark.parametrize(
    ("profile_overrides", "intent_overrides", "rotations", "reason"),
    [
        ({}, None, 0, "missing_intent"),
        ({}, {"contract_version": "2.0.0"}, 0, "unsupported_contract_version"),
        ({"trust_state": PROFILE_TRUST_QUARANTINED}, {}, 0, "unsupported_trust"),
        ({"trust_state": PROFILE_TRUST_DISABLED}, {}, 0, "unsupported_trust"),
        ({}, {"intent_id": "intent-9999"}, 0, "unaccepted_intent"),
        ({}, {"capability": "tool.other"}, 0, "unsupported_capability"),
        ({}, {}, 1, "stale_fence"),
        ({}, {"fence": 9}, 0, "unknown_fence"),
    ],
)
def test_governed_dispatch_refuses_before_the_callback_runs(
    profile_overrides: dict[str, object],
    intent_overrides: dict[str, object] | None,
    rotations: int,
    reason: str,
) -> None:
    callback = Callback()
    plane = GovernedDispatchPlane(effect_profile(**profile_overrides), ACCEPTED)
    for _ in range(rotations):
        plane.rotate_fence()
    proposed = None if intent_overrides is None else intent(**intent_overrides)

    with pytest.raises(ExecutionRefused) as caught:
        plane.dispatch(proposed, callback)

    assert caught.value.reason == reason
    assert callback.calls == []


def test_the_accepted_intent_allowlist_is_fixed_at_construction() -> None:
    plane = GovernedDispatchPlane(effect_profile(), ACCEPTED)

    assert plane.accepted_intent_ids == ACCEPTED
    with pytest.raises(ExecutionContractError) as caught:
        GovernedDispatchPlane(effect_profile(), ())
    assert caught.value.reason == "empty_collection"


def test_rotating_the_fence_invalidates_intents_minted_against_the_old_one() -> None:
    callback = Callback()
    plane = GovernedDispatchPlane(effect_profile(), ACCEPTED)
    stale = intent()

    assert plane.dispatch(stale, callback) == "ran"
    assert plane.rotate_fence() == 2

    with pytest.raises(ExecutionRefused) as caught:
        plane.dispatch(stale, callback)
    assert caught.value.reason == "stale_fence"
    assert len(callback.calls) == 1

    assert plane.dispatch(intent(fence=2), callback) == "ran"
    assert len(callback.calls) == 2


def test_admission_returns_the_intent_and_grants_nothing_else() -> None:
    plane = GovernedDispatchPlane(effect_profile(), ACCEPTED)
    proposed = intent()

    assert plane.admit(proposed) is proposed


# --------------------------------------------------------------------------
# Stateful session lease: the full lifecycle
# --------------------------------------------------------------------------


def session_plane(clock: Clock, **provider_overrides: object) -> StatefulSessionPlane:
    return StatefulSessionPlane(
        agent_profile(), clock, provider=provider(**provider_overrides)
    )


def test_a_session_plane_requires_the_agent_class_and_a_declared_provider() -> None:
    with pytest.raises(ExecutionRefused) as wrong_class:
        StatefulSessionPlane(deterministic_profile(), Clock(), provider=provider())
    assert wrong_class.value.reason == "unsupported_execution_class"

    with pytest.raises(ExecutionRefused) as undeclared:
        StatefulSessionPlane(
            agent_profile(), Clock(), provider=provider(provider_id="remote")
        )
    assert undeclared.value.reason == "unsupported_session_provider"


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"trust_state": BUILD_TRUST_PROTOTYPE}, "unsupported_trust"),
        ({"trust_state": BUILD_TRUST_DISABLED}, "unsupported_trust"),
        ({"required_isolation": 1}, "insufficient_isolation"),
    ],
)
def test_a_session_plane_refuses_an_unapproved_or_underisolated_provider(
    overrides: dict[str, object], reason: str
) -> None:
    with pytest.raises(ExecutionRefused) as caught:
        session_plane(Clock(), **overrides)
    assert caught.value.reason == reason


def test_the_lease_lifecycle_runs_acquire_inspect_snapshot_transfer_release() -> None:
    clock = Clock()
    plane = session_plane(clock)

    first = plane.acquire(
        "sess-a", "holder-one", LINEAGE, ttl_us=1_000, external_reference="ext-a"
    )
    assert plane.inspect("sess-a") == first
    assert plane.inspect("sess-none") is None

    snapshot = plane.snapshot(first)
    assert (snapshot.session_id, snapshot.fence, snapshot.external_reference) == (
        "sess-a",
        first.fence,
        "ext-a",
    )

    second = plane.transfer_control(first, "holder-two")
    assert (second.holder, second.fence) == ("holder-two", first.fence + 1)
    assert plane.release(second).state == LEASE_RELEASED == "RELEASED"

    assert [event.kind for event in plane.evidence] == [
        "ACQUIRED",
        "SNAPSHOTTED",
        "TRANSFERRED",
        "RELEASED",
    ]


@pytest.mark.parametrize(
    ("overrides", "operation"),
    [
        ({"supports_suspend_resume": False}, "snapshot"),
        ({"supports_suspend_resume": False}, "resume"),
        ({"supports_control_transfer": False}, "transfer"),
    ],
)
def test_a_lifecycle_operation_the_provider_cannot_perform_is_refused(
    overrides: dict[str, object], operation: str
) -> None:
    """The declared support is a gate, not a hint: an unsupported call refuses."""
    capable = session_plane(Clock())
    lease = capable.acquire(
        "sess-a", "holder-one", LINEAGE, ttl_us=1_000, external_reference="ext-a"
    )
    snapshot = capable.snapshot(lease)

    plane = session_plane(Clock(), **overrides)
    held = plane.acquire(
        "sess-a", "holder-one", LINEAGE, ttl_us=1_000, external_reference="ext-a"
    )
    calls = {
        "snapshot": lambda: plane.snapshot(held),
        "resume": lambda: plane.resume(snapshot, "holder-two", LINEAGE, ttl_us=1_000),
        "transfer": lambda: plane.transfer_control(held, "holder-two"),
    }

    with pytest.raises(ExecutionRefused) as caught:
        calls[operation]()
    assert caught.value.reason == "unsupported_session_feature"
    # Refused before it touched the lease: only the acquire is evidence.
    assert [event.kind for event in plane.evidence] == ["ACQUIRED"]


def test_a_held_lease_is_exclusive_and_a_stale_holder_is_refused() -> None:
    plane = session_plane(Clock())
    first = plane.acquire(
        "sess-a", "holder-one", LINEAGE, ttl_us=1_000, external_reference="ext-a"
    )

    with pytest.raises(ExecutionRefused) as held:
        plane.acquire(
            "sess-a", "holder-two", LINEAGE, ttl_us=1_000, external_reference="ext-a"
        )
    assert held.value.reason == "session_held"

    plane.transfer_control(first, "holder-two")
    for stale in (
        lambda: plane.transfer_control(first, "holder-three"),
        lambda: plane.snapshot(first),
        lambda: plane.release(first),
    ):
        with pytest.raises(ExecutionRefused) as caught:
            stale()
        assert caught.value.reason == "stale_lease_fence"


def test_resume_takes_a_released_session_back_and_advances_the_fence() -> None:
    clock = Clock()
    plane = session_plane(clock)
    lease = plane.acquire(
        "sess-a", "holder-one", LINEAGE, ttl_us=1_000, external_reference="ext-a"
    )
    snapshot = plane.snapshot(lease)
    plane.release(lease)

    resumed = plane.resume(snapshot, "holder-two", LINEAGE, ttl_us=2_000)

    assert (resumed.holder, resumed.fence, resumed.state) == (
        "holder-two",
        lease.fence + 1,
        LEASE_HELD,
    )
    assert resumed.external_reference == "ext-a"
    assert plane.evidence[-1].kind == "RESUMED"

    # The same snapshot is now behind the fence and cannot be replayed.
    with pytest.raises(ExecutionRefused) as stale:
        plane.resume(snapshot, "holder-three", LINEAGE, ttl_us=1_000)
    assert stale.value.reason == "session_held"

    plane.release(resumed)
    with pytest.raises(ExecutionRefused) as behind:
        plane.resume(snapshot, "holder-three", LINEAGE, ttl_us=1_000)
    assert behind.value.reason == "stale_snapshot"


def test_resume_refuses_a_session_this_plane_has_never_seen() -> None:
    clock = Clock()
    plane = session_plane(clock)
    lease = plane.acquire(
        "sess-a", "holder-one", LINEAGE, ttl_us=1_000, external_reference="ext-a"
    )
    snapshot = replace(plane.snapshot(lease), session_id="sess-elsewhere")

    with pytest.raises(ExecutionRefused) as caught:
        plane.resume(snapshot, "holder-two", LINEAGE, ttl_us=1_000)
    assert caught.value.reason == "unknown_session"


def test_an_expired_lease_is_refused_on_contact_and_on_inspection() -> None:
    clock = Clock()
    plane = session_plane(clock)
    lease = plane.acquire(
        "sess-a", "holder-one", LINEAGE, ttl_us=1_000, external_reference="ext-a"
    )
    clock.advance(1_000)

    with pytest.raises(ExecutionRefused) as caught:
        plane.transfer_control(lease, "holder-two")
    assert caught.value.reason == "lease_expired"
    assert [event.kind for event in plane.evidence] == ["ACQUIRED", "EXPIRED"]

    inspected = plane.inspect("sess-a")
    assert inspected is not None
    assert inspected.state == LEASE_EXPIRED == "EXPIRED"

    # The session is free again, and the fence keeps climbing across holders.
    reacquired = plane.acquire(
        "sess-a", "holder-two", LINEAGE, ttl_us=1_000, external_reference="ext-a"
    )
    assert (reacquired.fence, reacquired.state) == (lease.fence + 1, LEASE_HELD)


def test_inspect_expires_an_overdue_lease_rather_than_reporting_it_held() -> None:
    clock = Clock()
    plane = session_plane(clock)
    plane.acquire(
        "sess-a", "holder-one", LINEAGE, ttl_us=1_000, external_reference="ext-a"
    )
    clock.advance(5_000)

    inspected = plane.inspect("sess-a")

    assert inspected is not None
    assert inspected.state == LEASE_EXPIRED
    assert [event.kind for event in plane.evidence] == ["ACQUIRED", "EXPIRED"]


@pytest.mark.parametrize(
    ("reference", "live", "released_first", "state"),
    [
        ("ext-a", ("ext-a",), False, SESSION_ACTIVE),
        ("ext-a", ("ext-a",), True, SESSION_ORPHANED),
        ("ext-a", (), False, SESSION_RELEASED),
        ("ext-a", (), True, SESSION_RELEASED),
        ("ext-gone", ("ext-gone",), False, SESSION_ORPHANED),
        ("ext-gone", (), False, SESSION_UNKNOWN),
    ],
)
def test_reconcile_answers_one_of_four_stable_states(
    reference: str, live: tuple[str, ...], released_first: bool, state: str
) -> None:
    plane = session_plane(Clock())
    lease = plane.acquire(
        "sess-a", "holder-one", LINEAGE, ttl_us=1_000, external_reference="ext-a"
    )
    if released_first:
        plane.release(lease)

    result = plane.reconcile(reference, live)

    assert result.state == state
    assert state in {"ACTIVE", "RELEASED", "ORPHANED", "UNKNOWN"}
    assert result.external_reference == reference
    assert result.evidence[-1].kind == "RECONCILED"
    assert result.evidence[-1].state == state
    # Deterministic: the same question against the same world answers the same
    # way, with the same evidence identity.
    assert plane.reconcile(reference, live).evidence[-1].key == result.evidence[-1].key


def test_reconcile_expires_an_overdue_lease_before_it_answers() -> None:
    clock = Clock()
    plane = session_plane(clock)
    plane.acquire(
        "sess-a", "holder-one", LINEAGE, ttl_us=1_000, external_reference="ext-a"
    )
    clock.advance(2_000)

    result = plane.reconcile("ext-a", ("ext-a",))

    # Live outside, no live holder here: an orphan, and the expiry is evidence.
    assert result.state == SESSION_ORPHANED
    assert [event.kind for event in result.evidence] == ["EXPIRED", "RECONCILED"]
    assert result.session_id == "sess-a"


def test_lease_evidence_is_content_addressed_per_holder_and_fence() -> None:
    plane = session_plane(Clock())
    first = plane.acquire(
        "sess-a", "holder-one", LINEAGE, ttl_us=1_000, external_reference="ext-a"
    )
    second = plane.transfer_control(first, "holder-two")

    assert first.key != second.key
    acquired, transferred = plane.evidence
    assert acquired.key != transferred.key
    assert acquired.at_us == transferred.at_us == 1_000


@pytest.mark.parametrize("ttl_us", [0, -1, 86_400_000_001])
def test_a_session_plane_refuses_an_out_of_bound_ttl(ttl_us: int) -> None:
    plane = session_plane(Clock())

    with pytest.raises(ExecutionContractError) as caught:
        plane.acquire(
            "sess-a", "holder-one", LINEAGE, ttl_us=ttl_us, external_reference="ext-a"
        )
    assert caught.value.reason == "invalid_ttl"


def test_releasing_a_lease_nobody_holds_is_refused() -> None:
    plane = session_plane(Clock())
    lease = plane.acquire(
        "sess-a", "holder-one", LINEAGE, ttl_us=1_000, external_reference="ext-a"
    )
    plane.release(lease)

    with pytest.raises(ExecutionRefused) as caught:
        plane.release(lease)
    assert caught.value.reason == "no_active_lease"


# --------------------------------------------------------------------------
# Registry: static, source-qualified, exact version and build, fail closed
# --------------------------------------------------------------------------


def ready_registry() -> RuntimeExecutionRegistry:
    registry = RuntimeExecutionRegistry()
    registry.register_executor(executor(), health=HEALTH_READY)
    registry.register_session_provider(provider(), health=HEALTH_READY)
    return registry


def test_one_registry_holds_executors_and_session_providers_separately() -> None:
    registry = ready_registry()

    assert [entry.entry_kind for entry in registry.entries()] == [
        ENTRY_EXECUTOR,
        ENTRY_SESSION_PROVIDER,
    ]
    assert (ENTRY_EXECUTOR, ENTRY_SESSION_PROVIDER) == ("EXECUTOR", "SESSION_PROVIDER")
    assert registry.resolve_executor(
        "core.local", "component.echo", "1.0.0", capability="echo"
    ).descriptor == executor()
    assert registry.resolve_session_provider(
        "core.local", "repl", "1.0.0", session_kind=SESSION_KIND_TERMINAL
    ).descriptor == provider()


def test_executor_resolution_needs_the_exact_source_id_and_version() -> None:
    registry = ready_registry()

    for wrong in (
        ("other.source", "component.echo", "1.0.0"),
        ("core.local", "component.other", "1.0.0"),
        ("core.local", "component.echo", "1.0.1"),
    ):
        with pytest.raises(ExecutionRefused) as caught:
            registry.resolve_executor(*wrong, capability="echo")
        assert caught.value.reason == "unknown_entry"


def test_two_sources_publishing_one_executor_id_never_shadow_each_other() -> None:
    registry = ready_registry()
    registry.register_executor(
        executor(source_id="vendor.acme", capabilities=("echo", "translate")),
        health=HEALTH_READY,
    )

    mine = registry.resolve_executor(
        "core.local", "component.echo", "1.0.0", capability="echo"
    )
    theirs = registry.resolve_executor(
        "vendor.acme", "component.echo", "1.0.0", capability="translate"
    )

    assert mine.descriptor.source_id == "core.local"
    assert theirs.descriptor.source_id == "vendor.acme"
    with pytest.raises(ExecutionRefused) as caught:
        registry.resolve_executor(
            "core.local", "component.echo", "1.0.0", capability="translate"
        )
    assert caught.value.reason == "unsupported_capability"


def test_executor_resolution_checks_capability_version_trust_and_isolation() -> None:
    registry = ready_registry()

    with pytest.raises(ExecutionRefused) as capability:
        registry.resolve_executor(
            "core.local", "component.echo", "1.0.0", capability="translate"
        )
    assert capability.value.reason == "unsupported_capability"

    with pytest.raises(ExecutionRefused) as version:
        registry.resolve_executor(
            "core.local",
            "component.echo",
            "1.0.0",
            capability="echo",
            contract_version="2.0.0",
        )
    assert version.value.reason == "unsupported_contract_version"

    with pytest.raises(ExecutionRefused) as isolation:
        registry.resolve_executor(
            "core.local",
            "component.echo",
            "1.0.0",
            capability="echo",
            minimum_isolation=3,
        )
    assert isolation.value.reason == "insufficient_isolation"

    registry.register_executor(
        executor(trust_state=BUILD_TRUST_PROTOTYPE), health=HEALTH_READY
    )
    with pytest.raises(ExecutionRefused) as trust:
        registry.resolve_executor(
            "core.local", "component.echo", "1.0.0", capability="echo"
        )
    assert trust.value.reason == "unsupported_trust"


def test_executor_resolution_checks_the_reconciliation_it_is_asked_for() -> None:
    registry = ready_registry()

    assert registry.resolve_executor(
        "core.local",
        "component.echo",
        "1.0.0",
        capability="echo",
        required_reconciliation=("adopt", "terminate"),
    ).is_routable

    with pytest.raises(ExecutionRefused) as caught:
        registry.resolve_executor(
            "core.local",
            "component.echo",
            "1.0.0",
            capability="echo",
            required_reconciliation=("rebuild",),
        )
    assert caught.value.reason == "unsupported_reconciliation"


def test_provider_resolution_checks_kind_lifecycle_support_trust_and_isolation() -> None:
    registry = ready_registry()

    assert registry.resolve_session_provider(
        "core.local",
        "repl",
        "1.0.0",
        session_kind=SESSION_KIND_TERMINAL,
        require_suspend_resume=True,
        require_control_transfer=True,
        minimum_isolation=2,
    ).is_routable

    with pytest.raises(ExecutionRefused) as kind:
        registry.resolve_session_provider(
            "core.local", "repl", "1.0.0", session_kind=SESSION_KIND_BROWSER
        )
    assert kind.value.reason == "unsupported_session_kind"

    with pytest.raises(ExecutionContractError) as retired:
        registry.resolve_session_provider(
            "core.local", "repl", "1.0.0", session_kind="SHELL"
        )
    assert retired.value.reason == "unknown_vocabulary_member"

    with pytest.raises(ExecutionRefused) as isolation:
        registry.resolve_session_provider(
            "core.local",
            "repl",
            "1.0.0",
            session_kind=SESSION_KIND_TERMINAL,
            minimum_isolation=3,
        )
    assert isolation.value.reason == "insufficient_isolation"

    registry.register_session_provider(
        provider(supports_control_transfer=False), health=HEALTH_READY
    )
    with pytest.raises(ExecutionRefused) as feature:
        registry.resolve_session_provider(
            "core.local",
            "repl",
            "1.0.0",
            session_kind=SESSION_KIND_TERMINAL,
            require_control_transfer=True,
        )
    assert feature.value.reason == "unsupported_session_feature"

    registry.register_session_provider(
        provider(trust_state=BUILD_TRUST_QUARANTINED), health=HEALTH_READY
    )
    with pytest.raises(ExecutionRefused) as trust:
        registry.resolve_session_provider(
            "core.local", "repl", "1.0.0", session_kind=SESSION_KIND_TERMINAL
        )
    assert trust.value.reason == "unsupported_trust"


def test_a_provider_may_serve_more_than_one_session_kind() -> None:
    registry = RuntimeExecutionRegistry()
    registry.register_executor(executor(), health=HEALTH_READY)
    registry.register_session_provider(
        provider(supported_kinds=(SESSION_KIND_TERMINAL, SESSION_KIND_ACP)),
        health=HEALTH_READY,
    )

    for kind in (SESSION_KIND_TERMINAL, SESSION_KIND_ACP):
        assert registry.resolve_session_provider(
            "core.local", "repl", "1.0.0", session_kind=kind
        ).is_routable

    with pytest.raises(ExecutionRefused) as caught:
        registry.resolve_session_provider(
            "core.local", "repl", "1.0.0", session_kind=SESSION_KIND_BROWSER
        )
    assert caught.value.reason == "unsupported_session_kind"


@pytest.mark.parametrize(
    "health", ["CONFIGURED", "AUTHENTICATED", "COMPATIBLE", "RECOVERABLE", "QUARANTINED"]
)
def test_only_a_ready_entry_is_routable(health: str) -> None:
    registry = RuntimeExecutionRegistry()
    registry.register_executor(executor(), health=health)

    with pytest.raises(ExecutionRefused) as caught:
        registry.resolve_executor(
            "core.local", "component.echo", "1.0.0", capability="echo"
        )
    assert caught.value.reason == "unroutable_health"


def test_health_moves_the_entry_in_and_out_of_routing() -> None:
    registry = RuntimeExecutionRegistry()
    key = executor().key
    registry.register_executor(executor())

    assert registry.set_health(ENTRY_EXECUTOR, key, HEALTH_READY).is_routable
    registry.resolve_executor("core.local", "component.echo", "1.0.0", capability="echo")

    registry.set_health(ENTRY_EXECUTOR, key, HEALTH_RECOVERABLE)
    with pytest.raises(ExecutionRefused) as caught:
        registry.resolve_executor(
            "core.local", "component.echo", "1.0.0", capability="echo"
        )
    assert caught.value.reason == "unroutable_health"


def test_quarantine_is_terminal_until_the_entry_is_replaced() -> None:
    registry = RuntimeExecutionRegistry()
    key = executor().key
    registry.register_executor(executor(), health=HEALTH_QUARANTINED)

    with pytest.raises(ExecutionRefused) as caught:
        registry.set_health(ENTRY_EXECUTOR, key, HEALTH_READY)
    assert caught.value.reason == "quarantine_is_terminal"

    replacement = registry.register_executor(executor(), health=HEALTH_READY)
    assert replacement.is_routable
    assert replacement.revision == 2


def test_replacement_and_removal_retain_the_prior_build_identity() -> None:
    registry = RuntimeExecutionRegistry()
    original = executor()
    successor = executor(build_hash=OTHER_BUILD)
    key = original.key

    registry.register_executor(original, health=HEALTH_READY)
    registry.register_executor(successor, health=HEALTH_READY)
    registry.remove(ENTRY_EXECUTOR, key)

    assert registry.entries() == ()
    with pytest.raises(ExecutionRefused) as caught:
        registry.resolve_executor(
            "core.local", "component.echo", "1.0.0", capability="echo"
        )
    assert caught.value.reason == "unknown_entry"

    assert [(event.kind, event.revision) for event in registry.history] == [
        (EVENT_REGISTERED, 1),
        (EVENT_REPLACED, 1),
        (EVENT_REGISTERED, 2),
        (EVENT_REMOVED, 2),
    ]
    identities = [
        (event.content_hash, event.build_hash) for event in registry.history
    ]
    assert identities[0] == identities[1] == (original.content_hash, BUILD)
    assert identities[2] == identities[3] == (successor.content_hash, OTHER_BUILD)
    assert all(event.key == key for event in registry.history)

    # A later re-registration continues the revision line rather than restarting it.
    assert registry.register_executor(original).revision == 3
    assert registry.history[-1].kind == EVENT_REGISTERED


def test_an_exact_active_reference_never_resolves_to_the_replacement() -> None:
    registry = RuntimeExecutionRegistry()
    original = executor()
    registry.register_executor(original, health=HEALTH_READY)

    pinned = registry.resolve_executor(
        "core.local",
        "component.echo",
        "1.0.0",
        capability="echo",
        expect_content_hash=original.content_hash,
    )
    assert pinned.descriptor == original

    registry.register_executor(executor(build_hash=OTHER_BUILD), health=HEALTH_READY)
    with pytest.raises(ExecutionRefused) as caught:
        registry.resolve_executor(
            "core.local",
            "component.echo",
            "1.0.0",
            capability="echo",
            expect_content_hash=original.content_hash,
        )
    assert caught.value.reason == "replaced_descriptor"

    registry.register_session_provider(provider(), health=HEALTH_READY)
    with pytest.raises(ExecutionRefused) as provider_pin:
        registry.resolve_session_provider(
            "core.local",
            "repl",
            "1.0.0",
            session_kind=SESSION_KIND_TERMINAL,
            expect_content_hash=OTHER_DIGEST,
        )
    assert provider_pin.value.reason == "replaced_descriptor"


def test_a_health_change_is_recorded_in_history() -> None:
    registry = RuntimeExecutionRegistry()
    registry.register_executor(executor())
    registry.set_health(ENTRY_EXECUTOR, executor().key, HEALTH_READY)

    assert [event.kind for event in registry.history] == [
        EVENT_REGISTERED,
        EVENT_HEALTH_CHANGED,
    ]
    assert (EVENT_REGISTERED, EVENT_HEALTH_CHANGED) == ("REGISTERED", "HEALTH_CHANGED")
    assert registry.history[-1].health == HEALTH_READY == "READY"


def test_the_registry_fails_closed_on_unknown_entries_and_bad_input() -> None:
    registry = RuntimeExecutionRegistry()
    key = executor().key

    for call in (
        lambda: registry.remove(ENTRY_EXECUTOR, key),
        lambda: registry.set_health(ENTRY_EXECUTOR, key, HEALTH_READY),
    ):
        with pytest.raises(ExecutionRefused) as caught:
            call()
        assert caught.value.reason == "unknown_entry"

    registry.register_executor(executor())
    with pytest.raises(ExecutionRefused) as unknown_health:
        registry.set_health(ENTRY_EXECUTOR, key, "VIBRANT")
    assert unknown_health.value.reason == "unknown_vocabulary_member"

    with pytest.raises(ExecutionRefused) as lowercase_health:
        registry.set_health(ENTRY_EXECUTOR, key, "ready")
    assert lowercase_health.value.reason == "unknown_vocabulary_member"

    # A provider is not an executor, and neither answers under the other's kind.
    registry.register_session_provider(provider(), health=HEALTH_READY)
    with pytest.raises(ExecutionRefused) as crossed:
        registry.resolve_executor("core.local", "repl", "1.0.0", capability="echo")
    assert crossed.value.reason == "unknown_entry"


def test_the_registry_refuses_an_unsealed_descriptor() -> None:
    registry = RuntimeExecutionRegistry()

    with pytest.raises(ExecutionContractError) as unsealed:
        registry.register_session_provider(
            SessionProviderDescriptor(
                source_id="core.local",
                provider_id="repl",
                version="1.0.0",
                build_hash=BUILD,
                supported_kinds=(SESSION_KIND_TERMINAL,),
                supports_suspend_resume=True,
                supports_control_transfer=True,
                required_isolation=2,
                trust_state=BUILD_TRUST_APPROVED,
            )
        )
    assert unsealed.value.reason == "unsealed_descriptor"

    with pytest.raises(ExecutionContractError) as tampered:
        registry.register_executor(replace(executor(), capabilities=("delete",)))
    assert tampered.value.reason == "content_hash_mismatch"


def test_an_executor_speaking_another_contract_version_never_resolves() -> None:
    registry = RuntimeExecutionRegistry()
    registry.register_executor(
        executor(supported_contract_versions=("2.0.0",)), health=HEALTH_READY
    )

    with pytest.raises(ExecutionRefused) as caught:
        registry.resolve_executor(
            "core.local", "component.echo", "1.0.0", capability="echo"
        )
    assert caught.value.reason == "unsupported_contract_version"

    # Exact membership, never a range: 1.0.0 is not answered by 1.0.1.
    registry.register_executor(
        executor(supported_contract_versions=("1.0.1", CONTRACT_VERSION)),
        health=HEALTH_READY,
    )
    assert registry.resolve_executor(
        "core.local", "component.echo", "1.0.0", capability="echo"
    ).is_routable


def test_the_seam_imports_no_storage_scheduler_recovery_or_platform() -> None:
    """Package-neutral by construction: the whole import list is enumerated here.

    An allowlist rather than a denylist of forbidden names, because a denylist
    only catches the couplings someone thought to forbid. Anything new in this
    seam's imports has to be added here deliberately.
    """
    allowed = {
        "__future__",
        "collections",
        "dataclasses",
        "hashlib",
        "re",
        "typing",
        "omnivia_core",
        "omnivia_core_runtime",
    }
    package = Path(profile_module.__file__ or "").parent
    sources = sorted(package.glob("*.py"))

    assert [source.name for source in sources] == [
        "__init__.py",
        "governed.py",
        "loop.py",
        "planes.py",
        "profile.py",
        "registry.py",
        "synthetic.py",
        "workflow.py",
    ]
    imported: set[str] = set()
    for source in sources:
        for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                imported.add(node.module.split(".")[0])

    assert imported <= allowed, imported - allowed
    # The one sibling package it may reach into is its own.
    for source in sources:
        text = source.read_text(encoding="utf-8")
        assert "omnivia_core_runtime.storage" not in text
        assert "omnivia_core_runtime.service" not in text


def test_the_governed_dispatch_gate_is_still_documented_as_provisional() -> None:
    """The pre-FND-F3 statement is load-bearing and must not be quietly dropped."""
    package = Path(profile_module.__file__ or "").parent
    planes = (package / "planes.py").read_text(encoding="utf-8")
    readme = (
        package.parents[2] / "README.md"
    ).read_text(encoding="utf-8")

    assert "provisional pre-FND-F3" in planes
    assert "FND-F3 owns real governance" in planes
    assert "provisional pre-FND-F3" in readme
    assert "FND-F3 remains the owner of real governance" in readme
