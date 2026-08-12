"""The `context_pack.build` handler -- one authoritative snapshot, frozen, then built.

The sixth production application operation, and the only one whose *output is a
checkable claim about how it was produced*. Every other handler in this directory
returns material a caller has to trust the server about; this one returns a
content-addressed artifact that states the authorized frontier it ranked over, so a
second party holding the document and the out-of-band manifest can recompute both.

That is the reason this module is shaped the way it is, and the order below is the
security property rather than a convenience:

1. **the effective authority, or nothing.** `OperationContext.authority`, `.scopes` and
   `.purpose` are Amendment 009's pass-through of what
   `authorize_application_request` already decided. All three are required here, and an
   absent one refuses *before the first storage read and before any builder work*
   (:func:`_effective`) -- because those three values are written verbatim into a signed
   artifact, and a build that reached storage first would have read a workspace under an
   authority it could not state;
2. decode and fully validate the payload; take the workspace from the *authorized*
   context, never from the payload;
3. require the authoritative connection;
4. fix **one** resolution instant, from one clock read, floored to the millisecond the
   contract's `Timestamp` can spell -- so the instant this build filters at and the
   instant it attests are one value rather than two that agree by rounding;
5. read the whole snapshot at that instant under **one SQLite read transaction**: the
   authoritative checkpoint, the contributing projection's readiness, the projection's
   active build, every evidence candidate, the current canonical governed view and the
   history view. Six reads, one database state. Each of them opens its own read-only
   authorizer fence inside that transaction; the fence is the narrower guarantee that no
   statement can write, and the transaction is the snapshot;
6. **refuse a missing or lagging contributing projection.** Reporting staleness is not
   enough for this operation: the pack states its own freshness, and a pack whose
   freshness says the material it ranked over was behind the write model is
   known-invalid at the instant it is sealed;
7. apply the authorization chain, all of it, before anything is frozen -- the
   evidence-label grant and `authorized_frontier`'s workspace/ACL/sensitivity/tombstone/
   temporal filters for L0, the governed resolver's view/governance/temporal narrowing
   plus the request's own `record_type` and `domain_scope` selectors for L2;
8. **freeze**, in `storage/context_pack.py`, which produces the frozen frontier *and*
   the out-of-band manifest from the frozen members, before the first ranking or
   selection decision;
9. build, with the pure builder, which holds no store and no clock;
10. judge the result with the contract's own
    `validate_context_pack_build_result`, against the manifest the freeze produced and
    against the exact effective values this build used.

**The manifest is the freeze's, never the result's.** `freeze_context_pack_frontier`
returns the frontier and the manifest as two values, and the manifest handed to the
validator below is the one that call produced. It is never rebuilt from the response,
from the pack, from the selected items or from anything read back out of a result: a
manifest reconstructed downstream asserts equality with itself and is not evidence.

**Nothing here reconstructs authority.** The authority, scopes and purpose written into
the artifact and passed to the validator are `context.authority`, `context.scopes` and
`context.purpose` -- the same objects, unsorted, undeduplicated, unrepaired. The builder
refuses a grant that is not already canonical rather than tidying one up, which is what
makes "the effective authority is the exact set already authorized" a property of the
artifact instead of a claim about the code.

**The version fields split three ways, and each way is honest about its source.** The
builder, normalization, ranking, reranking, selection and tokenizer identities name
algorithms implemented in `storage/context_pack.py`, so they are that module's own
published constants and the builder refuses any other value. The summarizer and model
versions are the disabled/empty spellings, because neither ran. `retrieval_version` is
the one field the pure slice genuinely cannot own -- nothing there retrieves -- and it
is supplied here, from :data:`CONTEXT_PACK_RETRIEVAL_VERSION`, which names the retrieval
this module performs in step 5 and nothing else. `policy_versions` is the same kind of
statement about the two policies this build actually applies.

**Refusals carry no caller value.** Every message is a frozen module constant, and the
decode failure is contained rather than chained: the contract's decode errors quote the
payload they rejected, so the sentinel is set inside the helper and the refusal raised
after it exits, leaving `__context__` genuinely `None`.
`scripts/check-raise-discipline.py` enforces that over this directory.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any, Final, TypeVar

from omnivia_core.contracts.v1 import (
    CONTEXT_PACK_SUMMARIZER_DISABLED,
    ERROR_CODE_INTERNAL_NON_RECOVERABLE,
    ERROR_CODE_INVALID_REQUEST,
    ERROR_CODE_PROJECTION_UNAVAILABLE,
    ERROR_CODE_SIZE_LIMIT_EXCEEDED,
    ERROR_CODE_STALE_PROJECTION,
    ERROR_CODE_TOKEN_LIMIT_EXCEEDED,
    GOVERNED_RECORD_VIEW_CURRENT_CANONICAL,
    GOVERNED_RECORD_VIEW_HISTORY,
    RETRY_CLASS_RETRYABLE_AFTER_DELAY,
    ContextPackBuildInput,
    ContextPackBuildResult,
    ContractDecodeError,
    ContractSemanticError,
    GrantedAuthority,
    ProjectionFreshness,
    Purpose,
    Scope,
    decode_context_pack_build_input,
    validate_context_pack_build_input,
    validate_context_pack_build_result,
)
from omnivia_core_runtime.service.operations import OperationContext, OperationError
from omnivia_core_runtime.storage.context_pack import (
    CONTEXT_PACK_BUILDER_VERSION,
    CONTEXT_PACK_FRESHNESS_PROJECTION_ID,
    CONTEXT_PACK_FRONTIER_FILTERS,
    CONTEXT_PACK_NORMALIZATION_VERSION,
    CONTEXT_PACK_RANKING_VERSION,
    CONTEXT_PACK_RERANKER_DISABLED,
    CONTEXT_PACK_SELECTION_VERSION,
    CONTEXT_PACK_TOKENIZER_ID,
    CONTEXT_PACK_TOKENIZER_VERSION,
    OMISSION_TOKEN_BUDGET,
    ContextPackBuildContext,
    ContextPackBuilderInputInvalid,
    ContextPackFreeze,
    ContextPackFrontierTooLarge,
    FrozenEvidence,
    FrozenRecord,
    build_context_pack,
    freeze_context_pack_frontier,
)
from omnivia_core_runtime.storage.governed import (
    GovernedRecordValue,
    read_governed_record_values,
)
from omnivia_core_runtime.storage.projections.fts import ActiveBuild, active_build
from omnivia_core_runtime.storage.repository import (
    CONTRIBUTING_PROJECTIONS,
    authoritative_checkpoint,
    projection_readiness,
    read_evidence_candidates,
)
from omnivia_core_runtime.storage.retrieval import (
    authorized_frontier,
    local_owner_label_grant,
    normalize_query,
)

#: What one builder-owned step produces. Only :func:`_mapped` is generic over it, so that
#: the freeze and the build share one failure mapping without either losing its own type.
_Produced = TypeVar("_Produced")

#: The retrieval this operation performs, named by the module that performs it.
#:
#: `storage/context_pack.py` states why this one version field cannot be its own: nothing
#: in the pure slice retrieves, the candidates arrive already read, and which retrieval
#: produced them is a fact only this handler holds. So it is stated here, beside the read
#: it names -- the six-read authoritative snapshot of step 5 plus the L0 and L2
#: authorization chains of step 7 -- rather than taken from a caller or from a fixture. A
#: change to that retrieval without a change to this constant is the one drift a reviewer
#: can then actually see.
CONTEXT_PACK_RETRIEVAL_VERSION: Final = "context-pack.retrieval.authoritative-snapshot.v1"

#: The policies in force over one build of this operation, and their versions.
#:
#: Two entries, because this build applies two policies and no more: the Personal-mode
#: evidence-label grant `storage/retrieval.py` evaluates on every L0 candidate, and the
#: governed view grant that decides which of 0009's views this operation may resolve. Both
#: name code in this tree; neither is a placeholder and neither is read from a request. A
#: pack records the policy versions it was built under, so a third entry here without a
#: third policy behind it would be exactly the fabricated-but-verifying claim the version
#: constants above are pinned to prevent.
CONTEXT_PACK_POLICY_VERSIONS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "evidence_label_acl": "local-owner.all-labels.v1",
        "governed_view": "current-canonical-and-history.v1",
    }
)

#: The one governed view whose members this operation may present as current content, and
#: the one whose members are frontier and reproducibility input only. Both are resolved,
#: both are frozen, and only the first can be selected -- `storage/context_pack.py` omits
#: every history member by construction, because a superseded version presented beside a
#: current one is two contradictory claims about one instant.
CONTENT_VIEW: Final = GOVERNED_RECORD_VIEW_CURRENT_CANONICAL
HISTORY_VIEW: Final = GOVERNED_RECORD_VIEW_HISTORY

#: Refusal messages, frozen as constants for the reason every refusal on this path is: a
#: handler failure becomes a wire `ApiError` a caller reads, and nothing about this
#: server's state or this caller's own values may travel there.
#:
#: `_MESSAGE_NO_EFFECTIVE_AUTHORITY` is the one Amendment 009 adds, and it is deliberately
#: a single message for all three fields. Which of the three was absent is a fact about
#: this build's own wiring, not about the caller, and a refusal that named it would be
#: this path's only message carrying server state.
_MESSAGE_NO_EFFECTIVE_AUTHORITY: Final = (
    "this build cannot compose a Context Pack without the effective authority, scopes "
    "and purpose the authorization seam produced; a pack records all three verbatim, and "
    "a build that supplied any of them from anywhere else would attest an authority "
    "nobody granted"
)
_MESSAGE_INVALID_INPUT: Final = "the request payload is not a valid context pack build"
_MESSAGE_NO_STORAGE: Final = "this service instance is not serving authoritative storage"
_MESSAGE_PROJECTION_UNAVAILABLE: Final = (
    "this build has no active compatible projection for this context pack"
)
_MESSAGE_STALE_PROJECTION: Final = (
    "a projection this context pack reads lags the authoritative source checkpoint"
)
_MESSAGE_FRONTIER_TOO_LARGE: Final = (
    "the authorized frontier for this request is wider than one pack can account for in "
    "full, and this build refuses to truncate it"
)
_MESSAGE_NOT_BUILDABLE: Final = (
    "this build produced material a Context Pack cannot be composed from"
)
_MESSAGE_TOKEN_LIMIT: Final = (
    "the requested token budget cannot carry any matching authorized candidate"
)


def context_pack_build(context: OperationContext) -> Mapping[str, Any]:
    """Answer one `context_pack.build` over one frozen authoritative frontier."""
    # First, and before anything reads a row or builds a value. See `_effective`.
    authority, scopes, purpose = _effective(context)
    request_input = _input(context)

    connection = getattr(getattr(context, "service", None), "connection", None)
    if connection is None:
        raise OperationError(ERROR_CODE_INTERNAL_NON_RECOVERABLE, _MESSAGE_NO_STORAGE)

    workspace_id = context.workspace_id
    resolved_at_us, resolution = _instant()
    snapshot = _snapshot(
        connection, workspace_id=workspace_id, resolution_instant_us=resolved_at_us
    )

    # Refused, not reported. `ContextPackBuildResult` does carry a freshness statement, so
    # this operation *could* answer and say it was stale -- and that is exactly what §20.7
    # forbids for a sealed artifact: the pack would be known-invalid at the instant it was
    # content-addressed, and its digest would give that invalid answer an identity anyone
    # could go on citing.
    build = snapshot.build
    # One branch for the two shapes of "nothing to serve from", because they are one
    # answer for a caller: a projection with no activation at all, and an activation whose
    # build this snapshot could not read, both leave this operation with no freshness it
    # could truthfully state.
    if snapshot.missing or build is None:
        raise OperationError(
            ERROR_CODE_PROJECTION_UNAVAILABLE,
            _MESSAGE_PROJECTION_UNAVAILABLE,
            retry_class=RETRY_CLASS_RETRYABLE_AFTER_DELAY,
        )
    if snapshot.stale:
        raise OperationError(
            ERROR_CODE_STALE_PROJECTION,
            _MESSAGE_STALE_PROJECTION,
            retry_class=RETRY_CLASS_RETRYABLE_AFTER_DELAY,
        )

    # The L0 authorization chain. `configured_principal` is left at the constant in
    # `storage/retrieval.py` rather than passed from this context: comparing the session's
    # own principal with itself is not a check, and it would admit every principal.
    grant = local_owner_label_grant(
        principal_id=context.principal,
        workspace_id=workspace_id,
        granted_workspace=workspace_id,
    )
    frontier = authorized_frontier(
        snapshot.evidence,
        workspace_id=workspace_id,
        grant=grant,
        # `ContextPackBuildInput` declares no sensitivity selector, so the sensitivity
        # stage runs and admits every candidate. Stated rather than skipped: the stage is
        # a member of the attested chain, and a chain that claimed a filter it had jumped
        # over would be the defect the freeze exists to make visible.
        sensitivity=None,
        include_tombstoned=False,
        resolution_time_us=resolved_at_us,
    )

    # The freeze, in the retrieval layer, which produces the frontier the builder ranks
    # over *and* the out-of-band manifest the validator judges against. Everything below
    # this call sees frozen values and nothing else.
    frozen = _freeze(
        frontier_candidates=frontier,
        snapshot=snapshot,
        build=build,
        request=request_input,
        workspace_id=workspace_id,
    )

    freshness = ProjectionFreshness(
        as_of=resolution,
        # The same snapshot's own projection material, and the same two values the freeze
        # was given. Built here as an independent statement so the validator's
        # "the pack states one staleness to the envelope and another to the reader" check
        # compares two values that can disagree rather than the pack with itself.
        projection_versions={CONTEXT_PACK_FRESHNESS_PROJECTION_ID: build.build_digest},
        projection_watermarks={
            CONTEXT_PACK_FRESHNESS_PROJECTION_ID: build.source_checkpoint
        },
        stale=False,
    )
    result = _build(
        frozen,
        ContextPackBuildContext(
            request=request_input,
            workspace_id=workspace_id,
            # Verbatim, all three, exactly as the seam produced them.
            authority=authority,
            scopes=scopes,
            purpose=purpose,
            policy_versions=CONTEXT_PACK_POLICY_VERSIONS,
            canonical_resolution_time=resolution,
            # This module's own normalization of the request's query, through the same
            # NFKC-then-casefold `retrieval.py` applies. The builder recomputes it and
            # refuses any other value, so the form that is ranked and the form that is
            # attested cannot become two.
            normalized_query=normalize_query(request_input.query),
            normalization_version=CONTEXT_PACK_NORMALIZATION_VERSION,
            builder_version=CONTEXT_PACK_BUILDER_VERSION,
            retrieval_version=CONTEXT_PACK_RETRIEVAL_VERSION,
            ranking_version=CONTEXT_PACK_RANKING_VERSION,
            reranking_version=CONTEXT_PACK_RERANKER_DISABLED,
            selection_version=CONTEXT_PACK_SELECTION_VERSION,
            tokenizer_id=CONTEXT_PACK_TOKENIZER_ID,
            tokenizer_version=CONTEXT_PACK_TOKENIZER_VERSION,
            summarizer_version=CONTEXT_PACK_SUMMARIZER_DISABLED,
            model_versions={},
        ),
    )
    if (
        not result.sections
        and any(
            omission.code == OMISSION_TOKEN_BUDGET for omission in result.omissions
        )
    ):
        raise OperationError(ERROR_CODE_TOKEN_LIMIT_EXCEEDED, _MESSAGE_TOKEN_LIMIT)
    _judge(
        result,
        request_input,
        frozen,
        workspace_id=workspace_id,
        authority=authority,
        scopes=scopes,
        purpose=purpose,
        resolution=resolution,
        freshness=freshness,
    )
    return result.to_wire()


def _effective(
    context: OperationContext,
) -> tuple[GrantedAuthority, tuple[Scope, ...], Purpose]:
    """The seam's own effective authority, scopes and purpose, or a refusal.

    Amendment 009's pass-through is what makes this operation possible at all: a Context
    Pack records the authority it was built under, and the only truthful value for that is
    the one `authorize_application_request` already produced. This function is where the
    absence of any of the three stops the request, and it is called before the payload is
    decoded, before the connection is reached and before a single row is read -- so a
    build that could not state its authority never touches the workspace.

    Absence is `None` and nothing else. An empty roles tuple, an empty capability tuple
    and an empty-but-present scope tuple are all real answers about a real grant, and none
    of them is treated as missing here; the builder's own canonical-array rules are what
    then refuse a scope set that could never be satisfied.

    `internal_non_recoverable`, because this is a wiring fault of this build rather than
    anything a caller did or could fix by retrying: the production dispatcher always
    supplies all three, so reaching this refusal means a handler was invoked outside that
    path. The message is one frozen constant carrying no caller value and naming none of
    the three fields.
    """
    authority = context.authority
    scopes = context.scopes
    purpose = context.purpose
    if authority is None or scopes is None or purpose is None:
        raise OperationError(
            ERROR_CODE_INTERNAL_NON_RECOVERABLE, _MESSAGE_NO_EFFECTIVE_AUTHORITY
        )
    return authority, scopes, purpose


def _input(context: OperationContext) -> ContextPackBuildInput:
    """The payload as a validated input, or a refusal that quotes nothing.

    Both steps are required and `decode_context_pack_build_input` is only the first: the
    decoder refuses the fields this operation does not have, and
    `validate_context_pack_build_input` is what refuses an unrecognized mode, an empty
    query and a non-positive budget. The intermediate value is promoted to `decoded` only
    once both have passed.

    The sentinel-then-raise shape is this tree's convention and it is load-bearing: both
    contract errors quote the payload they rejected, and raising inside the handler would
    leave that text reachable through `__context__` on the exception a caller catches.
    """
    decoded: ContextPackBuildInput | None
    try:
        candidate = decode_context_pack_build_input(context.request.input)
        validate_context_pack_build_input(candidate)
        decoded = candidate
    except (ContractDecodeError, ContractSemanticError):
        decoded = None
    if decoded is None:
        raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID_INPUT)
    return decoded


class _Snapshot:
    """One coherent authoritative read, as the six facts it produced.

    A plain object rather than a value with behaviour: it exists so the read transaction
    is one statement in the handler and so nothing below it can reach the connection. It
    carries no handle, no cursor and no id to resolve.
    """

    __slots__ = ("build", "evidence", "history", "missing", "records", "stale")

    def __init__(
        self,
        *,
        build: ActiveBuild | None,
        evidence: tuple[Any, ...],
        records: tuple[GovernedRecordValue, ...],
        history: tuple[GovernedRecordValue, ...],
        missing: bool,
        stale: bool,
    ) -> None:
        self.build = build
        self.evidence = evidence
        self.records = records
        self.history = history
        self.missing = missing
        self.stale = stale


def _snapshot(
    connection: Any, *, workspace_id: str, resolution_instant_us: int
) -> _Snapshot:
    """Every fact this build needs, read in one transaction at one instant.

    Six reads and one `BEGIN`. The transaction is the whole point: a frontier assembled
    from evidence read in one database state and governed versions read in another can
    present a record beside the correction that replaced it, and the pack would then be a
    content-addressed account of a workspace that never existed.

    Each read fences itself read-only inside that transaction, and each already states a
    total `ORDER BY`; `resolve_governed_versions` and the hydration snapshot both decline
    to end a transaction they did not open, which is what lets all six share this one.
    Transaction control sits outside the fence deliberately -- the fence is about what may
    touch a row, not about who owns the transaction the rows are read in.

    The readiness comparison is made here, inside the snapshot, rather than re-asked
    later: a projection that moved between the read and the refusal would make the pack's
    freshness statement describe a database state this answer was never built from.
    """
    connection.execute("BEGIN")
    try:
        checkpoint = authoritative_checkpoint(connection, workspace_id=workspace_id)
        readiness = projection_readiness(
            connection,
            workspace_id=workspace_id,
            source_checkpoint=checkpoint,
            contributing=CONTRIBUTING_PROJECTIONS,
        )
        build = active_build(connection, workspace_id=workspace_id)
        evidence = read_evidence_candidates(connection, workspace_id=workspace_id)
        records = read_governed_record_values(
            connection,
            workspace_id=workspace_id,
            resolution_instant_us=resolution_instant_us,
            view=CONTENT_VIEW,
        )
        history = read_governed_record_values(
            connection,
            workspace_id=workspace_id,
            resolution_instant_us=resolution_instant_us,
            view=HISTORY_VIEW,
        )
    except BaseException:
        connection.execute("ROLLBACK")
        raise
    connection.execute("COMMIT")
    return _Snapshot(
        build=build,
        evidence=evidence,
        records=records,
        history=history,
        missing=bool(readiness.missing),
        stale=bool(readiness.stale),
    )


def _freeze(
    *,
    frontier_candidates: Any,
    snapshot: _Snapshot,
    build: ActiveBuild,
    request: ContextPackBuildInput,
    workspace_id: str,
) -> ContextPackFreeze:
    """Freeze the authorized frontier, and produce the manifest that vouches for it.

    The request's own two selectors are applied here, immediately before the freeze and
    to both governed partitions, because they are the last two members of the attested
    filter chain. They narrow `history` as well as `records`: history is frontier and
    reproducibility input, so a history member the request's selectors exclude would put a
    candidate in the manifest that this request was never asking about.

    Nothing else narrows. Everything upstream of this call has already run -- the request
    envelope's workspace, scope, purpose, capability and policy authorization, the L0
    grant and `authorized_frontier`'s five filters, and the governed resolver's view,
    governance and temporal narrowing -- and a filter applied at the freeze would be a
    filter applied *at* it rather than before it.

    `build` is the snapshot's own activated run, passed in already narrowed rather than
    re-read off `snapshot`: the refusal above is what establishes there is one, and
    threading the value keeps that establishment and this use one statement.

    The freeze is run through :func:`_mapped` for the same reason the builder is, and it is
    the more important of the two: the ceiling that refuses an over-wide frontier is
    enforced *here*, not in `build_context_pack`, so a handler that guarded only the
    builder would let `ContextPackFrontierTooLarge` escape the dispatcher entirely.
    """
    return _mapped(
        lambda: freeze_context_pack_frontier(
            workspace_id=workspace_id,
            evidence=tuple(
                FrozenEvidence(
                    recorded_at_us=candidate.recorded_at_us, artifact=candidate.artifact
                )
                for candidate in frontier_candidates.candidates
            ),
            records=_selected(snapshot.records, request),
            history=_selected(snapshot.history, request),
            filters_applied=CONTEXT_PACK_FRONTIER_FILTERS,
            projection_versions={
                CONTEXT_PACK_FRESHNESS_PROJECTION_ID: build.build_digest
            },
            projection_watermarks={
                CONTEXT_PACK_FRESHNESS_PROJECTION_ID: build.source_checkpoint
            },
            # From the same readiness the refusal above was decided on, rather than a
            # literal: one measurement, used twice, so the freeze cannot be told something
            # the handler did not check.
            projection_stale=snapshot.stale,
        )
    )


def _selected(
    values: tuple[GovernedRecordValue, ...], request: ContextPackBuildInput
) -> tuple[FrozenRecord, ...]:
    """One governed partition, narrowed by the request's `record_type`/`domain_scope`."""
    return tuple(
        FrozenRecord(recorded_at_us=value.recorded_at_us, record=value.record)
        for value in values
        if (
            request.record_type is None
            or value.record.record_type == request.record_type
        )
        and (
            request.domain_scope is None
            or value.record.domain_scope == request.domain_scope
        )
    )


def _build(
    frozen: ContextPackFreeze, build_context: ContextPackBuildContext
) -> ContextPackBuildResult:
    """The pure builder, behind the same failure mapping the freeze runs behind."""
    return _mapped(lambda: build_context_pack(frozen.frontier, build_context))


def _mapped(action: Callable[[], _Produced]) -> _Produced:
    """Run one builder-owned step, mapping its two failures to the contract's vocabulary.

    The two are kept apart because they mean different things to a caller. A frontier too
    wide for a pack that accounts for all of it is `size_limit_exceeded` -- the caller's
    request was too large, and the honest answer is to refuse rather than truncate the
    frontier a pack claims to have ranked over. Everything raised as an invalid builder
    input is a producer-side invariant a correct handler cannot reach, so it is
    `internal_non_recoverable`; the builder's own message names the rule and never the
    material, and neither of these does either.

    One helper rather than a block in each of the two call sites, because both raise both:
    the ceiling is enforced at the freeze and the canonical-array rules are enforced in the
    build, and a mapping written twice is two places for those two answers to drift.

    The sentinel-then-raise shape is this tree's convention: both builder errors are
    contained rather than chained, so nothing they carry is reachable through
    `__context__` on the error a caller catches.
    """
    refused: str | None = None
    try:
        return action()
    except ContextPackFrontierTooLarge:
        refused = ERROR_CODE_SIZE_LIMIT_EXCEEDED
    except ContextPackBuilderInputInvalid:
        refused = ERROR_CODE_INTERNAL_NON_RECOVERABLE
    if refused == ERROR_CODE_SIZE_LIMIT_EXCEEDED:
        raise OperationError(ERROR_CODE_SIZE_LIMIT_EXCEEDED, _MESSAGE_FRONTIER_TOO_LARGE)
    raise OperationError(ERROR_CODE_INTERNAL_NON_RECOVERABLE, _MESSAGE_NOT_BUILDABLE)


def _judge(
    result: ContextPackBuildResult,
    request: ContextPackBuildInput,
    frozen: ContextPackFreeze,
    *,
    workspace_id: str,
    authority: GrantedAuthority,
    scopes: tuple[Scope, ...],
    purpose: Purpose,
    resolution: str,
    freshness: ProjectionFreshness,
) -> None:
    """Run the contract's own judge before this pack goes to wire, or refuse.

    `expected_authorized_candidate_set` is `frozen.manifest` -- the value
    `freeze_context_pack_frontier` produced at the freeze, from the frozen members, before
    the first ranking decision. It is not rebuilt from the result, from the pack, from the
    selected items or from anything downstream, and that is the whole of its evidential
    value: the validator recomputes its checksum and compares it with what the artifact
    claims, so a build whose ranking saw one more candidate than its authorization
    admitted is refused here even though its response content would be byte-identical.

    Every producer assertion the build supplied is bound by exact equality rather than
    left to the validator's shape check, including the empty `model_versions`: an
    unsupplied expectation is only shape-checked, so a call that omitted one would be
    quietly weaker than it reads.

    A refusal here is `internal_non_recoverable` and carries a frozen message. The
    contract's own error quotes the material it rejected, so it is contained rather than
    chained -- a validator complaint naming a record id would otherwise reach a caller
    through `__context__`.
    """
    invalid = False
    try:
        validate_context_pack_build_result(
            result,
            request=request,
            expected_workspace_id=workspace_id,
            expected_authority=authority,
            expected_scopes=frozenset(scopes),
            expected_purpose=purpose,
            expected_policy_versions=CONTEXT_PACK_POLICY_VERSIONS,
            expected_authorized_candidate_set=frozen.manifest,
            canonical_resolution_time=resolution,
            response_freshness=freshness,
            expected_normalized_query=normalize_query(request.query),
            expected_normalization_version=CONTEXT_PACK_NORMALIZATION_VERSION,
            expected_builder_version=CONTEXT_PACK_BUILDER_VERSION,
            expected_retrieval_version=CONTEXT_PACK_RETRIEVAL_VERSION,
            expected_ranking_version=CONTEXT_PACK_RANKING_VERSION,
            expected_reranking_version=CONTEXT_PACK_RERANKER_DISABLED,
            expected_selection_version=CONTEXT_PACK_SELECTION_VERSION,
            expected_tokenizer_id=CONTEXT_PACK_TOKENIZER_ID,
            expected_tokenizer_version=CONTEXT_PACK_TOKENIZER_VERSION,
            expected_summarizer_version=CONTEXT_PACK_SUMMARIZER_DISABLED,
            expected_model_versions={},
        )
    except ContractSemanticError:
        invalid = True
    if invalid:
        raise OperationError(
            ERROR_CODE_INTERNAL_NON_RECOVERABLE, _MESSAGE_NOT_BUILDABLE
        )


def _instant() -> tuple[int, str]:
    """The one instant this build resolves at, as microseconds and as a `Timestamp`.

    One clock read, before anything is resolved, and the only one on this path -- the pure
    builder has no clock at all, and this is the only module in the pair that could read
    one.

    Floored to the millisecond before it is used for anything. A contract `Timestamp`
    carries milliseconds, so an unfloored microsecond instant would filter the frontier at
    one moment and attest a slightly earlier one, and a candidate recorded in the gap
    would be admitted by the filter and refused by the validator that reads the attested
    string. Flooring first makes the instant this build selects at and the instant it
    states one value.
    """
    resolved_at_us = time.time_ns() // 1_000_000 * 1_000
    return resolved_at_us, _timestamp(resolved_at_us)


def _timestamp(microseconds: int) -> str:
    """One microsecond instant as the contract's `Timestamp`.

    Millisecond precision with a `Z` suffix, the same rendering `storage/governed.py`
    gives every instant it publishes -- which is what makes the validator's comparisons
    comparisons between values rendered the same way.
    """
    moment = datetime.fromtimestamp(microseconds / 1_000_000, tz=UTC)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


__all__ = [
    "CONTENT_VIEW",
    "CONTEXT_PACK_POLICY_VERSIONS",
    "CONTEXT_PACK_RETRIEVAL_VERSION",
    "HISTORY_VIEW",
    "context_pack_build",
]
