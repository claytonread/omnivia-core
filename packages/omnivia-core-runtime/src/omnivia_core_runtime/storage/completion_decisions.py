"""Append-only Runtime completion decisions (DEV-REQ-137; migration 0066).

Persistence only, in the shape of `storage/review_finding_quarantine.py`: `record_decision` expects
its caller to be inside a `fenced_transaction`, and the settlement seam in
`service/completion_gate.py` opens that fence. A stored row is the Runtime's own accepted decision
that one run's final step may terminalize. It is never inferred from a provider result, a transport
status, an artefact or a `result_kind`, and nothing here reads an evidence source.

Identity is the canonical body. `decision_digest` is the SHA-256 of that body's canonical JSON, and
the body excludes the time it was recorded, so an exact replay is the same decision and dedups to
the existing row. `(workspace_id, run_id)` is unique, so a run can carry at most one decision and a
different body for it is a `CompletionConflict`. Reads re-derive the digest and the closed shape, so
a row edited outside this database reads as corrupt rather than as another decision.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Final, cast

from omnivia_core.contracts.v1 import is_identifier, to_canonical_json

DECISION_ACCEPTED: Final = "accepted"
#: The application closure a decision is bound to: its job's succeeded terminal observation, exactly.
CLOSURE_SUCCEEDED: Final = "succeeded"

_TABLE: Final = "omnivia_runtime_completion_decisions"
_INT64_MAX: Final = 2**63 - 1
#: The bound `omnivia_runtime_events` puts on a sequence, so a settled sequence names a real event slot.
_MAX_SEQUENCE: Final = 999
#: The bound `omnivia_job_attempts` puts on an application attempt number (migration 0010 family).
_MAX_APPLICATION_ATTEMPT: Final = 256
_MAX_BODY_BYTES: Final = 1024 * 1024
_COLUMNS: Final = (
    "workspace_id",
    "decision_digest",
    "run_id",
    "job_id",
    "run_step_id",
    "runtime_attempt_id",
    "application_attempt_number",
    "closure_state",
    "settled_sequence",
    "decision",
    "decision_body",
    "decided_under_generation",
    "decided_at_us",
    "service_instance_id",
)
_SELECT: Final = ", ".join(_COLUMNS)
_INSERT: Final = (
    f"INSERT INTO {_TABLE} ({_SELECT}) "
    f"VALUES ({', '.join(':' + column for column in _COLUMNS)})"
)


class CompletionDecisionInvalid(ValueError):
    """A decision, or a stored row read back, is outside its closed shape."""


class CompletionConflict(ValueError):
    """A run already carries a different completion decision."""


@dataclass(frozen=True, slots=True)
class EvidenceReference:
    """One raw evidence identity a decision rests on, with who collected and who reviewed it."""

    criterion: str
    evidence_id: str
    content_digest: str
    collected_by: str
    reviewed_by: str


@dataclass(frozen=True, slots=True)
class CompletionDecision:
    """The Runtime's accepted completion of one run's final step, bound to one claim.

    `proven_criteria` is every accepted criterion, sorted, and `evidence` names exactly one raw
    evidence identity for each of them. `unproven_criteria` is stated explicitly and is always
    empty, because a decision that leaves an accepted criterion unproven is refused, not stored.
    """

    workspace_id: str
    run_id: str
    job_id: str
    run_step_id: str
    runtime_attempt_id: str
    #: The durable job's application attempt the claim ran under, which the job's terminal observation states.
    application_attempt_number: int
    candidate_id: str
    binding_id: str
    definition_digest: str
    aggregate_id: str
    package_id: str
    application_id: str
    decided_under_generation: int
    #: The sequence of the run's succeeded event, which this decision and that event settle together.
    settled_sequence: int
    proven_criteria: tuple[str, ...]
    evidence: tuple[EvidenceReference, ...]
    self_evidence_attributed_to: str | None = None
    unproven_criteria: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        identifiers = (
            self.workspace_id,
            self.run_id,
            self.job_id,
            self.run_step_id,
            self.runtime_attempt_id,
            self.candidate_id,
            self.binding_id,
            self.aggregate_id,
            self.package_id,
            self.application_id,
        )
        if not all(_text(value, is_identifier) for value in identifiers):
            raise CompletionDecisionInvalid("a decision identity is outside its closed shape")
        if not _is_digest(self.definition_digest):
            raise CompletionDecisionInvalid("definition_digest is outside its closed shape")
        if not _bounded(self.decided_under_generation, 1):
            raise CompletionDecisionInvalid("decided_under_generation is outside its closed shape")
        if not _bounded(self.application_attempt_number, 1) or (
            self.application_attempt_number > _MAX_APPLICATION_ATTEMPT
        ):
            raise CompletionDecisionInvalid("application_attempt_number is outside its closed shape")
        if not _bounded(self.settled_sequence, 1) or self.settled_sequence > _MAX_SEQUENCE:
            raise CompletionDecisionInvalid("settled_sequence is outside its closed shape")
        if self.unproven_criteria:
            raise CompletionDecisionInvalid("an accepted decision leaves no criterion unproven")
        if not self.proven_criteria or list(self.proven_criteria) != sorted(
            set(self.proven_criteria)
        ):
            raise CompletionDecisionInvalid("proven_criteria must be non-empty, sorted and unique")
        evidenced = tuple(reference.criterion for reference in self.evidence)
        if evidenced != self.proven_criteria:
            raise CompletionDecisionInvalid("each proven criterion names exactly one evidence")
        for reference in self.evidence:
            if not (
                _text(reference.criterion, is_identifier)
                and _text(reference.evidence_id, is_identifier)
                and _text(reference.collected_by, is_identifier)
                and _text(reference.reviewed_by, is_identifier)
                and _is_digest(reference.content_digest)
            ):
                raise CompletionDecisionInvalid("an evidence reference is outside its closed shape")
        if self.self_evidence_attributed_to is not None and not _text(
            self.self_evidence_attributed_to, is_identifier
        ):
            raise CompletionDecisionInvalid("self_evidence_attributed_to is outside its shape")

    @property
    def decision_digest(self) -> str:
        return f"sha256:{sha256(to_canonical_json(self.to_body()).encode('utf-8')).hexdigest()}"

    def to_body(self) -> dict[str, Any]:
        return {
            "workspace_id": self.workspace_id,
            "run_id": self.run_id,
            "job_id": self.job_id,
            "run_step_id": self.run_step_id,
            "runtime_attempt_id": self.runtime_attempt_id,
            "application_attempt_number": self.application_attempt_number,
            "candidate_id": self.candidate_id,
            "binding_id": self.binding_id,
            "definition_digest": self.definition_digest,
            "aggregate_id": self.aggregate_id,
            "package_id": self.package_id,
            "application_id": self.application_id,
            "decided_under_generation": self.decided_under_generation,
            "settled_sequence": self.settled_sequence,
            "decision": DECISION_ACCEPTED,
            "proven_criteria": list(self.proven_criteria),
            "unproven_criteria": list(self.unproven_criteria),
            "evidence": [
                {
                    "criterion": reference.criterion,
                    "evidence_id": reference.evidence_id,
                    "content_digest": reference.content_digest,
                    "collected_by": reference.collected_by,
                    "reviewed_by": reference.reviewed_by,
                }
                for reference in self.evidence
            ],
            "self_evidence_attributed_to": self.self_evidence_attributed_to,
        }


@dataclass(frozen=True, slots=True)
class StoredCompletionDecision:
    """A decision read back from storage, with the digest it was stored under verified."""

    decision_digest: str
    decided_at_us: int
    #: The scheduler instance that decided, kept outside the digest because no later row can say it.
    service_instance_id: str
    decision: CompletionDecision


def record_decision(
    connection: sqlite3.Connection,
    *,
    decision: CompletionDecision,
    decided_at_us: int,
    service_instance_id: str,
) -> StoredCompletionDecision:
    """Persist `decision` under the caller's fence, or return the exact row already stored.

    `service_instance_id` is the scheduler instance writing now. It is stored beside the decision, outside
    its digest, and an exact replay returns the stored row without comparing it to a later owner.

    A different body for a run that already has one is refused, and nothing is written. An exact replay
    returns the stored row only after `read_decision` has verified it against its settling event.
    """
    if not _bounded(decided_at_us, 1):
        raise CompletionDecisionInvalid("decided_at_us is outside its closed shape")
    if not _text(service_instance_id, is_identifier):
        raise CompletionDecisionInvalid("service_instance_id is outside its closed shape")
    # The public read, so an existing row is returned only once its settling event agrees with it.
    existing = read_decision(connection, workspace_id=decision.workspace_id, run_id=decision.run_id)
    if existing is not None:
        if existing.decision_digest == decision.decision_digest:
            return existing
        raise CompletionConflict("the run already carries a different completion decision")
    connection.execute(
        _INSERT,
        {
            "workspace_id": decision.workspace_id,
            "decision_digest": decision.decision_digest,
            "run_id": decision.run_id,
            "job_id": decision.job_id,
            "run_step_id": decision.run_step_id,
            "runtime_attempt_id": decision.runtime_attempt_id,
            "application_attempt_number": decision.application_attempt_number,
            "closure_state": CLOSURE_SUCCEEDED,
            "settled_sequence": decision.settled_sequence,
            "decision": DECISION_ACCEPTED,
            "decision_body": to_canonical_json(decision.to_body()),
            "decided_under_generation": decision.decided_under_generation,
            "decided_at_us": decided_at_us,
            "service_instance_id": service_instance_id,
        },
    )
    return StoredCompletionDecision(
        decision_digest=decision.decision_digest,
        decided_at_us=decided_at_us,
        service_instance_id=service_instance_id,
        decision=decision,
    )


def read_decision(
    connection: sqlite3.Connection, *, workspace_id: str, run_id: str
) -> StoredCompletionDecision | None:
    """The stored decision for one run, revalidated, digest-checked and paired with its event, or `None`.

    The decided time sits outside the digest, so it is checked here against the event it was written
    with: the succeeded event at the decision's own sequence must state the same step and the same instant,
    and its details must be the closed shape the scheduler wrote, naming this decision and its lineage.
    The event's runtime attempt number is the immutable one of the decision's attempt, and its service
    instance is the one stored with the decision. Neither depends on who owns the workspace now.
    """
    row = connection.execute(
        f"SELECT {_SELECT} FROM {_TABLE} WHERE workspace_id = ? AND run_id = ?",
        (workspace_id, run_id),
    ).fetchone()
    if row is None:
        return None
    stored = _record(row)
    if stored is not None:
        _require_settling_event(connection, stored)
    return stored


_EVENT_MISMATCH: Final = "stored completion decision does not match its succeeded event"
#: The keys the scheduler writes into a succeeded run event's details, and no others.
_EVENT_DETAIL_KEYS: Final = frozenset(
    {
        "workspace_id",
        "run_id",
        "job_id",
        "run_step_id",
        "runtime_attempt_id",
        "runtime_attempt_number",
        "application_attempt_number",
        "service_instance_id",
        "fencing_generation",
        "completion_decision_digest",
    }
)


def _require_settling_event(connection: sqlite3.Connection, stored: StoredCompletionDecision) -> None:
    """The event at the decision's sequence is the succeeded event it was written with, at the same instant."""
    decision = stored.decision
    event = connection.execute(
        "SELECT event_kind, run_status, run_step_id, occurred_at_us, details_json "
        "FROM omnivia_runtime_events WHERE workspace_id = ? AND run_id = ? AND sequence = ?",
        (decision.workspace_id, decision.run_id, decision.settled_sequence),
    ).fetchone()
    if (
        event is None
        or event[0] != "run_succeeded"
        or event[1] != "succeeded"
        or event[2] != decision.run_step_id
        or event[3] != stored.decided_at_us
    ):
        raise CompletionDecisionInvalid(_EVENT_MISMATCH)
    attempt = connection.execute(
        "SELECT attempt_number FROM omnivia_runtime_attempts "
        "WHERE workspace_id = ? AND run_id = ? AND run_step_id = ? AND attempt_id = ?",
        (decision.workspace_id, decision.run_id, decision.run_step_id, decision.runtime_attempt_id),
    ).fetchone()
    if attempt is None:
        raise CompletionDecisionInvalid(_EVENT_MISMATCH)
    _require_settling_details(event[4], stored, attempt[0])


def _require_settling_details(
    details_text: object, stored: StoredCompletionDecision, runtime_attempt_number: object
) -> None:
    """The event's details are the closed shape the scheduler wrote, and name this decision and its lineage.

    The details are canonical JSON, so the text must re-serialize to itself. That refuses a duplicate key,
    which a parser keeps only once, and any reformatting. Each value is compared by exact built-in type
    first, so `True` never reads as `1` and a string never reads as a number.
    """
    decision = stored.decision
    if type(details_text) is not str:
        raise CompletionDecisionInvalid(_EVENT_MISMATCH)
    try:
        details = json.loads(details_text)
    except (ValueError, RecursionError) as error:
        raise CompletionDecisionInvalid(_EVENT_MISMATCH) from error
    if type(details) is not dict or set(details) != _EVENT_DETAIL_KEYS:
        raise CompletionDecisionInvalid(_EVENT_MISMATCH)
    # The event's `fencing_generation` is the decision's `decided_under_generation`. Its attempt number is
    # the decision's runtime attempt's own, and its service instance is the one stored with the decision.
    if not (
        _same(details["completion_decision_digest"], stored.decision_digest)
        and _same(details["workspace_id"], decision.workspace_id)
        and _same(details["run_id"], decision.run_id)
        and _same(details["job_id"], decision.job_id)
        and _same(details["run_step_id"], decision.run_step_id)
        and _same(details["runtime_attempt_id"], decision.runtime_attempt_id)
        and _same(details["application_attempt_number"], decision.application_attempt_number)
        and _same(details["fencing_generation"], decision.decided_under_generation)
        and _bounded(runtime_attempt_number, 1)
        and _same(details["runtime_attempt_number"], cast(int, runtime_attempt_number))
        and _same(details["service_instance_id"], stored.service_instance_id)
    ):
        raise CompletionDecisionInvalid(_EVENT_MISMATCH)
    if to_canonical_json(details) != details_text:
        raise CompletionDecisionInvalid(_EVENT_MISMATCH)


def _record(row: tuple[Any, ...]) -> StoredCompletionDecision:
    values: dict[str, Any] = dict(zip(_COLUMNS, row, strict=True))
    body_text = values["decision_body"]
    if (
        not isinstance(body_text, str)
        or len(body_text.encode("utf-8")) > _MAX_BODY_BYTES
        or values["decision"] != DECISION_ACCEPTED
        or values["closure_state"] != CLOSURE_SUCCEEDED
        or not _bounded(values["decided_at_us"], 1)
        or not _text(values["service_instance_id"], is_identifier)
    ):
        raise CompletionDecisionInvalid("stored completion decision is malformed")
    try:
        body = json.loads(body_text)
    except (ValueError, RecursionError) as error:
        raise CompletionDecisionInvalid("stored completion decision is malformed") from error
    decision = _from_body(body)
    if (
        body["workspace_id"] != values["workspace_id"]
        or body["run_id"] != values["run_id"]
        or decision.job_id != values["job_id"]
        or decision.run_step_id != values["run_step_id"]
        or decision.runtime_attempt_id != values["runtime_attempt_id"]
        or decision.application_attempt_number != values["application_attempt_number"]
        or decision.settled_sequence != values["settled_sequence"]
        or decision.decided_under_generation != values["decided_under_generation"]
        or decision.decision_digest != values["decision_digest"]
        or to_canonical_json(decision.to_body()) != body_text
    ):
        raise CompletionDecisionInvalid("stored completion decision does not verify its digest")
    return StoredCompletionDecision(
        decision_digest=values["decision_digest"],
        decided_at_us=values["decided_at_us"],
        service_instance_id=values["service_instance_id"],
        decision=decision,
    )


_BODY_KEYS: Final = frozenset(
    {
        "workspace_id",
        "run_id",
        "job_id",
        "run_step_id",
        "runtime_attempt_id",
        "application_attempt_number",
        "candidate_id",
        "binding_id",
        "definition_digest",
        "aggregate_id",
        "package_id",
        "application_id",
        "decided_under_generation",
        "settled_sequence",
        "decision",
        "proven_criteria",
        "unproven_criteria",
        "evidence",
        "self_evidence_attributed_to",
    }
)
_EVIDENCE_KEYS: Final = frozenset(
    {"criterion", "evidence_id", "content_digest", "collected_by", "reviewed_by"}
)


def _from_body(body: Any) -> CompletionDecision:
    """Decode a stored body under a closed shape: exact keys at every object level, no coercion.

    Every field is type-checked before it is used. A string is never read as an array of its
    characters, and a bool is never read as an integer. Anything else raises
    :class:`CompletionDecisionInvalid`, not an incidental `KeyError` or `TypeError`.
    """
    if not isinstance(body, dict) or set(body) != _BODY_KEYS or body["decision"] != DECISION_ACCEPTED:
        raise CompletionDecisionInvalid("stored completion decision is malformed")
    evidence = tuple(_evidence_from(item) for item in _array(body["evidence"]))
    return CompletionDecision(
        workspace_id=_string(body["workspace_id"]),
        run_id=_string(body["run_id"]),
        job_id=_string(body["job_id"]),
        run_step_id=_string(body["run_step_id"]),
        runtime_attempt_id=_string(body["runtime_attempt_id"]),
        application_attempt_number=_integer(body["application_attempt_number"]),
        candidate_id=_string(body["candidate_id"]),
        binding_id=_string(body["binding_id"]),
        definition_digest=_string(body["definition_digest"]),
        aggregate_id=_string(body["aggregate_id"]),
        package_id=_string(body["package_id"]),
        application_id=_string(body["application_id"]),
        decided_under_generation=_integer(body["decided_under_generation"]),
        settled_sequence=_integer(body["settled_sequence"]),
        proven_criteria=_strings(body["proven_criteria"]),
        unproven_criteria=_strings(body["unproven_criteria"]),
        evidence=evidence,
        self_evidence_attributed_to=_nullable_string(body["self_evidence_attributed_to"]),
    )


def _evidence_from(item: Any) -> EvidenceReference:
    if not isinstance(item, dict) or set(item) != _EVIDENCE_KEYS:
        raise CompletionDecisionInvalid("stored completion decision is malformed")
    return EvidenceReference(
        criterion=_string(item["criterion"]),
        evidence_id=_string(item["evidence_id"]),
        content_digest=_string(item["content_digest"]),
        collected_by=_string(item["collected_by"]),
        reviewed_by=_string(item["reviewed_by"]),
    )


def _array(value: Any) -> list[Any]:
    if not isinstance(value, list):
        raise CompletionDecisionInvalid("stored completion decision is malformed")
    return value


def _strings(value: Any) -> tuple[str, ...]:
    return tuple(_string(item) for item in _array(value))


def _string(value: Any) -> str:
    if type(value) is not str:
        raise CompletionDecisionInvalid("stored completion decision is malformed")
    return value


def _nullable_string(value: Any) -> str | None:
    return None if value is None else _string(value)


def _integer(value: Any) -> int:
    if not _bounded(value, 1):
        raise CompletionDecisionInvalid("stored completion decision is malformed")
    return int(value)


def _text(value: object, check: Callable[[str], bool]) -> bool:
    # Exact `str` only: a subclass can carry its own `__eq__`, and a closed shape must not run it.
    return type(value) is str and check(value)


def _is_digest(value: object) -> bool:
    return (
        type(value) is str
        and len(value) == 71
        and value.startswith("sha256:")
        and all(char in "0123456789abcdef" for char in value[7:])
    )


def _bounded(value: object, least: int) -> bool:
    # Exact `int` only: `bool` and `int` subclasses compare equal to ordinary integers.
    return type(value) is int and least <= value <= _INT64_MAX


def _same(value: object, expected: str | int) -> bool:
    # Exact type first: `True == 1` and `1.0 == 1` compare equal, and a subclass can carry its own `__eq__`.
    return type(value) is type(expected) and value == expected
