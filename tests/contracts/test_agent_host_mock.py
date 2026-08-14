"""V06-8 packet A9-P1: the deterministic in-process mock provider.

These tests are about what the provider *decides*, and they are written to
discriminate rather than to confirm. Most of them are a pair: the call that is
refused and the neighbouring call that differs in one field and is not, so a
provider that refused everything, or accepted everything, would fail them.

They are deliberately independent of the fixture corpus and of the four-layer
conformance module that will read it. Nothing here loads a fixture, names a
case identifier or asserts an expected-answer table; every assertion is about a
provider decision reached from a request, which is what has to stay true
whichever way the corpus is later replayed.

Two properties get the most attention, because both are places an adapter
author can ship the exact defect the SPI exists to prevent:

* **Turn terminal is not run terminal.** Closing a turn has to leave the run
  open and a later turn openable. A single terminal state would pass a test
  that only ever closed one turn, so the multi-turn tests close a turn and keep
  going.
* **A refusal composes nothing.** Every refusal test also asserts the empty
  composition, because a provider that refused the caller *after* reaching Core
  would produce the right disposition and the wrong effect.
"""

from __future__ import annotations

import pytest

from omnivia_core.agent_host import (
    ApprovalKind,
    Disposition,
    Hook,
    HookIntent,
    HookOutcome,
    MockProvider,
    ProviderProfile,
    Reason,
    SpiContractError,
    SpiProvenance,
    SpiRequest,
    VersionAxes,
)
from omnivia_core.agent_host.spi import (
    RUN_LEVEL_HOOKS,
    SPI_VERSION_MAXIMUM,
    TURN_SCOPED_HOOKS,
    envelope_carries_host_identity,
    envelope_leaks_provenance,
)
from omnivia_core.contracts.v1.generated import (
    COMPATIBILITY_STATUS_COMPATIBLE,
    COMPATIBILITY_STATUS_COMPATIBLE_WITH_DEPRECATIONS,
    COMPATIBILITY_STATUS_INCOMPATIBLE,
    COMPATIBILITY_STATUS_UPGRADE_REQUIRED,
    ERROR_CODE_AUTHENTICATION_REQUIRED,
    ERROR_CODE_AUTHORIZATION_DENIED,
    ERROR_CODE_CAPABILITY_NOT_GRANTED,
    ERROR_CODE_CONFLICT,
    ERROR_CODE_DEADLINE_EXCEEDED,
    ERROR_CODE_IDEMPOTENCY_CONFLICT,
    ERROR_CODE_INCOMPATIBLE_VERSION,
    ERROR_CODE_INTERNAL_NON_RECOVERABLE,
    ERROR_CODE_INVALID_PURPOSE,
    ERROR_CODE_INVALID_REQUEST,
    ERROR_CODE_SIZE_LIMIT_EXCEEDED,
    ERROR_CODE_WORKSPACE_LEASE_UNAVAILABLE,
    ERROR_CODE_WORKSPACE_MIGRATION_REQUIRED,
    ERROR_CODE_WORKSPACE_NOT_GRANTED,
    RETRY_CLASS_NON_RETRYABLE,
    ErrorResponseEnvelope,
    SuccessResponseEnvelope,
)

CALLER = "caller-1"
WORKSPACE = "workspace-1"
RUN = "run-1"

#: The purpose each hook travels under. Every one is on the default profile's
#: allowlist, so a purpose refusal in these tests is always the one the test
#: asked for.
HOOK_PURPOSES = {
    Hook.NEGOTIATE: "spi_negotiation",
    Hook.RECALL_BEFORE_TURN: "context_recall",
    Hook.MEMORY_SEARCH: "context_recall",
    Hook.CAPTURE_AFTER_TURN: "assistant_turn_capture",
    Hook.TOOL_RESULT_PERSIST: "tool_result_persistence",
    Hook.CONTEXT_COMPACT: "context_compaction",
    Hook.APPROVAL_REQUEST: "runtime_tool_approval",
    Hook.TURN_COMPLETE: "turn_control",
    Hook.TURN_CANCEL: "turn_control",
    Hook.TURN_RETRY: "turn_recovery",
}

#: Enough grant for any hook here to clear the capability check, and never more
#: than the default profile supports -- so an expansion would be visible.
GRANTED = ("memory.read@1.0", "memory.write@1.0", "knowledge.read@1.0")


def call(hook: Hook, sequence: int, *, turn: int | None = None, **overrides: object) -> SpiRequest:
    """A well-formed wrapper for `hook`; overrides are what a test is about."""
    if turn is None and hook in TURN_SCOPED_HOOKS:
        turn = 1
    base: dict[str, object] = {
        "hook": hook,
        "caller": CALLER,
        "workspace": WORKSPACE,
        "purpose": HOOK_PURPOSES[hook],
        "provenance": SpiProvenance(
            agent="agent-1", session="session-1", run=RUN, sequence=sequence, turn_ordinal=turn
        ),
        "granted_capabilities": GRANTED,
        "deadline_ms": 1_000,
    }
    if hook is Hook.APPROVAL_REQUEST:
        base["approval_kind"] = ApprovalKind.RUNTIME_TOOL_APPROVAL
    if hook in {Hook.CAPTURE_AFTER_TURN, Hook.TOOL_RESULT_PERSIST}:
        base["idempotency_key"] = f"key-{sequence}"
    base.update(overrides)
    return SpiRequest(**base)  # type: ignore[arg-type]


def negotiated(profile: ProviderProfile | None = None) -> MockProvider:
    """A provider past negotiation, with the run's first turn open."""
    provider = MockProvider(profile)
    assert provider.handle(call(Hook.NEGOTIATE, 0)).disposition is Disposition.NEGOTIATED
    assert provider.handle(call(Hook.RECALL_BEFORE_TURN, 1)).disposition is Disposition.ACCEPTED
    return provider


def refused(outcome: HookOutcome, reason: Reason, code: str) -> None:
    """A refusal is the disposition, the reason, the code -- and no effect."""
    assert outcome.disposition is Disposition.REFUSED
    assert outcome.reason is reason
    assert outcome.error_code == code
    assert outcome.composed_operations == ()
    assert outcome.nested_envelopes == ()
    assert isinstance(outcome.response, ErrorResponseEnvelope)
    assert outcome.response.error.code == code


# --- all ten hooks reach the provider ----------------------------------------


def test_every_hook_is_driven_by_the_mock_and_none_is_unhandled() -> None:
    provider = negotiated()
    seen = {Hook.NEGOTIATE: Disposition.NEGOTIATED, Hook.RECALL_BEFORE_TURN: Disposition.ACCEPTED}
    for index, hook in enumerate(
        [
            Hook.MEMORY_SEARCH,
            Hook.CAPTURE_AFTER_TURN,
            Hook.TOOL_RESULT_PERSIST,
            Hook.CONTEXT_COMPACT,
            Hook.APPROVAL_REQUEST,
            Hook.TURN_RETRY,
            Hook.TURN_CANCEL,
        ],
        start=2,
    ):
        outcome = provider.handle(call(hook, index))
        assert outcome.disposition is not Disposition.REFUSED, hook
        seen[hook] = outcome.disposition
    # `turn.complete` needs a live turn, and `turn.cancel` just closed turn 1.
    provider.handle(call(Hook.RECALL_BEFORE_TURN, 9, turn=2))
    seen[Hook.TURN_COMPLETE] = provider.handle(
        call(Hook.TURN_COMPLETE, 10, turn=2)
    ).disposition
    assert set(seen) == set(Hook)
    assert len(provider.journal) == 11


def test_only_the_hooks_that_declare_compositions_reach_core() -> None:
    provider = negotiated()
    assert provider.composed_operations == (
        "workspace.inspect",
        "memory.search",
        "knowledge.search",
        "evidence.search",
        "context_pack.build",
    )
    provider.handle(call(Hook.CONTEXT_COMPACT, 2))
    provider.handle(call(Hook.TURN_COMPLETE, 3))
    assert provider.composed_operations.count("job.cancel") == 0
    assert not any(
        outcome.implies_core_job_cancel or outcome.implies_core_job_retry
        for outcome in provider.journal
    )


def test_no_composed_envelope_carries_host_identity_or_leaks_provenance() -> None:
    provider = negotiated()
    provider.handle(call(Hook.CAPTURE_AFTER_TURN, 2))
    prov = SpiProvenance(agent="agent-1", session="session-1", run=RUN, sequence=1, turn_ordinal=1)
    assert provider.nested_envelopes
    for envelope in provider.nested_envelopes:
        assert not envelope_carries_host_identity(envelope)
        assert not envelope_leaks_provenance(envelope, prov)
    assert not any(outcome.host_identity_in_request_metadata for outcome in provider.journal)


# --- negotiation before use ---------------------------------------------------


@pytest.mark.parametrize(
    "hook", sorted(set(Hook) - {Hook.NEGOTIATE}, key=lambda hook: hook.value)
)
def test_no_hook_runs_before_negotiation(hook: Hook) -> None:
    provider = MockProvider()
    refused(
        provider.handle(call(hook, 1)),
        Reason.PRE_NEGOTIATION,
        ERROR_CODE_INVALID_REQUEST,
    )
    assert provider.composed_operations == ()


def test_the_same_hook_runs_once_negotiation_has_happened() -> None:
    provider = MockProvider()
    assert provider.handle(call(Hook.MEMORY_SEARCH, 1)).disposition is Disposition.REFUSED
    provider.handle(call(Hook.NEGOTIATE, 2))
    provider.handle(call(Hook.RECALL_BEFORE_TURN, 3))
    assert provider.handle(call(Hook.MEMORY_SEARCH, 4)).disposition is Disposition.ACCEPTED


def test_negotiation_reports_all_five_axes_together() -> None:
    provider = MockProvider()
    outcome = provider.handle(call(Hook.NEGOTIATE, 0, declared_spi_version="1.0.0"))
    axes = outcome.version_axes
    assert isinstance(axes, VersionAxes)
    assert (axes.spi, axes.api, axes.server, axes.workspace_format, axes.client) == (
        "1.0.0",
        "1.0",
        "0.6.8",
        "1.0",
        "0.6.8",
    )
    assert provider.selected_spi_version == "1.0.0"


def test_a_declared_version_above_the_window_negotiates_down() -> None:
    provider = MockProvider()
    outcome = provider.handle(call(Hook.NEGOTIATE, 0, declared_spi_version="1.2.0"))
    assert outcome.disposition is Disposition.NEGOTIATED
    assert provider.selected_spi_version == SPI_VERSION_MAXIMUM
    assert outcome.compatibility_status == COMPATIBILITY_STATUS_COMPATIBLE


def test_a_major_version_mismatch_is_incompatible_and_leaves_the_session_closed() -> None:
    provider = MockProvider()
    refused(
        provider.handle(call(Hook.NEGOTIATE, 0, declared_spi_version="2.0.0")),
        Reason.NEGOTIATED,
        ERROR_CODE_INCOMPATIBLE_VERSION,
    )
    assert not provider.negotiated
    assert provider.selected_spi_version is None


def test_a_deprecated_capability_negotiates_with_deprecations_rather_than_failing() -> None:
    provider = MockProvider()
    outcome = provider.handle(
        call(Hook.NEGOTIATE, 0, required_capabilities=("memory.read@1.0",))
    )
    assert outcome.disposition is Disposition.NEGOTIATED
    assert outcome.compatibility_status == COMPATIBILITY_STATUS_COMPATIBLE_WITH_DEPRECATIONS


def test_an_unsupported_capability_at_negotiation_is_incompatible() -> None:
    provider = MockProvider()
    refused(
        provider.handle(call(Hook.NEGOTIATE, 0, required_capabilities=("unknown.thing@1.0",))),
        Reason.NEGOTIATED,
        ERROR_CODE_CAPABILITY_NOT_GRANTED,
    )
    assert not provider.negotiated


def test_a_workspace_below_the_minimum_format_requires_an_upgrade() -> None:
    provider = MockProvider()
    outcome = provider.handle(
        call(Hook.NEGOTIATE, 0),
        profile=ProviderProfile(workspaces_below_minimum_format=frozenset({WORKSPACE})),
    )
    refused(outcome, Reason.NEGOTIATED, ERROR_CODE_WORKSPACE_MIGRATION_REQUIRED)
    assert outcome.compatibility_status == COMPATIBILITY_STATUS_UPGRADE_REQUIRED
    assert not provider.negotiated


def test_the_four_negotiation_statuses_are_reachable_and_distinct() -> None:
    statuses = {
        MockProvider().handle(call(Hook.NEGOTIATE, 0)).compatibility_status,
        MockProvider()
        .handle(call(Hook.NEGOTIATE, 0, required_capabilities=("memory.read@1.0",)))
        .compatibility_status,
        MockProvider()
        .handle(call(Hook.NEGOTIATE, 0, declared_spi_version="2.0.0"))
        .compatibility_status,
        MockProvider()
        .handle(
            call(Hook.NEGOTIATE, 0),
            profile=ProviderProfile(workspaces_below_minimum_format=frozenset({WORKSPACE})),
        )
        .compatibility_status,
    }
    assert statuses == {
        COMPATIBILITY_STATUS_COMPATIBLE,
        COMPATIBILITY_STATUS_COMPATIBLE_WITH_DEPRECATIONS,
        COMPATIBILITY_STATUS_INCOMPATIBLE,
        COMPATIBILITY_STATUS_UPGRADE_REQUIRED,
    }


# --- monotonic sequence -------------------------------------------------------


def test_a_stale_effecting_hook_is_refused() -> None:
    provider = negotiated()
    provider.handle(call(Hook.CAPTURE_AFTER_TURN, 5))
    refused(
        provider.handle(call(Hook.CAPTURE_AFTER_TURN, 3)),
        Reason.STALE_SEQUENCE,
        ERROR_CODE_INVALID_REQUEST,
    )


def test_a_stale_read_is_dropped_rather_than_refused() -> None:
    """A read changes nothing, so a late delivery of one is ignored (SPI-R-027)."""
    provider = negotiated()
    provider.handle(call(Hook.MEMORY_SEARCH, 5))
    outcome = provider.handle(call(Hook.MEMORY_SEARCH, 3))
    assert outcome.disposition is Disposition.IGNORED
    assert outcome.reason is Reason.STALE_SEQUENCE
    assert outcome.error_code is None
    assert outcome.composed_operations == ()


def test_a_repeated_sequence_is_stale_and_the_next_one_is_not() -> None:
    provider = negotiated()
    provider.handle(call(Hook.MEMORY_SEARCH, 5))
    assert provider.handle(call(Hook.MEMORY_SEARCH, 5)).disposition is Disposition.IGNORED
    assert provider.handle(call(Hook.MEMORY_SEARCH, 6)).disposition is Disposition.ACCEPTED


# --- the turn lifecycle -------------------------------------------------------


def test_a_turn_hook_on_a_turn_that_was_never_opened_is_refused() -> None:
    provider = negotiated()
    refused(
        provider.handle(call(Hook.CAPTURE_AFTER_TURN, 2, turn=7)),
        Reason.TURN_NOT_OPEN,
        ERROR_CODE_INVALID_REQUEST,
    )


def test_recall_opens_its_turn_and_a_second_recall_on_it_is_accepted() -> None:
    provider = negotiated()
    assert provider.open_turns[RUN] == (1,)
    assert provider.handle(call(Hook.RECALL_BEFORE_TURN, 2)).disposition is Disposition.ACCEPTED


def test_a_turn_behind_the_frontier_cannot_be_opened_again() -> None:
    provider = negotiated()
    provider.handle(call(Hook.RECALL_BEFORE_TURN, 2, turn=4))
    refused(
        provider.handle(call(Hook.RECALL_BEFORE_TURN, 3, turn=3)),
        Reason.TURN_NOT_OPEN,
        ERROR_CODE_INVALID_REQUEST,
    )


def test_completing_a_turn_is_terminal_for_that_turn_only() -> None:
    """Turn terminal is not run terminal: the run stays open behind it."""
    provider = negotiated()
    assert provider.handle(call(Hook.TURN_COMPLETE, 2)).disposition is Disposition.ACCEPTED
    refused(
        provider.handle(call(Hook.MEMORY_SEARCH, 3)),
        Reason.TURN_TERMINAL,
        ERROR_CODE_CONFLICT,
    )
    assert provider.handle(call(Hook.RECALL_BEFORE_TURN, 4, turn=2)).disposition is (
        Disposition.ACCEPTED
    )
    assert provider.handle(call(Hook.MEMORY_SEARCH, 5, turn=2)).disposition is (
        Disposition.ACCEPTED
    )
    assert provider.open_turns[RUN] == (2,)


def test_a_cancelled_turn_cannot_then_be_completed() -> None:
    provider = negotiated()
    assert provider.handle(call(Hook.TURN_CANCEL, 2)).disposition is Disposition.ACCEPTED
    refused(
        provider.handle(call(Hook.TURN_COMPLETE, 3)),
        Reason.TURN_TERMINAL,
        ERROR_CODE_CONFLICT,
    )


def test_repeating_a_turn_control_call_is_a_replay_not_a_conflict() -> None:
    provider = negotiated()
    provider.handle(call(Hook.TURN_COMPLETE, 2))
    outcome = provider.handle(call(Hook.TURN_COMPLETE, 3))
    assert outcome.disposition is Disposition.IDEMPOTENT_REPLAY
    assert outcome.reason is Reason.STATE_REPLAY
    assert outcome.error_code is None


def test_turn_retry_reopens_the_identified_turn_and_never_the_run() -> None:
    provider = negotiated()
    provider.handle(call(Hook.TURN_COMPLETE, 2))
    assert provider.open_turns[RUN] == ()
    assert provider.handle(call(Hook.TURN_RETRY, 3)).disposition is Disposition.ACCEPTED
    assert provider.open_turns[RUN] == (1,)
    assert provider.handle(call(Hook.MEMORY_SEARCH, 4)).disposition is Disposition.ACCEPTED


def test_turn_control_returns_job_handles_rather_than_acting_on_them() -> None:
    provider = negotiated()
    outcome = provider.handle(call(Hook.TURN_CANCEL, 2))
    assert [handle.job_id for handle in outcome.job_handles] == [f"job-{RUN}-1"]
    assert outcome.composed_operations == ()
    assert not outcome.implies_core_job_cancel


def test_two_turns_close_independently_in_one_run() -> None:
    provider = negotiated()
    provider.handle(call(Hook.RECALL_BEFORE_TURN, 2, turn=2))
    provider.handle(call(Hook.TURN_COMPLETE, 3, turn=1))
    assert provider.open_turns[RUN] == (2,)
    assert provider.handle(call(Hook.MEMORY_SEARCH, 4, turn=2)).disposition is (
        Disposition.ACCEPTED
    )
    provider.handle(call(Hook.TURN_CANCEL, 5, turn=2))
    assert provider.open_turns[RUN] == ()


# --- the deadline -------------------------------------------------------------


def test_an_omitted_deadline_reaches_the_provider_and_is_refused_there() -> None:
    provider = negotiated()
    refused(
        provider.handle(call(Hook.MEMORY_SEARCH, 2, deadline_ms=None)),
        Reason.MISSING_DEADLINE,
        ERROR_CODE_INVALID_REQUEST,
    )


def test_a_deadline_wider_than_the_parents_remaining_time_is_refused() -> None:
    provider = negotiated()
    refused(
        provider.handle(call(Hook.MEMORY_SEARCH, 2, deadline_ms=900, parent_remaining_ms=500)),
        Reason.NESTED_DEADLINE,
        ERROR_CODE_INVALID_REQUEST,
    )
    assert provider.handle(
        call(Hook.MEMORY_SEARCH, 3, deadline_ms=400, parent_remaining_ms=500)
    ).disposition is Disposition.ACCEPTED


def test_an_exhausted_deadline_is_refused_retryable() -> None:
    provider = negotiated()
    outcome = provider.handle(call(Hook.MEMORY_SEARCH, 2, deadline_ms=1_000, elapsed_ms=1_000))
    refused(outcome, Reason.DEADLINE_EXCEEDED, ERROR_CODE_DEADLINE_EXCEEDED)
    assert outcome.retry_class == "retryable"


def test_time_is_a_request_field_so_the_same_call_decides_the_same_way_twice() -> None:
    """No clock: two providers given identical inputs produce identical outcomes."""
    first = negotiated().handle(call(Hook.MEMORY_SEARCH, 2, elapsed_ms=999))
    second = negotiated().handle(call(Hook.MEMORY_SEARCH, 2, elapsed_ms=999))
    assert first == second


# --- authority: caller, workspace, purpose, capability ------------------------


def test_an_unauthenticated_caller_is_refused() -> None:
    provider = negotiated()
    refused(
        provider.handle(
            call(Hook.MEMORY_SEARCH, 2),
            profile=ProviderProfile(unauthenticated_callers=frozenset({CALLER})),
        ),
        Reason.AUTHENTICATION,
        ERROR_CODE_AUTHENTICATION_REQUIRED,
    )
    assert provider.handle(call(Hook.MEMORY_SEARCH, 3)).disposition is Disposition.ACCEPTED


def test_an_ungranted_workspace_is_refused() -> None:
    provider = negotiated()
    refused(
        provider.handle(
            call(Hook.MEMORY_SEARCH, 2),
            profile=ProviderProfile(ungranted_workspaces=frozenset({WORKSPACE})),
        ),
        Reason.WORKSPACE_BINDING,
        ERROR_CODE_WORKSPACE_NOT_GRANTED,
    )


def test_a_run_may_not_be_rebound_to_a_second_workspace() -> None:
    provider = negotiated()
    refused(
        provider.handle(call(Hook.MEMORY_SEARCH, 2, workspace="workspace-2")),
        Reason.WORKSPACE_BINDING,
        ERROR_CODE_WORKSPACE_NOT_GRANTED,
    )


def test_a_purpose_outside_the_allowlist_is_refused_rather_than_admitted() -> None:
    """Purpose is a positive check: unrecognised is refused, not passed through."""
    provider = negotiated()
    refused(
        provider.handle(call(Hook.MEMORY_SEARCH, 2, purpose="whatever_the_host_wanted")),
        Reason.PURPOSE,
        ERROR_CODE_INVALID_PURPOSE,
    )


def test_a_required_capability_outside_the_grant_is_refused() -> None:
    provider = negotiated()
    refused(
        provider.handle(
            call(Hook.MEMORY_SEARCH, 2, required_capabilities=("knowledge.govern@1.0",))
        ),
        Reason.CAPABILITY,
        ERROR_CODE_CAPABILITY_NOT_GRANTED,
    )
    assert provider.handle(
        call(Hook.MEMORY_SEARCH, 3, required_capabilities=("memory.read@1.0",))
    ).disposition is Disposition.ACCEPTED


def test_the_effective_set_never_exceeds_the_grant() -> None:
    provider = negotiated()
    outcome = provider.handle(call(Hook.MEMORY_SEARCH, 2))
    assert set(outcome.effective_capabilities) <= set(outcome.granted_capabilities)
    assert not outcome.capability_expanded


def test_a_grant_the_server_does_not_support_does_not_become_effective() -> None:
    provider = negotiated()
    outcome = provider.handle(
        call(Hook.MEMORY_SEARCH, 2, granted_capabilities=("memory.read@1.0", "nothing.here@1.0"))
    )
    assert "nothing.here@1.0" not in outcome.effective_capabilities


def test_an_unavailable_lease_is_refused_retryable_after_delay() -> None:
    provider = negotiated()
    outcome = provider.handle(
        call(Hook.MEMORY_SEARCH, 2),
        profile=ProviderProfile(leased_workspaces=frozenset({WORKSPACE})),
    )
    refused(outcome, Reason.LEASE_UNAVAILABLE, ERROR_CODE_WORKSPACE_LEASE_UNAVAILABLE)
    assert outcome.retry_class == "retryable_after_delay"


def test_a_profile_is_a_condition_of_the_call_not_of_the_session() -> None:
    provider = negotiated()
    leased = ProviderProfile(leased_workspaces=frozenset({WORKSPACE}))
    assert provider.handle(call(Hook.MEMORY_SEARCH, 2), profile=leased).disposition is (
        Disposition.REFUSED
    )
    assert provider.handle(call(Hook.MEMORY_SEARCH, 3)).disposition is Disposition.ACCEPTED


# --- the compositions the SPI may never make ---------------------------------


@pytest.mark.parametrize(
    ("flag", "reason", "code"),
    [
        ("inject_host_identity", Reason.IDENTITY_INJECTION, ERROR_CODE_INVALID_REQUEST),
        ("direct_storage_access", Reason.DIRECT_STORAGE_ACCESS, ERROR_CODE_AUTHORIZATION_DENIED),
        ("request_core_run_state", Reason.RUN_STATE_REQUESTED, ERROR_CODE_INVALID_REQUEST),
        (
            "compose_core_job_control",
            Reason.JOB_CONTROL_COMPOSITION,
            ERROR_CODE_INVALID_REQUEST,
        ),
        ("requires_host_source_patch", Reason.HOST_SOURCE_PATCH, ERROR_CODE_INVALID_REQUEST),
    ],
)
def test_a_forbidden_composition_is_refused_even_when_fully_capable(
    flag: str, reason: Reason, code: str
) -> None:
    """What is refused is the composition, not the capability: `job.control` does not help."""
    provider = negotiated()
    refused(
        provider.handle(
            call(
                Hook.MEMORY_SEARCH,
                2,
                granted_capabilities=("memory.read@1.0", "job.control@1.0"),
                intent=HookIntent(**{flag: True}),
            )
        ),
        reason,
        code,
    )


def test_a_boundary_refusal_precedes_every_other_check() -> None:
    """An otherwise unusable request is still refused for the composition it asked for."""
    provider = negotiated()
    outcome = provider.handle(
        call(
            Hook.MEMORY_SEARCH,
            2,
            purpose="not_on_the_allowlist",
            intent=HookIntent(compose_core_job_control=True),
        )
    )
    assert outcome.reason is Reason.JOB_CONTROL_COMPOSITION


# --- idempotency --------------------------------------------------------------


def test_an_effecting_hook_without_an_idempotency_key_is_refused() -> None:
    provider = negotiated()
    refused(
        provider.handle(call(Hook.CAPTURE_AFTER_TURN, 2, idempotency_key=None)),
        Reason.MISSING_IDEMPOTENCY_KEY,
        ERROR_CODE_INVALID_REQUEST,
    )


def test_a_read_needs_no_idempotency_key() -> None:
    provider = negotiated()
    assert provider.handle(call(Hook.MEMORY_SEARCH, 2)).disposition is Disposition.ACCEPTED


def test_a_replayed_key_returns_the_recorded_result_and_composes_no_second_effect() -> None:
    provider = negotiated()
    first = provider.handle(call(Hook.CAPTURE_AFTER_TURN, 2, idempotency_key="key-a"))
    assert first.composed_operations == ("memory.create",)
    replay = provider.handle(call(Hook.CAPTURE_AFTER_TURN, 3, idempotency_key="key-a"))
    assert replay.disposition is Disposition.IDEMPOTENT_REPLAY
    assert replay.reason is Reason.IDEMPOTENT_REPLAY
    assert replay.composed_operations == ()
    assert provider.composed_operations.count("memory.create") == 1


def test_the_same_key_with_a_different_payload_is_a_conflict() -> None:
    provider = negotiated()
    provider.handle(call(Hook.CAPTURE_AFTER_TURN, 2, idempotency_key="key-a"))
    refused(
        provider.handle(
            call(Hook.CAPTURE_AFTER_TURN, 3, idempotency_key="key-a", payload_digest="other")
        ),
        Reason.IDEMPOTENCY_CONFLICT,
        ERROR_CODE_IDEMPOTENCY_CONFLICT,
    )


def test_a_caller_defect_does_not_burn_the_key() -> None:
    """Nothing is uncertain after a non-retryable refusal, so the key stays usable."""
    provider = negotiated()
    refused(
        provider.handle(
            call(
                Hook.TOOL_RESULT_PERSIST,
                2,
                idempotency_key="key-b",
                intent=HookIntent(inline_payload_bytes=200_000),
            )
        ),
        Reason.SIZE_LIMIT,
        ERROR_CODE_SIZE_LIMIT_EXCEEDED,
    )
    retried = provider.handle(
        call(
            Hook.TOOL_RESULT_PERSIST,
            3,
            idempotency_key="key-b",
            intent=HookIntent(content_reference="content-ref-1"),
        )
    )
    assert retried.disposition is Disposition.ACCEPTED
    assert retried.composed_operations == ("memory.create",)


def test_a_retryable_failure_reserves_the_key_so_the_retry_is_safe() -> None:
    provider = negotiated()
    provider.handle(
        call(Hook.CAPTURE_AFTER_TURN, 2, idempotency_key="key-c", elapsed_ms=1_000)
    )
    retried = provider.handle(call(Hook.CAPTURE_AFTER_TURN, 3, idempotency_key="key-c"))
    assert retried.disposition is Disposition.IDEMPOTENT_REPLAY
    assert retried.composed_operations == ()


def test_two_distinct_keys_each_take_effect() -> None:
    provider = negotiated()
    provider.handle(call(Hook.CAPTURE_AFTER_TURN, 2, idempotency_key="key-d"))
    provider.handle(call(Hook.CAPTURE_AFTER_TURN, 3, idempotency_key="key-e"))
    assert provider.composed_operations.count("memory.create") == 2


# --- oversized results --------------------------------------------------------


def test_an_oversized_inline_result_is_refused_and_the_same_result_by_reference_is_not() -> None:
    provider = negotiated()
    profile = ProviderProfile(max_inline_result_bytes=1_024)
    refused(
        provider.handle(
            call(
                Hook.TOOL_RESULT_PERSIST,
                2,
                idempotency_key="key-f",
                intent=HookIntent(inline_payload_bytes=1_025),
            ),
            profile=profile,
        ),
        Reason.SIZE_LIMIT,
        ERROR_CODE_SIZE_LIMIT_EXCEEDED,
    )
    by_reference = provider.handle(
        call(
            Hook.TOOL_RESULT_PERSIST,
            3,
            idempotency_key="key-g",
            intent=HookIntent(inline_payload_bytes=0, content_reference="content-ref-1"),
        ),
        profile=profile,
    )
    assert by_reference.disposition is Disposition.ACCEPTED


def test_a_payload_at_the_inline_bound_is_accepted() -> None:
    provider = negotiated()
    outcome = provider.handle(
        call(
            Hook.TOOL_RESULT_PERSIST,
            2,
            idempotency_key="key-h",
            intent=HookIntent(inline_payload_bytes=1_024),
        ),
        profile=ProviderProfile(max_inline_result_bytes=1_024),
    )
    assert outcome.disposition is Disposition.ACCEPTED


def test_a_size_refusal_is_non_retryable() -> None:
    provider = negotiated()
    outcome = provider.handle(
        call(
            Hook.TOOL_RESULT_PERSIST,
            2,
            idempotency_key="key-i",
            intent=HookIntent(inline_payload_bytes=200_000),
        )
    )
    assert outcome.retry_class == RETRY_CLASS_NON_RETRYABLE


# --- approval kinds -----------------------------------------------------------


def test_a_runtime_tool_approval_stays_run_local() -> None:
    """It composes no Core operation, records no Core decision and is not audited."""
    provider = negotiated()
    outcome = provider.handle(
        call(Hook.APPROVAL_REQUEST, 2, approval_kind=ApprovalKind.RUNTIME_TOOL_APPROVAL)
    )
    assert outcome.disposition is Disposition.ACCEPTED
    assert outcome.composed_operations == ()
    assert not outcome.audit_record_required
    assert isinstance(outcome.response, SuccessResponseEnvelope)
    assert outcome.response.metadata.audit_reference is None


def test_a_governance_escalation_is_reported_and_never_taken() -> None:
    provider = negotiated()
    outcome = provider.handle(
        call(
            Hook.APPROVAL_REQUEST,
            2,
            purpose="governed_mutation",
            approval_kind=ApprovalKind.CORE_GOVERNANCE_ESCALATION,
        )
    )
    assert outcome.disposition is Disposition.ESCALATION_REQUIRED
    assert outcome.reason is Reason.GOVERNANCE_ESCALATION
    assert outcome.composed_operations == ()
    assert outcome.audit_record_required
    assert isinstance(outcome.response, SuccessResponseEnvelope)
    assert outcome.response.result == {"required_path": "knowledge.govern"}


def test_the_two_approval_kinds_are_never_interchangeable() -> None:
    provider = negotiated()
    runtime = provider.handle(
        call(Hook.APPROVAL_REQUEST, 2, approval_kind=ApprovalKind.RUNTIME_TOOL_APPROVAL)
    )
    escalation = provider.handle(
        call(
            Hook.APPROVAL_REQUEST,
            3,
            purpose="governed_mutation",
            approval_kind=ApprovalKind.CORE_GOVERNANCE_ESCALATION,
        )
    )
    assert runtime.disposition is not escalation.disposition
    assert runtime.audit_record_required is not escalation.audit_record_required


def test_promoting_to_governed_knowledge_escalates_rather_than_writing() -> None:
    provider = negotiated()
    outcome = provider.handle(
        call(
            Hook.CAPTURE_AFTER_TURN,
            2,
            purpose="governed_mutation",
            intent=HookIntent(promote_to_governed_knowledge=True),
        )
    )
    assert outcome.disposition is Disposition.ESCALATION_REQUIRED
    assert outcome.composed_operations == ()


def test_ending_a_turn_never_takes_the_governance_path() -> None:
    provider = negotiated()
    outcome = provider.handle(call(Hook.TURN_COMPLETE, 2))
    assert outcome.disposition is Disposition.ACCEPTED
    assert outcome.reason is not Reason.GOVERNANCE_ESCALATION
    assert outcome.composed_operations == ()


# --- retry classification -----------------------------------------------------


def test_a_non_retryable_prior_failure_is_not_retried() -> None:
    provider = negotiated()
    refused(
        provider.handle(
            call(Hook.TURN_RETRY, 2, intent=HookIntent(prior_failure_code=ERROR_CODE_CONFLICT))
        ),
        Reason.NON_RETRYABLE_FAILURE,
        ERROR_CODE_CONFLICT,
    )


def test_a_retryable_prior_failure_is_retried() -> None:
    provider = negotiated()
    outcome = provider.handle(
        call(Hook.TURN_RETRY, 2, intent=HookIntent(prior_failure_code="rate_limited"))
    )
    assert outcome.disposition is Disposition.ACCEPTED


def test_an_unrecognised_retry_class_is_never_inferred_to_be_retryable() -> None:
    provider = negotiated()
    outcome = provider.handle(
        call(Hook.TURN_RETRY, 2, intent=HookIntent(unrecognised_retry_class="retry_soonish"))
    )
    refused(outcome, Reason.UNRECOGNISED_RETRY_CLASS, ERROR_CODE_INTERNAL_NON_RECOVERABLE)
    assert outcome.retry_class == RETRY_CLASS_NON_RETRYABLE


def test_every_refusal_carries_the_frozen_catalogues_class_for_its_code() -> None:
    provider = negotiated()
    provider.handle(call(Hook.MEMORY_SEARCH, 2, purpose="nope"))
    provider.handle(call(Hook.MEMORY_SEARCH, 3, elapsed_ms=1_000))
    refusals = [o for o in provider.journal if o.disposition is Disposition.REFUSED]
    assert len(refusals) == 2
    for outcome in refusals:
        assert outcome.retry_class is not None
        assert outcome.response.error.retry_class == outcome.retry_class  # type: ignore[union-attr]


# --- audit policy -------------------------------------------------------------


def test_a_run_level_hook_is_never_audited() -> None:
    provider = negotiated()
    for hook in sorted(RUN_LEVEL_HOOKS, key=lambda item: item.value):
        outcome = provider.handle(call(hook, 2 + len(provider.journal)))
        assert not outcome.audit_record_required, hook


def test_an_authority_refusal_is_audited_and_a_shape_refusal_is_not() -> None:
    provider = negotiated()
    authority = provider.handle(call(Hook.MEMORY_SEARCH, 2, purpose="nope"))
    shape = provider.handle(call(Hook.MEMORY_SEARCH, 3, deadline_ms=None))
    assert authority.audit_record_required
    assert not shape.audit_record_required


def test_an_audited_outcome_carries_an_audit_reference_and_an_unaudited_one_does_not() -> None:
    provider = negotiated()
    audited = provider.handle(call(Hook.CAPTURE_AFTER_TURN, 2))
    unaudited = provider.handle(call(Hook.CONTEXT_COMPACT, 3))
    assert audited.audit_record_required
    assert audited.response.metadata.audit_reference is not None
    assert not unaudited.audit_record_required
    assert unaudited.response.metadata.audit_reference is None


# --- compaction ---------------------------------------------------------------


def test_compaction_is_a_host_local_notice_that_reaches_nothing() -> None:
    provider = negotiated()
    outcome = provider.handle(call(Hook.CONTEXT_COMPACT, 2))
    assert outcome.disposition is Disposition.IGNORED
    assert outcome.reason is Reason.COMPACTION_NOTICE
    assert outcome.composed_operations == ()
    assert not outcome.audit_record_required


# --- no durable state ---------------------------------------------------------


def test_reset_drops_every_scrap_of_session_state() -> None:
    provider = negotiated()
    provider.handle(call(Hook.CAPTURE_AFTER_TURN, 2, idempotency_key="key-j"))
    provider.reset()
    assert not provider.negotiated
    assert provider.selected_spi_version is None
    assert provider.journal == ()
    assert dict(provider.open_turns) == {}
    refused(
        provider.handle(call(Hook.MEMORY_SEARCH, 3)),
        Reason.PRE_NEGOTIATION,
        ERROR_CODE_INVALID_REQUEST,
    )


def test_the_provider_exposes_no_persistence_entry_point() -> None:
    surface = {name for name in dir(MockProvider) if not name.startswith("_")}
    assert not surface & {"persist", "load", "save", "checkpoint", "restore", "replay"}


def test_two_runs_keep_separate_turn_state_in_one_provider() -> None:
    provider = negotiated()
    other = SpiProvenance(
        agent="agent-1", session="session-1", run="run-2", sequence=1, turn_ordinal=1
    )
    provider.handle(call(Hook.RECALL_BEFORE_TURN, 1, provenance=other))
    provider.handle(call(Hook.TURN_COMPLETE, 2))
    assert provider.open_turns[RUN] == ()
    assert provider.open_turns["run-2"] == (1,)


def test_the_journal_is_bounded_rather_than_unbounded() -> None:
    provider = negotiated()
    with pytest.raises(SpiContractError, match="bound exceeded"):
        for index in range(4_200):
            provider.handle(call(Hook.CONTEXT_COMPACT, 2 + index))


def test_an_invalid_profile_is_refused_at_construction() -> None:
    with pytest.raises(SpiContractError):
        ProviderProfile(max_inline_result_bytes=0)
    with pytest.raises(SpiContractError):
        ProviderProfile(supported_capabilities=frozenset({"not-a-token"}))
