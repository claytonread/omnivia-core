"""Trigger telemetry over migration 0043, and nothing above it.

0043 adds four append-only tables: trigger declarations, subscription events, trigger
observations and wait-signal observations. This module is their writer and their bounded
reads. It starts no work, schedules nothing, resolves no wait and decides no
acceptance; those belong to the writer that later owns each stimulus, and this module
records what that writer decided in the fenced transaction it already holds.

Writes
------

Every write is issued into a transaction the caller already fenced, either through
:func:`trigger_telemetry_writer` (which opens one) or :func:`transaction_local_telemetry_writer`
(for a caller inside one). Sequence numbers are allocated here and enforced by 0043's
triggers; a caller never names its own position in a durable history.

A replay is decided on the stored row in every field a caller supplies. The same
identifier with the same input returns the stored row and writes nothing; the same
identifier with different input refuses, so a caller-minted id can never be reused to
rewrite history. An idempotency key already accepted for a trigger refuses a second
acceptance and refuses a duplicate whose envelope changed.

Reads
-----

Every read is bounded: a window of at most :data:`MAX_OBSERVATION_WINDOW` observations
and a page of at most :data:`MAX_TRIGGER_PAGE` triggers. A projection is keyed by
Project, Workflow and trigger, and a trigger that is not bound to that Project and
Workflow reads as absent, never as someone else's.

*Delivery is not processing.* An observation's delivery status says what happened at the
door. Processing is read from the job and run ledgers through the observation's link,
and an accepted observation with no link, or a link the ledgers know nothing about, reads
as ``unlinked`` or ``unknown``. Completion is never inferred from acceptance.

*Uncertainty is derived, never stored as a guess.* The codes in :data:`UNCERTAINTY_CODES`
are computed on read from what the ledgers hold.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from typing import Final

from omnivia_core_runtime.ownership.fencing import fenced_transaction
from omnivia_core_runtime.ownership.identity import ServiceInstanceIdentity
from omnivia_core_runtime.storage.connection import StorageError

__all__ = [
    "DEAD_LETTER_REASONS",
    "DELIVERY_STATUSES",
    "MAX_OBSERVATION_WINDOW",
    "MAX_TRIGGER_PAGE",
    "SUBSCRIPTION_STATES",
    "TRIGGER_KINDS",
    "UNCERTAINTY_CODES",
    "UNCERTAIN_REASONS",
    "WAIT_DEAD_LETTER_REASONS",
    "ObservationView",
    "SubscriptionEvent",
    "SubscriptionStatus",
    "TriggerDeclaration",
    "TriggerFailure",
    "TriggerObservation",
    "TriggerTelemetry",
    "TriggerTelemetryPage",
    "TriggerTelemetryWriter",
    "WaitSignalObservation",
    "WaitSignalTelemetry",
    "list_workflow_trigger_telemetry",
    "read_accepted_trigger_observation",
    "read_trigger_declaration",
    "read_trigger_telemetry",
    "read_wait_signal_observation",
    "read_wait_signal_telemetry",
    "transaction_local_telemetry_writer",
    "trigger_telemetry_writer",
]

_DECLARATIONS: Final = "omnivia_runtime_trigger_declarations"
_SUBSCRIPTIONS: Final = "omnivia_runtime_trigger_subscription_events"
_OBSERVATIONS: Final = "omnivia_runtime_trigger_observations"
_WAIT_SIGNALS: Final = "omnivia_runtime_wait_signal_observations"

#: Restated from 0043's CHECK constraints, which remain the authority. A value outside
#: these refuses here with a message, and would refuse there regardless.
TRIGGER_KINDS: Final[tuple[str, ...]] = (
    "manual",
    "schedule",
    "webhook",
    "cloudevent",
    "catalogue_event",
)
SUBSCRIPTION_STATES: Final[tuple[str, ...]] = (
    "active",
    "paused",
    "unavailable",
    "disabled",
)
DELIVERY_STATUSES: Final[tuple[str, ...]] = (
    "accepted",
    "duplicate",
    "dead_lettered",
    "uncertain",
)
DEAD_LETTER_REASONS: Final[tuple[str, ...]] = (
    "inactive_trigger",
    "event_type_mismatch",
    "trigger_cooldown",
    "trigger_debounce",
    "no_automation_for_trigger",
    "inactive_automation",
    "automation_concurrency",
    "payload_rejected",
)
WAIT_DEAD_LETTER_REASONS: Final[tuple[str, ...]] = (
    "wait_already_resolved",
    "deadline_passed",
    "contract_rejected",
    "payload_rejected",
)
UNCERTAIN_REASONS: Final[tuple[str, ...]] = (
    "delivery_unconfirmed",
    "recovery_interrupted",
)

#: What a subscription may move to from each state. `disabled` is terminal, and
#: nothing returns to the start: a subscription begins `active` or `paused`.
_INITIAL_STATES: Final = frozenset({"active", "paused"})
_TRANSITIONS: Final[Mapping[str, frozenset[str]]] = {
    "active": frozenset({"paused", "unavailable", "disabled"}),
    "paused": frozenset({"active", "disabled"}),
    "unavailable": frozenset({"active", "paused", "disabled"}),
    "disabled": frozenset(),
}

#: Derived on read; never stored.
UNCERTAINTY_CODES: Final[tuple[str, ...]] = (
    "delivery_uncertain",
    "job_run_disagree",
    "job_terminal_provenance_unrecorded",
    "linked_run_workflow_mismatch",
    "no_subscription_recorded",
    "processing_unknown",
    "processing_unlinked",
    "resolution_not_recorded",
    "source_time_unknown",
    "subscription_unavailable",
)

MAX_OBSERVATION_WINDOW: Final = 100
DEFAULT_OBSERVATION_WINDOW: Final = 20
MAX_TRIGGER_PAGE: Final = 50
DEFAULT_TRIGGER_PAGE: Final = 25
MAX_PAGE_OBSERVATION_WINDOW: Final = 20

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_JOB_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}")
_EVENT_TYPE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}")
_WORKFLOW_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}")
_VERSION = re.compile(r"[0-9][0-9A-Za-z.+-]{0,127}")
_CODE = re.compile(r"[a-z][a-z0-9_.]{0,127}")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")

_TERMINAL = frozenset({"succeeded", "failed", "cancelled", "partially_completed"})
_RUN_ROLLUP: Final[Mapping[str, str]] = {
    "admitted": "pending",
    "running": "in_progress",
    "waiting": "in_progress",
    "succeeded": "succeeded",
    "partially_completed": "partially_completed",
    "failed": "failed",
    "cancelled": "cancelled",
    "uncertain": "uncertain",
}
_JOB_ROLLUP: Final[Mapping[str, str]] = {
    "queued": "pending",
    "running": "in_progress",
    "succeeded": "succeeded",
    "failed": "failed",
    "cancelled": "cancelled",
}


# --- records -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TriggerDeclaration:
    """One immutable version of a trigger, exactly as 0043 holds it.

    A description, not a grant: it names no principal, scope or capability.
    """

    trigger_declaration_id: str
    trigger_id: str
    declaration_sequence: int
    trigger_kind: str
    project_id: str
    workflow_id: str
    workflow_version: str
    plan_hash: str
    event_type: str
    event_contract_digest: str
    configuration_digest: str
    declared_at_us: int
    audit_ref: str


@dataclass(frozen=True, slots=True)
class SubscriptionEvent:
    """One step of a trigger's subscription lifecycle."""

    subscription_event_id: str
    trigger_id: str
    declaration_sequence: int
    subscription_sequence: int
    subscription_state: str
    reason: str
    observed_at_us: int
    audit_ref: str


@dataclass(frozen=True, slots=True)
class TriggerObservation:
    """One stimulus a declared trigger received, and what happened to it at the door."""

    trigger_observation_id: str
    trigger_id: str
    declaration_sequence: int
    observation_sequence: int
    event_id: str
    idempotency_key: str
    event_type: str
    envelope_digest: str
    occurred_at_us: int | None
    observed_at_us: int
    delivery_status: str
    delivery_reason: str | None
    duplicate_of_observation_id: str | None
    job_id: str | None
    run_id: str | None
    audit_ref: str


@dataclass(frozen=True, slots=True)
class WaitSignalObservation:
    """One signal delivered to an `external_signal` wait."""

    wait_signal_observation_id: str
    wait_id: str
    observation_sequence: int
    event_id: str
    envelope_digest: str
    occurred_at_us: int | None
    observed_at_us: int
    delivery_status: str
    delivery_reason: str | None
    duplicate_of_observation_id: str | None
    audit_ref: str


@dataclass(frozen=True, slots=True)
class SubscriptionStatus:
    """The current subscription state, or `state=None` when none was ever recorded."""

    state: str | None
    reason: str | None
    observed_at_us: int | None
    subscription_sequence: int


@dataclass(frozen=True, slots=True)
class TriggerFailure:
    """One failure the telemetry can state, with where it was read from.

    `source` is `delivery`, `job` or `run`. `reason` is a code, never free text.
    """

    trigger_observation_id: str
    source: str
    reason: str


@dataclass(frozen=True, slots=True)
class ObservationView:
    """An observation beside the processing evidence its link permits.

    `processing` is one of `not_applicable`, `unlinked`, `unknown`, `pending`,
    `in_progress`, `succeeded`, `partially_completed`, `failed`, `cancelled` or
    `uncertain`. Only `succeeded` after the ledger says so; delivery acceptance alone
    never produces it.
    """

    observation: TriggerObservation
    processing: str
    job_state: str | None
    run_status: str | None
    uncertainty: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TriggerTelemetry:
    """The bounded read model for one trigger.

    `delivery_counts` and `failures` cover the returned window only;
    `observation_total` is the trigger's whole count, taken from the highest
    contiguous sequence rather than counted.
    """

    declaration: TriggerDeclaration
    subscription: SubscriptionStatus
    last_observation: ObservationView | None
    delivery_status: str | None
    observation_total: int
    window: tuple[ObservationView, ...]
    delivery_counts: Mapping[str, int]
    failures: tuple[TriggerFailure, ...]
    uncertainty: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TriggerTelemetryPage:
    """A bounded page of triggers of one Workflow, ordered by trigger id."""

    items: tuple[TriggerTelemetry, ...]
    next_after_trigger_id: str | None


@dataclass(frozen=True, slots=True)
class WaitSignalTelemetry:
    """The bounded read model for signals delivered to one wait.

    `wait_status` is the wait's own state (`pending`, `resolved`, `expired`,
    `cancelled`), read from 0018. An accepted signal does not make it `resolved`.
    """

    wait_id: str
    run_id: str
    wait_status: str
    run_status: str | None
    last_observation: WaitSignalObservation | None
    delivery_status: str | None
    observation_total: int
    window: tuple[WaitSignalObservation, ...]
    delivery_counts: Mapping[str, int]
    uncertainty: tuple[str, ...]


# --- validation ----------------------------------------------------------------------


def _text(value: object, pattern: re.Pattern[str], label: str) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise StorageError(f"{label} is malformed")
    return value


def _optional_text(value: object, pattern: re.Pattern[str], label: str) -> str | None:
    return None if value is None else _text(value, pattern, label)


def _optional_time(value: object, label: str) -> int | None:
    return None if value is None else _time(value, label)


def _time(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise StorageError(f"{label} must be a positive integer of microseconds")
    return value


def _one_of(value: object, allowed: tuple[str, ...], label: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise StorageError(f"{label} must be one of {', '.join(allowed)}")
    return value


def _window(value: int, ceiling: int, label: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= ceiling
    ):
        raise StorageError(f"{label} must be between 1 and {ceiling}")
    return value


def _delivery(
    status: object, reason: object, vocabulary: tuple[str, ...]
) -> tuple[str, str | None]:
    status = _one_of(status, DELIVERY_STATUSES, "delivery_status")
    if status == "dead_lettered":
        return status, _one_of(reason, vocabulary, "delivery_reason")
    if status == "uncertain":
        return status, _one_of(reason, UNCERTAIN_REASONS, "delivery_reason")
    if reason is not None:
        raise StorageError(f"a {status} delivery carries no reason")
    return status, None


def _require_row(
    connection: sqlite3.Connection,
    table: str,
    workspace_id: str,
    column: str,
    value: str,
) -> None:
    found = connection.execute(
        f"SELECT 1 FROM {table} WHERE workspace_id = ? AND {column} = ?",
        (workspace_id, value),
    ).fetchone()
    if found is None:
        raise StorageError(f"{column} {value!r} is not recorded in this workspace")


def _require_audit(
    connection: sqlite3.Connection, workspace_id: str, audit_ref: str
) -> None:
    found = connection.execute(
        "SELECT 1 FROM omnivia_application_audit_events "
        "WHERE audit_ref = ? AND workspace_id = ?",
        (audit_ref, workspace_id),
    ).fetchone()
    if found is None:
        raise StorageError(f"audit_ref {audit_ref!r} is not recorded in this workspace")


# --- writer --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TriggerTelemetryWriter:
    """The telemetry writes, issued into a transaction that is already open."""

    connection: sqlite3.Connection
    workspace_id: str

    def declare_trigger(
        self,
        *,
        trigger_declaration_id: str,
        trigger_id: str,
        trigger_kind: str,
        project_id: str,
        workflow_id: str,
        workflow_version: str,
        plan_hash: str,
        event_type: str,
        event_contract_digest: str,
        configuration_digest: str,
        declared_at_us: int,
        audit_ref: str,
    ) -> TriggerDeclaration:
        """Append one numbered declaration of a trigger.

        The first declaration fixes the trigger's kind, Project and Workflow; later
        ones may change only the Workflow version and plan, event contract and
        configuration. Every declaration is a new numbered version, changed or not. A
        replay of the same declaration id with the same input returns the stored
        declaration.
        """
        requested = TriggerDeclaration(
            trigger_declaration_id=_text(
                trigger_declaration_id, _ID, "trigger_declaration_id"
            ),
            trigger_id=_text(trigger_id, _ID, "trigger_id"),
            declaration_sequence=0,
            trigger_kind=_one_of(trigger_kind, TRIGGER_KINDS, "trigger_kind"),
            project_id=_text(project_id, _ID, "project_id"),
            workflow_id=_text(workflow_id, _WORKFLOW_ID, "workflow_id"),
            workflow_version=_text(workflow_version, _VERSION, "workflow_version"),
            plan_hash=_text(plan_hash, _DIGEST, "plan_hash"),
            event_type=_text(event_type, _EVENT_TYPE, "event_type"),
            event_contract_digest=_text(
                event_contract_digest, _DIGEST, "event_contract_digest"
            ),
            configuration_digest=_text(
                configuration_digest, _DIGEST, "configuration_digest"
            ),
            declared_at_us=_time(declared_at_us, "declared_at_us"),
            audit_ref=_text(audit_ref, _ID, "audit_ref"),
        )
        stored = _declaration_by_id(
            self.connection, self.workspace_id, requested.trigger_declaration_id
        )
        if stored is not None:
            if _without_sequence(stored) != _without_sequence(requested):
                raise StorageError(
                    f"trigger declaration {stored.trigger_declaration_id!r} was already "
                    "recorded on different terms"
                )
            return stored
        _require_audit(self.connection, self.workspace_id, requested.audit_ref)
        current = _latest_declaration(
            self.connection, self.workspace_id, requested.trigger_id
        )
        if current is not None and (
            current.trigger_kind,
            current.project_id,
            current.workflow_id,
        ) != (requested.trigger_kind, requested.project_id, requested.workflow_id):
            raise StorageError(
                f"trigger {requested.trigger_id!r} is bound to another kind, Project "
                "or Workflow"
            )
        recorded = replace(
            requested,
            declaration_sequence=1
            if current is None
            else current.declaration_sequence + 1,
        )
        _insert(self.connection, _DECLARATIONS, self.workspace_id, _fields(recorded))
        return recorded

    def record_subscription_state(
        self,
        *,
        subscription_event_id: str,
        trigger_id: str,
        subscription_state: str,
        reason: str,
        observed_at_us: int,
        audit_ref: str,
    ) -> SubscriptionEvent:
        """Append one lifecycle step against the trigger's latest declaration."""
        subscription_event_id = _text(
            subscription_event_id, _ID, "subscription_event_id"
        )
        trigger_id = _text(trigger_id, _ID, "trigger_id")
        state = _one_of(subscription_state, SUBSCRIPTION_STATES, "subscription_state")
        reason = _text(reason, _CODE, "reason")
        observed = _time(observed_at_us, "observed_at_us")
        audit_ref = _text(audit_ref, _ID, "audit_ref")
        stored = _subscription_by_id(
            self.connection, self.workspace_id, subscription_event_id
        )
        if stored is not None:
            if (stored.trigger_id, stored.subscription_state, stored.reason) != (
                trigger_id,
                state,
                reason,
            ) or (stored.observed_at_us, stored.audit_ref) != (observed, audit_ref):
                raise StorageError(
                    f"subscription event {subscription_event_id!r} was already recorded "
                    "on different terms"
                )
            return stored
        declaration = _latest_declaration(
            self.connection, self.workspace_id, trigger_id
        )
        if declaration is None:
            raise StorageError(
                f"trigger {trigger_id!r} is not declared in this workspace"
            )
        _require_audit(self.connection, self.workspace_id, audit_ref)
        previous = _latest_subscription(self.connection, self.workspace_id, trigger_id)
        if previous is None:
            if state not in _INITIAL_STATES:
                raise StorageError("a subscription starts active or paused")
        elif state not in _TRANSITIONS[previous.subscription_state]:
            raise StorageError(
                f"a subscription cannot move from {previous.subscription_state} to {state}"
            )
        if previous is not None and observed < previous.observed_at_us:
            raise StorageError("subscription time must not regress")
        recorded = SubscriptionEvent(
            subscription_event_id=subscription_event_id,
            trigger_id=trigger_id,
            declaration_sequence=declaration.declaration_sequence,
            subscription_sequence=1
            if previous is None
            else previous.subscription_sequence + 1,
            subscription_state=state,
            reason=reason,
            observed_at_us=observed,
            audit_ref=audit_ref,
        )
        _insert(self.connection, _SUBSCRIPTIONS, self.workspace_id, _fields(recorded))
        return recorded

    def record_observation(
        self,
        *,
        trigger_observation_id: str,
        trigger_id: str,
        event_id: str,
        idempotency_key: str,
        event_type: str,
        envelope_digest: str,
        occurred_at_us: int | None,
        observed_at_us: int,
        delivery_status: str,
        delivery_reason: str | None = None,
        duplicate_of_observation_id: str | None = None,
        job_id: str | None = None,
        run_id: str | None = None,
        audit_ref: str,
    ) -> TriggerObservation:
        """Append one stimulus a declared trigger received, against its latest declaration.

        `job_id` and `run_id` link an accepted stimulus to the work it started, and are
        the only route to processing status. Neither is required: an unlinked accepted
        stimulus reads as unlinked, not as done.
        """
        status, reason = _delivery(
            delivery_status, delivery_reason, DEAD_LETTER_REASONS
        )
        requested = TriggerObservation(
            trigger_observation_id=_text(
                trigger_observation_id, _ID, "trigger_observation_id"
            ),
            trigger_id=_text(trigger_id, _ID, "trigger_id"),
            declaration_sequence=0,
            observation_sequence=0,
            event_id=_text(event_id, _ID, "event_id"),
            idempotency_key=_text(idempotency_key, _ID, "idempotency_key"),
            event_type=_text(event_type, _EVENT_TYPE, "event_type"),
            envelope_digest=_text(envelope_digest, _DIGEST, "envelope_digest"),
            occurred_at_us=_optional_time(occurred_at_us, "occurred_at_us"),
            observed_at_us=_time(observed_at_us, "observed_at_us"),
            delivery_status=status,
            delivery_reason=reason,
            duplicate_of_observation_id=_optional_text(
                duplicate_of_observation_id, _ID, "duplicate_of_observation_id"
            ),
            job_id=_optional_text(job_id, _JOB_ID, "job_id"),
            run_id=_optional_text(run_id, _ID, "run_id"),
            audit_ref=_text(audit_ref, _ID, "audit_ref"),
        )
        if (status == "duplicate") != (
            requested.duplicate_of_observation_id is not None
        ):
            raise StorageError(
                "duplicate_of_observation_id is set exactly for a duplicate"
            )
        if status != "accepted" and (requested.job_id or requested.run_id):
            raise StorageError("only an accepted observation may link a job or run")
        stored = _observation_by_id(
            self.connection, self.workspace_id, requested.trigger_observation_id
        )
        if stored is not None:
            if _without_position(stored) != _without_position(requested):
                raise StorageError(
                    f"trigger observation {stored.trigger_observation_id!r} was already "
                    "recorded on different terms"
                )
            return stored
        declaration = _latest_declaration(
            self.connection, self.workspace_id, requested.trigger_id
        )
        if declaration is None:
            raise StorageError(f"trigger {requested.trigger_id!r} is not declared")
        _require_audit(self.connection, self.workspace_id, requested.audit_ref)
        if requested.job_id is not None:
            _require_row(
                self.connection,
                "omnivia_job_application_metadata",
                self.workspace_id,
                "job_id",
                requested.job_id,
            )
        if requested.run_id is not None:
            _require_row(
                self.connection,
                "omnivia_runtime_runs",
                self.workspace_id,
                "run_id",
                requested.run_id,
            )
        self._check_equivalence(requested)
        previous = _latest_observation_sequence(
            self.connection, self.workspace_id, requested.trigger_id
        )
        recorded = replace(
            requested,
            declaration_sequence=declaration.declaration_sequence,
            observation_sequence=previous + 1,
        )
        _insert(self.connection, _OBSERVATIONS, self.workspace_id, _fields(recorded))
        return recorded

    def _check_equivalence(self, requested: TriggerObservation) -> None:
        accepted = self.connection.execute(
            f"SELECT trigger_observation_id, envelope_digest FROM {_OBSERVATIONS} "
            "WHERE workspace_id = ? AND trigger_id = ? AND idempotency_key = ? "
            "AND delivery_status = 'accepted'",
            (self.workspace_id, requested.trigger_id, requested.idempotency_key),
        ).fetchone()
        if requested.delivery_status == "accepted" and accepted is not None:
            raise StorageError(
                f"idempotency key {requested.idempotency_key!r} was already accepted; "
                "record the repeat as a duplicate"
            )
        if requested.delivery_status == "duplicate":
            if accepted is None or accepted[0] != requested.duplicate_of_observation_id:
                raise StorageError(
                    "a duplicate must name the accepted observation it repeats"
                )
            if accepted[1] != requested.envelope_digest:
                raise StorageError(
                    f"idempotency key {requested.idempotency_key!r} was reused with "
                    "different content"
                )

    def record_wait_signal(
        self,
        *,
        wait_signal_observation_id: str,
        wait_id: str,
        event_id: str,
        envelope_digest: str,
        occurred_at_us: int | None,
        observed_at_us: int,
        delivery_status: str,
        delivery_reason: str | None = None,
        duplicate_of_observation_id: str | None = None,
        audit_ref: str,
    ) -> WaitSignalObservation:
        """Append one signal delivered to an `external_signal` wait.

        Records delivery only. Resolving the wait remains `resolve_runtime_wait`'s
        decision, made in the same fenced transaction by the caller.
        """
        status, reason = _delivery(
            delivery_status, delivery_reason, WAIT_DEAD_LETTER_REASONS
        )
        requested = WaitSignalObservation(
            wait_signal_observation_id=_text(
                wait_signal_observation_id, _ID, "wait_signal_observation_id"
            ),
            wait_id=_text(wait_id, _ID, "wait_id"),
            observation_sequence=0,
            event_id=_text(event_id, _ID, "event_id"),
            envelope_digest=_text(envelope_digest, _DIGEST, "envelope_digest"),
            occurred_at_us=_optional_time(occurred_at_us, "occurred_at_us"),
            observed_at_us=_time(observed_at_us, "observed_at_us"),
            delivery_status=status,
            delivery_reason=reason,
            duplicate_of_observation_id=_optional_text(
                duplicate_of_observation_id, _ID, "duplicate_of_observation_id"
            ),
            audit_ref=_text(audit_ref, _ID, "audit_ref"),
        )
        if (status == "duplicate") != (
            requested.duplicate_of_observation_id is not None
        ):
            raise StorageError(
                "duplicate_of_observation_id is set exactly for a duplicate"
            )
        stored = _wait_signal_by_id(
            self.connection, self.workspace_id, requested.wait_signal_observation_id
        )
        if stored is not None:
            if _without_position(stored) != _without_position(requested):
                raise StorageError(
                    f"wait signal observation {stored.wait_signal_observation_id!r} was "
                    "already recorded on different terms"
                )
            return stored
        wait = self.connection.execute(
            "SELECT kind FROM omnivia_runtime_waits WHERE workspace_id = ? AND wait_id = ?",
            (self.workspace_id, requested.wait_id),
        ).fetchone()
        if wait is None:
            raise StorageError(
                f"wait {requested.wait_id!r} is not recorded in this workspace"
            )
        if wait[0] != "external_signal":
            raise StorageError("only an external_signal wait takes signal observations")
        _require_audit(self.connection, self.workspace_id, requested.audit_ref)
        accepted = self.connection.execute(
            f"SELECT wait_signal_observation_id, event_id, envelope_digest FROM {_WAIT_SIGNALS} "
            "WHERE workspace_id = ? AND wait_id = ? AND delivery_status = 'accepted'",
            (self.workspace_id, requested.wait_id),
        ).fetchone()
        if status == "accepted" and accepted is not None:
            raise StorageError(f"wait {requested.wait_id!r} already accepted a signal")
        if status == "duplicate" and (
            accepted is None
            or accepted[0] != requested.duplicate_of_observation_id
            or accepted[1] != requested.event_id
            or accepted[2] != requested.envelope_digest
        ):
            raise StorageError(
                "a duplicate signal must repeat the accepted signal of the same wait unchanged"
            )
        previous = self.connection.execute(
            f"SELECT COALESCE(MAX(observation_sequence), 0) FROM {_WAIT_SIGNALS} "
            "WHERE workspace_id = ? AND wait_id = ?",
            (self.workspace_id, requested.wait_id),
        ).fetchone()
        recorded = replace(requested, observation_sequence=int(previous[0]) + 1)
        _insert(self.connection, _WAIT_SIGNALS, self.workspace_id, _fields(recorded))
        return recorded


def transaction_local_telemetry_writer(
    connection: sqlite3.Connection, *, workspace_id: str
) -> TriggerTelemetryWriter:
    """The telemetry writes, for a caller that already holds a fenced transaction.

    Opens no transaction and validates no authority; 0043's triggers refuse an
    unguarded insert whichever object issued it.
    """
    return TriggerTelemetryWriter(connection, workspace_id)


@contextmanager
def trigger_telemetry_writer(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
) -> Iterator[TriggerTelemetryWriter]:
    """One fenced transaction, and the telemetry writes that may be issued into it."""
    with fenced_transaction(
        connection,
        identity,
        workspace_id=workspace_id,
        fencing_generation=fencing_generation,
    ):
        yield transaction_local_telemetry_writer(connection, workspace_id=workspace_id)


# --- reads ---------------------------------------------------------------------------


def read_trigger_declaration(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    project_id: str,
    workflow_id: str,
    trigger_id: str,
) -> TriggerDeclaration | None:
    """The latest declaration of a trigger bound to this Project and Workflow, or `None`."""
    workspace_id = _text(workspace_id, _ID, "workspace_id")
    project_id = _text(project_id, _ID, "project_id")
    workflow_id = _text(workflow_id, _WORKFLOW_ID, "workflow_id")
    found = _latest_declaration(
        connection, workspace_id, _text(trigger_id, _ID, "trigger_id")
    )
    if found is None or (found.project_id, found.workflow_id) != (
        project_id,
        workflow_id,
    ):
        return None
    return found


def read_accepted_trigger_observation(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    trigger_id: str,
    idempotency_key: str,
) -> TriggerObservation | None:
    """The accepted observation holding this idempotency key for a trigger, or `None`.

    Only an accepted observation holds its key. A dead-lettered delivery does not, so a
    redelivery after the trigger is reactivated is decided afresh.
    """
    row = connection.execute(
        f"SELECT {_OBSERVATION_COLUMNS} FROM {_OBSERVATIONS} "
        "WHERE workspace_id = ? AND trigger_id = ? AND idempotency_key = ? "
        "AND delivery_status = 'accepted'",
        (
            _text(workspace_id, _ID, "workspace_id"),
            _text(trigger_id, _ID, "trigger_id"),
            _text(idempotency_key, _ID, "idempotency_key"),
        ),
    ).fetchone()
    return None if row is None else _observation_from_row(row)


def read_trigger_telemetry(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    project_id: str,
    workflow_id: str,
    trigger_id: str,
    observation_limit: int = DEFAULT_OBSERVATION_WINDOW,
) -> TriggerTelemetry | None:
    """Subscription, last observation, delivery, processing, failures and uncertainty.

    `None` when the trigger is not declared in this workspace for this Project and
    Workflow. The newest `observation_limit` observations (at most
    :data:`MAX_OBSERVATION_WINDOW`) are read, newest first.
    """
    limit = _window(observation_limit, MAX_OBSERVATION_WINDOW, "observation_limit")
    declaration = read_trigger_declaration(
        connection,
        workspace_id=workspace_id,
        project_id=project_id,
        workflow_id=workflow_id,
        trigger_id=trigger_id,
    )
    if declaration is None:
        return None
    return _project_trigger(connection, workspace_id, declaration, limit)


def list_workflow_trigger_telemetry(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    project_id: str,
    workflow_id: str,
    limit: int = DEFAULT_TRIGGER_PAGE,
    after_trigger_id: str | None = None,
    observation_limit: int = 5,
) -> TriggerTelemetryPage:
    """One bounded page of a Workflow's triggers, ordered by trigger id.

    This is the shared aggregation the trigger health requirement names: Expose trigger
    health through a shared aggregation keyed by Project/Workflow, including per-trigger
    subscription state, last observation, delivery/processing status, failures and
    uncertainty.
    """
    count = _window(limit, MAX_TRIGGER_PAGE, "limit")
    window = _window(
        observation_limit, MAX_PAGE_OBSERVATION_WINDOW, "observation_limit"
    )
    workspace_id = _text(workspace_id, _ID, "workspace_id")
    project_id = _text(project_id, _ID, "project_id")
    workflow_id = _text(workflow_id, _WORKFLOW_ID, "workflow_id")
    after = _optional_text(after_trigger_id, _ID, "after_trigger_id") or ""
    rows = connection.execute(
        f"SELECT DISTINCT trigger_id FROM {_DECLARATIONS} "
        "WHERE workspace_id = ? AND project_id = ? AND workflow_id = ? AND trigger_id > ? "
        "ORDER BY trigger_id LIMIT ?",
        (workspace_id, project_id, workflow_id, after, count + 1),
    ).fetchall()
    ids = [str(row[0]) for row in rows]
    items: list[TriggerTelemetry] = []
    for trigger_id in ids[:count]:
        declaration = _latest_declaration(connection, workspace_id, trigger_id)
        assert declaration is not None  # selected from the same table above
        items.append(_project_trigger(connection, workspace_id, declaration, window))
    return TriggerTelemetryPage(
        items=tuple(items),
        next_after_trigger_id=ids[count - 1] if len(ids) > count else None,
    )


def read_wait_signal_observation(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    wait_id: str,
    wait_signal_observation_id: str,
) -> WaitSignalObservation | None:
    """One recorded signal of one wait, or `None`. A retry of a refused signal finds it here."""
    row = connection.execute(
        f"SELECT {_WAIT_COLUMNS} FROM {_WAIT_SIGNALS} "
        "WHERE workspace_id = ? AND wait_id = ? AND wait_signal_observation_id = ?",
        (
            _text(workspace_id, _ID, "workspace_id"),
            _text(wait_id, _ID, "wait_id"),
            _text(wait_signal_observation_id, _ID, "wait_signal_observation_id"),
        ),
    ).fetchone()
    return None if row is None else _wait_signal_from_row(row)


def read_wait_signal_telemetry(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    wait_id: str,
    observation_limit: int = DEFAULT_OBSERVATION_WINDOW,
) -> WaitSignalTelemetry | None:
    """Signals delivered to one wait, beside the wait's own state.

    `None` when the wait is not an `external_signal` wait of this workspace.
    """
    limit = _window(observation_limit, MAX_OBSERVATION_WINDOW, "observation_limit")
    workspace_id = _text(workspace_id, _ID, "workspace_id")
    wait_id = _text(wait_id, _ID, "wait_id")
    wait = connection.execute(
        "SELECT run_id, kind FROM omnivia_runtime_waits WHERE workspace_id = ? AND wait_id = ?",
        (workspace_id, wait_id),
    ).fetchone()
    if wait is None or wait[1] != "external_signal":
        return None
    run_id = str(wait[0])
    resolution = connection.execute(
        "SELECT status FROM omnivia_runtime_wait_resolutions WHERE workspace_id = ? AND wait_id = ?",
        (workspace_id, wait_id),
    ).fetchone()
    wait_status = "pending" if resolution is None else str(resolution[0])
    window = tuple(
        _wait_signal_from_row(row)
        for row in connection.execute(
            f"SELECT {_WAIT_COLUMNS} FROM {_WAIT_SIGNALS} "
            "WHERE workspace_id = ? AND wait_id = ? ORDER BY observation_sequence DESC LIMIT ?",
            (workspace_id, wait_id, limit),
        )
    )
    uncertainty: set[str] = set()
    for item in window:
        if item.occurred_at_us is None:
            uncertainty.add("source_time_unknown")
        if item.delivery_status == "uncertain":
            uncertainty.add("delivery_uncertain")
        if item.delivery_status == "accepted" and wait_status == "pending":
            uncertainty.add("resolution_not_recorded")
    return WaitSignalTelemetry(
        wait_id=wait_id,
        run_id=run_id,
        wait_status=wait_status,
        run_status=_run_status(connection, workspace_id, run_id),
        last_observation=window[0] if window else None,
        delivery_status=window[0].delivery_status if window else None,
        observation_total=window[0].observation_sequence if window else 0,
        window=window,
        delivery_counts=_counts(item.delivery_status for item in window),
        uncertainty=tuple(sorted(uncertainty)),
    )


# --- projection ----------------------------------------------------------------------


def _project_trigger(
    connection: sqlite3.Connection,
    workspace_id: str,
    declaration: TriggerDeclaration,
    limit: int,
) -> TriggerTelemetry:
    latest = _latest_subscription(connection, workspace_id, declaration.trigger_id)
    subscription = SubscriptionStatus(
        state=None if latest is None else latest.subscription_state,
        reason=None if latest is None else latest.reason,
        observed_at_us=None if latest is None else latest.observed_at_us,
        subscription_sequence=0 if latest is None else latest.subscription_sequence,
    )
    window = tuple(
        _view(connection, workspace_id, declaration, _observation_from_row(row))
        for row in connection.execute(
            f"SELECT {_OBSERVATION_COLUMNS} FROM {_OBSERVATIONS} "
            "WHERE workspace_id = ? AND trigger_id = ? "
            "ORDER BY observation_sequence DESC LIMIT ?",
            (workspace_id, declaration.trigger_id, limit),
        )
    )
    uncertainty: set[str] = set()
    if latest is None:
        uncertainty.add("no_subscription_recorded")
    elif latest.subscription_state == "unavailable":
        uncertainty.add("subscription_unavailable")
    failures: list[TriggerFailure] = []
    for view in window:
        uncertainty.update(view.uncertainty)
        observation_id = view.observation.trigger_observation_id
        if view.observation.delivery_status == "dead_lettered":
            failures.append(
                TriggerFailure(
                    observation_id, "delivery", str(view.observation.delivery_reason)
                )
            )
        if view.processing == "failed":
            source = "run" if view.run_status == "failed" else "job"
            failures.append(TriggerFailure(observation_id, source, f"{source}_failed"))
    return TriggerTelemetry(
        declaration=declaration,
        subscription=subscription,
        last_observation=window[0] if window else None,
        delivery_status=window[0].observation.delivery_status if window else None,
        observation_total=window[0].observation.observation_sequence if window else 0,
        window=window,
        delivery_counts=_counts(view.observation.delivery_status for view in window),
        failures=tuple(failures),
        uncertainty=tuple(sorted(uncertainty)),
    )


def _view(
    connection: sqlite3.Connection,
    workspace_id: str,
    declaration: TriggerDeclaration,
    observation: TriggerObservation,
) -> ObservationView:
    uncertainty: set[str] = set()
    if observation.occurred_at_us is None:
        uncertainty.add("source_time_unknown")
    if observation.delivery_status == "uncertain":
        uncertainty.add("delivery_uncertain")
    if observation.delivery_status in {"duplicate", "dead_lettered"}:
        return ObservationView(
            observation, "not_applicable", None, None, tuple(sorted(uncertainty))
        )
    if observation.delivery_status == "uncertain":
        return ObservationView(
            observation, "unknown", None, None, tuple(sorted(uncertainty))
        )

    run_id = observation.run_id
    if run_id is None and observation.job_id is not None:
        found = connection.execute(
            "SELECT run_id FROM omnivia_runtime_runs WHERE workspace_id = ? AND job_id = ?",
            (workspace_id, observation.job_id),
        ).fetchone()
        run_id = None if found is None else str(found[0])
    if run_id is not None:
        bound = connection.execute(
            "SELECT workflow_id FROM omnivia_workflow_runs WHERE workspace_id = ? AND run_id = ?",
            (workspace_id, run_id),
        ).fetchone()
        if bound is None or str(bound[0]) != declaration.workflow_id:
            uncertainty.add("linked_run_workflow_mismatch")
            run_id = None
    job_state = _job_state(connection, workspace_id, observation.job_id)
    run_status = (
        None if run_id is None else _run_status(connection, workspace_id, run_id)
    )
    if observation.job_id is not None and _unrecorded_terminal(
        connection, workspace_id, observation.job_id
    ):
        uncertainty.add("job_terminal_provenance_unrecorded")

    job_rollup = None if job_state is None else _JOB_ROLLUP.get(job_state)
    run_rollup = None if run_status is None else _RUN_ROLLUP.get(run_status)
    if observation.job_id is None and observation.run_id is None:
        processing = "unlinked"
        uncertainty.add("processing_unlinked")
    elif job_rollup is None and run_rollup is None:
        processing = "unknown"
        uncertainty.add("processing_unknown")
    elif (
        job_rollup is not None
        and run_rollup is not None
        and job_rollup != run_rollup
        and (job_rollup in _TERMINAL or run_rollup in _TERMINAL)
    ):
        processing = "uncertain"
        uncertainty.add("job_run_disagree")
    else:
        processing = run_rollup or job_rollup or "unknown"
    return ObservationView(
        observation, processing, job_state, run_status, tuple(sorted(uncertainty))
    )


# --- row access ----------------------------------------------------------------------

_DECLARATION_COLUMNS: Final = (
    "trigger_declaration_id, trigger_id, declaration_sequence, trigger_kind, project_id, "
    "workflow_id, workflow_version, plan_hash, event_type, event_contract_digest, "
    "configuration_digest, declared_at_us, audit_ref"
)
_SUBSCRIPTION_COLUMNS: Final = (
    "subscription_event_id, trigger_id, declaration_sequence, subscription_sequence, "
    "subscription_state, reason, observed_at_us, audit_ref"
)
_OBSERVATION_COLUMNS: Final = (
    "trigger_observation_id, trigger_id, declaration_sequence, observation_sequence, "
    "event_id, idempotency_key, event_type, envelope_digest, occurred_at_us, observed_at_us, "
    "delivery_status, delivery_reason, duplicate_of_observation_id, job_id, run_id, audit_ref"
)
_WAIT_COLUMNS: Final = (
    "wait_signal_observation_id, wait_id, observation_sequence, event_id, envelope_digest, "
    "occurred_at_us, observed_at_us, delivery_status, delivery_reason, "
    "duplicate_of_observation_id, audit_ref"
)


def _fields(record: object) -> dict[str, object]:
    return {name: getattr(record, name) for name in record.__slots__}  # type: ignore[attr-defined]


def _without_sequence(declaration: TriggerDeclaration) -> dict[str, object]:
    return {
        k: v for k, v in _fields(declaration).items() if k != "declaration_sequence"
    }


def _without_position(record: object) -> dict[str, object]:
    return {
        k: v
        for k, v in _fields(record).items()
        if k not in {"declaration_sequence", "observation_sequence"}
    }


def _insert(
    connection: sqlite3.Connection,
    table: str,
    workspace_id: str,
    values: dict[str, object],
) -> None:
    columns = ["workspace_id", *values]
    connection.execute(
        f"INSERT INTO {table} ({', '.join(columns)}) "
        f"VALUES ({', '.join('?' for _ in columns)})",
        (workspace_id, *values.values()),
    )


def _declaration_from_row(row: tuple[object, ...]) -> TriggerDeclaration:
    return TriggerDeclaration(*row)  # type: ignore[arg-type]


def _observation_from_row(row: tuple[object, ...]) -> TriggerObservation:
    return TriggerObservation(*row)  # type: ignore[arg-type]


def _wait_signal_from_row(row: tuple[object, ...]) -> WaitSignalObservation:
    return WaitSignalObservation(*row)  # type: ignore[arg-type]


def _declaration_by_id(
    connection: sqlite3.Connection, workspace_id: str, declaration_id: str
) -> TriggerDeclaration | None:
    row = connection.execute(
        f"SELECT {_DECLARATION_COLUMNS} FROM {_DECLARATIONS} "
        "WHERE workspace_id = ? AND trigger_declaration_id = ?",
        (workspace_id, declaration_id),
    ).fetchone()
    return None if row is None else _declaration_from_row(row)


def _latest_declaration(
    connection: sqlite3.Connection, workspace_id: str, trigger_id: str
) -> TriggerDeclaration | None:
    row = connection.execute(
        f"SELECT {_DECLARATION_COLUMNS} FROM {_DECLARATIONS} "
        "WHERE workspace_id = ? AND trigger_id = ? ORDER BY declaration_sequence DESC LIMIT 1",
        (workspace_id, trigger_id),
    ).fetchone()
    return None if row is None else _declaration_from_row(row)


def _subscription_by_id(
    connection: sqlite3.Connection, workspace_id: str, event_id: str
) -> SubscriptionEvent | None:
    row = connection.execute(
        f"SELECT {_SUBSCRIPTION_COLUMNS} FROM {_SUBSCRIPTIONS} "
        "WHERE workspace_id = ? AND subscription_event_id = ?",
        (workspace_id, event_id),
    ).fetchone()
    return None if row is None else SubscriptionEvent(*row)


def _latest_subscription(
    connection: sqlite3.Connection, workspace_id: str, trigger_id: str
) -> SubscriptionEvent | None:
    row = connection.execute(
        f"SELECT {_SUBSCRIPTION_COLUMNS} FROM {_SUBSCRIPTIONS} "
        "WHERE workspace_id = ? AND trigger_id = ? ORDER BY subscription_sequence DESC LIMIT 1",
        (workspace_id, trigger_id),
    ).fetchone()
    return None if row is None else SubscriptionEvent(*row)


def _observation_by_id(
    connection: sqlite3.Connection, workspace_id: str, observation_id: str
) -> TriggerObservation | None:
    row = connection.execute(
        f"SELECT {_OBSERVATION_COLUMNS} FROM {_OBSERVATIONS} "
        "WHERE workspace_id = ? AND trigger_observation_id = ?",
        (workspace_id, observation_id),
    ).fetchone()
    return None if row is None else _observation_from_row(row)


def _wait_signal_by_id(
    connection: sqlite3.Connection, workspace_id: str, observation_id: str
) -> WaitSignalObservation | None:
    row = connection.execute(
        f"SELECT {_WAIT_COLUMNS} FROM {_WAIT_SIGNALS} "
        "WHERE workspace_id = ? AND wait_signal_observation_id = ?",
        (workspace_id, observation_id),
    ).fetchone()
    return None if row is None else _wait_signal_from_row(row)


def _latest_observation_sequence(
    connection: sqlite3.Connection, workspace_id: str, trigger_id: str
) -> int:
    row = connection.execute(
        f"SELECT COALESCE(MAX(observation_sequence), 0) FROM {_OBSERVATIONS} "
        "WHERE workspace_id = ? AND trigger_id = ?",
        (workspace_id, trigger_id),
    ).fetchone()
    return int(row[0])


def _job_state(
    connection: sqlite3.Connection, workspace_id: str, job_id: str | None
) -> str | None:
    if job_id is None:
        return None
    row = connection.execute(
        "SELECT state FROM omnivia_job_events WHERE workspace_id = ? AND job_id = ? "
        "ORDER BY sequence DESC LIMIT 1",
        (workspace_id, job_id),
    ).fetchone()
    return None if row is None else str(row[0])


def _run_status(
    connection: sqlite3.Connection, workspace_id: str, run_id: str
) -> str | None:
    row = connection.execute(
        "SELECT run_status FROM omnivia_runtime_events WHERE workspace_id = ? AND run_id = ? "
        "ORDER BY sequence DESC LIMIT 1",
        (workspace_id, run_id),
    ).fetchone()
    return None if row is None else str(row[0])


def _unrecorded_terminal(
    connection: sqlite3.Connection, workspace_id: str, job_id: str
) -> bool:
    row = connection.execute(
        "SELECT 1 FROM omnivia_job_terminal_observations WHERE workspace_id = ? "
        "AND job_id = ? AND provenance_kind = 'legacy_unrecorded' LIMIT 1",
        (workspace_id, job_id),
    ).fetchone()
    return row is not None


def _counts(statuses: Iterable[str]) -> Mapping[str, int]:
    counts = dict.fromkeys(DELIVERY_STATUSES, 0)
    for status in statuses:
        counts[status] += 1
    return counts
