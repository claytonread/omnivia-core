"""Explicit cross-Project knowledge sharing records (DEV-REQ-081; migration 0067).

Persistence only, in the shape of `storage/completion_decisions.py`: `record_share` and
`record_decision` expect their caller to be inside a `fenced_transaction`, and the service seam in
`service/knowledge_sharing.py` opens that fence. A share is an owner's proposal that one sealed,
canonical governed version be visible to one recipient Project. A decision is the accepted or
revoked state of that proposal. Nothing here decides eligibility from anything but these rows, and
nothing here reads the governed version it names.

Identity is the canonical share body. `share_digest` is the SHA-256 of that body's canonical JSON, so
an exact replay is the same proposal and returns the stored row. A different body under an existing
`share_id`, or the same body under another `share_id`, is a `KnowledgeShareConflict`. A decision is
keyed by `(share_id, decision)`: an exact replay returns the stored decision, and a different actor for
the same decision is a conflict. Reads re-derive the digest from the columns, so an altered row reads as
`KnowledgeShareInvalid` rather than as another share.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Final

from omnivia_core.contracts.v1 import (
    is_identifier,
    is_record_domain_scope,
    to_canonical_json,
)

DECISION_ACCEPTED: Final = "accepted"
DECISION_REVOKED: Final = "revoked"
_DECISIONS: Final = frozenset({DECISION_ACCEPTED, DECISION_REVOKED})

_SHARES: Final = "omnivia_knowledge_shares"
_DECISIONS_TABLE: Final = "omnivia_knowledge_share_decisions"
_INT64_MAX: Final = 2**63 - 1
_SHARE_COLUMNS: Final = (
    "workspace_id",
    "share_id",
    "share_digest",
    "source_project_id",
    "recipient_project_id",
    "governed_record_id",
    "governed_assembly_id",
    "governed_record_version_id",
    "domain_scope",
    "content_digest",
    "proposed_by",
    "proposed_under_generation",
    "proposed_at_us",
)
_SHARE_SELECT: Final = ", ".join(_SHARE_COLUMNS)
_SHARE_INSERT: Final = (
    f"INSERT INTO {_SHARES} ({_SHARE_SELECT}) "
    f"VALUES ({', '.join(':' + column for column in _SHARE_COLUMNS)})"
)
_DECISION_COLUMNS: Final = (
    "workspace_id",
    "share_id",
    "decision",
    "decided_by",
    "decided_under_generation",
    "decided_at_us",
)
_DECISION_SELECT: Final = ", ".join(_DECISION_COLUMNS)
_DECISION_INSERT: Final = (
    f"INSERT INTO {_DECISIONS_TABLE} ({_DECISION_SELECT}) "
    f"VALUES ({', '.join(':' + column for column in _DECISION_COLUMNS)})"
)


class KnowledgeShareInvalid(ValueError):
    """A share or decision, or a stored row read back, is outside its closed shape."""


class KnowledgeShareConflict(ValueError):
    """A share id or body, or a decision, already names a different fact."""


@dataclass(frozen=True, slots=True)
class KnowledgeShare:
    """One owner-proposed sharing of one sealed, canonical governed version to one recipient Project."""

    workspace_id: str
    share_id: str
    source_project_id: str
    recipient_project_id: str
    governed_record_id: str
    governed_assembly_id: str
    governed_record_version_id: str
    domain_scope: str
    content_digest: str
    proposed_by: str
    proposed_under_generation: int
    proposed_at_us: int

    def __post_init__(self) -> None:
        identifiers = (
            self.workspace_id,
            self.share_id,
            self.source_project_id,
            self.recipient_project_id,
            self.governed_record_id,
            self.governed_assembly_id,
            self.governed_record_version_id,
            self.proposed_by,
        )
        if not all(is_identifier(value) for value in identifiers):
            raise KnowledgeShareInvalid("a share identity is outside its closed shape")
        if self.source_project_id == self.recipient_project_id:
            raise KnowledgeShareInvalid(
                "a share must name a recipient other than its source"
            )
        if not is_record_domain_scope(self.domain_scope):
            raise KnowledgeShareInvalid("domain_scope is outside its closed shape")
        if not _is_digest(self.content_digest):
            raise KnowledgeShareInvalid("content_digest is outside its closed shape")
        if not _bounded(self.proposed_under_generation, 1) or not _bounded(
            self.proposed_at_us, 1
        ):
            raise KnowledgeShareInvalid(
                "a share generation or time is outside its closed shape"
            )

    @property
    def share_digest(self) -> str:
        return f"sha256:{sha256(to_canonical_json(self.to_body()).encode('utf-8')).hexdigest()}"

    def to_body(self) -> dict[str, Any]:
        return {
            "workspace_id": self.workspace_id,
            "share_id": self.share_id,
            "source_project_id": self.source_project_id,
            "recipient_project_id": self.recipient_project_id,
            "governed_record_id": self.governed_record_id,
            "governed_assembly_id": self.governed_assembly_id,
            "governed_record_version_id": self.governed_record_version_id,
            "domain_scope": self.domain_scope,
            "content_digest": self.content_digest,
            "proposed_by": self.proposed_by,
        }


@dataclass(frozen=True, slots=True)
class ShareDecision:
    """The accepted or revoked state of one share, with who recorded it and under which generation."""

    workspace_id: str
    share_id: str
    decision: str
    decided_by: str
    decided_under_generation: int
    decided_at_us: int

    def __post_init__(self) -> None:
        if self.decision not in _DECISIONS or not is_identifier(self.workspace_id):
            raise KnowledgeShareInvalid("a share decision is outside its closed shape")
        if not is_identifier(self.share_id) or not is_identifier(self.decided_by):
            raise KnowledgeShareInvalid(
                "a share decision identity is outside its closed shape"
            )
        if not _bounded(self.decided_under_generation, 1) or not _bounded(
            self.decided_at_us, 1
        ):
            raise KnowledgeShareInvalid(
                "a decision generation or time is outside its closed shape"
            )


def record_share(
    connection: sqlite3.Connection, share: KnowledgeShare
) -> KnowledgeShare:
    """Persist `share` under the caller's fence, or return the exact share already stored.

    A different body under the same id, or the same body under another id, is refused and nothing is
    written.
    """
    existing = read_share(
        connection, workspace_id=share.workspace_id, share_id=share.share_id
    )
    if existing is not None:
        if existing.share_digest == share.share_digest:
            return existing
        raise KnowledgeShareConflict("the share id already names a different proposal")
    connection.execute(
        _SHARE_INSERT,
        {
            "workspace_id": share.workspace_id,
            "share_id": share.share_id,
            "share_digest": share.share_digest,
            "source_project_id": share.source_project_id,
            "recipient_project_id": share.recipient_project_id,
            "governed_record_id": share.governed_record_id,
            "governed_assembly_id": share.governed_assembly_id,
            "governed_record_version_id": share.governed_record_version_id,
            "domain_scope": share.domain_scope,
            "content_digest": share.content_digest,
            "proposed_by": share.proposed_by,
            "proposed_under_generation": share.proposed_under_generation,
            "proposed_at_us": share.proposed_at_us,
        },
    )
    return share


def read_share(
    connection: sqlite3.Connection, *, workspace_id: str, share_id: str
) -> KnowledgeShare | None:
    """The stored share, with its digest re-derived from the row, or `None`."""
    row = connection.execute(
        f"SELECT {_SHARE_SELECT} FROM {_SHARES} WHERE workspace_id = ? AND share_id = ?",
        (workspace_id, share_id),
    ).fetchone()
    if row is None:
        return None
    values: dict[str, Any] = dict(zip(_SHARE_COLUMNS, row, strict=True))
    digest = values.pop("share_digest")
    try:
        share = KnowledgeShare(**values)
    except (KnowledgeShareInvalid, TypeError) as error:
        raise KnowledgeShareInvalid("stored knowledge share is malformed") from error
    if share.share_digest != digest:
        raise KnowledgeShareInvalid("stored knowledge share does not verify its digest")
    return share


def record_decision(
    connection: sqlite3.Connection, decision: ShareDecision
) -> ShareDecision:
    """Persist `decision` under the caller's fence, or return the exact decision already stored.

    A revocation without an acceptance, an acceptance after a revocation, and a second decision of the
    same kind by a different actor are refused and nothing is written. The lifecycle checks run before
    the replay check, so a replayed acceptance after a revocation is still refused.
    """
    existing = read_decisions(
        connection, workspace_id=decision.workspace_id, share_id=decision.share_id
    )
    if decision.decision == DECISION_REVOKED and DECISION_ACCEPTED not in existing:
        raise KnowledgeShareInvalid("a share can be revoked only after it is accepted")
    if decision.decision == DECISION_ACCEPTED and DECISION_REVOKED in existing:
        raise KnowledgeShareConflict("a revoked share cannot be accepted")
    current = existing.get(decision.decision)
    if current is not None:
        if current.decided_by == decision.decided_by:
            return current
        raise KnowledgeShareConflict(
            "the share already carries a different decision of this kind"
        )
    connection.execute(
        _DECISION_INSERT,
        {
            "workspace_id": decision.workspace_id,
            "share_id": decision.share_id,
            "decision": decision.decision,
            "decided_by": decision.decided_by,
            "decided_under_generation": decision.decided_under_generation,
            "decided_at_us": decision.decided_at_us,
        },
    )
    return decision


def read_decisions(
    connection: sqlite3.Connection, *, workspace_id: str, share_id: str
) -> dict[str, ShareDecision]:
    """Every stored decision for one share, keyed by its kind, revalidated on read."""
    rows = connection.execute(
        f"SELECT {_DECISION_SELECT} FROM {_DECISIONS_TABLE} "
        "WHERE workspace_id = ? AND share_id = ? ORDER BY decided_at_us, decision",
        (workspace_id, share_id),
    ).fetchall()
    decisions: dict[str, ShareDecision] = {}
    for row in rows:
        values = dict(zip(_DECISION_COLUMNS, row, strict=True))
        try:
            decisions[values["decision"]] = ShareDecision(**values)
        except KnowledgeShareInvalid as error:
            raise KnowledgeShareInvalid("stored share decision is malformed") from error
    return decisions


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
