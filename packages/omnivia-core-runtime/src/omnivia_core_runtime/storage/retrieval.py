"""The authorized candidate frontier as a value, and the filter chain that builds it.

This module is the security seam of V06-3, and the property it exists to make
*structural* rather than a matter of discipline is packet section 7.2:

    No ranking, reranking, selection, scoring, diversification, snippeting or budget
    decision may observe a candidate that has not already passed every workspace,
    scope, purpose, capability, policy, ACL, sensitivity, temporal and governance
    filter. The authorized candidate frontier SHALL be materialised and frozen as a
    single value before the first such decision, and every item in any result SHALL
    be a member of that frozen frontier.

Read the import block above and note what is *not* in it. This module imports the
frozen contract and the standard library and nothing else. There is no `sqlite3`, no
`omnivia_core_runtime.storage.connection`, no `omnivia_core_runtime.storage.repository`
and no handle of any kind -- not as a parameter, not as a module global, not captured
in a closure, and not hidden behind a callback. That is §20.12's four prohibitions,
and the reason they are stated as an *import boundary* rather than as a signature is
the owner's ruling in the same section: *"The function signature, import boundary and
F4 mutation collectively provide the proof. A signature-only inspection is
insufficient."* A signature says nothing about a module-level store; the absence of
the import does.

So neither ranker here can reach an unfiltered candidate. Not "must not" -- there is
no expression either could contain that would get one. The dependency runs one way:
`repository.py` imports this module to build `EvidenceCandidate` values, and this
module imports nothing back.

**Both rankers are here, and the production one is `rank_projected`.** Lane B ships an
FTS5 projection, and the first attempt at it put the production ranker inside
`storage/projections/fts.py` holding the connection its index lived on -- which gave up
§20.12's import-boundary proof and, worse, scored with `bm25()`'s *workspace-wide*
statistics, so an artifact a filter had excluded still moved the relative order of the
members that were returned. Neither is repaired by an assertion over a result page.

The shape that keeps the invariant is a split. The projection adapter narrows its
service-materialised per-document token material by exactly the frozen frontier's ids
and hands back a `ProjectedFrontier` -- an immutable value carrying the same candidates
and their token sequences and nothing else: no connection, no store, no callback, no
lookup that could still reach the corpus. `rank_projected` takes that value, and every
statistic BM25 needs -- document frequency, average document length, the length
normalisation -- is recomputed from it. An excluded artifact is therefore not merely
unreturnable; it is absent from every number the ordering is computed from.

**The digest is the check, and the attestation is not.** `AuthorizedFrontier.checksum`
is computed by the contract's own `compute_authorized_candidate_set_checksum` over the
contract's own manifest shape, whose preimage is workspace domain separation plus
immutable identities and *nothing a later ranking or selection step could change*. That
is what makes packet §8.1's F2 falsifiable: widen the frontier by one unauthorized item
that ranking scores last and selection drops, and the returned page is byte-identical
while the checksum moves. No assertion over a result page can catch that, which is
precisely why the digest exists and why a boolean attestation field would not do (F3).

**The ACL stage always runs.** Packet §20.3 names five forbidden implementations of the
Personal-mode all-label grant -- an absent check, a global local bypass, an anonymous
default, an unknown-principal default and a skipped ACL stage -- and this module
implements none of them. `EvidenceLabelGrant` is an explicit effective value that the
filter chain evaluates on every candidate, and `local_owner_label_grant` hands the
all-label grant to the exact configured local-owner principal in its exact granted
workspace and the **empty** grant to everyone else. Deny by default, evaluated, never
skipped.

**The governed frontier is the same shape, one partition over.** `GovernedFrontier` and
`rank_governed` are to L2 governed records what `AuthorizedFrontier` and the two evidence
rankers are to L0 artifacts, and they are here rather than in `governed.py` for exactly the
reason that module is not: `governed.py` imports `sqlite3` and holds a connection, so a
ranker written there would have a store within reach and §20.12's import-boundary proof
would be gone. The seam is the same one Lane B established -- resolution and authorization
happen elsewhere, hand this module a frozen value, and the ordering is computed from
nothing else. `rank_governed`'s parameters are that frozen value, a string, an optional
order selector and an integer; there is no connection, repository, projection, callback or
lazy lookup among them, and the import block above is why there could not be.

`EVIDENCE-LABEL-GRANT-DEFERRED` bounds this: production Personal mode has one principal
and that principal receives every label, so this release does not prove differentiated
evidence access between production principals. A real principal-to-label grant
mechanism is required before shared-host, multi-user or Organisation-mode deployment.
"""

from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass
from typing import Final

from omnivia_core.contracts.v1 import (
    CONTEXT_PACK_AUTHORIZED_CANDIDATE_SET_FORMAT,
    CONTEXT_PACK_CANDIDATE_PARTITION_EVIDENCE,
    KNOWLEDGE_SEARCH_ORDER_RELEVANCE,
    KNOWLEDGE_SEARCH_ORDERS,
    ContextPackAuthorizedCandidateSetManifest,
    ContextPackAuthorizedEvidenceCandidate,
    EvidenceArtifact,
    GovernedRecord,
    compute_authorized_candidate_set_checksum,
    to_canonical_json,
)

#: The configured Personal-mode local-owner principal, and the *only* principal that
#: holds an evidence-label grant.
#:
#: It is a constant here rather than a value read off the session because a check
#: against the session's own principal is not a check: it compares a value with itself
#: and admits whoever the session happens to name, which is packet §20.3's forbidden
#: "unknown-principal default" wearing a comparison. The discriminator has to come from
#: somewhere the session cannot move.
#:
#: `service/main.py:71` states the same string as `LOCAL_PRINCIPAL`, and the two must
#: agree. Nothing in the language makes them, so `test_retrieval_filter_chain.py` pins
#: the equality directly -- a duplicated constant with a guard over it, rather than an
#: import from the process entrypoint into the storage layer.
CONFIGURED_LOCAL_OWNER: Final = "local-user"

#: The filters this chain applies, in the order it applies them, named so the frontier
#: can state what it was narrowed by rather than leaving a reviewer to infer it from
#: the code. Every one of them runs *before* the freeze; there is no post-ranking
#: member of this tuple and adding one would be the defect §7.2 exists to prevent.
FRONTIER_FILTERS: Final[tuple[str, ...]] = (
    "workspace",
    "evidence_label_acl",
    "sensitivity",
    "tombstone",
    "temporal",
)


@dataclass(frozen=True, slots=True)
class EvidenceCandidate:
    """One L0 evidence artifact as a candidate, with the facts the filters decide on.

    `artifact` is the fully hydrated contract DTO, carried here rather than fetched
    again after ranking. That is deliberate and it is the stronger shape: the result
    page is built by mapping over frozen frontier members, so "every item in any result
    is a member of the frozen frontier" is true by construction rather than by a
    membership check that could be forgotten. It also means the read layer runs exactly
    once, before the freeze, and nothing on this path can issue a second query.

    Carrying it costs nothing in the digest: the candidate-set preimage is
    `(partition, evidence_id, content_checksum)` and excludes content by contract, so
    two honest builds over the same frontier agree whatever the artifacts hold.

    `search_text` is the *identity* surface a Lane A query matches against -- source
    kind, native id and locator, case-folded and NFKC-normalized. It is not artifact
    content: no blob is fetched on this path, and packet §7.3 forbids introducing a
    caller-supplied path here.

    Lane B indexes this same surface and no other, so the projection is a different
    *index* over the same text rather than a different text. It replaces the ordering
    behind this same frozen frontier and nothing else; the seam is the ordering only.
    """

    evidence_id: str
    workspace_id: str
    content_checksum: str
    sensitivity: str
    permission_labels: tuple[str, ...]
    tombstoned: bool
    recorded_at_us: int
    search_text: str
    artifact: EvidenceArtifact


@dataclass(frozen=True, slots=True)
class EvidenceLabelGrant:
    """The explicit effective evidence-label grant one principal holds in one workspace.

    A *value the ACL stage evaluates*, which is the whole of what packet §20.3 Option A
    requires and what its five forbidden implementations all avoid being. `all_labels`
    is not a bypass: `permits` still runs, still reads the candidate's labels, and still
    answers per candidate. The difference between the configured local owner and anyone
    else is what this value says, never whether the stage executes.
    """

    principal_id: str
    workspace_id: str
    all_labels: bool
    labels: frozenset[str]

    def permits(self, labels: tuple[str, ...]) -> bool:
        """Whether this grant admits an artifact carrying exactly `labels`.

        An unlabelled artifact carries no restriction and is admitted by any grant --
        there is nothing for the grant to fail to hold. A labelled artifact needs
        *every* one of its labels held, not one of them: labels restrict, so holding a
        subset is holding less than the artifact demands.
        """
        if self.all_labels:
            return True
        return frozenset(labels) <= self.labels


def local_owner_label_grant(
    *,
    principal_id: str,
    workspace_id: str,
    granted_workspace: str,
    configured_principal: str = CONFIGURED_LOCAL_OWNER,
) -> EvidenceLabelGrant:
    """The evidence-label grant a principal holds, deny-by-default for everyone else.

    Packet §20.3, verbatim: *"The configured Personal-mode local-owner principal
    receives an explicit effective grant containing all evidence labels for its granted
    workspace"*, and *"Any principal other than the exact configured local owner
    defaults to no evidence-label grant."*

    Both halves of "exact" are tested, and both matter. A principal that is the
    configured local owner but is asking about a workspace this endpoint was not
    launched to own gets the empty grant, because the decision grants all labels *for
    its granted workspace* and not for storage generally. Anything else would be the
    "global local bypass" the same decision forbids by name.

    The empty grant is a real grant that denies every labelled artifact, not an absent
    check and not an unknown-principal default that silently admits: `permits` runs
    against it exactly as it runs against the all-label grant.
    """
    owner = principal_id == configured_principal and workspace_id == granted_workspace
    return EvidenceLabelGrant(
        principal_id=principal_id,
        workspace_id=workspace_id,
        all_labels=owner,
        labels=frozenset(),
    )


@dataclass(frozen=True, slots=True)
class AuthorizedFrontier:
    """The frozen authorized candidate frontier, and its own digest.

    Frozen in both senses: the dataclass is immutable, and this value is materialised
    before the first ranking, reranking, selection or budget decision on the path. It is
    the complete set of material this principal is authorized to see for this request --
    not the whole workspace, and not what a result ended up showing.

    `checksum` is computed by the contract, over the contract's manifest shape, at the
    moment of the freeze. It is in-process verifier input and is never a response field:
    reading a frontier digest back out of the artifact it is supposed to check verifies
    nothing, which the contract itself says in as many words.
    """

    workspace_id: str
    candidates: tuple[EvidenceCandidate, ...]
    checksum: str
    filters_applied: tuple[str, ...]
    #: The candidates the chain removed, kept for the accounting a reviewer needs to
    #: see that a filter did something. Never ranked, never returned, never digested.
    excluded: tuple[EvidenceCandidate, ...]


@dataclass(frozen=True, slots=True)
class ProjectedCandidate:
    """One authorized candidate and the projection's token sequence for it.

    `terms` is the document exactly as the projection's FTS5 tokenizer split it, in
    order. One tuple carries everything BM25 asks of a document -- term frequency is a
    count over it, length normalisation is its length, and phrase matching is a slice
    comparison -- so there is no second structure to keep in step and nothing here a
    ranker could follow back to a row.
    """

    candidate: EvidenceCandidate
    terms: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ProjectedFrontier:
    """The frozen frontier with its projection material beside it, and nothing else.

    Produced by the projection adapter *after* the freeze, from the frontier's own ids.
    Membership is identical to the `AuthorizedFrontier` it was narrowed from -- which is
    why it carries that frontier's `checksum` verbatim rather than recomputing one: the
    two are the same set, and a second digest computed over a second shape could only
    ever agree by accident or disagree by construction.

    What it deliberately does not carry is any way to obtain a candidate it does not
    already hold. There is no connection on it, no store, no callback and no id it could
    resolve; a ranker handed one has the whole of its input in the value.
    """

    workspace_id: str
    checksum: str
    candidates: tuple[ProjectedCandidate, ...]


def candidate_set_manifest(
    workspace_id: str, candidates: tuple[EvidenceCandidate, ...]
) -> ContextPackAuthorizedCandidateSetManifest:
    """The out-of-band manifest naming exactly this frontier.

    Produced by the retrieval layer at the moment the frontier is frozen, which is
    packet §8.1's binding clause: a manifest reconstructed from a result, from the
    selected items, or from a fixture written by reading a previous result asserts
    equality with itself and is not evidence.
    """
    return ContextPackAuthorizedCandidateSetManifest(
        format=CONTEXT_PACK_AUTHORIZED_CANDIDATE_SET_FORMAT,
        workspace_id=workspace_id,
        candidates=tuple(
            ContextPackAuthorizedEvidenceCandidate(
                partition=CONTEXT_PACK_CANDIDATE_PARTITION_EVIDENCE,
                evidence_id=candidate.evidence_id,
                content_checksum=candidate.content_checksum,
            )
            for candidate in candidates
        ),
    )


def authorized_frontier(
    candidates: tuple[EvidenceCandidate, ...],
    *,
    workspace_id: str,
    grant: EvidenceLabelGrant,
    sensitivity: str | None = None,
    include_tombstoned: bool = False,
    resolution_time_us: int,
) -> AuthorizedFrontier:
    """Apply every filter, then freeze.

    The order below is `FRONTIER_FILTERS` and it is the order a reviewer should be able
    to read off the source. Two properties are worth naming because they are what the
    negative tests turn on:

    **The filters compose; none substitutes for another.** `include_tombstoned` widens
    the tombstone filter and touches nothing else, so a tombstoned artifact the ACL
    stage excluded stays excluded with `include_tombstoned` set -- packet §12.3 test 10.
    Each filter is a separate `continue`, and a candidate has to survive all of them.

    **The temporal filter is a frontier filter, not an ordering hint.** A record
    recorded after the resolution instant is *absent from the frontier*, not merely
    ranked low -- §12.3 test 12. It is therefore absent from the digest too, which is
    what makes the difference observable.

    There is no filter after this function returns. Anything that narrows the set later
    is a selection decision operating on already-authorized material, which is what
    ranking is allowed to be.
    """
    admitted: list[EvidenceCandidate] = []
    excluded: list[EvidenceCandidate] = []
    for candidate in candidates:
        if candidate.workspace_id != workspace_id:
            excluded.append(candidate)
            continue
        if not grant.permits(candidate.permission_labels):
            excluded.append(candidate)
            continue
        if sensitivity is not None and candidate.sensitivity != sensitivity:
            excluded.append(candidate)
            continue
        if candidate.tombstoned and not include_tombstoned:
            excluded.append(candidate)
            continue
        if candidate.recorded_at_us > resolution_time_us:
            excluded.append(candidate)
            continue
        admitted.append(candidate)

    frozen = tuple(admitted)
    return AuthorizedFrontier(
        workspace_id=workspace_id,
        candidates=frozen,
        checksum=compute_authorized_candidate_set_checksum(
            candidate_set_manifest(workspace_id, frozen)
        ),
        filters_applied=FRONTIER_FILTERS,
        excluded=tuple(excluded),
    )


def normalize_query(query: str) -> str:
    """One spelling of a query, so matching is a property of the text and not its form.

    NFKC then case-folded, which is what the contract's own `EvidenceQuery` describes as
    normalization. Applied identically to the query and to every candidate's
    `search_text`, because normalizing one side only makes the match depend on which
    side a caller happened to type.
    """
    return unicodedata.normalize("NFKC", query).casefold()


def relevance_order_key(
    candidate: EvidenceCandidate, relevance: float
) -> tuple[float, int, str]:
    """The one total order every ranking in this build sorts by.

    `relevance` ascending first, because SQLite's `bm25()` returns a *negative*
    score whose magnitude grows with relevance -- so ascending is best-first, and a
    candidate with no relevance signal at all takes `0.0` and sorts behind every
    match rather than in front of it. With a constant relevance the key degrades
    exactly to recency-then-identity, which is why Lane A's ordering is expressible
    as this key and not merely similar to it.

    Then `recorded_at_us` descending, then `evidence_id` ascending. Both
    tie-breakers are load-bearing rather than decorative: **bm25 ties are the common
    case**, not the exotic one -- two artifacts whose identity surfaces contain the
    same terms the same number of times score identically to the last bit -- and
    equal instants are equally common because bulk ingestion writes many artifacts
    at one microsecond. A sort keyed on relevance alone returns whatever order the
    rows arrived in, which is packet §8.2's first ordering hazard wearing a score.

    It lives here, in the module that owns the frozen frontier, so that the FTS5
    ranking in `storage/projections/fts.py` and the ordering below are one fact
    with one definition. `EvidenceCandidate` is the only thing it reads; it holds
    no store and reaches nothing.
    """
    return (relevance, -candidate.recorded_at_us, candidate.evidence_id)


def rank_candidates(
    frontier: AuthorizedFrontier, query: str, *, limit: int
) -> tuple[EvidenceCandidate, ...]:
    """Select and totally order the frozen frontier. The ranker.

    This is the function packet §7.2 and §20.12 constrain, and every constraint on it is
    visible from here: its parameters are a frozen value, a string and an integer; its
    module imports no repository, no storage module and no `sqlite3`; it holds no
    module-global or closure-captured store; and none of its parameters is a callback,
    so there is nothing it could call to retrieve a candidate that is not already in
    `frontier.candidates`. Reaching one is not forbidden to it -- it is unreachable.

    **The order is total, and that is a determinism requirement rather than a nicety.**
    `recorded_at_us` descending puts recent evidence first; `evidence_id` ascending
    breaks every tie on a stable immutable identity. Not insertion order, not `dict`
    iteration order, and not a SQLite row order no `ORDER BY` pins -- packet §8.2 names
    all three as where determinism actually breaks. Ties are common, not exotic: bulk
    ingestion writes many artifacts at one microsecond.

    **The query selects; it does not score.** Lane A ships no index (§2.3, §10.1), so
    matching is a normalized substring test over each candidate's identity surface and
    the ordering is recency-then-identity rather than relevance. That is a valid,
    contract-conformant `evidence.search` -- the catalogue fixes no ordering requirement
    -- and it is stated plainly here rather than dressed up: this is not relevance
    ranking, and Lane B replaces the ordering behind this same frozen frontier.
    """
    needle = normalize_query(query)
    matched = [
        candidate
        for candidate in frontier.candidates
        if needle in normalize_query(candidate.search_text)
    ]
    # One relevance for every candidate, because Lane A ships no index and has no
    # score to distinguish them with. The key is shared with the FTS5 ordering
    # rather than reimplemented, so the tie-breakers cannot drift apart.
    matched.sort(key=lambda candidate: relevance_order_key(candidate, 0.0))
    return tuple(matched[:limit])


#: BM25's free parameters, at the values SQLite's own `bm25()` uses. Identical on
#: purpose: this ranker computes the same function FTS5 computes and differs only in the
#: corpus the statistics come from, so the constants are not a place to have an opinion.
BM25_K1: Final = 1.2
BM25_B: Final = 0.75

#: The smallest inverse document frequency this ranker will use, and the reason it has a
#: floor at all. `ln((N - df + 0.5) / (df + 0.5))` goes negative once a term appears in
#: more than about half the documents, and a negative idf inverts the whole ordering --
#: the *worst* match would sort first. FTS5 clamps at exactly this value; so does this.
BM25_MINIMUM_IDF: Final = 1e-6

#: One token: a run of Unicode alphanumerics. That is what `unicode61` treats as token
#: characters with `remove_diacritics 0`, so this splits a query exactly as the
#: projection's own FTS5 tokenizer split the documents it is matched against.
#: `test_fts_projection_lifecycle.py` pins the two against each other over a real index
#: rather than trusting the equivalence, because a drift here is a silent recall bug.
_TOKEN: Final = re.compile(r"[^\W_]+")


def query_tokens(query: str) -> tuple[str, ...]:
    """A query as the token sequence the projection's documents were tokenized into.

    Normalized first, by the same `normalize_query` the materialisation applies to every
    document, so a match is a property of the text rather than of which side a caller
    happened to type.
    """
    return tuple(_TOKEN.findall(normalize_query(query)))


def rank_projected(
    projected: ProjectedFrontier, query: str, *, limit: int
) -> tuple[EvidenceCandidate, ...]:
    """Select and totally order the projected frontier by BM25. The production ranker.

    Read the import block at the top of this module and this function's three
    parameters together, because that pair is the whole of §20.12's proof. The
    parameters are an immutable value, a string and an integer. The module imports the
    frozen contract and the standard library -- no `sqlite3`, no
    `storage.connection`, no `storage.repository`, no `storage.projections`. There is no
    module-global store, no closure-captured handle, no callback parameter and no
    default that could smuggle one in. An unfiltered candidate is not forbidden here; it
    is unreachable.

    **Every statistic is recomputed from the narrowed value.** `total` is the projected
    frontier's size, the document frequency behind `idf` is how many of *those* members
    match, and `average` is the mean document length across them. None of the three is a
    property of the workspace, so an
    artifact a filter excluded cannot be counted, cannot lengthen the average and cannot
    shift the length normalisation of a member that was admitted. That is the failure the
    first Lane B design shipped: it constrained *which rows FTS5 returned* to the
    frontier while letting `bm25()` take its idf and its average document length from the
    whole index, so an excluded artifact carrying the query term still moved the returned
    members relative to one another -- invisibly, because the page still contained only
    authorized ids.

    **The query is one phrase, matched on token boundaries.** `terms[start:start + span]`
    asks for the query's tokens contiguous and in order, which is the same narrowing of
    Lane A's substring test that the FTS5 phrase query made, and it is what makes a
    caller-supplied string harmless: there is no query grammar here to reach. `AND`,
    `NOT`, `NEAR`, a column filter and a prefix wildcard are all just tokens that no
    document contains next to the rest of the query.

    The score is negated on the way out because `relevance_order_key` sorts relevance
    ascending -- the convention SQLite's `bm25()` established and the one Lane A's
    constant `0.0` relies on to sort unscored candidates behind every match.
    """
    phrase = query_tokens(query)
    total = len(projected.candidates)
    if not phrase or total == 0:
        return ()
    matched = [
        (item, hits)
        for item in projected.candidates
        if (hits := _phrase_hits(item.terms, phrase))
    ]
    if not matched:
        return ()

    average = sum(len(item.terms) for item in projected.candidates) / total
    idf = max(
        math.log((total - len(matched) + 0.5) / (len(matched) + 0.5)), BM25_MINIMUM_IDF
    )
    scored = [
        (
            item.candidate,
            -idf
            * (hits * (BM25_K1 + 1.0))
            / (
                hits
                + BM25_K1 * (1.0 - BM25_B + BM25_B * len(item.terms) / average)
            ),
        )
        for item, hits in matched
    ]
    scored.sort(key=lambda pair: relevance_order_key(pair[0], pair[1]))
    return tuple(candidate for candidate, _ in scored[:limit])


def _phrase_hits(terms: tuple[str, ...], phrase: tuple[str, ...]) -> int:
    """How many times `phrase` occurs in `terms`, contiguous and in order."""
    span = len(phrase)
    return sum(
        1
        for start in range(len(terms) - span + 1)
        if terms[start : start + span] == phrase
    )


#: The filters the caller must have applied before it may freeze a `GovernedFrontier`, in
#: the order it applies them. It is the governed partition's `FRONTIER_FILTERS` and it is
#: stated for the same reason: the frontier has to say what narrowed it rather than leave a
#: reviewer to infer it, and §7.2's ordering property is a claim about *these* filters
#: running before the freeze.
#:
#: `rank_governed` cannot verify the claim, and that is not a gap this module should try to
#: close. Verifying it would mean re-reading governance, temporal or scope facts from
#: somewhere, which is exactly the store the import boundary above exists to keep out of
#: reach. The filters run in the handler, the frontier records that they did, and the
#: ranker's guarantee is the narrower, checkable one: it observes nothing but this value.
GOVERNED_FRONTIER_FILTERS: Final[tuple[str, ...]] = (
    "workspace",
    "view",
    "governance",
    "temporal",
    "record_type",
    "domain_scope",
)


@dataclass(frozen=True, slots=True)
class GovernedCandidate:
    """One resolved governed record as a candidate: the hydrated DTO, and the one ranking
    fact the DTO does not carry.

    `record` is the fully hydrated contract value, carried here rather than fetched again
    after ranking, for the reason `EvidenceCandidate.artifact` is: the result is built by
    mapping over frozen frontier members, so "every item in any result is a member of the
    frozen frontier" holds by construction rather than by a membership check somebody has
    to remember to write. Nothing downstream reconstructs a record, and nothing downstream
    resolves an id -- there is no id here to resolve one from.

    `recorded_at_us` is the only added fact, and it is added because it is the only one
    missing. `GovernedRecord.provenance.temporal` carries ISO-8601 instants, which order
    correctly as strings only if every writer agrees on offset, precision and the `Z`
    spelling; `recorded_at_us` is the integer 0009 actually stored and the one
    `resolve_governed_versions` already ordered by, so recency here is the same fact
    recency was over there.

    `record_id` and `version` are deliberately *not* fields. They are read off the
    identity of the record this candidate will return, so the tie-break cannot be computed
    from an id that disagrees with the record the caller receives -- a drift that a
    duplicated field makes possible and this makes unrepresentable.
    """

    recorded_at_us: int
    record: GovernedRecord

    @property
    def record_id(self) -> str:
        """The governed record id, from the record itself."""
        return self.record.provenance.identity.record_id

    @property
    def version(self) -> str:
        """The governed record version, from the record itself."""
        return self.record.provenance.identity.version


@dataclass(frozen=True, slots=True)
class GovernedFrontier:
    """The frozen authorized governed frontier: the L2 counterpart of `AuthorizedFrontier`.

    Frozen in both senses, and for the same reason. What it deliberately does not carry is
    any way to obtain a candidate it does not already hold: no connection, no repository,
    no projection, no callback and no id it could resolve. A ranker handed one has the
    whole of its input in the value, so widening the result is not merely forbidden -- the
    material to widen it with is absent.
    """

    workspace_id: str
    candidates: tuple[GovernedCandidate, ...]
    #: Which of `GOVERNED_FRONTIER_FILTERS` the builder ran. Required rather than
    #: defaulted: a default would let a frontier narrowed by four filters claim six.
    filters_applied: tuple[str, ...]


def governed_search_text(record: GovernedRecord) -> str:
    """The normalized surface a governed query matches against: the record's own content.

    Two normalizations, and both are load-bearing. The content is *opaque* JSON, so there
    is no field this module may privilege and no schema it may assume -- it matches the
    whole document. `to_canonical_json` is the contract's own serialization, and it sorts
    keys, so two mappings built in different insertion orders produce identical text and a
    record's matchability is a property of what it says rather than of how a decoder
    happened to build it. `normalize_query` then applies the same NFKC and case folding it
    applies to the query, because normalizing one side only makes a match depend on which
    side a caller typed.

    Serializing rather than walking is the same decision the contract already made for
    every digest in this build, and it keeps one definition of "this content as text". It
    does mean object keys and JSON punctuation are part of the matched surface; that is
    visible, deterministic and stated, rather than a hidden field-selection policy this
    module would otherwise have to invent for content it is not allowed to understand.
    """
    return normalize_query(to_canonical_json(record.content))


def governed_order_key(
    candidate: GovernedCandidate, relevance: int
) -> tuple[int, int, str, str]:
    """The one total order both governed orderings sort by.

    `relevance` descending, then `recorded_at_us` descending, then `record_id` ascending,
    then `version` ascending -- negated where descending, because `sort` is ascending and
    a key is the honest place to state direction.

    It is a separate function from `relevance_order_key` rather than a reuse of it, and the
    difference is not cosmetic: that key sorts relevance *ascending* because SQLite's
    `bm25()` returns negative scores, and it breaks ties on `evidence_id`, a field no
    governed record has. Sharing one function would mean one of the two lanes silently
    carrying the other's sign convention.

    **Both tie-breakers are load-bearing, not decorative.** A count-based signal ties
    constantly -- two records mentioning a term once each score identically -- and equal
    instants are equally common, because a correction sealed in one transaction writes
    every version at one microsecond. A sort keyed on score alone returns whatever order
    the rows arrived in, which is packet §8.2's first ordering hazard wearing a score.
    `(record_id, version)` closes the key: 0009 makes the pair unique per workspace, so the
    order is total and the same frontier resolves to the same page on every run.

    With a constant `relevance` the key degrades exactly to recency-then-identity, which is
    why the recency ordering is expressible as this key rather than merely similar to it.
    """
    return (
        -relevance,
        -candidate.recorded_at_us,
        candidate.record_id,
        candidate.version,
    )


def rank_governed(
    frontier: GovernedFrontier, query: str, *, order: str | None, limit: int
) -> tuple[GovernedRecord, ...]:
    """Select and totally order the frozen governed frontier. The governed ranker.

    Read this signature and the import block at the top of the module together, because
    that pair is the whole of §20.12's proof for this partition. The parameters are an
    immutable value, two strings and an integer -- no connection, no repository, no
    storage accessor, no projection and no callback -- and none of them has a default a
    store could be bound into. The module imports the frozen contract and the standard
    library, so there is no module-global handle, no closure-captured one and nothing a
    lazy lookup inside this function could reach. An unfiltered record is not forbidden
    here; it is unreachable.

    **The query selects, then the order orders.** Selection is a normalized substring test
    over `governed_search_text`, so a record that does not match is absent from the result
    under either ordering rather than merely ranked last -- the same relationship the
    temporal filter has to the frontier one level up.

    **An absent order is relevance**, which is the contract's own default reading, and an
    order this build does not recognize is *refused* rather than quietly resolved into it.
    That is the fail-closed direction `resolve_governed_versions` takes for an unrecognized
    view and the contract's own `_validate_order_selector` takes for this same value: an
    unrecognized selector could mean an ordering this build cannot honour or verify, and
    serving a different one under its name is worse than serving nothing. The refusal
    happens before the query is even looked at, so it does not depend on there being
    anything to order. The refusal names the requirement and the recognized selectors --
    both server-authored -- and never the value the caller supplied, so no refusal or
    diagnostic on this path can carry caller-supplied text back out.

    **Relevance is a count, and it is stated as one.** The signal is how many times the
    normalized query occurs in the normalized content -- explainable, recomputable by hand
    from the returned record, and honest about what it is not: there is no index behind it,
    no term weighting and no corpus statistics, so it is a ranking signal rather than
    relevance ranking. It is also computed from the frontier's own members and nothing
    else, so no record outside the frontier can influence the order of one inside it --
    the failure BM25's corpus-wide statistics made possible in Lane B, absent here because
    there is no corpus term in the formula at all.

    An empty normalized query and a non-positive limit both return `()`. The contract
    already refuses both upstream, so neither is a shape a valid request produces; they
    fail closed rather than matching everything and rather than letting a negative limit
    slice from the wrong end.
    """
    resolved = KNOWLEDGE_SEARCH_ORDER_RELEVANCE if order is None else order
    if resolved not in KNOWLEDGE_SEARCH_ORDERS:
        raise ValueError(
            "order is not a recognized MemorySearchOrder for knowledge.search; "
            f"must be one of {sorted(KNOWLEDGE_SEARCH_ORDERS)!r} or absent"
        )

    needle = normalize_query(query)
    if not needle or limit <= 0:
        return ()

    matched = [
        (candidate, hits)
        for candidate in frontier.candidates
        if (hits := governed_search_text(candidate.record).count(needle))
    ]
    # One relevance for every match under `recency`, so the shared key degrades to
    # recency-then-identity rather than being a second sort with its own tie-breakers.
    scored = resolved == KNOWLEDGE_SEARCH_ORDER_RELEVANCE
    matched.sort(key=lambda pair: governed_order_key(pair[0], pair[1] if scored else 0))
    return tuple(candidate.record for candidate, _ in matched[:limit])


__all__ = [
    "BM25_B",
    "BM25_K1",
    "BM25_MINIMUM_IDF",
    "CONFIGURED_LOCAL_OWNER",
    "FRONTIER_FILTERS",
    "GOVERNED_FRONTIER_FILTERS",
    "AuthorizedFrontier",
    "EvidenceCandidate",
    "EvidenceLabelGrant",
    "GovernedCandidate",
    "GovernedFrontier",
    "ProjectedCandidate",
    "ProjectedFrontier",
    "authorized_frontier",
    "candidate_set_manifest",
    "governed_order_key",
    "governed_search_text",
    "local_owner_label_grant",
    "normalize_query",
    "query_tokens",
    "rank_candidates",
    "rank_governed",
    "rank_projected",
    "relevance_order_key",
]
