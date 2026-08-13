"""V06-8 packet A9-P1: the agent-host SPI's hook vocabulary and validated DTOs.

These tests are about the contract side only -- what a wrapper, an outcome and
a composed envelope are allowed to be -- and they hold nothing about what a
provider decides, which is `test_agent_host_mock.py`'s job.

They are deliberately independent of the fixture corpus. A test that reads the
corpus checks that the code agrees with a file; these check that the code
refuses a value, which is the thing that has to stay true whether or not the
corpus is loaded and whichever way the later conformance module reads it.

The tests that matter most here are the *partition* ones. `turn_ordinal` is a
coordinate and never an authorization input, and the only thing making that
structural rather than remembered is that a turn-scoped hook cannot be built
without one and a run-level hook cannot be built with one. Both directions are
asserted, because enforcing one direction alone leaves a run-level hook able to
carry a turn position it does not have.
"""

from __future__ import annotations

from dataclasses import fields, replace

import pytest

from omnivia_core.agent_host.spi import (
    CORE_JOB_CONTROL_OPERATIONS,
    EFFECTING_HOOKS,
    HOOK_COMPOSITIONS,
    HOST_IDENTITY_FIELD_NAMES,
    MAX_CAPABILITIES,
    MAX_DEADLINE_MS,
    MAX_LABEL_LENGTH,
    MAX_SEQUENCE,
    READ_ONLY_HOOKS,
    RUN_LEVEL_HOOKS,
    SPI_VERSION,
    SPI_VERSION_MAXIMUM,
    SPI_VERSION_MINIMUM,
    TURN_CONTROL_HOOKS,
    TURN_SCOPED_HOOKS,
    ApprovalKind,
    Disposition,
    Hook,
    HookIntent,
    HookOutcome,
    Reason,
    SpiContractError,
    SpiProvenance,
    SpiRequest,
    VersionAxes,
    build_nested_envelope,
    build_response,
    build_version_envelope,
    capability_token,
    envelope_carries_host_identity,
    envelope_leaks_provenance,
    frozen_retry_class,
    parse_capability_token,
    resolve_effective_capabilities,
)
from omnivia_core.contracts.v1.generated import (
    COMPATIBILITY_STATUS_COMPATIBLE,
    ERROR_CODE_CONFLICT,
    ERROR_CODE_DEADLINE_EXCEEDED,
    RETRY_CLASS_NON_RETRYABLE,
    UPGRADE_STATE_REQUIRED,
    ApiError,
    CapabilityRef,
    CapabilitySet,
    ClientIdentity,
    GrantedAuthority,
    PrincipalClaim,
    VersionWindow,
)

#: The ten hook names, spelled out rather than derived from the enum. Deriving
#: them would make the test agree with whatever the enum says, including a
#: rename or an eleventh member, which is the one thing SPI-R-002 forbids.
TEN_HOOK_NAMES = frozenset(
    {
        "spi.negotiate",
        "recall.before_turn",
        "memory.search",
        "capture.after_turn",
        "tool_result.persist",
        "context.compact",
        "approval.request",
        "turn.complete",
        "turn.cancel",
        "turn.retry",
    }
)


def provenance(**overrides: object) -> SpiProvenance:
    base: dict[str, object] = {
        "agent": "agent-1",
        "session": "session-1",
        "run": "run-1",
        "sequence": 1,
    }
    base.update(overrides)
    return SpiProvenance(**base)  # type: ignore[arg-type]


def request(hook: Hook, **overrides: object) -> SpiRequest:
    """A minimally well-formed wrapper for `hook`, with the turn coordinate right."""
    turn = 1 if hook in TURN_SCOPED_HOOKS else None
    base: dict[str, object] = {
        "hook": hook,
        "caller": "caller-1",
        "workspace": "workspace-1",
        "purpose": "context_recall",
        "provenance": provenance(turn_ordinal=turn),
        "deadline_ms": 1_000,
    }
    base.update(overrides)
    return SpiRequest(**base)  # type: ignore[arg-type]


# --- the ten hooks and their partitions --------------------------------------


def test_the_hook_set_is_exactly_the_ten_named_hooks() -> None:
    assert {hook.value for hook in Hook} == TEN_HOOK_NAMES


def test_turn_scope_partition_is_exhaustive_and_disjoint() -> None:
    assert TURN_SCOPED_HOOKS | RUN_LEVEL_HOOKS == set(Hook)
    assert not TURN_SCOPED_HOOKS & RUN_LEVEL_HOOKS


def test_every_hook_declares_its_compositions() -> None:
    assert set(HOOK_COMPOSITIONS) == set(Hook)


def test_no_hook_composes_core_job_control() -> None:
    composed = {op for ops in HOOK_COMPOSITIONS.values() for op in ops}
    assert not composed & CORE_JOB_CONTROL_OPERATIONS


def test_turn_control_and_compaction_compose_nothing() -> None:
    for hook in TURN_CONTROL_HOOKS | {Hook.CONTEXT_COMPACT, Hook.APPROVAL_REQUEST}:
        assert HOOK_COMPOSITIONS[hook] == ()


def test_effecting_hooks_are_the_ones_that_create_and_no_others() -> None:
    creating = {
        hook for hook, ops in HOOK_COMPOSITIONS.items() if "memory.create" in ops
    }
    assert EFFECTING_HOOKS == creating


def test_read_only_hooks_compose_only_searching_and_packing() -> None:
    for hook in READ_ONLY_HOOKS:
        assert all(
            op.endswith(".search") or op == "context_pack.build"
            for op in HOOK_COMPOSITIONS[hook]
        )


def test_spi_version_sits_inside_its_own_accepted_window() -> None:
    assert SPI_VERSION_MINIMUM <= SPI_VERSION <= SPI_VERSION_MAXIMUM


# --- the turn coordinate partition, both directions --------------------------


@pytest.mark.parametrize("hook", sorted(TURN_SCOPED_HOOKS, key=lambda h: h.value))
def test_turn_scoped_hook_without_a_turn_ordinal_is_refused(hook: Hook) -> None:
    with pytest.raises(SpiContractError, match="turn-scoped"):
        request(hook, provenance=provenance(turn_ordinal=None))


@pytest.mark.parametrize("hook", sorted(RUN_LEVEL_HOOKS, key=lambda h: h.value))
def test_run_level_hook_carrying_a_turn_ordinal_is_refused(hook: Hook) -> None:
    with pytest.raises(SpiContractError, match="run-level"):
        request(hook, provenance=provenance(turn_ordinal=0))


def test_turn_ordinal_zero_is_a_position_not_an_absence() -> None:
    """A falsy coordinate must not read as a missing one."""
    call = request(Hook.TURN_COMPLETE, provenance=provenance(turn_ordinal=0))
    assert call.turn_ordinal == 0


def test_turn_ordinal_reads_through_from_provenance() -> None:
    assert request(Hook.MEMORY_SEARCH).turn_ordinal == 1


# --- scalar validation --------------------------------------------------------


@pytest.mark.parametrize("value", ["", "x" * (MAX_LABEL_LENGTH + 1), "bad\nlabel", 7, None])
def test_labels_outside_the_accepted_domain_are_refused(value: object) -> None:
    with pytest.raises(SpiContractError):
        request(Hook.MEMORY_SEARCH, caller=value)


def test_a_label_at_the_bound_is_accepted() -> None:
    assert request(Hook.MEMORY_SEARCH, caller="x" * MAX_LABEL_LENGTH).caller


@pytest.mark.parametrize("value", [-1, MAX_SEQUENCE + 1, "1", 1.0, None])
def test_sequences_outside_the_accepted_domain_are_refused(value: object) -> None:
    with pytest.raises(SpiContractError):
        provenance(sequence=value)


def test_a_boolean_sequence_is_refused_rather_than_read_as_an_integer() -> None:
    """`bool` is an `int` in Python; a wrapper that coerced it would accept `True` as 1."""
    with pytest.raises(SpiContractError, match="sequence must be an integer"):
        provenance(sequence=True)


def test_a_deadline_past_the_bound_is_refused() -> None:
    with pytest.raises(SpiContractError):
        request(Hook.MEMORY_SEARCH, deadline_ms=MAX_DEADLINE_MS + 1)


def test_a_missing_deadline_builds_and_is_left_for_the_provider_to_refuse() -> None:
    """SPI-R-017: an omitted deadline has to reach the provider, not fail here."""
    assert request(Hook.MEMORY_SEARCH, deadline_ms=None).deadline_ms is None


# --- capability spelling ------------------------------------------------------


@pytest.mark.parametrize(
    "token",
    [
        "memory.read",
        "memory.read@1",
        "memory.read@1.0.0",
        "Memory.Read@1.0",
        "memory@1.0",
        "memory.read@01.0",
        "",
    ],
)
def test_a_capability_outside_the_frozen_spelling_is_refused(token: str) -> None:
    with pytest.raises(SpiContractError):
        parse_capability_token(token)


def test_capability_tokens_round_trip_through_the_frozen_reference_type() -> None:
    ref = parse_capability_token("knowledge.govern@1.0")
    assert ref == CapabilityRef(id="knowledge.govern", version="1.0")
    assert capability_token(ref) == "knowledge.govern@1.0"


def test_a_repeated_capability_is_refused() -> None:
    with pytest.raises(SpiContractError, match="must not repeat"):
        request(
            Hook.MEMORY_SEARCH,
            granted_capabilities=("memory.read@1.0", "memory.read@1.0"),
        )


def test_too_many_capabilities_are_refused() -> None:
    tokens = tuple(f"memory.read{index}@1.0" for index in range(MAX_CAPABILITIES + 1))
    with pytest.raises(SpiContractError, match="too many"):
        request(Hook.MEMORY_SEARCH, granted_capabilities=tokens)


def test_effective_capabilities_are_the_intersection_and_never_more() -> None:
    effective = resolve_effective_capabilities(
        ("memory.read@1.0", "memory.write@1.0"),
        ("memory.read@1.0", "knowledge.govern@1.0"),
    )
    assert effective == ("memory.read@1.0",)


# --- field placement ----------------------------------------------------------


def test_approval_kind_belongs_to_approval_request_only() -> None:
    with pytest.raises(SpiContractError, match="approval.request only"):
        request(Hook.TURN_COMPLETE, approval_kind=ApprovalKind.RUNTIME_TOOL_APPROVAL)
    assert (
        request(
            Hook.APPROVAL_REQUEST,
            approval_kind=ApprovalKind.CORE_GOVERNANCE_ESCALATION,
        ).approval_kind
        is ApprovalKind.CORE_GOVERNANCE_ESCALATION
    )


def test_declared_spi_version_belongs_to_negotiation_only() -> None:
    with pytest.raises(SpiContractError, match="spi.negotiate only"):
        request(Hook.MEMORY_SEARCH, declared_spi_version="1.0.0")


def test_a_declared_version_that_is_not_semantic_is_refused() -> None:
    with pytest.raises(SpiContractError, match="semantic version"):
        request(Hook.NEGOTIATE, provenance=provenance(), declared_spi_version="1.0")


def test_the_default_intent_declares_nothing() -> None:
    intent = HookIntent()
    flags = [
        getattr(intent, f.name) for f in fields(intent) if isinstance(getattr(intent, f.name), bool)
    ]
    assert flags and not any(flags)
    assert intent.inline_payload_bytes == 0


def test_with_sequence_moves_the_call_and_changes_nothing_else() -> None:
    original = request(Hook.MEMORY_SEARCH)
    moved = original.with_sequence(9)
    assert moved.provenance.sequence == 9
    assert moved.provenance.turn_ordinal == original.provenance.turn_ordinal
    assert replace(moved, provenance=original.provenance) == original


# --- the outcome algebra ------------------------------------------------------


def outcome(**overrides: object) -> HookOutcome:
    base: dict[str, object] = {
        "hook": Hook.MEMORY_SEARCH,
        "disposition": Disposition.ACCEPTED,
        "reason": Reason.ACCEPTED,
        "audit_record_required": True,
        "response": response(),
    }
    base.update(overrides)
    return HookOutcome(**base)  # type: ignore[arg-type]


def response(*, error: ApiError | None = None) -> object:
    return build_response(
        request_id="spi-memory-search-1",
        version=version_envelope(),
        authority=GrantedAuthority(principal_id="caller-1", roles=(), capabilities=()),
        audit_reference=None,
        result=None if error is not None else {},
        error=error,
    )


def version_envelope() -> object:
    return build_version_envelope(
        VersionAxes(spi="1.0.0", api="1.0", server="0.6.8", workspace_format="1.0", client="0.6.8"),
        status=COMPATIBILITY_STATUS_COMPATIBLE,
        upgrade_state=UPGRADE_STATE_REQUIRED,
        supported_api=VersionWindow(minimum="1.0", maximum="1.0"),
        supported_workspace=VersionWindow(minimum="1.0", maximum="1.0"),
        deprecations=(),
        capabilities=CapabilitySet(supported=(), granted=(), effective=()),
    )


def test_an_error_code_and_its_retry_class_travel_together() -> None:
    with pytest.raises(SpiContractError, match="travel together"):
        outcome(disposition=Disposition.REFUSED, error_code=ERROR_CODE_CONFLICT)


def test_an_outcome_may_not_restate_the_catalogues_retry_class_wrongly() -> None:
    with pytest.raises(SpiContractError, match="frozen catalogue"):
        outcome(
            disposition=Disposition.REFUSED,
            error_code=ERROR_CODE_DEADLINE_EXCEEDED,
            retry_class=RETRY_CLASS_NON_RETRYABLE,
        )


def test_only_a_refusal_carries_an_error_code() -> None:
    with pytest.raises(SpiContractError, match="only a refusal"):
        outcome(
            disposition=Disposition.ACCEPTED,
            error_code=ERROR_CODE_CONFLICT,
            retry_class=RETRY_CLASS_NON_RETRYABLE,
            response=response(
                error=ApiError(
                    code=ERROR_CODE_CONFLICT, message="m", retry_class=RETRY_CLASS_NON_RETRYABLE
                )
            ),
        )


def test_a_refusal_without_an_error_code_is_refused() -> None:
    with pytest.raises(SpiContractError, match="only a refusal"):
        outcome(disposition=Disposition.REFUSED, reason=Reason.CAPABILITY)


def test_a_refused_hook_composes_nothing() -> None:
    with pytest.raises(SpiContractError, match="composes nothing"):
        outcome(
            disposition=Disposition.REFUSED,
            reason=Reason.CAPABILITY,
            error_code=ERROR_CODE_CONFLICT,
            retry_class=RETRY_CLASS_NON_RETRYABLE,
            composed_operations=("memory.search",),
            response=response(
                error=ApiError(
                    code=ERROR_CODE_CONFLICT, message="m", retry_class=RETRY_CLASS_NON_RETRYABLE
                )
            ),
        )


def test_capability_expansion_is_derived_from_what_the_hook_actually_held() -> None:
    assert not outcome(
        granted_capabilities=("memory.read@1.0",),
        effective_capabilities=("memory.read@1.0",),
    ).capability_expanded
    assert outcome(
        granted_capabilities=("memory.read@1.0",),
        effective_capabilities=("memory.read@1.0", "knowledge.govern@1.0"),
    ).capability_expanded


def test_job_control_implication_is_read_off_the_composed_operations() -> None:
    clean = outcome(composed_operations=("memory.search",))
    assert not clean.implies_core_job_cancel
    assert not clean.implies_core_job_retry


def test_exactly_one_of_result_or_error_is_present() -> None:
    with pytest.raises(SpiContractError, match="exactly one"):
        build_response(
            request_id="spi-memory-search-1",
            version=version_envelope(),  # type: ignore[arg-type]
            authority=GrantedAuthority(principal_id="c", roles=(), capabilities=()),
            audit_reference=None,
            result={},
            error=ApiError(
                code=ERROR_CODE_CONFLICT, message="m", retry_class=RETRY_CLASS_NON_RETRYABLE
            ),
        )


# --- envelope construction ----------------------------------------------------


def nested(operation: str, *, idempotency_key: str | None = None) -> object:
    return build_nested_envelope(
        operation,
        request_id="spi-memory-search-1",
        api_version="1.0",
        client=ClientIdentity(id="omnivia-core-agent-host-mock", version="0.6.8"),
        workspace_id="workspace-1",
        scopes=("memory",),
        purpose="context_recall",
        required_capabilities=(),
        principal_claim=PrincipalClaim(claimed_principal_id="caller-1"),
        deadline_ms=1_000,
        idempotency_key=idempotency_key,
    )


@pytest.mark.parametrize("operation", sorted(CORE_JOB_CONTROL_OPERATIONS))
def test_a_nested_envelope_may_never_carry_core_job_control(operation: str) -> None:
    with pytest.raises(SpiContractError, match="Core job control"):
        nested(operation)


def test_the_frozen_metadata_declares_no_host_identity_field() -> None:
    assert not envelope_carries_host_identity(nested("memory.search"))  # type: ignore[arg-type]


def test_host_identity_field_names_name_fields_the_contract_does_not_have() -> None:
    """The assertion tracks the contract: if a host field is ever added, this fails."""
    envelope = nested("memory.search")
    declared = {f.name for f in fields(envelope.metadata)}  # type: ignore[attr-defined]
    assert not declared & HOST_IDENTITY_FIELD_NAMES


def test_a_clean_envelope_does_not_read_as_leaking_provenance() -> None:
    """Absent values on both sides must not collide into a false positive."""
    assert not envelope_leaks_provenance(nested("memory.search"), provenance())  # type: ignore[arg-type]
    assert not envelope_leaks_provenance(
        nested("memory.create", idempotency_key="key-1"),  # type: ignore[arg-type]
        provenance(turn_ordinal=1),
    )


def test_a_smuggled_run_identifier_is_caught_by_value_not_only_by_field_name() -> None:
    smuggled = build_nested_envelope(
        "memory.search",
        request_id="run-1",
        api_version="1.0",
        client=ClientIdentity(id="omnivia-core-agent-host-mock", version="0.6.8"),
        workspace_id="workspace-1",
        scopes=("memory",),
        purpose="context_recall",
        required_capabilities=(),
        principal_claim=PrincipalClaim(claimed_principal_id="caller-1"),
        deadline_ms=1_000,
    )
    assert not envelope_carries_host_identity(smuggled)
    assert envelope_leaks_provenance(smuggled, provenance())


def test_the_version_envelope_wraps_the_frozen_upgrade_state_constant() -> None:
    envelope = version_envelope()
    assert envelope.compatibility.upgrade_state.value == UPGRADE_STATE_REQUIRED  # type: ignore[attr-defined]


# --- retry classification -----------------------------------------------------


def test_an_unrecognised_error_code_fails_safe_as_non_retryable() -> None:
    """SPI-R-011: never inferred to be retryable and never mapped onto a known code."""
    assert frozen_retry_class("something_the_spi_has_never_seen") == RETRY_CLASS_NON_RETRYABLE


def test_a_known_code_keeps_the_catalogues_own_class() -> None:
    assert frozen_retry_class(ERROR_CODE_DEADLINE_EXCEEDED) == "retryable"
    assert frozen_retry_class(ERROR_CODE_CONFLICT) == RETRY_CLASS_NON_RETRYABLE
