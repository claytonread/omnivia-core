"""RT-108 acceptance for the synchronous WorkerAdapter port and scripted oracle.

The adapter has no database, dispatcher, tool, credential or policy handle, so
these tests build sessions purely from lineage and scripted events and prove
the port's own contract: descriptor negotiation, vocabulary and bounds
validation, dedupe/conflict quarantine, wait/resume lineage enforcement, and
the open/start/wait/resume/cancel/close/dispose state machine.
"""

from __future__ import annotations

import inspect

import pytest
from omnivia_core_runtime.service.worker_adapter import (
    ADAPTER_TYPE_DETERMINISTIC,
    CONTRACT_VERSION,
    DISABLEABLE_CAPABILITIES,
    EVENT_KINDS,
    HostLineage,
    NormalizedWorkerEvent,
    ScriptedWorkerEvent,
    WorkerAdapter,
    WorkerAdapterRegistry,
    WorkerCapabilities,
    WorkerContractError,
    WorkerDescriptor,
    WorkerEventConflict,
    WorkerEventRejected,
    WorkerScriptIncomplete,
    WorkerSinkError,
    WorkerStateError,
    negotiate_descriptor,
)

LINEAGE = HostLineage(
    workspace_id="ws-rt108",
    run_id="run-rt108-0001",
    run_step_id="step-rt108-0001",
    attempt_id="att-rt108-0001",
)

OTHER_LINEAGE = HostLineage(
    workspace_id="ws-rt108",
    run_id="run-rt108-0001",
    run_step_id="step-rt108-0001",
    attempt_id="att-rt108-0002",
)


class Sink:
    """Records every normalized event handed to it, in order."""

    def __init__(self) -> None:
        self.events: list[NormalizedWorkerEvent] = []

    def __call__(self, event: NormalizedWorkerEvent) -> None:
        self.events.append(event)


class Abort:
    def __init__(self, *, set_: bool = False) -> None:
        self._set = set_

    def is_set(self) -> bool:
        return self._set

    def set(self) -> None:
        self._set = True


def scripted(
    source_event_id: str,
    kind: str,
    *,
    occurred_at_us: int = 1_000,
    safe_summary: str = "ok",
    payload_ref: str | None = None,
    external_session_id: str | None = None,
    external_turn_id: str | None = None,
    claimed_lineage: HostLineage | None = None,
) -> ScriptedWorkerEvent:
    return ScriptedWorkerEvent(
        source_event_id=source_event_id,
        kind=kind,
        occurred_at_us=occurred_at_us,
        safe_summary=safe_summary,
        external_session_id=external_session_id,
        external_turn_id=external_turn_id,
        payload_ref=payload_ref,
        claimed_lineage=claimed_lineage,
    )


WAIT_SCRIPT = (
    scripted("evt-1", "message_delta"),
    scripted("evt-2", "wait_requested"),
    scripted("evt-3", "message_completed"),
    scripted("evt-4", "turn_completed"),
)

COMPLETE_SCRIPT = (
    scripted("evt-1", "message_delta"),
    scripted("evt-2", "turn_completed"),
)


# --- descriptor negotiation -------------------------------------------------


def test_negotiate_descriptor_accepts_supported_version_with_no_disables() -> None:
    descriptor = negotiate_descriptor(requested_contract_version=CONTRACT_VERSION)
    assert descriptor.contract_version == CONTRACT_VERSION
    assert descriptor.disabled_capabilities == frozenset()


def test_negotiate_descriptor_rejects_unsupported_version() -> None:
    with pytest.raises(WorkerContractError, match="unsupported contract version"):
        negotiate_descriptor(requested_contract_version=2)


def test_negotiate_descriptor_disables_an_allowlisted_capability() -> None:
    descriptor = negotiate_descriptor(
        requested_contract_version=CONTRACT_VERSION, disable=["plan_proposed"]
    )
    assert descriptor.disabled_capabilities == frozenset({"plan_proposed"})


def test_negotiate_descriptor_rejects_disabling_a_non_allowlisted_capability() -> None:
    with pytest.raises(WorkerContractError, match="non-allowlisted"):
        negotiate_descriptor(
            requested_contract_version=CONTRACT_VERSION, disable=["turn_completed"]
        )


def test_capability_registry_matches_the_required_vocabulary() -> None:
    assert EVENT_KINDS == {
        "message_delta",
        "message_completed",
        "plan_proposed",
        "action_proposed",
        "wait_requested",
        "child_proposed",
        "artifact_proposed",
        "usage_updated",
        "turn_completed",
        "turn_failed",
        "diagnostic",
    }
    assert DISABLEABLE_CAPABILITIES == {
        "plan_proposed",
        "action_proposed",
        "child_proposed",
        "artifact_proposed",
    }
    assert DISABLEABLE_CAPABILITIES <= EVENT_KINDS


# --- descriptor identity / capabilities ---------------------------------------

_TRUTHFUL_DETERMINISTIC_CAPABILITIES = WorkerCapabilities(
    streaming=True,
    resume_session=True,
    cancel_turn=True,
    plan_events=True,
    action_proposals=True,
    child_proposals=True,
    structured_output=False,
    usage_events=True,
)


def test_negotiate_descriptor_identifies_the_deterministic_scripted_adapter() -> None:
    descriptor = negotiate_descriptor(requested_contract_version=CONTRACT_VERSION)
    assert descriptor.adapter_type == ADAPTER_TYPE_DETERMINISTIC
    assert descriptor.adapter_version == "1.0.0"
    assert descriptor.contract_version == CONTRACT_VERSION
    assert descriptor.protocol_name is None
    assert descriptor.protocol_version is None
    assert descriptor.required_host_capabilities == ()
    assert descriptor.trust_profile_id
    assert descriptor.capabilities == _TRUTHFUL_DETERMINISTIC_CAPABILITIES


def _descriptor_kwargs(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "adapter_type": ADAPTER_TYPE_DETERMINISTIC,
        "adapter_version": "1.0.0",
        "contract_version": CONTRACT_VERSION,
        "capabilities": _TRUTHFUL_DETERMINISTIC_CAPABILITIES,
        "trust_profile_id": "core.deterministic_scripted",
    }
    base.update(overrides)
    return base


@pytest.mark.parametrize(
    "overrides",
    [
        {"adapter_type": ""},
        {"adapter_type": "x" * 200},
        {"adapter_version": ""},
        {"contract_version": 0},
        {"trust_profile_id": ""},
        {"protocol_name": "bad\x00name"},
        {"protocol_version": "x" * 200},
        {"required_host_capabilities": ("api_key=leak",)},
        {"disabled_capabilities": frozenset({"turn_completed"})},
    ],
)
def test_worker_descriptor_validates_identifier_and_version_bounds(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(WorkerContractError):
        WorkerDescriptor(**_descriptor_kwargs(**overrides))


@pytest.mark.parametrize(
    "overrides",
    [
        {"protocol_name": "json-rpc"},
        {"protocol_version": "2.0"},
    ],
)
def test_worker_descriptor_requires_a_complete_protocol_pair(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(WorkerContractError, match="provided together"):
        WorkerDescriptor(**_descriptor_kwargs(**overrides))


# --- open/lineage ------------------------------------------------------------


def test_open_binds_a_deterministic_session_id_to_exact_lineage() -> None:
    first = WorkerAdapter().open(lineage=LINEAGE, script=())
    second = WorkerAdapter().open(lineage=LINEAGE, script=())
    third = WorkerAdapter().open(lineage=OTHER_LINEAGE, script=())
    assert first.session_id == second.session_id
    assert first.session_id != third.session_id


def test_reopening_the_same_lineage_on_one_adapter_is_refused() -> None:
    adapter = WorkerAdapter()
    adapter.open(lineage=LINEAGE, script=())
    with pytest.raises(WorkerStateError, match="already open"):
        adapter.open(lineage=LINEAGE, script=())


# --- host lineage bounds -------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "",
        "x" * 200,
        "ws\x00bad",
        "ws\x7fbad",
        "here is the api_key=sk-abcdef1234567890",
    ],
)
def test_host_lineage_rejects_invalid_field_values(value: str) -> None:
    with pytest.raises(WorkerContractError):
        HostLineage(
            workspace_id=value,
            run_id="run-1",
            run_step_id="step-1",
            attempt_id="att-1",
        )


def test_host_lineage_accepts_well_formed_fields() -> None:
    lineage = HostLineage(
        workspace_id="ws-1", run_id="run-1", run_step_id="step-1", attempt_id="att-1"
    )
    assert lineage.workspace_id == "ws-1"


# --- start/turn vocabulary ----------------------------------------------------


def test_start_normalizes_events_with_exact_lineage_and_stops_at_wait() -> None:
    adapter = WorkerAdapter()
    opened = adapter.open(lineage=LINEAGE, script=WAIT_SCRIPT)
    sink = Sink()

    result = adapter.start(session_id=opened.session_id, sink=sink)

    assert [e.kind for e in sink.events] == ["message_delta", "wait_requested"]
    assert result.state == "waiting"
    for event in sink.events:
        assert event.lineage == LINEAGE
        assert event.session_id == opened.session_id
        assert event.turn_id == result.turn_id


def test_start_requires_the_open_state() -> None:
    adapter = WorkerAdapter()
    opened = adapter.open(lineage=LINEAGE, script=COMPLETE_SCRIPT)
    adapter.start(session_id=opened.session_id, sink=Sink())
    with pytest.raises(WorkerStateError, match="must be 'open'"):
        adapter.start(session_id=opened.session_id, sink=Sink())


@pytest.mark.parametrize("kind", sorted(EVENT_KINDS))
def test_every_contract_event_kind_is_accepted(kind: str) -> None:
    adapter = WorkerAdapter()
    script = [scripted("evt-kind", kind)]
    if kind not in {"wait_requested", "turn_completed", "turn_failed"}:
        script.append(scripted("evt-terminal", "turn_completed"))
    opened = adapter.open(lineage=LINEAGE, script=script)
    sink = Sink()

    result = adapter.start(session_id=opened.session_id, sink=sink)

    assert sink.events[0].kind == kind
    expected_state = {
        "wait_requested": "waiting",
        "turn_failed": "failed",
    }.get(kind, "completed")
    assert result.state == expected_state


def test_unknown_event_kind_is_rejected() -> None:
    adapter = WorkerAdapter()
    opened = adapter.open(lineage=LINEAGE, script=(scripted("evt-1", "bogus_kind"),))
    with pytest.raises(WorkerEventRejected, match="unknown event kind"):
        adapter.start(session_id=opened.session_id, sink=Sink())


def test_disabled_capability_event_is_rejected() -> None:
    adapter = WorkerAdapter()
    opened = adapter.open(
        lineage=LINEAGE,
        script=(scripted("evt-1", "plan_proposed"),),
        disable=["plan_proposed"],
    )
    with pytest.raises(
        WorkerEventRejected, match="capability 'plan_proposed' is disabled"
    ):
        adapter.start(session_id=opened.session_id, sink=Sink())


@pytest.mark.parametrize(
    "event",
    [
        scripted("", "message_delta"),
        scripted("x" * 200, "message_delta"),
        scripted("evt-1", "message_delta", safe_summary=""),
        scripted("evt-1", "message_delta", safe_summary="x" * 600),
        scripted("evt-1", "message_delta", occurred_at_us=0),
        scripted("evt-1", "message_delta", external_session_id=""),
    ],
)
def test_bounded_field_violations_are_rejected(event: ScriptedWorkerEvent) -> None:
    adapter = WorkerAdapter()
    opened = adapter.open(lineage=LINEAGE, script=(event,))
    with pytest.raises(WorkerEventRejected):
        adapter.start(session_id=opened.session_id, sink=Sink())


@pytest.mark.parametrize(
    "event",
    [
        scripted("evt\x00bad", "message_delta"),
        scripted("api_key=sk-abcdef1234567890", "message_delta"),
        scripted("evt-1", "message_delta", external_session_id="external\x7fbad"),
        scripted(
            "evt-1",
            "message_delta",
            external_turn_id="access_token=abcdefghijklmnop",
        ),
    ],
)
def test_event_identifiers_refuse_control_and_secret_channels(
    event: ScriptedWorkerEvent,
) -> None:
    adapter = WorkerAdapter()
    opened = adapter.open(lineage=LINEAGE, script=(event,))
    with pytest.raises(WorkerEventRejected):
        adapter.start(session_id=opened.session_id, sink=Sink())


@pytest.mark.parametrize(
    "payload_ref",
    ["not-a-ref", "payload:", "payload:" + "x" * 300, "payload:has spaces"],
)
def test_invalid_payload_ref_is_rejected(payload_ref: str) -> None:
    adapter = WorkerAdapter()
    opened = adapter.open(
        lineage=LINEAGE,
        script=(scripted("evt-1", "message_delta", payload_ref=payload_ref),),
    )
    with pytest.raises(WorkerEventRejected, match="payload_ref"):
        adapter.start(session_id=opened.session_id, sink=Sink())


def test_valid_payload_ref_is_accepted() -> None:
    adapter = WorkerAdapter()
    opened = adapter.open(
        lineage=LINEAGE,
        script=(
            scripted("evt-1", "artifact_proposed", payload_ref="payload:abc-123"),
            scripted("evt-2", "turn_completed"),
        ),
    )
    sink = Sink()
    adapter.start(session_id=opened.session_id, sink=sink)
    assert sink.events[0].payload_ref == "payload:abc-123"


@pytest.mark.parametrize(
    "safe_summary",
    [
        "here is the api_key=sk-abcdef1234567890",
        "password: hunter2hunter2",
        "Authorization: Bearer abcdefgh12345678",
        "-----BEGIN PRIVATE KEY-----",
    ],
)
def test_secret_bearing_content_is_rejected(safe_summary: str) -> None:
    adapter = WorkerAdapter()
    opened = adapter.open(
        lineage=LINEAGE,
        script=(scripted("evt-1", "message_delta", safe_summary=safe_summary),),
    )
    with pytest.raises(WorkerEventRejected, match="secret"):
        adapter.start(session_id=opened.session_id, sink=Sink())


def test_lineage_substitution_in_an_event_is_rejected() -> None:
    adapter = WorkerAdapter()
    opened = adapter.open(
        lineage=LINEAGE,
        script=(scripted("evt-1", "message_delta", claimed_lineage=OTHER_LINEAGE),),
    )
    with pytest.raises(
        WorkerEventRejected, match="does not match the exact session lineage"
    ):
        adapter.start(session_id=opened.session_id, sink=Sink())


# --- dedupe / conflict ---------------------------------------------------------


def test_duplicate_identical_source_event_dedupes_without_resink() -> None:
    adapter = WorkerAdapter()
    script = (
        scripted("evt-1", "message_delta"),
        scripted("evt-1", "message_delta"),
        scripted("evt-2", "turn_completed"),
    )
    opened = adapter.open(lineage=LINEAGE, script=script)
    sink = Sink()
    adapter.start(session_id=opened.session_id, sink=sink)
    assert [e.source_event_id for e in sink.events] == ["evt-1", "evt-2"]


def test_conflicting_duplicate_source_event_quarantines_and_raises() -> None:
    adapter = WorkerAdapter()
    script = (
        scripted("evt-1", "message_delta", safe_summary="first"),
        scripted("evt-1", "message_delta", safe_summary="second"),
    )
    opened = adapter.open(lineage=LINEAGE, script=script)
    with pytest.raises(WorkerEventConflict, match="conflicting content"):
        adapter.start(session_id=opened.session_id, sink=Sink())

    with pytest.raises(WorkerStateError):
        adapter.cancel(session_id=opened.session_id)
    with pytest.raises(WorkerStateError):
        adapter.resume(session_id=opened.session_id, lineage=LINEAGE, sink=Sink())
    adapter.close(session_id=opened.session_id)  # close still works


# --- wait/resume ---------------------------------------------------------------


def test_resume_requires_the_waiting_state() -> None:
    adapter = WorkerAdapter()
    opened = adapter.open(lineage=LINEAGE, script=COMPLETE_SCRIPT)
    with pytest.raises(WorkerStateError, match="must be 'waiting'"):
        adapter.resume(session_id=opened.session_id, lineage=LINEAGE, sink=Sink())


def test_resume_with_the_wrong_lineage_is_refused() -> None:
    adapter = WorkerAdapter()
    opened = adapter.open(lineage=LINEAGE, script=WAIT_SCRIPT)
    adapter.start(session_id=opened.session_id, sink=Sink())
    with pytest.raises(WorkerStateError, match="does not match the exact lineage"):
        adapter.resume(session_id=opened.session_id, lineage=OTHER_LINEAGE, sink=Sink())


def test_resume_continues_the_remaining_script_to_completion() -> None:
    adapter = WorkerAdapter()
    opened = adapter.open(lineage=LINEAGE, script=WAIT_SCRIPT)
    first_sink = Sink()
    started = adapter.start(session_id=opened.session_id, sink=first_sink)

    second_sink = Sink()
    result = adapter.resume(
        session_id=opened.session_id, lineage=LINEAGE, sink=second_sink
    )

    assert [e.kind for e in second_sink.events] == [
        "message_completed",
        "turn_completed",
    ]
    assert result.turn_id == started.turn_id
    assert result.state == "completed"


# --- cancel ----------------------------------------------------------------


def test_cancel_before_the_turn_starts() -> None:
    adapter = WorkerAdapter()
    opened = adapter.open(lineage=LINEAGE, script=COMPLETE_SCRIPT)
    result = adapter.cancel(session_id=opened.session_id)
    assert result.turn_id is None
    with pytest.raises(WorkerStateError, match="must be 'open'"):
        adapter.start(session_id=opened.session_id, sink=Sink())


def test_cancel_while_a_turn_is_waiting_mid_script() -> None:
    adapter = WorkerAdapter()
    opened = adapter.open(lineage=LINEAGE, script=WAIT_SCRIPT)
    adapter.start(session_id=opened.session_id, sink=Sink())
    result = adapter.cancel(session_id=opened.session_id)
    assert result.turn_id is not None
    with pytest.raises(WorkerStateError):
        adapter.resume(session_id=opened.session_id, lineage=LINEAGE, sink=Sink())


def test_abort_signal_cancels_mid_drive_without_raising() -> None:
    adapter = WorkerAdapter()
    opened = adapter.open(lineage=LINEAGE, script=COMPLETE_SCRIPT)
    abort = Abort()
    sink = Sink()

    def abort_after_first_event(event: NormalizedWorkerEvent) -> None:
        sink(event)
        abort.set()

    result = adapter.start(
        session_id=opened.session_id, sink=abort_after_first_event, abort=abort
    )
    assert result.state == "cancelled"
    assert [event.kind for event in sink.events] == ["message_delta"]


# --- close / dispose ---------------------------------------------------------


def test_close_is_idempotent() -> None:
    adapter = WorkerAdapter()
    opened = adapter.open(lineage=LINEAGE, script=COMPLETE_SCRIPT)
    first = adapter.close(session_id=opened.session_id)
    second = adapter.close(session_id=opened.session_id)
    assert first == second


def test_dispose_closes_every_session_and_refuses_further_calls() -> None:
    adapter = WorkerAdapter()
    opened = adapter.open(lineage=LINEAGE, script=WAIT_SCRIPT)
    adapter.start(session_id=opened.session_id, sink=Sink())
    assert adapter.session_count == 1

    adapter.dispose()
    adapter.dispose()  # idempotent

    assert adapter.session_count == 0
    with pytest.raises(WorkerStateError, match="disposed"):
        adapter.open(lineage=OTHER_LINEAGE, script=())
    with pytest.raises(WorkerStateError, match="disposed"):
        adapter.resume(session_id=opened.session_id, lineage=LINEAGE, sink=Sink())
    with pytest.raises(WorkerStateError, match="disposed"):
        adapter.cancel(session_id=opened.session_id)
    with pytest.raises(WorkerStateError, match="disposed"):
        adapter.close(session_id=opened.session_id)


# --- fail-closed script completion ---------------------------------------------


def test_empty_script_fails_closed_instead_of_returning_running() -> None:
    adapter = WorkerAdapter()
    opened = adapter.open(lineage=LINEAGE, script=())
    with pytest.raises(WorkerScriptIncomplete):
        adapter.start(session_id=opened.session_id, sink=Sink())
    with pytest.raises(WorkerStateError):
        adapter.resume(session_id=opened.session_id, lineage=LINEAGE, sink=Sink())


def test_nonterminal_script_fails_closed_instead_of_returning_running() -> None:
    adapter = WorkerAdapter()
    opened = adapter.open(lineage=LINEAGE, script=(scripted("evt-1", "message_delta"),))
    with pytest.raises(WorkerScriptIncomplete):
        adapter.start(session_id=opened.session_id, sink=Sink())


# --- sink failure ----------------------------------------------------------------


class FailingSink:
    """Records calls, then raises on every invocation to simulate a broken sink."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, event: NormalizedWorkerEvent) -> None:
        self.calls += 1
        raise RuntimeError("sink exploded")


def test_sink_failure_quarantines_and_does_not_advance() -> None:
    adapter = WorkerAdapter()
    opened = adapter.open(lineage=LINEAGE, script=COMPLETE_SCRIPT)
    sink = FailingSink()

    with pytest.raises(WorkerSinkError) as excinfo:
        adapter.start(session_id=opened.session_id, sink=sink)

    assert isinstance(excinfo.value.__cause__, RuntimeError)
    assert sink.calls == 1
    with pytest.raises(WorkerStateError):
        adapter.resume(session_id=opened.session_id, lineage=LINEAGE, sink=Sink())


# --- adapter registry ------------------------------------------------------------


def test_registry_rejects_unknown_adapter_type() -> None:
    registry = WorkerAdapterRegistry()
    with pytest.raises(WorkerContractError, match="unknown adapter type"):
        registry.create(adapter_type="not_a_real_adapter")


def test_registry_creates_the_deterministic_adapter_and_its_descriptor() -> None:
    registry = WorkerAdapterRegistry()
    descriptor = registry.descriptor(adapter_type=ADAPTER_TYPE_DETERMINISTIC)
    assert descriptor.adapter_type == ADAPTER_TYPE_DETERMINISTIC
    assert descriptor.capabilities == _TRUTHFUL_DETERMINISTIC_CAPABILITIES

    adapter = registry.create(adapter_type=ADAPTER_TYPE_DETERMINISTIC)
    assert isinstance(adapter, WorkerAdapter)
    assert adapter.descriptor == descriptor


def test_registry_disabling_before_create_rejects_creation() -> None:
    registry = WorkerAdapterRegistry()
    registry.disable(ADAPTER_TYPE_DETERMINISTIC)
    with pytest.raises(WorkerContractError, match="disabled"):
        registry.create(adapter_type=ADAPTER_TYPE_DETERMINISTIC)


def test_registry_rejects_incompatible_host_contract_before_creation() -> None:
    registry = WorkerAdapterRegistry()
    with pytest.raises(WorkerContractError, match="incompatible"):
        registry.create(
            adapter_type=ADAPTER_TYPE_DETERMINISTIC, host_contract_version=2
        )


# --- no privileged handles ---------------------------------------------------


def test_worker_adapter_declares_no_privileged_handles() -> None:
    forbidden = (
        "db",
        "database",
        "connection",
        "dispatcher",
        "tool",
        "credential",
        "policy",
    )
    init_params = inspect.signature(WorkerAdapter.__init__).parameters
    for name in init_params:
        lowered = name.lower()
        assert not any(term in lowered for term in forbidden), name

    adapter = WorkerAdapter()
    for name, value in vars(adapter).items():
        lowered = name.lower()
        assert not any(term in lowered for term in forbidden), (name, value)
