"""Evidence-only quarantine of unvalidatable review findings (DEV-REQ-176; migration 0065).

Persistence only, in the shape of `storage/dataset_state.py`: `record_finding` expects its caller
to be inside a `fenced_transaction`, and the service seam in `service/review_finding_quarantine.py`
opens that fence. Nothing here reads or writes a Task, run, job, lease, approval or accepted review,
and nothing here treats a row as validation. A finding that names a stale generation or an absent
generation, workspace or run is kept as evidence with its reason, and that reason stays on the row.

Identity is the canonical envelope. `finding_digest` is the SHA-256 of the envelope's canonical
JSON, so an exact resubmission returns the existing row and different evidence bytes or different
reason or binding facts create a new one. `idempotency_key` is unique per workspace and bound to
one envelope: the same key with different bytes is an `ReviewFindingConflict`, and so is the same
bytes under a second key. A key is therefore only ever observed for the one digest it is stored
under, which is what makes a key's binding durable rather than a matter of which call came first.

Refusals name fields, never values. The reader applies the same checks to a stored row and
verifies its digest, so a row that does not reproduce its own identity is refused.
"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass
from hashlib import sha256
from typing import Any, Final

from omnivia_core.contracts.v1 import (
    ERROR_CODE_IDEMPOTENCY_CONFLICT,
    is_identifier,
    to_canonical_json,
)

#: The closed reasons a finding can be quarantined under. Each names one absent or stale fact; none
#: names a validation, and none is a state the rest of the Runtime reads as authority.
REASON_STALE_GENERATION: Final = "stale_generation"
REASON_MISSING_GENERATION: Final = "missing_generation"
REASON_MISSING_WORKSPACE: Final = "missing_workspace"
REASON_MISSING_RUN: Final = "missing_run"
REASONS: Final = frozenset(
    {
        REASON_STALE_GENERATION,
        REASON_MISSING_GENERATION,
        REASON_MISSING_WORKSPACE,
        REASON_MISSING_RUN,
    }
)

_TABLE: Final = "omnivia_review_finding_quarantines"
_INT64_MAX: Final = 2**63 - 1
_COLUMNS: Final = (
    "workspace_id",
    "finding_digest",
    "idempotency_key",
    "run_id",
    "candidate_id",
    "evidence_id",
    "content_digest",
    "reason",
    "observed_generation",
    "observed_binding",
    "quarantined_under_generation",
    "attributed_to",
    "recorded_at_us",
)
_SELECT: Final = ", ".join(_COLUMNS)
_INSERT: Final = (
    f"INSERT INTO {_TABLE} ({_SELECT}) "
    f"VALUES ({', '.join(':' + column for column in _COLUMNS)})"
)


class ReviewFindingInvalid(ValueError):
    """A finding envelope, or a stored row read back, is outside its closed shape."""


class ReviewFindingConflict(ValueError):
    """An idempotency key was reused for different evidence bytes."""

    error_code: Final = ERROR_CODE_IDEMPOTENCY_CONFLICT


@dataclass(frozen=True, slots=True)
class ReviewFindingEnvelope:
    """One stale or unvalidatable finding, as the producer observed it.

    `run_id` is `None` only for `missing_run`, and `observed_generation` and `observed_binding` are
    `None` only for the reasons that name their absence. The shapes are checked by `_refusals`.
    """

    workspace_id: str
    run_id: str | None
    candidate_id: str
    evidence_id: str
    content_digest: str
    reason: str
    observed_generation: int | None
    observed_binding: str | None
    attributed_to: str
    recorded_at_us: int


@dataclass(frozen=True, slots=True)
class QuarantinedFinding:
    """One stored quarantine record, read back with its digest verified."""

    finding_digest: str
    idempotency_key: str
    quarantined_under_generation: int
    envelope: ReviewFindingEnvelope


def record_finding(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    idempotency_key: str,
    envelope: ReviewFindingEnvelope,
) -> QuarantinedFinding:
    """Quarantine `envelope` under the caller's fence and return its canonical record.

    An exact resubmission returns the existing row. The same key with different bytes is refused,
    and so are the same bytes under a different key: that second key would otherwise be observed
    for a digest it was never bound to. Both refusals happen before any write. The row binds the
    current fencing generation, so it names the authority it was accepted under.
    """
    if envelope.workspace_id != workspace_id:
        raise ReviewFindingInvalid("a finding must name the open workspace")
    generation = _current_generation(connection)
    refused = _refusals(envelope, idempotency_key, generation)
    if refused:
        raise ReviewFindingInvalid(
            f"review finding envelope is outside its closed shape: {refused}"
        )
    digest = _finding_digest(envelope)
    # The key is checked before the digest, so a key is never observed for a digest it does not bind.
    bound = _by_key(connection, workspace_id, idempotency_key)
    if bound is not None:
        if bound.finding_digest == digest:
            return bound
        raise ReviewFindingConflict("the idempotency key is bound to different evidence")
    if _by_digest(connection, workspace_id, digest) is not None:
        raise ReviewFindingConflict(
            "this evidence is already quarantined under another idempotency key"
        )
    values: dict[str, object] = {
        **asdict(envelope),
        "finding_digest": digest,
        "idempotency_key": idempotency_key,
        "quarantined_under_generation": generation,
    }
    connection.execute(_INSERT, values)
    return QuarantinedFinding(
        finding_digest=digest,
        idempotency_key=idempotency_key,
        quarantined_under_generation=generation,
        envelope=envelope,
    )


def read_finding(
    connection: sqlite3.Connection, *, workspace_id: str, finding_digest: str
) -> QuarantinedFinding | None:
    """The record for one digest, or `None` if it was never quarantined."""
    return _by_digest(connection, workspace_id, finding_digest)


def read_findings(
    connection: sqlite3.Connection, *, workspace_id: str
) -> tuple[QuarantinedFinding, ...]:
    """Every record in one workspace, in recording order then digest order."""
    rows = connection.execute(
        f"SELECT {_SELECT} FROM {_TABLE} WHERE workspace_id = ? "
        "ORDER BY recorded_at_us, finding_digest",
        (workspace_id,),
    ).fetchall()
    return tuple(_record(row) for row in rows)


def _current_generation(connection: sqlite3.Connection) -> int:
    row = connection.execute(
        "SELECT fencing_generation FROM omnivia_workspace_state WHERE singleton = 1"
    ).fetchone()
    if row is None:
        raise ReviewFindingInvalid("no open workspace state to quarantine under")
    return int(row[0])


def _by_digest(
    connection: sqlite3.Connection, workspace_id: str, digest: str
) -> QuarantinedFinding | None:
    row = connection.execute(
        f"SELECT {_SELECT} FROM {_TABLE} WHERE workspace_id = ? AND finding_digest = ?",
        (workspace_id, digest),
    ).fetchone()
    return None if row is None else _record(row)


def _by_key(
    connection: sqlite3.Connection, workspace_id: str, key: str
) -> QuarantinedFinding | None:
    row = connection.execute(
        f"SELECT {_SELECT} FROM {_TABLE} WHERE workspace_id = ? AND idempotency_key = ?",
        (workspace_id, key),
    ).fetchone()
    return None if row is None else _record(row)


def _refusals(
    envelope: ReviewFindingEnvelope, idempotency_key: str, generation: int
) -> list[str]:
    """The names of every field outside its shape, never the values themselves."""
    refused: list[str] = []
    identifiers = {
        "workspace_id": envelope.workspace_id,
        "candidate_id": envelope.candidate_id,
        "evidence_id": envelope.evidence_id,
        "attributed_to": envelope.attributed_to,
        "idempotency_key": idempotency_key,
    }
    refused += [name for name, value in identifiers.items() if not is_identifier(value)]
    if envelope.run_id is not None and not is_identifier(envelope.run_id):
        refused.append("run_id")
    if envelope.observed_binding is not None and not is_identifier(envelope.observed_binding):
        refused.append("observed_binding")
    if not _is_digest(envelope.content_digest):
        refused.append("content_digest")
    if not isinstance(envelope.reason, str) or envelope.reason not in REASONS:
        refused.append("reason")
    if envelope.observed_generation is not None and not _bounded(envelope.observed_generation, 0):
        refused.append("observed_generation")
    if not _bounded(envelope.recorded_at_us, 1):
        refused.append("recorded_at_us")
    if not _bounded(generation, 1):
        refused.append("quarantined_under_generation")
    if refused:
        return refused
    if not _reason_shape(envelope, generation):
        refused.append("reason shape")
    return refused


def _reason_shape(envelope: ReviewFindingEnvelope, generation: int) -> bool:
    observed = envelope.observed_generation
    match envelope.reason:
        case "stale_generation":
            return (
                envelope.run_id is not None
                and observed is not None
                and envelope.observed_binding is not None
                and observed < generation
            )
        case "missing_generation":
            return envelope.run_id is not None and observed is None
        case "missing_workspace":
            return envelope.run_id is not None and envelope.observed_binding is None
        case "missing_run":
            return envelope.run_id is None
    return False


def _finding_digest(envelope: ReviewFindingEnvelope) -> str:
    return f"sha256:{sha256(to_canonical_json(asdict(envelope)).encode('utf-8')).hexdigest()}"


def _is_digest(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 71
        and value.startswith("sha256:")
        and all(char in "0123456789abcdef" for char in value[7:])
    )


def _bounded(value: object, least: int) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and least <= value <= _INT64_MAX
    )


def _record(row: tuple[Any, ...]) -> QuarantinedFinding:
    values: dict[str, Any] = dict(zip(_COLUMNS, row, strict=True))
    envelope = ReviewFindingEnvelope(
        workspace_id=values["workspace_id"],
        run_id=values["run_id"],
        candidate_id=values["candidate_id"],
        evidence_id=values["evidence_id"],
        content_digest=values["content_digest"],
        reason=values["reason"],
        observed_generation=values["observed_generation"],
        observed_binding=values["observed_binding"],
        attributed_to=values["attributed_to"],
        recorded_at_us=values["recorded_at_us"],
    )
    key = values["idempotency_key"]
    generation = values["quarantined_under_generation"]
    if not isinstance(key, str) or not _bounded(generation, 1):
        raise ReviewFindingInvalid("stored review finding quarantine is malformed")
    refused = _refusals(envelope, key, generation)
    if refused:
        raise ReviewFindingInvalid(
            f"stored review finding quarantine is outside its closed shape: {refused}"
        )
    if values["finding_digest"] != _finding_digest(envelope):
        raise ReviewFindingInvalid("stored review finding quarantine does not verify its digest")
    return QuarantinedFinding(
        finding_digest=values["finding_digest"],
        idempotency_key=key,
        quarantined_under_generation=generation,
        envelope=envelope,
    )
