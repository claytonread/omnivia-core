"""Task-context exports and outcome requests: persistence only (migration 0068).

Shaped like `storage/knowledge_shares.py`. `record_export` and `record_outcome_request` expect their
caller to be inside a `fenced_transaction`; the domain rules in `service/task_context.py` open that
fence. Nothing here decides what a payload may contain or whether a row is current.

Each record is derived from one value. An export is its canonical `document_json`: every column is read
from that document, so `export_id` is `tcx-` plus the SHA-256 of the exact bytes it stores, and the byte
and token estimates describe those bytes. An outcome request is identified by its canonical body, so
`outreq-` plus that SHA-256 is its id. Replaying identical content returns the stored row, and different
content can never occupy an existing id.

Reads rebuild the record from its stored document or fields and compare every column with the row. An
altered row therefore reads as `TaskContextInvalid`, never as another export or request.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Final

from omnivia_core.contracts.v1 import to_canonical_json

EXPORT_PREFIX: Final = "tcx-"
OUTCOME_PREFIX: Final = "outreq-"
OUTCOME_STATUS_RECEIVED: Final = "received"

_EXPORTS: Final = "omnivia_task_context_exports"
_REQUESTS: Final = "omnivia_outcome_requests"
_EXPORT_COLUMNS: Final = (
    "workspace_id",
    "export_id",
    "content_identity",
    "exported_by",
    "source_handoff_identity",
    "policy_digest",
    "fencing_generation",
    "token_budget",
    "byte_budget",
    "byte_estimate",
    "token_estimate",
    "created_at_us",
    "document_json",
)
_REQUEST_COLUMNS: Final = (
    "workspace_id",
    "outcome_request_id",
    "requested_by",
    "objective",
    "export_id",
    "source_handoff_identity",
    "status",
    "fencing_generation",
    "created_at_us",
)
_EXPORT_SELECT: Final = ", ".join(_EXPORT_COLUMNS)
_EXPORT_INSERT: Final = (
    f"INSERT INTO {_EXPORTS} ({_EXPORT_SELECT}) "
    f"VALUES ({', '.join(':' + column for column in _EXPORT_COLUMNS)})"
)
_REQUEST_SELECT: Final = ", ".join(_REQUEST_COLUMNS)
_REQUEST_INSERT: Final = (
    f"INSERT INTO {_REQUESTS} ({_REQUEST_SELECT}) "
    f"VALUES ({', '.join(':' + column for column in _REQUEST_COLUMNS)})"
)


class TaskContextInvalid(ValueError):
    """A value handed to storage, or a stored row read back, is outside its closed shape or fails its identity."""


def token_estimate(byte_length: int) -> int:
    """`utf8-ceil4-v1`: ceil(bytes / 4), the same proxy the Dev reference reports."""
    return -(-byte_length // 4)


def _sha256_hex(text: str) -> str:
    return sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class StoredExport:
    """One immutable export. Its canonical document is the only stored content; every column derives from it."""

    document_json: str
    created_at_us: int

    @classmethod
    def of(cls, document: dict[str, Any], created_at_us: int) -> StoredExport:
        return cls(to_canonical_json(document), created_at_us)

    @property
    def document(self) -> dict[str, Any]:
        document: dict[str, Any] = json.loads(self.document_json)
        return document

    @property
    def export_id(self) -> str:
        return str(self.columns()["export_id"])

    def columns(self) -> dict[str, Any]:
        """Every column this export must carry, read from its document. Raises when the document is malformed."""
        try:
            document = self.document
            if not isinstance(document, dict) or to_canonical_json(document) != self.document_json:
                raise TaskContextInvalid("task-context export document is not canonical")
            byte_estimate = len(self.document_json.encode("utf-8"))
            identity = _sha256_hex(self.document_json)
            return {
                "workspace_id": document["workspaceId"],
                "export_id": EXPORT_PREFIX + identity,
                "content_identity": identity,
                "exported_by": document["exportedBy"],
                "source_handoff_identity": document["sourceHandoffIdentity"],
                "policy_digest": document["policyDigest"],
                "fencing_generation": document["fencingGeneration"],
                "token_budget": document["tokenBudget"],
                "byte_budget": document["byteBudget"],
                "byte_estimate": byte_estimate,
                "token_estimate": token_estimate(byte_estimate),
                "created_at_us": self.created_at_us,
                "document_json": self.document_json,
            }
        except (KeyError, TypeError, ValueError) as error:
            raise TaskContextInvalid("task-context export is malformed") from error


@dataclass(frozen=True, slots=True)
class StoredOutcomeRequest:
    """One received request for an outcome, naming exactly one export and carrying the objective verbatim."""

    workspace_id: str
    requested_by: str
    objective: str
    export_id: str
    source_handoff_identity: str
    fencing_generation: int
    created_at_us: int

    @property
    def status(self) -> str:
        return OUTCOME_STATUS_RECEIVED

    @property
    def outcome_request_id(self) -> str:
        body = to_canonical_json(
            {
                "workspaceId": self.workspace_id,
                "requestedBy": self.requested_by,
                "objective": self.objective,
                "exportId": self.export_id,
                "sourceHandoffIdentity": self.source_handoff_identity,
            }
        )
        return OUTCOME_PREFIX + _sha256_hex(body)

    def columns(self) -> dict[str, Any]:
        return {
            "workspace_id": self.workspace_id,
            "outcome_request_id": self.outcome_request_id,
            "requested_by": self.requested_by,
            "objective": self.objective,
            "export_id": self.export_id,
            "source_handoff_identity": self.source_handoff_identity,
            "status": self.status,
            "fencing_generation": self.fencing_generation,
            "created_at_us": self.created_at_us,
        }


def record_export(connection: sqlite3.Connection, export: StoredExport) -> StoredExport:
    """Persist `export` under the caller's fence, or return the identical export already stored."""
    columns = export.columns()
    existing = read_export(
        connection, workspace_id=columns["workspace_id"], export_id=columns["export_id"]
    )
    if existing is not None:
        return existing
    connection.execute(_EXPORT_INSERT, columns)
    return export


def read_export(
    connection: sqlite3.Connection, *, workspace_id: str, export_id: str
) -> StoredExport | None:
    """The stored export, with every column re-derived from its document and compared, or `None`."""
    row = connection.execute(
        f"SELECT {_EXPORT_SELECT} FROM {_EXPORTS} WHERE workspace_id = ? AND export_id = ?",
        (workspace_id, export_id),
    ).fetchone()
    if row is None:
        return None
    values = dict(zip(_EXPORT_COLUMNS, row, strict=True))
    export = StoredExport(values["document_json"], values["created_at_us"])
    if export.columns() != values:
        raise TaskContextInvalid("stored task-context export does not verify its identity")
    return export


def record_outcome_request(
    connection: sqlite3.Connection, request: StoredOutcomeRequest
) -> StoredOutcomeRequest:
    """Persist `request` under the caller's fence, or return the identical request already stored."""
    existing = read_outcome_request(
        connection, workspace_id=request.workspace_id, outcome_request_id=request.outcome_request_id
    )
    if existing is not None:
        return existing
    connection.execute(_REQUEST_INSERT, request.columns())
    return request


def read_outcome_request(
    connection: sqlite3.Connection, *, workspace_id: str, outcome_request_id: str
) -> StoredOutcomeRequest | None:
    """The stored request, with its id re-derived from the row, or `None`."""
    row = connection.execute(
        f"SELECT {_REQUEST_SELECT} FROM {_REQUESTS} WHERE workspace_id = ? AND outcome_request_id = ?",
        (workspace_id, outcome_request_id),
    ).fetchone()
    if row is None:
        return None
    values = dict(zip(_REQUEST_COLUMNS, row, strict=True))
    request = StoredOutcomeRequest(
        workspace_id=values["workspace_id"],
        requested_by=values["requested_by"],
        objective=values["objective"],
        export_id=values["export_id"],
        source_handoff_identity=values["source_handoff_identity"],
        fencing_generation=values["fencing_generation"],
        created_at_us=values["created_at_us"],
    )
    if request.columns() != values:
        raise TaskContextInvalid("stored outcome request does not verify its identity")
    return request
