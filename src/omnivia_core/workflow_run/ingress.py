"""Structured ingress recipes and the observation they produce (V06-8, A8-N1).

The input side of the lane, and it is answer-blind by construction: nothing here
imports :mod:`~omnivia_core.workflow_run.conformance`, so a recipe cannot be
written towards an expectation, and :func:`derive_decision` never sees a case
identifier, title, prose stimulus or expected field. What it sees is a recipe --
operation, workspace binding, granted capabilities, caller principal, payload
provenance, idempotency ledger state and explicit cancellation state -- and the
frozen operation catalogue. Everything else it computes.

Where a case has Core ingress the request really is encoded through the frozen
codec and really does cross
:class:`~omnivia_core.contracts.v1.adapter.InProcessFakeAdapter`, the existing
application-contract seam. The seam is a transcript, so it cannot decide
anything: what it proves is that the decision this module derived encodes as a
well-formed envelope on the branch it claims, survives the trip, and comes back
naming the same error code and the same audit reference. Run-local cases make no
Core call at all and are recorded as explicit no-Core-operation observations.

`RUN-V-15` is executed by recording *no* exchange for its request. The fake
adapter refuses an unrecorded request rather than improvising one, which is
exactly the situation the case models: the run issued the mutation and never
observed the response. The outcome is then the reconciliation oracle's two
branches, not a boolean.

Idempotency keys are derived here, locally, from run id, task node id and a
fixed canonical content digest. `RUN-P-05` is unresolved, so this derivation is
deliberately private to the harness: it declares no public key-derivation
contract, and no product code reads it.

Standard library only. No Workflow Runtime, no host adapter, no run-state
storage, no new operation, capability, error code or envelope field.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Final

from omnivia_core.contracts.v1.adapter import InProcessFakeAdapter
from omnivia_core.contracts.v1.codec import (
    decode_response,
    encode_request,
    encode_response,
    response_error,
    retry_class_for,
    to_canonical_json,
)
from omnivia_core.contracts.v1.compatibility import ContractSemanticError
from omnivia_core.contracts.v1.generated import (
    ApiError,
    CandidateAssertion,
    CandidateExtractionMetadata,
    CapabilityRef,
    CapabilityRequirement,
    CapabilitySet,
    ClientIdentity,
    CompatibilityMetadata,
    ErrorResponseEnvelope,
    EvidenceReference,
    GrantedAuthority,
    MemoryCreateInput,
    RequestEnvelope,
    RequestMetadata,
    ResponseMetadata,
    SourceReference,
    SuccessResponseEnvelope,
    UpgradeState,
    VersionCapabilityEnvelope,
    VersionWindow,
)
from omnivia_core.contracts.v1.semantics import validate_memory_create_input
from omnivia_core.workflow_run.boundary import (
    CATALOGUE,
    GOVERNANCE_CAPABILITY,
    INSTALLATION_SCOPE_KIND,
    NO_SIDE_EFFECT,
    RUN_LINEAGE_SOURCE_KIND,
    BranchId,
    Disposition,
    MutationAuthority,
    PrincipalKind,
)

WORKSPACE_ALPHA: Final = "ws.a8-alpha"
WORKSPACE_BETA: Final = "ws.a8-beta"
AGENT_ID: Final = "agent.synthetic.a8"
AGENT_PRINCIPAL: Final = "principal.a8-agent"
HUMAN_PRINCIPAL: Final = "principal.a8-governor"
API_VERSION: Final = "1.0"
SERVER_VERSION: Final = "1.0.0"
ASSERTED_AT: Final = "2026-08-03T00:00:00Z"
RECORD_TYPE: Final = "memory.fact"
DOMAIN_SCOPE: Final = "operations.deployment"
EVIDENCE_AVAILABLE: Final = "available"

PURPOSE_CAPTURE: Final = "agent.knowledge_capture"
PURPOSE_RESEARCH: Final = "agent.research"
PURPOSE_GOVERNANCE: Final = "governance.review"

#: One octet over the frozen `Identifier` bound. Carried at full length: the
#: refusal under test is that Core rejects it rather than truncating it to fit,
#: and a truncated probe could not tell those apart.
OVERLONG_RUN_ID: Final = "run.a8-" + "0" * 122


class IngressRecipeError(ValueError):
    """A recipe that does not describe a runnable ingress."""


def content_digest(content: Mapping[str, Any]) -> str:
    """The canonical content digest an idempotency key is derived over.

    Canonical JSON from the frozen codec, so two runtimes that agree on the
    content agree on the digest without agreeing on key order or whitespace.
    """
    encoded = to_canonical_json(dict(content)).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


@dataclass(frozen=True, slots=True)
class KeyDerivation:
    """The three compaction-invariant inputs an idempotency key derives from."""

    run_id: str
    task_node_id: str
    content: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.content, Mapping):
            raise IngressRecipeError("content must be a mapping")
        object.__setattr__(self, "content", MappingProxyType(dict(self.content)))

    @property
    def digest(self) -> str:
        return content_digest(self.content)

    @property
    def key(self) -> str:
        material = f"{self.run_id}\x1f{self.task_node_id}\x1f{self.digest}"
        fingerprint = hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]
        return f"{self.run_id}:{self.task_node_id}:{fingerprint}"


@dataclass(frozen=True, slots=True)
class EvidenceRecipe:
    """One piece of evidence the candidate cites beyond its own run lineage."""

    kind: str
    source_id: str
    locator: str | None = None
    #: Where the evidence lives. `None` means the run's bound workspace.
    workspace_id: str | None = None
    #: Whether the reference resolves inside Core. A runtime handle does not,
    #: and Core never dereferences one.
    core_resolvable: bool = True


@dataclass(frozen=True, slots=True)
class IngressRecipe:
    """One structured stimulus, stated as inputs rather than as an outcome."""

    case_id: str
    run_id: str
    task_node_id: str
    session_id: str
    principal_kind: PrincipalKind
    bound_workspace_id: str
    purpose: str
    agent_id: str = AGENT_ID
    principal_id: str = AGENT_PRINCIPAL
    #: Whether Core validated the caller principal server-side. A claimed
    #: principal is untrusted until it has.
    principal_validated: bool = True
    #: `None` for a run-local case: the run does no Core ingress at all.
    operation: str | None = None
    requested_workspace_id: str | None = None
    granted_capabilities: tuple[str, ...] = ()
    #: The content the request carries, and the content a key derives over.
    request_input: Mapping[str, Any] = field(default_factory=dict)
    evidence: tuple[EvidenceRecipe, ...] = ()
    #: The run identifier the runtime emitted into provenance, when it is not
    #: the run's own well-formed id.
    lineage_run_id: str | None = None
    #: What Core already recorded under a derived key before this attempt.
    prior_attempts: tuple[KeyDerivation, ...] = ()
    #: A key that was persisted and reused after the content it was derived
    #: from changed -- the failure mode compaction makes possible.
    stale_key_derivation: KeyDerivation | None = None
    #: Run-local replay of ingress Core has already keyed.
    replayed_operations: tuple[str, ...] = ()
    #: The run was cancelled with a mutating ingress in flight.
    cancellation_raced: bool = False

    def __post_init__(self) -> None:
        if self.operation is not None and self.operation not in CATALOGUE:
            raise IngressRecipeError(
                f"{self.case_id} names an operation outside the catalogue:"
                f" {self.operation!r}"
            )
        for name in self.replayed_operations:
            if name not in CATALOGUE:
                raise IngressRecipeError(
                    f"{self.case_id} replays an operation outside the catalogue:"
                    f" {name!r}"
                )
        if self.operation is None and (
            self.granted_capabilities or self.requested_workspace_id
        ):
            raise IngressRecipeError(
                f"{self.case_id} is run-local yet describes a Core call"
            )
        object.__setattr__(
            self, "request_input", MappingProxyType(dict(self.request_input))
        )

    @property
    def carries_memory_payload(self) -> bool:
        return self.operation == "memory.create"

    @property
    def key_derivation(self) -> KeyDerivation:
        return KeyDerivation(
            run_id=self.run_id,
            task_node_id=self.task_node_id,
            content=self.request_input,
        )

    @property
    def idempotency_key(self) -> str:
        derivation = self.stale_key_derivation or self.key_derivation
        return derivation.key


@dataclass(frozen=True, slots=True)
class ObservedBranch:
    """One reconciliation outcome the oracle says is legitimate."""

    branch_id: BranchId
    core_mutation: bool
    canonical_mutation: bool
    core_audit_event: bool


@dataclass(frozen=True, slots=True)
class IngressDecision:
    """What the boundary rules make of one recipe, before it is compared."""

    disposition: Disposition
    error_code: str | None
    core_mutation: bool | None
    canonical_mutation: bool
    mutation_authority: MutationAuthority
    core_audit_event: bool | None
    response_lost: bool = False
    branches: tuple[ObservedBranch, ...] = ()


@dataclass(frozen=True, slots=True)
class IngressObservation:
    """Everything the harness observed for one case."""

    case_id: str
    principal_kind: PrincipalKind
    core_operations: tuple[str, ...]
    disposition: Disposition
    error_code: str | None
    core_mutation: bool | None
    canonical_mutation: bool
    mutation_authority: MutationAuthority
    core_audit_event: bool | None
    reconciliation_branches: tuple[ObservedBranch, ...] = ()
    request_metadata: Mapping[str, Any] = field(default_factory=dict)
    lineage_sources: tuple[Mapping[str, Any], ...] = ()
    response_observed: bool = True
    cancellation_raced: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "request_metadata", MappingProxyType(dict(self.request_metadata))
        )


# --- payload -----------------------------------------------------------------


def _lineage_source(recipe: IngressRecipe) -> SourceReference:
    """Run lineage as payload provenance: kind, run id, task node locator."""
    return SourceReference(
        kind=RUN_LINEAGE_SOURCE_KIND,
        source_id=recipe.lineage_run_id or recipe.run_id,
        locator=recipe.task_node_id,
    )


def memory_payload(recipe: IngressRecipe) -> MemoryCreateInput:
    """Build the candidate the recipe proposes, lineage included.

    `MemoryCreateInput` carries no authority level, governance state,
    currentness, record id or supersession field, so what a run can build here
    is structurally a candidate and nothing more.
    """
    sources = (
        _lineage_source(recipe),
        *(
            SourceReference(
                kind=item.kind, source_id=item.source_id, locator=item.locator
            )
            for item in recipe.evidence
        ),
    )
    assertion = CandidateAssertion(
        actor_id=recipe.agent_id,
        actor_kind=recipe.principal_kind.value,
        actor_role="author",
        asserted_at=ASSERTED_AT,
        evidence=tuple(EvidenceReference(source=source) for source in sources),
    )
    return MemoryCreateInput(
        record_type=RECORD_TYPE,
        domain_scope=DOMAIN_SCOPE,
        content=dict(recipe.request_input),
        evidence_disposition=EVIDENCE_AVAILABLE,
        sources=sources,
        assertion=assertion,
        extraction=CandidateExtractionMetadata(
            extractor_id=recipe.agent_id,
            extracted_at=ASSERTED_AT,
            extractor_version="1.0.0",
            model_version="1.0.0",
            prompt_version="1.0.0",
        ),
    )


def _payload_faults(recipe: IngressRecipe) -> tuple[str, ...]:
    """Why the payload is not a well-formed, in-workspace candidate."""
    if not recipe.carries_memory_payload:
        return ()
    faults: list[str] = []
    for item in recipe.evidence:
        if item.workspace_id is not None and (
            item.workspace_id != recipe.bound_workspace_id
        ):
            faults.append(
                f"cites evidence held in {item.workspace_id!r},"
                f" outside {recipe.bound_workspace_id!r}"
            )
        if not item.core_resolvable:
            faults.append(
                f"cites {item.source_id!r}, a runtime handle Core never resolves"
            )
    try:
        validate_memory_create_input(memory_payload(recipe))
    except ContractSemanticError as error:
        faults.append(str(error))
    return tuple(faults)


# --- decision ----------------------------------------------------------------


def derive_decision(recipe: IngressRecipe) -> IngressDecision:
    """Derive the outcome of one ingress from the recipe and the catalogue.

    The gates run in the order Core would have to apply them, and each one
    fails closed. Nothing here reads a case identifier or an expectation.
    """
    if recipe.operation is None:
        return _run_local_decision(recipe)
    operation = CATALOGUE[recipe.operation]
    audited = operation.audit.audited
    required = operation.required_capability.id

    # An agent actor never holds governance authority, whatever a caller
    # claims: recalled content, tool arguments and model output can never
    # expand a granted capability.
    if required == GOVERNANCE_CAPABILITY and (
        recipe.principal_kind is not PrincipalKind.HUMAN
        or not recipe.principal_validated
    ):
        return _rejected("capability_not_granted", audited)
    if required not in recipe.granted_capabilities:
        return _rejected("capability_not_granted", audited)

    if operation.scope.scope_kind == INSTALLATION_SCOPE_KIND:
        if recipe.requested_workspace_id is not None:
            return _rejected("invalid_request", audited)
    elif recipe.requested_workspace_id != recipe.bound_workspace_id:
        # A run is bound to one workspace for ingress; the grant comes from the
        # validated principal, never from run state.
        return _rejected("workspace_not_granted", audited)

    if _payload_faults(recipe):
        return _rejected("invalid_request", audited)

    if operation.idempotency.required:
        ledger = {attempt.key: attempt.digest for attempt in recipe.prior_attempts}
        recorded = ledger.get(recipe.idempotency_key)
        if recorded is not None:
            if recorded != recipe.key_derivation.digest:
                return _rejected("idempotency_conflict", audited)
            return IngressDecision(
                disposition=Disposition.NO_OP,
                error_code=None,
                core_mutation=False,
                canonical_mutation=False,
                mutation_authority=MutationAuthority.NONE,
                core_audit_event=audited,
            )

    if recipe.cancellation_raced:
        return IngressDecision(
            disposition=Disposition.INDETERMINATE_UNTIL_RECONCILED,
            error_code=None,
            core_mutation=None,
            canonical_mutation=False,
            mutation_authority=MutationAuthority.NONE,
            core_audit_event=None,
            response_lost=True,
            branches=reconcile(recipe),
        )

    canonical = (
        required == GOVERNANCE_CAPABILITY
        and recipe.principal_kind is PrincipalKind.HUMAN
        and recipe.principal_validated
    )
    return IngressDecision(
        disposition=Disposition.ACCEPTED,
        error_code=None,
        core_mutation=operation.scope.side_effect != NO_SIDE_EFFECT,
        canonical_mutation=canonical,
        mutation_authority=(
            MutationAuthority.VALIDATED_HUMAN_PRINCIPAL
            if canonical
            else MutationAuthority.NONE
        ),
        core_audit_event=audited,
    )


def _rejected(error_code: str, audited: bool) -> IngressDecision:
    """A refusal leaves no Core effect and no partial write."""
    return IngressDecision(
        disposition=Disposition.REJECTED,
        error_code=error_code,
        core_mutation=False,
        canonical_mutation=False,
        mutation_authority=MutationAuthority.NONE,
        core_audit_event=audited,
    )


def _run_local_decision(recipe: IngressRecipe) -> IngressDecision:
    """A run-local step invokes no Core operation, so it changes nothing.

    It can still be audited, but only through ingress Core already keyed: a
    whole-run replay resolves each mutating call through its derived key, which
    Core audits without committing anything a second time.
    """
    audited = any(CATALOGUE[name].audit.audited for name in recipe.replayed_operations)
    return IngressDecision(
        disposition=Disposition.NO_OP,
        error_code=None,
        core_mutation=False,
        canonical_mutation=False,
        mutation_authority=MutationAuthority.NONE,
        core_audit_event=audited,
    )


def reconcile(recipe: IngressRecipe) -> tuple[ObservedBranch, ...]:
    """The two legitimate outcomes of a cancellation racing a mutation.

    Reconciliation observes which occurred by replaying the derived key. It
    never undoes a Core effect that already committed, and neither branch can
    carry governance authority, so canonical mutation stays determinately false
    in both.
    """
    if not recipe.cancellation_raced or recipe.operation is None:
        raise IngressRecipeError(
            f"{recipe.case_id} has no cancellation race to reconcile"
        )
    operation = CATALOGUE[recipe.operation]
    if operation.scope.side_effect == NO_SIDE_EFFECT:
        raise IngressRecipeError(
            f"{recipe.case_id} races a read, which leaves nothing to reconcile"
        )
    return (
        ObservedBranch(
            branch_id=BranchId.COMMITTED_ONCE,
            core_mutation=True,
            canonical_mutation=False,
            core_audit_event=operation.audit.audited,
        ),
        ObservedBranch(
            branch_id=BranchId.NOT_COMMITTED,
            core_mutation=False,
            canonical_mutation=False,
            core_audit_event=False,
        ),
    )


# --- the wire ----------------------------------------------------------------


def _capability_refs(recipe: IngressRecipe) -> tuple[CapabilityRef, ...]:
    return tuple(
        CapabilityRef(id=name, version="1.0")
        for name in sorted(recipe.granted_capabilities)
    )


def request_envelope(recipe: IngressRecipe) -> RequestEnvelope:
    """Encode one ingress. Run, session and agent identity are not in here.

    `RequestMetadata` defines no field for them and sets
    `unevaluatedProperties: false`, so run lineage rides in the payload's
    provenance instead.
    """
    if recipe.operation is None:
        raise IngressRecipeError(f"{recipe.case_id} makes no Core call to encode")
    operation = CATALOGUE[recipe.operation]
    metadata = RequestMetadata(
        request_id=f"req.{recipe.case_id}",
        correlation_id=f"corr.{recipe.case_id}",
        trace_id=f"trace.{recipe.case_id}",
        api_version=API_VERSION,
        client=ClientIdentity(id="omnivia.workflow_run", version=SERVER_VERSION),
        scopes=operation.scope.required_scopes,
        purpose=recipe.purpose,
        required_capabilities=(
            CapabilityRequirement(
                id=operation.required_capability.id,
                minimum_version=operation.required_capability.minimum_version,
                required=True,
            ),
        ),
        workspace_id=recipe.requested_workspace_id,
        idempotency_key=(
            recipe.idempotency_key if operation.idempotency.required else None
        ),
    )
    payload: Mapping[str, Any] = (
        memory_payload(recipe).to_wire()
        if recipe.carries_memory_payload
        else dict(recipe.request_input)
    )
    return RequestEnvelope(operation=recipe.operation, metadata=metadata, input=payload)


def _response_metadata(recipe: IngressRecipe, audited: bool) -> ResponseMetadata:
    refs = _capability_refs(recipe)
    return ResponseMetadata(
        request_id=f"req.{recipe.case_id}",
        correlation_id=f"corr.{recipe.case_id}",
        version=VersionCapabilityEnvelope(
            api_version=API_VERSION,
            server_version=SERVER_VERSION,
            workspace_format_version=API_VERSION,
            compatibility=CompatibilityMetadata(
                selected_api_version=API_VERSION,
                selected_workspace_version=API_VERSION,
                supported_api_versions=VersionWindow(
                    minimum=API_VERSION, maximum=API_VERSION
                ),
                supported_workspace_versions=VersionWindow(
                    minimum=API_VERSION, maximum=API_VERSION
                ),
                status="compatible",
                upgrade_state=UpgradeState(value="none"),
                deprecations=(),
            ),
            capabilities=CapabilitySet(supported=refs, granted=refs, effective=refs),
        ),
        authority=GrantedAuthority(
            principal_id=recipe.principal_id,
            roles=("member",),
            capabilities=refs,
        ),
        audit_reference=f"audit.{recipe.case_id}" if audited else None,
    )


def _response_wire(
    recipe: IngressRecipe, decision: IngressDecision
) -> Mapping[str, Any]:
    metadata = _response_metadata(recipe, bool(decision.core_audit_event))
    if decision.error_code is None:
        return encode_response(SuccessResponseEnvelope(metadata=metadata, result={}))
    return encode_response(
        ErrorResponseEnvelope(
            metadata=metadata,
            error=ApiError(
                code=decision.error_code,
                message=f"{recipe.operation} refused",
                retry_class=retry_class_for(decision.error_code),
            ),
        )
    )


def execute_recipe(recipe: IngressRecipe) -> IngressObservation:
    """Derive the decision, then put it across the real adapter seam."""
    decision = derive_decision(recipe)
    if recipe.operation is None:
        return IngressObservation(
            case_id=recipe.case_id,
            principal_kind=recipe.principal_kind,
            core_operations=(),
            disposition=decision.disposition,
            error_code=decision.error_code,
            core_mutation=decision.core_mutation,
            canonical_mutation=decision.canonical_mutation,
            mutation_authority=decision.mutation_authority,
            core_audit_event=decision.core_audit_event,
        )
    envelope = request_envelope(recipe)
    request = encode_request(envelope)
    # A lost response is modelled by recording no exchange: the fake adapter
    # refuses an unrecorded request rather than improvising one, which is the
    # situation `RUN-V-15` describes.
    exchanges = (
        ()
        if decision.response_lost
        else ((f"req.{recipe.case_id}", _response_wire(recipe, decision)),)
    )
    adapter = InProcessFakeAdapter(exchanges)
    lineage = tuple(
        MappingProxyType(dict(source))
        for source in _wire_sources(request)
        if source.get("kind") == RUN_LINEAGE_SOURCE_KIND
    )
    metadata = request["metadata"]
    request_metadata: Mapping[str, Any] = (
        dict(metadata) if isinstance(metadata, Mapping) else {}
    )
    try:
        raw = adapter.call(request)
    except ContractSemanticError:
        if not decision.response_lost:
            raise
        return IngressObservation(
            case_id=recipe.case_id,
            principal_kind=recipe.principal_kind,
            core_operations=(recipe.operation,),
            disposition=decision.disposition,
            error_code=None,
            core_mutation=None,
            canonical_mutation=decision.canonical_mutation,
            mutation_authority=decision.mutation_authority,
            core_audit_event=None,
            reconciliation_branches=decision.branches,
            request_metadata=request_metadata,
            lineage_sources=lineage,
            response_observed=False,
            cancellation_raced=recipe.cancellation_raced,
        )
    response = decode_response(raw)
    error = response_error(response)
    observed_code = None if error is None else error.code
    if observed_code != decision.error_code:
        raise IngressRecipeError(
            f"{recipe.case_id}: the seam returned {observed_code!r},"
            f" not {decision.error_code!r}"
        )
    audited = response.metadata.audit_reference is not None
    if audited != bool(decision.core_audit_event):
        raise IngressRecipeError(
            f"{recipe.case_id}: the seam disagrees about the audit reference"
        )
    return IngressObservation(
        case_id=recipe.case_id,
        principal_kind=recipe.principal_kind,
        core_operations=(recipe.operation,),
        disposition=decision.disposition,
        error_code=observed_code,
        core_mutation=decision.core_mutation,
        canonical_mutation=decision.canonical_mutation,
        mutation_authority=decision.mutation_authority,
        core_audit_event=audited,
        request_metadata=request_metadata,
        lineage_sources=lineage,
        cancellation_raced=recipe.cancellation_raced,
    )


def _wire_sources(request: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    payload = request.get("input")
    if not isinstance(payload, Mapping):
        return ()
    sources = payload.get("sources")
    if not isinstance(sources, Sequence) or isinstance(sources, str | bytes):
        return ()
    return tuple(item for item in sources if isinstance(item, Mapping))


# --- the twenty-four recipes -------------------------------------------------
#
# Structured inputs only. No entry names a disposition, an error code or an
# expectation, and the order is the corpus's own.

CLAIM_ALPHA: Final[Mapping[str, Any]] = MappingProxyType(
    {"claim": "the alpha workspace deploy window is monthly"}
)
CLAIM_ALPHA_RESTATED: Final[Mapping[str, Any]] = MappingProxyType(
    {"claim": "deploys to the alpha workspace happen once a month"}
)
CLAIM_COMPACTED: Final[Mapping[str, Any]] = MappingProxyType(
    {"claim": "the alpha rollback budget is two hours"}
)
CLAIM_LINEAGE: Final[Mapping[str, Any]] = MappingProxyType(
    {"claim": "the alpha release train runs on Tuesdays"}
)
CLAIM_RACED: Final[Mapping[str, Any]] = MappingProxyType(
    {"claim": "the alpha incident channel is staffed overnight"}
)
CLAIM_CROSS_WORKSPACE: Final[Mapping[str, Any]] = MappingProxyType(
    {"claim": "the beta runbook applies to alpha"}
)
CLAIM_OVERSIZED: Final[Mapping[str, Any]] = MappingProxyType(
    {"claim": "the crawl produced 41k rows of dependency data"}
)
CLAIM_OVERLONG_LINEAGE: Final[Mapping[str, Any]] = MappingProxyType(
    {"claim": "the alpha cache warms in under a minute"}
)

_ALPHA_ATTEMPT: Final = KeyDerivation("run.a8-0001", "node.001", CLAIM_ALPHA)
_COMPACTED_ATTEMPT: Final = KeyDerivation("run.a8-0005", "node.041", CLAIM_COMPACTED)

_JOB_CANCEL_INPUT: Final[Mapping[str, Any]] = MappingProxyType(
    {"job_id": "job.a8-import-1", "reason_code": "run_operator_request"}
)
_IMPORT_INPUT: Final[Mapping[str, Any]] = MappingProxyType(
    {"source": {"kind": "runtime_result", "reference": "handle.a8-0007"}}
)
_MEMORY_GET_INPUT: Final[Mapping[str, Any]] = MappingProxyType(
    {"record_id": "rec.a8-0001"}
)
_JOB_GET_INPUT: Final[Mapping[str, Any]] = MappingProxyType(
    {"job_id": "job.a8-import-1"}
)
_CONTEXT_PACK_INPUT: Final[Mapping[str, Any]] = MappingProxyType(
    {"mode": "balanced", "query": "alpha deploy window"}
)


def _agent(
    case_id: str,
    run_id: str,
    task_node_id: str,
    session_id: str,
    **overrides: Any,
) -> IngressRecipe:
    overrides.setdefault("purpose", PURPOSE_CAPTURE)
    return IngressRecipe(
        case_id=case_id,
        run_id=run_id,
        task_node_id=task_node_id,
        session_id=session_id,
        principal_kind=PrincipalKind.AGENT,
        bound_workspace_id=WORKSPACE_ALPHA,
        **overrides,
    )


def _human(
    case_id: str,
    run_id: str,
    task_node_id: str,
    session_id: str,
    **overrides: Any,
) -> IngressRecipe:
    overrides.setdefault("purpose", PURPOSE_GOVERNANCE)
    return IngressRecipe(
        case_id=case_id,
        run_id=run_id,
        task_node_id=task_node_id,
        session_id=session_id,
        principal_kind=PrincipalKind.HUMAN,
        principal_id=HUMAN_PRINCIPAL,
        bound_workspace_id=WORKSPACE_ALPHA,
        **overrides,
    )


RECIPES: Final[tuple[IngressRecipe, ...]] = (
    _agent(
        "RUN-V-01",
        "run.a8-0001",
        "node.001",
        "session.a8-0001",
        operation="memory.create",
        requested_workspace_id=WORKSPACE_ALPHA,
        granted_capabilities=("memory.write",),
        request_input=CLAIM_ALPHA,
    ),
    _agent(
        "RUN-V-02",
        "run.a8-0001",
        "node.002",
        "session.a8-0001",
        operation="candidate.approve",
        requested_workspace_id=WORKSPACE_ALPHA,
        granted_capabilities=("memory.read", "memory.write"),
        request_input={"record_id": "rec.a8-0001", "rationale": {"reason_code": "ok"}},
    ),
    _agent(
        "RUN-V-03",
        "run.a8-0002",
        "node.011",
        "session.a8-0002",
        operation="record.supersede",
        requested_workspace_id=WORKSPACE_ALPHA,
        granted_capabilities=("memory.read", "memory.write"),
        request_input={"record_id": "rec.a8-0001", "reason_code": "stale"},
    ),
    _agent(
        "RUN-V-04",
        "run.a8-0002",
        "node.012",
        "session.a8-0002",
        operation="workspace.create",
        granted_capabilities=("memory.read", "memory.write"),
        request_input={"workspace_id": "ws.a8-scratch"},
    ),
    _agent("RUN-V-05", "run.a8-0003", "node.021", "session.a8-0003"),
    _agent(
        "RUN-V-06",
        "run.a8-0003",
        "node.022",
        "session.a8-0003",
        operation="knowledge.propose",
        requested_workspace_id=WORKSPACE_ALPHA,
        granted_capabilities=("memory.read", "memory.write"),
        request_input={"record_id": "rec.a8-0001"},
    ),
    _human(
        "RUN-V-07",
        "run.a8-0001",
        "node.001",
        "session.a8-0004",
        operation="candidate.approve",
        requested_workspace_id=WORKSPACE_ALPHA,
        granted_capabilities=("knowledge.govern", "memory.read"),
        request_input={"record_id": "rec.a8-0001", "rationale": {"reason_code": "ok"}},
    ),
    _agent(
        "RUN-V-08",
        "run.a8-0001",
        "node.001",
        "session.a8-0005",
        operation="memory.create",
        requested_workspace_id=WORKSPACE_ALPHA,
        granted_capabilities=("memory.write",),
        request_input=CLAIM_ALPHA,
        prior_attempts=(_ALPHA_ATTEMPT,),
    ),
    _agent(
        "RUN-V-09",
        "run.a8-0001",
        "node.001",
        "session.a8-0005",
        operation="memory.create",
        requested_workspace_id=WORKSPACE_ALPHA,
        granted_capabilities=("memory.write",),
        request_input=CLAIM_ALPHA_RESTATED,
        prior_attempts=(_ALPHA_ATTEMPT,),
        stale_key_derivation=_ALPHA_ATTEMPT,
    ),
    _agent(
        "RUN-V-10",
        "run.a8-0001",
        "node.replay",
        "session.a8-0006",
        replayed_operations=("memory.create",),
    ),
    _agent(
        "RUN-V-11",
        "run.a8-0004",
        "node.031",
        "session.a8-0007",
        operation="memory.create",
        requested_workspace_id=WORKSPACE_BETA,
        granted_capabilities=("memory.write",),
        request_input=CLAIM_ALPHA,
    ),
    _agent(
        "RUN-V-12",
        "run.a8-0004",
        "node.032",
        "session.a8-0007",
        operation="memory.create",
        requested_workspace_id=WORKSPACE_ALPHA,
        granted_capabilities=("memory.write",),
        request_input=CLAIM_CROSS_WORKSPACE,
        evidence=(
            EvidenceRecipe(
                kind="document",
                source_id="doc.a8-beta-runbook",
                workspace_id=WORKSPACE_BETA,
            ),
        ),
    ),
    _agent(
        "RUN-V-13",
        "run.a8-0005",
        "node.041",
        "session.a8-0008",
        operation="memory.create",
        requested_workspace_id=WORKSPACE_ALPHA,
        granted_capabilities=("memory.write",),
        request_input=CLAIM_COMPACTED,
        prior_attempts=(_COMPACTED_ATTEMPT,),
    ),
    _agent(
        "RUN-V-14",
        "run.a8-0005",
        "node.042",
        "session.a8-0008",
        operation="memory.create",
        requested_workspace_id=WORKSPACE_ALPHA,
        granted_capabilities=("memory.write",),
        request_input=CLAIM_LINEAGE,
    ),
    _agent(
        "RUN-V-15",
        "run.a8-0006",
        "node.051",
        "session.a8-0009",
        operation="memory.create",
        requested_workspace_id=WORKSPACE_ALPHA,
        granted_capabilities=("memory.write",),
        request_input=CLAIM_RACED,
        cancellation_raced=True,
    ),
    _agent(
        "RUN-V-16",
        "run.a8-0006",
        "node.052",
        "session.a8-0009",
        operation="job.cancel",
        requested_workspace_id=WORKSPACE_ALPHA,
        granted_capabilities=("job.control", "job.read"),
        request_input=_JOB_CANCEL_INPUT,
    ),
    _human(
        "RUN-V-17",
        "run.a8-0001",
        "node.001",
        "session.a8-0004",
        operation="memory.get",
        requested_workspace_id=WORKSPACE_ALPHA,
        granted_capabilities=("memory.read",),
        request_input=_MEMORY_GET_INPUT,
    ),
    _human(
        "RUN-V-18",
        "run.a8-0001",
        "node.001",
        "session.a8-0010",
        operation="memory.get",
        requested_workspace_id=WORKSPACE_ALPHA,
        granted_capabilities=("memory.read",),
        request_input=_MEMORY_GET_INPUT,
    ),
    _agent(
        "RUN-V-19",
        "run.a8-0007",
        "node.061",
        "session.a8-0011",
        operation="memory.create",
        requested_workspace_id=WORKSPACE_ALPHA,
        granted_capabilities=("memory.write",),
        request_input=CLAIM_OVERSIZED,
        evidence=(
            EvidenceRecipe(
                kind="runtime.handle",
                source_id="handle.a8-0007",
                core_resolvable=False,
            ),
        ),
    ),
    _agent(
        "RUN-V-20",
        "run.a8-0007",
        "node.062",
        "session.a8-0011",
        operation="import.start",
        requested_workspace_id=WORKSPACE_ALPHA,
        granted_capabilities=("ingestion.import", "job.read"),
        request_input=_IMPORT_INPUT,
    ),
    _human(
        "RUN-V-21",
        "run.a8-0007",
        "node.063",
        "session.a8-0012",
        operation="job.get",
        requested_workspace_id=WORKSPACE_ALPHA,
        granted_capabilities=("job.read",),
        request_input=_JOB_GET_INPUT,
    ),
    _agent(
        "RUN-V-22",
        "run.a8-0008",
        "node.071",
        "session.a8-0013",
        operation="memory.create",
        requested_workspace_id=WORKSPACE_ALPHA,
        granted_capabilities=("memory.write",),
        request_input=CLAIM_OVERLONG_LINEAGE,
        lineage_run_id=OVERLONG_RUN_ID,
    ),
    _agent(
        "RUN-V-23",
        "run.a8-0009",
        "node.081",
        "session.a8-0014",
        purpose=PURPOSE_RESEARCH,
        operation="context_pack.build",
        requested_workspace_id=WORKSPACE_ALPHA,
        granted_capabilities=("context_pack.build", "evidence.read", "memory.read"),
        request_input=_CONTEXT_PACK_INPUT,
    ),
    _agent("RUN-V-24", "run.a8-0009", "node.082", "session.a8-0014"),
)

RECIPES_BY_CASE: Final[Mapping[str, IngressRecipe]] = MappingProxyType(
    {recipe.case_id: recipe for recipe in RECIPES}
)


__all__ = [
    "AGENT_ID",
    "AGENT_PRINCIPAL",
    "HUMAN_PRINCIPAL",
    "OVERLONG_RUN_ID",
    "RECIPES",
    "RECIPES_BY_CASE",
    "WORKSPACE_ALPHA",
    "WORKSPACE_BETA",
    "EvidenceRecipe",
    "IngressDecision",
    "IngressObservation",
    "IngressRecipe",
    "IngressRecipeError",
    "KeyDerivation",
    "ObservedBranch",
    "content_digest",
    "derive_decision",
    "execute_recipe",
    "memory_payload",
    "reconcile",
    "request_envelope",
]
