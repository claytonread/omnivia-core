"""The `engineering.*` handlers (SPEC-CORE-ENGMEM-001, plans PR-D/PR-F).

All twelve engineering-memory operations are durable here and in
`handlers.continuity`: the continuity vertical (register/append/close/handoff),
the retrieval reads (search/expand) served from the governed record store, the
supersession edge table and the continuity checkpoint index, the non-persisted
context pack builder, the priority writes, the review attestations, the
trusted source record and the repository/checkout registration surface.

The pack builder (§12) is a non-persisting read: one frozen frontier, one
resolution instant, exact budget reconciliation with mandatory notices rendered
first and optional sections dropped lowest-priority first, and a self-verifying
`pack_id` — the canonical SHA-256 of the result after removing exactly the root
`pack_id` and the nested `reproducibility.artifact_checksum`. The v1 renderer's
pinned token counting is a whitespace split, recomputable from the rendering.

Retrieval security shape, inherited from the knowledge family and the plan:

1. payloads decode through the contract's own decoder; identity, workspace and
   purpose come from the authorised context, never from the payload;
2. the authorised frontier is frozen *before* any scoring: the candidate set is
   the workspace's engineering observations under the requested view, read at
   one resolution instant, and `rank_previews` sees nothing else — no corpus
   statistics and no restricted document reach the rank (§11.3). The ranker's
   own relevance signal is a stated occurrence count computed from the
   frontier's members and nothing else;
3. previews are bounded projections (≤480 code points and ≤2 KiB each, ≤64 KiB
   per response) stored beside each version by migration 0053. The governed
   views read only authorised identities, evidence links, stored digests and
   those projection rows: no `content_json` or `claim_json` is read to rank,
   filter, page, check `current_safe` applicability or render, so a full body is
   never hydrated on this path (§11.1, AC-033). A version the grant admits with
   no projection row is `projection_unavailable`, one with a stale row is
   `stale_projection`; neither falls back to the body. Exact reads and expansion
   are where a body is hydrated;
4. a hypothesis observation is never served under the `accepted` view, even
   after governance accepts it (§8.2);
5. continuations are the established MAC'd tokens, bound to the request
   digest, the frozen snapshot and the resolution instant; a changed binding,
   snapshot or epoch is an explicit restart (§11.4);
6. `applicability` reports only what is known. In the default `diagnostic`
   mode, with no assessment for the exact (record version, target snapshot),
   it is `not_evaluated`; with one, the stored status is re-assessed
   conservatively and never reported as `matched`, and the coverage block
   stays `unavailable` rather than implying freshness (§15.1);
7. `current_safe` (search and pack build) consults authoritative source
   coverage first: a target that is not a recorded snapshot inside its
   stream's contiguous validated coverage is refused with
   `dependency_unavailable` / `applicability_pending` before any frontier read
   or ranking, never downgraded. For covered targets the shared evaluator in
   `storage.engineering_source` then checks each admitted candidate's exact
   dependency set directly, before scoring, and only proven `matched` records
   are served. In both modes search reads the governed frontier through
   `storage.engineering_preview.read_authorized_previews`, which folds the
   effective caller's evidence-label grant from identities and links before it
   reads any projection row, so a denied version is never previewed;
   exact references (the expand anchor and its supersession endpoints, the
   priority and review targets) hydrate through
   `storage.memory.read_authorized_memory_snapshot`. The pack pages that same
   authorized identity frontier, ranks bounded projections, and uses the narrow
   metered hydrator only for selected versions and their authorized support;
8. `working_context` reads the continuity checkpoint index — reported
   accomplishments are labelled as continuity evidence, never as governed
   knowledge (§12.3). Search and the `resume` pack read only checkpoints of
   sessions the effective principal owns, filtered in SQL before matching,
   pagination, the snapshot digest, selection and rendering. There is no
   sharing grant, so working context is same-principal only.

`engineering.source.record` is the trusted source producer's write: it records
one immutable source event under its own `engineering:source` grant through the
same fenced, audited, idempotent mutation seam as every other write here.

`engineering.repository.register` is the ratified production registration path
(spec §16.3) onto `storage.repository_identity`: an explicitly authorized local
operator's own write, under its own `engineering:repository` grant, binding one
exact installation-local checkout to one logical repository identity through
that same fenced, audited, idempotent seam.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Final

from omnivia_core.contracts.v1 import (
    DEFAULT_RETRY_CLASSIFICATION,
    ENGINEERING_COUNTING_MODE_BYTE_ONLY,
    ENGINEERING_COUNTING_MODE_EXACT_TOKENS,
    ERROR_CODE_AUTHORIZATION_DENIED,
    ERROR_CODE_CONFLICT,
    ERROR_CODE_CONTEXT_BUDGET_INSUFFICIENT,
    ERROR_CODE_DEPENDENCY_UNAVAILABLE,
    ERROR_CODE_INVALID_REQUEST,
    ERROR_CODE_MUTATION_PRECONDITION_FAILED,
    ERROR_CODE_NOT_FOUND,
    ERROR_CODE_PROJECTION_UNAVAILABLE,
    ERROR_CODE_SIZE_LIMIT_EXCEEDED,
    ERROR_CODE_STALE_PROJECTION,
    ERROR_CODE_TOKEN_LIMIT_EXCEEDED,
    ERROR_CODE_TOKENIZER_UNAVAILABLE,
    GOVERNANCE_STATE_ACCEPTED,
    RETRY_CLASS_RETRYABLE_AFTER_DELAY,
    ContextPrioritySetInput,
    ContextPrioritySetResult,
    ContractDecodeError,
    ContractSemanticError,
    EngineeringExpandInput,
    EngineeringRepositoryRegisterInput,
    EngineeringRepositoryRegisterResult,
    EngineeringReviewRecordInput,
    EngineeringReviewRecordResult,
    EngineeringSearchInput,
    EngineeringSourceCaptureCommitInput,
    EngineeringSourceCaptureCommitResult,
    EngineeringSourceRecordInput,
    EngineeringSourceRecordResult,
    decode_engineering_context_build_input,
    idempotency_equivalence,
    is_identifier,
    to_canonical_json,
)
from omnivia_core_runtime.ownership.identity import Clock, SystemClock
from omnivia_core_runtime.service import source_capture
from omnivia_core_runtime.service.authorization import (
    AuthenticatedSession,
    ServiceBinding,
)
from omnivia_core_runtime.service.engineering_pack import (
    WORKING_CONTEXT_SHARE_DIVISOR,
    BuildContext,
    MandatoryContextTooLarge,
    PackRecord,
    WorkingItem,
    build_pack,
    build_pack_byte_only,
)
from omnivia_core_runtime.service.mutation import (
    MutationIdempotencyConflict,
    MutationPreconditionFailed,
    MutationSettlementContext,
    execute_mutation,
    issue_mutation_grant,
)
from omnivia_core_runtime.service.operations import (
    AuditedOperationResult,
    OperationContext,
    OperationError,
    application_refusal,
)
from omnivia_core_runtime.service.pagination import (
    PROCESS_CONTINUATION_TOKENS,
    token_digest,
)
from omnivia_core_runtime.storage import continuity as continuity_storage
from omnivia_core_runtime.storage import engineering_applicability as app_storage
from omnivia_core_runtime.storage import (
    engineering_conflicts,
    repository_identity,
)
from omnivia_core_runtime.storage import (
    engineering_invalidation as invalidation_storage,
)
from omnivia_core_runtime.storage import engineering_source as source_storage
from omnivia_core_runtime.storage.engineering_preview import (
    PREVIEW_MAX_CODEPOINTS,
    PROJECTION_VERSION,
    PreviewCandidate,
    PreviewProjectionStale,
    PreviewProjectionUnavailable,
    preview_search_text,
    rank_previews,
    read_authorized_previews,
    read_previews_for_frontier,
)
from omnivia_core_runtime.storage.governed import (
    GovernedPayloadComponents,
    hydrate_authorized_governed_record_values,
    plan_authorized_governed_payload,
    read_governed_supersessions,
)
from omnivia_core_runtime.storage.memory import (
    AUTHORIZED_FRONTIER_PAGE_SIZE,
    IdentifierAllocator,
    random_identifier,
    read_authorized_memory_frontier,
    read_authorized_memory_snapshot,
    read_memory_record_id_page,
    read_snapshot,
)
from omnivia_core_runtime.storage.payload_budget import (
    PayloadBudgetExceeded,
    PayloadLengthMismatch,
    PayloadReadBudget,
)
from omnivia_core_runtime.storage.retrieval import (
    EvidenceLabelGrant,
    local_owner_label_grant,
    normalize_query,
)

_MESSAGE_INVALID: Final = "the request payload is not valid for this engineering operation"
_MESSAGE_NO_STORAGE: Final = (
    "this service instance is not serving authoritative storage"
)
_MESSAGE_NOT_FOUND: Final = "the requested engineering record was not found"
_MESSAGE_PROJECTION_UNAVAILABLE: Final = (
    "the engineering preview projection has no row for a version this search admits"
)
_MESSAGE_STALE_PROJECTION: Final = (
    "the engineering preview projection is not current for a version this search admits"
)
_MESSAGE_PRECONDITION: Final = (
    "the engineering target moved under this request; re-read and re-decide"
)

#: The engineering domain this retrieval serves: observations ride the frozen
#: catalogue's finding/risk/decision types under this domain (§22.1).
OBSERVATION_DOMAIN: Final = "engineering.codebase"

#: The view→governed-resolver mapping (§11.2). `working_context` is absent
#: deliberately: it reads the continuity checkpoint index, not governed records.
_GOVERNED_VIEWS: Final[dict[str, str]] = {
    "accepted": "current_canonical",
    "candidates": "candidates",
    "history": "history",
}
_ENGINEERING_VIEWS: Final[frozenset[str]] = frozenset(
    {"accepted", "candidates", "history", "working_context"}
)

_TOKEN_KEYS: Final[frozenset[str]] = frozenset({"b", "o", "s", "t", "v"})

#: The default and hard-maximum page sizes for engineering search (§11.1).
SEARCH_DEFAULT_LIMIT: Final = 20
SEARCH_MAX_LIMIT: Final = 100

#: The hard maximum of one search response: 64 KiB of canonical JSON (§11.1). A
#: page that would exceed it is cut short and continues, never truncated
#: mid-preview. `_RESPONSE_RESERVE` is held back for what surrounds the previews:
#: the page token (about 230 bytes), the coverage block, the JSON around them and
#: the response envelope (about 750 bytes), so the whole frame stays inside the cap.
RESPONSE_MAX_BYTES: Final = 65536
_RESPONSE_RESERVE: Final = 2048

_MESSAGE_BUDGET: Final = (
    "the minimum safe engineering context does not fit the effective budget"
)
_MESSAGE_TOKENIZER_UNAVAILABLE: Final = (
    "the requested exact tokenizer is not available in this service"
)

#: Server hard budget ceilings (§12.4). A supplied budget above its ceiling, zero,
#: negative or non-integer is refused (`invalid_request`) rather than silently
#: clamped; an absent field resolves to its default.
BUDGET_CEILING_TOKENS: Final = 16000
BUDGET_CEILING_BYTES: Final = 65536
BUDGET_CEILING_HYDRATIONS: Final = 32
BUDGET_CEILING_EVIDENCE_BYTES: Final = 1_048_576
BUDGET_CEILING_AUTHORIZED_CANDIDATES: Final = 10_000
BUDGET_DEFAULT_TOKENS: Final = 4000
BUDGET_DEFAULT_BYTES: Final = 16384
BUDGET_DEFAULT_HYDRATIONS: Final = 8
BUDGET_DEFAULT_EVIDENCE_BYTES: Final = 262_144
BUDGET_DEFAULT_AUTHORIZED_CANDIDATES: Final = 2_000
_MESSAGE_BUDGET_INVALID: Final = (
    "the requested budget is not a positive integer at or below its server ceiling"
)
_MESSAGE_HYDRATION_BOUND: Final = (
    "the admitted engineering frontier exceeds the effective hydration budget"
)
_MESSAGE_SOURCE_READ_BOUND: Final = (
    "the selected engineering payloads exceed the effective evidence byte budget"
)
_MESSAGE_AUTHORIZED_CANDIDATE_BOUND: Final = (
    "the authorized engineering frontier exceeds its bounded candidate budget"
)
_MESSAGE_PAYLOAD_INVALID: Final = (
    "an engineering source payload failed its stored byte-length check"
)

CONTEXT_BUILD_SECTION_CAP: Final = 24
CONTEXT_BUILD_ABSOLUTE_SECTION_CAP: Final = 64
CONTEXT_BUILD_CHECKPOINT_CAP: Final = 5
CONTEXT_BUILD_SELECTION_PROFILE: Final = "eng-preview-select-3"


@dataclass(frozen=True, slots=True)
class EngineeringBudgetPolicy:
    """Server-owned limits for one named engineering retrieval profile."""

    model_tokens: int
    model_bytes: int
    hydrations: int
    evidence_bytes: int
    authorized_candidates: int


_DEFAULT_ENGINEERING_BUDGET_POLICY: Final = EngineeringBudgetPolicy(
    model_tokens=BUDGET_DEFAULT_TOKENS,
    model_bytes=BUDGET_DEFAULT_BYTES,
    hydrations=BUDGET_DEFAULT_HYDRATIONS,
    evidence_bytes=BUDGET_DEFAULT_EVIDENCE_BYTES,
    authorized_candidates=BUDGET_DEFAULT_AUTHORIZED_CANDIDATES,
)
ENGINEERING_PROFILE_BUDGETS: Final[Mapping[str, EngineeringBudgetPolicy]] = (
    MappingProxyType(
        {
            profile: _DEFAULT_ENGINEERING_BUDGET_POLICY
            for profile in ("investigate", "implement", "review", "resume")
        }
    )
)

_MESSAGE_PRIORITY: Final = (
    "context priority ships contracts first; the preference store lands in a "
    "later engineering-memory package"
)
_MESSAGE_REVIEW: Final = (
    "engineering review recording ships contracts first; the attestation "
    "producer lands in a later engineering-memory package"
)

#: The compatibility-preserving refusal signal of `current_safe` (§15): the
#: existing `dependency_unavailable` code with this fixed message, not a newly
#: ratified error code. It is raised before any frontier read or ranking and is
#: never answered by downgrading to `diagnostic`.
APPLICABILITY_PENDING: Final = "applicability_pending"
_APPLICABILITY_MODES: Final[frozenset[str]] = frozenset({"diagnostic", "current_safe"})
#: The bounded direct-check budget of one `current_safe` read: candidates beyond
#: it are refused as a size limit rather than silently left unevaluated.
CURRENT_SAFE_CANDIDATE_CAP: Final = 1000
#: The bounded target count of one `current_safe` pack build, enforced before any
#: coverage read so candidate-by-target work and manifest loading stay bounded.
CURRENT_SAFE_TARGET_CAP: Final = 16
EVALUATOR_VERSION: Final = "eng-applicability-1"
_MESSAGE_CURRENT_SAFE_BOUND: Final = (
    "the current_safe frontier exceeds its bounded applicability check budget"
)
_MESSAGE_SOURCE_INVALID: Final = (
    "the source record is outside its bounded, validated shape"
)
_MESSAGE_SOURCE_TOO_LARGE: Final = (
    "the source manifest or pending window exceeds this workspace's bound"
)
_MESSAGE_SOURCE_CONFLICT: Final = (
    "the source record conflicts with an immutable source identity or binding"
)
_MESSAGE_SOURCE_FOREIGN: Final = "the source stream is owned by another principal"
_MESSAGE_CAPTURE_NOT_FOUND: Final = "the requested sealed source capture was not found"
_MESSAGE_CAPTURE_FOREIGN: Final = (
    "the sealed source capture is not owned by this authenticated installation"
)
_MESSAGE_CAPTURE_PRECONDITION: Final = (
    "the sealed source capture does not match the expected manifest digest"
)
_CAPTURE_COMMIT_KEYS: Final[frozenset[str]] = frozenset(
    {
        "repository_id",
        "stream_id",
        "sequence",
        "predecessor",
        "snapshot_id",
        "expected_manifest_digest",
    }
)
_MESSAGE_REPOSITORY_INVALID: Final = (
    "the repository registration request is outside its bounded, validated shape"
)
_MESSAGE_REPOSITORY_CONFLICT: Final = (
    "the repository id is already registered with different identity metadata"
)
#: `EngineeringRepositoryRegisterInput` declares `unevaluatedProperties: false` and its
#: own docstring says unknown keys are refused, but the generated `from_wire` ignores
#: any it doesn't recognise (so a newer peer's additive minor release still decodes) --
#: so the raw payload's keys are checked against this set directly, before decoding
#: proceeds, rather than trusting the decoder to reject them.
_REPOSITORY_REGISTER_KEYS: Final[frozenset[str]] = frozenset(
    {"repository_id", "display_name", "provider_hint", "checkout_root"}
)


def _applicability_pending() -> OperationError:
    return application_refusal(ERROR_CODE_DEPENDENCY_UNAVAILABLE, APPLICABILITY_PENDING)


def _payload_read_refusal(
    error: PayloadBudgetExceeded | PayloadLengthMismatch,
) -> OperationError:
    if isinstance(error, PayloadBudgetExceeded):
        return application_refusal(
            ERROR_CODE_SIZE_LIMIT_EXCEEDED, _MESSAGE_SOURCE_READ_BOUND
        )
    return application_refusal(
        ERROR_CODE_DEPENDENCY_UNAVAILABLE, _MESSAGE_PAYLOAD_INVALID
    )


def _governed_payload_components(
    plan: GovernedPayloadComponents,
) -> tuple[tuple[str, str, int], ...]:
    """Stable identities and exact bytes for one metadata-only record plan."""
    return tuple(
        (kind, identity, byte_length)
        for kind, lengths in (
            ("content", plan.content_byte_lengths),
            ("rationale", plan.transition_rationale_byte_lengths),
            ("claim", plan.claim_byte_lengths),
        )
        for identity, byte_length in sorted(lengths.items())
    )


def _checkpoint_working_upper_bound(
    metadata: continuity_storage.CheckpointMetadata,
) -> int:
    """A pre-read upper bound for one checkpoint's model-facing UTF-8 bytes.

    The stored JSON is at least as large as the objective/unresolved strings it
    contains. Add every renderer-owned label and separator that can surround
    those strings, so admission never needs to fetch a checkpoint merely to
    discover that its working-context section cannot fit.
    """
    wrapper = (
        "\n\n[working_context] [uncited checkpoint "
        f"{metadata.checkpoint_id}#{metadata.sequence}]  Unresolved: "
    )
    return metadata.payload_byte_length + len(wrapper.encode("utf-8"))


def _proven_version(
    connection: Any,
    workspace_id: str,
    record_id: str,
    version: str,
    evidence_available: bool,
    targets: list[source_storage.CoveredSnapshot],
    payload_budget: PayloadReadBudget | None = None,
    snapshot_cache: dict[
        tuple[str, str, str | None], source_storage.CoveredSnapshot | None
    ] | None = None,
) -> bool:
    """Whether the evaluator proves `matched` for this exact version at every target.

    Evidence counts only as the version's own resolved evidence: a proposal saved
    with `evidence_disposition` other than `available`, or with no source, stays
    unqualified however well its digests line up. The check names an exact version
    and reads no content, so it needs no hydrated record.
    """
    return all(
        source_storage.evaluate_applicability(
            connection,
            workspace_id=workspace_id,
            record_id=record_id,
            version=version,
            evidence_available=evidence_available,
            target=target,
            payload_budget=payload_budget,
            snapshot_cache=snapshot_cache,
        )
        == "matched"
        for target in targets
    )


def _proven_matched(
    connection: Any,
    workspace_id: str,
    record: Any,
    targets: list[source_storage.CoveredSnapshot],
) -> bool:
    """`_proven_version` for a hydrated record, as the pack builder holds one."""
    provenance = record.provenance
    return _proven_version(
        connection,
        workspace_id,
        provenance.identity.record_id,
        provenance.identity.version,
        provenance.evidence_disposition == "available" and bool(provenance.sources),
        targets,
    )


def _as_result(outcome: Any) -> Mapping[str, Any] | AuditedOperationResult:
    if isinstance(outcome, Mapping):
        return outcome
    if isinstance(outcome, AuditedOperationResult):
        return outcome
    # A MutationOutcome from the coordinator: its `.result` is the answer.
    from typing import cast

    return cast(Mapping[str, Any], outcome.result)


def _bounded(value: str, limit: int = PREVIEW_MAX_CODEPOINTS) -> tuple[str, bool]:
    """One preview rendering: bounded text plus its honest truncation flag."""
    if len(value) <= limit:
        return value, False
    return value[:limit], True


def _render_preview(candidate: PreviewCandidate) -> dict[str, Any]:
    """One admitted version as a search preview, from its projection row alone.

    The title and preview text are the projection's bounded fields; nothing is
    read from, or cut out of, the version's body. An optional field the projection
    does not hold is absent rather than invented.
    """
    preview: dict[str, Any] = {
        "record_id": candidate.record_id,
        "version": candidate.version,
        "title": candidate.title,
        "preview": candidate.preview,
        "truncated": candidate.truncated,
        "governance_state": candidate.governance_state,
        "applicability": "not_evaluated",
        "evidence_available": candidate.evidence_available,
    }
    for key, value in (
        ("observation_kind", candidate.observation_kind),
        ("assertion_basis", candidate.assertion_basis),
        ("topic_key", candidate.topic_key),
        ("repository_id", candidate.repository_id),
        ("snapshot_id", candidate.snapshot_id),
    ):
        if value is not None:
            preview[key] = value
    return preview


def _within_response_cap(previews: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The leading previews of a page that fit one response, and always at least one.

    A preview is bounded by itself (a title of at most 200 and a text of at most 480
    code points), so one always fits; the cap is what bounds a page of many.
    """
    budget = RESPONSE_MAX_BYTES - _RESPONSE_RESERVE
    kept: list[dict[str, Any]] = []
    for preview in previews:
        cost = len(to_canonical_json(preview).encode("utf-8")) + 1
        if kept and cost > budget:
            break
        budget -= cost
        kept.append(preview)
    return kept


class EngineeringHandlers:
    """The engineering retrieval reads, plus the three still-honest refusals."""

    def __init__(
        self,
        service: Any,
        session: AuthenticatedSession | None = None,
        binding: ServiceBinding | None = None,
        clock: Clock | None = None,
        allocate_identifier: IdentifierAllocator = random_identifier,
    ) -> None:
        self.service = service
        self._issued_session = session
        self._issued_binding = binding
        self.clock = SystemClock() if clock is None else clock
        self.allocate_identifier = allocate_identifier

    def _session(self) -> AuthenticatedSession:
        if self._issued_session is None:
            raise OperationError("internal_non_recoverable", _MESSAGE_NO_STORAGE)
        return self._issued_session

    def _binding(self) -> ServiceBinding:
        if self._issued_binding is None:
            raise OperationError("internal_non_recoverable", _MESSAGE_NO_STORAGE)
        return self._issued_binding

    def _connection(self) -> Any:
        connection = getattr(self.service, "connection", None)
        if connection is None:
            raise OperationError("internal_non_recoverable", _MESSAGE_NO_STORAGE)
        return connection

    def _label_grant(self, context: OperationContext) -> EvidenceLabelGrant:
        """The EFFECTIVE caller's evidence-label grant.

        It is the grant of `context.principal`, never of the principal this
        owner-composed handler was issued for: a session dispatch runs it as another
        principal. The granted workspace is the server binding's, and a binding with
        no granted workspace grants no evidence label.
        """
        granted = self._binding().workspace_id
        return local_owner_label_grant(
            principal_id=context.principal,
            workspace_id=context.workspace_id,
            granted_workspace="" if granted is None else granted,
        )

    def _effective_budget(self, budget: Any) -> tuple[int, int, int, int, int]:
        """The five effective budget fields, each validated before any read.

        A supplied field must be a positive, non-bool integer at or below its
        server ceiling; anything else -- zero, negative, non-integer, a bool
        or one that exceeds its ceiling -- is refused outright, never silently
        clamped. An absent field resolves to its default.
        """

        def resolve(value: Any, default: int, ceiling: int) -> int:
            if value is None:
                return default
            if type(value) is not int or value <= 0 or value > ceiling:
                raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_BUDGET_INVALID)
            return value

        if budget is None:
            return (
                BUDGET_DEFAULT_TOKENS,
                BUDGET_DEFAULT_BYTES,
                BUDGET_DEFAULT_HYDRATIONS,
                BUDGET_DEFAULT_EVIDENCE_BYTES,
                BUDGET_DEFAULT_AUTHORIZED_CANDIDATES,
            )
        return (
            resolve(budget.model_tokens, BUDGET_DEFAULT_TOKENS, BUDGET_CEILING_TOKENS),
            resolve(budget.model_bytes, BUDGET_DEFAULT_BYTES, BUDGET_CEILING_BYTES),
            resolve(
                budget.hydrations, BUDGET_DEFAULT_HYDRATIONS, BUDGET_CEILING_HYDRATIONS
            ),
            resolve(
                budget.evidence_bytes,
                BUDGET_DEFAULT_EVIDENCE_BYTES,
                BUDGET_CEILING_EVIDENCE_BYTES,
            ),
            resolve(
                budget.authorized_candidates,
                BUDGET_DEFAULT_AUTHORIZED_CANDIDATES,
                BUDGET_CEILING_AUTHORIZED_CANDIDATES,
            ),
        )

    def _authorized_values(
        self,
        connection: Any,
        context: OperationContext,
        *,
        resolution_instant_us: int,
        view: str,
    ) -> tuple[Any, ...]:
        """The governed frontier, hydrated: identities and evidence-label grants are
        resolved first and only admitted versions are hydrated, so a denied version
        never reaches applicability, scoring, the candidate cap or omissions.

        Exact references read here; search and pack selection never do, because
        they rank bounded previews before any selected pack hydration.
        """
        return read_authorized_memory_snapshot(
            connection,
            workspace_id=context.workspace_id,
            resolution_instant_us=resolution_instant_us,
            view=view,
            label_grant=self._label_grant(context),
        ).values

    def _preview_candidates(
        self,
        connection: Any,
        context: OperationContext,
        *,
        resolution_instant_us: int,
        view: str,
    ) -> tuple[tuple[PreviewCandidate, ...], str]:
        """The engineering observations the effective caller's grant admits, as
        bounded previews, or the projection refusal that says why none can be served.

        The frontier is frozen from identities and evidence links first; only the
        admitted versions' projection rows are then read. A version with no row is
        `projection_unavailable` and one with a stale row is `stale_projection`, both
        retryable, and neither is answered from the stored body.
        """
        refused: str | None = None
        try:
            return read_authorized_previews(
                connection,
                workspace_id=context.workspace_id,
                resolution_instant_us=resolution_instant_us,
                view=view,
                label_grant=self._label_grant(context),
            )
        except PreviewProjectionUnavailable:
            refused = ERROR_CODE_PROJECTION_UNAVAILABLE
        except PreviewProjectionStale:
            refused = ERROR_CODE_STALE_PROJECTION
        # Raised after the handlers end, so the storage error's text is never
        # chained to the refusal (the tree's sentinel-then-raise convention).
        if refused == ERROR_CODE_STALE_PROJECTION:
            raise OperationError(
                ERROR_CODE_STALE_PROJECTION,
                _MESSAGE_STALE_PROJECTION,
                retry_class=RETRY_CLASS_RETRYABLE_AFTER_DELAY,
            )
        raise OperationError(
            ERROR_CODE_PROJECTION_UNAVAILABLE,
            _MESSAGE_PROJECTION_UNAVAILABLE,
            retry_class=RETRY_CLASS_RETRYABLE_AFTER_DELAY,
        )

    def _previews_for_frontier(
        self,
        connection: Any,
        context: OperationContext,
        frontier: Any,
    ) -> tuple[PreviewCandidate, ...]:
        """Map one frozen frontier to its bounded projections in the same snapshot."""
        try:
            return read_previews_for_frontier(
                connection,
                workspace_id=context.workspace_id,
                frontier=frontier,
            )
        except PreviewProjectionUnavailable as error:
            raise OperationError(
                ERROR_CODE_PROJECTION_UNAVAILABLE,
                _MESSAGE_PROJECTION_UNAVAILABLE,
                retry_class=RETRY_CLASS_RETRYABLE_AFTER_DELAY,
            ) from error
        except PreviewProjectionStale as error:
            raise OperationError(
                ERROR_CODE_STALE_PROJECTION,
                _MESSAGE_STALE_PROJECTION,
                retry_class=RETRY_CLASS_RETRYABLE_AFTER_DELAY,
            ) from error

    def _timestamp_us(self, value: str) -> int:
        import datetime as _dt

        parsed = _dt.datetime.fromisoformat(value)
        return int(parsed.timestamp() * 1_000_000)

    def _visible_records(
        self,
        connection: Any,
        context: OperationContext,
        *,
        resolution_instant_us: int,
    ) -> dict[tuple[str, str], Any]:
        """Every governed version the effective caller's grant admits under any
        governed view, keyed by exact (record id, version)."""
        visible: dict[tuple[str, str], Any] = {}
        for governed_view in ("current_canonical", "candidates", "history"):
            for value in self._authorized_values(
                connection,
                context,
                resolution_instant_us=resolution_instant_us,
                view=governed_view,
            ):
                identity = value.record.provenance.identity
                visible[(identity.record_id, identity.version)] = value.record
        return visible

    def _require_visible_version(
        self,
        connection: Any,
        context: OperationContext,
        *,
        record_id: str,
        version: str,
    ) -> str | None:
        """The exact record version must be visible; returns its claimed repository.

        A priority or a review names an exact visible target: a reference that
        resolves under no governed view for the effective caller's evidence grant
        is `not_found` -- hidden and nonexistent alike, naming neither. The claimed
        repository comes from the record's own applicability, so a review can be
        assessed against the registry without trusting the caller.
        """
        record = self._visible_records(
            connection, context, resolution_instant_us=time.time_ns() // 1000
        ).get((record_id, version))
        if record is None:
            raise OperationError(ERROR_CODE_NOT_FOUND, _MESSAGE_NOT_FOUND)
        content = record.content
        if isinstance(content, Mapping):
            applicability = content.get("applicability")
            if isinstance(applicability, Mapping):
                claimed = applicability.get("repository_id")
                if isinstance(claimed, str) and claimed:
                    return claimed
        return None

    def _execute(
        self,
        context: OperationContext,
        connection: Any,
        identity: Any,
        guard: Any,
        equivalence: Any,
        mutate: Any,
        valid_result: Any,
        precondition: Any = None,
    ) -> Any:
        grant = issue_mutation_grant(
            context.authorization,
            session=self._session(),
            binding=self._binding(),
            guard=guard,
            equivalence=equivalence,
            clock=self.clock,
        )
        try:
            return execute_mutation(
                connection,
                identity,
                grant=grant,
                context=context.authorization,
                equivalence=equivalence,
                precondition=precondition,
                mutate=mutate,
                validate_result=valid_result,
                clock=self.clock,
                allocate_identifier=self.allocate_identifier,
            )
        except MutationIdempotencyConflict as error:
            raise OperationError(
                error.code, error.message, retry_class=error.retry_class
            ) from error
        except source_storage.SourceStreamForeignPrincipal as error:
            raise application_refusal(
                ERROR_CODE_AUTHORIZATION_DENIED, _MESSAGE_SOURCE_FOREIGN
            ) from error
        except source_storage.CapturedSourceUnauthorized as error:
            raise application_refusal(
                ERROR_CODE_AUTHORIZATION_DENIED, _MESSAGE_CAPTURE_FOREIGN
            ) from error
        except source_storage.CapturedSourceNotFound as error:
            raise application_refusal(
                ERROR_CODE_NOT_FOUND, _MESSAGE_CAPTURE_NOT_FOUND
            ) from error
        except source_storage.CapturedSourcePreconditionFailed as error:
            raise application_refusal(
                ERROR_CODE_MUTATION_PRECONDITION_FAILED,
                _MESSAGE_CAPTURE_PRECONDITION,
            ) from error
        except source_storage.SourceConflict as error:
            raise application_refusal(
                ERROR_CODE_CONFLICT, _MESSAGE_SOURCE_CONFLICT
            ) from error
        except repository_identity.RepositoryIdentityConflict as error:
            raise application_refusal(
                ERROR_CODE_CONFLICT, _MESSAGE_REPOSITORY_CONFLICT
            ) from error
        except source_storage.SourceWindowExceeded as error:
            raise application_refusal(
                ERROR_CODE_SIZE_LIMIT_EXCEEDED, _MESSAGE_SOURCE_TOO_LARGE
            ) from error
        except (
            app_storage.RecordVersionNotFound,
            app_storage.AssessmentPreconditionFailed,
        ) as error:
            code = (
                ERROR_CODE_NOT_FOUND
                if isinstance(error, app_storage.RecordVersionNotFound)
                else ERROR_CODE_MUTATION_PRECONDITION_FAILED
            )
            raise OperationError(
                code,
                _MESSAGE_INVALID if code == ERROR_CODE_NOT_FOUND else _MESSAGE_PRECONDITION,
                retry_class=DEFAULT_RETRY_CLASSIFICATION[code],
            ) from error
        except MutationPreconditionFailed as error:
            raise OperationError(
                ERROR_CODE_MUTATION_PRECONDITION_FAILED,
                _MESSAGE_PRECONDITION,
                retry_class=DEFAULT_RETRY_CLASSIFICATION[
                    ERROR_CODE_MUTATION_PRECONDITION_FAILED
                ],
            ) from error

    # --- engineering.search ------------------------------------------------------

    def engineering_search(
        self, context: OperationContext
    ) -> Mapping[str, Any] | AuditedOperationResult:
        try:
            request = EngineeringSearchInput.from_wire(context.request.input)
        except (ContractDecodeError, ContractSemanticError) as error:
            raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID) from error
        connection = self._connection()
        limit = SEARCH_DEFAULT_LIMIT
        if request.limit is not None:
            # The contract states 100 as the hard maximum of one page.
            limit = min(request.limit, SEARCH_MAX_LIMIT)
        view = request.view or "accepted"
        mode = request.applicability_mode or "diagnostic"
        if view not in _ENGINEERING_VIEWS or mode not in _APPLICABILITY_MODES:
            raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID)
        target: source_storage.CoveredSnapshot | None = None
        if mode == "current_safe":
            if view not in ("accepted", "candidates") or request.repository_target is None:
                raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID)
            # Authoritative coverage first: an uncovered target is refused before
            # the frontier is read or anything is ranked.
            target = source_storage.covered_snapshot(
                connection,
                workspace_id=context.workspace_id,
                snapshot_id=request.repository_target.snapshot_id,
                repository_id=request.repository_target.repository_id,
            )
            if target is None:
                raise _applicability_pending()
        binding = request.to_wire()
        binding.pop("page", None)
        binding_digest = token_digest(
            {
                "principal": context.principal,
                "workspace": context.workspace_id,
                "operation": "engineering.search",
                "api_version": context.request.metadata.api_version,
                "authority": (
                    None if context.authority is None else context.authority.to_wire()
                ),
                "scopes": None if context.scopes is None else list(context.scopes),
                "purpose": context.purpose,
                "granted_operations": sorted(context.granted_operations),
                "input": binding,
                "limit": limit,
                "view": view,
            }
        )
        supplied: Mapping[str, Any] | None = None
        start = 0
        if request.page is not None:
            token = request.page.continuation_token
            assert token is not None
            try:
                supplied = PROCESS_CONTINUATION_TOKENS.decode(token)
            except (ValueError, ContractDecodeError, ContractSemanticError):
                pass
            if (
                supplied is None
                or set(supplied) != _TOKEN_KEYS
                or supplied.get("v") != 1
                or supplied.get("b") != binding_digest
            ):
                raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID)
            instant = supplied.get("t")
            if type(instant) is not int or instant <= 0:
                raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID)
            resolved_at_us = instant
        else:
            # The canonical resolution instant: one clock read, before anything
            # is resolved, and the only one on this path (as in knowledge.py).
            resolved_at_us = time.time_ns() // 1000

        if view == "working_context":
            previews, total, snapshot_digest, start = self._working_context_previews(
                connection,
                context,
                query=request.query,
                limit=limit,
                supplied=supplied,
            )
        else:
            # Both modes read only versions the effective caller's evidence grant
            # admits, and only their bounded projection rows: a denied version
            # never reaches scoring, previews, totals or the continuation's
            # snapshot digest, and no version's body is read at any point.
            admitted, authorization_frontier_digest = self._preview_candidates(
                connection,
                context,
                resolution_instant_us=resolved_at_us,
                view=_GOVERNED_VIEWS[view],
            )
            eligible: list[PreviewCandidate] = []
            evaluated = 0
            needle = normalize_query(request.query) if target is not None else ""
            for candidate in admitted:
                if view == "accepted" and candidate.assertion_basis == "hypothesis":
                    # §8.2: a hypothesis stays marked and is excluded from
                    # accepted-facts selection even after governance accepts it.
                    continue
                if target is not None:
                    # current_safe: the cap and the bounded direct check are spent
                    # only by candidates `rank_previews` could ever match -- an
                    # empty query matches nothing (its own rule), and a candidate
                    # whose preview text lacks the query is never a match, so
                    # neither reaches the evaluator or the cap.
                    if not needle or needle not in preview_search_text(candidate):
                        continue
                    evaluated += 1
                    if evaluated > CURRENT_SAFE_CANDIDATE_CAP:
                        raise application_refusal(
                            ERROR_CODE_SIZE_LIMIT_EXCEEDED, _MESSAGE_CURRENT_SAFE_BOUND
                        )
                    if not _proven_version(
                        connection,
                        context.workspace_id,
                        candidate.record_id,
                        candidate.version,
                        candidate.evidence_disposition == "available"
                        and candidate.evidence_available,
                        [target],
                    ):
                        continue
                elif (
                    request.repository_target is not None
                    and candidate.repository_id != request.repository_target.repository_id
                ):
                    continue
                eligible.append(candidate)
            ordered = rank_previews(eligible, request.query)
            preferred = app_storage.preferred_targets(
                connection,
                workspace_id=context.workspace_id,
                principal_id=context.principal,
                now_us=resolved_at_us,
            )
            if preferred:
                ordered = tuple(
                    sorted(
                        ordered,
                        key=lambda candidate: (
                            0 if (candidate.record_id, candidate.version) in preferred else 1,
                        ),
                    )
                )
            # The snapshot a continuation is bound to: the ranked versions, each by
            # its stored content digest, under the projection version that
            # rendered them. It names the content without reading it.
            snapshot_digest = token_digest(
                {
                    "authorization_frontier": authorization_frontier_digest,
                    "projection_version": PROJECTION_VERSION,
                    "ordered": [
                        [candidate.record_id, candidate.version, candidate.content_digest]
                        for candidate in ordered
                    ],
                }
            )
            start = 0
            if supplied is not None:
                if supplied.get("s") != snapshot_digest:
                    raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID)
                offset = supplied.get("o")
                if type(offset) is not int or not 0 < offset < len(ordered):
                    raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID)
                start = offset
            previews = []
            for candidate in ordered[start : start + limit]:
                rendered = _render_preview(candidate)
                if target is not None:
                    rendered["applicability"] = "matched"
                elif request.repository_target is not None:
                    latest = app_storage.latest_assessment(
                        connection,
                        workspace_id=context.workspace_id,
                        record_id=rendered["record_id"],
                        version=rendered["version"],
                        target_snapshot_id=request.repository_target.snapshot_id,
                    )
                    if latest is not None:
                        # The stored status goes back through the conservative
                        # assessment rather than being replayed, so a legacy
                        # `matched` row is not certified by recency. This is
                        # a read and writes nothing to the history.
                        rendered["applicability"] = (
                            app_storage.assess_against_registered_head(
                                connection,
                                workspace_id=context.workspace_id,
                                claimed_repository_id=rendered.get("repository_id"),
                                target_snapshot_id=request.repository_target.snapshot_id,
                                prior_status=latest["status"],
                            )
                        )
                previews.append(rendered)
            total = len(ordered)

        # The response stays inside its byte cap: a page that would pass it ends
        # early and continues from what it held, in either view family.
        previews = _within_response_cap(previews)
        continuation = None
        if start + len(previews) < total:
            continuation = PROCESS_CONTINUATION_TOKENS.encode(
                {
                    "b": binding_digest,
                    "o": start + len(previews),
                    "s": snapshot_digest,
                    "t": resolved_at_us,
                    "v": 1,
                }
            )
        return {
            "previews": previews,
            "page": ({"continuation_token": continuation} if continuation else {}),
            "coverage": {
                "projection": "current",
                "applicability": "unavailable" if target is None else "current",
            },
        }

    def _working_context_previews(
        self,
        connection: Any,
        context: OperationContext,
        *,
        query: str,
        limit: int,
        supplied: Mapping[str, Any] | None,
    ) -> tuple[list[dict[str, Any]], int, str, int]:
        """One page of the effective principal's own matching checkpoints.

        Another principal's checkpoint never reaches matching, the total, the
        snapshot digest or the offset. As in the governed views, the page is cut
        from the matched set, and a continuation must name the same snapshot.
        """
        normalized = " ".join(query.lower().split())
        matched: list[tuple[str, int, str]] = []
        for checkpoint_id, sequence, payload_json in continuity_storage.read_checkpoints(
            connection, workspace_id=context.workspace_id, principal_id=context.principal
        ):
            objective = str(json.loads(payload_json).get("objective", ""))
            if normalized in objective.lower():
                matched.append((checkpoint_id, sequence, objective))
        snapshot_digest = token_digest([list(item) for item in matched])
        start = 0
        if supplied is not None:
            offset = supplied.get("o")
            if (
                supplied.get("s") != snapshot_digest
                or type(offset) is not int
                or not 0 < offset < len(matched)
            ):
                raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID)
            start = offset
        previews: list[dict[str, Any]] = []
        for checkpoint_id, sequence, objective in matched[start : start + limit]:
            body, truncated = _bounded(objective)
            previews.append(
                {
                    "record_id": checkpoint_id,
                    "version": str(sequence),
                    "title": body[:200],
                    "preview": body,
                    "truncated": truncated,
                    "governance_state": "continuity_evidence",
                    "applicability": "not_evaluated",
                    "evidence_available": True,
                }
            )
        return previews, len(matched), snapshot_digest, start

    # --- engineering.expand ------------------------------------------------------

    def engineering_expand(
        self, context: OperationContext
    ) -> Mapping[str, Any] | AuditedOperationResult:
        try:
            request = EngineeringExpandInput.from_wire(context.request.input)
        except (ContractDecodeError, ContractSemanticError) as error:
            raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID) from error
        connection = self._connection()
        now_us = time.time_ns() // 1000
        # The anchor and every endpoint resolve under the effective caller's
        # evidence grant: a hidden anchor is `not_found`, exactly as a missing
        # one, and a hidden neighbour is never an edge, a node or a count.
        visible = self._visible_records(
            connection, context, resolution_instant_us=now_us
        )
        anchor = (request.anchor.record_id, request.anchor.version)
        if anchor not in visible:
            raise OperationError(ERROR_CODE_NOT_FOUND, _MESSAGE_NOT_FOUND)

        depth = 1 if request.depth is None else request.depth
        node_limit = 30 if request.node_limit is None else request.node_limit
        edge_limit = 60 if request.edge_limit is None else request.edge_limit

        nodes: list[dict[str, Any]] = [
            {"record_id": request.anchor.record_id, "version": request.anchor.version}
        ]
        edges: list[dict[str, Any]] = []
        visible_edges: list[
            tuple[dict[str, Any], tuple[str, str]]
        ] = []
        supersessions = read_governed_supersessions(
            connection,
            workspace_id=context.workspace_id,
            resolution_instant_us=now_us,
        )
        for edge in supersessions:
            source = (edge.governed_record_id, edge.source_version_id)
            target = (edge.governed_record_id, edge.target_version_id)
            if anchor not in (source, target) or not (
                source in visible and target in visible
            ):
                continue
            other = target if source == anchor else source
            visible_edges.append(
                (
                    {
                    "from_record": {
                        "record_id": edge.governed_record_id,
                        "version": edge.source_version_id,
                    },
                    "to_record": {
                        "record_id": edge.governed_record_id,
                        "version": edge.target_version_id,
                    },
                    "relation": "supersedes",
                    "status": "accepted",
                    },
                    other,
                )
            )

        relation_candidates = (
            engineering_conflicts.read_authorized_relation_candidates_for_anchor(
                connection,
                workspace_id=context.workspace_id,
                anchor_record_id=request.anchor.record_id,
                anchor_version=request.anchor.version,
                resolution_instant_us=now_us,
                label_grant=self._label_grant(context),
            )
        )
        for candidate in relation_candidates:
            source = (
                candidate.endpoint_a.record_id,
                candidate.endpoint_a.version,
            )
            target = (
                candidate.endpoint_b.record_id,
                candidate.endpoint_b.version,
            )
            if anchor not in (source, target) or not (
                source in visible and target in visible
            ):
                continue
            visible_edges.append(
                (
                    {
                        "from_record": {
                            "record_id": source[0],
                            "version": source[1],
                        },
                        "to_record": {
                            "record_id": target[0],
                            "version": target[1],
                        },
                        "relation": candidate.proposed_relation,
                        "status": candidate.status,
                    },
                    target if source == anchor else source,
                )
            )

        truncated = depth > 1
        for edge_payload, other in visible_edges:
            if len(edges) >= edge_limit:
                truncated = True
                break
            node = {"record_id": other[0], "version": other[1]}
            if node not in nodes and len(nodes) >= node_limit:
                truncated = True
                continue
            edges.append(edge_payload)
            if node not in nodes:
                nodes.append(node)
        # `depth` is declared by the contract and bounded by it (1..3); this
        # build expands one hop, so depth 2+ would add nothing today and is
        # reported as truncation rather than silently pretended.
        return {
            "nodes": nodes,
            "edges": edges,
            "truncated": truncated,
            "coverage": {"projection": "current", "applicability": "unavailable"},
        }

    # --- context.priority.set ------------------------------------------------------

    def context_priority_set(
        self, context: OperationContext
    ) -> Mapping[str, Any] | AuditedOperationResult:
        try:
            request = ContextPrioritySetInput.from_wire(context.request.input)
        except (ContractDecodeError, ContractSemanticError) as error:
            raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID) from error
        connection = self._connection()
        from omnivia_core_runtime.ownership.fencing import (
            read_guard as _read_guard,
        )

        guard = _read_guard(connection)
        identity = getattr(self.service, "identity", None)
        assert identity is not None and guard is not None
        equivalence = idempotency_equivalence(
            context.request.operation,
            context.request.metadata,
            request.to_wire(),
            principal_id=context.principal,
            workspace_id=context.workspace_id,
        )

        self._require_visible_version(
            connection,
            context,
            record_id=request.target.record_id,
            version=request.target.version,
        )

        def mutate(
            fenced: Any, settlement: MutationSettlementContext
        ) -> Mapping[str, Any]:
            # Rechecked under the fence: a revocation since the check above
            # writes no priority and rolls the audit back with it.
            self._require_visible_version(
                fenced,
                context,
                record_id=request.target.record_id,
                version=request.target.version,
            )
            app_storage.set_priority(
                fenced,
                settlement,
                workspace_id=context.workspace_id,
                principal_id=context.principal,
                target_record_id=request.target.record_id,
                target_version=request.target.version,
                priority=request.priority,
                expires_at_us=(
                    None if request.expires_at is None
                    else self._timestamp_us(request.expires_at)
                ),
                updated_at_us=settlement.settled_at_us,
            )
            result: dict[str, Any] = {
                "target": request.target.to_wire(),
                "priority": request.priority,
                "audit_reference": settlement.audit_ref,
            }
            if request.expires_at is not None:
                result["expires_at"] = request.expires_at
            return result

        def valid_result(wire: Mapping[str, Any]) -> bool:
            try:
                ContextPrioritySetResult.from_wire(wire)
            except (ContractDecodeError, ContractSemanticError):
                return False
            return True

        return _as_result(
            self._execute(
                context, connection, identity, guard, equivalence, mutate, valid_result
            )
        )

    # --- engineering.source.record -------------------------------------------------

    def engineering_source_capture_commit(
        self, context: OperationContext
    ) -> Mapping[str, Any] | AuditedOperationResult:
        """Commit an already sealed capture without accepting any capture facts."""

        if (
            not isinstance(context.request.input, Mapping)
            or not set(context.request.input) <= _CAPTURE_COMMIT_KEYS
        ):
            raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_SOURCE_INVALID)
        try:
            decoded = EngineeringSourceCaptureCommitInput.from_wire(
                context.request.input
            )
            request = source_storage.parse_captured_source_commit(
                context.request.input
            )
        except (
            ContractDecodeError,
            ContractSemanticError,
            source_storage.SourceRecordInvalid,
        ) as error:
            raise OperationError(
                ERROR_CODE_INVALID_REQUEST, _MESSAGE_SOURCE_INVALID
            ) from error
        connection = self._connection()
        from omnivia_core_runtime.ownership.fencing import read_guard as _read_guard

        guard = _read_guard(connection)
        identity = getattr(self.service, "identity", None)
        if identity is None or guard is None:
            raise OperationError("internal_non_recoverable", _MESSAGE_NO_STORAGE)
        equivalence = idempotency_equivalence(
            context.request.operation,
            context.request.metadata,
            decoded.to_wire(),
            principal_id=context.principal,
            workspace_id=context.workspace_id,
        )

        def mutate(
            fenced: Any, settlement: MutationSettlementContext
        ) -> Mapping[str, Any]:
            return source_storage.record_captured_source_event(
                fenced,
                settlement,
                workspace_id=context.workspace_id,
                principal_id=context.principal,
                installation_id=identity.installation_id,
                request=request,
            )

        def valid_result(wire: Mapping[str, Any]) -> bool:
            try:
                EngineeringSourceCaptureCommitResult.from_wire(wire)
            except (ContractDecodeError, ContractSemanticError):
                return False
            return True

        outcome = self._execute(
            context, connection, identity, guard, equivalence, mutate, valid_result
        )
        # Both lanes' semantics preserved: a captured commit advances coverage
        # exactly like a plain source record, so it also gets the same
        # best-effort durable invalidation catch-up (migration 0054).
        self._drain_invalidation(
            connection,
            identity,
            guard,
            workspace_id=context.workspace_id,
            stream_id=request.stream_id,
        )
        return AuditedOperationResult(outcome.result, audit_reference=outcome.audit_ref)

    def engineering_source_record(
        self, context: OperationContext
    ) -> Mapping[str, Any] | AuditedOperationResult:
        """Record one trusted source event and commit its stream head and barrier.

        The contract decoder is tolerant, so the raw payload is also validated
        strictly: unknown keys, malformed paths or digests and oversized manifests
        are refused before any grant is issued. Stream ownership comes from the
        authenticated principal, never from the payload.
        """
        try:
            request = EngineeringSourceRecordInput.from_wire(context.request.input)
        except (ContractDecodeError, ContractSemanticError) as error:
            raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID) from error
        try:
            record = source_storage.parse_source_record(context.request.input)
        except source_storage.SourceRecordTooLarge as error:
            raise application_refusal(
                ERROR_CODE_SIZE_LIMIT_EXCEEDED, _MESSAGE_SOURCE_TOO_LARGE
            ) from error
        except source_storage.SourceRecordInvalid as error:
            raise OperationError(
                ERROR_CODE_INVALID_REQUEST, _MESSAGE_SOURCE_INVALID
            ) from error
        connection = self._connection()
        from omnivia_core_runtime.ownership.fencing import (
            read_guard as _read_guard,
        )

        guard = _read_guard(connection)
        identity = getattr(self.service, "identity", None)
        if identity is None or guard is None:
            raise OperationError("internal_non_recoverable", _MESSAGE_NO_STORAGE)
        equivalence = idempotency_equivalence(
            context.request.operation,
            context.request.metadata,
            request.to_wire(),
            principal_id=context.principal,
            workspace_id=context.workspace_id,
        )

        def mutate(
            fenced: Any, settlement: MutationSettlementContext
        ) -> Mapping[str, Any]:
            return source_storage.record_source_event(
                fenced,
                settlement,
                workspace_id=context.workspace_id,
                principal_id=context.principal,
                record=record,
            )

        def valid_result(wire: Mapping[str, Any]) -> bool:
            try:
                EngineeringSourceRecordResult.from_wire(wire)
            except (ContractDecodeError, ContractSemanticError):
                return False
            return True

        outcome = self._execute(
            context, connection, identity, guard, equivalence, mutate, valid_result
        )
        self._drain_invalidation(
            connection,
            identity,
            guard,
            workspace_id=context.workspace_id,
            stream_id=record.stream_id,
        )
        return AuditedOperationResult(outcome.result, audit_reference=outcome.audit_ref)

    def _drain_invalidation(
        self, connection: Any, identity: Any, guard: Any, *, workspace_id: str, stream_id: str
    ) -> None:
        """Best-effort catch-up for the stream this record just advanced coverage on.

        Runs after the record's own mutation has already committed, in its own
        fenced transaction(s): the trusted source event is durable either way,
        and invalidation progress is itself durable and resumable (migration
        0054), so a failure here -- including this instance's fencing
        generation having been superseded in the meantime -- is left for the
        next call or the next restart's recovery pass rather than failing a
        request whose own write already succeeded.
        """
        try:
            invalidation_storage.drain_invalidation(
                connection,
                identity,
                workspace_id=workspace_id,
                stream_id=stream_id,
                fencing_generation=guard.fencing_generation,
                now_us=int(self.clock.wall_time().timestamp() * 1_000_000),
            )
        except Exception:  # noqa: BLE001,S110 - best-effort; the record already committed
            pass

    # --- engineering.repository.register --------------------------------------------

    def engineering_repository_register(
        self, context: OperationContext
    ) -> Mapping[str, Any] | AuditedOperationResult:
        """Bind one exact, installation-local checkout to one logical repository.

        An explicitly authorized local operator's own act, reached only through the
        accepted local client/CLI and never over the model-facing catalogue: the
        workspace comes from the authorised context and the installation from this
        service instance's own trusted identity, and neither can be named by the
        payload. `repository_id` is the caller's stable identity, never derived from
        `display_name` or from anything the checkout itself asserts, and
        `checkout_root` is validated as a real, non-symlinked, absolute local
        directory before anything is read from storage or written to it. Repeating
        an identical registration is idempotent; naming an already-registered
        repository id under different metadata is refused as a conflict; and
        re-registering an already-bound path under a different repository is an
        audited rebind (`checkout_disposition: "rebound"`), never a silent one.

        The contract's decoder tolerates and drops unknown fields (so a newer peer's
        additive minor release still decodes), but the schema itself refuses them, so
        the raw payload's keys are checked against the declared set before anything
        else: an unrecognised key -- `workspace_id`, `installation_id` or any other --
        is refused as invalid, never silently ignored.
        """
        if (
            not isinstance(context.request.input, Mapping)
            or not set(context.request.input) <= _REPOSITORY_REGISTER_KEYS
        ):
            raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_REPOSITORY_INVALID)
        try:
            request = EngineeringRepositoryRegisterInput.from_wire(context.request.input)
        except (ContractDecodeError, ContractSemanticError) as error:
            raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID) from error
        if (
            not is_identifier(request.repository_id)
            or not 1 <= len(request.display_name) <= 256
            or "\x00" in request.display_name
            or (
                request.provider_hint is not None
                and (
                    not 1 <= len(request.provider_hint) <= 256
                    or "\x00" in request.provider_hint
                )
            )
            or not source_capture.is_trusted_local_checkout_root(request.checkout_root)
        ):
            raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_REPOSITORY_INVALID)

        connection = self._connection()
        from omnivia_core_runtime.ownership.fencing import (
            read_guard as _read_guard,
        )

        guard = _read_guard(connection)
        identity = getattr(self.service, "identity", None)
        if identity is None or guard is None:
            raise OperationError("internal_non_recoverable", _MESSAGE_NO_STORAGE)
        # Trusted service state, never the payload: the same source
        # `_require_bound_checkout` reads before a snapshot capture is served.
        installation_id = identity.installation_id
        equivalence = idempotency_equivalence(
            context.request.operation,
            context.request.metadata,
            request.to_wire(),
            principal_id=context.principal,
            workspace_id=context.workspace_id,
        )

        def mutate(
            fenced: Any, settlement: MutationSettlementContext
        ) -> Mapping[str, Any]:
            # Read before write, so the disposition reports what was true before this
            # delivery, not what this delivery just made true.
            existing_repository = fenced.execute(
                "SELECT display_name, provider_hint FROM omnivia_engineering_repositories "
                "WHERE workspace_id = ? AND repository_id = ?",
                (context.workspace_id, request.repository_id),
            ).fetchone()
            repository_disposition = (
                "already_registered" if existing_repository is not None else "registered"
            )
            repository_identity.register_repository(
                fenced,
                settlement,
                workspace_id=context.workspace_id,
                repository_id=request.repository_id,
                display_name=request.display_name,
                provider_hint=request.provider_hint,
                registered_at_us=settlement.settled_at_us,
            )
            existing_checkout = fenced.execute(
                "SELECT checkout_id, repository_id FROM omnivia_engineering_checkouts "
                "WHERE workspace_id = ? AND installation_id = ? AND checkout_hint = ?",
                (context.workspace_id, installation_id, request.checkout_root),
            ).fetchone()
            if existing_checkout is None:
                checkout_id = self.allocate_identifier("erc")
                checkout_disposition = "bound"
            else:
                checkout_id = str(existing_checkout[0])
                checkout_disposition = (
                    "already_bound"
                    if existing_checkout[1] == request.repository_id
                    else "rebound"
                )
            repository_identity.register_checkout(
                fenced,
                settlement,
                workspace_id=context.workspace_id,
                checkout_id=checkout_id,
                repository_id=request.repository_id,
                installation_id=installation_id,
                checkout_hint=request.checkout_root,
                registered_at_us=settlement.settled_at_us,
            )
            return {
                "repository_id": request.repository_id,
                "checkout_id": checkout_id,
                "repository_disposition": repository_disposition,
                "checkout_disposition": checkout_disposition,
                "audit_reference": settlement.audit_ref,
            }

        def valid_result(wire: Mapping[str, Any]) -> bool:
            try:
                EngineeringRepositoryRegisterResult.from_wire(wire)
            except (ContractDecodeError, ContractSemanticError):
                return False
            return True

        outcome = self._execute(
            context, connection, identity, guard, equivalence, mutate, valid_result
        )
        return AuditedOperationResult(outcome.result, audit_reference=outcome.audit_ref)

    # --- engineering.review.record -------------------------------------------------

    def engineering_review_record(
        self, context: OperationContext
    ) -> Mapping[str, Any] | AuditedOperationResult:
        try:
            request = EngineeringReviewRecordInput.from_wire(context.request.input)
        except (ContractDecodeError, ContractSemanticError) as error:
            raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID) from error
        connection = self._connection()
        from omnivia_core_runtime.ownership.fencing import (
            read_guard as _read_guard,
        )

        guard = _read_guard(connection)
        identity = getattr(self.service, "identity", None)
        assert identity is not None and guard is not None
        equivalence = idempotency_equivalence(
            context.request.operation,
            context.request.metadata,
            request.to_wire(),
            principal_id=context.principal,
            workspace_id=context.workspace_id,
        )

        self._require_visible_version(
            connection,
            context,
            record_id=request.record_ref.record_id,
            version=request.record_ref.version,
        )
        # Set under the fence by `precondition`, which always runs before `mutate`.
        claimed: list[str | None] = []

        def mutate(
            fenced: Any, settlement: MutationSettlementContext
        ) -> Mapping[str, Any]:
            latest = app_storage.latest_assessment(
                fenced,
                workspace_id=context.workspace_id,
                record_id=request.record_ref.record_id,
                version=request.record_ref.version,
                target_snapshot_id=request.target_snapshot.snapshot_id,
            )
            if (
                request.expected_assessment_version is not None
                and (latest is None or latest["assessment_id"] != request.expected_assessment_version)
            ):
                raise app_storage.AssessmentPreconditionFailed(
                    "the target's current assessment is not the version this review expects"
                )
            # §15.5: no qualified dependency validation exists yet, so no review
            # outcome or evidence id can mint `matched` or clear a prior
            # `invalid` / `potentially_stale`. The review is recorded and the
            # assessment stays conservative.
            status = app_storage.assess_against_registered_head(
                fenced,
                workspace_id=context.workspace_id,
                claimed_repository_id=claimed[-1],
                target_snapshot_id=request.target_snapshot.snapshot_id,
                prior_status=None if latest is None else latest["status"],
            )
            app_storage.record_assessment(
                fenced,
                settlement,
                workspace_id=context.workspace_id,
                assessment_id=self.allocate_identifier("eas"),
                record_id=request.record_ref.record_id,
                version=request.record_ref.version,
                target_snapshot_id=request.target_snapshot.snapshot_id,
                status=status,
                basis="review",
                assessed_at_us=settlement.settled_at_us,
            )
            app_storage.record_attestation(
                fenced,
                settlement,
                workspace_id=context.workspace_id,
                attestation_id=self.allocate_identifier("eat"),
                record_id=request.record_ref.record_id,
                version=request.record_ref.version,
                target_snapshot_id=request.target_snapshot.snapshot_id,
                outcome=request.review_outcome,
                review_evidence_id=request.review_evidence_id,
                recorded_at_us=settlement.settled_at_us,
            )
            return {
                "record_ref": request.record_ref.to_wire(),
                "applicability": status,
                "audit_reference": settlement.audit_ref,
            }

        def valid_result(wire: Mapping[str, Any]) -> bool:
            try:
                EngineeringReviewRecordResult.from_wire(wire)
            except (ContractDecodeError, ContractSemanticError):
                return False
            return True

        def precondition(fenced: Any) -> str:
            # Rechecked under the fence and before the stated version is compared:
            # a revocation since the check above neither writes a review nor
            # discloses the hidden target's assessment count.
            claimed.append(
                self._require_visible_version(
                    fenced,
                    context,
                    record_id=request.record_ref.record_id,
                    version=request.record_ref.version,
                )
            )
            count = fenced.execute(
                "SELECT COUNT(*) FROM omnivia_engineering_assessments "
                "WHERE workspace_id = ? AND record_id = ? AND version = ? "
                "AND target_snapshot_id = ?",
                (
                    context.workspace_id,
                    request.record_ref.record_id,
                    request.record_ref.version,
                    request.target_snapshot.snapshot_id,
                ),
            ).fetchone()[0]
            return f"assessment-{count}"

        outcome = self._execute(
            context,
            connection,
            identity,
            guard,
            equivalence,
            mutate,
            valid_result,
            precondition=precondition,
        )
        return _as_result(outcome)

    # --- engineering.context.build ------------------------------------------------

    def engineering_context_build(
        self, context: OperationContext
    ) -> Mapping[str, Any] | AuditedOperationResult:
        """One non-persisted pack from a frozen frontier, under exact budgets.

        The builder never reads a clock and never opens a connection of its own:
        the resolution instant is captured once, the frontier is the same frozen
        authorised candidate set the search path freezes, and the rendering,
        citation ids and applicability statements are all derived from it under
        the effective budget. `pack_id` is the canonical SHA-256 of the result
        with exactly the root `pack_id` and the nested
        `reproducibility.artifact_checksum` removed (§12.3a).
        """
        try:
            request = decode_engineering_context_build_input(context.request.input)
        except (ContractDecodeError, ContractSemanticError) as error:
            raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID) from error
        mode = request.applicability_mode or "diagnostic"
        if mode not in _APPLICABILITY_MODES:
            raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID)

        negotiated = request.counting_mode is not None
        if negotiated:
            profile_policy = ENGINEERING_PROFILE_BUDGETS.get(request.profile)
            if profile_policy is None or request.budget is None:
                raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID)
            caller_tokens = (
                profile_policy.model_tokens
                if request.budget.model_tokens is None
                else request.budget.model_tokens
            )
            caller_bytes = (
                profile_policy.model_bytes
                if request.budget.model_bytes is None
                else request.budget.model_bytes
            )
            caller_hydrations = (
                profile_policy.hydrations
                if request.budget.hydrations is None
                else request.budget.hydrations
            )
            caller_evidence_bytes = (
                profile_policy.evidence_bytes
                if request.budget.evidence_bytes is None
                else request.budget.evidence_bytes
            )
            caller_authorized_candidates = (
                profile_policy.authorized_candidates
                if request.budget.authorized_candidates is None
                else request.budget.authorized_candidates
            )
            effective_tokens = min(
                caller_tokens,
                profile_policy.model_tokens,
                BUDGET_CEILING_TOKENS,
            )
            effective_bytes = min(
                caller_bytes,
                profile_policy.model_bytes,
                BUDGET_CEILING_BYTES,
            )
            effective_hydrations = min(
                caller_hydrations,
                profile_policy.hydrations,
                BUDGET_CEILING_HYDRATIONS,
            )
            effective_evidence_bytes = min(
                caller_evidence_bytes,
                profile_policy.evidence_bytes,
                BUDGET_CEILING_EVIDENCE_BYTES,
            )
            effective_authorized_candidates = min(
                caller_authorized_candidates,
                profile_policy.authorized_candidates,
                BUDGET_CEILING_AUTHORIZED_CANDIDATES,
            )
            if request.counting_mode == ENGINEERING_COUNTING_MODE_EXACT_TOKENS:
                raise OperationError(
                    ERROR_CODE_TOKENIZER_UNAVAILABLE,
                    _MESSAGE_TOKENIZER_UNAVAILABLE,
                    retry_class=DEFAULT_RETRY_CLASSIFICATION[
                        ERROR_CODE_TOKENIZER_UNAVAILABLE
                    ],
                )
        else:
            # Preserve the reviewed v1 budget gate: supplied legacy fields are
            # validated before storage access and are never silently clamped.
            (
                effective_tokens,
                effective_bytes,
                effective_hydrations,
                effective_evidence_bytes,
                effective_authorized_candidates,
            ) = self._effective_budget(request.budget)

        connection = self._connection()
        payload_budget = PayloadReadBudget(effective_evidence_bytes)
        source_snapshot_cache: dict[
            tuple[str, str, str | None], source_storage.CoveredSnapshot | None
        ] = {}
        if mode == "current_safe":
            # Validate the bounded shape before storage. Coverage itself is read
            # in the one outer snapshot below and still precedes every frontier.
            if not request.targets:
                raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID)
            if len(request.targets) > CURRENT_SAFE_TARGET_CAP:
                raise OperationError(
                    ERROR_CODE_SIZE_LIMIT_EXCEEDED, _MESSAGE_CURRENT_SAFE_BOUND
                )

        resolved_at_us = time.time_ns() // 1000
        views = ("current_canonical",) + (
            ("candidates",) if request.profile == "investigate" else ()
        )
        covered: list[source_storage.CoveredSnapshot] = []
        working: list[dict[str, Any]] = []
        selected_previews: list[
            tuple[PreviewCandidate, str, tuple[str, ...]]
        ] = []
        values: tuple[Any, ...] = ()
        evaluated = unproven = 0
        selection_omitted = False
        source_budget_omitted = False
        working_share_omitted = False
        authorized_candidate_count = eligible_count = 0
        normalized_query = normalize_query(request.query)
        label_grant = self._label_grant(context)
        frontier_hashers = {view: hashlib.sha256() for view in views}

        def selection_key(
            item: tuple[PreviewCandidate, str, tuple[str, ...]],
        ) -> tuple[int, int, str, str]:
            candidate = item[0]
            hits = (
                preview_search_text(candidate).count(normalized_query)
                if normalized_query
                else 0
            )
            return (-hits, -candidate.recorded_at_us, candidate.record_id, candidate.version)

        # Coverage, authorized identities, bounded projections, applicability
        # proof, selection, exact hydration and owned checkpoints all observe one
        # SQLite snapshot. Bodies are selected only after preview ranking.
        with read_snapshot(connection):
            if mode == "current_safe":
                for requested in request.targets:
                    try:
                        resolved = source_storage.covered_snapshot(
                            connection,
                            workspace_id=context.workspace_id,
                            snapshot_id=requested.snapshot_id,
                            repository_id=requested.repository_id,
                            payload_budget=payload_budget,
                            cache=source_snapshot_cache,
                        )
                    except (PayloadBudgetExceeded, PayloadLengthMismatch) as error:
                        raise _payload_read_refusal(error) from error
                    if resolved is None:
                        raise _applicability_pending()
                    covered.append(resolved)

            if request.profile == "resume":
                try:
                    checkpoint_metadata = continuity_storage.list_checkpoint_metadata(
                        connection,
                        workspace_id=context.workspace_id,
                        principal_id=context.principal,
                        payload_budget=payload_budget,
                        limit=CONTEXT_BUILD_SECTION_CAP,
                    )
                    working_share_limit = (
                        effective_bytes // WORKING_CONTEXT_SHARE_DIVISOR
                    )
                    if (
                        request.counting_mode
                        != ENGINEERING_COUNTING_MODE_BYTE_ONLY
                    ):
                        working_share_limit = min(
                            working_share_limit,
                            effective_tokens // WORKING_CONTEXT_SHARE_DIVISOR,
                        )
                    selected_checkpoint_metadata: list[
                        continuity_storage.CheckpointMetadata
                    ] = []
                    planned_checkpoint_bytes = 0
                    planned_working_bytes = 0
                    for metadata in checkpoint_metadata:
                        if (
                            len(selected_checkpoint_metadata)
                            == CONTEXT_BUILD_CHECKPOINT_CAP
                        ):
                            break
                        if (
                            planned_checkpoint_bytes
                            + metadata.payload_byte_length
                            > payload_budget.remaining
                        ):
                            source_budget_omitted = True
                            continue
                        working_upper_bound = _checkpoint_working_upper_bound(metadata)
                        if (
                            planned_working_bytes + working_upper_bound
                            > working_share_limit
                        ):
                            working_share_omitted = True
                            continue
                        selected_checkpoint_metadata.append(metadata)
                        planned_checkpoint_bytes += metadata.payload_byte_length
                        planned_working_bytes += working_upper_bound
                    checkpoint_rows = continuity_storage.read_selected_checkpoints(
                        connection,
                        workspace_id=context.workspace_id,
                        principal_id=context.principal,
                        selected=tuple(selected_checkpoint_metadata),
                        payload_budget=payload_budget,
                    )
                except (PayloadBudgetExceeded, PayloadLengthMismatch) as error:
                    raise _payload_read_refusal(error) from error
                for checkpoint_id, sequence, payload_json in checkpoint_rows:
                    payload = json.loads(payload_json)
                    working.append(
                        {
                            "checkpoint_id": checkpoint_id,
                            "sequence": sequence,
                            "objective": str(payload.get("objective", "")),
                            "unresolved": payload.get("unresolved_work", []),
                        }
                    )

            record_capacity = min(
                effective_hydrations,
                max(0, CONTEXT_BUILD_SECTION_CAP - len(working)),
                CONTEXT_BUILD_ABSOLUTE_SECTION_CAP,
            )
            best_accepted: list[
                tuple[PreviewCandidate, str, tuple[str, ...]]
            ] = []
            best_candidates: list[
                tuple[PreviewCandidate, str, tuple[str, ...]]
            ] = []
            after_record_id: str | None = None
            while True:
                record_ids = read_memory_record_id_page(
                    connection,
                    workspace_id=context.workspace_id,
                    resolution_instant_us=resolved_at_us,
                    domain_scope=OBSERVATION_DOMAIN,
                    after_record_id=after_record_id,
                    limit=AUTHORIZED_FRONTIER_PAGE_SIZE,
                )
                if not record_ids:
                    break
                for governed_view in views:
                    frontier = read_authorized_memory_frontier(
                        connection,
                        workspace_id=context.workspace_id,
                        resolution_instant_us=resolved_at_us,
                        view=governed_view,
                        label_grant=label_grant,
                        domain_scope=OBSERVATION_DOMAIN,
                        body_free=True,
                        record_ids=record_ids,
                    )
                    previews = self._previews_for_frontier(connection, context, frontier)
                    next_authorized_candidate_count = (
                        authorized_candidate_count + len(previews)
                    )
                    if (
                        next_authorized_candidate_count
                        > effective_authorized_candidates
                    ):
                        raise application_refusal(
                            ERROR_CODE_SIZE_LIMIT_EXCEEDED,
                            _MESSAGE_AUTHORIZED_CANDIDATE_BOUND,
                        )
                    authorized_candidate_count = next_authorized_candidate_count
                    versions_by_assembly = {
                        version.assembly_id: version for version in frontier.versions
                    }
                    for candidate in previews:
                        version = versions_by_assembly[candidate.assembly_id]
                        support = frontier.support_assembly_ids_by_record.get(
                            candidate.record_id, ()
                        )
                        manifest_item = to_canonical_json(
                            {
                                "assembly_id": version.assembly_id,
                                "content_digest": version.content_digest,
                                "domain_scope": version.domain_scope,
                                "evidence_disposition": version.evidence_disposition,
                                "governance_disposition": version.governance_disposition,
                                "has_evidence": version.has_evidence,
                                "layer": version.layer,
                                "record_id": version.record_id,
                                "record_type": version.record_type,
                                "recorded_at_us": version.recorded_at_us,
                                "support_assembly_ids": list(support),
                                "version": version.version_id,
                            }
                        ).encode("utf-8")
                        frontier_hashers[governed_view].update(
                            len(manifest_item).to_bytes(8, "big")
                        )
                        frontier_hashers[governed_view].update(manifest_item)
                    ordered = (
                        rank_previews(previews, request.query)
                        if normalized_query
                        else tuple(sorted(previews, key=lambda candidate: selection_key((candidate, "", ()))))
                    )
                    for candidate in ordered:
                        partition = (
                            "accepted_knowledge"
                            if governed_view == "current_canonical"
                            and candidate.governance_state == GOVERNANCE_STATE_ACCEPTED
                            and candidate.assertion_basis != "hypothesis"
                            else "candidate_findings"
                        )
                        if covered:
                            evaluated += 1
                            if evaluated > CURRENT_SAFE_CANDIDATE_CAP:
                                raise application_refusal(
                                    ERROR_CODE_SIZE_LIMIT_EXCEEDED,
                                    _MESSAGE_CURRENT_SAFE_BOUND,
                                )
                            try:
                                proven = _proven_version(
                                    connection,
                                    context.workspace_id,
                                    candidate.record_id,
                                    candidate.version,
                                    candidate.evidence_disposition == "available"
                                    and candidate.evidence_available,
                                    covered,
                                    payload_budget=payload_budget,
                                    snapshot_cache=source_snapshot_cache,
                                )
                            except (
                                PayloadBudgetExceeded,
                                PayloadLengthMismatch,
                            ) as error:
                                raise _payload_read_refusal(error) from error
                            if not proven:
                                unproven += 1
                                continue
                        eligible_count += 1
                        support = frontier.support_assembly_ids_by_record.get(
                            candidate.record_id, ()
                        )
                        item = (candidate, partition, support)
                        bucket = (
                            best_accepted
                            if partition == "accepted_knowledge"
                            else best_candidates
                        )
                        bucket.append(item)
                after_record_id = record_ids[-1]
                if len(record_ids) < AUTHORIZED_FRONTIER_PAGE_SIZE:
                    break

            best_accepted.sort(key=selection_key)
            best_candidates.sort(key=selection_key)
            selection_omitted = eligible_count > record_capacity
            ranked_previews = (*best_accepted, *best_candidates)
            admitted_components: set[tuple[str, str]] = set()
            planned_payload_bytes = 0
            for item in ranked_previews:
                if len(selected_previews) == record_capacity:
                    break
                candidate, _partition, support = item
                try:
                    plan = plan_authorized_governed_payload(
                        connection,
                        workspace_id=context.workspace_id,
                        resolution_instant_us=resolved_at_us,
                        authorized_support={candidate.assembly_id: support},
                        payload_budget=payload_budget,
                    )
                except (PayloadBudgetExceeded, PayloadLengthMismatch) as error:
                    raise _payload_read_refusal(error) from error
                components = _governed_payload_components(plan)
                incremental_components = tuple(
                    (kind, identity, byte_length)
                    for kind, identity, byte_length in components
                    if (kind, identity) not in admitted_components
                )
                incremental_bytes = sum(
                    byte_length
                    for _kind, _identity, byte_length in incremental_components
                )
                if (
                    planned_payload_bytes + incremental_bytes
                    > payload_budget.remaining
                ):
                    source_budget_omitted = True
                    continue
                selected_previews.append(item)
                planned_payload_bytes += incremental_bytes
                admitted_components.update(
                    (kind, identity)
                    for kind, identity, _byte_length in incremental_components
                )
            assembly_ids = tuple(item[0].assembly_id for item in selected_previews)
            support_assembly_ids = tuple(
                sorted(
                    {
                        support_id
                        for _candidate, _partition, support in selected_previews
                        for support_id in support
                    }
                )
            )
            try:
                values = hydrate_authorized_governed_record_values(
                    connection,
                    workspace_id=context.workspace_id,
                    resolution_instant_us=resolved_at_us,
                    assembly_ids=assembly_ids,
                    support_assembly_ids=support_assembly_ids,
                    payload_budget=payload_budget,
                )
            except (PayloadBudgetExceeded, PayloadLengthMismatch) as error:
                raise _payload_read_refusal(error) from error

        partition_by_assembly = {
            candidate.assembly_id: partition
            for candidate, partition, _support in selected_previews
        }
        selected: list[tuple[Any, str]] = []
        for assembly_id, value in zip(
            (item[0].assembly_id for item in selected_previews), values, strict=True
        ):
            selected.append((value.record, partition_by_assembly[assembly_id]))
        authorized_frontier_document = to_canonical_json(
            {
                "grant": {
                    "all_labels": label_grant.all_labels,
                    "labels": sorted(label_grant.labels),
                    "principal_id": label_grant.principal_id,
                },
                "profile": CONTEXT_BUILD_SELECTION_PROFILE,
                "projection_version": PROJECTION_VERSION,
                "resolution_instant_us": resolved_at_us,
                "view_digests": {
                    view: hasher.hexdigest()
                    for view, hasher in frontier_hashers.items()
                },
                "workspace_id": context.workspace_id,
            }
        )
        authorized_frontier_digest = (
            "sha256:"
            + hashlib.sha256(authorized_frontier_document.encode("utf-8")).hexdigest()
        )

        omissions: list[dict[str, Any]] = []
        selection_uncertainties: list[str] = []
        if covered:
            notice = (
                "current_safe: every cited record is proven `matched` at every "
                "target by whole-file dependency digests recorded by a trusted "
                "source; records whose applicability is unknown, potentially stale "
                "or invalid are omitted."
            )
            if unproven:
                omissions.append({"field": "sections", "reason": "applicability_unproven"})
        else:
            notice = (
                "Target applicability is not evaluated in this build; every "
                "applicability statement is `not_evaluated`."
            )
        if selection_omitted:
            omissions.append({"field": "sections", "reason": "selection_limit"})
            selection_uncertainties.append(
                "Additional authorized preview matches were omitted by the bounded "
                "hydration and section selection."
            )
        if source_budget_omitted:
            omissions.append({"field": "sections", "reason": "source_budget"})
        if working_share_omitted:
            omissions.append(
                {"field": "working_context", "reason": "working_context_share"}
            )
        if source_budget_omitted or working_share_omitted:
            selection_uncertainties.append(
                "Additional authorized content was omitted because its payload did "
                "not fit the bounded source-read or working-context budget."
            )

        build_context = BuildContext(
            resolved_at_us=resolved_at_us,
            workspace_id=context.workspace_id,
            principal_id=context.principal,
            query=request.query,
            profile=request.profile,
            mode=mode,
            targets=tuple(target.to_wire() for target in request.targets),
            source_coverage=tuple(
                {
                    "snapshot_id": target.snapshot_id,
                    "stream_id": target.stream_id,
                    "sequence": target.sequence,
                    "manifest_digest": target.manifest_digest,
                }
                for target in covered
            ),
            requested_budget=(
                None
                if request.budget is None
                else {
                    key: value
                    for key, value in {
                        "model_tokens": request.budget.model_tokens,
                        "model_bytes": request.budget.model_bytes,
                        "hydrations": request.budget.hydrations,
                        "evidence_bytes": request.budget.evidence_bytes,
                        "authorized_candidates": (
                            request.budget.authorized_candidates
                        ),
                    }.items()
                    if value is not None
                }
            ),
            effective_tokens=effective_tokens,
            effective_bytes=effective_bytes,
            projection_version=PROJECTION_VERSION,
            applicability_evaluator=EVALUATOR_VERSION,
            counting_mode=request.counting_mode,
            effective_hydrations=effective_hydrations,
            effective_evidence_bytes=effective_evidence_bytes,
            effective_authorized_candidates=effective_authorized_candidates,
            hydrations=len(values),
            source_bytes_read=payload_budget.source_bytes_read,
            selection_profile=CONTEXT_BUILD_SELECTION_PROFILE,
            authorized_frontier_digest=authorized_frontier_digest,
            authorized_candidate_count=authorized_candidate_count,
        )
        try:
            builder = (
                build_pack_byte_only
                if request.counting_mode == ENGINEERING_COUNTING_MODE_BYTE_ONLY
                else build_pack
            )
            pack = builder(
                build_context,
                tuple(
                    PackRecord(
                        record_id=record.provenance.identity.record_id,
                        version=record.provenance.identity.version,
                        partition=partition,
                        title=str(
                            record.content.get("title")
                            or record.provenance.identity.record_id
                        ),
                        body=str(
                            record.content.get("summary") or record.content.get("what") or ""
                        ),
                    )
                    for record, partition in selected
                ),
                tuple(
                    WorkingItem(
                        checkpoint_id=item["checkpoint_id"],
                        sequence=item["sequence"],
                        objective=item["objective"],
                        unresolved=tuple(str(u) for u in item["unresolved"]),
                    )
                    for item in working
                ),
                notice=notice,
                uncertainties=[notice, *selection_uncertainties],
                omissions=omissions,
            )
        except MandatoryContextTooLarge as error:
            error_code = (
                ERROR_CODE_CONTEXT_BUDGET_INSUFFICIENT
                if request.counting_mode == ENGINEERING_COUNTING_MODE_BYTE_ONLY
                else ERROR_CODE_TOKEN_LIMIT_EXCEEDED
            )
            raise OperationError(
                error_code,
                _MESSAGE_BUDGET,
                retry_class=DEFAULT_RETRY_CLASSIFICATION[error_code],
            ) from error
        return {"pack": pack}
