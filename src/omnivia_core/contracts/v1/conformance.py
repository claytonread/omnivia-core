"""The shared adapter conformance corpus and its runner (A2.6, ADR-038/ADR-039).

One corpus, many adapters. The recorded exchanges in
``application-wire-adapter-conformance-v1.json`` state what this contract sounds
like on the wire; :func:`run_adapter_conformance` drives them through anything
satisfying :class:`~omnivia_core.contracts.v1.adapter.ApplicationWireAdapter` and
holds it to them. The in-process fake is only the first caller. When the HTTP
adapter arrives it is held to this same corpus, and "both adapters pass" then
means something stronger than "both adapters have tests".

The runner restates no *per-value* semantics. Whether an error code is one an
operation may raise, whether a retry class is right, how a replay classifies,
whether a job's state may move as it did, whether a governed transition is
attributed to the right actor -- each of those was frozen in an earlier slice and
is *called* here. A conformance runner that reimplemented those rules would be
able to pass an adapter that the real contract rejects, which is the one outcome
that would make the whole exercise worthless.

It does own a few rules, and they are all *relational*: what an honest replay
must answer with and what a conflict must be refused with
(:func:`_check_replay_outcome`), that a continuation token still binds the query
that produced it (:func:`_check_pagination`), that every workspace-scoped
identifier in a result is the one the request selected
(:func:`_check_result_belongs_to_the_selected_workspace`), and that a case's
declared principal is the one its response attests
(:func:`_check_principal_correlation`). Each concerns two exchanges, or an
exchange and the request that provoked it -- which is exactly what a validator
taking a single decoded value cannot express. They are listed rather than
described as "never restated", because a module that claims to own no semantics
while owning several teaches its readers to stop checking.

Which relationships those rules apply to is read from the exchanges rather than
from what a case declares: see :class:`_DerivedRelationships`.

What the runner adds is the thing no single-operation validator can see: that a
recorded exchange is internally coherent end to end -- the request the caller
sent really is the request the catalogue describes, the response really is the
one the adapter returns, the values survive the trip in both directions
unchanged, and two exchanges that describe the same job agree about it.

Standard library only. Nothing here may depend on runtime, storage, HTTP, MCP,
CLI, Platform, Dev, or a validation framework.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import datetime
from typing import Any, Final

from omnivia_core.contracts.v1 import generated
from omnivia_core.contracts.v1.adapter import ApplicationWireAdapter
from omnivia_core.contracts.v1.codec import (
    SCHEMA_BASE_URI,
    decode_request,
    decode_response,
    encode_request,
    encode_response,
    retry_class_for,
    to_canonical_json,
)
from omnivia_core.contracts.v1.compatibility import (
    ContractSemanticError,
    validate_capability_set,
    validate_version_capability_envelope,
    validate_version_window,
)
from omnivia_core.contracts.v1.generated import (
    ErrorResponseEnvelope,
    OperationMetadata,
    SuccessResponseEnvelope,
)
from omnivia_core.contracts.v1.semantics import (
    validate_governed_record,
    validate_memory_create_input,
    validate_memory_create_result,
    validate_record_provenance,
    validate_record_temporal_metadata,
    validate_workspace_compatibility,
)
from omnivia_core.contracts.v1.semantics_evidence import (
    validate_evidence_artifact,
    validate_evidence_search_input,
    validate_evidence_search_result,
)
from omnivia_core.contracts.v1.semantics_jobs import (
    IDEMPOTENCY_CONFLICT,
    IDEMPOTENCY_DISTINCT,
    IDEMPOTENCY_REPLAY,
    JobEventsPageBinding,
    classify_idempotent_replay,
    idempotency_equivalence,
    validate_import_source_descriptor,
    validate_import_start_input,
    validate_import_start_result,
    validate_job_attempt,
    validate_job_cancel_input,
    validate_job_cancel_result,
    validate_job_control_replay,
    validate_job_events_input,
    validate_job_events_result,
    validate_job_get_input,
    validate_job_get_result,
    validate_job_handle,
    validate_job_observation_progression,
    validate_job_retry_input,
    validate_job_retry_result,
    validate_operation_idempotency_metadata,
    validate_synchronous_job_response_job_reference,
)
from omnivia_core.contracts.v1.semantics_knowledge import (
    validate_candidate_approve_input,
    validate_candidate_approve_result,
    validate_candidate_reject_input,
    validate_candidate_reject_result,
    validate_context_pack_build_input,
    validate_context_pack_build_result,
    validate_graph_traversal_input,
    validate_graph_traversal_result,
    validate_knowledge_propose_input,
    validate_knowledge_propose_result,
    validate_knowledge_search_input,
    validate_knowledge_search_result,
    validate_projection_freshness,
    validate_record_supersede_input,
    validate_record_supersede_result,
)
from omnivia_core.contracts.v1.semantics_operations import (
    get_operation_metadata,
    validate_operation_error,
    validate_operation_request_metadata,
)

__all__ = [
    "ADAPTER_CONFORMANCE_CORPUS_FILE",
    "ADAPTER_CONFORMANCE_CORPUS_FORMAT",
    "AdapterConformanceCase",
    "AdapterConformanceError",
    "load_adapter_conformance_corpus",
    "run_adapter_conformance",
    "validate_case_collection",
]

ADAPTER_CONFORMANCE_CORPUS_FILE: Final = "application-wire-adapter-conformance-v1.json"
ADAPTER_CONFORMANCE_CORPUS_FORMAT: Final = "omnivia.adapter-conformance.v1"

SUCCESS_BRANCH: Final = "success"
ERROR_BRANCH: Final = "error"
_BRANCHES: Final = frozenset({SUCCESS_BRANCH, ERROR_BRANCH})
#: The frozen catalogue operation names, for guarding caller-supplied cases.
_CATALOGUE_NAMES: Final[frozenset[str]] = frozenset(
    entry.name for entry in generated.OPERATION_CATALOGUE
)
_IDEMPOTENCY_CLASSIFICATIONS: Final = frozenset(
    {IDEMPOTENCY_REPLAY, IDEMPOTENCY_CONFLICT, IDEMPOTENCY_DISTINCT}
)

#: Operation -> the frozen semantic rule for its *input* payload.
#:
#: Every entry takes the decoded payload alone, so this table needs no trusted
#: context and applies to every case of that operation unconditionally. An
#: operation absent from the table has no input semantics beyond its declared
#: shape, which the payload decode already enforces.
_INPUT_SEMANTICS: Final[dict[str, Callable[[Any], None]]] = {
    "candidate.approve": validate_candidate_approve_input,
    "candidate.reject": validate_candidate_reject_input,
    "context_pack.build": validate_context_pack_build_input,
    "evidence.search": validate_evidence_search_input,
    "graph.traverse": validate_graph_traversal_input,
    "import.start": validate_import_start_input,
    "job.cancel": validate_job_cancel_input,
    "job.events": validate_job_events_input,
    "job.get": validate_job_get_input,
    "job.retry": validate_job_retry_input,
    "knowledge.propose": validate_knowledge_propose_input,
    "knowledge.search": validate_knowledge_search_input,
    "memory.create": validate_memory_create_input,
    "record.supersede": validate_record_supersede_input,
}


#: Operation -> the frozen semantic rule for its *success result*, for the
#: operations whose trusted context the exchange itself supplies.
#:
#: The remaining result validators need context no exchange carries -- the prior
#: job handle, the accepted import source, the reviewer who approved, the
#: authorized view set. Those are deliberately absent rather than stubbed: passing
#: a value invented here would make the check assert agreement with this module
#: instead of with the server that produced the response.
_RESULT_SEMANTICS: Final[dict[str, Callable[[_ResultContext], None]]] = {}


#: Generated type -> the frozen rule for that type, wherever it appears.
#:
#: These are the validators that belong to a *type* rather than to an operation:
#: a `GovernedRecord` is subject to the same governance invariants whether it
#: arrives from `memory.get`, inside a `memory.list` page, or nested in a
#: Context Pack. Dispatching on the type reaches all of those; dispatching on the
#: operation reached only the ones somebody remembered to wire, which is how
#: `memory.get` came to accept a proposed record claiming canonical authority and
#: `workspace.list` came to accept a format version outside its supported range.
#:
#: Every entry takes the decoded value alone. Relational rules needing trusted
#: context stay in :data:`_RESULT_SEMANTICS`, keyed by operation.
_NESTED_SEMANTICS: Final[dict[type, Callable[[Any], object]]] = {
    generated.CapabilitySet: validate_capability_set,
    generated.EvidenceArtifact: validate_evidence_artifact,
    generated.GovernedRecord: validate_governed_record,
    generated.ImportSourceDescriptor: validate_import_source_descriptor,
    generated.JobAttempt: validate_job_attempt,
    generated.JobHandle: validate_job_handle,
    # Wired but inert, and deliberately so. `OperationIdempotencyMetadata` is a
    # field of `OperationMetadata` -- a *catalogue* entry -- so it appears in no
    # request input, no result, and no response envelope, and this rule never
    # fires. It stays because the completeness rule is "every frozen
    # single-argument type validator is wired", and an exception maintained by
    # hand is how the `WorkspaceCompatibility` gap happened. If a later contract
    # ever puts this type in a payload, the rule is already there.
    # `test_every_wired_type_rule_is_reachable_or_declared_inert` pins the fact.
    generated.OperationIdempotencyMetadata: validate_operation_idempotency_metadata,
    generated.ProjectionFreshness: validate_projection_freshness,
    generated.RecordProvenance: validate_record_provenance,
    generated.RecordTemporalMetadata: validate_record_temporal_metadata,
    generated.VersionCapabilityEnvelope: validate_version_capability_envelope,
    generated.VersionWindow: validate_version_window,
    generated.WorkspaceCompatibility: validate_workspace_compatibility,
}


def _check_nested_semantics(case: AdapterConformanceCase, decoded: object, label: str) -> None:
    """Apply every frozen type rule to every value of that type in `decoded`.

    Walks the decoded dataclass graph rather than the raw document, for the same
    reason the tenancy rule does: a type is what the contract declared, not what
    a key happens to be called.
    """
    def walk(node: object, path: str) -> None:
        if is_dataclass(node) and not isinstance(node, type):
            rule = _NESTED_SEMANTICS.get(type(node))
            if rule is not None:
                try:
                    rule(node)
                except ContractSemanticError as error:
                    raise AdapterConformanceError(
                        case.id,
                        f"{label}{path} is not a valid {type(node).__name__}: {error}",
                    ) from error
            for declared in fields(node):
                walk(getattr(node, declared.name), f"{path}.{declared.name}")
        elif isinstance(node, (list, tuple)):
            for index, item in enumerate(node):
                walk(item, f"{path}[{index}]")

    walk(decoded, "")


@dataclass
class _JobRecord:
    """What the collection has established about one job so far.

    More than the last handle, because a job carries facts that are fixed at its
    origin and can never change afterwards. The source an `import.start` accepted
    is one: the descriptor is immutable, and every later terminal result for that
    job reports the *same* import. Keeping only handles meant a `job.get`
    completion could report a different source and be judged against a trusted
    fact the same case supplied -- a second, mutable statement of something the
    transcript already establishes.
    """

    observed_by: str
    handle: Any
    #: The `ImportSourceDescriptor` an observed `import.start` accepted, and the
    #: case that established it. `None` when no originating exchange is present.
    accepted_import_source: Any = None
    accepted_by: str | None = None


@dataclass
class _RunState:
    """What earlier exchanges in this run established, carried forward.

    A conformance run is a transcript, not a bag of independent exchanges. Facts
    an earlier exchange fixed -- the job it started and the source it accepted,
    the binding it attached to a continuation token it issued -- are the
    authoritative statement of those facts for every later exchange that refers
    to them. Without somewhere to keep them, each case ends up supplying its own
    copy in ``trusted``, and a check that compares a document against a value the
    same case wrote is a check the document cannot fail.
    """

    jobs: dict[str, _JobRecord] = field(default_factory=dict)
    #: (operation, principal, workspace, token) -> the binding attached at issue.
    token_bindings: dict[tuple[str, str, str | None, str], Any] = field(default_factory=dict)


def _collect_job_handles(decoded: object) -> list[Any]:
    """Every `JobHandle` in a decoded graph, in walk order."""
    found: list[Any] = []

    def walk(node: object) -> None:
        if is_dataclass(node) and not isinstance(node, type):
            if isinstance(node, generated.JobHandle):
                found.append(node)
            for declared in fields(node):
                walk(getattr(node, declared.name))
        elif isinstance(node, (list, tuple)):
            for item in node:
                walk(item)

    walk(decoded)
    return found


def _check_job_observations(
    case: AdapterConformanceCase,
    decoded: object,
    state: _RunState,
    restates_an_earlier_outcome: bool,
) -> None:
    """Hold two exchanges that describe the same job to the frozen progression rule.

    A job outlives the call that created it: `import.start` returns a handle,
    `job.get` reads it again, `job.events` pages its history, `job.cancel` and
    `job.retry` act on it. Each of those was checked against the *one* exchange it
    appeared in, and `validate_job_observation_progression` was reachable only
    through a trusted `previous_job_handle` a case declared for itself -- so two
    recorded exchanges naming the same job were never compared, and one could say
    it was created on the 30th while the other said the 29th.

    ``recovery_accepted`` is set for `job.retry` because that is what the
    operation *is*: accepting a recovery is the one thing that lets a job's state
    legally move back to `queued`, and the frozen rule takes the flag for exactly
    this reason.

    An honest replay is skipped, and has to be -- but not for the reason first
    given here. The original justification was that a replay answers byte-for-byte
    with the recorded outcome, so it observes nothing new. That is false for the
    job-control operations: `validate_job_control_replay` deliberately allows a
    replay to carry an honest *later* reading of the job.

    The real reason is that a replay's handle has the wrong baseline for this
    chain. Whether it repeats the recorded instant or reports a later one, it is
    a reading relative to the call it replays, not to whatever exchange happened
    most recently -- so comparing it against the running head of the chain can
    make a correct replay look like a job going backwards. Its progression is
    already checked against the right baseline, the original, by the frozen
    replay rule.
    """
    if restates_an_earlier_outcome:
        return
    for handle in _collect_job_handles(decoded):
        job_id = getattr(getattr(handle, "identity", None), "job_id", None)
        if not isinstance(job_id, str) or not job_id:
            continue
        seen = state.jobs.get(job_id)
        if seen is not None:
            try:
                validate_job_observation_progression(
                    seen.handle,
                    handle,
                    recovery_accepted=case.operation == "job.retry",
                    label=f"job {job_id!r}",
                )
            except ContractSemanticError as error:
                raise AdapterConformanceError(
                    case.id,
                    f"contradicts {seen.observed_by!r}, which describes the same job: {error}",
                ) from error
            seen.observed_by, seen.handle = case.id, handle
        else:
            state.jobs[job_id] = _JobRecord(observed_by=case.id, handle=handle)


def _record_job_origin_facts(
    case: AdapterConformanceCase,
    decoded_input: Any,
    decoded_result: Any,
    state: _RunState,
) -> None:
    """Retain the immutable facts an originating exchange fixes about its job.

    Only `import.start` fixes one today: the `ImportSourceDescriptor` it accepted.
    That descriptor is what every later terminal result for the job must report,
    and it is established by *this* exchange -- so it is read from the request
    input the operation accepted rather than from anything a later case declares
    about it.
    """
    if case.operation != "import.start":
        return
    source = getattr(decoded_input, "source", None)
    handle = getattr(decoded_result, "job", None)
    job_id = getattr(getattr(handle, "identity", None), "job_id", None)
    if source is None or not isinstance(job_id, str) or not job_id:
        return
    record = state.jobs.get(job_id)
    if record is None:
        record = _JobRecord(observed_by=case.id, handle=handle)
        state.jobs[job_id] = record
    if record.accepted_import_source is None:
        record.accepted_import_source, record.accepted_by = source, case.id
    elif record.accepted_import_source != source:
        # A job's accepted source is fixed when the job is started, so two
        # exchanges starting the *same* job from different sources contradict
        # each other. Keeping the first quietly would let collection order decide
        # which import a later completion is judged against.
        raise AdapterConformanceError(
            case.id,
            f"starts job {job_id!r} from a different source than {record.accepted_by!r} "
            "accepted for the same job; the descriptor import.start accepted is immutable",
        )


def _token_scope(case: AdapterConformanceCase, token: str) -> tuple[str, str, str | None, str]:
    """A continuation token is only the same token within its own scope."""
    return (case.operation, case.principal_id, case.workspace_id, token)


def _record_issued_token_binding(case: AdapterConformanceCase, state: _RunState) -> None:
    """Retain what an exchange bound to the continuation token it issued.

    The binding is the adapter's statement of what the opaque token was found to
    carry -- principal, workspace, operation, job, ordering, snapshot. Kept here,
    it becomes the authoritative answer for whoever presents that token next,
    instead of each page declaring its own copy and the pair agreeing because one
    author wrote both.
    """
    token = _issued_continuation_token(case)
    if token is None:
        return
    binding = _page_binding(case, "result_page_binding")
    if binding is None:
        return
    scope = _token_scope(case, token)
    retained = state.token_bindings.get(scope)
    if retained is None:
        state.token_bindings[scope] = binding
    elif retained != binding:
        # The same hazard as a binding that drifts between issue and use, seen
        # from the other side: one opaque token cannot stand for two different
        # cursors. Keeping the first quietly would make which exchange the
        # transcript happens to list first decide what the token means.
        raise AdapterConformanceError(
            case.id,
            f"issues continuation token {token!r} bound differently from the binding an "
            "earlier exchange attached to that same token",
        )


class AdapterConformanceError(ValueError):
    """One recorded exchange an adapter failed to honour.

    Carries the case id because a corpus failure is only actionable if you know
    which exchange produced it: "the response did not match" is a bug report, and
    "``memory.list/page-2`` returned a different continuation token" is a fix.
    """

    def __init__(self, case_id: str, reason: str) -> None:
        self.case_id = case_id
        self.reason = reason
        super().__init__(f"conformance case {case_id!r}: {reason}")


@dataclass(frozen=True)
class AdapterConformanceCase:
    """One recorded request/response exchange, with what it is meant to prove.

    ``request`` and ``response`` are fully materialized wire mappings: the corpus
    factors the parts every case shares into a ``defaults`` block for
    readability, and the loader merges them before anything here sees a case, so
    a case is always a complete exchange rather than a fragment plus a lookup.
    """

    id: str
    operation: str
    description: str
    request: Mapping[str, Any]
    response: Mapping[str, Any]
    branch: str
    principal_id: str
    workspace_id: str | None = None
    error_code: str | None = None
    retry_class: str | None = None
    #: The case this one repeats. Set together with :attr:`idempotency`.
    replay_of: str | None = None
    #: The frozen A2.4 classification this pair must produce.
    idempotency: str | None = None
    #: The case whose continuation token this request must carry.
    page_of: str | None = None
    #: Facts a *server* knows that no wire document carries -- the binding behind
    #: an opaque token, the prior job handle, the reviewer who approved. A frozen
    #: relational validator needs these, and inventing them here would make the
    #: check assert agreement with this module rather than with the exchange, so
    #: the corpus states them explicitly instead.
    trusted: Mapping[str, Any] = field(default_factory=dict)

    @property
    def request_id(self) -> str:
        metadata = self.request.get("metadata")
        request_id = metadata.get("request_id") if isinstance(metadata, Mapping) else None
        return request_id if isinstance(request_id, str) else ""

    @property
    def operation_input(self) -> Mapping[str, Any]:
        value = self.request.get("input")
        return value if isinstance(value, Mapping) else {}


class _CircularValue(Exception):
    """A document that contains itself, caught before the stack runs out."""


def _canonical(case_id: str, value: object, label: str) -> str:
    """Canonicalize caller- or adapter-supplied data, or fail as a conformance error.

    ``to_canonical_json`` raises for anything JSON cannot carry -- a
    ``mappingproxy`` (which is exactly what this codec decodes open objects to),
    a ``NaN``, a circular reference, a ``set``. Those arrive from a caller's own
    case or from the adapter under test, so they are conformance failures about a
    named exchange, not crashes of the harness.
    """
    try:
        return to_canonical_json(_plain(value))
    except _CircularValue as error:
        raise AdapterConformanceError(
            case_id, f"{label} contains a circular reference and has no canonical form"
        ) from error
    except (TypeError, ValueError) as error:
        raise AdapterConformanceError(
            case_id, f"{label} cannot be canonicalized: {type(error).__name__}: {error}"
        ) from error


def _plain(value: object) -> Any:
    """Convert a decoded value to plain JSON types for canonical comparison.

    An open ``JsonObject`` field decodes to a read-only ``mappingproxy``, which
    the canonicalizer cannot serialize. Normalizing both sides here keeps the
    comparison about the *values* rather than about which container type each
    side happens to arrive in.
    """
    return _plain_guarded(value, set())


def _plain_guarded(value: object, active: set[int]) -> Any:
    """Normalize one value, refusing a structure that contains itself.

    A self-referential document has no canonical form, and recursing into one
    exhausts the stack before any check can report which exchange carried it.
    Tracking the containers currently being walked -- rather than every container
    seen -- lets a document legitimately repeat a shared sub-object while still
    catching a genuine cycle.
    """
    if isinstance(value, (Mapping, list, tuple)):
        marker = id(value)
        if marker in active:
            raise _CircularValue
        active = active | {marker}
    if isinstance(value, Mapping):
        return {key: _plain_guarded(item, active) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_guarded(item, active) for item in value]
    return value


def _require_mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise ContractSemanticError(f"{label}: expected an object, got {type(value).__name__}")
    return value


def _require_str(value: object, label: str) -> str:
    if isinstance(value, bool) or not isinstance(value, str):
        raise ContractSemanticError(f"{label}: expected a string, got {type(value).__name__}")
    return value


def _optional_str(value: object, label: str) -> str | None:
    return None if value is None else _require_str(value, label)


def _merged_metadata(
    defaults: Mapping[str, Any], envelope: Mapping[str, Any], label: str
) -> dict[str, Any]:
    """Overlay a case's own metadata on the corpus-wide defaults.

    The merge is one level deep and the case always wins. Anything deeper would
    make a case's stated value depend on how a default was structured, and the
    point of the defaults block is to remove noise, not to hide meaning.
    """
    merged = dict(defaults)
    merged.update(_require_mapping(envelope.get("metadata", {}), f"{label}.metadata"))
    return merged


def _case_from_document(
    raw: Mapping[str, Any], defaults: Mapping[str, Any], index: int
) -> AdapterConformanceCase:
    label = f"cases[{index}]"
    case_id = _require_str(raw.get("id"), f"{label}.id")
    request = _require_mapping(raw.get("request"), f"{case_id}.request")
    response = _require_mapping(raw.get("response"), f"{case_id}.response")
    expect = _require_mapping(raw.get("expect", {}), f"{case_id}.expect")

    branch = _require_str(expect.get("branch"), f"{case_id}.expect.branch")
    if branch not in _BRANCHES:
        raise ContractSemanticError(
            f"{case_id}.expect.branch: must be one of {sorted(_BRANCHES)}, got {branch!r}"
        )
    idempotency = _optional_str(expect.get("idempotency"), f"{case_id}.expect.idempotency")
    if idempotency is not None and idempotency not in _IDEMPOTENCY_CLASSIFICATIONS:
        raise ContractSemanticError(
            f"{case_id}.expect.idempotency: must be one of "
            f"{sorted(_IDEMPOTENCY_CLASSIFICATIONS)}, got {idempotency!r}"
        )

    request_metadata = _merged_metadata(
        _require_mapping(defaults.get("request_metadata", {}), "defaults.request_metadata"),
        request,
        f"{case_id}.request",
    )
    response_metadata = _merged_metadata(
        _require_mapping(defaults.get("response_metadata", {}), "defaults.response_metadata"),
        response,
        f"{case_id}.response",
    )
    materialized_request = {**request, "metadata": request_metadata}
    materialized_response = {**response, "metadata": response_metadata}

    workspace_id = request_metadata.get("workspace_id")
    return AdapterConformanceCase(
        id=case_id,
        operation=_require_str(raw.get("operation"), f"{case_id}.operation"),
        description=_require_str(raw.get("description"), f"{case_id}.description"),
        request=materialized_request,
        response=materialized_response,
        branch=branch,
        principal_id=_require_str(
            raw.get("principal_id", defaults.get("principal_id")), f"{case_id}.principal_id"
        ),
        workspace_id=_optional_str(workspace_id, f"{case_id}.request.metadata.workspace_id"),
        error_code=_optional_str(expect.get("error_code"), f"{case_id}.expect.error_code"),
        retry_class=_optional_str(expect.get("retry_class"), f"{case_id}.expect.retry_class"),
        replay_of=_optional_str(expect.get("replay_of"), f"{case_id}.expect.replay_of"),
        idempotency=idempotency,
        page_of=_optional_str(expect.get("page_of"), f"{case_id}.expect.page_of"),
        trusted=_require_mapping(raw.get("trusted", {}), f"{case_id}.trusted"),
    )


def load_adapter_conformance_corpus() -> tuple[AdapterConformanceCase, ...]:
    """Load and structurally validate the packaged conformance corpus.

    Read through the packaged-resource accessor rather than a constructed path,
    so an installed wheel and a source checkout load the same bytes.
    """
    from omnivia_core.contracts.v1 import resources

    document = _require_mapping(
        resources.read_fixture(ADAPTER_CONFORMANCE_CORPUS_FILE), "corpus"
    )
    corpus_format = document.get("format")
    if corpus_format != ADAPTER_CONFORMANCE_CORPUS_FORMAT:
        raise ContractSemanticError(
            f"corpus.format: must be {ADAPTER_CONFORMANCE_CORPUS_FORMAT!r}, got {corpus_format!r}"
        )
    raw_cases = document.get("cases")
    if not isinstance(raw_cases, Sequence) or isinstance(raw_cases, (str, bytes)):
        raise ContractSemanticError("corpus.cases: must be an array")
    defaults = _require_mapping(document.get("defaults", {}), "corpus.defaults")

    cases = tuple(
        _case_from_document(_require_mapping(raw, f"cases[{index}]"), defaults, index)
        for index, raw in enumerate(raw_cases)
    )
    validate_case_collection(cases)
    return cases



# --------------------------------------------------------------------------
# Canonical wire-language validation
# --------------------------------------------------------------------------

#: The JSON Schema keywords the Application Contract v1 documents actually use.
#:
#: Deliberately a closed list rather than an open framework. If a canonical
#: schema ever starts using a keyword absent from here, validation must fail
#: loudly rather than ignore it -- silently skipping an unknown keyword is how a
#: validator ends up certifying documents it never really checked.
_SUPPORTED_SCHEMA_KEYWORDS: Final[frozenset[str]] = frozenset({
    "$schema", "$id", "$ref", "$defs", "title", "description",
    "type", "properties", "required", "additionalProperties",
    "unevaluatedProperties", "propertyNames", "minProperties",
    "items", "minItems", "maxItems", "uniqueItems",
    "pattern", "minLength", "maxLength", "format",
    "minimum", "maximum", "oneOf", "const", "enum",
})

_JSON_TYPES: Final[dict[str, tuple[type, ...]]] = {
    "object": (Mapping,),
    "array": (list, tuple),
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "null": (type(None),),
}


class _CanonicalSchemas:
    """The packaged canonical schema documents, resolved by reference.

    Read through :mod:`~omnivia_core.contracts.v1.resources` rather than a
    constructed path, so an installed wheel and a source checkout validate
    against the same bytes. Documents are cached per instance: a single payload
    check resolves dozens of references, and re-reading a packaged resource for
    each one would make validation cost meaningless.
    """

    def __init__(self) -> None:
        self._documents: dict[str, Mapping[str, Any]] = {}

    def document(self, name: str) -> Mapping[str, Any]:
        cached = self._documents.get(name)
        if cached is None:
            from omnivia_core.contracts.v1 import resources

            cached = _require_mapping(resources.read_schema(name), f"{name}.schema.json")
            self._documents[name] = cached
        return cached

    def resolve(self, ref: str) -> Mapping[str, Any]:
        """Resolve an absolute in-contract ``...#/$defs/<Name>`` reference."""
        if not ref.startswith(SCHEMA_BASE_URI) or "#/$defs/" not in ref:
            raise ContractSemanticError(f"unsupported schema reference {ref!r}")
        document_uri, _, pointer = ref.partition("#/$defs/")
        name = document_uri[len(SCHEMA_BASE_URI) :].removesuffix(".schema.json")
        defs = self.document(name).get("$defs")
        if not isinstance(defs, Mapping) or pointer not in defs:
            raise ContractSemanticError(f"schema reference {ref!r} resolves to nothing")
        return _require_mapping(defs[pointer], ref)


def _validate_against_schema(
    value: object, schema: Mapping[str, Any], schemas: _CanonicalSchemas, path: str
) -> list[str]:
    """Validate one value against one canonical schema node.

    A bounded Draft 2020-12 subset -- exactly the keywords the contract uses --
    implemented over the packaged schemas with the standard library alone. This
    exists because the generated dataclasses decode *tolerantly*: `from_wire`
    proves a document is structurally representable, not that it speaks the
    canonical wire language. An empty `RecordId` and a `GovernedRecordType` of
    ``"INVALID VALUE"`` are both strings a dataclass will happily carry.

    ``pattern`` uses :func:`re.search`, which is JSON Schema's own semantics, so
    this agrees exactly with the strict ``jsonschema`` validation the conformance
    gate applies to the checked-in schemas.

    It also now agrees with the semantic layer's :func:`re.fullmatch`. That was
    the open terminal-anchor question left to A2.7: a bare ``$`` matches before a
    trailing newline under Python search semantics, so search accepted values
    fullmatch refused, and this runtime read a wider language than the library
    around it. Every canonical pattern now ends ``$(?![\\s\\S])``, so search and
    fullmatch describe the same language and neither has to be tightened here.
    ``tests/contracts/test_scalar_anchor_parity.py`` holds that agreement.
    """
    findings: list[str] = []

    unknown = sorted(set(schema) - _SUPPORTED_SCHEMA_KEYWORDS)
    unknown = [key for key in unknown if not key.startswith("x-")]
    if unknown:
        return [f"{path}: canonical schema uses unsupported keyword(s) {unknown}"]

    if "$ref" in schema:
        return _validate_against_schema(value, schemas.resolve(schema["$ref"]), schemas, path)

    declared = schema.get("type")
    if isinstance(declared, str):
        expected = _JSON_TYPES.get(declared)
        if expected is None:
            return [f"{path}: canonical schema declares unknown type {declared!r}"]
        # bool is an int in Python; JSON keeps them apart and so must this.
        if declared in {"integer", "number"} and isinstance(value, bool):
            return [f"{path}: expected {declared}, got boolean"]
        if not isinstance(value, expected):
            return [f"{path}: expected {declared}, got {type(value).__name__}"]

    if "const" in schema and value != schema["const"]:
        findings.append(f"{path}: must be {schema['const']!r}, got {value!r}")
    if "enum" in schema and value not in schema["enum"]:
        findings.append(f"{path}: must be one of {schema['enum']!r}, got {value!r}")

    if isinstance(value, str):
        declared_format = schema.get("format")
        if isinstance(declared_format, str):
            findings.extend(_validate_format(value, declared_format, path))
        pattern = schema.get("pattern")
        if isinstance(pattern, str) and re.search(pattern, value) is None:
            findings.append(f"{path}: {value!r} does not match {pattern}")
        minimum_length = schema.get("minLength")
        if isinstance(minimum_length, int) and len(value) < minimum_length:
            findings.append(f"{path}: shorter than minLength {minimum_length}")
        maximum_length = schema.get("maxLength")
        if isinstance(maximum_length, int) and len(value) > maximum_length:
            findings.append(f"{path}: longer than maxLength {maximum_length}")

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        minimum = schema.get("minimum")
        if isinstance(minimum, (int, float)) and value < minimum:
            findings.append(f"{path}: below minimum {minimum}")
        maximum = schema.get("maximum")
        if isinstance(maximum, (int, float)) and value > maximum:
            findings.append(f"{path}: above maximum {maximum}")

    if isinstance(value, (list, tuple)):
        findings.extend(_validate_array(value, schema, schemas, path))
    elif isinstance(value, Mapping):
        findings.extend(_validate_object(value, schema, schemas, path))

    if "oneOf" in schema:
        branches = schema["oneOf"]
        matched = [
            branch
            for branch in branches
            if not _validate_against_schema(value, branch, schemas, path)
        ]
        if len(matched) != 1:
            findings.append(
                f"{path}: must match exactly one of {len(branches)} alternatives, matched "
                f"{len(matched)}"
            )
    return findings


def _validate_format(value: str, declared: str, path: str) -> list[str]:
    """Assert the formats this contract actually declares.

    A pattern is not a calendar. ``2024-99-99T99:99:99Z`` satisfies the
    ``Timestamp`` pattern character for character and is not a date, so a
    validator that stops at the pattern accepts an instant that does not exist.
    An unknown format is refused outright rather than skipped: silently ignoring
    a declared constraint is how a validator ends up claiming coverage it does
    not have.
    """
    if declared != "date-time":
        return [f"{path}: canonical schema declares unsupported format {declared!r}"]
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        datetime.fromisoformat(text)
    except ValueError:
        return [f"{path}: {value!r} is not a valid RFC 3339 date-time"]
    return []


def _validate_array(
    value: Sequence[Any], schema: Mapping[str, Any], schemas: _CanonicalSchemas, path: str
) -> list[str]:
    findings: list[str] = []
    minimum_items = schema.get("minItems")
    if isinstance(minimum_items, int) and len(value) < minimum_items:
        findings.append(f"{path}: fewer than minItems {minimum_items}")
    maximum_items = schema.get("maxItems")
    if isinstance(maximum_items, int) and len(value) > maximum_items:
        findings.append(f"{path}: more than maxItems {maximum_items}")
    if schema.get("uniqueItems") is True:
        seen: list[str] = [to_canonical_json(_plain(item)) for item in value]
        if len(set(seen)) != len(seen):
            findings.append(f"{path}: items must be unique")
    items = schema.get("items")
    if isinstance(items, Mapping):
        for index, item in enumerate(value):
            findings.extend(
                _validate_against_schema(item, items, schemas, f"{path}[{index}]")
            )
    return findings


def _validate_object(
    value: Mapping[str, Any], schema: Mapping[str, Any], schemas: _CanonicalSchemas, path: str
) -> list[str]:
    findings: list[str] = []
    minimum_properties = schema.get("minProperties")
    if isinstance(minimum_properties, int) and len(value) < minimum_properties:
        findings.append(f"{path}: fewer than minProperties {minimum_properties}")

    required = schema.get("required")
    if isinstance(required, list):
        missing = sorted(name for name in required if name not in value)
        if missing:
            findings.append(f"{path}: missing required field(s) {missing}")

    properties = schema.get("properties")
    declared_names: set[str] = set()
    if isinstance(properties, Mapping):
        declared_names = set(properties)
        for name, subschema in properties.items():
            if name in value and isinstance(subschema, Mapping):
                findings.extend(
                    _validate_against_schema(value[name], subschema, schemas, f"{path}.{name}")
                )

    property_names = schema.get("propertyNames")
    if isinstance(property_names, Mapping):
        for name in value:
            findings.extend(
                _validate_against_schema(name, property_names, schemas, f"{path}<key {name!r}>")
            )

    # `unevaluatedProperties: false` is how this contract closes its objects.
    closed = schema.get("unevaluatedProperties")
    if closed is False:
        extra = sorted(set(value) - declared_names)
        if extra:
            findings.append(f"{path}: undeclared field(s) {extra}")
    additional = schema.get("additionalProperties")
    if additional is False:
        extra = sorted(set(value) - declared_names)
        if extra:
            findings.append(f"{path}: undeclared field(s) {extra}")
    elif isinstance(additional, Mapping):
        for name in sorted(set(value) - declared_names):
            findings.extend(
                _validate_against_schema(value[name], additional, schemas, f"{path}.{name}")
            )
    return findings


#: The canonical definitions for the envelopes themselves.
#:
#: The response is named by *branch* rather than through the `ResponseEnvelope`
#: union. Validating against the union reports only "matched 0 of 2
#: alternatives", which names neither the field nor the rule -- and a diagnostic
#: that cannot be acted on is most of the way to no check at all. The branch is
#: already known by the time this runs, so the failing field can be named.
_REQUEST_ENVELOPE_REF: Final = f"{SCHEMA_BASE_URI}envelopes.schema.json#/$defs/RequestEnvelope"
_SUCCESS_ENVELOPE_REF: Final = (
    f"{SCHEMA_BASE_URI}envelopes.schema.json#/$defs/SuccessResponseEnvelope"
)
_ERROR_ENVELOPE_REF: Final = f"{SCHEMA_BASE_URI}envelopes.schema.json#/$defs/ErrorResponseEnvelope"


def _check_envelope_against_the_canonical_schema(
    case: AdapterConformanceCase,
    document: Mapping[str, Any],
    schema_ref: str,
    label: str,
    schemas: _CanonicalSchemas,
) -> None:
    """Hold an envelope to the contract's own statement of the wire language.

    The payload check does this for `input` and `result`, and nothing did it for
    the envelope around them: an envelope was only decoded and round-tripped, and
    a decode proves a document is *representable*, not that it speaks the
    canonical language. Every scalar pattern and every bound the envelope schema
    declares -- `maxItems` on a role list, a `minLength` on an identifier -- went
    unchecked, so a response could attest 65 roles against a declared maximum of
    64 and conform.

    Run after the decode/encode round trip so that a field the contract cannot
    represent is still reported as the round-trip failure it is; this check is
    for what survives the trip and is still not legal.
    """
    try:
        findings = _validate_against_schema(
            _plain(document), schemas.resolve(schema_ref), schemas, label
        )
    except ContractSemanticError as error:
        raise AdapterConformanceError(
            case.id, f"{label}: cannot resolve {schema_ref!r}: {error}"
        ) from error
    if findings:
        raise AdapterConformanceError(
            case.id,
            f"{label} does not satisfy its canonical schema: " + "; ".join(findings[:3]),
        )


def _payload_type(schema_ref: str, case_id: str, label: str) -> type[Any]:
    """Resolve a catalogue schema reference to the generated type it names.

    The catalogue binds each operation to an absolute
    ``...#/$defs/<Definition>`` reference, and the generator emits a dataclass of
    exactly that name. Resolving through the reference rather than a table here
    is what makes the binding *the catalogue's*: an operation repointed at a
    different definition changes what this validates, with no second list to keep
    in step.
    """
    definition = schema_ref.rsplit("/", 1)[-1]
    payload_type = getattr(generated, definition, None)
    if (
        payload_type is None
        or not isinstance(payload_type, type)
        or not is_dataclass(payload_type)
        or not hasattr(payload_type, "from_wire")
    ):
        raise AdapterConformanceError(
            case_id, f"{label} schema reference {schema_ref!r} names no generated type"
        )
    return payload_type


def _check_payload(
    case: AdapterConformanceCase,
    schema_ref: str,
    payload: object,
    label: str,
    schemas: _CanonicalSchemas,
) -> Any:
    """Decode `payload` as the exact type the catalogue binds, and prove it round-trips.

    A conformance harness that checked only the envelope would certify an adapter
    that answered `memory.get` with a `workspace.list` result, or with an object
    of no declared shape at all -- the envelope is identical in every case. The
    decode is what binds an exchange to *this* operation's payload contract.

    The round trip is checked as well as the decode: tolerant decoding drops
    fields the build does not know, so a payload carrying something the contract
    cannot represent decodes cleanly and silently loses it.
    """
    # The canonical schema first: it is the contract's own statement of the wire
    # language, and it is the only one of the two checks that enforces scalar
    # patterns and bounds.
    try:
        schema_findings = _validate_against_schema(
            _plain(payload), schemas.resolve(schema_ref), schemas, label
        )
    except ContractSemanticError as error:
        raise AdapterConformanceError(
            case.id, f"{label}: cannot resolve {schema_ref!r}: {error}"
        ) from error
    if schema_findings:
        raise AdapterConformanceError(
            case.id,
            f"{label} does not satisfy its canonical schema: " + "; ".join(schema_findings[:3]),
        )

    payload_type = _payload_type(schema_ref, case.id, label)
    try:
        decoded = payload_type.from_wire(payload)
    except Exception as error:
        raise AdapterConformanceError(
            case.id, f"{label} is not a valid {payload_type.__name__}: {error}"
        ) from error
    if _canonical(case.id, decoded.to_wire(), label) != _canonical(case.id, payload, label):
        raise AdapterConformanceError(
            case.id,
            f"{label} does not survive a {payload_type.__name__} round trip unchanged "
            "(it carries something the contract cannot represent)",
        )
    return decoded


def _check_payload_semantics(
    case: AdapterConformanceCase, operation: OperationMetadata, decoded_input: object
) -> None:
    """Run the frozen per-operation input semantics where the contract defines them.

    Only the *input* validators are universal: each takes the payload alone and
    needs no trusted context, so every operation that has one can be held to it
    here. Result semantics are relational -- they need the prior record, the
    reviewer, the accepted source -- and are checked in
    :func:`_check_declared_result_semantics` against context the case states.
    """
    validator = _INPUT_SEMANTICS.get(operation.name)
    if validator is None:
        return
    try:
        validator(decoded_input)
    except ContractSemanticError as error:
        raise AdapterConformanceError(
            case.id, f"request input fails {operation.name!r} semantics: {error}"
        ) from error


def _require_case(value: object, index: int) -> AdapterConformanceCase:
    """Every element of a public case collection must actually be a case.

    ``AdapterConformanceCase`` is a frozen dataclass, not a validated type: a
    caller can build one with fields of any runtime type, or pass something else
    entirely. Reaching for ``.id`` on whatever arrives is how a public API leaks
    ``AttributeError`` instead of naming the problem.
    """
    if not isinstance(value, AdapterConformanceCase):
        raise AdapterConformanceError(
            f"<cases[{index}]>",
            f"expected an AdapterConformanceCase, got {type(value).__name__}",
        )
    return value


def _check_case_is_well_formed(case: AdapterConformanceCase) -> None:
    """Type-guard and cross-check one case's own declarations.

    The packaged loader builds cases from JSON it has already validated. A
    caller-supplied case has been through none of that, so the same rules are
    applied here -- otherwise ``run_adapter_conformance(adapter, cases=...)``
    would hold custom collections to a weaker standard than the corpus they
    stand in for.
    """
    for name, value in (
        ("id", case.id),
        ("operation", case.operation),
        ("description", case.description),
        ("branch", case.branch),
        ("principal_id", case.principal_id),
    ):
        if not isinstance(value, str) or not value.strip():
            raise AdapterConformanceError(
                case.id if isinstance(case.id, str) and case.id.strip() else "<unnamed>",
                f"{name} must be a non-blank string, got {value!r}",
            )
    for name, optional in (
        ("workspace_id", case.workspace_id),
        ("error_code", case.error_code),
        ("retry_class", case.retry_class),
        ("replay_of", case.replay_of),
        ("idempotency", case.idempotency),
        ("page_of", case.page_of),
    ):
        if optional is not None and (not isinstance(optional, str) or not optional.strip()):
            raise AdapterConformanceError(
                case.id, f"{name} must be a non-blank string or absent, got {optional!r}"
            )
    for name, envelope in (("request", case.request), ("response", case.response)):
        if not isinstance(envelope, Mapping) or not all(
            isinstance(key, str) for key in envelope
        ):
            raise AdapterConformanceError(
                case.id, f"{name} must be an object, got {type(envelope).__name__}"
            )
    if not isinstance(case.trusted, Mapping) or not all(
        isinstance(key, str) for key in case.trusted
    ):
        raise AdapterConformanceError(
            case.id, f"trusted must be an object, got {type(case.trusted).__name__}"
        )

    if case.branch not in _BRANCHES:
        raise AdapterConformanceError(
            case.id, f"branch must be one of {sorted(_BRANCHES)}, got {case.branch!r}"
        )
    if case.idempotency is not None and case.idempotency not in _IDEMPOTENCY_CLASSIFICATIONS:
        raise AdapterConformanceError(
            case.id,
            f"idempotency must be one of {sorted(_IDEMPOTENCY_CLASSIFICATIONS)}, "
            f"got {case.idempotency!r}",
        )
    if (case.replay_of is None) != (case.idempotency is None):
        raise AdapterConformanceError(
            case.id,
            "replay_of and idempotency state one fact together and must both be present "
            "or both absent",
        )

    # Branch and expectation must agree before an adapter is asked anything.
    if case.branch == SUCCESS_BRANCH:
        if case.error_code is not None or case.retry_class is not None:
            raise AdapterConformanceError(
                case.id, "a success case must not declare an error code or retry class"
            )
        if "result" not in case.response:
            raise AdapterConformanceError(case.id, "a success case must record a result")
    else:
        if case.error_code is None:
            raise AdapterConformanceError(
                case.id, "an error case must declare the exact error code it expects"
            )
        if "error" not in case.response:
            raise AdapterConformanceError(case.id, "an error case must record an error")
        recorded = case.response.get("error")
        recorded_code = recorded.get("code") if isinstance(recorded, Mapping) else None
        if recorded_code != case.error_code:
            raise AdapterConformanceError(
                case.id,
                f"declares error code {case.error_code!r} but records {recorded_code!r}",
            )

    # A case's declared workspace is used to judge its result, so it may not
    # disagree with the request it claims to describe. Left uncorrelated, a case
    # could declare one tenant, send another, and have its own tenancy check
    # measured against the declaration it invented.
    request_metadata = case.request.get("metadata")
    stated = (
        request_metadata.get("workspace_id")
        if isinstance(request_metadata, Mapping)
        else None
    )
    if case.workspace_id != stated:
        raise AdapterConformanceError(
            case.id,
            f"declares workspace {case.workspace_id!r} but its request selects {stated!r}",
        )

    if case.operation not in _CATALOGUE_NAMES:
        raise AdapterConformanceError(
            case.id, f"{case.operation!r} is not a catalogue operation"
        )


def validate_case_collection(
    cases: Sequence[AdapterConformanceCase],
) -> Mapping[str, AdapterConformanceCase]:
    """Prove a case collection is internally coherent, returning its index.

    The packaged corpus and a caller's own cases go through this same function,
    and through the same rules. Without that, ``run_adapter_conformance(adapter,
    cases=...)`` would apply weaker rules than the corpus it exists to replace:
    an index built by comprehension lets a duplicate id silently overwrite its
    twin, so one of the two exchanges is never run and the returned count still
    says it was.

    Everything here runs *before* any exchange executes. A collection that
    cannot be satisfied is a defect in the collection, and reporting it up front
    is more useful than surfacing it midway through a run as a puzzling
    comparison failure -- and far better than letting an adapter be called on the
    strength of a collection that was never coherent.
    """
    try:
        listed = list(cases)
    except TypeError as error:
        raise AdapterConformanceError(
            "<cases>", f"cases must be iterable, got {type(cases).__name__}"
        ) from error
    checked = [_require_case(case, index) for index, case in enumerate(listed)]

    by_id: dict[str, AdapterConformanceCase] = {}
    for case in checked:
        _check_case_is_well_formed(case)
        if case.id in by_id:
            raise AdapterConformanceError(case.id, "duplicate case id")
        by_id[case.id] = case

    seen_requests: dict[str, str] = {}
    for case in checked:
        request_id = case.request_id
        if not request_id.strip():
            raise AdapterConformanceError(
                case.id, "request metadata has no non-blank request_id"
            )
        if request_id in seen_requests:
            raise AdapterConformanceError(
                case.id,
                f"request id {request_id!r} is already used by case "
                f"{seen_requests[request_id]!r}; an adapter keyed by request id could not "
                "tell the two exchanges apart",
            )
        seen_requests[request_id] = case.id

    position = {case.id: index for index, case in enumerate(checked)}
    for case in checked:
        _check_link_integrity(case, by_id)
        for link, target_id in (("replay_of", case.replay_of), ("page_of", case.page_of)):
            if target_id is None:
                continue
            # An exchange cannot replay or continue one that has not happened.
            # Running a second page before the request that issued its token
            # would let the collection "pass" in an order no caller could
            # actually perform.
            if position[target_id] >= position[case.id]:
                raise AdapterConformanceError(
                    case.id,
                    f"{link} names {target_id!r}, which does not come earlier in the "
                    "collection; a link must resolve to an exchange already performed",
                )
    return by_id


def _check_request_is_what_the_catalogue_describes(
    case: AdapterConformanceCase, schemas: _CanonicalSchemas
) -> Any:
    """The recorded request must be a request this contract could actually issue."""
    try:
        envelope = decode_request(case.request)
    except Exception as error:
        raise AdapterConformanceError(case.id, f"request does not decode: {error}") from error

    if envelope.operation != case.operation:
        raise AdapterConformanceError(
            case.id,
            f"request names operation {envelope.operation!r}, case declares {case.operation!r}",
        )
    if _canonical(case.id, encode_request(envelope), "re-encoded request") != _canonical(
        case.id, case.request, "recorded request"
    ):
        raise AdapterConformanceError(
            case.id, "request does not survive a decode/encode round trip unchanged"
        )
    _check_envelope_against_the_canonical_schema(
        case, case.request, _REQUEST_ENVELOPE_REF, "request envelope", schemas
    )
    try:
        operation = validate_operation_request_metadata(case.operation, envelope.metadata)
    except ContractSemanticError as error:
        raise AdapterConformanceError(
            case.id, f"request metadata does not satisfy the catalogue: {error}"
        ) from error

    decoded_input = _check_payload(
        case, operation.input_schema_ref, envelope.input, "request input", schemas
    )
    _check_payload_semantics(case, operation, decoded_input)
    _check_nested_semantics(case, decoded_input, "request input")
    return decoded_input


def _decoded_response(
    case: AdapterConformanceCase, payload: Mapping[str, Any]
) -> SuccessResponseEnvelope | ErrorResponseEnvelope:
    try:
        return decode_response(payload)
    except Exception as error:
        raise AdapterConformanceError(case.id, f"response does not decode: {error}") from error


def _check_response_metadata_semantics(
    case: AdapterConformanceCase,
    envelope: SuccessResponseEnvelope | ErrorResponseEnvelope,
) -> None:
    """The response rules that belong to the envelope, not to a result.

    Both apply whichever branch the response took.

    The nested walk reaches contract types carried by ``ResponseMetadata``: a
    `ProjectionFreshness` on a response is the same object under the same rule as
    one inside a result. The synchronous-completion rule says an operation the
    catalogue completes synchronously must not announce a job -- a caller reading
    one would believe asynchronous work had begun, and that is no less misleading
    on a failure than on a success. `import.start` is excluded because it
    genuinely does start a job, and has its own rule for saying so.
    """
    _check_nested_semantics(case, envelope.metadata, "response metadata")
    if get_operation_metadata(case.operation).job.completion_mode == "synchronous":
        try:
            validate_synchronous_job_response_job_reference(
                case.operation, envelope.metadata.job
            )
        except ContractSemanticError as error:
            raise AdapterConformanceError(case.id, str(error)) from error


def _check_response_branch_and_error(
    case: AdapterConformanceCase,
    envelope: SuccessResponseEnvelope | ErrorResponseEnvelope,
    decoded_input: Any,
    schemas: _CanonicalSchemas,
    state: _RunState,
    restates_an_earlier_outcome: bool,
) -> Any:
    """Check the branch, and return the decoded success result if there is one."""
    actual_branch = SUCCESS_BRANCH if isinstance(envelope, SuccessResponseEnvelope) else ERROR_BRANCH
    if actual_branch != case.branch:
        raise AdapterConformanceError(
            case.id, f"expected the {case.branch!r} branch, got {actual_branch!r}"
        )

    # Cross-cutting response rules, applied to *both* branches before the branch
    # split. A response envelope is the same document whichever branch it takes:
    # a `ProjectionFreshness` on an error carries the same meaning, and an
    # operation that completes synchronously may no more announce a job when it
    # fails than when it succeeds. Living inside the success branch, these two
    # ran on 47 of the corpus's exchanges and silently skipped the other 26 --
    # and a schema-valid error envelope could contradict the frozen contract
    # freely, because the canonical schema enforces structure, not semantics.
    #
    # Operation-specific *result* semantics stay success-only below: an error
    # response has no result for them to judge.
    _check_response_metadata_semantics(case, envelope)

    if isinstance(envelope, SuccessResponseEnvelope):
        if case.error_code is not None:
            raise AdapterConformanceError(
                case.id, "a success case must not declare an expected error code"
            )
        operation = get_operation_metadata(case.operation)
        decoded_result = _check_payload(
            case, operation.result_schema_ref, envelope.result, "success result", schemas
        )
        _check_result_belongs_to_the_selected_workspace(case, decoded_result)
        _check_nested_semantics(case, decoded_result, "success result")
        _check_job_observations(
            case, decoded_result, state, restates_an_earlier_outcome
        )
        _record_job_origin_facts(case, decoded_input, decoded_result, state)
        _check_declared_result_semantics(
            case, envelope, decoded_input, decoded_result, restates_an_earlier_outcome,
            state,
        )
        # Recorded after this exchange's own result semantics have validated the
        # binding it declares, so only a binding the frozen rule accepted is
        # carried forward to the page that presents the token.
        _record_issued_token_binding(case, state)
        return decoded_result

    if case.error_code is not None and envelope.error.code != case.error_code:
        raise AdapterConformanceError(
            case.id, f"expected error {case.error_code!r}, got {envelope.error.code!r}"
        )
    # The allow-list and retry-class rules are the frozen ones, called not restated.
    try:
        validate_operation_error(case.operation, envelope.error)
    except ContractSemanticError as error:
        raise AdapterConformanceError(case.id, str(error)) from error

    frozen_class = retry_class_for(envelope.error.code)
    if envelope.error.retry_class != frozen_class:
        raise AdapterConformanceError(
            case.id,
            f"error {envelope.error.code!r} must carry retry class {frozen_class!r}, "
            f"got {envelope.error.retry_class!r}",
        )
    if case.retry_class is not None and case.retry_class != frozen_class:
        raise AdapterConformanceError(
            case.id,
            f"case declares retry class {case.retry_class!r}, but {envelope.error.code!r} "
            f"is frozen as {frozen_class!r}",
        )
    return None


def _check_identifier_correlation(
    case: AdapterConformanceCase, envelope: SuccessResponseEnvelope | ErrorResponseEnvelope
) -> None:
    """The response must answer *this* request, by identifier.

    A response is only an answer if it says which call it answers. Without this,
    an adapter that returned a correct-looking envelope for a different in-flight
    request would conform, and no other check here would notice: every field it
    carries is individually valid.
    """
    request_metadata = _require_mapping(case.request.get("metadata", {}), "request.metadata")
    for name, actual in (
        ("request_id", envelope.metadata.request_id),
        ("correlation_id", envelope.metadata.correlation_id),
    ):
        expected = request_metadata.get(name)
        if actual != expected:
            raise AdapterConformanceError(
                case.id,
                f"response {name} {actual!r} does not match the request's {expected!r}",
            )


def _check_principal_correlation(
    case: AdapterConformanceCase, envelope: SuccessResponseEnvelope | ErrorResponseEnvelope
) -> None:
    """The case's declared principal must be the one the response attests.

    ``principal_id`` decides which exchanges are comparable for idempotency and
    which actor a governance transition is judged against, so a case that
    declares one principal while its response attests another is measured against
    an identity that never made the call. This is the same correlation the
    workspace already gets, applied to the other half of the tenancy pair.
    """
    authority = envelope.metadata.authority
    attested = getattr(authority, "principal_id", None)
    if attested != case.principal_id:
        raise AdapterConformanceError(
            case.id,
            f"declares principal {case.principal_id!r} but its response attests "
            f"{attested!r}",
        )


def _check_link_integrity(case: AdapterConformanceCase, by_id: Mapping[str, Any]) -> None:
    """A `replay_of`/`page_of` link must name a real, compatible, earlier case.

    Checked before the links are *used*, so a malformed reference is reported as
    a corpus defect rather than surfacing later as a confusing mismatch -- or, in
    the self-referential case, as a comparison that trivially succeeds.
    """
    for link, target_id in (("replay_of", case.replay_of), ("page_of", case.page_of)):
        if target_id is None:
            continue
        if target_id == case.id:
            raise AdapterConformanceError(case.id, f"{link} refers to itself")
        target = by_id.get(target_id)
        if target is None:
            raise AdapterConformanceError(case.id, f"{link} names unknown case {target_id!r}")
        if target.operation != case.operation:
            raise AdapterConformanceError(
                case.id,
                f"{link} names {target_id!r}, which is {target.operation!r} rather than "
                f"{case.operation!r}",
            )
        # Checked over the *union* of both link kinds: allowing `page_of` to
        # name a `replay_of` case (or the reverse) lets chains of unbounded depth
        # through while each individual link looks well formed.
        #
        # This is also what makes a cycle unconstructible, together with the
        # ordering rule in `validate_case_collection`: every link must resolve to
        # an exchange that is both strictly earlier and itself unlinked, so the
        # relationship graph has depth at most one and no edge can point forward.
        # A separate cycle walk used to run after these two and could never fire.
        if target.replay_of is not None or target.page_of is not None:
            raise AdapterConformanceError(
                case.id,
                f"{link} names {target_id!r}, which is itself linked; a link must "
                "resolve to an originating exchange, not chain",
            )
        if target.workspace_id != case.workspace_id or target.principal_id != case.principal_id:
            raise AdapterConformanceError(
                case.id,
                f"{link} names {target_id!r}, which ran under a different principal or "
                "workspace, so the two exchanges are not comparable",
            )
        if link == "page_of" and target.branch != SUCCESS_BRANCH:
            raise AdapterConformanceError(
                case.id, f"page_of names {target_id!r}, which is not a success exchange"
            )


def _result_of(case: AdapterConformanceCase) -> Mapping[str, Any]:
    """The case's success result as plain JSON types, or an empty mapping."""
    result = case.response.get("result")
    if not isinstance(result, Mapping):
        return {}
    plain: Mapping[str, Any] = _plain(result)
    return plain


@dataclass(frozen=True)
class _DerivedRelationships:
    """The links the *exchanges* imply, independent of what any case declares.

    Two exchanges are a repeat because they carry the same idempotency key under
    the same principal and workspace, and one continues another because it
    presents the token the other issued. Both are facts about the recorded
    documents. Reading them from the declaration instead -- which is what this
    runner did -- means a check exists only where a case volunteers that it
    should: omit ``expect.replay_of`` and the idempotency comparison disappears,
    which is precisely the case an adapter with a duplicate-mutation bug would
    produce. The declaration is kept, and checked against what was derived, but
    it is no longer the trigger.
    """

    replay_origin: Mapping[str, str]
    page_origin: Mapping[str, str]


def _idempotency_key(case: AdapterConformanceCase) -> str | None:
    metadata = case.request.get("metadata")
    key = metadata.get("idempotency_key") if isinstance(metadata, Mapping) else None
    return key if isinstance(key, str) and key.strip() else None


def _issued_continuation_token(case: AdapterConformanceCase) -> str | None:
    """The continuation token this exchange's result handed out, if any."""
    result = case.response.get("result")
    page = result.get("page") if isinstance(result, Mapping) else None
    token = page.get("continuation_token") if isinstance(page, Mapping) else None
    return token if isinstance(token, str) and token else None


def _presented_continuation_token(case: AdapterConformanceCase) -> str | None:
    """The continuation token this exchange's request carried, if any."""
    page = case.operation_input.get("page")
    token = page.get("continuation_token") if isinstance(page, Mapping) else None
    return token if isinstance(token, str) and token else None


def _derive_relationships(
    cases: Sequence[AdapterConformanceCase],
) -> _DerivedRelationships:
    """Read the repeat and continuation links out of the exchanges themselves."""
    replay_origin: dict[str, str] = {}
    page_origin: dict[str, str] = {}
    first_for_key: dict[tuple[str, str, str | None, str], str] = {}
    issuer_of_token: dict[tuple[str, str, str | None, str], str] = {}

    for case in cases:
        scope = (case.operation, case.principal_id, case.workspace_id)
        key = _idempotency_key(case)
        if key is not None:
            fingerprint = (*scope, key)
            origin = first_for_key.get(fingerprint)
            if origin is None:
                first_for_key[fingerprint] = case.id
            else:
                replay_origin[case.id] = origin

        presented = _presented_continuation_token(case)
        if presented is not None:
            issuer = issuer_of_token.get((*scope, presented))
            if issuer is not None:
                page_origin[case.id] = issuer
        issued = _issued_continuation_token(case)
        if issued is not None:
            issuer_of_token.setdefault((*scope, issued), case.id)

    return _DerivedRelationships(replay_origin=replay_origin, page_origin=page_origin)


#: Operations whose replay equality is *semantic*, not byte-exact.
#:
#: A replayed control call re-authorizes and answers from the recorded outcome,
#: but the job it describes goes on evolving between the original call and the
#: replay: a cancellation that was `cancellation_requested` may since have taken
#: effect, and a job a `retry_scheduled` left `queued` may since have started.
#: `validate_job_control_replay` therefore fixes the operation, the job identity
#: and the disposition, and checks the *handle* as an observation progression
#: from the recorded one. Demanding byte equality here rejected an honest replay
#: for reporting the truth -- a conforming provider refused by the harness.
_SEMANTIC_REPLAY_OPERATIONS: Final[frozenset[str]] = frozenset(
    {"import.start", "job.cancel", "job.retry"}
)

#: The operations whose *fresh* result validator must not be re-run on a replay.
#:
#: Those rules judge a mutation against the handle it acted on. A replay
#: performed no mutation, so re-running them demands that a job which has since
#: moved on still look like the instant the original call returned -- which is
#: exactly what the frozen replay rule says must not be required.
#:
#: `import.start` belongs here for the same reason, and was wrongly left out on
#: the grounds that `validate_import_start_result` is a shape check rather than a
#: transition rule. It is not: it requires the job to be *non-terminal*, because
#: a fresh `import.start` returning a finished job would describe a job that never
#: existed. An honest replay may observe that same job after it has succeeded, so
#: the constraint rejected a conforming provider.
_FRESH_TRANSITION_OPERATIONS: Final[frozenset[str]] = frozenset(
    {"import.start", "job.cancel", "job.retry"}
)


def _decoded_result_of(case: AdapterConformanceCase, label: str) -> Any:
    """Decode a case's recorded success result as the type the catalogue binds."""
    operation = get_operation_metadata(case.operation)
    payload_type = _payload_type(operation.result_schema_ref, case.id, label)
    try:
        return payload_type.from_wire(_plain(case.response.get("result")))
    except Exception as error:
        raise AdapterConformanceError(
            case.id, f"{label} is not a valid {payload_type.__name__}: {error}"
        ) from error


def _check_replay_outcome(
    case: AdapterConformanceCase,
    by_id: Mapping[str, Any],
    origin_id: str | None,
    classification: str | None,
    decoded_result: Any = None,
) -> None:
    """An honest replay must answer with the original outcome; a conflict must refuse.

    These are the two halves of what an idempotency key is *for*. A replay that
    returned a different result would mean the second call did work the first had
    already done -- exactly the duplicate mutation the key exists to prevent --
    and a conflict answered on the success branch would mean one key silently
    committed two different requests.

    Driven by the classification *computed* from the two requests rather than by
    the one the case declares, so an undeclared repeat is held to the same rule
    as a declared one.
    """
    if origin_id is None or classification is None:
        return
    first = by_id[origin_id]

    if classification == IDEMPOTENCY_REPLAY:
        if case.branch != SUCCESS_BRANCH:
            raise AdapterConformanceError(
                case.id, "an honest replay must answer on the success branch"
            )
        if case.operation in _SEMANTIC_REPLAY_OPERATIONS:
            # The frozen rule, called rather than restated. It fixes the job,
            # the operation and the disposition, and allows the handle to be an
            # honest later reading -- which byte equality cannot express.
            try:
                validate_job_control_replay(
                    _decoded_result_of(first, f"result of {origin_id!r}"),
                    decoded_result
                    if decoded_result is not None
                    else _decoded_result_of(case, "result"),
                    label=f"replay of {origin_id!r}",
                )
            except ContractSemanticError as error:
                raise AdapterConformanceError(case.id, str(error)) from error
            return
        if _canonical(case.id, _result_of(case), "result") != _canonical(
            case.id, _result_of(first), f"result of {origin_id!r}"
        ):
            raise AdapterConformanceError(
                case.id,
                f"an honest replay must return the same result as {origin_id!r}, "
                "answered from the recorded outcome rather than mutating again",
            )
    elif classification == IDEMPOTENCY_CONFLICT:
        if case.branch != ERROR_BRANCH:
            raise AdapterConformanceError(
                case.id,
                "an idempotency conflict must be refused on the error branch, not answered "
                "with a success",
            )
        if case.error_code != "idempotency_conflict":
            raise AdapterConformanceError(
                case.id,
                f"an idempotency conflict must carry error code 'idempotency_conflict', "
                f"found {case.error_code!r}",
            )


@dataclass(frozen=True)
class _ResultContext:
    """Everything a frozen result validator may draw on.

    Two sources, kept apart on purpose. Fields taken from the *exchange* are
    facts the documents themselves carry. Everything else comes from the case's
    ``trusted`` block -- facts a server knows and no wire document states. Mixing
    them would let the runner quietly supply a value it invented, and the check
    would then assert agreement with the runner rather than with the server.
    """

    case: AdapterConformanceCase
    decoded_input: Any
    decoded_result: Any
    job_reference: Any
    precondition: Any
    authority: Any
    scopes: Any
    purpose: Any
    canonical_resolution_time: Any
    freshness: Any
    #: What earlier exchanges in this collection established.
    state: _RunState = field(default_factory=lambda: _RunState())

    def request_page_binding(self) -> Any:
        """The binding of the token this request presents.

        Taken from the exchange that *issued* the token whenever the collection
        contains it. Nothing otherwise required the binding a page claims when it
        consumes a token to match the one attached when the token was issued, so
        one token could name a four-event snapshot on the way out and a five-event
        snapshot on the way back, with each page internally coherent.

        A case may still declare the binding, and it is then checked rather than
        used -- and it remains the only source for a token whose issuing exchange
        the collection does not contain.
        """
        presented = _presented_continuation_token(self.case)
        retained = (
            self.state.token_bindings.get(_token_scope(self.case, presented))
            if presented is not None
            else None
        )
        declared = _page_binding(self.case, "request_page_binding")
        if retained is None:
            return declared
        if declared is not None and declared != retained:
            raise AdapterConformanceError(
                self.case.id,
                "trusted.request_page_binding disagrees with the binding attached to "
                f"continuation token {presented!r} when it was issued",
            )
        return retained

    def accepted_import_source(self, job_id: object) -> Any:
        """The source `import.start` accepted for `job_id`, preferring the exchange.

        A transcript that contains the originating `import.start` already states
        this authoritatively, and the descriptor is immutable, so the retained
        value wins over anything the case declares. Left to the trusted block, a
        completion could report a different source and be judged against a fact
        moved to match it -- the two documents agreeing only because the same
        author wrote both.

        The trusted fallback survives for a collection that records a later
        reading of a job whose origin it does not contain, which is a legitimate
        transcript. A declaration that *contradicts* a known origin is refused
        rather than quietly preferred either way.
        """
        record = self.state.jobs.get(job_id) if isinstance(job_id, str) else None
        if record is None or record.accepted_import_source is None:
            return self.trusted("accepted_import_source", generated.ImportSourceDescriptor)
        retained = record.accepted_import_source
        if self.case.trusted.get("accepted_import_source") is not None:
            stated = self.trusted("accepted_import_source", generated.ImportSourceDescriptor)
            if stated != retained:
                raise AdapterConformanceError(
                    self.case.id,
                    "trusted.accepted_import_source contradicts the source "
                    f"{record.accepted_by!r} accepted for job {job_id!r}; the descriptor "
                    "import.start accepted is immutable",
                )
        return retained

    def string_set(self, key: str) -> frozenset[str]:
        """A trusted fact that is a set of strings, guarded element by element.

        Checking only the container leaves an unhashable element to raise a raw
        ``TypeError`` out of ``frozenset``.
        """
        values = self.trusted(key, expected=list)
        for index, item in enumerate(values):
            if not isinstance(item, str):
                raise AdapterConformanceError(
                    self.case.id,
                    f"trusted.{key}[{index}] must be a string, got {type(item).__name__}",
                )
        return frozenset(values)

    def trusted(
        self,
        key: str,
        decoder: type[Any] | None = None,
        *,
        expected: type[Any] | tuple[type[Any], ...] | None = None,
    ) -> Any:
        """A declared trusted fact, decoded through its generated type if given.

        The corpus states trusted facts as *wire documents*, so they stay
        reviewable JSON rather than opaque blobs. Decoding them here through the
        accepted generated types means a malformed declaration fails as a
        conformance error naming the case, instead of reaching a frozen validator
        as the wrong Python type.
        """
        if key not in self.case.trusted:
            raise AdapterConformanceError(
                self.case.id,
                f"{self.case.operation!r} result semantics require the trusted fact "
                f"{key!r}, which this case does not declare",
            )
        value = self.case.trusted[key]
        if expected is not None and not isinstance(value, expected):
            raise AdapterConformanceError(
                self.case.id,
                f"trusted.{key} must be {getattr(expected, '__name__', expected)}, "
                f"got {type(value).__name__}",
            )
        if decoder is None:
            return value
        try:
            return decoder.from_wire(value)
        except Exception as error:
            raise AdapterConformanceError(
                self.case.id,
                f"trusted.{key} is not a valid {decoder.__name__}: {error}",
            ) from error


def _discard(_value: object) -> None:
    """Drop a validator's return value; only "it did not raise" matters here."""


def _attested_authority(context: _ResultContext) -> generated.GrantedAuthority:
    """The authority the *response* attests this exchange ran under.

    Taken from ``response.metadata.authority`` rather than from the case, because
    that field is the server's own validated statement of what the call was
    performed with. ``request.metadata.principal_claim`` is not an alternative:
    the contract labels it UNTRUSTED, a claim that never becomes a
    :class:`GrantedAuthority` without server-side validation, so judging a result
    against it would check the caller's assertion about itself.
    """
    authority = context.authority
    if not isinstance(authority, generated.GrantedAuthority):
        raise AdapterConformanceError(
            context.case.id,
            f"response metadata carries no granted authority to judge "
            f"{context.case.operation!r} against, got {type(authority).__name__}",
        )
    return authority


def _attested_principal(context: _ResultContext) -> str:
    """The principal the response attests this exchange ran under."""
    authority = _attested_authority(context)
    return _require_str(authority.principal_id, "response metadata authority.principal_id")


def _governance_result(validator: Callable[..., None]) -> Callable[[_ResultContext], None]:
    """The four governance transitions share one relational shape.

    The actor is the principal the exchange attests, not a second identity the
    case declares beside it. A governance transition records who performed it,
    and a case that states the reviewer separately is checking that the response
    agrees with its own declaration -- which it will, because the same author
    wrote both. Taking the actor from the attested authority means the recorded
    history has to agree with the exchange that produced it.
    """

    def check(context: _ResultContext) -> None:
        validator(
            context.decoded_result,
            context.decoded_input,
            context.precondition,
            context.case.workspace_id,
            _attested_principal(context),
            # The actor *kind* stays trusted: no wire document in this contract
            # states whether the principal is a user, a service or an agent.
            context.trusted("expected_actor_kind"),
        )

    return check


def _result_semantics_table() -> dict[str, Callable[[_ResultContext], None]]:
    return {
        "candidate.approve": _governance_result(validate_candidate_approve_result),
        "candidate.reject": _governance_result(validate_candidate_reject_result),
        "record.supersede": _governance_result(validate_record_supersede_result),
        "knowledge.propose": _governance_result(validate_knowledge_propose_result),
        "graph.traverse": lambda c: validate_graph_traversal_result(
            c.decoded_result,
            c.decoded_input,
            c.case.workspace_id,
            c.canonical_resolution_time,
            c.string_set("authorized_views"),
        ),
        "knowledge.search": lambda c: validate_knowledge_search_result(
            c.decoded_result,
            c.decoded_input,
            c.case.workspace_id,
            c.canonical_resolution_time,
            # A declared view set is a *set*: the corpus states it as a JSON
            # array because JSON has no set, so it is converted at the boundary.
            c.string_set("authorized_views"),
        ),
        "context_pack.build": lambda c: validate_context_pack_build_result(
            c.decoded_result,
            request=c.decoded_input,
            expected_workspace_id=c.case.workspace_id,
            # The authority the response attests, not one the case declares
            # beside it: a pack states the authority it was built under, and the
            # only statement that can contradict it is the exchange's own.
            expected_authority=_attested_authority(c),
            expected_scopes=c.scopes,
            expected_purpose=c.purpose,
            expected_policy_versions=c.trusted("expected_policy_versions"),
            expected_authorized_candidate_set=c.trusted(
                "expected_authorized_candidate_set",
                generated.ContextPackAuthorizedCandidateSetManifest,
            ),
            canonical_resolution_time=c.canonical_resolution_time,
            response_freshness=c.freshness,
        ),
        "job.get": lambda c: validate_job_get_result(
            c.decoded_result,
            c.decoded_input,
            accepted_import_source=c.accepted_import_source(
                getattr(c.decoded_input, "job_id", None)
            ),
        ),
        # These two return the accepted disposition rather than None; the
        # conformance table only cares that they did not raise.
        "job.cancel": lambda c: _discard(
            validate_job_cancel_result(
                c.decoded_result,
                c.decoded_input,
                previous=c.trusted("previous_job_handle", generated.JobHandle),
            )
        ),
        "job.retry": lambda c: _discard(
            validate_job_retry_result(
                c.decoded_result,
                c.decoded_input,
                previous=c.trusted("previous_job_handle", generated.JobHandle),
            )
        ),
        "memory.create": lambda c: validate_memory_create_result(
            c.decoded_result, c.case.workspace_id
        ),
        "evidence.search": lambda c: validate_evidence_search_result(
            c.decoded_result, c.decoded_input, c.case.workspace_id
        ),
        "import.start": lambda c: validate_import_start_result(
            c.decoded_result, response_job_reference=c.job_reference
        ),
        "job.events": lambda c: validate_job_events_result(
            c.decoded_result,
            c.decoded_input,
            principal_id=c.case.principal_id,
            workspace_id=c.case.workspace_id,
            request_binding=c.request_page_binding(),
            result_binding=_page_binding(c.case, "result_page_binding"),
        ),
    }


def _page_binding(case: AdapterConformanceCase, key: str) -> JobEventsPageBinding | None:
    """Build the trusted page binding the case declares, if any.

    A continuation token is opaque on the wire: nothing in the document says what
    it binds. The server knows, so the corpus states it, and the frozen rule then
    checks the token against that statement rather than against a value this
    module made up.
    """
    declared = case.trusted.get(key)
    if declared is None:
        return None
    fields = _require_mapping(declared, f"{case.id}.trusted.{key}")
    try:
        return JobEventsPageBinding(**fields)
    except TypeError as error:
        raise AdapterConformanceError(
            case.id, f"trusted.{key} is not a valid page binding: {error}"
        ) from error


def _check_result_belongs_to_the_selected_workspace(
    case: AdapterConformanceCase, result: object
) -> None:
    """Every workspace-scoped identifier in a result must be the selected workspace.

    This is cross-cutting rather than per-operation on purpose. Only some
    operations have a frozen result validator, and the ones that do not --
    `memory.get`, `memory.list`, `workspace.inspect` -- are exactly the reads
    where returning another tenant's record would be most damaging and least
    visible. A result that names a workspace the request did not select is
    answering a question nobody asked, whatever else about it is well formed.
    """
    selected = case.workspace_id
    if selected is None:
        return

    # Walk the *decoded* graph, not the raw document. A `workspace_id` key is
    # only tenancy metadata when the contract declares it as such: inside an
    # opaque `JsonObject` -- a governed record's `content`, an error's `details`
    # -- the same key is caller data the contract says nothing about, and a
    # record legitimately carrying `{"workspace_id": "external-domain-value"}`
    # must not be refused for it. Recursing through dataclasses alone draws that
    # line exactly where the contract draws it.
    def walk(node: object, path: str) -> None:
        if is_dataclass(node) and not isinstance(node, type):
            for declared in fields(node):
                value = getattr(node, declared.name)
                if (
                    declared.name == "workspace_id"
                    and isinstance(value, str)
                    and value != selected
                ):
                    raise AdapterConformanceError(
                        case.id,
                        f"result{path}.workspace_id is {value!r}, but the request selected "
                        f"{selected!r}",
                    )
                walk(value, f"{path}.{declared.name}")
        elif isinstance(node, (list, tuple)):
            for index, item in enumerate(node):
                walk(item, f"{path}[{index}]")

    walk(result, "")


def _check_response_job_reference_correlation(
    case: AdapterConformanceCase,
    envelope: SuccessResponseEnvelope,
    decoded_result: Any,
) -> None:
    """The response must reference the job its own result describes.

    `import.start` states which job it started twice -- once in the result handle
    and once in ``ResponseMetadata.job`` -- and a caller that held one id while
    tracking the other would be watching the wrong job. The frozen validator
    makes this check for a fresh call, but cannot run against an honest replay
    (it also requires a non-terminal job, which a replay may legitimately have
    outlived), so the correlation is made here for that case alone.

    Relational, and owned here for the same reason the request/response
    identifier correlation is: it binds two halves of one envelope rather than
    judging a value against the contract's own rules.
    """
    reference = envelope.metadata.job
    referenced = getattr(reference, "job_id", None)
    identity = getattr(getattr(decoded_result, "job", None), "identity", None)
    described = getattr(identity, "job_id", None)
    if referenced != described:
        raise AdapterConformanceError(
            case.id,
            f"response metadata references job {referenced!r} but its result describes "
            f"{described!r}",
        )


def _check_declared_result_semantics(
    case: AdapterConformanceCase,
    envelope: SuccessResponseEnvelope,
    decoded_input: Any,
    decoded_result: Any,
    restates_an_earlier_outcome: bool = False,
    state: _RunState | None = None,
) -> None:
    """Apply the frozen result rule for this operation, where one is applicable."""
    validator = _RESULT_SEMANTICS.get(case.operation)
    if validator is None:
        return
    # A fresh-result validator judges a mutation against the handle it acted on.
    # An honest replay performed no mutation, so running it here would reject a
    # replay for reporting a job that has since moved on -- the same false
    # rejection byte equality produced, arriving by another route.
    # `validate_job_control_replay` is what holds these pairs together instead.
    #
    # Dropping the whole validator would trade that false rejection for a false
    # certification, so what it establishes is accounted for rather than lost.
    # Immutable job identity, job kind and originating operation need no
    # restatement: the exchange being replayed passed the full validator, and the
    # frozen replay rule requires the replay's identity to equal it exactly. The
    # one statement left unaccounted for is which job the *replay's own* response
    # references, which is a correlation rather than a transition rule and holds
    # for a replay exactly as it does for a fresh call.
    if restates_an_earlier_outcome and case.operation in _FRESH_TRANSITION_OPERATIONS:
        if case.operation == "import.start":
            _check_response_job_reference_correlation(case, envelope, decoded_result)
        return
    request_metadata = decode_request(case.request).metadata
    context = _ResultContext(
        case=case,
        decoded_input=decoded_input,
        decoded_result=decoded_result,
        job_reference=envelope.metadata.job,
        precondition=request_metadata.mutation_precondition,
        authority=envelope.metadata.authority,
        scopes=request_metadata.scopes,
        purpose=request_metadata.purpose,
        canonical_resolution_time=envelope.metadata.canonical_resolution_time,
        freshness=envelope.metadata.freshness,
        state=state if state is not None else _RunState(),
    )
    try:
        validator(context)
    except AdapterConformanceError:
        raise
    except ContractSemanticError as error:
        raise AdapterConformanceError(
            case.id, f"success result fails {case.operation!r} semantics: {error}"
        ) from error


def _check_pagination(
    case: AdapterConformanceCase, by_id: Mapping[str, Any], derived: _DerivedRelationships
) -> None:
    """A later page must actually carry the token the earlier page issued.

    Reached whenever this request presents a token some earlier exchange issued,
    not only when the case declares itself a continuation. A second page that
    quietly changes the query still presents the first page's token, so the token
    is the honest trigger; the declaration is checked against it.
    """
    derived_origin = derived.page_origin.get(case.id)
    if case.page_of is not None and derived_origin is not None and case.page_of != derived_origin:
        raise AdapterConformanceError(
            case.id,
            f"declares it continues {case.page_of!r}, but the token it presents was issued "
            f"by {derived_origin!r}",
        )
    origin_id = case.page_of if case.page_of is not None else derived_origin
    if origin_id is None:
        return
    previous = by_id.get(origin_id)
    if previous is None:
        raise AdapterConformanceError(case.id, f"page_of names unknown case {origin_id!r}")
    if not get_operation_metadata(case.operation).pagination.paginated:
        raise AdapterConformanceError(
            case.id, f"{case.operation!r} is not a paginated operation"
        )

    # The token lives in the *result's* own `page`, not the envelope metadata:
    # pagination is part of each paginated operation's typed result contract, so
    # that is the statement a later page has to answer.
    previous_result = previous.response.get("result")
    previous_page = (
        previous_result.get("page") if isinstance(previous_result, Mapping) else None
    )
    issued = previous_page.get("continuation_token") if isinstance(previous_page, Mapping) else None
    if not isinstance(issued, str):
        raise AdapterConformanceError(
            case.id, f"case {origin_id!r} issued no continuation token to follow"
        )
    presented = case.operation_input.get("page")
    carried = presented.get("continuation_token") if isinstance(presented, Mapping) else None
    if carried != issued:
        raise AdapterConformanceError(
            case.id,
            f"must present the continuation token {issued!r} issued by {origin_id!r}, "
            f"got {carried!r}",
        )

    # Carrying the token is not the same as being the same query. A token is
    # issued *for* a particular request: principal, workspace, operation, filters
    # and ordering. Change the view or the query and present the old token, and
    # the page you get back is a page of something else -- literal token equality
    # would wave that through, so the stable part of the input is compared too.
    stable_now = {
        key: value for key, value in case.operation_input.items() if key != "page"
    }
    stable_first = {
        key: value for key, value in previous.operation_input.items() if key != "page"
    }
    if to_canonical_json(_plain(stable_now)) != to_canonical_json(_plain(stable_first)):
        raise AdapterConformanceError(
            case.id,
            f"presents {origin_id!r}'s continuation token under a different request; "
            "a token binds the query, filters and ordering that produced it",
        )


def _check_idempotency(
    case: AdapterConformanceCase, by_id: Mapping[str, Any], derived: _DerivedRelationships
) -> tuple[str | None, str | None]:
    """Classify this exchange against the one it repeats, and return that pair.

    The pair is whichever the *exchanges* imply -- same operation, principal,
    workspace and idempotency key -- falling back to the declaration. A case that
    declares a repeat the wire does not support, or repeats an exchange it never
    mentions, is a defect either way: the two statements have to agree.
    """
    derived_origin = derived.replay_origin.get(case.id)
    if (
        case.replay_of is not None
        and derived_origin is not None
        and case.replay_of != derived_origin
    ):
        raise AdapterConformanceError(
            case.id,
            f"declares it replays {case.replay_of!r}, but its idempotency key is the one "
            f"{derived_origin!r} used; the declaration and the exchange disagree",
        )
    if case.replay_of is None and derived_origin is None:
        if case.idempotency is not None:
            raise AdapterConformanceError(
                case.id, "expect.idempotency requires expect.replay_of"
            )
        return None, None
    if case.replay_of is not None and case.idempotency is None:
        raise AdapterConformanceError(case.id, "expect.replay_of requires expect.idempotency")

    origin_id = case.replay_of if case.replay_of is not None else derived_origin
    assert origin_id is not None
    first = by_id.get(origin_id)
    if first is None:
        raise AdapterConformanceError(case.id, f"replay_of names unknown case {origin_id!r}")

    # Any failure here -- including a malformed *referenced* case, which decodes
    # outside this case's own request path -- is normalized. A raw
    # `ContractDecodeError` escaping the public runner would make a corpus defect
    # look like a crash and would carry no case id to act on.
    try:
        first_equivalence = idempotency_equivalence(
            first.operation,
            decode_request(first.request).metadata,
            first.operation_input,
            principal_id=first.principal_id,
            workspace_id=first.workspace_id,
        )
        second_equivalence = idempotency_equivalence(
            case.operation,
            decode_request(case.request).metadata,
            case.operation_input,
            principal_id=case.principal_id,
            workspace_id=case.workspace_id,
        )
    except AdapterConformanceError:
        raise
    except Exception as error:
        raise AdapterConformanceError(
            case.id,
            f"cannot compute idempotency equivalence against {origin_id!r}: "
            f"{type(error).__name__}: {error}",
        ) from error

    classification = classify_idempotent_replay(first_equivalence, second_equivalence)
    if case.idempotency is not None and classification != case.idempotency:
        raise AdapterConformanceError(
            case.id,
            f"against {origin_id!r} this request classifies as {classification!r}, "
            f"case declares {case.idempotency!r}",
        )
    return origin_id, classification


def _check_exchange(
    case: AdapterConformanceCase,
    adapter: ApplicationWireAdapter,
    by_id: Mapping[str, Any],
    schemas: _CanonicalSchemas,
    derived: _DerivedRelationships,
    state: _RunState,
) -> None:
    decoded_input = _check_request_is_what_the_catalogue_describes(case, schemas)
    _check_link_integrity(case, by_id)

    # The adapter is handed its own copy, and that copy is compared afterwards.
    # A request is the caller's to own: an adapter that rewrote it in place would
    # corrupt a retry, a replay comparison, or an idempotency fingerprint computed
    # from it -- each of which would then measure the adapter's edit rather than
    # the caller's intent. Copying also keeps a misbehaving adapter from editing
    # the corpus itself and silently changing every later case in the run.
    presented = copy.deepcopy(dict(case.request))
    before = _canonical(case.id, presented, "request envelope")
    try:
        actual = adapter.call(presented)
    except AdapterConformanceError:
        raise
    except (KeyboardInterrupt, SystemExit):
        # The operator's, not the adapter's; never disguise these as a verdict.
        raise
    except BaseException as error:
        raise AdapterConformanceError(
            case.id, f"adapter raised {type(error).__name__}: {error}"
        ) from error
    if _canonical(case.id, presented, "request envelope") != before:
        raise AdapterConformanceError(
            case.id, "adapter mutated the request envelope it was given"
        )

    if not isinstance(actual, Mapping) or not all(isinstance(key, str) for key in actual):
        raise AdapterConformanceError(
            case.id,
            f"adapter returned {type(actual).__name__}, not a response envelope mapping",
        )
    actual_mapping: Mapping[str, Any] = actual
    if _canonical(case.id, actual_mapping, "adapter response") != _canonical(
        case.id, case.response, "recorded response"
    ):
        raise AdapterConformanceError(
            case.id, "adapter response does not match the recorded response"
        )

    envelope = _decoded_response(case, actual_mapping)
    if _canonical(case.id, encode_response(envelope), "re-encoded response") != _canonical(
        case.id, case.response, "recorded response"
    ):
        raise AdapterConformanceError(
            case.id,
            "response does not survive a decode/encode round trip unchanged "
            "(a provenance, temporal, or nested value was lost or reordered)",
        )
    _check_envelope_against_the_canonical_schema(
        case,
        actual_mapping,
        _SUCCESS_ENVELOPE_REF
        if isinstance(envelope, SuccessResponseEnvelope)
        else _ERROR_ENVELOPE_REF,
        "response envelope",
        schemas,
    )
    _check_identifier_correlation(case, envelope)
    _check_principal_correlation(case, envelope)
    # A success case that repeats an earlier call answers from the recorded
    # outcome; it restates, it does not observe.
    #
    # Narrower than "has a replay link", and it has to be: a case may *declare*
    # `replay_of` while carrying a different idempotency key, which classifies as
    # `distinct` and pins its result to nothing. Treating that as a restatement
    # let it skip the job correlation and contradict an earlier exchange freely.
    # Only a classification of `replay` forces the result to equal the original's,
    # and only that earns the exemption. A derived-but-undeclared repeat is safe
    # too: it classifies as `replay` (equality enforced) or as a conflict, which
    # is refused on the error branch and carries no result at all.
    restates = case.branch == SUCCESS_BRANCH and (
        case.idempotency == IDEMPOTENCY_REPLAY
        or (case.replay_of is None and case.id in derived.replay_origin)
    )
    decoded_result = _check_response_branch_and_error(
        case, envelope, decoded_input, schemas, state, restates
    )
    _check_pagination(case, by_id, derived)
    origin_id, classification = _check_idempotency(case, by_id, derived)
    _check_replay_outcome(case, by_id, origin_id, classification, decoded_result)


def run_adapter_conformance(
    adapter: ApplicationWireAdapter,
    cases: Iterable[AdapterConformanceCase] | None = None,
) -> int:
    """Drive every corpus case through `adapter`, returning the number verified.

    Raises :class:`AdapterConformanceError` on the first exchange the adapter
    fails to honour. Failing at the first divergence rather than collecting every
    one keeps the report about a single concrete exchange: once an adapter is
    returning the wrong response for one call, later failures are usually that
    same defect seen again rather than independent evidence.
    """
    if cases is None:
        corpus = load_adapter_conformance_corpus()
    else:
        try:
            corpus = tuple(cases)
        except TypeError as error:
            raise AdapterConformanceError(
                "<cases>", f"cases must be iterable, got {type(cases).__name__}"
            ) from error
    by_id = validate_case_collection(corpus)
    derived = _derive_relationships(corpus)
    schemas = _CanonicalSchemas()
    # Every observation of a job, across every exchange, so two recorded
    # exchanges naming the same job can be held to the frozen progression rule.
    state = _RunState()
    for case in corpus:
        _check_exchange(case, adapter, by_id, schemas, derived, state)
    return len(corpus)


_RESULT_SEMANTICS.update(_result_semantics_table())
