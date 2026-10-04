"""Domain rules for the routine-flow entry: a task-context export and an outcome request (DEV-REQ-159, DEV-REQ-008).

Core does not assemble task context. A Dev handoff is assembled elsewhere, carries its own
`contentIdentity`, and arrives here as data. This module checks that the handoff has the closed shape
the contract describes, recomputes its identity with the same canonical algorithm the assembler uses,
and refuses it if the recomputed identity disagrees. An export is then one fixed, Core-owned projection
of that handoff: an allowlist of its content sections, a fixed set of redaction patterns, and the
workspace, principal, fence and budgets it was produced under. Its identity is the SHA-256 of its own
canonical document, so the identifier names exactly what was recorded.

An outcome request names an export by identifier and carries a bounded objective verbatim. Its
workspace and requester come from the authenticated grant, and it is refused unless the export it names is
in the workspace, was recorded under the current fence, verifies against its own identity, and was
produced under the policy this build still serves.

Everything here is pure. The storage seam is `storage/task_context.py`, which derives every stored column
from the export document and verifies it again on read. The service seam that opens the fenced transaction
is `service/handlers/task_context.py`. Refusals carry a closed reason and no caller or stored value.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from typing import Any, Final, TypeGuard

from omnivia_core.contracts.v1 import to_canonical_json
from omnivia_core_runtime.storage.task_context import (
    StoredExport,
    StoredOutcomeRequest,
    token_estimate,
)

HANDOFF_ADAPTER: Final = "task-context-handoff"
HANDOFF_OPERATION: Final = "task_context.assemble"
EXPORT_ADAPTER: Final = "canonical-export"
EXPORT_OPERATION: Final = "task_context.export"
EXPORT_KIND: Final = "task-context-handoff"
TOKEN_ESTIMATOR: Final = "utf8-ceil4-v1"

#: Core ceilings for an explicit budget. The byte ceiling keeps one exported document within a single
#: read response on every transport. ponytail: fixed ceilings; raise them only when a real caller needs more.
MAX_TOKEN_BUDGET: Final = 4_000_000
MAX_BYTE_BUDGET: Final = 1_048_576
MAX_OBJECTIVE_BYTES: Final = 8192
MAX_HANDOFF_OMISSIONS: Final = 20

#: The keys a handoff carries, exactly. A handoff is closed: an extra or a missing key is not a handoff.
HANDOFF_KEYS: Final = frozenset(
    {
        "adapter",
        "operation",
        "project",
        "target",
        "revision",
        "objective",
        "constraints",
        "plan",
        "completedChanges",
        "currentResults",
        "generationId",
        "assembledAt",
        "omissions",
        "omissionOverflow",
        "tokenBudget",
        "byteBudget",
        "tokenEstimator",
        "contentIdentity",
        "tokenEstimate",
        "byteEstimate",
        "sourceReferences",
        "truncated",
    }
)

#: Exactly the fields the assembler excludes from a handoff's identity: the assembly clock, the envelope's
#: budgets and estimates, the omission bookkeeping and the truncation flag. This is the algorithm Dev's
#: `task_context.handoff.VOLATILE_HANDOFF_FIELDS` names, so an identity computed here matches Dev's.
VOLATILE_HANDOFF_FIELDS: Final = frozenset(
    {
        "assembledAt",
        "byteBudget",
        "byteEstimate",
        "contentIdentity",
        "omissionOverflow",
        "omissions",
        "tokenBudget",
        "tokenEstimate",
        "tokenEstimator",
        "truncated",
    }
)

_TEXT_FIELDS: Final = (
    "project",
    "target",
    "revision",
    "objective",
    "constraints",
    "plan",
    "completedChanges",
    "currentResults",
    "assembledAt",
)
_TASK_SOURCE_MAP_KEYS: Final = frozenset(
    {"kind", "generationId", "path", "relationship", "reason", "confidence"}
)
_CONTEXT_PACK_KEYS: Final = frozenset({"kind", "packId", "captureAuthorization"})
_OMISSION_KEYS: Final = frozenset({"kind", "identifier", "reason"})
_HEX64: Final = re.compile(r"[0-9a-f]{64}")

#: The content sections an export carries, exactly. Every other handoff field is withheld and listed.
EXPORT_CONTENT_FIELDS: Final = frozenset(
    {
        "project",
        "target",
        "revision",
        "objective",
        "plan",
        "completedChanges",
        "currentResults",
        "sourceReferences",
        "generationId",
    }
)

#: The fixed redaction patterns, applied to every string inside the exported content in this order. The
#: names and expressions are Dev's C13a egress defaults, so a redaction here matches the same text there.
REDACTION_PATTERNS: Final[Mapping[str, str]] = {
    "email": r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
    "bearer": r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+",
    "secret_assignment": r"(?i)(?:token|secret|password|api_?key)\s*[:=]\s*\S+",
}
_COMPILED_PATTERNS: Final = tuple(
    (name, re.compile(expression)) for name, expression in REDACTION_PATTERNS.items()
)

REFUSED_HANDOFF_MISSING: Final = "handoff_missing"
REFUSED_HANDOFF_INVALID: Final = "handoff_invalid"
REFUSED_BUDGET_INVALID: Final = "budget_invalid"
REFUSED_BUDGET_INSUFFICIENT: Final = "budget_insufficient"
REFUSED_SIZE_EXCEEDED: Final = "size_exceeded"
REFUSED_OBJECTIVE_INVALID: Final = "objective_invalid"
REFUSED_OBJECTIVE_UNBOUNDED: Final = "objective_unbounded"
REFUSED_NOT_FOUND: Final = "not_found"
REFUSED_STALE_FENCE: Final = "stale_fence"
REFUSED_INELIGIBLE: Final = "ineligible"


class TaskContextRefused(Exception):
    """A task-context operation was refused for one closed, named reason."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def _canonical(payload: Mapping[str, Any]) -> str:
    return to_canonical_json(payload)


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _plain(value: object) -> Any:
    """A JSON-shaped copy of a decoded value. Wire mappings are read-only, so a copy is what gets hashed."""
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _is_plain_int(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_text(value: object) -> bool:
    return isinstance(value, str)


def _is_text_or_null(value: object) -> bool:
    return value is None or isinstance(value, str)


def _is_count(value: object) -> bool:
    return _is_plain_int(value) and value >= 0


def _is_number_or_null(value: object) -> bool:
    return value is None or (isinstance(value, (int, float)) and not isinstance(value, bool))


def _is_hex64(value: object) -> bool:
    return isinstance(value, str) and _HEX64.fullmatch(value) is not None


def _valid_reference(reference: object) -> bool:
    if not isinstance(reference, dict):
        return False
    kind = reference.get("kind")
    if kind == "task-source-map":
        return (
            set(reference) == _TASK_SOURCE_MAP_KEYS
            and _is_text_or_null(reference["generationId"])
            and _is_text(reference["path"])
            and _is_text(reference["relationship"])
            and _is_text(reference["reason"])
            and _is_number_or_null(reference["confidence"])
        )
    if kind == "context-pack":
        return (
            set(reference) == _CONTEXT_PACK_KEYS
            and _is_text(reference["packId"])
            and _is_hex64(reference["captureAuthorization"])
        )
    return False


def _valid_omission(omission: object) -> bool:
    return (
        isinstance(omission, dict)
        and set(omission) == _OMISSION_KEYS
        and _is_text(omission["kind"])
        and _is_text_or_null(omission["identifier"])
        and _is_text(omission["reason"])
    )


def _check_handoff_shape(handoff: Mapping[str, Any]) -> None:
    """Refuse anything outside the closed handoff shape. The identity is only meaningful after this."""
    valid = (
        set(handoff) == HANDOFF_KEYS
        and handoff["adapter"] == HANDOFF_ADAPTER
        and handoff["operation"] == HANDOFF_OPERATION
        and handoff["tokenEstimator"] == TOKEN_ESTIMATOR
        and all(_is_text(handoff[name]) for name in _TEXT_FIELDS)
        and _is_text_or_null(handoff["generationId"])
        and isinstance(handoff["omissions"], list)
        and len(handoff["omissions"]) <= MAX_HANDOFF_OMISSIONS
        and all(_valid_omission(item) for item in handoff["omissions"])
        and _is_count(handoff["omissionOverflow"])
        and _is_plain_int(handoff["tokenBudget"])
        and _is_plain_int(handoff["byteBudget"])
        and _is_hex64(handoff["contentIdentity"])
        and _is_count(handoff["tokenEstimate"])
        and _is_count(handoff["byteEstimate"])
        and isinstance(handoff["sourceReferences"], list)
        and all(_valid_reference(item) for item in handoff["sourceReferences"])
        and isinstance(handoff["truncated"], bool)
    )
    if not valid:
        raise TaskContextRefused(REFUSED_HANDOFF_INVALID, "the handoff is outside its closed shape")


def handoff_identity(handoff: Mapping[str, Any]) -> str:
    """The identity a handoff delivered, recomputed from its own content.

    The same algorithm the assembler uses: SHA-256 over the canonical JSON of the handoff with the volatile
    fields left out. A holder of the handoff alone can compute it, and a changed field changes it.
    """
    stable = {key: value for key, value in handoff.items() if key not in VOLATILE_HANDOFF_FIELDS}
    return _digest(_canonical(stable))


def verify_handoff(handoff: object) -> str:
    """Return the handoff's identity once it is shaped correctly and recomputes to the one it records.

    A missing, empty or refused handoff is `handoff_missing`. Anything else that is not a handoff, or
    whose content does not recompute to its recorded `contentIdentity`, is `handoff_invalid`.
    """
    if not isinstance(handoff, Mapping) or not handoff or handoff.get("refused") is True:
        raise TaskContextRefused(REFUSED_HANDOFF_MISSING, "no usable handoff was supplied")
    plain = _plain(handoff)
    _check_handoff_shape(plain)
    try:
        identity = handoff_identity(plain)
    except (TypeError, ValueError, UnicodeEncodeError) as error:
        raise TaskContextRefused(REFUSED_HANDOFF_INVALID, "the handoff is outside its closed shape") from error
    if identity != plain["contentIdentity"]:
        raise TaskContextRefused(
            REFUSED_HANDOFF_INVALID, "the handoff does not verify against its recorded identity"
        )
    return identity


def check_budgets(token_budget: object, byte_budget: object) -> None:
    """Both explicit budgets are plain positive integers within the Core ceilings. Nothing is coerced."""
    if not _is_plain_int(token_budget) or not 1 <= token_budget <= MAX_TOKEN_BUDGET:
        raise TaskContextRefused(REFUSED_BUDGET_INVALID, "the token budget is outside its bounds")
    if not _is_plain_int(byte_budget) or not 1 <= byte_budget <= MAX_BYTE_BUDGET:
        raise TaskContextRefused(REFUSED_BUDGET_INVALID, "the byte budget is outside its bounds")


def _redact(value: Any, counts: dict[str, int]) -> Any:
    """Apply the fixed patterns to every string in `value`, counting each pattern's matches."""
    if isinstance(value, str):
        for name, pattern in _COMPILED_PATTERNS:
            value, matches = pattern.subn(f"[redacted:{name}]", value)
            if matches:
                counts[name] = counts.get(name, 0) + matches
        return value
    if isinstance(value, dict):
        return {key: _redact(item, counts) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item, counts) for item in value]
    return value


def _policy_digest() -> str:
    return _digest(
        _canonical(
            {
                "exportKind": EXPORT_KIND,
                "fields": sorted(EXPORT_CONTENT_FIELDS),
                "patterns": dict(REDACTION_PATTERNS),
            }
        )
    )


#: The digest of the one fixed policy this build serves. An export carries it, and an export recorded under
#: any other digest is ineligible for an outcome request.
POLICY_DIGEST: Final = _policy_digest()


def build_export(
    *,
    workspace_id: str,
    principal: str,
    fencing_generation: int,
    handoff: object,
    token_budget: int,
    byte_budget: int,
    created_at_us: int,
) -> StoredExport:
    """Project one verified handoff into one bounded, content-addressed export, or refuse it.

    The budgets are checked before the handoff, so a malformed budget is reported as one. An export is
    never truncated: a document that does not fit its explicit budget is refused. A budget that cannot even
    hold the document without its content is `budget_insufficient`. Anything larger is `size_exceeded`.
    """
    check_budgets(token_budget, byte_budget)
    source_identity = verify_handoff(handoff)
    plain = _plain(handoff)

    counts: dict[str, int] = {}
    content = {name: _redact(plain[name], counts) for name in sorted(EXPORT_CONTENT_FIELDS)}
    document: dict[str, Any] = {
        "adapter": EXPORT_ADAPTER,
        "operation": EXPORT_OPERATION,
        "exportKind": EXPORT_KIND,
        "workspaceId": workspace_id,
        "exportedBy": principal,
        "sourceHandoffIdentity": source_identity,
        "policyDigest": POLICY_DIGEST,
        "fencingGeneration": fencing_generation,
        "tokenBudget": token_budget,
        "byteBudget": byte_budget,
        "tokenEstimator": TOKEN_ESTIMATOR,
        "withheldFields": sorted(name for name in plain if name not in EXPORT_CONTENT_FIELDS),
        "redactions": [{"pattern": name, "count": counts[name]} for name in sorted(counts)],
        "truncated": False,
        "content": content,
    }
    try:
        text = _canonical(document)
        byte_length = len(text.encode("utf-8"))
        envelope_length = len(
            _canonical({**document, "content": {}, "withheldFields": [], "redactions": []}).encode("utf-8")
        )
    except (TypeError, ValueError, UnicodeEncodeError) as error:
        raise TaskContextRefused(REFUSED_HANDOFF_INVALID, "the handoff is outside its closed shape") from error

    if envelope_length > byte_budget or token_estimate(envelope_length) > token_budget:
        raise TaskContextRefused(REFUSED_BUDGET_INSUFFICIENT, "the budget cannot hold the export envelope")
    if byte_length > byte_budget or token_estimate(byte_length) > token_budget:
        raise TaskContextRefused(REFUSED_SIZE_EXCEEDED, "the export does not fit its explicit budget")
    return StoredExport(text, created_at_us)


def validate_objective(objective: object) -> str:
    """A non-empty natural-language objective, bounded in bytes, kept verbatim."""
    if not isinstance(objective, str):
        raise TaskContextRefused(REFUSED_OBJECTIVE_INVALID, "the objective must be text")
    try:
        encoded = objective.encode("utf-8")
    except UnicodeEncodeError as error:
        raise TaskContextRefused(REFUSED_OBJECTIVE_INVALID, "the objective is not valid text") from error
    if not objective.strip():
        raise TaskContextRefused(REFUSED_OBJECTIVE_INVALID, "the objective is empty")
    if len(encoded) > MAX_OBJECTIVE_BYTES:
        raise TaskContextRefused(REFUSED_OBJECTIVE_UNBOUNDED, "the objective exceeds its byte bound")
    return objective


def build_outcome_request(
    *,
    workspace_id: str,
    principal: str,
    objective: str,
    export: StoredExport,
    current_generation: int,
    created_at_us: int,
) -> StoredOutcomeRequest:
    """Receive one outcome request against a stored export, or refuse it.

    The export must be in this workspace, recorded under the current fence, and produced under the policy
    this build serves. A request names the export by identity and does not re-derive it. The workspace and
    the requester are the authenticated caller's, supplied by the handler and never read from the payload.
    """
    objective = validate_objective(objective)
    columns = export.columns()
    if columns["workspace_id"] != workspace_id:
        raise TaskContextRefused(REFUSED_NOT_FOUND, "the export is not in this workspace")
    if columns["fencing_generation"] != current_generation:
        raise TaskContextRefused(REFUSED_STALE_FENCE, "the export was recorded under an earlier fence")
    if columns["policy_digest"] != POLICY_DIGEST:
        raise TaskContextRefused(REFUSED_INELIGIBLE, "the export was produced under another policy")
    return StoredOutcomeRequest(
        workspace_id=workspace_id,
        requested_by=principal,
        objective=objective,
        export_id=columns["export_id"],
        source_handoff_identity=columns["source_handoff_identity"],
        fencing_generation=current_generation,
        created_at_us=created_at_us,
    )


__all__ = [
    "EXPORT_CONTENT_FIELDS",
    "HANDOFF_KEYS",
    "MAX_BYTE_BUDGET",
    "MAX_OBJECTIVE_BYTES",
    "MAX_TOKEN_BUDGET",
    "POLICY_DIGEST",
    "REDACTION_PATTERNS",
    "REFUSED_BUDGET_INSUFFICIENT",
    "REFUSED_BUDGET_INVALID",
    "REFUSED_HANDOFF_INVALID",
    "REFUSED_HANDOFF_MISSING",
    "REFUSED_INELIGIBLE",
    "REFUSED_NOT_FOUND",
    "REFUSED_OBJECTIVE_INVALID",
    "REFUSED_OBJECTIVE_UNBOUNDED",
    "REFUSED_SIZE_EXCEEDED",
    "REFUSED_STALE_FENCE",
    "TaskContextRefused",
    "build_export",
    "build_outcome_request",
    "check_budgets",
    "handoff_identity",
    "validate_objective",
    "verify_handoff",
]
