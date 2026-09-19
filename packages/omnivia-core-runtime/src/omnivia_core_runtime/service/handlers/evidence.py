"""The evidence handler family: `evidence.search` reads, `evidence.capture` writes.

`evidence_capture` is at the bottom of this module and has its own long comment. Its one
architectural claim, stated here because it is a property of the *pair* rather than of
either handler: a capture may not report success until the content it captured is
findable by the search handler above it, and the step that makes that true runs after the
durable business commit rather than inside it. Everything else about it is the ordinary
mutation path every other workspace mutation in this build takes.

--

The `evidence.search` handler -- the first application handler that reads storage.

`workspace.inspect` before it opened nothing. This one does, and the order of what it
does is the security property of the whole V06-3 stage, so it is written to be read
top to bottom:

1. decode the request payload, and take the workspace from the *authorized* context;
2. refuse if a contributing projection is missing or lags the read point;
3. read every candidate in the workspace, under a fenced read;
4. build the evidence-label grant for this principal in this workspace;
5. **freeze the authorized frontier** -- workspace, ACL, sensitivity, tombstone and
   temporal filters, all of them, before anything below this line;
6. narrow the projection's materialised token material to exactly that frontier's ids,
   producing an immutable projected frontier;
7. rank, which is the first step that selects or orders, and the first step that sees
   the query;
8. map frontier members to the result page.

Step 5 is the line packet §7.2 draws, and steps 6 to 8 are on the far side of it. The
page is built by mapping over ranked frontier members, so "every item in any result is
a member of the frozen frontier" holds by construction rather than by a check that
could be skipped.

**Steps 6 and 7 are two steps on purpose, and that is the one architectural claim of
this lane a reviewer should check rather than accept.** §20.12's proof about Lane A's
ranker was an *import boundary*: `storage/retrieval.py` holds no `sqlite3`, no
connection and no callback, so an unfiltered candidate was unreachable rather than
merely forbidden. The obvious way to add an FTS5 ordering gives that up -- the index is
in SQLite, so a ranker that reads it holds a connection -- and it gives up more than the
proof, because `bm25()` computes its inverse document frequency and its average document
length over the whole index. An artifact the filter chain excluded then moves the
relative order of the members that *are* returned, invisibly, because every id in the
page is authorized either way.

So the projection is narrowed before anything is ranked. `SearchProjection.project`
addresses the session's material by the frozen frontier's ids and hands back a value
carrying those candidates and their token sequences; `retrieval.rank_projected` takes
that value and recomputes every statistic from it. The ranker keeps Lane A's import
boundary exactly, and an excluded artifact is absent from the numbers rather than merely
absent from the page.

What the handler owns is that there is no second path: the material is produced by the
service's own startup before this endpoint accepts anything, a request that finds none
is refused, and no branch below falls back to an unindexed scan or to the artifact's
authoritative `search_text`. Falling back would answer a caller successfully from an
ordering this build does not claim to serve, which is worse than the retryable refusal
it replaces.

**The workspace is the authorized one.** `context.workspace_id` comes from the session
grant and the endpoint binding after the seam refused every workspace they disagreed
on. `EvidenceSearchInput` carries no workspace field by contract, and none is read.

**Two things `retry_class` and this handler get right that no test enforces.**
`OperationError` defaults to `non_retryable`, while `stale_projection` and
`projection_unavailable` are contractually retryable after a delay, and nothing in this
tree validates that the two agree at runtime. Every raise site below passes
`retry_class` explicitly for that reason -- an omission would be silently wrong on the
wire and green in CI.

**Refusals carry no caller value.** Every message is a frozen module constant. The
decode failure in particular is contained rather than chained: the contract's own
decode errors quote the payload they rejected, so the sentinel is set inside the
handler and the refusal raised after it exits, leaving `__context__` genuinely `None`.
That is this tree's stated convention and `scripts/check-raise-discipline.py` enforces
it over exactly this directory.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from omnivia_core.contracts.v1 import (
    ERROR_CODE_CONFLICT,
    ERROR_CODE_INTERNAL_NON_RECOVERABLE,
    ERROR_CODE_INTERNAL_RECOVERABLE,
    ERROR_CODE_INVALID_REQUEST,
    ERROR_CODE_PROJECTION_UNAVAILABLE,
    ERROR_CODE_SIZE_LIMIT_EXCEEDED,
    ERROR_CODE_STALE_PROJECTION,
    EVIDENCE_CAPTURE_MAX_CONTENT_BYTES,
    EVIDENCE_CAPTURE_SOURCE_KIND,
    RETRY_CLASS_RETRYABLE,
    RETRY_CLASS_RETRYABLE_AFTER_DELAY,
    ContractDecodeError,
    ContractSemanticError,
    EvidenceCaptureInput,
    EvidenceCaptureResult,
    EvidenceCaptureSizeLimitError,
    EvidenceSearchInput,
    EvidenceSearchResult,
    IdempotencyEquivalence,
    PageMetadata,
    SourceReference,
    canonical_timestamp_nanoseconds,
    decode_evidence_capture_input,
    decode_evidence_search_input,
    idempotency_equivalence,
    to_canonical_json,
    validate_evidence_capture_result,
)
from omnivia_core_runtime.ownership.fencing import read_guard
from omnivia_core_runtime.ownership.identity import Clock
from omnivia_core_runtime.service.authorization import (
    AuthenticatedSession,
    ServiceBinding,
)
from omnivia_core_runtime.service.mutation import (
    MutationSettlementContext,
    execute_mutation,
    issue_mutation_grant,
)
from omnivia_core_runtime.service.operations import (
    AuditedOperationResult,
    OperationContext,
    OperationError,
)
from omnivia_core_runtime.service.pagination import (
    PROCESS_CONTINUATION_TOKENS,
    token_digest,
)
from omnivia_core_runtime.storage.memory import IdentifierAllocator
from omnivia_core_runtime.storage.projections.fts import (
    ProjectionError,
    ProjectionUnavailable,
    SearchProjection,
    StaleProjection,
    build_search_projection,
    open_search_projection,
    require_current,
    session_search_projection,
)
from omnivia_core_runtime.storage.repository import (
    CONTRIBUTING_PROJECTIONS,
    authoritative_checkpoint,
    projection_readiness,
    read_evidence_candidates,
)
from omnivia_core_runtime.storage.retrieval import (
    AuthorizedFrontier,
    ProjectedFrontier,
    authorized_frontier,
    first_query_token,
    local_owner_label_grant,
    rank_projected,
)
from omnivia_core_runtime.workspace.blob_publication import (
    BlobPublicationRefused,
    publish_blob,
)

#: The page size a request that names none gets. Well under the catalogue's
#: `max_page_size` of 1000, and stated here rather than defaulted implicitly so that a
#: result page's size is always a decision this build made.
DEFAULT_PAGE_LIMIT: Final = 50

#: The ceiling the frozen catalogue fixes for this operation. A request may ask for
#: less; it cannot ask for more, and the schema refuses a larger value before this
#: handler sees it.
MAX_PAGE_LIMIT: Final = 1000

#: Refusal messages, frozen as constants for the same reason the authorization seam's
#: are: a handler failure becomes a wire `ApiError` a caller reads, and nothing about
#: this server's state or this caller's own values may travel there.
_MESSAGE_INVALID_INPUT: Final = "the request payload is not a valid evidence search"
_MESSAGE_NO_STORAGE: Final = (
    "this service instance is not serving authoritative storage"
)
_MESSAGE_STALE_PROJECTION: Final = (
    "a projection this search reads lags the authoritative source checkpoint"
)
_MESSAGE_PROJECTION_UNAVAILABLE: Final = (
    "this build has no active compatible projection for this search"
)
_TOKEN_KEYS: Final = frozenset({"b", "o", "s", "t", "v"})

#: The two operations this module serves, named once so the family wiring, the purpose
#: map and the registry all read the same strings.
EVIDENCE_SEARCH_OPERATION: Final = "evidence.search"
EVIDENCE_CAPTURE_OPERATION: Final = "evidence.capture"

#: The one `source_kind` a capture writes, from the contract rather than restated here.
CAPTURE_SOURCE_KIND: Final = EVIDENCE_CAPTURE_SOURCE_KIND

#: The largest base64 text that could decode within the capture ceiling. Applied to the
#: encoded string *before* it is decoded, so a hostile payload costs one integer
#: comparison rather than its own decoded size in memory. The contract applies the same
#: bound at the same point; this one is the trusted runtime's own, because the digest and
#: the byte length this service persists are computed here and must be bounded here.
_MAX_ENCODED_LENGTH: Final = 4 * ((EVIDENCE_CAPTURE_MAX_CONTENT_BYTES + 2) // 3)

_MESSAGE_INVALID_CAPTURE: Final = "the request payload is not a valid evidence capture"
_MESSAGE_SOURCE_CONFLICT: Final = (
    "this source identity already names evidence with different content or claims"
)
_MESSAGE_SOURCE_NOT_UNIQUE: Final = (
    "this source identity does not name exactly one evidence artifact"
)
_MESSAGE_BLOB_UNPUBLISHED: Final = (
    "the submitted content could not be made durable in this workspace"
)
_MESSAGE_CAPTURE_TOO_LARGE: Final = (
    "the submitted content exceeds the evidence capture size limit"
)


def evidence_search(context: OperationContext) -> Mapping[str, Any]:
    """Answer one `evidence.search` over the authorized frontier."""
    request_input = _decode(context)

    connection = getattr(getattr(context, "service", None), "connection", None)
    if connection is None:
        raise OperationError(ERROR_CODE_INTERNAL_NON_RECOVERABLE, _MESSAGE_NO_STORAGE)

    workspace_id = context.workspace_id

    # Freshness, before any read that would answer the caller. Packet §20.7's clause
    # resolves to *refuse* rather than report for this operation, because
    # `EvidenceSearchResult` has no freshness field to report in and succeeding
    # silently from a lagging projection is the thing the clause forbids.
    limit = _limit(request_input)
    binding = request_input.to_wire()
    binding.pop("page", None)
    binding_digest = token_digest(
        {
            "principal": context.principal,
            "workspace": context.workspace_id,
            "operation": "evidence.search",
            "input": binding,
            "limit": limit,
        }
    )
    supplied: Mapping[str, Any] | None = None
    if request_input.page is not None:
        token = request_input.page.continuation_token
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
            raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID_INPUT)
        read_point = supplied.get("t")
        if type(read_point) is not int or read_point <= 0:
            raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID_INPUT)
        checkpoint = str(read_point)
    else:
        checkpoint = authoritative_checkpoint(connection, workspace_id=workspace_id)
    readiness = projection_readiness(
        connection,
        workspace_id=workspace_id,
        source_checkpoint=checkpoint,
        contributing=CONTRIBUTING_PROJECTIONS,
    )
    if readiness.missing:
        raise OperationError(
            ERROR_CODE_PROJECTION_UNAVAILABLE,
            _MESSAGE_PROJECTION_UNAVAILABLE,
            retry_class=RETRY_CLASS_RETRYABLE_AFTER_DELAY,
        )
    if readiness.stale:
        raise OperationError(
            ERROR_CODE_STALE_PROJECTION,
            _MESSAGE_STALE_PROJECTION,
            retry_class=RETRY_CLASS_RETRYABLE_AFTER_DELAY,
        )

    candidates = read_evidence_candidates(connection, workspace_id=workspace_id)

    # The ACL stage's input: an explicit effective grant, evaluated per candidate by
    # the filter chain below. Not an absent check, not a bypass, not a default for an
    # unknown principal -- packet §20.3 forbids all three by name.
    #
    # `configured_principal` is left at its default, which is the constant in
    # `storage/retrieval.py` rather than anything reachable from this request. Passing
    # `context.principal` for both sides would compare a value with itself and admit
    # every principal, which is the bypass that decision names. A session for any other
    # principal reaches here and gets the empty grant.
    grant = local_owner_label_grant(
        principal_id=context.principal,
        workspace_id=workspace_id,
        granted_workspace=workspace_id,
    )

    # The freeze. Everything below this call sees a frozen value and nothing else.
    frontier = authorized_frontier(
        candidates,
        workspace_id=workspace_id,
        grant=grant,
        sensitivity=request_input.sensitivity,
        include_tombstoned=bool(request_input.include_tombstoned),
        # The read point. `authoritative_checkpoint` is this workspace's high-water
        # `recorded_at_us`, so resolving at it admits everything the fenced read saw
        # and nothing written after it -- which is what "fresh at their transaction
        # read point" means for a direct authoritative read (§20.7).
        resolution_time_us=int(checkpoint),
    )

    # Ordering, from the projection this build actually serves and from nothing else.
    # The material was produced by the service's own startup path before this endpoint
    # accepted anything, so what is reached for here either exists or the request is
    # refused -- there is no Lane A ordering behind this call to fall back to, and a
    # fallback would answer from an unindexed substring scan while reporting success.
    projection = session_search_projection(connection)
    if projection is None:
        raise OperationError(
            ERROR_CODE_PROJECTION_UNAVAILABLE,
            _MESSAGE_PROJECTION_UNAVAILABLE,
            retry_class=RETRY_CLASS_RETRYABLE_AFTER_DELAY,
        )
    # Two statements, and the order of the two is the architecture. The adapter narrows
    # the projection's material *by the frozen frontier's ids* and returns a value; the
    # pure ranker then computes every score and every statistic from that value alone.
    # Neither half can be handed the corpus: the first is given the frontier, and the
    # second is given only what the first returned.
    projected = _projected(connection, projection, frontier)
    ranked = rank_projected(
        projected, request_input.query, limit=len(projected.candidates)
    )
    snapshot_digest = token_digest(
        [
            [candidate.artifact.evidence_id, candidate.artifact.content_checksum]
            for candidate in ranked
        ]
    )
    start = 0
    if supplied is not None:
        if supplied.get("s") != snapshot_digest:
            raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID_INPUT)
        offset = supplied.get("o")
        if type(offset) is not int or not 0 < offset < len(ranked):
            raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID_INPUT)
        start = offset

    page_items = ranked[start : start + limit]
    continuation = None
    if start + len(page_items) < len(ranked):
        continuation = PROCESS_CONTINUATION_TOKENS.encode(
            {
                "b": binding_digest,
                "o": start + len(page_items),
                "s": snapshot_digest,
                "t": int(checkpoint),
                "v": 1,
            }
        )
    return EvidenceSearchResult(
        evidence=tuple(candidate.artifact for candidate in page_items),
        page=PageMetadata(continuation_token=continuation),
    ).to_wire()


def _decode(context: OperationContext) -> EvidenceSearchInput:
    """The request payload as a validated input, or a refusal that quotes nothing.

    The sentinel-then-raise shape is this tree's convention and it is load-bearing
    here: both contract errors quote the payload they rejected, and raising inside the
    handler would leave that text reachable through `__context__` on the exception a
    caller catches.
    """
    decoded: EvidenceSearchInput | None
    try:
        decoded = decode_evidence_search_input(context.request.input)
    except (ContractDecodeError, ContractSemanticError):
        decoded = None
    if decoded is None:
        raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID_INPUT)
    return decoded


def _projected(
    connection: Any,
    projection: SearchProjection,
    frontier: AuthorizedFrontier,
) -> ProjectedFrontier:
    """The frozen frontier with its projection material, or the same two refusals.

    The freshness gate above already ran, and `require_current` is not a repeat of it: it
    re-proves per request that the run this material was built from is still the
    activated one and still level with the workspace, so a projection that moved between
    the gate and this line refuses here instead of answering from material that no longer
    describes the workspace. Refusing twice costs two queries; serving once from stale
    material is the silent staleness §20.7 exists to forbid.

    `project` refuses too, for the other reason: an authorized candidate the material has
    no document for. That is retryable and it is deliberately not a fallback -- the
    artifact's authoritative `search_text` would answer the request from outside the
    projection this build claims to serve.

    The sentinel-then-raise shape is this tree's convention and it is load-bearing:
    raising inside the `except` would leave the projection's own message -- which names
    run ids and checkpoints -- reachable through `__context__` on the error a caller
    catches. `scripts/check-raise-discipline.py` enforces it over this directory.
    """
    refused: str | None = None
    try:
        require_current(connection, projection)
        return projection.project(frontier)
    except ProjectionUnavailable:
        refused = ERROR_CODE_PROJECTION_UNAVAILABLE
    except StaleProjection:
        refused = ERROR_CODE_STALE_PROJECTION
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


def _limit(request_input: EvidenceSearchInput) -> int:
    """The page size this request gets, clamped to what the catalogue allows."""
    requested = request_input.limit
    if requested is None:
        return DEFAULT_PAGE_LIMIT
    return min(int(requested), MAX_PAGE_LIMIT)


@dataclass(frozen=True)
class EvidenceHandlers:
    """The evidence family's bound handlers. One mutation, and the barrier behind it.

    `evidence_search` above stays a free function: it holds nothing, and giving it a
    `self` would only invite state onto a read path that has none. A capture cannot be
    written that way -- it needs the server's clock, its session, its binding and its
    identifier allocator, and every one of those is a server fact a request may not
    reach -- so those four live here and nowhere a handler could derive them from a
    payload.
    """

    service: Any
    session: AuthenticatedSession
    binding: ServiceBinding
    clock: Clock
    allocate_identifier: IdentifierAllocator

    def evidence_capture(self, context: OperationContext) -> AuditedOperationResult:
        """Record one submitted document as immutable L0 evidence, then make it findable.

        The order below is the operation, and each step is where it is for a reason:

        1. **decode, then compute.** The payload is validated by the generated contract
           code, and the checksum and byte length this service persists are then computed
           *here*, from the decoded bytes, rather than taken from anything the request
           said about them. An encoded payload is bounded before it is decoded, so the
           largest cost a hostile `content_base64` can impose is one comparison.
        2. **the grant, from server state.** `issue_mutation_grant` reads the session, the
           binding, the live guard row and the frozen catalogue; nothing about it is the
           caller's. Every attempt takes a fresh one, replays included, so a principal
           whose authority was withdrawn between two identical calls is refused at the
           second rather than served from the first one's stored answer.
        3. **conflicts before bytes; bytes before the row that names them.** The fenced
           mutation resolves an idempotency claim and the direct-source identity before
           `publish_blob` is allowed to run, so a rejected changed-body replay or source
           conflict cannot accumulate unreferenced objects. An accepted new source then
           publishes before its row is inserted. A stored replay verifies (and, when the
           object was reclaimed, repairs) its already-authoritative blob after resolution
           and before the projection barrier. Publication remains atomic and
           content-addressed in every branch.
        4. **one durable transaction**, through the standard coordinator: the audit
           event, the idempotency claim, the domain rows and the outcome all commit
           together or not at all.
        5. **the projection barrier, after that commit and outside it.** This is the one
           step that is particular to this operation, and :meth:`_publish_projection`
           carries its own argument.

        **The submitted text is inert.** It is decoded, hashed, written to a blob and
        indexed. Nothing in this path opens a URL, resolves a path from it, evaluates it,
        or reads a key out of it: the only fields consulted are the six the contract
        declares, and the source identity is built from `source_native_id` alone. A
        submission that spells out `file:///etc/passwd`, `{"role": "admin"}` or an
        absolute path is a document containing those characters and is treated as one.
        """
        submitted = self._decode(context)
        content = _content_bytes(submitted)
        witness = _lexical_witness(content, submitted.source_native_id)
        checksum = f"sha256:{hashlib.sha256(content).hexdigest()}"
        length = len(content)

        connection = getattr(self.service, "connection", None)
        identity = getattr(self.service, "identity", None)
        layout = getattr(self.service, "layout", None)
        blobs_root = getattr(layout, "blobs_path", None)
        guard = None if connection is None else read_guard(connection)
        if (
            connection is None
            or identity is None
            or guard is None
            or not isinstance(blobs_root, Path)
            or context.authorization is None
        ):
            raise OperationError(
                ERROR_CODE_INTERNAL_NON_RECOVERABLE, _MESSAGE_NO_STORAGE
            )

        equivalence, compatible_equivalences = _capture_idempotency_equivalences(
            context, submitted, content
        )
        grant = issue_mutation_grant(
            context.authorization,
            session=self.session,
            binding=self.binding,
            guard=guard,
            equivalence=equivalence,
            clock=self.clock,
        )

        def publish() -> None:
            published = True
            try:
                publish_blob(blobs_root, checksum, content)
            except (BlobPublicationRefused, OSError):
                # Contained rather than chained: the primitive's message names a path
                # under the workspace root, and this refusal becomes a wire error.
                published = False
            if not published:
                raise OperationError(
                    ERROR_CODE_INTERNAL_RECOVERABLE,
                    _MESSAGE_BLOB_UNPUBLISHED,
                    retry_class=RETRY_CLASS_RETRYABLE,
                )

        def mutate(
            fenced: Any, settlement: MutationSettlementContext
        ) -> Mapping[str, Any]:
            existing = _existing_direct_source(
                fenced,
                workspace_id=context.workspace_id,
                source_native_id=submitted.source_native_id,
            )
            if existing is not None:
                _require_identical(
                    existing,
                    submitted=submitted,
                    checksum=checksum,
                    length=length,
                )
                # Exact source reuse is accepted, not a conflict. Verify the bytes are
                # still present before settling its fresh claim, and repair a legitimately
                # reclaimed object through the same idempotent publication primitive.
                publish()
                return _capture_result(
                    evidence_id=existing.evidence_id,
                    submitted=submitted,
                    checksum=checksum,
                    length=length,
                    disposition="already_captured",
                )
            # This callback runs only after the mutation coordinator has resolved the
            # idempotency scope. Publishing here also follows the source-identity check
            # above, while still preceding every row that names these bytes below.
            publish()
            return _append_direct_evidence(
                fenced,
                settlement,
                workspace_id=context.workspace_id,
                submitted=submitted,
                checksum=checksum,
                length=length,
                principal=context.principal,
                allocate_identifier=self.allocate_identifier,
            )

        outcome = execute_mutation(
            connection,
            identity,
            grant=grant,
            context=context.authorization,
            equivalence=equivalence,
            compatible_equivalences=compatible_equivalences,
            mutate=mutate,
            validate_result=_valid_capture_result,
            clock=self.clock,
            allocate_identifier=self.allocate_identifier,
        )
        if outcome.replayed:
            # Replays deliberately skip the domain callback. Verify/repair their already
            # settled object here, after the idempotency check accepted the exact body and
            # before the projection barrier can report the stored success.
            publish()
        # The id the barrier must find, taken from the settled result rather than from
        # anything this attempt computed: on a replay that is the *first* attempt's
        # evidence id, which is exactly the document the caller will look for.
        captured = outcome.result.get("evidence_id")
        self._publish_projection(
            connection,
            identity,
            workspace_id=context.workspace_id,
            blobs_root=blobs_root,
            evidence_id=captured if isinstance(captured, str) else "",
            principal=context.principal,
            witness=witness,
        )
        return AuditedOperationResult(outcome.result, outcome.audit_ref)

    def _publish_projection(
        self,
        connection: sqlite3.Connection,
        identity: Any,
        *,
        workspace_id: str,
        blobs_root: Path,
        evidence_id: str,
        principal: str,
        witness: str,
    ) -> None:
        """Gate A: bring `evidence.search` level with this commit, or do not report success.

        **Why this is a separate step rather than part of the mutation.** The business
        commit and the projection advance are two transactions on purpose. The
        coordinator's transaction is fenced and carries the audit, the claim, the domain
        rows and the outcome; the projection lifecycle opens its own fenced transactions
        per phase, and nesting those inside the coordinator's would either deadlock the
        single write connection or make a projection failure roll back a mutation that
        the contract says settled. So the commit stands and this runs after it.

        **What that costs, and how it is paid.** A crash -- or a failure -- between the
        two leaves durable evidence that the projection has not caught up with, and this
        call then refuses rather than reporting a success the search handler would
        contradict. The refusal is honest and the state is recoverable, because this same
        barrier runs on *every* attempt including a same-key replay: the replay answers
        from the stored outcome, runs this step again, and returns the stored result once
        the projection is level. Nothing has to remember that the first attempt failed,
        because the recovery is derived from the database rather than from process state.

        **Which refusal.** Not a guess: after a failure the readiness of the projection is
        read back and reported as what it actually is -- `projection_unavailable` when
        nothing is activated at all, `stale_projection` when something is activated and is
        behind. Both are in this operation's allowed error set and both are retryable
        after a delay, which is exactly what a caller should do: replay the same key.

        **What counts as done, and why an open that returned is not it.** The projection
        composes each document out of two things -- the durable row's identity surface and
        the artifact's own stored bytes -- and it is deliberately tolerant of the second:
        a reclaimed, unreadable, symlinked or mistyped object yields a document indexed by
        identity alone rather than an unstartable workspace. That tolerance is right for a
        workspace and wrong for this barrier, because a capture whose content never
        reached the index would report success while every word the caller submitted
        answers "not found" -- and the identity surface, which carries the source id the
        caller chose, would still match a query naming it. So the check is the projection's
        own statement that *this* evidence id was composed from content, read off the value
        `open_search_projection` returned. It then runs the content-derived witness
        through the same authorized frontier and production ranker as
        `evidence.search`; no identity-only marker can satisfy that check.

        `build_search_projection` is idempotent and re-derives its position from the
        database, so calling it here is the same maintenance call startup makes, not a
        second mechanism. The guard is re-read rather than reused, so the generation this
        runs under is the live one and not the one the mutation began with.
        """
        failed = False
        try:
            guard = read_guard(connection)
            if guard is None:
                failed = True
            else:
                build_search_projection(
                    connection,
                    identity,
                    workspace_id=workspace_id,
                    fencing_generation=guard.fencing_generation,
                    now_us=int(self.clock.wall_time().timestamp() * 1_000_000),
                )
                projection = open_search_projection(
                    connection, workspace_id=workspace_id, blobs_root=blobs_root
                )
                candidates = read_evidence_candidates(
                    connection, workspace_id=workspace_id
                )
                checkpoint = int(
                    authoritative_checkpoint(connection, workspace_id=workspace_id)
                )
                frontier = authorized_frontier(
                    candidates,
                    workspace_id=workspace_id,
                    grant=local_owner_label_grant(
                        principal_id=principal,
                        workspace_id=workspace_id,
                        granted_workspace=workspace_id,
                    ),
                    resolution_time_us=checkpoint,
                )
                projected = _projected(connection, projection, frontier)
                matches = rank_projected(
                    projected, witness, limit=len(projected.candidates)
                )
                if evidence_id not in projection.content_indexed or not any(
                    candidate.evidence_id == evidence_id for candidate in matches
                ):
                    failed = True
        except (OperationError, ProjectionError, sqlite3.Error, OSError):
            # Contained rather than chained: the projection's own messages name run ids
            # and checkpoints, and this refusal travels to a caller. The category is
            # decided below, from the database, not from which exception arrived.
            failed = True
        if not failed:
            return
        code = self._projection_refusal(connection, workspace_id=workspace_id)
        raise OperationError(
            code,
            _MESSAGE_STALE_PROJECTION
            if code == ERROR_CODE_STALE_PROJECTION
            else _MESSAGE_PROJECTION_UNAVAILABLE,
            retry_class=RETRY_CLASS_RETRYABLE_AFTER_DELAY,
        )

    def _projection_refusal(
        self, connection: sqlite3.Connection, *, workspace_id: str
    ) -> str:
        """Which of the two canonical projection refusals this workspace is actually in.

        Read from the same `projection_readiness` gate `evidence.search` itself consults,
        so the code this capture returns is the code the next search would refuse with. A
        workspace whose projection is level but whose *session* material failed to open
        has nothing activated to serve from in this process, which is
        `projection_unavailable` rather than staleness.
        """
        try:
            readiness = projection_readiness(
                connection,
                workspace_id=workspace_id,
                source_checkpoint=authoritative_checkpoint(
                    connection, workspace_id=workspace_id
                ),
                contributing=CONTRIBUTING_PROJECTIONS,
            )
        except (sqlite3.Error, OSError):
            return ERROR_CODE_PROJECTION_UNAVAILABLE
        if readiness.stale:
            return ERROR_CODE_STALE_PROJECTION
        return ERROR_CODE_PROJECTION_UNAVAILABLE

    def _decode(self, context: OperationContext) -> EvidenceCaptureInput:
        """The payload as a validated capture input, or a refusal that quotes nothing.

        Sentinel-then-raise, this tree's convention and load-bearing here for the reason
        it is load-bearing above: the contract's decode and semantic errors quote the
        text -- so raising inside the `except` would leave that text reachable through
        `__context__` on the error a caller catches.
        """
        decoded: EvidenceCaptureInput | None
        error_code: str | None = None
        try:
            decoded = decode_evidence_capture_input(context.request.input)
        except EvidenceCaptureSizeLimitError:
            decoded = None
            error_code = ERROR_CODE_SIZE_LIMIT_EXCEEDED
        except (ContractDecodeError, ContractSemanticError):
            decoded = None
        if decoded is None:
            raise OperationError(
                ERROR_CODE_INVALID_REQUEST if error_code is None else error_code,
                _MESSAGE_INVALID_CAPTURE
                if error_code is None
                else _MESSAGE_CAPTURE_TOO_LARGE,
            )
        return decoded


@dataclass(frozen=True, slots=True)
class _StoredSource:
    """What one already-captured direct submission says about itself."""

    evidence_id: str
    content_checksum: str
    content_length_bytes: int | None
    media_type: str
    source_version: str | None
    event_at_ns: int | None
    observed_at_ns: int | None


def _content_bytes(submitted: EvidenceCaptureInput) -> bytes:
    """The submitted content as bytes, bounded before anything is allocated.

    Computed in the runtime rather than trusted from the contract's own decode, because
    the checksum and the byte length this service persists and returns are *its*
    statements about what it stored. The contract has already refused a payload outside
    these bounds; reaching a refusal here would mean the two disagreed, which is a build
    fault rather than a caller's mistake -- but it is still a refusal rather than an
    unbounded allocation.
    """
    if submitted.content_base64 is not None:
        encoded = submitted.content_base64
        # Before the decode, and therefore before the allocation. 1 MiB of content is at
        # most this many base64 characters, so anything longer cannot be within bound
        # whatever it decodes to.
        if len(encoded) > _MAX_ENCODED_LENGTH:
            raise OperationError(
                ERROR_CODE_SIZE_LIMIT_EXCEEDED, _MESSAGE_CAPTURE_TOO_LARGE
            )
        # Sentinel-then-raise, this directory's convention: `binascii.Error`'s message
        # quotes the encoded payload it rejected, and that payload is the caller's own
        # submitted document. `from None` would suppress the chain but not the frame the
        # traceback still carries, so the refusal is raised after the handler exits.
        decoded: bytes | None
        try:
            decoded = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            decoded = None
        if decoded is None:
            raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID_CAPTURE)
        content = decoded
    else:
        text = submitted.text
        if text is None:
            raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID_CAPTURE)
        content = text.encode("utf-8")
    if len(content) > EVIDENCE_CAPTURE_MAX_CONTENT_BYTES:
        raise OperationError(ERROR_CODE_SIZE_LIMIT_EXCEEDED, _MESSAGE_CAPTURE_TOO_LARGE)
    if len(content) < 1:
        raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID_CAPTURE)
    return content


def _lexical_witness(content: bytes, source_native_id: str) -> str:
    """One bounded production query token without narrowing valid content.

    Prefer content so the barrier exercises content lookup whenever the document
    has a lexical term. Punctuation-only content is still a valid immutable
    capture: ``content_indexed`` attests that its bytes were composed into the
    projection, and its required source identifier supplies the production
    search witness. ``first_query_token`` stops after one bounded token, so a
    token-dense one-MiB document does not become hundreds of thousands of Python
    objects merely to validate the post-commit barrier.
    """
    return first_query_token(content.decode("utf-8")) or source_native_id


def _capture_idempotency_equivalences(
    context: OperationContext, submitted: EvidenceCaptureInput, content: bytes
) -> tuple[IdempotencyEquivalence, tuple[IdempotencyEquivalence, ...]]:
    """Canonical capture identity plus the two pre-canonical wire spellings.

    ``text`` and ``content_base64`` carry the same contract value.  New settlements
    always fingerprint the canonical RFC 4648 spelling, which is compact enough for
    OVC1 and independent of which public form the caller chose.  Claims written by an
    older Core may instead contain the caller's exact spelling (including a text form
    or a valid non-canonical pad-bit spelling), so those contract-computed
    fingerprints are retained as read-only replay aliases.  They can answer an
    existing claim but are never written for a new one.
    """
    original = submitted.to_wire()
    canonical = dict(original)
    canonical.pop("text", None)
    canonical["content_base64"] = base64.b64encode(content).decode("ascii")

    text_form = dict(canonical)
    text_form.pop("content_base64")
    text_form["text"] = content.decode("utf-8")

    def equivalence_for(payload: Mapping[str, Any]) -> IdempotencyEquivalence:
        return idempotency_equivalence(
            context.request.operation,
            context.request.metadata,
            payload,
            principal_id=context.principal,
            workspace_id=context.workspace_id,
        )

    primary = equivalence_for(canonical)
    alternatives: list[IdempotencyEquivalence] = []
    for spelling in (original, text_form):
        candidate = equivalence_for(spelling)
        if candidate.fingerprint != primary.fingerprint and all(
            candidate.fingerprint != existing.fingerprint
            for existing in alternatives
        ):
            alternatives.append(candidate)
    return primary, tuple(alternatives)


def _microseconds(value: str | None) -> int | None:
    """One exact contract timestamp narrowed to the legacy storage projection."""
    if value is None:
        return None
    return canonical_timestamp_nanoseconds(value) // 1_000


def _stored_timestamp_claims(
    metadata_json: object,
    *,
    event_at_us: int | None,
    observed_at_us: int | None,
) -> tuple[int | None, int | None]:
    """Recover exact capture claims, with a safe fallback for pre-upgrade rows."""
    metadata: object = None
    try:
        metadata = json.loads(metadata_json) if isinstance(metadata_json, str) else None
    except (json.JSONDecodeError, TypeError, ValueError):
        metadata = None
    if not isinstance(metadata, dict):
        raise OperationError(
            ERROR_CODE_INTERNAL_NON_RECOVERABLE, _MESSAGE_SOURCE_NOT_UNIQUE
        )
    event = metadata.get("event_at")
    observed = metadata.get("observed_at")
    parsed: tuple[int | None, int | None] | None = None
    try:
        exact_event = (
            canonical_timestamp_nanoseconds(event)
            if isinstance(event, str)
            else None
            if event_at_us is None
            else event_at_us * 1_000
        )
        exact_observed = (
            canonical_timestamp_nanoseconds(observed)
            if isinstance(observed, str)
            else None
            if observed_at_us is None
            else observed_at_us * 1_000
        )
        parsed = (exact_event, exact_observed)
    except ContractSemanticError:
        parsed = None
    if parsed is None:
        raise OperationError(
            ERROR_CODE_INTERNAL_NON_RECOVERABLE, _MESSAGE_SOURCE_NOT_UNIQUE
        )
    return parsed


def _existing_direct_source(
    connection: sqlite3.Connection, *, workspace_id: str, source_native_id: str
) -> _StoredSource | None:
    """The artifact this source identity already names, or `None`.

    The predicate is the whole direct-submission identity 0041 made unique -- workspace,
    kind, native id, and a locator and retrieval instant that are both NULL -- and
    nothing else. In particular it is not scoped by principal: two principals submitting
    under one source id are submitting the same source, and making the identity
    principal-relative would let the same source become two authoritative artifacts.

    More than one row is an invariant failure rather than a caller's conflict. 0041's
    unique index makes it unreachable through this path; if the database is nevertheless
    holding two, the honest answer is that this build cannot say which is the source, not
    to pick one.
    """
    rows = connection.execute(
        "SELECT a.evidence_id, a.content_checksum, a.media_type, a.event_at_us, "
        "       a.observed_at_us, b.content_length_bytes, s.source_version, "
        "       a.original_metadata_json "
        "FROM omnivia_evidence_artifacts a "
        "LEFT JOIN omnivia_blob_objects b "
        "  ON b.workspace_id = a.workspace_id "
        " AND b.content_digest = a.blob_content_digest "
        "LEFT JOIN omnivia_staged_sources s "
        "  ON s.workspace_id = a.workspace_id "
        " AND s.staged_source_ref = a.staged_source_ref "
        "WHERE a.workspace_id = ? AND a.source_kind = ? AND a.source_native_id = ? "
        "  AND a.source_locator IS NULL AND a.source_retrieved_at_us IS NULL",
        (workspace_id, CAPTURE_SOURCE_KIND, source_native_id),
    ).fetchall()
    if not rows:
        return None
    if len(rows) != 1:
        raise OperationError(
            ERROR_CODE_INTERNAL_NON_RECOVERABLE, _MESSAGE_SOURCE_NOT_UNIQUE
        )
    row = rows[0]
    event_at_us = None if row[3] is None else int(row[3])
    observed_at_us = None if row[4] is None else int(row[4])
    event_at_ns, observed_at_ns = _stored_timestamp_claims(
        row[7], event_at_us=event_at_us, observed_at_us=observed_at_us
    )
    return _StoredSource(
        evidence_id=str(row[0]),
        content_checksum=str(row[1]),
        media_type=str(row[2]),
        event_at_ns=event_at_ns,
        observed_at_ns=observed_at_ns,
        content_length_bytes=None if row[5] is None else int(row[5]),
        source_version=None if row[6] is None else str(row[6]),
    )


def _require_identical(
    stored: _StoredSource,
    *,
    submitted: EvidenceCaptureInput,
    checksum: str,
    length: int,
) -> None:
    """Refuse unless this submission is the one already captured, in every stated respect.

    Six comparisons and all six must hold. Reuse is not "the bytes look the same": an
    identical document submitted under a different `source_version`, a different declared
    event time or a different media type is a *different claim* about the same source,
    and returning the stored artifact for it would silently discard the difference. The
    conflict carries no value from either side.
    """
    if (
        stored.content_checksum != checksum
        or stored.content_length_bytes != length
        or stored.media_type != submitted.media_type
        or stored.source_version != submitted.source_version
        or stored.event_at_ns
        != (
            None
            if submitted.event_at is None
            else canonical_timestamp_nanoseconds(submitted.event_at)
        )
        or stored.observed_at_ns
        != (
            None
            if submitted.observed_at is None
            else canonical_timestamp_nanoseconds(submitted.observed_at)
        )
    ):
        raise OperationError(ERROR_CODE_CONFLICT, _MESSAGE_SOURCE_CONFLICT)


def _capture_result(
    *,
    evidence_id: str,
    submitted: EvidenceCaptureInput,
    checksum: str,
    length: int,
    disposition: str,
) -> Mapping[str, Any]:
    """The canonical result, built once for both dispositions."""
    return EvidenceCaptureResult(
        evidence_id=evidence_id,
        source=SourceReference(
            kind=CAPTURE_SOURCE_KIND, source_id=submitted.source_native_id
        ),
        media_type=submitted.media_type,
        content_checksum=checksum,
        content_length_bytes=length,
        capture_disposition=disposition,
    ).to_wire()


def _valid_capture_result(wire: Mapping[str, Any]) -> bool:
    """Whether the server will serve this as an `evidence.capture` result."""
    try:
        validate_evidence_capture_result(EvidenceCaptureResult.from_wire(wire))
    except (ContractDecodeError, ContractSemanticError):
        return False
    return True


def _append_direct_evidence(
    connection: sqlite3.Connection,
    settlement: MutationSettlementContext,
    *,
    workspace_id: str,
    submitted: EvidenceCaptureInput,
    checksum: str,
    length: int,
    principal: str,
    allocate_identifier: IdentifierAllocator,
) -> Mapping[str, Any]:
    """Write the five 0008 rows one capture consists of, inside the caller's transaction.

    The same shape and the same order the maintenance local-file capture writes, because
    these are the same durable facts: the content identity, the verification that earned
    it, the staged descriptor that carries the provenance claims, the artifact, and the
    artifact's first provenance event. What differs is only what a direct submission *is*:
    `source_kind` is `direct_submission`, and `source_locator` and `source_retrieved_at_us`
    are NULL -- there is no locator to record and no retrieval that happened, and 0041
    makes that tuple the unique identity rather than a convention.

    No principal is written into the source identity. The actor is recorded on the
    provenance event, which is where "who did this" belongs; putting it in the identity
    would make one source two.

    The metadata carries the submission's own source id and nothing from its content: it
    is returned to every reader of the artifact, so the submitted text must not be in it.
    """
    now_us = settlement.settled_at_us
    staged_source_ref = allocate_identifier("stg")
    evidence_id = allocate_identifier("evd")
    capture_metadata = {
        "capture": CAPTURE_SOURCE_KIND,
        "source_id": submitted.source_native_id,
    }
    if submitted.event_at is not None:
        capture_metadata["event_at"] = submitted.event_at
    if submitted.observed_at is not None:
        capture_metadata["observed_at"] = submitted.observed_at
    metadata = to_canonical_json(capture_metadata)
    metadata_digest = f"sha256:{hashlib.sha256(metadata.encode('utf-8')).hexdigest()}"

    blob = connection.execute(
        "SELECT content_length_bytes FROM omnivia_blob_objects "
        "WHERE workspace_id = ? AND content_digest = ?",
        (workspace_id, checksum),
    ).fetchone()
    if blob is None:
        connection.execute(
            "INSERT INTO omnivia_blob_objects "
            "(workspace_id, content_digest, content_length_bytes, created_at_us, "
            "verified_at_us) VALUES (?, ?, ?, ?, ?)",
            (workspace_id, checksum, length, now_us, now_us),
        )
    elif int(blob[0]) != length:
        # One content address, two byte lengths: the workspace disagrees with itself
        # about what these bytes are. Not the caller's doing and not repairable here.
        raise OperationError(
            ERROR_CODE_INTERNAL_NON_RECOVERABLE, _MESSAGE_SOURCE_NOT_UNIQUE
        )

    integrity_sequence = int(
        connection.execute(
            "SELECT COALESCE(MAX(integrity_sequence), 0) + 1 "
            "FROM omnivia_blob_integrity_events "
            "WHERE workspace_id = ? AND content_digest = ?",
            (workspace_id, checksum),
        ).fetchone()[0]
    )
    connection.execute(
        "INSERT INTO omnivia_blob_integrity_events "
        "(integrity_event_id, workspace_id, content_digest, integrity_sequence, "
        "outcome, observed_digest, observed_length_bytes, expected_length_bytes, "
        "inventory_id, checked_at_us) VALUES (?, ?, ?, ?, 'verified', ?, ?, ?, NULL, ?)",
        (
            allocate_identifier("bie"),
            workspace_id,
            checksum,
            integrity_sequence,
            checksum,
            length,
            length,
            now_us,
        ),
    )
    connection.execute(
        "INSERT INTO omnivia_staged_sources "
        "(staged_source_ref, workspace_id, source_kind, declared_checksum, "
        "content_length_bytes, media_type, source_version, computed_checksum, "
        "original_metadata_json, original_metadata_digest, staging_outcome, "
        "blob_workspace_id, blob_content_digest, recorded_at_us) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'verified', ?, ?, ?)",
        (
            staged_source_ref,
            workspace_id,
            CAPTURE_SOURCE_KIND,
            checksum,
            length,
            submitted.media_type,
            submitted.source_version,
            checksum,
            metadata,
            metadata_digest,
            workspace_id,
            checksum,
            now_us,
        ),
    )
    connection.execute(
        "INSERT INTO omnivia_evidence_artifacts "
        "(evidence_id, workspace_id, source_kind, source_native_id, source_locator, "
        "source_retrieved_at_us, event_at_us, observed_at_us, ingested_at_us, "
        "recorded_at_us, content_checksum, blob_content_digest, media_type, "
        "original_metadata_json, original_metadata_digest, sensitivity, parser_status, "
        "ingestion_status, staged_source_ref, import_run_id) "
        "VALUES (?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'private', "
        "'not_parsed', 'ingested', ?, NULL)",
        (
            evidence_id,
            workspace_id,
            CAPTURE_SOURCE_KIND,
            submitted.source_native_id,
            _microseconds(submitted.event_at),
            _microseconds(submitted.observed_at),
            now_us,
            now_us,
            checksum,
            checksum,
            submitted.media_type,
            metadata,
            metadata_digest,
            staged_source_ref,
        ),
    )
    connection.execute(
        "INSERT INTO omnivia_evidence_provenance_events "
        "(provenance_event_id, evidence_id, workspace_id, provenance_sequence, actor_id, "
        "actor_kind, action, occurred_at_us, reason_code, reason_comment, parser_status, "
        "ingestion_status, tombstoned_observation, source_kind, source_native_id, "
        "audit_ref) VALUES (?, ?, ?, 1, ?, 'agent', 'captured', ?, NULL, NULL, "
        "'not_parsed', 'ingested', 0, ?, ?, ?)",
        (
            allocate_identifier("prv"),
            evidence_id,
            workspace_id,
            principal,
            now_us,
            CAPTURE_SOURCE_KIND,
            submitted.source_native_id,
            settlement.audit_ref,
        ),
    )
    return _capture_result(
        evidence_id=evidence_id,
        submitted=submitted,
        checksum=checksum,
        length=length,
        disposition="created",
    )


__all__ = [
    "CAPTURE_SOURCE_KIND",
    "DEFAULT_PAGE_LIMIT",
    "EVIDENCE_CAPTURE_OPERATION",
    "EVIDENCE_SEARCH_OPERATION",
    "MAX_PAGE_LIMIT",
    "EvidenceHandlers",
    "evidence_search",
]
