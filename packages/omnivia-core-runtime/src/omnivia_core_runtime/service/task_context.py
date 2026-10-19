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
from dataclasses import dataclass
from typing import Any, Final, TypeGuard

from omnivia_core.contracts.v1 import is_identifier, is_workspace_id, to_canonical_json
from omnivia_core_runtime.service.outcome_admission import (
    ACTION_READ,
    ACTION_SUBMIT,
    NO_OUTCOME_ADMISSIONS,
    SCOPES,
    DeclaredRoles,
    OutcomeAdmissionAuthority,
    OutcomeAdmissionRefused,
)
from omnivia_core_runtime.storage.task_context import (
    StoredExport,
    StoredOutcomeRequest,
    StoredProjectContext,
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
#: Structured admission (C08). A shape, bound, identity or declared-fact mismatch is `admission_invalid`. A Project,
#: Work, source or revision the Workspace does not bind is `admission_not_found`. A caller who is not an owner or
#: member of the Project is `not_member`. The active Project, or its generation, is not the one named, or an
#: export does not describe the Project, target and revision named, is a conflict, as is a closed lifecycle.
REFUSED_ADMISSION_INVALID: Final = "admission_invalid"
REFUSED_ADMISSION_NOT_FOUND: Final = "admission_not_found"
REFUSED_NOT_MEMBER: Final = "not_member"
REFUSED_CONTEXT_MISMATCH: Final = "context_mismatch"
REFUSED_EXPORT_MISMATCH: Final = "export_mismatch"
REFUSED_LIFECYCLE_CLOSED: Final = "lifecycle_closed"

#: Dev's admission summary, exactly. Revision is an integer from one; the labels say what kind of claim each field is.
ADMISSION_REVISION_MINIMUM: Final = 1
ADMISSION_ADAPTER: Final = "dev-task-admission"
ADMISSION_LABELS: Final[Mapping[str, str]] = {
    "adapter": ADMISSION_ADAPTER,
    "disposition": "draft-for-review",
    "executionState": "not-authorized",
    "bindingStatus": "declared-not-verified",
    "scopeStatus": "requested-not-granted",
    "roleStatus": "declared-not-authenticated",
}
#: Dev's admission ceiling for a byte budget. It is larger than the export's, which is Core's own bound.
MAX_ADMISSION_BYTE_BUDGET: Final = 16 * 1024 * 1024
#: Dev's list and text bounds on a summary, mirrored exactly and checked across both lists together.
MAX_ADMISSION_LIST: Final = 16
MAX_ADMISSION_TEXT_BYTES: Final = 512
MAX_ADMISSION_LIST_TOTAL_BYTES: Final = 4096
MAX_ADMISSION_SUMMARY_BYTES: Final = 65_536
MAX_CONTEXT_GENERATION: Final = 9_223_372_036_854_775_807
_CONTEXT_TOKEN: Final = re.compile(r"ctxgen-([1-9][0-9]{0,18})")
_IDENTITY: Final = re.compile(r"[0-9a-f]{64}")
_ADMISSION_KEYS: Final = frozenset({"summary", "identity"})
_SUMMARY_KEYS: Final = frozenset(
    {
        "revision",
        *ADMISSION_LABELS,
        "outcomeObjective",
        "appContext",
        "declaredBindings",
        "declaredRoles",
        "assumptions",
        "constraints",
        "requestedScopes",
        "budgets",
    }
)
_APP_KEYS: Final = frozenset({"appId", "surfaceId"})
_BINDING_KEYS: Final = frozenset(
    {
        "projectId",
        "workspaceId",
        "workId",
        "sourceTarget",
        "sourceRevision",
        "expectedContextGeneration",
    }
)
_ROLE_KEYS: Final = frozenset({"owner", "executor", "reviewer"})
_BUDGET_KEYS: Final = frozenset({"tokenBudget", "byteBudget", "tokenEstimator"})
#: How an admission decision's closed refusal maps onto the reasons above. Anything not listed is invalid.
_ADMISSION_REASON: Final[Mapping[str, str]] = {
    "unknown_project": REFUSED_ADMISSION_NOT_FOUND,
    "unknown_work": REFUSED_ADMISSION_NOT_FOUND,
    "unknown_source": REFUSED_ADMISSION_NOT_FOUND,
    "unknown_revision": REFUSED_ADMISSION_NOT_FOUND,
    "lifecycle_closed": REFUSED_LIFECYCLE_CLOSED,
}


class TaskContextRefused(Exception):
    """A task-context operation was refused for one closed, named reason."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def _canonical(payload: Mapping[str, Any]) -> str:
    return to_canonical_json(payload)


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def plain_copy(value: object) -> Any:
    """A JSON-shaped copy of a decoded value. Wire mappings are read-only, so a copy is what gets hashed."""
    if isinstance(value, Mapping):
        return {str(key): plain_copy(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain_copy(item) for item in value]
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
    plain = plain_copy(handoff)
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


def check_budgets(token_budget: object, byte_budget: object, *, byte_ceiling: int = MAX_BYTE_BUDGET) -> None:
    """Both explicit budgets are plain positive integers within their ceilings. Nothing is coerced."""
    if not _is_plain_int(token_budget) or not 1 <= token_budget <= MAX_TOKEN_BUDGET:
        raise TaskContextRefused(REFUSED_BUDGET_INVALID, "the token budget is outside its bounds")
    if not _is_plain_int(byte_budget) or not 1 <= byte_budget <= byte_ceiling:
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
    plain = plain_copy(handoff)

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


@dataclass(frozen=True, slots=True)
class ParsedAdmission:
    """One structured admission whose shape, bounds, labels and claimed identity hold. Not yet admitted."""

    summary: dict[str, Any]
    identity: str
    workspace_id: str
    project_id: str
    work_id: str
    objective: str
    source_target: str
    source_revision: str
    context_generation: int
    owner: str
    executor: str
    reviewer: str
    requested_scopes: tuple[str, ...]


def _admission_invalid() -> TaskContextRefused:
    return TaskContextRefused(
        REFUSED_ADMISSION_INVALID, "the admission is outside its closed shape or does not verify"
    )


def _closed(value: object, keys: frozenset[str]) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != keys:
        raise _admission_invalid()
    return value


def _utf8_size(value: str) -> int:
    try:
        return len(value.encode("utf-8"))
    except UnicodeEncodeError as error:
        raise _admission_invalid() from error


def _identifier(value: object) -> str:
    if not isinstance(value, str) or not is_identifier(value):
        raise _admission_invalid()
    return value


def _source_target(value: object) -> str:
    """Dev's rule for a source target: non-blank text of at most 512 bytes. Core matches it against its bindings."""
    if not isinstance(value, str) or not value.strip() or _utf8_size(value) > MAX_ADMISSION_TEXT_BYTES:
        raise _admission_invalid()
    return value


def _admitted_texts(assumptions: object, constraints: object) -> None:
    """Dev's list bounds, across both lists together: a count and an item size each, and one aggregate size."""
    total = 0
    for value in (assumptions, constraints):
        if not isinstance(value, list) or len(value) > MAX_ADMISSION_LIST:
            raise _admission_invalid()
        for item in value:
            if not isinstance(item, str) or not item.strip():
                raise _admission_invalid()
            size = _utf8_size(item)
            if size > MAX_ADMISSION_TEXT_BYTES:
                raise _admission_invalid()
            total += size
    if total > MAX_ADMISSION_LIST_TOTAL_BYTES:
        raise _admission_invalid()


def _scopes(value: object) -> tuple[str, ...]:
    """Dev's scope set in the order Dev emits it: one to three distinct scopes from the closed vocabulary, ascending."""
    if not isinstance(value, list) or not 1 <= len(value) <= len(SCOPES):
        raise _admission_invalid()
    if not all(isinstance(scope, str) and scope in SCOPES for scope in value):
        raise _admission_invalid()
    if len(set(value)) != len(value) or tuple(value) != tuple(sorted(value)):
        raise _admission_invalid()
    return tuple(value)


def _context_generation(value: object) -> int:
    """The positive generation a `ctxgen-` token names. Anything else, including leading zeros, is invalid."""
    match = _CONTEXT_TOKEN.fullmatch(value) if isinstance(value, str) else None
    if match is None or int(match.group(1)) > MAX_CONTEXT_GENERATION:
        raise _admission_invalid()
    return int(match.group(1))


def parse_admission(admission: object) -> ParsedAdmission:
    """Check one structured admission against Dev's exact summary: its closed shape, labels, bounds, budgets and claimed identity.

    The identity is SHA-256 over the canonical JSON of the summary, which is Dev's `canonical_dumps` for this shape. A
    summary that is not canonical with its claimed identity is refused, and so is any extra or missing member at any
    level. Nothing here decides whether the admission is admitted. That is `admit_structured`.
    """
    entry = _closed(plain_copy(admission), _ADMISSION_KEYS)
    summary = _closed(entry["summary"], _SUMMARY_KEYS)
    try:
        canonical = to_canonical_json(summary)
    except (TypeError, ValueError, UnicodeEncodeError) as error:
        raise _admission_invalid() from error
    identity = entry["identity"]
    size = _utf8_size(canonical)
    if (
        size > MAX_ADMISSION_SUMMARY_BYTES
        or not isinstance(identity, str)
        or _IDENTITY.fullmatch(identity) is None
        or _digest(canonical) != identity
    ):
        raise _admission_invalid()
    revision = summary["revision"]
    if not _is_plain_int(revision) or revision < ADMISSION_REVISION_MINIMUM:
        raise _admission_invalid()
    for key, label in ADMISSION_LABELS.items():
        if summary[key] != label:
            raise _admission_invalid()
    app = _closed(summary["appContext"], _APP_KEYS)
    bindings = _closed(summary["declaredBindings"], _BINDING_KEYS)
    roles = _closed(summary["declaredRoles"], _ROLE_KEYS)
    budgets = _closed(summary["budgets"], _BUDGET_KEYS)
    workspace_id = bindings["workspaceId"]
    if not isinstance(workspace_id, str) or not is_workspace_id(workspace_id):
        raise _admission_invalid()
    _identifier(app["appId"])
    _identifier(app["surfaceId"])
    owner, executor, reviewer = (_identifier(roles[role]) for role in ("owner", "executor", "reviewer"))
    if len({owner, executor, reviewer}) != 3:
        raise _admission_invalid()
    _admitted_texts(summary["assumptions"], summary["constraints"])
    requested = _scopes(summary["requestedScopes"])
    if budgets["tokenEstimator"] != TOKEN_ESTIMATOR:
        raise _admission_invalid()
    token_budget, byte_budget = budgets["tokenBudget"], budgets["byteBudget"]
    check_budgets(token_budget, byte_budget, byte_ceiling=MAX_ADMISSION_BYTE_BUDGET)
    if size > byte_budget or token_estimate(size) > token_budget:
        raise TaskContextRefused(
            REFUSED_BUDGET_INSUFFICIENT, "the summary does not fit its explicit budget"
        )
    return ParsedAdmission(
        summary=dict(summary),
        identity=identity,
        workspace_id=workspace_id,
        project_id=_identifier(bindings["projectId"]),
        work_id=_identifier(bindings["workId"]),
        objective=validate_objective(summary["outcomeObjective"]),
        source_target=_source_target(bindings["sourceTarget"]),
        source_revision=_identifier(bindings["sourceRevision"]),
        context_generation=_context_generation(bindings["expectedContextGeneration"]),
        owner=owner,
        executor=executor,
        reviewer=reviewer,
        requested_scopes=requested,
    )


def check_standing(
    admission: ParsedAdmission,
    *,
    principal: str,
    authority: OutcomeAdmissionAuthority,
    active: StoredProjectContext | None,
) -> None:
    """The caller is an owner or member of the declared Project, and it is the active context at the named generation.

    The declared roles are claims a request makes, so they are never the caller's standing. This check runs before
    storage, and again on a replay, so a stored answer is not served to a caller who has since lost standing.
    """
    try:
        binding = authority.project(admission.project_id, ACTION_READ)
    except OutcomeAdmissionRefused as refused:
        raise TaskContextRefused(
            REFUSED_ADMISSION_NOT_FOUND, "no such Project is bound for this Workspace"
        ) from refused
    if principal not in binding.owners | binding.members:
        raise TaskContextRefused(
            REFUSED_NOT_MEMBER, "the caller is not an owner or member of the Project"
        )
    if (
        active is None
        or active.project_id != admission.project_id
        or active.context_generation != admission.context_generation
    ):
        raise TaskContextRefused(
            REFUSED_CONTEXT_MISMATCH,
            "the Project is not the active context at the generation the admission names",
        )


def admit_structured(
    admission: ParsedAdmission,
    *,
    objective: str,
    workspace_id: str,
    principal: str,
    export: StoredExport,
    authority: OutcomeAdmissionAuthority,
    active: StoredProjectContext | None,
) -> None:
    """Admit one structured request against its export and the composed authority, or refuse it.

    Checked in order: the summary's objective is the request's; its Workspace is the caller's; the caller stands in
    the declared Project and that Project is the active context at the named generation; the export names that same
    Project, target and revision; and the composed authority admits the Work target for a submission.
    """
    if admission.objective != objective:
        raise _admission_invalid()
    if admission.workspace_id != workspace_id:
        raise TaskContextRefused(
            REFUSED_ADMISSION_NOT_FOUND, "the declared Workspace is not the caller's"
        )
    check_standing(admission, principal=principal, authority=authority, active=active)
    content = export.document["content"]
    if (
        content.get("project") != admission.project_id
        or content.get("target") != admission.source_target
        or content.get("revision") != admission.source_revision
    ):
        raise TaskContextRefused(
            REFUSED_EXPORT_MISMATCH, "the export does not describe the Project, target and revision named"
        )
    try:
        authority.admit(
            project_id=admission.project_id,
            action=ACTION_SUBMIT,
            work_id=admission.work_id,
            target=admission.source_target,
            revision=admission.source_revision,
            scopes=admission.requested_scopes,
            roles=DeclaredRoles(
                owner=admission.owner,
                executor=admission.executor,
                reviewer=admission.reviewer,
            ),
        )
    except OutcomeAdmissionRefused as refused:
        raise TaskContextRefused(
            _ADMISSION_REASON.get(refused.reason, REFUSED_ADMISSION_INVALID),
            "the admission is not admitted by the Project bindings",
        ) from refused


def build_outcome_request(
    *,
    workspace_id: str,
    principal: str,
    objective: str,
    export: StoredExport,
    current_generation: int,
    created_at_us: int,
    admission: ParsedAdmission | None = None,
    authority: OutcomeAdmissionAuthority = NO_OUTCOME_ADMISSIONS,
    active: StoredProjectContext | None = None,
) -> StoredOutcomeRequest:
    """Receive one outcome request against a stored export, or refuse it.

    The export must be in this workspace, recorded under the current fence, and produced under the policy this build
    serves. A request names the export by identity and does not re-derive it. The workspace and the requester are the
    authenticated caller's, supplied by the handler and never read from the payload. A structured request is also
    admitted (`admit_structured`) and records its accepted admission with the generation it was accepted under.
    """
    objective = validate_objective(objective)
    columns = export.columns()
    if columns["workspace_id"] != workspace_id:
        raise TaskContextRefused(REFUSED_NOT_FOUND, "the export is not in this workspace")
    if columns["fencing_generation"] != current_generation:
        raise TaskContextRefused(REFUSED_STALE_FENCE, "the export was recorded under an earlier fence")
    if columns["policy_digest"] != POLICY_DIGEST:
        raise TaskContextRefused(REFUSED_INELIGIBLE, "the export was produced under another policy")
    if admission is None:
        return StoredOutcomeRequest(
            workspace_id=workspace_id,
            requested_by=principal,
            objective=objective,
            export_id=columns["export_id"],
            source_handoff_identity=columns["source_handoff_identity"],
            fencing_generation=current_generation,
            created_at_us=created_at_us,
        )
    admit_structured(
        admission,
        objective=objective,
        workspace_id=workspace_id,
        principal=principal,
        export=export,
        authority=authority,
        active=active,
    )
    return StoredOutcomeRequest(
        workspace_id=workspace_id,
        requested_by=principal,
        objective=objective,
        export_id=columns["export_id"],
        source_handoff_identity=columns["source_handoff_identity"],
        fencing_generation=current_generation,
        created_at_us=created_at_us,
        project_id=admission.project_id,
        admission_json=to_canonical_json(admission.summary),
        context_generation=admission.context_generation,
    )


def decide_switch(
    *,
    workspace_id: str,
    principal: str,
    project_id: str,
    current: StoredProjectContext | None,
    authority: OutcomeAdmissionAuthority,
    fencing_generation: int,
    switched_at_us: int,
) -> StoredProjectContext:
    """The Workspace's active context after choosing `project_id`. Returns `current` itself when nothing changes.

    The Project must be bound in the composed authority, and the caller must be an owner or member of it. The first
    choice is generation one. A change to a different Project advances the generation by one. Choosing the Project
    that is already active is a no-op that keeps its generation.
    """
    try:
        binding = authority.project(project_id, ACTION_READ)
    except OutcomeAdmissionRefused as refused:
        raise TaskContextRefused(
            REFUSED_ADMISSION_NOT_FOUND, "no such Project is bound for this Workspace"
        ) from refused
    if principal not in binding.owners | binding.members:
        raise TaskContextRefused(
            REFUSED_NOT_MEMBER, "the caller is not an owner or member of the Project"
        )
    if current is not None and current.project_id == project_id:
        return current
    generation = 1 if current is None else current.context_generation + 1
    if generation > MAX_CONTEXT_GENERATION:
        raise TaskContextRefused(
            REFUSED_CONTEXT_MISMATCH, "the Project context generation cannot advance"
        )
    return StoredProjectContext(
        workspace_id=workspace_id,
        project_id=project_id,
        context_generation=generation,
        fencing_generation=fencing_generation,
        switched_by=principal,
        switched_at_us=switched_at_us,
    )


__all__ = [
    "ADMISSION_LABELS",
    "ADMISSION_REVISION_MINIMUM",
    "EXPORT_CONTENT_FIELDS",
    "HANDOFF_KEYS",
    "MAX_BYTE_BUDGET",
    "MAX_OBJECTIVE_BYTES",
    "MAX_TOKEN_BUDGET",
    "POLICY_DIGEST",
    "REDACTION_PATTERNS",
    "REFUSED_ADMISSION_INVALID",
    "REFUSED_ADMISSION_NOT_FOUND",
    "REFUSED_BUDGET_INSUFFICIENT",
    "REFUSED_BUDGET_INVALID",
    "REFUSED_CONTEXT_MISMATCH",
    "REFUSED_EXPORT_MISMATCH",
    "REFUSED_HANDOFF_INVALID",
    "REFUSED_HANDOFF_MISSING",
    "REFUSED_INELIGIBLE",
    "REFUSED_LIFECYCLE_CLOSED",
    "REFUSED_NOT_FOUND",
    "REFUSED_NOT_MEMBER",
    "REFUSED_OBJECTIVE_INVALID",
    "REFUSED_OBJECTIVE_UNBOUNDED",
    "REFUSED_SIZE_EXCEEDED",
    "REFUSED_STALE_FENCE",
    "ParsedAdmission",
    "TaskContextRefused",
    "admit_structured",
    "build_export",
    "build_outcome_request",
    "check_budgets",
    "check_standing",
    "decide_switch",
    "handoff_identity",
    "parse_admission",
    "validate_objective",
    "verify_handoff",
]
