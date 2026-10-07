"""C21 acceptance for the trigger operations, through the production application dispatcher.

What the operations promise, proved end to end over a real migrated workspace:

*Every mutation is fenced, audited and idempotent.* An honest replay answers from the stored
result and writes nothing; an altered replay under the same key is refused; a grant that a
takeover has outdated is refused before anything is written.

*Delivery is decided and recorded, and never mistaken for processing.* A stimulus is accepted
only into an `active` subscription with the declared type. Anything else is recorded as
dead-lettered with its reason. An accepted stimulus reads `unlinked`, and no job or run is ever
started by admission.

*Reads are bounded and scoped.* A health read names one trigger or pages a Workflow's triggers,
never more than the server maximum, and a trigger of another workspace reads as absent.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest
import test_application_audit_idempotency_migration as m1
import test_c21_trigger_telemetry_migration as m43
import test_t0693_workflow_application as wf
import test_v06_5_s0_mutation_foundation as s0
import test_workflow_runs_migration as m27
from omnivia_core_runtime.ownership.fencing import StaleGeneration
from omnivia_core_runtime.ownership.identity import FakeClock
from omnivia_core_runtime.ownership.lease import acquire_lease
from omnivia_core_runtime.service.application import (
    TRIGGER_FAMILY_PURPOSES,
    ApplicationDispatcher,
    build_trigger_application_dispatcher,
)
from omnivia_core_runtime.service.authorization import Grant
from omnivia_core_runtime.service.dispatch import Dispatcher
from omnivia_core_runtime.service.handlers.trigger import (
    TRIGGER_DECLARE_OPERATION,
    TRIGGER_HEALTH_OPERATION,
    TRIGGER_INGEST_OPERATION,
    TRIGGER_LIFECYCLE_OPERATION,
)
from omnivia_core_runtime.service.operations import SERVICE_OPERATIONS
from omnivia_core_runtime.storage.migrations import materialise_phase0_baseline
from omnivia_core_runtime.storage.retrieval import CONFIGURED_LOCAL_OWNER

from omnivia_core.contracts.v1 import (
    ErrorResponseEnvelope,
    RequestEnvelope,
    ResponseEnvelope,
    SuccessResponseEnvelope,
    get_operation_metadata,
)

WORKSPACE_ID = m27.WORKSPACE_ID
OTHER_WORKSPACE_ID = m1.OTHER_WORKSPACE_ID
INSTALLATION_ID = s0.INSTALLATION_ID
PRINCIPAL = CONFIGURED_LOCAL_OWNER
PROJECT_ID = m43.PROJECT_ID
WORKFLOW_ID = wf.WORKFLOW_ID
WORKFLOW_VERSION = wf.WORKFLOW_VERSION
PLAN_HASH = wf.plan().content_hash
EVENT_TYPE = m43.EVENT_TYPE
DIGEST_CONTRACT = m43.DIGEST_CONTRACT
DIGEST_CONFIG = m43.DIGEST_CONFIG
DIGEST_ENVELOPE = m43.DIGEST_ENVELOPE
DIGEST_OTHER = "sha256:" + "e" * 64

_READS = itertools.count(1)
DECLARATIONS = "omnivia_runtime_trigger_declarations"
SUBSCRIPTIONS = "omnivia_runtime_trigger_subscription_events"
OBSERVATIONS = "omnivia_runtime_trigger_observations"


@pytest.fixture
def owned(tmp_path: Path) -> Iterator[m1.Owned]:
    path = tmp_path / "workspace.sqlite"
    materialise_phase0_baseline(path)
    m1.bootstrap_and_migrate(path, workspace_id=WORKSPACE_ID)
    holder = m1.take_ownership(path)
    yield holder
    holder.connection.close()


def allocator(tag: str) -> Callable[[str], str]:
    counts: dict[str, int] = {}

    def allocate(prefix: str) -> str:
        counts[prefix] = counts.get(prefix, 0) + 1
        return f"{prefix}-{tag}-{counts[prefix]}"

    return allocate


def known_release(*, workflow_id: str, workflow_version: str) -> Any:
    """A release authority that holds exactly the one released Workflow version of this file."""
    if (workflow_id, workflow_version) == (WORKFLOW_ID, WORKFLOW_VERSION):
        return wf.release()
    return None


def fallback() -> Dispatcher:
    return Dispatcher.for_service_operations(
        Grant(
            principal=PRINCIPAL,
            workspaces=frozenset({WORKSPACE_ID}),
            operations=frozenset(SERVICE_OPERATIONS),
        )
    )


def trigger_dispatcher(
    holder: m1.Owned,
    *,
    resolve: Callable[..., Any] | None = known_release,
    workspace_id: str = WORKSPACE_ID,
    tag: str = "trg",
) -> ApplicationDispatcher:
    """The real trigger family, over one owned workspace, with only the release seam supplied."""
    return build_trigger_application_dispatcher(
        service=holder,
        principal_id=PRINCIPAL,
        installation_id=INSTALLATION_ID,
        workspace_id=workspace_id,
        fallback=fallback(),
        clock=FakeClock(wall=wf.WALL),
        allocate_identifier=allocator(tag),
        resolve_release=resolve,
    )


def request(
    operation: str,
    operation_input: Mapping[str, object],
    *,
    request_id: str,
    idempotency_key: str | None = None,
    workspace_id: str = WORKSPACE_ID,
) -> RequestEnvelope:
    return s0.envelope_for(
        get_operation_metadata(operation),
        operation_input=operation_input,
        request_id=request_id,
        correlation_id=f"cor-{request_id}",
        trace_id=f"trc-{request_id}",
        idempotency_key=idempotency_key,
        purpose=TRIGGER_FAMILY_PURPOSES[operation],
        workspace_id=workspace_id,
    )


def send(
    served: ApplicationDispatcher,
    operation: str,
    operation_input: Mapping[str, object],
    *,
    key: str,
    request_id: str | None = None,
    workspace_id: str = WORKSPACE_ID,
) -> ResponseEnvelope:
    return served.dispatch(
        request(
            operation,
            operation_input,
            request_id=request_id or f"req-{key}",
            idempotency_key=key,
            workspace_id=workspace_id,
        )
    )


def declaration_input(trigger_id: str = "trigger-1", **overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "project_id": PROJECT_ID,
        "workflow_id": WORKFLOW_ID,
        "trigger_id": trigger_id,
        "trigger_kind": "webhook",
        "workflow_version": WORKFLOW_VERSION,
        "plan_hash": PLAN_HASH,
        "event_type": EVENT_TYPE,
        "event_contract_digest": DIGEST_CONTRACT,
        "configuration_digest": DIGEST_CONFIG,
        "subscription_state": "active",
        "subscription_reason": "subscription.created",
    }
    values.update(overrides)
    return values


def declare(
    served: ApplicationDispatcher,
    *,
    trigger_id: str = "trigger-1",
    key: str | None = None,
    request_id: str | None = None,
    **overrides: object,
) -> ResponseEnvelope:
    return send(
        served,
        TRIGGER_DECLARE_OPERATION,
        declaration_input(trigger_id, **overrides),
        key=key or f"idem-declare-{trigger_id}",
        request_id=request_id,
    )


def move(
    served: ApplicationDispatcher,
    state: str,
    *,
    key: str,
    trigger_id: str = "trigger-1",
    reason: str = "operator.changed",
) -> ResponseEnvelope:
    return send(
        served,
        TRIGGER_LIFECYCLE_OPERATION,
        {
            "project_id": PROJECT_ID,
            "workflow_id": WORKFLOW_ID,
            "trigger_id": trigger_id,
            "subscription_state": state,
            "reason": reason,
        },
        key=key,
    )


def ingest(
    served: ApplicationDispatcher,
    *,
    key: str,
    trigger_id: str = "trigger-1",
    event_type: str = EVENT_TYPE,
    envelope_digest: str = DIGEST_ENVELOPE,
    event_key: str | None = None,
    occurred_at: str | None = None,
) -> ResponseEnvelope:
    payload: dict[str, object] = {
        "project_id": PROJECT_ID,
        "workflow_id": WORKFLOW_ID,
        "trigger_id": trigger_id,
        "event_id": f"event-{key}",
        "event_idempotency_key": event_key or f"ekey-{key}",
        "event_type": event_type,
        "envelope_digest": envelope_digest,
    }
    if occurred_at is not None:
        payload["occurred_at"] = occurred_at
    return send(served, TRIGGER_INGEST_OPERATION, payload, key=key)


def health(
    served: ApplicationDispatcher,
    *,
    trigger_id: str | None = None,
    workspace_id: str = WORKSPACE_ID,
    **extra: object,
) -> ResponseEnvelope:
    payload: dict[str, object] = {"project_id": PROJECT_ID, "workflow_id": WORKFLOW_ID}
    if trigger_id is not None:
        payload["trigger_id"] = trigger_id
    payload.update(extra)
    return served.dispatch(
        request(
            TRIGGER_HEALTH_OPERATION,
            payload,
            request_id=f"req-health-{next(_READS)}",
            workspace_id=workspace_id,
        )
    )


def result(response: ResponseEnvelope) -> Mapping[str, Any]:
    assert isinstance(response, SuccessResponseEnvelope), _error(response)
    return response.result


def code(response: ResponseEnvelope) -> str:
    assert isinstance(response, ErrorResponseEnvelope), response
    return response.error.code


def _error(response: ResponseEnvelope) -> str:
    if isinstance(response, ErrorResponseEnvelope):
        return f"{response.error.code}: {response.error.message}"
    return "success"


def rows(holder: m1.Owned, table: str) -> int:
    return int(holder.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def ledger(holder: m1.Owned) -> tuple[int, int, int]:
    return (rows(holder, DECLARATIONS), rows(holder, SUBSCRIPTIONS), rows(holder, OBSERVATIONS))


# --- the round trip ----------------------------------------------------------------


def test_declare_move_admit_and_read_health_round_trip(owned: m1.Owned) -> None:
    served = trigger_dispatcher(owned)

    declared = result(declare(served))
    assert declared["trigger_id"] == "trigger-1"
    assert (declared["declaration_sequence"], declared["subscription_state"]) == (1, "active")
    assert declared["subscription_sequence"] == 1

    paused = result(move(served, "paused", key="k-pause"))
    assert (paused["subscription_state"], paused["subscription_sequence"]) == ("paused", 2)

    held = result(ingest(served, key="k-held"))
    assert held["delivery_status"] == "dead_lettered"
    assert held["delivery_reason"] == "inactive_trigger"
    assert held["processing"] == "not_applicable"

    result(move(served, "active", key="k-resume"))
    admitted = result(ingest(served, key="k-admitted", occurred_at="2026-10-04T02:59:00.000000Z"))
    assert admitted["delivery_status"] == "accepted"
    assert admitted["processing"] == "unlinked"
    assert admitted["uncertainty"] == ["processing_unlinked"]
    assert "delivery_reason" not in admitted

    page = result(health(served, trigger_id="trigger-1"))
    (item,) = page["items"]
    assert item["subscription"]["state"] == "active"
    assert item["observation_total"] == 2
    assert item["delivery_counts"] == {"accepted": 1, "duplicate": 0, "dead_lettered": 1, "uncertain": 0}
    assert item["last_observation"]["delivery_status"] == "accepted"
    assert item["last_observation"]["processing"] == "unlinked"
    # The held stimulus carries no source time, so the trigger reports that too: uncertainty is
    # the union the store derives over the observations it returns.
    assert item["uncertainty"] == ["processing_unlinked", "source_time_unknown"]
    assert page["page"] == {}
    # Admission records and starts nothing: no workflow run exists for the stimulus.
    assert rows(owned, m27.RUNS) == 0


# --- replay and fencing ------------------------------------------------------------


def test_an_honest_replay_returns_the_stored_answer_and_writes_nothing(owned: m1.Owned) -> None:
    served = trigger_dispatcher(owned)
    first = declare(served, key="k-declare")
    before = ledger(owned)

    again = declare(served, key="k-declare", request_id="req-declare-replay")

    assert result(again) == result(first)
    assert again.metadata.audit_reference == first.metadata.audit_reference
    assert ledger(owned) == before


def test_an_altered_replay_under_the_same_key_is_refused_and_writes_nothing(owned: m1.Owned) -> None:
    served = trigger_dispatcher(owned)
    declare(served, key="k-declare")
    before = ledger(owned)

    refused = declare(served, key="k-declare", subscription_state="paused")

    assert code(refused) == "idempotency_conflict"
    assert ledger(owned) == before


def test_a_grant_outdated_by_a_takeover_is_refused_and_writes_nothing(owned: m1.Owned) -> None:
    served = trigger_dispatcher(owned)
    before = ledger(owned)
    taken = acquire_lease(
        owned.connection,
        m1.make_identity("svc-trigger-takeover", pid=4545),
        clock=FakeClock(),
        workspace_id=WORKSPACE_ID,
        holds_storage_lock=True,
        lock_mechanism="flock",
    )
    assert taken.fencing_generation > owned.generation

    with pytest.raises(StaleGeneration):
        declare(served, key="k-stale")

    assert ledger(owned) == before


# --- lifecycle, delivery and processing --------------------------------------------


def test_a_refused_lifecycle_transition_is_a_conflict_and_leaves_the_subscription(
    owned: m1.Owned,
) -> None:
    served = trigger_dispatcher(owned)
    declare(served, key="k-declare")
    assert result(move(served, "disabled", key="k-disable"))["subscription_state"] == "disabled"

    refused = move(served, "active", key="k-reenable")

    assert code(refused) == "conflict"
    page = result(health(served, trigger_id="trigger-1"))
    assert page["items"][0]["subscription"]["state"] == "disabled"


@pytest.mark.parametrize("state", ["unavailable", "disabled"])
def test_a_stimulus_to_an_unavailable_or_disabled_trigger_is_dead_lettered(
    owned: m1.Owned, state: str
) -> None:
    served = trigger_dispatcher(owned)
    declare(served, key="k-declare")
    move(served, state, key=f"k-{state}")

    dead = result(ingest(served, key=f"k-ingest-{state}"))

    assert dead["delivery_status"] == "dead_lettered"
    assert dead["delivery_reason"] == "inactive_trigger"
    assert dead["processing"] == "not_applicable"


def test_a_mismatched_event_type_is_dead_lettered_not_refused(owned: m1.Owned) -> None:
    served = trigger_dispatcher(owned)
    declare(served, key="k-declare")

    dead = result(ingest(served, key="k-other", event_type="com.example.invoice.paid"))

    assert dead["delivery_status"] == "dead_lettered"
    assert dead["delivery_reason"] == "event_type_mismatch"
    assert dead["processing"] == "not_applicable"


def test_a_repeat_of_an_accepted_stimulus_is_a_duplicate_and_an_altered_one_is_refused(
    owned: m1.Owned,
) -> None:
    served = trigger_dispatcher(owned)
    declare(served, key="k-declare")
    first = result(ingest(served, key="k-first", event_key="ekey-shared"))

    repeat = result(ingest(served, key="k-repeat", event_key="ekey-shared"))
    assert repeat["delivery_status"] == "duplicate"
    assert repeat["duplicate_of_observation_id"] == first["trigger_observation_id"]
    assert repeat["processing"] == "not_applicable"

    before = ledger(owned)
    altered = ingest(served, key="k-altered", event_key="ekey-shared", envelope_digest=DIGEST_OTHER)
    assert code(altered) == "idempotency_conflict"
    assert ledger(owned) == before


# --- declaration ---------------------------------------------------------------------


def test_a_declaration_names_only_a_release_the_authority_holds(owned: m1.Owned) -> None:
    assert code(declare(trigger_dispatcher(owned, resolve=None), key="k-absent")) == (
        "dependency_unavailable"
    )
    assert code(declare(trigger_dispatcher(owned, resolve=lambda **_: None), key="k-unknown")) == (
        "not_found"
    )
    served = trigger_dispatcher(owned)
    assert code(declare(served, key="k-plan", plan_hash="sha256:" + "f" * 64)) == "conflict"
    assert ledger(owned) == (0, 0, 0)


def test_a_trigger_keeps_its_kind_project_and_workflow_across_declarations(owned: m1.Owned) -> None:
    served = trigger_dispatcher(owned)
    declare(served, key="k-first")

    changed = declare(served, key="k-kind", trigger_kind="manual", configuration_digest=DIGEST_OTHER)

    assert code(changed) == "conflict"
    assert rows(owned, DECLARATIONS) == 1


def test_a_later_version_keeps_the_subscription_it_holds(owned: m1.Owned) -> None:
    served = trigger_dispatcher(owned)
    declare(served, key="k-first")

    # Same subscription state: a new numbered version, and no subscription step is written.
    later = result(declare(served, key="k-version-2", configuration_digest=DIGEST_OTHER))
    assert (later["declaration_sequence"], later["subscription_state"]) == (2, "active")
    assert later["subscription_sequence"] == 1
    assert ledger(owned) == (2, 1, 0)

    # A change of state is a step, and the transition table decides it as it does a lifecycle move.
    moved = result(
        declare(
            served,
            key="k-version-3",
            subscription_state="paused",
            subscription_reason="operator.paused",
        )
    )
    assert (moved["declaration_sequence"], moved["subscription_state"]) == (3, "paused")
    assert moved["subscription_sequence"] == 2


def test_a_stimulus_carrying_a_raw_payload_is_refused_not_recorded(owned: m1.Owned) -> None:
    served = trigger_dispatcher(owned)
    declare(served, key="k-declare")
    payload = {
        "project_id": PROJECT_ID,
        "workflow_id": WORKFLOW_ID,
        "trigger_id": "trigger-1",
        "event_id": "event-raw",
        "event_idempotency_key": "ekey-raw",
        "event_type": EVENT_TYPE,
        "envelope_digest": DIGEST_ENVELOPE,
        "payload": {"order": 7},
    }

    refused = send(served, TRIGGER_INGEST_OPERATION, payload, key="k-raw")

    assert code(refused) == "invalid_request"
    assert ledger(owned) == (1, 1, 0)


def test_a_malformed_request_is_refused_before_anything_is_written(owned: m1.Owned) -> None:
    served = trigger_dispatcher(owned)

    assert code(declare(served, key="k-kind", trigger_kind="cron")) == "invalid_request"
    assert code(declare(served, key="k-plan-shape", plan_hash="sha256:XYZ")) == "invalid_request"
    assert code(ingest(served, key="k-type", event_type="not a type")) == "invalid_request"
    assert code(ingest(served, key="k-when", occurred_at="2026-13-40T00:00:00Z")) == "invalid_request"
    assert ledger(owned) == (0, 0, 0)


# --- bounded reads and scope ---------------------------------------------------------


def test_health_reads_are_bounded_and_page_by_trigger(owned: m1.Owned) -> None:
    served = trigger_dispatcher(owned)
    for trigger_id in ("trigger-a", "trigger-b", "trigger-c"):
        declare(served, trigger_id=trigger_id, key=f"k-{trigger_id}")

    first = result(health(served, limit=2))
    assert [item["trigger_id"] for item in first["items"]] == ["trigger-a", "trigger-b"]
    assert first["page"] == {"continuation_token": "trigger-b"}

    second = result(health(served, limit=2, page={"continuation_token": "trigger-b"}))
    assert [item["trigger_id"] for item in second["items"]] == ["trigger-c"]
    assert second["page"] == {}

    everything = result(health(served, limit=500))
    assert len(everything["items"]) == 3

    assert code(health(served, observation_limit=21)) == "invalid_request"
    assert code(health(served, trigger_id="trigger-a", limit=2)) == "invalid_request"
    assert code(health(served, trigger_id="trigger-nowhere")) == "not_found"
    # A present page must name the token it resumes from; `{}` states nothing to continue from.
    assert code(health(served, page={})) == "invalid_request"


def test_the_observation_window_is_bounded_per_trigger(owned: m1.Owned) -> None:
    served = trigger_dispatcher(owned)
    declare(served, key="k-declare")
    for number in range(3):
        ingest(served, key=f"k-admit-{number}", event_key=f"ekey-{number}")

    window = result(health(served, trigger_id="trigger-1", observation_limit=2))
    (item,) = window["items"]
    assert len(item["observations"]) == 2
    assert item["observation_total"] == 3
    assert item["observations"][0]["observation_sequence"] == 3


def test_another_workspace_reads_the_trigger_as_absent(owned: m1.Owned) -> None:
    served = trigger_dispatcher(owned)
    declare(served, key="k-declare")

    foreign = trigger_dispatcher(owned, workspace_id=OTHER_WORKSPACE_ID, tag="foreign")

    assert code(health(foreign, trigger_id="trigger-1", workspace_id=OTHER_WORKSPACE_ID)) == "not_found"
    empty = result(health(foreign, workspace_id=OTHER_WORKSPACE_ID))
    assert empty["items"] == []
    assert empty["page"] == {}
