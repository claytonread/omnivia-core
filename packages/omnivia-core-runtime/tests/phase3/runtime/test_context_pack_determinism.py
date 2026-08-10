"""V06-3 Lane D: the frozen Context Pack frontier, and the purity of its builder.

What this file covers, and what it deliberately does not, stated first because the
distinction is the whole point of the slice it tests.

**Covered.** `storage/context_pack.py` is a pure value transform: a freeze that takes
already-authorized material and produces a frozen frontier plus the out-of-band manifest
that vouches for it, and a builder that turns those two frozen values into a
`ContextPackBuildResult`. Every property below is a property of those functions over
values, and every positive test states the falsifier that makes it worth asserting.

**Not covered, and not claimed anywhere below.** This slice is two files. It does not show
that the later Lane D handler performs its six reads in one coherent database snapshot,
that the L0/L2 filter chain actually *executes* before the freeze -- the F-series simulates
a chain that did not, it does not run one -- that a real grant propagates from the request
envelope into the build context, where `retrieval_version` comes from, that the dispatcher
propagates the pack, that `ContextPackFrontierTooLarge` is mapped to the contract's
`size_limit_exceeded`, or that anything is registered in the production operation registry.
Those are integration requirements of the handler slice, they need a database, a session
and a registry to be checkable at all, and no assertion in this file should be read as
evidence for any of them.

**Two of those gaps carry a packet label, so they are named rather than left to be inferred
from the list above.**

*F1 is not closed by this file, and cannot be.* F1 asks for the production authorization
chain -- `local_owner_label_grant`, `authorized_frontier`, the governed resolver -- to have
an ACL or sensitivity filter relocated from before the freeze to after ranking, and for the
consequence to be caught. No such chain runs in these two files. Nothing here filters, so
there is no filter to move: the F1-shape test below freezes a wider frontier by hand and the
extra candidate is dropped by a *query non-match*, which is not an authorization decision
and is not what F1 names. What that test does establish is the second half of F1 -- that the
consequence of a filter that ran too late is invisible in the response and caught only by
the independently held manifest. The first half needs the real chain, a real ACL or
sensitivity label, and a database, and only the later handler slice can supply them.

*E3 is closed for eighteen of its nineteen fields.* The nineteenth is `retrieval_version`,
and the only thing this file can show about it is that a fixture literal handed to the
builder is stored unchanged. A fixture literal is not a source, and no assertion below
treats it as one; E3 is closed when the handler supplies that field from a real,
non-circular retrieval, which is an integration test with a database in it.

**Why the obvious test would be worthless.** A test that builds a pack and asserts it
validates passes identically for a builder that ranked material the caller was never
entitled to see, provided the builder also wrote down a checksum of the frontier it
actually used. The artifact is self-consistent by construction; what makes it *checkable*
is the independent manifest, produced at the freeze and never reconstructed from a
result. So the falsifiers below are the file, and the positive tests are what they
falsify.

The falsifier map, as the names in this file spell it:

Every D, F and E label below is the packet's own. For D1-D10, F2-F4 and E1/E2/E4 each is
the exact mutation and the exact direction the packet names; the two exceptions are F1 and
E3, which are the integration-only gaps described above -- F1 is asserted in shape only,
and E3 in eighteen of its nineteen fields. The properties this file previously numbered
D1-D10 and E1-E3 are all still here and still asserted, under `supplemental_` names: they
are worth having, they were simply never what those labels meant.

* **D1-D10 -- determinism, as digest mutations.** The first five perturb one member of a
  sealed pack and require the *recomputed* artifact digest to move: two sections reordered
  (D1), two citations reordered inside one conformant two-citation section (D2), one
  section's `token_count` with `budget.tokens_used` kept consistent (D3), one
  `projection_versions` value (D4), and one candidate of the frozen frontier -- which moves
  the authorized-candidate checksum and the pack digest together, each freeze judged against
  its own independent manifest (D5). Then what must be refused or must *not* move:
  `generated_at` alone, resealed, is refused by the validator (D6); the out-of-band
  manifest's element order is normative, so shuffling it leaves the pack accepted and its
  digest unchanged (D7); and one document written by materially different ordinary JSON
  writers digests identically through the contract's own helper (D8). D9 moves the supplied
  instant -- resolution, generation and freshness together -- and requires a valid pack with
  a different identity. D10 is the no-clock proof: a mutation that drops the supplied-instant
  discipline for a wall-clock read must be killed by the structural guard, and one exact
  `ContextPackBuildContext` instance used twice must produce byte-identical output.
* **F1-F4 -- forgery and isolation.** The attack is on the *implementation*, not on the
  verifier. An honest narrow frontier is frozen and its manifest held independently; the
  defect is then produced by freezing a *wider* production frontier carrying one
  unauthorized candidate that ranking sees and drops. F1 is that shape only -- the relocated
  ACL filter it actually names cannot run here, and the paragraph above says so. F2 is the
  literal one: the widened frontier is built so that the production ranker scores the
  unauthorized candidate, places it *last* in the total ranked order -- asserted against
  `context_pack._selectables` itself, not against a ranking restated here -- and selection
  drops it on a budget set to exactly the honest pack's own `tokens_used`. In both, the
  selected response content is byte-identical to the honest pack while the widened pack's
  candidate checksum differs, and verification against the independently held narrow
  manifest refuses. F3 is the same widened pack carrying
  `pre_ranking_authorization_enforced = true`, which rescues nothing. F4 writes each of the
  four prohibited handle shapes into a copy of the production source, *runs* it against a
  tracing store this file owns, and requires the smuggled read to have actually executed and
  reached the output before requiring the structural guard to kill it.
* **E1-E4 -- exactness.** E1 is total attribution: a section naming no citation, and a
  section naming an absent citation, are both refused. E2 is groundedness: a resealed pack
  whose citation names an identity outside the independently held frozen manifest is
  refused. E3 is version attribution -- eighteen of the nineteen reproducibility fields tied
  to a non-circular source, with the provenance table written out in the test rather than
  implied, and the nineteenth (`retrieval_version`) left open and labelled as the
  integration gap it is. E4 is exact identity accounting: frontier minus selected minus
  omissions is empty, and the sets are disjoint.
* **G1-G8 -- the invariants, as refusals.** Every one of these is an input the builder
  once accepted and turned into a pack the contract's own validator approved, because the
  defect was never a malformed artifact -- it was a well-formed artifact stating something
  untrue of the build that produced it, which no validator can catch. The attested filter
  chain must be the chain that ran (G1); no candidate may come from another workspace
  (G2); `records` holds current versions and `history` superseded ones, and a version in
  the wrong one stops the build rather than being moved (G3); the freshness statement must
  name exactly this operation's own projection and must not be stale (G4); the query
  ranked and the query attested are one value, checked and then *used* (G5, G5b); a
  summarizer or model that never ran may not be given a version (G6), and neither may an
  algorithm this module implements itself, which is pinned to the module's own published
  constant rather than accepted as a caller's label (G6b, G6c); the build context
  and the frontier name one workspace (G7); the returned pack is transitively immutable
  while still being its own digest (G8); and no refusal names the material it refused
  (G9).

Two of the supplemental tests replace tests that asserted the opposite and passed. The
authority test required the builder to sort and deduplicate a scrambled grant; it now
requires the exact authorized value to be stored byte for byte and every malformed spelling
to be refused, because Amendment 009 makes the effective authority the set already
authorized rather than one the producer tidied up. The insertion-order test proved
determinism using two invented model versions; it now proves the same property over
`policy_versions` and a candidate's own opaque JSON, and requires `model_versions` to be
empty.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
import itertools
import json
import re
import sys
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType, ModuleType
from typing import Any

import pytest
from omnivia_core_runtime.storage import context_pack as context_pack_module
from omnivia_core_runtime.storage.context_pack import (
    CONTEXT_PACK_BUILDER_VERSION,
    CONTEXT_PACK_FRESHNESS_PROJECTION_ID,
    CONTEXT_PACK_FRONTIER_FILTERS,
    CONTEXT_PACK_MAX_FRONTIER_CANDIDATES,
    CONTEXT_PACK_MAX_SECTION_CONTENT_LENGTH,
    CONTEXT_PACK_NORMALIZATION_VERSION,
    CONTEXT_PACK_RANKING_VERSION,
    CONTEXT_PACK_RERANKER_DISABLED,
    CONTEXT_PACK_SELECTION_VERSION,
    CONTEXT_PACK_TOKENIZER_ID,
    CONTEXT_PACK_TOKENIZER_VERSION,
    OMISSION_NOT_MATCHED,
    OMISSION_SECTION_TOO_LARGE,
    OMISSION_SUPERSEDED,
    OMISSION_TOKEN_BUDGET,
    ContextPackBuildContext,
    ContextPackBuilderInputInvalid,
    ContextPackFreeze,
    ContextPackFrontierTooLarge,
    ContextPackFrozenFrontier,
    FrozenEvidence,
    FrozenRecord,
    build_context_pack,
    freeze_context_pack_frontier,
)
from omnivia_core_runtime.storage.projections import EVIDENCE_SEARCH_PROJECTION_ID

from omnivia_core.contracts.v1 import (
    CONTEXT_PACK_ARTIFACT_CANONICALIZATION,
    CONTEXT_PACK_CANDIDATE_PARTITION_EVIDENCE,
    CONTEXT_PACK_CANDIDATE_PARTITION_HISTORY,
    CONTEXT_PACK_CANDIDATE_PARTITION_RECORDS,
    CONTEXT_PACK_FORMAT_VERSION,
    CONTEXT_PACK_NORMALIZED_REQUEST_VIEW,
    CONTEXT_PACK_SUMMARIZER_DISABLED,
    GOVERNANCE_LAYER_GOVERNED,
    GOVERNANCE_STATE_ACCEPTED,
    CapabilityRef,
    ContextPackAuthorizedCandidateSetManifest,
    ContextPackAuthorizedEvidenceCandidate,
    ContextPackBudget,
    ContextPackBuildInput,
    ContextPackBuildResult,
    ContextPackEvidenceCitation,
    ContextPackEvidenceReference,
    ContextPackRecordCitation,
    ContextPackReproducibility,
    EvidenceArtifact,
    GovernedRecord,
    GrantedAuthority,
    ProjectionFreshness,
    ProvenanceEntry,
    RecordIdentity,
    RecordProvenance,
    RecordTemporalMetadata,
    RecordVersionReference,
    SourceReference,
    compute_authorized_candidate_set_checksum,
    compute_context_pack_artifact_digest,
    to_canonical_json,
    validate_context_pack_build_result,
)
from omnivia_core.contracts.v1.compatibility import (
    ContractDecodeError,
    ContractSemanticError,
)

WORKSPACE_ID = "ws-pack-0001"

#: The one instant. Every fixture below is stamped with it, and it is what the three
#: reproducibility instants are checked against -- so a builder that reached for a clock
#: would have to produce this exact string by accident.
RESOLUTION_TIME = "2023-11-14T22:13:20.000Z"
BASE_US = 1_700_000_000_000_000

QUERY = "alpha"

#: The projection material the later handler reads in the same snapshot as the
#: candidates: the FTS projection's active build digest as its version, and the source
#: checkpoint that build is level with as its watermark. The *values* are supplied rather
#: than derived -- the builder owns no projection and may not invent one -- while the key
#: is pinned by the builder itself, because which projection served a read is a fact the
#: builder does know.
FTS_PROJECTION_ID = CONTEXT_PACK_FRESHNESS_PROJECTION_ID
FTS_BUILD_DIGEST = "sha256:" + "b" * 64
FTS_SOURCE_CHECKPOINT = "chk-000000000009"
PROJECTION_VERSIONS = {FTS_PROJECTION_ID: FTS_BUILD_DIGEST}
PROJECTION_WATERMARKS = {FTS_PROJECTION_ID: FTS_SOURCE_CHECKPOINT}

#: The one version field this pure slice genuinely does not own. Nothing in
#: `context_pack.py` retrieves: the candidates arrive already read, and which retrieval
#: produced them is a fact only the later Lane D handler holds. So this is an arbitrary
#: fixture label on purpose -- it stands for a handler-supplied producer statement, and no
#: assertion in this file claims to establish where a real one would come from. That
#: remains an integration requirement.
HANDLER_SUPPLIED_RETRIEVAL_VERSION = "retrieval-1"

#: Every producer assertion the build context supplies, bound exactly at validation. The
#: keys are the `validate_context_pack_build_result` keyword suffixes, so the binding test
#: below can walk them rather than restate them.
#:
#: Only two entries are free labels: the normalized query, which is the request's own text
#: folded, and the retrieval version above. Every other version names an algorithm that
#: lives in `context_pack.py`, so the value here is that module's *own published constant*
#: -- a fixture string would let this file agree with a builder that had changed its
#: ranking and kept its label.
EXPECTED_VERSIONS: Mapping[str, Any] = {
    "normalized_query": QUERY,
    "normalization_version": CONTEXT_PACK_NORMALIZATION_VERSION,
    "builder_version": CONTEXT_PACK_BUILDER_VERSION,
    "retrieval_version": HANDLER_SUPPLIED_RETRIEVAL_VERSION,
    "ranking_version": CONTEXT_PACK_RANKING_VERSION,
    "reranking_version": CONTEXT_PACK_RERANKER_DISABLED,
    "selection_version": CONTEXT_PACK_SELECTION_VERSION,
    "tokenizer_id": CONTEXT_PACK_TOKENIZER_ID,
    "tokenizer_version": CONTEXT_PACK_TOKENIZER_VERSION,
    # No summarizer and no model runs in this build, so these are the only values the
    # builder accepts. See the refusal tests.
    "summarizer_version": CONTEXT_PACK_SUMMARIZER_DISABLED,
    "model_versions": {},
}

AUTHORITY = GrantedAuthority(
    principal_id="local-user",
    roles=("owner",),
    capabilities=(CapabilityRef(id="context_pack.build", version="1.0"),),
)
SCOPES = ("workspace.read",)
PURPOSE = "assistant.answer"
POLICY_VERSIONS = {"evidence_acl": "policy-1", "governance": "policy-2"}

_ENTRY = ProvenanceEntry(
    actor_id="local-user",
    actor_kind="human",
    action="ingested",
    occurred_at=RESOLUTION_TIME,
)
_RECORD_SOURCE = SourceReference(
    kind="user.statement", source_id="local-user", retrieved_at=RESOLUTION_TIME
)

#: One token, restated rather than imported from the production module. Importing the
#: compiled pattern would make E1 a test of nothing: a tokenizer that had stopped counting
#: would agree with itself.
_TOKEN_RE = re.compile(r"[^\W_]+|[^\s]")


# --- fixtures, built from the real contract DTOs ------------------------------
#
# Real DTOs rather than stubs, because the validator this file leans on is the contract's
# own: a stub would let these tests agree with a builder the contract would refuse.


def artifact(evidence_id: str, *, locator: str) -> EvidenceArtifact:
    """One conformant L0 artifact. `locator` is what a query does or does not match."""
    return EvidenceArtifact(
        evidence_id=evidence_id,
        workspace_id=WORKSPACE_ID,
        source=SourceReference(
            kind="filesystem.archive", source_id=evidence_id, locator=locator
        ),
        temporal=RecordTemporalMetadata(
            ingested_at=RESOLUTION_TIME, recorded_at=RESOLUTION_TIME
        ),
        content_checksum="sha256:" + evidence_id.encode().hex().ljust(64, "0")[:64],
        media_type="text/markdown",
        metadata={},
        permission_labels=(),
        sensitivity="internal",
        tombstoned=False,
        parser_status="parsed",
        ingestion_status="ingested",
        provenance_history=(_ENTRY,),
    )


def record(
    record_id: str,
    *,
    version: str = "v1",
    content: dict[str, Any] | None = None,
    currentness: str = "current",
) -> GovernedRecord:
    """One conformant current-canonical governed record."""
    return GovernedRecord(
        workspace_id=WORKSPACE_ID,
        record_type="note",
        domain_scope="workspace",
        authority_level="canonical",
        reviewer="reviewer-1",
        provenance=RecordProvenance(
            identity=RecordIdentity(
                record_id=record_id,
                version=version,
                layer=GOVERNANCE_LAYER_GOVERNED,
                governance_state=GOVERNANCE_STATE_ACCEPTED,
                currentness=currentness,
            ),
            temporal=RecordTemporalMetadata(
                ingested_at=RESOLUTION_TIME, recorded_at=RESOLUTION_TIME
            ),
            history=(_ENTRY,),
            evidence_disposition="asserted_with_evidence",
            sources=(_RECORD_SOURCE,),
        ),
        content={"body": QUERY} if content is None else content,
    )


def evidence_member(
    evidence_id: str, *, locator: str, recorded_at_us: int = BASE_US
) -> FrozenEvidence:
    return FrozenEvidence(
        recorded_at_us=recorded_at_us, artifact=artifact(evidence_id, locator=locator)
    )


def record_member(
    record_id: str,
    *,
    version: str = "v1",
    content: dict[str, Any] | None = None,
    currentness: str = "current",
    recorded_at_us: int = BASE_US,
) -> FrozenRecord:
    return FrozenRecord(
        recorded_at_us=recorded_at_us,
        record=record(
            record_id, version=version, content=content, currentness=currentness
        ),
    )


def freeze(
    *,
    workspace_id: str = WORKSPACE_ID,
    evidence: Sequence[FrozenEvidence] = (),
    records: Sequence[FrozenRecord] = (),
    history: Sequence[FrozenRecord] = (),
    filters_applied: Sequence[str] = CONTEXT_PACK_FRONTIER_FILTERS,
    projection_versions: Mapping[str, str] | None = None,
    projection_watermarks: Mapping[str, str] | None = None,
    projection_stale: bool = False,
) -> ContextPackFreeze:
    return freeze_context_pack_frontier(
        workspace_id=workspace_id,
        evidence=evidence,
        records=records,
        history=history,
        filters_applied=filters_applied,
        projection_versions=(
            PROJECTION_VERSIONS if projection_versions is None else projection_versions
        ),
        projection_watermarks=(
            PROJECTION_WATERMARKS
            if projection_watermarks is None
            else projection_watermarks
        ),
        projection_stale=projection_stale,
    )


def request(
    *, token_budget: int = 4096, query: str = QUERY
) -> ContextPackBuildInput:
    return ContextPackBuildInput(
        query=query, mode="deterministic_view", token_budget=token_budget
    )


def context(
    req: ContextPackBuildInput,
    *,
    workspace_id: str = WORKSPACE_ID,
    authority: GrantedAuthority = AUTHORITY,
    scopes: tuple[str, ...] = SCOPES,
    policy_versions: Mapping[str, str] | None = None,
    model_versions: Mapping[str, str] | None = None,
    normalized_query: str | None = None,
    summarizer_version: str | None = None,
    canonical_resolution_time: str = RESOLUTION_TIME,
) -> ContextPackBuildContext:
    """The complete immutable build context: no defaults hiding a clock or a version."""
    return ContextPackBuildContext(
        request=req,
        workspace_id=workspace_id,
        authority=authority,
        scopes=scopes,
        purpose=PURPOSE,
        policy_versions=POLICY_VERSIONS if policy_versions is None else policy_versions,
        canonical_resolution_time=canonical_resolution_time,
        normalized_query=(
            str(EXPECTED_VERSIONS["normalized_query"])
            if normalized_query is None
            else normalized_query
        ),
        normalization_version=str(EXPECTED_VERSIONS["normalization_version"]),
        builder_version=str(EXPECTED_VERSIONS["builder_version"]),
        retrieval_version=str(EXPECTED_VERSIONS["retrieval_version"]),
        ranking_version=str(EXPECTED_VERSIONS["ranking_version"]),
        reranking_version=str(EXPECTED_VERSIONS["reranking_version"]),
        selection_version=str(EXPECTED_VERSIONS["selection_version"]),
        tokenizer_id=str(EXPECTED_VERSIONS["tokenizer_id"]),
        tokenizer_version=str(EXPECTED_VERSIONS["tokenizer_version"]),
        summarizer_version=(
            str(EXPECTED_VERSIONS["summarizer_version"])
            if summarizer_version is None
            else summarizer_version
        ),
        model_versions={} if model_versions is None else model_versions,
    )


def validate(
    result: ContextPackBuildResult,
    *,
    req: ContextPackBuildInput,
    manifest: ContextPackAuthorizedCandidateSetManifest,
    authority: GrantedAuthority = AUTHORITY,
    scopes: Sequence[str] = SCOPES,
    policy_versions: Mapping[str, str] | None = None,
    canonical_resolution_time: str = RESOLUTION_TIME,
    **overrides: Any,
) -> None:
    """Run the contract's own judge, with every expectation this build supplied bound.

    Every `expected_*` the validator offers is passed, including `{}` model versions:
    an unsupplied expectation is only shape-checked, so a call that omitted one would be
    quietly weaker than it reads.
    """
    expectations = dict(EXPECTED_VERSIONS)
    expectations.update(overrides)
    validate_context_pack_build_result(
        result,
        request=req,
        expected_workspace_id=WORKSPACE_ID,
        expected_authority=authority,
        expected_scopes=set(scopes),
        expected_purpose=PURPOSE,
        expected_policy_versions=(
            POLICY_VERSIONS if policy_versions is None else policy_versions
        ),
        expected_authorized_candidate_set=manifest,
        canonical_resolution_time=canonical_resolution_time,
        response_freshness=result.reproducibility.freshness,
        **{f"expected_{name}": value for name, value in expectations.items()},
    )


#: The standard frontier: one matching artifact, one that does not match, one matching
#: current record, one that does not, and one superseded version that is frontier input
#: and never content. Five candidates, two selected, three accounted for -- which is what
#: makes E4's arithmetic non-trivial.
def standard_freeze(
    *, extra_evidence: Sequence[FrozenEvidence] = ()
) -> ContextPackFreeze:
    """The standard frontier, optionally widened by candidates the F-series supplies.

    `extra_evidence` exists for the F1-shape test and F3 and nowhere else. It is what lets a
    test freeze the *implementation's* frontier -- the honest five candidates plus one the
    authorization chain should have removed -- while the honest freeze that produced the
    independently held manifest is the same call with no extras. Both are real freezes with
    real manifests; the difference between them is the defect. F2 needs a frontier this one
    cannot provide and builds its own; see `f2_freeze`.
    """
    return freeze(
        evidence=(
            evidence_member("ev-1", locator="archive://alpha.md"),
            evidence_member("ev-2", locator="archive://beta.md"),
            *extra_evidence,
        ),
        records=(
            record_member("rec-1"),
            record_member("rec-2", content={"body": "beta"}),
        ),
        history=(
            record_member("rec-1", version="v0", currentness="superseded"),
        ),
    )


def standard_pack() -> tuple[ContextPackBuildResult, ContextPackFreeze,
                             ContextPackBuildInput]:
    frozen = standard_freeze()
    req = request()
    return build_context_pack(frozen.frontier, context(req)), frozen, req


def paths(result: ContextPackBuildResult) -> set[str]:
    return {omission.path or "" for omission in result.omissions}


def selected_identities(result: ContextPackBuildResult) -> set[tuple[str, str, str]]:
    """Every identity this pack actually returned, partition-tagged."""
    return {
        (CONTEXT_PACK_CANDIDATE_PARTITION_EVIDENCE, item.evidence_id,
         item.content_checksum)
        for item in result.evidence
    } | {
        (CONTEXT_PACK_CANDIDATE_PARTITION_RECORDS,
         item.provenance.identity.record_id, item.provenance.identity.version)
        for item in result.records
    }


def frontier_identities(
    manifest: ContextPackAuthorizedCandidateSetManifest,
) -> set[tuple[str, str, str]]:
    """Every identity the frozen frontier held, read off the out-of-band manifest."""
    identities: set[tuple[str, str, str]] = set()
    for candidate in manifest.candidates:
        if isinstance(candidate, ContextPackAuthorizedEvidenceCandidate):
            identities.add(
                (candidate.partition, candidate.evidence_id, candidate.content_checksum)
            )
        else:
            identities.add((candidate.partition, candidate.record_id, candidate.version))
    return identities


def accounted_identities(result: ContextPackBuildResult) -> set[tuple[str, str, str]]:
    """Every identity an omission accounts for, parsed back out of its `path`."""
    identities: set[tuple[str, str, str]] = set()
    for omission in result.omissions:
        assert omission.path is not None
        partition, rest = omission.path.split("/", 1)
        first, second = rest.rsplit("@", 1)
        identities.add((partition, first, second))
    return identities


def artifact_digest(result: ContextPackBuildResult) -> str:
    """The contract's own artifact digest, recomputed over this exact value.

    Every D-series mutation below goes through this helper rather than reading `pack_id`,
    because reading a digest a mutated document still carries would compare a stale value
    with itself. The two members the rule removes are blanked first, so what is hashed is
    the reduction the contract defines and not a document with a foreign digest inside it.
    """
    placeholder = "sha256:" + "0" * 64
    blank = replace(
        result,
        pack_id=placeholder,
        reproducibility=replace(result.reproducibility, artifact_checksum=placeholder),
    )
    return compute_context_pack_artifact_digest(blank.to_wire())


def reseal(result: ContextPackBuildResult) -> ContextPackBuildResult:
    """Recompute a hand-built result's content address.

    Used where a test needs a *conformant* pack the production builder does not naturally
    emit -- a two-citation section -- or needs a mutated pack to be internally consistent so
    that the validator's refusal is about the mutation rather than about a stale checksum.
    It is the contract's own digest helper over the contract's own reduction, so the fixture
    is content-addressed by the same rule the builder is, and nothing here is evidence about
    the builder.
    """
    digest = artifact_digest(result)
    return replace(
        result,
        pack_id=digest,
        reproducibility=replace(result.reproducibility, artifact_checksum=digest),
    )


# --- the baseline -------------------------------------------------------------


def test_the_baseline_pack_passes_the_contracts_own_judge() -> None:
    """One pack, from one freeze, judged by the contract with every expectation bound.

    Everything after this test is a property *of* this pack or a falsifier *against* it,
    so it is worth naming what the call actually establishes: the manifest is the one the
    freeze produced rather than one reconstructed from the result, and all eleven producer
    assertions are bound by exact equality rather than shape-checked. A call that omitted
    the manifest could not be made -- the validator requires it -- and one that omitted the
    expectations would still pass for a builder that invented every version string.
    """
    result, frozen, req = standard_pack()

    validate(result, req=req, manifest=frozen.manifest)

    assert result.pack_id == result.reproducibility.artifact_checksum
    assert result.pack_id.startswith("sha256:")
    assert result.fresh_authorization_required is True
    assert [section.section_id for section in result.sections] == ["s-0001", "s-0002"]
    assert [
        citation.citation_id for citation in result.citations
    ] == ["c-0001", "c-0002"]


def test_the_frontier_states_the_filters_that_ran_before_it_was_frozen() -> None:
    """The frozen value says what narrowed it, and it is the union of both chains.

    Falsifier: a default here would let a frontier narrowed by four filters claim
    thirteen, and the claim is exactly what a reviewer would otherwise have to infer.
    """
    frozen = standard_freeze()

    assert frozen.frontier.filters_applied == CONTEXT_PACK_FRONTIER_FILTERS
    assert set(CONTEXT_PACK_FRONTIER_FILTERS) >= {
        "workspace", "scope", "purpose", "capability", "policy",
        "evidence_label_acl", "sensitivity", "tombstone",
        "view", "governance", "temporal", "record_type", "domain_scope",
    }
    with pytest.raises(TypeError):
        freeze_context_pack_frontier(  # type: ignore[call-arg]
            workspace_id=WORKSPACE_ID,
            evidence=(),
            records=(),
            history=(),
            projection_versions=PROJECTION_VERSIONS,
            projection_watermarks=PROJECTION_WATERMARKS,
            projection_stale=False,
        )


def test_the_manifest_is_produced_at_the_freeze_and_is_never_a_response_field() -> None:
    """The manifest names the whole frontier -- including history -- and travels apart.

    Falsifier: a manifest rebuilt from the result would name only the two selected items,
    would agree with the pack by construction, and would make F1 and F2 unfalsifiable.
    """
    result, frozen, _ = standard_pack()

    assert frontier_identities(frozen.manifest) == {
        (CONTEXT_PACK_CANDIDATE_PARTITION_EVIDENCE, "ev-1",
         artifact("ev-1", locator="x").content_checksum),
        (CONTEXT_PACK_CANDIDATE_PARTITION_EVIDENCE, "ev-2",
         artifact("ev-2", locator="x").content_checksum),
        (CONTEXT_PACK_CANDIDATE_PARTITION_RECORDS, "rec-1", "v1"),
        (CONTEXT_PACK_CANDIDATE_PARTITION_RECORDS, "rec-2", "v1"),
        (CONTEXT_PACK_CANDIDATE_PARTITION_HISTORY, "rec-1", "v0"),
    }
    assert frozen.frontier.candidate_set_checksum == (
        result.reproducibility.authorization_context.authorized_candidate_set_checksum
    )
    assert "authorized_candidate_set" not in result.to_wire()


def test_history_is_frontier_input_and_context_models_are_empty() -> None:
    """A superseded version reaches the manifest and an omission, and no section.

    Falsifier: returning it in `history` would present two contradictory claims about one
    instant; dropping it from the manifest would make the frontier unreproducible; leaving
    it unaccounted would break E4.
    """
    result, frozen, _ = standard_pack()

    assert result.history == ()
    assert result.context_models == ()
    assert frozen.frontier.context_models == ()
    assert f"{CONTEXT_PACK_CANDIDATE_PARTITION_HISTORY}/rec-1@v0" in paths(result)
    assert all("rec-1" not in section.content or section.kind != "governed_record"
               or "v0" not in section.content for section in result.sections)
    cited = {
        citation.record_reference.version
        for citation in result.citations
        if isinstance(citation, ContextPackRecordCitation)
    }
    assert "v0" not in cited


def test_a_current_canonical_record_may_be_selected_as_content() -> None:
    """The complement of the test above: `records` is content, and is proven to be.

    Falsifier: a builder that excluded every governed record would pass the history test
    and every accounting test while answering no governed query at all.
    """
    result, _, _ = standard_pack()

    assert [item.provenance.identity.record_id for item in result.records] == ["rec-1"]
    assert any(section.kind == "governed_record" for section in result.sections)


# --- D1-D10: determinism, as digest mutations ----------------------------------
#
# Each of D1-D5 takes a sealed pack, perturbs exactly one member, and requires the digest
# *recomputed by the contract's own helper* to move. That direction matters: a test that
# asserted "the reordered array is refused" would be a test of the validator's ascending
# check, and would still pass for a digest rule that covered only half the document. What
# the packet asks for is the content addressing itself -- that each of these members is
# inside the preimage -- so the assertion is on `artifact_digest`, not on a refusal.


def test_d1_reordering_two_sections_changes_the_recomputed_artifact_digest() -> None:
    """Section order is inside the digest preimage.

    Falsifier: a digest computed over a *set* of sections, or over their contents with the
    order dropped, would leave these two documents sharing one identity -- and a pack whose
    identity does not cover the order its content is presented in is a pack two different
    presentations can both claim.
    """
    result, _, _ = standard_pack()
    first, second = result.sections

    reordered = replace(result, sections=(second, first))

    assert artifact_digest(reordered) != artifact_digest(result)
    assert artifact_digest(result) == result.pack_id


def test_d2_reordering_two_citations_inside_one_section_changes_the_digest() -> None:
    """Citation order *within* one section is inside the preimage too.

    A finer mutation than D1 and a genuinely separate one: `citation_ids` is a nested array,
    so a digest that covered the section list but canonicalized each section's members as an
    unordered collection would pass D1 and fail here.

    The production builder emits one citation per section, so the two-citation section is
    built by hand -- and it is built *conformant*: both citations still resolve, both
    selected items are still cited, and `tokens_used` is still the sum of the sections that
    remain, which the supplemental test below confirms by putting it through the validator.
    """
    result, _, _ = standard_pack()
    first, _ = result.sections
    ascending = reseal(
        replace(
            result,
            sections=(replace(first, citation_ids=("c-0001", "c-0002")),),
            budget=ContextPackBudget(
                token_budget=result.budget.token_budget,
                tokens_used=first.token_count,
            ),
        )
    )
    descending = replace(
        ascending,
        sections=(replace(ascending.sections[0], citation_ids=("c-0002", "c-0001")),),
    )

    assert artifact_digest(descending) != artifact_digest(ascending)


def test_d3_changing_one_section_token_count_changes_the_digest() -> None:
    """The token accounting is inside the preimage, not merely checked beside it.

    The mutation keeps `budget.tokens_used` consistent with the new count on purpose. An
    inconsistent pair would be caught by the validator's own arithmetic and would prove
    nothing about the digest; a *self-consistent* pair is a document the validator has no
    complaint about, so the only thing that can distinguish it from the honest pack is the
    content address.

    Falsifier: a digest taken over section content alone -- an easy and plausible reduction,
    since the content is what the caller reads -- leaves these two indistinguishable, and a
    pack could then restate what its content cost without changing what it is.
    """
    result, _, _ = standard_pack()
    first, *rest = result.sections

    inflated = replace(
        result,
        sections=(replace(first, token_count=first.token_count + 1), *rest),
        budget=ContextPackBudget(
            token_budget=result.budget.token_budget,
            tokens_used=result.budget.tokens_used + 1,
        ),
    )

    assert artifact_digest(inflated) != artifact_digest(result)


def test_d4_changing_one_projection_version_value_changes_the_digest() -> None:
    """The freshness statement is inside the preimage.

    Falsifier: freshness is the one part of a pack that describes the *snapshot* rather than
    the content, so it is the natural thing to leave outside a content address. Left outside,
    two packs built from the same material against projections at different build digests
    would share one identity, and a stale read would be indistinguishable from a fresh one
    by the only value a caller can independently recompute.
    """
    result, _, _ = standard_pack()
    freshness = result.reproducibility.freshness

    redated = replace(
        result,
        reproducibility=replace(
            result.reproducibility,
            freshness=replace(
                freshness, projection_versions={FTS_PROJECTION_ID: "sha256:" + "c" * 64}
            ),
        ),
    )

    assert artifact_digest(redated) != artifact_digest(result)


def test_d5_changing_one_frozen_candidate_moves_the_checksum_and_the_digest() -> None:
    """One candidate changed on the frontier, and both digests follow it.

    Two independent freezes rather than one pack edited: each is judged against *its own*
    manifest, produced at its own freeze, so both packs are honest and accepted. What
    changes between them is one frontier member -- `ev-2` becomes `ev-3` -- and the
    assertion is that this reaches both the authorized-candidate checksum and the pack's own
    identity.

    Falsifier: the changed candidate is one neither pack selects (its locator does not match
    the query), so nothing in `sections`, `citations`, `evidence` or `records` differs. A
    checksum computed over the *selected* items instead of the frozen frontier would
    therefore be identical across the two, and the frontier claim would be unverifiable --
    which is precisely the substitution F1-F3 below attack.
    """
    def frozen_with(second_evidence_id: str) -> ContextPackFreeze:
        return freeze(
            evidence=(
                evidence_member("ev-1", locator="archive://alpha.md"),
                evidence_member(second_evidence_id, locator="archive://beta.md"),
            ),
            records=(
                record_member("rec-1"),
                record_member("rec-2", content={"body": "beta"}),
            ),
            history=(record_member("rec-1", version="v0", currentness="superseded"),),
        )

    before = frozen_with("ev-2")
    after = frozen_with("ev-3")
    req = request()
    before_pack = build_context_pack(before.frontier, context(req))
    after_pack = build_context_pack(after.frontier, context(req))

    validate(before_pack, req=req, manifest=before.manifest)
    validate(after_pack, req=req, manifest=after.manifest)

    assert before.frontier.candidate_set_checksum != (
        after.frontier.candidate_set_checksum
    )
    assert artifact_digest(before_pack) != artifact_digest(after_pack)
    assert before_pack.sections == after_pack.sections
    assert before_pack.citations == after_pack.citations


def test_d6_moving_generated_at_alone_is_refused_by_the_validator() -> None:
    """The generation instant is the resolution instant, and resealing does not rescue it.

    The mutated pack is *resealed*, so it is internally consistent: its `pack_id` really is
    the digest of its own content. That is what makes the refusal meaningful -- a validator
    that only checked content addressing would accept it, and the pack would then carry a
    generation time that no longer matches the instant it resolved at, which is exactly how
    two identical builds acquire two identities.

    Falsifier: without the reseal this test would pass against a validator that noticed only
    the stale checksum, and would say nothing about the instants.
    """
    result, frozen, req = standard_pack()
    later = "2023-11-14T22:14:00.000Z"

    validate(result, req=req, manifest=frozen.manifest)
    drifted = reseal(
        replace(
            result,
            reproducibility=replace(result.reproducibility, generated_at=later),
        )
    )

    assert drifted.pack_id == artifact_digest(drifted)
    assert drifted.reproducibility.canonical_resolution_time == RESOLUTION_TIME
    assert drifted.reproducibility.freshness.as_of == RESOLUTION_TIME
    with pytest.raises(ContractSemanticError, match="generated_at"):
        validate(drifted, req=req, manifest=frozen.manifest)


def test_d7_shuffling_the_out_of_band_manifest_changes_neither_verdict_nor_digest(
) -> None:
    """Manifest element order is normative, so the manifest is a set in practice.

    `compute_authorized_candidate_set_checksum` sorts the candidates internally by
    `(partition, identity components)` before hashing, because RFC 8785 orders object
    members and says nothing about array elements. So a caller who assembles the same
    frontier in a different order holds the same statement, and the pack it verifies is
    untouched.

    Falsifier: without that internal sort, a verifier's own iteration order would decide
    whether an honest pack passed -- and the whole F-series below, which turns on an
    independently held manifest, would be reporting the verifier's bookkeeping rather than
    the producer's behaviour.
    """
    result, frozen, req = standard_pack()
    shuffled = replace(
        frozen.manifest, candidates=tuple(reversed(frozen.manifest.candidates))
    )

    assert shuffled.candidates != frozen.manifest.candidates
    assert compute_authorized_candidate_set_checksum(shuffled) == (
        compute_authorized_candidate_set_checksum(frozen.manifest)
    )

    validate(result, req=req, manifest=shuffled)
    assert artifact_digest(result) == result.pack_id


def reversed_key_order(value: Any) -> Any:
    """The same JSON value with every object's members written in the opposite order.

    Ordinary Python dictionaries preserve insertion order and `json.dumps` writes them in
    it, so this is how a test produces a *materially different serialization* of one
    document without changing the document.
    """
    if isinstance(value, dict):
        return {
            key: reversed_key_order(item)
            for key, item in reversed(list(value.items()))
        }
    if isinstance(value, list):
        return [reversed_key_order(item) for item in value]
    return value


def test_d8_materially_different_json_writers_produce_one_canonical_digest() -> None:
    """Serialization choices are not part of the identity; the canonical form is.

    Three ordinary writers, none of them canonical: compact separators, indented with sorted
    keys, and indented with every object's members reversed. Each is parsed back and the
    decoded document handed to the contract's *own* digest helper.

    Falsifier: a digest taken over `json.dumps` output rather than over the RFC 8785
    canonicalization would give these three documents three identities, and a pack's address
    would then depend on which writer a server happened to use -- which no second party
    could reproduce.
    """
    result, _, _ = standard_pack()
    wire = result.to_wire()

    documents = [
        json.dumps(wire, separators=(",", ":")),
        json.dumps(wire, indent=2, sort_keys=True),
        json.dumps(reversed_key_order(wire), indent=4),
    ]
    assert len(set(documents)) == 3

    digests = {
        compute_context_pack_artifact_digest(json.loads(document))
        for document in documents
    }

    assert digests == {compute_context_pack_artifact_digest(wire)}


def test_d9_a_new_supplied_instant_moves_resolution_generation_and_freshness_together(
) -> None:
    """The instant is an input, and moving it moves the whole pack.

    All three instants come from one field, so supplying a different one has to move
    `canonical_resolution_time`, `generated_at` and `freshness.as_of` at once or the
    validator refuses. It does not refuse: the rebuilt pack is valid under the new instant,
    and it carries a different identity.

    Falsifier: this is the positive complement of D6 and of the no-clock proof. A builder
    that pinned the instant to a constant would produce the *same* pack here; one that read
    a clock would fail the equalities; one that rendered the three from two different sources
    would fail the validator.
    """
    frozen = standard_freeze()
    req = request()
    later = "2024-05-01T00:00:00.000Z"

    original = build_context_pack(frozen.frontier, context(req))
    moved = build_context_pack(
        frozen.frontier, context(req, canonical_resolution_time=later)
    )

    validate(moved, req=req, manifest=frozen.manifest, canonical_resolution_time=later)
    repro = moved.reproducibility
    assert repro.canonical_resolution_time == later
    assert repro.generated_at == later
    assert repro.freshness.as_of == later
    assert moved.pack_id != original.pack_id
    assert artifact_digest(moved) == moved.pack_id


def test_d10_the_builder_cannot_read_a_clock_and_the_guard_kills_one_that_does() -> None:
    """No clock, proved by mutation rather than by reading the import list.

    An import assertion over the production file proves the file has not changed. This
    writes the defect *in*: the mutation deletes the supplied-instant discipline -- the one
    line that reads `context.canonical_resolution_time` and the `generated_at` that is set
    from it -- and replaces it with a wall-clock read, which is exactly the shape a builder
    acquires when someone decides a pack should record when it was emitted.

    The guard must fire on it. Both halves are asserted: the mutated module produces the
    clock finding, and the production module produces none, so the difference being measured
    is real.
    """
    mutated, expected = mutate("clock")
    assert mutated != production_source()

    with load_mutant(mutated, "_context_pack_mutant_clock") as mutant:
        findings = purity_violations(mutated, mutant)

    assert expected in findings, findings
    assert purity_violations(production_source(), context_pack_module) == ()


def test_d10b_one_exact_build_context_instance_used_twice_is_byte_identical() -> None:
    """One value, two builds, one document -- and it is the *same* instance both times.

    Two separately constructed contexts carrying equal fields would leave a builder free to
    depend on something that happens to be equal across them. One instance removes that: if
    the two documents differ, the difference came from outside both arguments.

    Falsifier: a wall-clock `generated_at`, a set iterated without sorting, or a mapping
    rendered in insertion order would each give these two builds two identities -- and
    because the artifact is content-addressed, two identities is two different packs.
    """
    frozen = standard_freeze()
    one_context = context(request())

    first = build_context_pack(frozen.frontier, one_context)
    second = build_context_pack(frozen.frontier, one_context)

    assert first.to_wire() == second.to_wire()
    assert first.pack_id == second.pack_id


# --- supplemental determinism properties ---------------------------------------
#
# Everything below this line is a property worth having that is not one of the packet's
# D1-D10 mutations: the contract's ascending orders, the total ranking order, permutation
# invariance, the Amendment 009 authority rules, and the exact freshness values. These were
# previously numbered D1-D10, which is why the packet's own list had to be restored above
# rather than assumed to be present under those names.


def test_supplemental_a_section_naming_two_citations_is_ascending_or_refused() -> None:
    """The multi-citation ordering rule, on a fixture built to exercise it.

    The production builder emits one citation per section, so this rule is unreachable
    from a natural result and a test over one would assert nothing. The fixture below is a
    conformant pack -- both citations still resolve, both are still used, both selected
    items are still cited, and `tokens_used` is still the sum of the sections that remain
    -- differing only in that one section carries both citation ids.

    Falsifier: the descending spelling of the same conformant pack must be refused, or
    "ascending" is a word the contract uses and nothing enforces.
    """
    result, frozen, req = standard_pack()
    first, second = result.sections
    merged = replace(first, citation_ids=("c-0001", "c-0002"))
    two_citation = reseal(
        replace(
            result,
            sections=(merged,),
            budget=ContextPackBudget(
                token_budget=result.budget.token_budget,
                tokens_used=merged.token_count,
            ),
        )
    )

    validate(two_citation, req=req, manifest=frozen.manifest)
    assert second.section_id not in {s.section_id for s in two_citation.sections}

    descending = reseal(
        replace(
            two_citation,
            sections=(replace(merged, citation_ids=("c-0002", "c-0001")),),
        )
    )
    with pytest.raises(ContractSemanticError, match="ascending"):
        validate(descending, req=req, manifest=frozen.manifest)


def test_supplemental_the_selected_partitions_and_their_reproducibility_arrays_agree() -> None:
    """Selected arrays ascend by identity, and the reproducibility record restates them.

    Falsifier: the validator requires `evidence_versions`/`record_versions` to be exactly
    the selected identities in ascending order, so a builder that emitted the *ranked*
    order in either place -- the order the sections are in -- would fail here and only
    here.
    """
    frozen = freeze(
        evidence=(
            evidence_member("ev-9", locator="archive://alpha-9.md"),
            evidence_member("ev-1", locator="archive://alpha-1.md"),
        ),
        records=(
            record_member("rec-9", content={"body": "alpha nine"}),
            record_member("rec-1", content={"body": "alpha one"}),
        ),
    )
    req = request()
    result = build_context_pack(frozen.frontier, context(req))

    validate(result, req=req, manifest=frozen.manifest)
    assert [item.evidence_id for item in result.evidence] == ["ev-1", "ev-9"]
    assert [
        item.provenance.identity.record_id for item in result.records
    ] == ["rec-1", "rec-9"]
    assert [
        reference.evidence_id
        for reference in result.reproducibility.evidence_versions
    ] == ["ev-1", "ev-9"]
    assert [
        reference.record_id for reference in result.reproducibility.record_versions
    ] == ["rec-1", "rec-9"]


def test_supplemental_omissions_ascend_by_code_then_path_then_message() -> None:
    """Four omissions, three codes, one deterministic order.

    The budget is chosen so the *ranked* order and the *contract's* order genuinely
    disagree: the highest-ranked candidate is the one that does not fit, so a builder
    emitting omissions as it considered candidates would put `token_budget_exhausted`
    first, and the contract's strict ascending check over `(code, path, message)` would
    refuse the pack. Asserting only `sorted(keys) == keys` on a fixture whose two orders
    coincide would prove nothing, which is why this test does not use the baseline pack.
    """
    frozen = standard_freeze()
    req = request(token_budget=12)
    result = build_context_pack(frozen.frontier, context(req))

    validate(result, req=req, manifest=frozen.manifest)
    keys = [
        (omission.code, omission.path, omission.message)
        for omission in result.omissions
    ]
    assert keys == sorted(keys)
    assert {omission.code for omission in result.omissions} == {
        OMISSION_NOT_MATCHED,
        OMISSION_SUPERSEDED,
        OMISSION_TOKEN_BUDGET,
    }
    # Ranked order would have put the budget omission first; the contract's order does not.
    assert result.omissions[0].code == OMISSION_NOT_MATCHED
    assert result.omissions[-1].code == OMISSION_TOKEN_BUDGET
    assert len(result.omissions) == 4


def test_supplemental_the_effective_authority_and_scopes_are_stored_byte_for_byte() -> None:
    """Amendment 009: the artifact records the grant that was already authorized.

    This test replaces one that supplied a scrambled, duplicated authority and asserted the
    builder tidied it into a conformant one. That behaviour was the defect. Sorting means a
    caller can hand the builder a different array from the one authorization produced and
    have it silently accepted; deduplicating means a malformed grant is repaired into a
    well-formed *attestation of something nobody granted*. Neither is detectable afterwards,
    because the artifact then carries the repaired value and records no repair.

    Falsifier: the supplied roles, capabilities and scopes below are already canonical but
    are *not* what a sorting builder would necessarily produce -- they carry more than one
    entry each, so a builder that rebuilt them from a Python `set` would reorder at least
    one -- and every stored array is asserted identical to the supplied object.
    """
    granted = GrantedAuthority(
        principal_id="local-user",
        roles=("owner", "reader"),
        capabilities=(
            CapabilityRef(id="context_pack.build", version="1.0"),
            CapabilityRef(id="knowledge.search", version="1.0"),
        ),
    )
    scopes = ("evidence.read", "workspace.read")
    frozen = standard_freeze()
    req = request()
    result = build_context_pack(
        frozen.frontier, context(req, authority=granted, scopes=scopes)
    )

    validate(result, req=req, manifest=frozen.manifest, authority=granted, scopes=scopes)
    stored = result.reproducibility.authorization_context
    assert stored.authority.roles == granted.roles
    assert stored.authority.capabilities == granted.capabilities
    assert stored.scopes == scopes


@pytest.mark.parametrize(
    ("name", "authority", "scopes"),
    [
        (
            "roles_descending",
            replace(AUTHORITY, roles=("reader", "owner")),
            SCOPES,
        ),
        (
            "roles_duplicated",
            replace(AUTHORITY, roles=("owner", "owner")),
            SCOPES,
        ),
        (
            "capabilities_descending",
            replace(
                AUTHORITY,
                capabilities=(
                    CapabilityRef(id="knowledge.search", version="1.0"),
                    CapabilityRef(id="context_pack.build", version="1.0"),
                ),
            ),
            SCOPES,
        ),
        (
            "capability_id_twice",
            replace(
                AUTHORITY,
                capabilities=(
                    CapabilityRef(id="context_pack.build", version="1.0"),
                    CapabilityRef(id="context_pack.build", version="1.1"),
                ),
            ),
            SCOPES,
        ),
        ("scopes_descending", AUTHORITY, ("workspace.read", "evidence.read")),
        ("scopes_duplicated", AUTHORITY, ("workspace.read", "workspace.read")),
        ("scopes_empty", AUTHORITY, ()),
    ],
)
def test_supplemental_a_malformed_authority_or_scope_set_is_refused_rather_than_repaired(
    name: str, authority: GrantedAuthority, scopes: tuple[str, ...]
) -> None:
    """The complement of D5, one refusal per way a supplied grant can be malformed.

    Each of these is a value the *previous* builder accepted and quietly fixed -- a
    descending order, a repeated role, one capability id at two versions, an empty scope
    set. Each now fails closed, and the exception is the builder-input one rather than the
    frontier-size one, so the later handler can map "the caller asked for too much" and
    "this producer was handed something impossible" to different outcomes.

    Falsifier: without the refusal every one of these produces a pack the contract's own
    validator accepts, attesting an authority array that is not the one authorization ran.
    """
    frozen = standard_freeze()
    req = request()

    with pytest.raises(ContextPackBuilderInputInvalid):
        build_context_pack(
            frozen.frontier, context(req, authority=authority, scopes=scopes)
        )


def test_supplemental_mapping_insertion_order_reaches_nothing() -> None:
    """Two builds whose mappings were built in opposite orders are one pack.

    Falsifier: `policy_versions` and each candidate's opaque JSON content are `Mapping`s
    the caller assembles. Rendering either in insertion order would make the pack's
    identity a function of how a dictionary happened to be built.

    `model_versions` is deliberately not exercised here any more: this builder requires it
    to be empty, so there is no insertion order left in it to disagree about. The empty
    map is a *stronger* statement, not a weakened test -- see the refusal test below.
    """
    policies = {"evidence_acl": "policy-1", "governance": "policy-2"}
    reversed_policies = dict(reversed(list(policies.items())))
    body = {"body": QUERY, "note": "alpha again"}
    reversed_body = dict(reversed(list(body.items())))
    req = request()

    forward = build_context_pack(
        freeze(records=(record_member("rec-1", content=body),)).frontier,
        context(req, policy_versions=policies),
    )
    backward = build_context_pack(
        freeze(records=(record_member("rec-1", content=reversed_body),)).frontier,
        context(req, policy_versions=reversed_policies),
    )

    assert forward.pack_id == backward.pack_id
    assert list(
        forward.reproducibility.authorization_context.policy_versions
    ) == ["evidence_acl", "governance"]
    assert forward.reproducibility.model_versions == {}


def test_supplemental_every_input_permutation_yields_the_identical_pack() -> None:
    """The freeze is a function of its members, not of the order rows arrived in.

    Falsifier: a partition sorted only within itself, an unsorted manifest, or a ranking
    tie broken by list position would each let at least one of these permutations
    disagree. Asserting one permutation proves nothing; asserting all of them is the
    property.
    """
    evidence = (
        evidence_member("ev-1", locator="archive://alpha-1.md"),
        evidence_member("ev-2", locator="archive://alpha-2.md"),
        evidence_member("ev-3", locator="archive://beta-3.md"),
    )
    records = (
        record_member("rec-1"),
        record_member("rec-2", content={"body": "alpha two"}),
        record_member("rec-3", content={"body": "beta three"}),
    )
    req = request()

    packs = {
        build_context_pack(
            freeze(evidence=ordered_evidence, records=ordered_records).frontier,
            context(req),
        ).pack_id
        for ordered_evidence in itertools.permutations(evidence)
        for ordered_records in itertools.permutations(records)
    }

    assert len(packs) == 1


def test_supplemental_an_equal_score_at_an_equal_instant_breaks_on_partition_then_identity(
) -> None:
    """The identity tie-break is what makes the order total.

    Two records and one artifact, all scoring once, all recorded at the same microsecond.
    Relevance and recency both tie, so the whole order is the tie-break -- partition
    first (`evidence` before `records`), then the identity pair.

    The artifact is deliberately named so that it sorts *after* both record ids. That is
    what makes the partition component load-bearing rather than incidental: drop it and
    this frontier comes back interleaved by bare identity, with the artifact last. Two
    partitions also have genuinely independent identity domains -- an artifact's second
    component is a content checksum and a record's is a version -- so without the partition
    separation the key's remaining components are not comparing like with like.
    """
    frozen = freeze(
        evidence=(evidence_member("zz-artifact", locator="archive://alpha.md"),),
        records=(
            record_member("rec-b", content={"body": QUERY}),
            record_member("rec-a", content={"body": QUERY}),
        ),
    )
    req = request()
    result = build_context_pack(frozen.frontier, context(req))

    validate(result, req=req, manifest=frozen.manifest)
    ordered = [
        citation.evidence_reference.evidence_id
        if isinstance(citation, ContextPackEvidenceCitation)
        else citation.record_reference.record_id
        for citation in result.citations
    ]
    assert ordered == ["zz-artifact", "rec-a", "rec-b"]


def test_supplemental_the_ranking_key_is_total_on_its_own_and_not_by_grace_of_the_freeze(
) -> None:
    """The identity tie-break, isolated from the sort that would otherwise mask it.

    The freeze already orders each partition by identity, and `list.sort` is stable -- so
    a ranking key that dropped its identity components entirely would still produce the
    right order for every frontier the freeze built. That is a genuine masking, and it
    makes the obvious tie-break test unable to fail.

    So this frontier is constructed *directly*, with both partitions in descending identity
    order, which is the one input the freeze can never hand over. Every candidate ties on
    relevance and on recorded instant, so the whole of the resulting order is the ranking
    key's own tie-break.

    Falsifier: drop the identity components from `_Selectable.order_key` and this returns
    the descending order it was given, because stability preserves the input.
    """
    unsorted = ContextPackFrozenFrontier(
        workspace_id=WORKSPACE_ID,
        evidence=(
            evidence_member("ev-b", locator="archive://alpha.md"),
            evidence_member("ev-a", locator="archive://alpha.md"),
        ),
        records=(
            record_member("rec-b", content={"body": QUERY}),
            record_member("rec-a", content={"body": QUERY}),
        ),
        history=(),
        context_models=(),
        filters_applied=CONTEXT_PACK_FRONTIER_FILTERS,
        candidate_set_checksum="sha256:" + "0" * 64,
        projection_versions=PROJECTION_VERSIONS,
        projection_watermarks=PROJECTION_WATERMARKS,
        projection_stale=False,
    )
    req = request()

    result = build_context_pack(unsorted, context(req))

    ordered = [
        citation.evidence_reference.evidence_id
        if isinstance(citation, ContextPackEvidenceCitation)
        else citation.record_reference.record_id
        for citation in result.citations
    ]
    assert ordered == ["ev-a", "ev-b", "rec-a", "rec-b"]


def test_supplemental_the_three_instants_are_one_supplied_string() -> None:
    """The instants are the context's, spelled once.

    The contract requires `canonical_resolution_time`, `generated_at` and
    `freshness.as_of` to be the same *string*, not merely the same moment: the artifact is
    content-addressed, so two spellings of one instant are two identities.

    Falsifier: the import check is what a value assertion cannot give. A builder that read
    a clock and happened to be called at the fixture instant would satisfy the equalities
    on this run and on no other.
    """
    result, _, _ = standard_pack()
    repro = result.reproducibility

    assert repro.canonical_resolution_time == RESOLUTION_TIME
    assert repro.generated_at == RESOLUTION_TIME
    assert repro.freshness.as_of == RESOLUTION_TIME
    assert set(module_imports(production_source())) & {
        "time", "datetime", "calendar", "zoneinfo"
    } == set()


def test_supplemental_the_freshness_is_the_supplied_projection_material_verbatim() -> None:
    """The FTS projection id, its active build digest, its source checkpoint, not stale.

    Falsifier: a builder that derived a freshness claim from anything else -- a second
    read, a default, a `stale=True` fallback -- would still validate against a permissive
    reader, so the exact values are asserted rather than the shape.
    """
    result, _, _ = standard_pack()
    freshness = result.reproducibility.freshness

    assert dict(freshness.projection_versions) == {FTS_PROJECTION_ID: FTS_BUILD_DIGEST}
    assert dict(freshness.projection_watermarks) == {
        FTS_PROJECTION_ID: FTS_SOURCE_CHECKPOINT
    }
    assert freshness.stale is False


def test_supplemental_relevance_dominates_recency_dominates_identity() -> None:
    """One ordering, and each component shown to be load-bearing on its own.

    `rec-c` scores twice and is the oldest and sorts last by identity, so it can only come
    first if relevance dominates. `rec-b` and `rec-a` tie on score, so recency separates
    them; `rec-a` and `rec-z` tie on score and instant, so identity separates them.

    Falsifier: a key missing any one component reorders at least one of the three pairs.
    """
    frozen = freeze(
        records=(
            record_member("rec-c", content={"body": "alpha alpha"},
                          recorded_at_us=BASE_US - 100),
            record_member("rec-b", content={"body": "alpha"},
                          recorded_at_us=BASE_US + 10),
            record_member("rec-a", content={"body": "alpha"}, recorded_at_us=BASE_US),
            record_member("rec-z", content={"body": "alpha"}, recorded_at_us=BASE_US),
        )
    )
    req = request()
    result = build_context_pack(frozen.frontier, context(req))

    validate(result, req=req, manifest=frozen.manifest)
    ranked = [
        citation.record_reference.record_id
        for citation in result.citations
        if isinstance(citation, ContextPackRecordCitation)
    ]
    assert ranked == ["rec-c", "rec-b", "rec-a", "rec-z"]


def test_the_frozen_frontier_is_disconnected_from_the_callers_own_mappings() -> None:
    """The copy, and why "frozen" would otherwise describe only the dataclass.

    `GovernedRecord.content` and `EvidenceArtifact.metadata` are opaque JSON: the DTOs are
    frozen dataclasses, the payloads inside them are ordinary mutable mappings, and the
    reader that produced them still holds a reference.

    Falsifier: without the copy the mutation below changes the matched surface, the emitted
    section content, the token count and therefore the pack's own identity -- a frontier
    frozen in name and live in fact.
    """
    content: dict[str, Any] = {"body": QUERY}
    metadata: dict[str, Any] = {"tags": ["one"]}
    live_artifact = replace(
        artifact("ev-1", locator="archive://alpha.md"), metadata=metadata
    )
    frozen = freeze(
        evidence=(FrozenEvidence(recorded_at_us=BASE_US, artifact=live_artifact),),
        records=(
            FrozenRecord(
                recorded_at_us=BASE_US,
                record=replace(record("rec-1"), content=content),
            ),
        ),
    )
    req = request()
    before = build_context_pack(frozen.frontier, context(req))

    content["body"] = "omega"
    content["injected"] = {"nested": ["mutated"]}
    metadata["tags"].append("two")

    after = build_context_pack(frozen.frontier, context(req))

    assert frozen.frontier.records[0].record.content is not content
    assert frozen.frontier.records[0].record.content == {"body": QUERY}
    # A tuple, not a list: the copy is the contract's own decode, which renders a JSON
    # array as an immutable sequence. `append` on the caller's list therefore cannot reach
    # it, and neither can anything else.
    assert frozen.frontier.evidence[0].artifact.metadata == {"tags": ("one",)}
    assert before.to_wire() == after.to_wire()
    assert '"omega"' not in to_canonical_json(after.to_wire())


def test_the_freeze_accepts_the_contracts_own_decoded_dtos() -> None:
    """The freeze must not refuse the one input shape a real handler will hand it.

    This is the regression for a copy that was `deepcopy`. Every DTO that reaches the later
    handler through the contract's decoder carries `MappingProxyType` opaque JSON, and
    `deepcopy` raises `TypeError: cannot pickle 'mappingproxy' object` on one -- so the
    freeze crashed on exactly the valid, canonical input it exists to take, and passed only
    on the hand-built dictionaries the tests happened to supply.

    Falsifier: `assert deepcopy` still raises on the same values. Without it this test
    would keep passing against a builder that had quietly gone back to `deepcopy` on some
    other input shape, and would look like a test of nothing.
    """
    decoded_artifact = EvidenceArtifact.from_wire(
        replace(
            artifact("ev-1", locator="archive://alpha.md"),
            metadata={"tags": ["one"], "nested": {"deep": ["x"]}},
        ).to_wire()
    )
    decoded_record = GovernedRecord.from_wire(
        record("rec-1", content={"body": QUERY, "nested": {"deep": ["x"]}}).to_wire()
    )
    assert isinstance(decoded_artifact.metadata, MappingProxyType)
    assert isinstance(decoded_record.content, MappingProxyType)
    with pytest.raises(TypeError):
        deepcopy(decoded_artifact)
    with pytest.raises(TypeError):
        deepcopy(decoded_record)

    frozen = freeze(
        evidence=(FrozenEvidence(recorded_at_us=BASE_US, artifact=decoded_artifact),),
        records=(FrozenRecord(recorded_at_us=BASE_US, record=decoded_record),),
    )
    req = request()
    result = build_context_pack(frozen.frontier, context(req))

    validate(result, req=req, manifest=frozen.manifest)
    assert len(result.sections) == 2


def test_the_frozen_frontiers_nested_json_cannot_be_mutated_after_the_freeze() -> None:
    """The other half of "deep": not merely a fresh copy, an immutable one.

    `deepcopy` on a hand-built DTO produced a *new mutable* dictionary. That disconnects
    the caller's own reference and leaves the frontier editable through the frontier -- so
    the same candidate could be matched, sectioned and digested as one value and then read
    back as another, which is the failure the freeze exists to prevent.

    Falsifier: every level is probed, not just the top one. A shallow guard would refuse
    the outermost assignment and admit the nested one, and the nested value is where a
    matched surface actually lives.
    """
    frozen = freeze(
        evidence=(
            FrozenEvidence(
                recorded_at_us=BASE_US,
                artifact=replace(
                    artifact("ev-1", locator="archive://alpha.md"),
                    metadata={"tags": ["one"], "nested": {"deep": ["x"]}},
                ),
            ),
        ),
        records=(
            record_member("rec-1", content={"body": QUERY, "nested": {"deep": ["x"]}}),
        ),
    )
    stored_metadata = frozen.frontier.evidence[0].artifact.metadata
    stored_content = frozen.frontier.records[0].record.content

    with pytest.raises(TypeError):
        stored_metadata["tags"] = ["two"]  # type: ignore[index]
    with pytest.raises(TypeError):
        stored_metadata["nested"]["deep"] = ["mutated"]  # type: ignore[index]
    with pytest.raises(AttributeError):
        stored_metadata["nested"]["deep"].append("mutated")
    with pytest.raises(TypeError):
        stored_content["body"] = "omega"  # type: ignore[index]
    with pytest.raises(TypeError):
        stored_content["nested"]["deep"] = ["mutated"]  # type: ignore[index]


def test_the_frozen_frontiers_own_freshness_maps_are_read_only() -> None:
    """A frozen frontier cannot be re-dated after the fact.

    Falsifier: a plain `dict` on the frontier would let a caller holding it rewrite the
    projection version between two builds, and the second pack would state a freshness the
    snapshot never had.
    """
    frozen = standard_freeze()

    with pytest.raises(TypeError):
        frozen.frontier.projection_versions[FTS_PROJECTION_ID] = "sha256:" + "c" * 64  # type: ignore[index]
    with pytest.raises(TypeError):
        frozen.frontier.projection_watermarks[FTS_PROJECTION_ID] = "chk-1"  # type: ignore[index]


def test_the_build_contexts_own_mappings_are_copied_at_construction() -> None:
    """One context instance, and the caller's dictionary can no longer reach it.

    `policy_versions` and `model_versions` arrive as caller-owned mappings. Without the copy
    at construction, "frozen dataclass" would describe only the field bindings: the caller
    still holds the dictionary, and a write to it between two builds from *one* context
    would give the two packs different policy attestations and therefore different pack ids
    -- the exact failure the frozen frontier's own copy exists to prevent, one layer up.

    Falsifier: all three routes are probed -- writing through the caller's original, writing
    through the stored view, and rebuilding from the same instance afterwards. A copy
    without the read-only wrapper would stop the first and admit the second.
    """
    caller_policies: dict[str, str] = {"governance": "policy-2", "evidence_acl": "policy-1"}
    frozen = standard_freeze()
    req = request()
    one_context = context(req, policy_versions=caller_policies)

    before = build_context_pack(frozen.frontier, one_context)
    caller_policies["evidence_acl"] = "policy-9"
    caller_policies["injected"] = "policy-x"
    after = build_context_pack(frozen.frontier, one_context)

    assert isinstance(one_context.policy_versions, MappingProxyType)
    assert isinstance(one_context.model_versions, MappingProxyType)
    assert dict(one_context.policy_versions) == {
        "evidence_acl": "policy-1", "governance": "policy-2"
    }
    # Sorted at construction, so the stored order is a function of the keys rather than of
    # how the caller happened to assemble the dictionary.
    assert list(one_context.policy_versions) == ["evidence_acl", "governance"]
    with pytest.raises(TypeError):
        one_context.policy_versions["evidence_acl"] = "policy-9"  # type: ignore[index]
    with pytest.raises(TypeError):
        one_context.model_versions["answerer"] = "model-9"  # type: ignore[index]
    assert before.to_wire() == after.to_wire()


def test_the_freshness_projection_id_has_not_drifted_from_the_projection_it_names(
) -> None:
    """The one duplicated literal in the production module, checked from the test side.

    `context_pack.py` restates `evidence.search.fts5` rather than importing it, because
    importing it would put an `omnivia_core_runtime.*` module in that file's import list and
    give up the isolation the F4 guard checks. Restating means it can drift -- and a
    freshness statement naming a projection this operation never read from is a claim about
    a snapshot rather than a report of one.

    So the comparison lives here, where importing runtime projection code costs nothing.
    This is the only place the two are ever brought together.
    """
    assert CONTEXT_PACK_FRESHNESS_PROJECTION_ID == EVIDENCE_SEARCH_PROJECTION_ID
    assert "omnivia_core_runtime.storage.projections" not in module_imports(
        production_source()
    )


# --- G1-G6: the freeze- and build-time invariants ------------------------------
#
# Every test below is a refusal. That is the point: each names an input the builder used
# to accept and write into a pack the contract's own validator would then approve, because
# the defect was never a malformed *artifact* -- it was a well-formed artifact stating
# something that was not true of the build that produced it. A validator cannot catch one
# of these, so the producer has to.


@pytest.mark.parametrize(
    ("name", "filters"),
    [
        ("empty", ()),
        ("reordered", tuple(reversed(CONTEXT_PACK_FRONTIER_FILTERS))),
        ("missing_one", CONTEXT_PACK_FRONTIER_FILTERS[:-1]),
        ("extra", (*CONTEXT_PACK_FRONTIER_FILTERS, "invented")),
        (
            "duplicated",
            (CONTEXT_PACK_FRONTIER_FILTERS[0], *CONTEXT_PACK_FRONTIER_FILTERS),
        ),
    ],
)
def test_g1_the_attested_filter_chain_must_be_the_chain_that_ran(
    name: str, filters: tuple[str, ...]
) -> None:
    """`filters_applied` is checked, not merely required to be present.

    Requiring the argument only proves a caller passed *something*. The frozen value's
    whole job is to state what narrowed it, and an unchecked statement is the one field of
    the frontier that nothing downstream can contradict -- the manifest vouches for which
    candidates were on it, never for what filtered them out.

    Falsifier: five spellings, each of which the previous freeze recorded verbatim.
    """
    with pytest.raises(ContextPackBuilderInputInvalid):
        freeze(records=(record_member("rec-1"),), filters_applied=filters)


def test_g2_a_candidate_from_another_workspace_is_refused() -> None:
    """Workspace isolation, at the one place this slice can actually enforce it.

    The freeze cannot prove the read that produced these candidates was workspace-scoped.
    What it can prove is that every candidate it was handed *says* it belongs to the
    workspace being frozen, and that a candidate saying otherwise stops the build rather
    than being dropped. Dropping would silently narrow the frontier the manifest then
    vouches for; the caller would get a pack that accounted for less than it ranked over.

    Falsifier: both partitions, since a check on one is no check on the other.
    """
    foreign_artifact = replace(
        artifact("ev-1", locator="archive://alpha.md"), workspace_id="ws-pack-0002"
    )
    foreign_record = replace(record("rec-1"), workspace_id="ws-pack-0002")

    with pytest.raises(ContextPackBuilderInputInvalid):
        freeze(
            evidence=(
                FrozenEvidence(recorded_at_us=BASE_US, artifact=foreign_artifact),
            )
        )
    with pytest.raises(ContextPackBuilderInputInvalid):
        freeze(records=(FrozenRecord(recorded_at_us=BASE_US, record=foreign_record),))


@pytest.mark.parametrize(
    ("name", "records", "history"),
    [
        (
            "superseded_in_records",
            (record_member("rec-1", version="v0", currentness="superseded"),),
            (),
        ),
        ("current_in_history", (), (record_member("rec-1"),)),
    ],
)
def test_g3_a_version_in_the_wrong_partition_is_refused(
    name: str, records: tuple[FrozenRecord, ...], history: tuple[FrozenRecord, ...]
) -> None:
    """The partition a version is frozen into is a claim about that version.

    `records` is what may become content and `history` is what may only be accounted for,
    so a superseded version in `records` is a pack presenting a stale claim as current, and
    a current version in `history` is a pack withholding content the caller was entitled to
    and calling it superseded. Both are refused rather than moved: this module cannot
    re-read the snapshot, so "repairing" the partition would be guessing at a stored fact.

    Falsifier: both directions, because a check in one direction leaves the other open.
    """
    with pytest.raises(ContextPackBuilderInputInvalid):
        freeze(records=records, history=history)


@pytest.mark.parametrize(
    ("name", "versions", "watermarks", "stale"),
    [
        ("empty", {}, {}, False),
        (
            "mismatched_keys",
            {CONTEXT_PACK_FRESHNESS_PROJECTION_ID: FTS_BUILD_DIGEST},
            {"other.projection": FTS_SOURCE_CHECKPOINT},
            False,
        ),
        (
            "wrong_projection",
            {"other.projection": FTS_BUILD_DIGEST},
            {"other.projection": FTS_SOURCE_CHECKPOINT},
            False,
        ),
        (
            "extra_projection",
            {
                CONTEXT_PACK_FRESHNESS_PROJECTION_ID: FTS_BUILD_DIGEST,
                "other.projection": FTS_BUILD_DIGEST,
            },
            {
                CONTEXT_PACK_FRESHNESS_PROJECTION_ID: FTS_SOURCE_CHECKPOINT,
                "other.projection": FTS_SOURCE_CHECKPOINT,
            },
            False,
        ),
        ("stale", PROJECTION_VERSIONS, PROJECTION_WATERMARKS, True),
    ],
)
def test_g4_the_freshness_statement_must_name_this_operations_own_projection(
    name: str,
    versions: Mapping[str, str],
    watermarks: Mapping[str, str],
    stale: bool,
) -> None:
    """Which projection served a read is a fact the builder knows; the values are not.

    So the key set is pinned and the values stay supplied. The empty case matters most:
    two empty maps produce a `ProjectionFreshness` the contract refuses outright, so the
    previous freeze would build a frontier whose every pack was invalid -- discovered at
    validation, by which point the work is done and the failure names the wrong layer.

    `stale=True` is refused for a different reason: it is not malformed, it is *true*. A
    v0.6 build served from a projection behind the write model would be a correctly
    self-describing pack that no caller should act on, so it is refused at the freeze
    rather than produced and disclaimed.

    Falsifier: every one of these five produced a frontier before, and four of them
    produced a pack.
    """
    with pytest.raises(ContextPackBuilderInputInvalid):
        freeze(
            records=(record_member("rec-1"),),
            projection_versions=versions,
            projection_watermarks=watermarks,
            projection_stale=stale,
        )


@pytest.mark.parametrize("supplied", ["beta", "ALPHA", "alpha ", ""])
def test_g5_the_query_ranked_and_the_query_attested_are_one_value(
    supplied: str,
) -> None:
    """The pack's normalized query must be the form it actually matched on.

    The builder normalized the request itself to rank and then wrote down whatever
    `normalized_query` it was handed. Those are two independent values, and nothing in the
    artifact could ever show they disagreed: a pack could attest `"beta"` while every
    section in it was selected by matching `"alpha"`, and it would validate.

    `"ALPHA"` is the case that shows this is an equality check rather than a
    case-insensitive comparison -- the module's normalization case-folds, so the supplied
    form has to be the *folded* one, not merely an equivalent one.

    Falsifier: the request's own query is `"alpha"`, so the correct value is accepted in
    every other test in this file, and each of these four is refused.
    """
    frozen = standard_freeze()
    req = request()

    with pytest.raises(ContextPackBuilderInputInvalid):
        build_context_pack(frozen.frontier, context(req, normalized_query=supplied))


def test_g5b_the_verified_normalized_query_is_what_ranking_actually_used() -> None:
    """The equality is checked *and* the checked value is the one that ranks.

    Falsifier: a builder could satisfy G5 and still rank on something else. So this uses a
    query whose normalized form differs from its raw form -- `"ALPHA"` folds to `"alpha"` --
    and requires both that the folded form is what the pack attests and that the candidates
    matching the folded form are the ones selected. Matching the raw `"ALPHA"` against the
    canonical JSON below finds nothing and would return an empty pack.
    """
    frozen = standard_freeze()
    req = request(query="ALPHA")
    result = build_context_pack(frozen.frontier, context(req, normalized_query="alpha"))

    validate(result, req=req, manifest=frozen.manifest, normalized_query="alpha")
    assert result.query == "ALPHA"
    assert result.reproducibility.normalized_request.normalized_query == "alpha"
    assert len(result.sections) == 2


@pytest.mark.parametrize(
    ("name", "summarizer_version", "model_versions"),
    [
        ("summarizer_named", "summarizer-1", None),
        ("summarizer_empty", "", None),
        ("model_named", None, {"answerer": "model-1"}),
        ("both", "summarizer-1", {"answerer": "model-1"}),
    ],
)
def test_g6_a_component_that_never_ran_may_not_be_given_a_version(
    name: str, summarizer_version: str | None, model_versions: Mapping[str, str] | None
) -> None:
    """No summarizer and no model runs in this build, so those are the only true values.

    This is the one place the "everything supplied is recorded verbatim" rule is wrong.
    The nine other version fields are producer facts this module genuinely cannot know, so
    recording them unread is the honest thing to do. Whether a summarizer ran is not one of
    those: this module is the producer, and it knows nothing summarized anything. Writing
    down `"summarizer-1"` would be a fabricated claim that verifies -- worse than a missing
    one, because a reader can check a missing field and cannot check an invented one.

    Falsifier: the replaced test supplied `{"planner": "model-1", "answerer": "model-2"}`
    and asserted the builder rendered them in sorted order. It passed, and what it proved
    was that the builder faithfully recorded two models that had never run.
    """
    frozen = standard_freeze()
    req = request()

    with pytest.raises(ContextPackBuilderInputInvalid):
        build_context_pack(
            frozen.frontier,
            context(
                req,
                summarizer_version=summarizer_version,
                model_versions=model_versions,
            ),
        )


@pytest.mark.parametrize(
    "field",
    [
        "builder_version",
        "normalization_version",
        "ranking_version",
        "reranking_version",
        "selection_version",
        "tokenizer_id",
        "tokenizer_version",
    ],
)
def test_g6b_a_version_for_an_algorithm_this_module_owns_may_not_be_a_free_label(
    field: str,
) -> None:
    """The seven identities `context_pack.py` implements are pinned, not accepted.

    This module used to take all of these as free strings on the ground that a producer
    should not invent versions about itself. That reasoning is right about
    `retrieval_version` and wrong about these seven: the normalization *is* `_normalize`,
    the ranking *is* `order_key`, the selection *is* the greedy pass, the tokenizer *is* the
    module's own regex, the builder is the module, and no reranker exists. A caller free to
    name a version for code it does not own can state one that is untrue and verifies --
    which is the same defect `summarizer_version` was already closed against.

    Falsifier: before the constants existed, every value below produced a pack the
    contract's own validator accepted, attesting an algorithm version nobody could check.
    """
    frozen = standard_freeze()
    req = request()
    mislabelled = replace(context(req), **{field: "invented-9"})

    with pytest.raises(ContextPackBuilderInputInvalid):
        build_context_pack(frozen.frontier, mislabelled)


def test_g6c_the_reranker_is_disabled_rather_than_versioned() -> None:
    """`disabled` is a statement about this build, not a placeholder.

    There is no reranking pass in this module at all, so the only two documents that could
    be produced are one saying `disabled` and one naming a reranker that never ran. The
    second is the fabricated-but-verifying claim, so it is refused -- the same rule
    `CONTEXT_PACK_SUMMARIZER_DISABLED` already carries, applied to the other absent stage.
    """
    result, _, _ = standard_pack()

    assert CONTEXT_PACK_RERANKER_DISABLED == CONTEXT_PACK_SUMMARIZER_DISABLED == "disabled"
    assert result.reproducibility.reranking_version == CONTEXT_PACK_RERANKER_DISABLED
    assert result.reproducibility.summarizer_version == CONTEXT_PACK_SUMMARIZER_DISABLED


def test_g7_the_context_and_the_frontier_must_name_one_workspace() -> None:
    """The authorization attested and the material ranked belong to one workspace.

    Falsifier: unchecked, a pack could record workspace A's authority context over a
    frontier frozen for workspace B, and every other rule in the contract would hold --
    the manifest vouches for B, the authority context says A, and nothing compares them.
    """
    frozen = standard_freeze()
    req = request()

    with pytest.raises(ContextPackBuilderInputInvalid):
        build_context_pack(frozen.frontier, context(req, workspace_id="ws-pack-0002"))


def test_g8_the_returned_pack_is_transitively_immutable_and_still_its_own_digest(
) -> None:
    """A sealed pack that can be edited is a pack that is no longer what it addresses.

    The result used to be a frozen dataclass wrapped around ordinary dictionaries: the
    selected records' opaque `content`, the artifacts' `metadata` and the reproducibility
    record's four mapping fields were all writable through the value the caller received.
    Editing any of them leaves `pack_id` naming a document that no longer exists.

    Falsifier: both halves. Immutability alone would be satisfiable by a builder that
    rebuilt the result into a different value, so the digest is recomputed here by the
    contract's own helper and required to still be the one the pack carries.
    """
    result, frozen, req = standard_pack()

    validate(result, req=req, manifest=frozen.manifest)
    with pytest.raises(TypeError):
        result.records[0].content["body"] = "omega"  # type: ignore[index]
    with pytest.raises(TypeError):
        result.evidence[0].metadata["injected"] = "x"  # type: ignore[index]
    with pytest.raises(TypeError):
        result.reproducibility.authorization_context.policy_versions[  # type: ignore[index]
            "evidence_acl"
        ] = "policy-9"
    with pytest.raises(TypeError):
        result.reproducibility.model_versions["answerer"] = "model-9"  # type: ignore[index]
    with pytest.raises(TypeError):
        result.reproducibility.freshness.projection_versions[  # type: ignore[index]
            FTS_PROJECTION_ID
        ] = "sha256:" + "c" * 64
    with pytest.raises(TypeError):
        result.reproducibility.freshness.projection_watermarks[  # type: ignore[index]
            FTS_PROJECTION_ID
        ] = "chk-1"

    assert reseal(result).pack_id == result.pack_id
    assert result.reproducibility.artifact_checksum == result.pack_id


def test_g9_no_refusal_names_the_material_it_refused() -> None:
    """A refusal states the rule; the candidate is the one thing it must not carry.

    This module's whole claim is that it cannot leak what it was handed, and an exception
    string is the most natural place for that claim to quietly stop being true -- an
    `f"{candidate!r}"` in a message is one keystroke and it would put an evidence id, a
    record id or a slice of opaque JSON into whatever the handler logs.

    Falsifier: two refusals whose triggering values are deliberately distinctive strings,
    both required to be absent from the message.
    """
    marked_artifact = replace(
        artifact("ev-leaky", locator="archive://leaky-locator.md"),
        workspace_id="ws-pack-0002",
    )
    with pytest.raises(ContextPackBuilderInputInvalid) as freeze_refusal:
        freeze(
            evidence=(FrozenEvidence(recorded_at_us=BASE_US, artifact=marked_artifact),)
        )
    message = str(freeze_refusal.value)
    assert "ev-leaky" not in message
    assert "leaky-locator" not in message
    assert "ws-pack-0002" not in message

    with pytest.raises(ContextPackBuilderInputInvalid) as build_refusal:
        build_context_pack(
            standard_freeze().frontier,
            context(request(), normalized_query="leaky-normalized-query"),
        )
    assert "leaky-normalized-query" not in str(build_refusal.value)


# --- F1-F4: forgery and isolation ---------------------------------------------


#: The four members of a response that carry what the caller actually received. Everything
#: the F-series claims about "byte identical" is claimed about exactly these, read off the
#: wire object rather than compared as DTOs, because the wire object is what a caller sees.
SELECTED_CONTENT_MEMBERS = ("sections", "citations", "evidence", "records")


def selected_content(result: ContextPackBuildResult) -> dict[str, Any]:
    wire = result.to_wire()
    return {member: wire[member] for member in SELECTED_CONTENT_MEMBERS}


def assert_forgery_is_invisible_in_the_response_and_caught_by_the_manifest(
    honest: ContextPackBuildResult,
    honest_freeze: ContextPackFreeze,
    widened: ContextPackBuildResult,
    widened_freeze: ContextPackFreeze,
    req: ContextPackBuildInput,
) -> None:
    """The shared shape of F1, F2 and F3, asserted once.

    Four claims, in the order that makes the attack legible:

    1. the honest pack verifies against the honest manifest -- otherwise nothing below is
       about a forgery;
    2. the widened pack verifies against *its own* manifest, because the producer is
       internally consistent. This is what makes the defect invisible from inside;
    3. the selected response content is byte-identical between the two, so no assertion
       about the page a caller received could distinguish them; and yet
    4. the candidate checksums differ, and the widened pack is refused by the
       independently held narrow manifest.
    """
    validate(honest, req=req, manifest=honest_freeze.manifest)
    validate(widened, req=req, manifest=widened_freeze.manifest)

    assert selected_content(widened) == selected_content(honest)
    assert (
        widened.reproducibility.authorization_context.authorized_candidate_set_checksum
        != honest.reproducibility.authorization_context.authorized_candidate_set_checksum
    )

    with pytest.raises(ContractSemanticError, match="authorized_candidate_set_checksum"):
        validate(widened, req=req, manifest=honest_freeze.manifest)


def test_f1_shape_a_frontier_frozen_around_more_than_the_chain_authorized_is_caught(
) -> None:
    """The *consequence* half of F1: the manifest sees what the response cannot.

    **This is not F1, and the module docstring says so first.** F1 names a defect in the
    production authorization chain -- an ACL or sensitivity filter relocated from before the
    freeze to after ranking. That chain is `local_owner_label_grant`, `authorized_frontier`
    and the governed resolver; none of them runs in this two-file slice, nothing here
    filters, and so there is no filter here to move. The candidate below is dropped because
    its locator does not contain the query, and a query non-match is not an ACL decision.
    Only the later handler slice, running the real chain against a database, can close F1.

    What this test does establish is the half that does not need the chain: *if* a filter
    ran too late, the material it should have removed is frozen, digested into the
    frontier's own checksum and ranked -- and every byte the caller receives is unchanged, so
    no assertion about the response could catch it. The honest narrow manifest, held
    independently and produced before ranking, is the only thing that refuses.

    The direction matters. Widening the *verifier's* expected manifest and observing that
    two checksums differ demonstrates only that a checksum is a function of its input. The
    attack is the other way round: an honest narrow manifest refusing a pack whose producer
    ranked over more than it was entitled to.

    Falsifier: run the same comparison over `sections`, `citations`, `evidence` and
    `records` and every one of them matches.
    """
    honest_freeze = standard_freeze()
    unauthorized = evidence_member(
        "ev-unauthorized", locator="archive://beta-unauthorized.md"
    )
    widened_freeze = standard_freeze(extra_evidence=(unauthorized,))
    req = request()

    honest = build_context_pack(honest_freeze.frontier, context(req))
    widened = build_context_pack(widened_freeze.frontier, context(req))

    assert_forgery_is_invisible_in_the_response_and_caught_by_the_manifest(
        honest, honest_freeze, widened, widened_freeze, req
    )
    # Frozen, ranked, and then dropped: the candidate really did reach the frontier.
    assert f"{CONTEXT_PACK_CANDIDATE_PARTITION_EVIDENCE}/ev-unauthorized@" in "".join(
        omission.path or "" for omission in widened.omissions
    )
    assert OMISSION_NOT_MATCHED in {
        omission.code
        for omission in widened.omissions
        if (omission.path or "").startswith(
            f"{CONTEXT_PACK_CANDIDATE_PARTITION_EVIDENCE}/ev-unauthorized@"
        )
    }


#: F2's own frontier, and the reason it is not the standard one.
#:
#: F2 requires the unauthorized candidate to be *last* in the production ranker's total
#: order while still scoring, and the standard frontier cannot deliver that. Its ranking key
#: is relevance first, so a candidate that matches once outranks the two standard members
#: that match not at all: on `standard_freeze` the injected candidate lands third of five,
#: and "ranked last" would simply be untrue.
#:
#: So every honest selectable here matches the normalized query at least twice, and the
#: injected candidate matches exactly once. Relevance alone then puts it last, which makes
#: the claim independent of the recency and identity tie-breaks -- a frontier that had to
#: rely on those would be asserting the ranker's tie-break rather than its ordering.
#: `history` stays, because it is what keeps the manifests non-trivial.
def f2_freeze(
    *, extra_records: Sequence[FrozenRecord] = ()
) -> ContextPackFreeze:
    return freeze(
        evidence=(
            evidence_member("ev-1", locator="archive://alpha-alpha.md"),
            evidence_member("ev-2", locator="archive://alpha-alpha-alpha.md"),
        ),
        records=(
            record_member("rec-1", content={"body": "alpha alpha"}),
            record_member("rec-2", content={"body": "alpha alpha alpha"}),
            *extra_records,
        ),
        history=(record_member("rec-1", version="v0", currentness="superseded"),),
    )


#: The four honest candidates in the exact order the production ranker must put them in:
#: relevance descending (three, three, two, two), and inside each pair the partition, since
#: every member is stamped with the same instant. Written out rather than derived, because a
#: derived expectation is the ranker restated in the test.
F2_HONEST_RANKED_ORDER = [
    (CONTEXT_PACK_CANDIDATE_PARTITION_EVIDENCE, "ev-2",
     artifact("ev-2", locator="x").content_checksum),
    (CONTEXT_PACK_CANDIDATE_PARTITION_RECORDS, "rec-2", "v1"),
    (CONTEXT_PACK_CANDIDATE_PARTITION_EVIDENCE, "ev-1",
     artifact("ev-1", locator="x").content_checksum),
    (CONTEXT_PACK_CANDIDATE_PARTITION_RECORDS, "rec-1", "v1"),
]

F2_UNAUTHORIZED_IDENTITY = (
    CONTEXT_PACK_CANDIDATE_PARTITION_RECORDS, "rec-unauthorized", "v1"
)
F2_UNAUTHORIZED_PATH = "{}/{}@{}".format(*F2_UNAUTHORIZED_IDENTITY)


def test_f2_one_unauthorized_item_ranked_last_and_dropped_on_the_budget_is_caught(
) -> None:
    """The literal F2: scored by the real ranker, ranked last by it, dropped on the budget.

    The F1-shape candidate never scored, so a reader could object that it was never really
    in contention and that "ranked last" was an accident of it having no score at all. This
    one carries the query, so the production scorer gives it a positive relevance and it
    competes -- and the frontier is built so that every honest candidate outscores it, which
    is what makes *last* a statement about the ranking rather than about a tie-break.

    Five claims, each asserted against the production path rather than a restatement of it:

    1. **the ranker really included it, and really ranked it last.** The order is read off
       `context_pack._selectables` -- the module's own ranking function, the one
       `build_context_pack` calls, over the same widened frontier and the same needle the
       pack itself attests. The whole ranked identity order is compared, not just its tail,
       so a ranker that had started dropping honest candidates would fail here too;
    2. **it scored.** Its relevance is positive and every honest candidate's is strictly
       greater, so it was in contention and lost the ordering, rather than being filtered;
    3. **selection dropped it**, on the budget, which is set to exactly the honest pack's own
       `tokens_used` -- so the honest four still fit exactly and the fifth cannot;
    4. **the caller cannot tell.** `sections`, `citations`, `evidence` and `records` are
       byte-identical to the honest pack's;
    5. **the independently held narrow manifest refuses it**, on the candidate checksum.

    That is the more dangerous shape of the defect, because the unauthorized material
    genuinely competed for the caller's budget: had it been more relevant it would have
    displaced honest content, and the response would still have looked conformant.
    """
    honest_freeze = f2_freeze()
    exact_budget = build_context_pack(
        honest_freeze.frontier, context(request())
    ).budget.tokens_used
    req = request(token_budget=exact_budget)

    unauthorized = record_member("rec-unauthorized", content={"body": QUERY})
    widened_freeze = f2_freeze(extra_records=(unauthorized,))

    honest = build_context_pack(honest_freeze.frontier, context(req))
    widened = build_context_pack(widened_freeze.frontier, context(req))

    # 1 and 2. The production ranker, run over the production frontier, with the needle the
    # widened pack attests -- which `build_context_pack` has already required to be this
    # module's own normalization of the request's query.
    needle = widened.reproducibility.normalized_request.normalized_query
    ranked = context_pack_module._selectables(widened_freeze.frontier, needle)
    assert [
        (item.partition, item.first, item.second) for item in ranked
    ] == [*F2_HONEST_RANKED_ORDER, F2_UNAUTHORIZED_IDENTITY]
    assert ranked[-1].relevance > 0
    assert all(item.relevance > ranked[-1].relevance for item in ranked[:-1])

    # 3. Selection dropped it, and dropped it on the budget rather than on anything else.
    assert honest.budget.tokens_used == exact_budget
    assert widened.budget.tokens_used == exact_budget
    assert F2_UNAUTHORIZED_IDENTITY not in selected_identities(widened)
    assert {
        omission.code
        for omission in widened.omissions
        if omission.path == F2_UNAUTHORIZED_PATH
    } == {OMISSION_TOKEN_BUDGET}
    assert selected_identities(widened) == set(F2_HONEST_RANKED_ORDER)

    # 4 and 5. Identical bytes to the caller; refused by the narrow manifest.
    assert_forgery_is_invisible_in_the_response_and_caught_by_the_manifest(
        honest, honest_freeze, widened, widened_freeze, req
    )
    assert F2_UNAUTHORIZED_IDENTITY not in frontier_identities(honest_freeze.manifest)
    assert F2_UNAUTHORIZED_IDENTITY in frontier_identities(widened_freeze.manifest)


def test_f3_the_hardcoded_pre_ranking_boolean_does_not_rescue_the_widened_pack() -> None:
    """A boolean attestation is not a check, demonstrated rather than argued.

    `pre_ranking_authorization_enforced` is written as a literal `True` in the production
    source -- asserted here, so the claim is about the code rather than about a fixture --
    and the contract requires it to be `true`. It is `true` on the widened pack, which is
    the pack whose producer ranked over an unauthorized candidate. It is also literally
    accurate about that build: the frontier really was frozen before ranking. It was simply
    frozen around the wrong set.

    So the flag is necessary and carries no evidential weight on its own, and the same
    narrow-manifest verification refuses regardless of it.
    """
    honest_freeze = standard_freeze()
    unauthorized = evidence_member(
        "ev-unauthorized", locator="archive://beta-unauthorized.md"
    )
    widened_freeze = standard_freeze(extra_evidence=(unauthorized,))
    req = request()

    honest = build_context_pack(honest_freeze.frontier, context(req))
    widened = build_context_pack(widened_freeze.frontier, context(req))

    assert "pre_ranking_authorization_enforced=True" in production_source()
    for pack in (honest, widened):
        assert pack.reproducibility.authorization_context.\
            pre_ranking_authorization_enforced is True

    assert_forgery_is_invisible_in_the_response_and_caught_by_the_manifest(
        honest, honest_freeze, widened, widened_freeze, req
    )
    assert widened.reproducibility.authorization_context.\
        pre_ranking_authorization_enforced is True


# --- F4: the structural guard, run against executed mutations -------------------
#
# The guard is a function rather than a sequence of assertions, so it can be run against
# something other than the file it was written for. A guard that only ever inspects the
# production module proves that the production module has not changed -- not that the
# guard would notice if it had.
#
# Every mutant below is *executed*, and that is the substance of this section rather than a
# detail of it. A mutation that only changes a signature's shape proves the guard reads
# signatures; it does not prove that the smuggled handle would have been used, and a
# prohibition on a shape nobody would have called is a prohibition on nothing. So each of the
# four forms is written so the mutated build path performs one extra candidate read through
# a handle this file owns, and each branch requires three separate things: that the read
# actually ran, that what it returned reached the pack, and that the guard kills the form --
# while finding nothing in the production file.
#
# No database is opened anywhere here. The "row" every mutant reads is a `FrozenRecord` this
# file constructs, handed back by a tracing store this file constructs, and the one mutant
# that carries `import sqlite3` carries it as the import-clause specimen and never connects.

#: The exact parameters each entry point may have. An exact set rather than a forbidden
#: list: a callback named `hydrate` is as much a store as one named `connection`, and only
#: an allow-list refuses a name nobody thought to forbid.
GUARDED_SIGNATURES: Mapping[str, frozenset[str]] = {
    "freeze_context_pack_frontier": frozenset(
        {
            "workspace_id",
            "evidence",
            "records",
            "history",
            "filters_applied",
            "projection_versions",
            "projection_watermarks",
            "projection_stale",
        }
    ),
    "build_context_pack": frozenset({"frontier", "context"}),
}


def production_source() -> str:
    return Path(inspect.getsourcefile(context_pack_module) or "").read_text()


def module_imports(source: str) -> set[str]:
    """Every module named by an `import` statement, read from the source.

    From the source rather than the runtime namespace, so a lazy import inside a function
    body does not evade it.
    """
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    return imported


def purity_violations(source: str, module: ModuleType) -> tuple[str, ...]:
    """Every way this module could still reach a row, as a list of findings.

    Four hiding places, none substituting for another: the import list (what a signature
    cannot show), the parameters (what an import list cannot show -- a callback imports
    nothing), the module globals, and the closure cells. A finding is returned rather than
    raised so a mutation test can assert *which* prohibition fired.

    A clock is the fifth thing this module may not reach, and it is checked here rather than
    by a bare assertion over the production import list for the reason D10 states: a module
    that has no clock today and an assertion that says so are the same statement, and
    neither notices the day someone adds one. Reading a wall clock is not a purity violation
    in general -- it is one *here*, because this artifact is content-addressed and a
    generation instant read rather than supplied gives two identical builds two identities.
    """
    findings: list[str] = []

    for name in sorted(module_imports(source)):
        if name == "sqlite3" or name.split(".")[0] == "omnivia_core_runtime":
            findings.append(f"import:{name}")
        if name.split(".")[0] in {"time", "datetime", "calendar", "zoneinfo"}:
            findings.append(f"clock:{name}")

    for name, value in sorted(vars(module).items()):
        if name.startswith("__"):
            continue
        if hasattr(value, "execute") or hasattr(value, "cursor"):
            findings.append(f"global:{name}")

    for name, allowed in sorted(GUARDED_SIGNATURES.items()):
        function = getattr(module, name)
        if function.__closure__ is not None:
            findings.append(f"closure:{name}")
        signature = inspect.signature(function)
        for parameter in signature.parameters.values():
            if parameter.name not in allowed:
                findings.append(f"parameter:{name}.{parameter.name}")
            if parameter.default is not inspect.Parameter.empty:
                findings.append(f"default:{name}.{parameter.name}")

    return tuple(findings)


@contextmanager
def load_mutant(
    source: str, name: str, namespace: Mapping[str, object] | None = None
) -> Iterator[ModuleType]:
    """Import one mutated copy of the production module, and unregister it after.

    Registered in `sys.modules` for the duration because `@dataclass(slots=True)` resolves
    its own module to re-create the class, and removed again so no mutant outlives the
    branch that built it.

    `namespace` is installed *before* the source runs, and it is how the two forms whose
    handle is not a parameter acquire one: a module-global store the source never assigns --
    which is exactly what a handle installed at import looks like, and exactly what no
    source-level import scan can see -- and, for the closure form, an opener the mutated
    source calls once at import so the store ends up in a cell and in no global. Every name
    seeded is this file's own object; nothing production-owned is injected.
    """
    module = ModuleType(name)
    module.__file__ = f"<{name}>"
    module.__dict__.update(namespace or {})
    sys.modules[name] = module
    try:
        exec(compile(source, f"<{name}>", "exec"), module.__dict__)  # noqa: S102
        yield module
    finally:
        sys.modules.pop(name, None)


#: The four mutations §20.12 names, each as an exact edit to the production source. The
#: anchors are asserted to have matched, so a refactor of the production file makes this
#: test fail loudly instead of silently mutating nothing and passing.
_STORE_PARAMETER_ANCHOR = (
    "def build_context_pack(\n"
    "    frontier: ContextPackFrozenFrontier, context: ContextPackBuildContext\n"
    ") -> ContextPackBuildResult:"
)
_IMPORT_ANCHOR = "import re\n"

#: The point on the build path every executable mutant inserts its read at: the last line
#: before the selection loop, and therefore before `_selectables` scores and orders anything.
#: A read placed here is a read the ranking actually consumes.
_BUILD_PATH_ANCHOR = "    budget = request.token_budget\n"

#: D10's anchor: the one line that renders the generation instant from the supplied one.
#: Asserted before it is replaced, so a refactor of the production file fails this test
#: loudly rather than mutating nothing and passing.
_GENERATED_AT_ANCHOR = "        generated_at=resolution,\n"

#: The one statement every mutant issues, and none issues twice. It is SQL-shaped because
#: that is what an extra-row read looks like; nothing executes it, and the "row" it returns
#: is the `FrozenRecord` :class:`_TracingStore` hands back.
SMUGGLED_READ = "SELECT record_id, version FROM governed_record_value LIMIT 1"

#: The identity that read returns: a governed record on no frozen frontier and in no
#: manifest, carrying the query so the production ranker selects and sections it. Its
#: presence in a pack is what makes "the read would actually have happened" a fact about the
#: output rather than a claim about a signature.
SMUGGLED_RECORD_ID = "rec-smuggled"


class _TracingStore:
    """A live handle owned by this file: the two attributes a connection has, and a log.

    A poison store rather than a stub. Nothing in this file ever hands it to production code,
    so every entry in `reads` is a read some *mutated* copy of the production source
    performed -- and an empty log after a mutant has been invoked would mean the mutation was
    inert and the branch was proving nothing.
    """

    def __init__(self) -> None:
        self.reads: list[str] = []

    def execute(self, statement: str) -> FrozenRecord:
        self.reads.append(statement)
        return record_member(SMUGGLED_RECORD_ID, content={"body": QUERY})

    def cursor(self) -> object:
        return self


def _extra_read(call: str) -> str:
    """The one line an executable mutant adds, with the handle expression spliced in.

    It widens the frozen frontier the builder was handed by one candidate obtained from the
    handle, and leaves everything else -- scoring, ordering, selection, sectioning, digesting
    -- to the production code the mutant did not touch. `replace` is already imported by the
    module, so the mutation adds no import and remains a pure edit to the build path.
    """
    return (
        "    frontier = replace(\n"
        f"        frontier, records=(*frontier.records, {call})\n"
        "    )\n"
    ) + _BUILD_PATH_ANCHOR


def _closure_mutation(read: str) -> str:
    """The closure form: the handle lives in a cell, and in nothing a scan can enumerate.

    `_bind` is called once at import with an opener this file seeded, so the store is
    reachable through `build_context_pack.__closure__` and through no module global, no
    import and no parameter. That is what makes `closure:` the only finding this branch
    produces, and it is why the seeded name is an opener rather than the store itself.
    """
    return f'''

_PRODUCTION_BUILD = build_context_pack


def _bind(bound: object) -> object:
    def build_context_pack(
        frontier: ContextPackFrozenFrontier, context: ContextPackBuildContext
    ) -> ContextPackBuildResult:
        return _PRODUCTION_BUILD(
            replace(frontier, records=(*frontier.records, bound.execute({read!r}))),
            context,
        )

    return build_context_pack


build_context_pack = _bind(_open_store())
'''


def mutate(form: str) -> tuple[str, str]:
    """Return `(mutated source, the violation that mutation must produce)`."""
    source = production_source()
    if form == "parameter":
        assert _STORE_PARAMETER_ANCHOR in source
        assert _BUILD_PATH_ANCHOR in source
        mutated = source.replace(
            _STORE_PARAMETER_ANCHOR,
            "def build_context_pack(\n"
            "    frontier: ContextPackFrozenFrontier,\n"
            "    context: ContextPackBuildContext,\n"
            "    *,\n"
            "    store: object,\n"
            ") -> ContextPackBuildResult:",
        ).replace(
            _BUILD_PATH_ANCHOR, _extra_read(f"store.execute({SMUGGLED_READ!r})"), 1
        )
        return mutated, "parameter:build_context_pack.store"
    if form == "import_and_global":
        assert _IMPORT_ANCHOR in source
        assert _BUILD_PATH_ANCHOR in source
        # `import sqlite3` is the import-clause specimen and is never used: this branch
        # smuggles a *store*, and the store is the one this file owns and can trace. The
        # runtime import is real, and is what the second import finding is about.
        mutated = source.replace(
            _IMPORT_ANCHOR,
            "import re\nimport sqlite3\n",
            1,
        ).replace(
            _BUILD_PATH_ANCHOR, _extra_read(f"_STORE.execute({SMUGGLED_READ!r})"), 1
        ) + (
            "\n\nfrom omnivia_core_runtime.storage import repository  # noqa: E402\n"
            "\n_REPOSITORY = repository\n"
            "# `_STORE` is deliberately not assigned here. It is installed into this\n"
            "# module's namespace at load, which is the shape a handle acquired by an\n"
            "# import-time factory has and the one no source-level scan can see. The\n"
            "# guard's globals clause is what catches it.\n"
        )
        return mutated, "global:_STORE"
    if form == "closure":
        return (
            source + _closure_mutation(SMUGGLED_READ),
            "closure:build_context_pack",
        )
    if form == "clock":
        # D10's mutation, and the only one that edits *behaviour* rather than shape: the
        # supplied-instant discipline is removed -- the resolution instant stops being the
        # one field the three timestamps are rendered from -- and a wall-clock read takes
        # its place for `generated_at`. This is the exact edit someone makes when they
        # decide a pack should record when it was emitted.
        assert _IMPORT_ANCHOR in source
        assert _GENERATED_AT_ANCHOR in source
        mutated = source.replace(_IMPORT_ANCHOR, "import datetime\nimport re\n", 1)
        mutated = mutated.replace(
            _GENERATED_AT_ANCHOR,
            "        generated_at=datetime.datetime.now(datetime.UTC).isoformat(),\n",
            1,
        )
        return mutated, "clock:datetime"
    assert form == "callback"
    assert _STORE_PARAMETER_ANCHOR in source
    assert _BUILD_PATH_ANCHOR in source
    mutated = source.replace(
        _STORE_PARAMETER_ANCHOR,
        "def build_context_pack(\n"
        "    frontier: ContextPackFrozenFrontier,\n"
        "    context: ContextPackBuildContext,\n"
        "    *,\n"
        "    fetch_candidate: object = None,\n"
        ") -> ContextPackBuildResult:",
    ).replace(
        _BUILD_PATH_ANCHOR, _extra_read(f"fetch_candidate({SMUGGLED_READ!r})"), 1
    )
    return mutated, "parameter:build_context_pack.fetch_candidate"


def mutant_namespace(form: str, store: _TracingStore) -> dict[str, object]:
    """What each form needs installed before its mutated source runs.

    Two of the four forms take their handle from somewhere a caller cannot reach, so it has
    to be installed rather than passed. The difference between them is the point of the
    split: `import_and_global` installs the store itself as a module global, and `closure`
    installs only an opener, so that the store is in a cell and in nothing else.
    """
    if form == "import_and_global":
        return {"_STORE": store}
    if form == "closure":
        return {"_open_store": lambda: store}
    return {}


def invoke_mutant(
    mutant: ModuleType, form: str, store: _TracingStore
) -> ContextPackBuildResult:
    """Run the mutated build path over an honest frontier and an honest build context.

    Honest on purpose: nothing about the *input* here is unauthorized, and the frontier is
    the same `standard_freeze` the rest of this file uses. So any identity in the output that
    the honest pack does not carry arrived through the smuggled read and through nothing
    else.
    """
    frozen = standard_freeze()
    req = request()
    if form == "parameter":
        result = mutant.build_context_pack(frozen.frontier, context(req), store=store)
    elif form == "callback":
        # A bound method: a callback that imports nothing, appears in no module global and
        # is visible only in the signature -- which is the whole reason the guard's parameter
        # allow-list is an allow-list rather than a list of forbidden names.
        result = mutant.build_context_pack(
            frozen.frontier, context(req), fetch_candidate=store.execute
        )
    else:
        result = mutant.build_context_pack(frozen.frontier, context(req))
    assert isinstance(result, ContextPackBuildResult)
    return result


def test_f4_the_guard_passes_the_production_module() -> None:
    """The guard's other half: it must not fire on the code it is guarding.

    A guard that fires on everything kills every mutation and proves nothing. This is the
    statement that the four mutation tests below are measuring a real difference.
    """
    assert purity_violations(production_source(), context_pack_module) == ()


@pytest.mark.parametrize(
    "form", ["parameter", "import_and_global", "closure", "callback"]
)
def test_f4_the_guard_kills_an_executed_extra_row_read_in_any_of_the_four_forms(
    form: str,
) -> None:
    """F4, as the mutation it is: a defective module that *runs*, and a guard that kills it.

    Each branch writes one prohibited handle shape into a copy of the production source and
    puts one extra candidate read on the ranking path through it. Three things are then
    required, and none stands in for another:

    1. **the read executed.** The tracing store this file owns logs exactly one statement, so
       what is being killed is a defect that happens rather than a shape that exists. A
       branch whose mutation were inert would fail here even though the guard fired;
    2. **the read reached the output.** The smuggled candidate is scored, ordered, selected,
       sectioned and cited by the production algorithms the mutant left alone, and comes back
       in the mutant's own `records` -- while the production build over the same honest
       frontier returns no such record, which is the difference being measured. The mutant's
       pack still attests the honest frontier's candidate checksum, so it is refused by the
       manifest for the same reason F1-F3's widened packs are: this is that forgery, produced
       by a smuggled read instead of a widened freeze;
    3. **the guard killed it**, naming that exact prohibition, and found nothing in the file
       it guards -- asserted inside the same branch, so the comparison is between two
       measurements rather than between one measurement and a memory.

    The four-way split matters because none of the four is caught by the same clause as
    another: a callback imports nothing, a module global appears in no signature, a closure
    cell appears in neither, and only the parameter form is visible in a signature at all.
    """
    mutated, expected = mutate(form)
    assert mutated != production_source()
    store = _TracingStore()

    with load_mutant(
        mutated, f"_context_pack_mutant_{form}", mutant_namespace(form, store)
    ) as mutant:
        findings = purity_violations(mutated, mutant)
        smuggled = invoke_mutant(mutant, form, store)

    # 1. Executed, exactly once, through the handle that form smuggles.
    assert store.reads == [SMUGGLED_READ]

    # 2. And in the pack: an identity the honest build cannot produce, and one the
    #    independently held manifest the mutant still attests does not contain.
    honest, honest_freeze, req = standard_pack()
    smuggled_ids = {item.provenance.identity.record_id for item in smuggled.records}
    assert SMUGGLED_RECORD_ID in smuggled_ids
    assert SMUGGLED_RECORD_ID not in {
        item.provenance.identity.record_id for item in honest.records
    }
    assert len(smuggled.sections) == len(honest.sections) + 1
    assert (CONTEXT_PACK_CANDIDATE_PARTITION_RECORDS, SMUGGLED_RECORD_ID, "v1") not in (
        frontier_identities(honest_freeze.manifest)
    )
    with pytest.raises(
        ContractSemanticError, match="the authorized candidate set does not contain"
    ):
        validate(smuggled, req=req, manifest=honest_freeze.manifest)

    # 3. The guard names the form, and finds nothing in production.
    assert expected in findings, findings
    assert purity_violations(production_source(), context_pack_module) == ()

    if form == "import_and_global":
        # The imports *and* the global, since this branch smuggles all three and none
        # implies another.
        assert "import:sqlite3" in findings
        assert "import:omnivia_core_runtime.storage" in findings
    if form == "closure":
        # And nothing else: the handle is in the cell, so no import and no global names it.
        assert findings == ("closure:build_context_pack",)
    if form == "callback":
        # A callback also carries the default a handle could be bound into later.
        assert "default:build_context_pack.fetch_candidate" in findings


def test_f4b_neither_entry_point_captures_a_cell_or_carries_a_default() -> None:
    """The runtime half of the closure and default clauses, on the real functions.

    Falsifier: `__closure__` is `None` only if the function captured nothing at all, so
    there is no cell for a handle to be sitting in -- a fact no source-level check can
    establish.
    """
    for function in (build_context_pack, freeze_context_pack_frontier):
        assert function.__closure__ is None, function.__name__
        assert function.__defaults__ is None, function.__name__
        assert function.__kwdefaults__ is None, function.__name__


# --- E1-E4: exactness ----------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "citation_ids", "refusal"),
    [
        ("no_citation", (), "citation_ids names 0 citation"),
        ("absent_citation", ("c-9999",), "does not resolve to a citation"),
    ],
)
def test_e1_a_section_must_attribute_all_of_its_content(
    name: str, citation_ids: tuple[str, ...], refusal: str
) -> None:
    """Total attribution, in both directions a section can fail it.

    A section carrying no citation id at all is content a caller cannot attribute to
    anything; a section naming an id no citation carries is content attributed to nothing.
    Both are resealed first, so each is a document whose own content address is correct and
    whose only defect is the attribution -- otherwise the refusal could be about the stale
    checksum and the test would establish nothing about attribution.

    Falsifier: the same pack with its real citation ids is accepted throughout this file.
    """
    result, frozen, req = standard_pack()
    first, *rest = result.sections

    validate(result, req=req, manifest=frozen.manifest)
    unattributed = reseal(
        replace(result, sections=(replace(first, citation_ids=citation_ids), *rest))
    )

    assert unattributed.pack_id == artifact_digest(unattributed)
    with pytest.raises(ContractDecodeError, match=refusal):
        validate(unattributed, req=req, manifest=frozen.manifest)


def test_e2_a_citation_naming_an_identity_outside_the_frozen_manifest_is_refused(
) -> None:
    """Groundedness: a pack may cite only what its frozen frontier authorized.

    The mutation is coherent on purpose. The foreign artifact is substituted everywhere the
    contract cross-checks -- the selected `evidence` array, the citation that points at it,
    and the `reproducibility.evidence_versions` restatement -- and the result is resealed,
    so every internal rule the validator can check against the document alone still holds.
    The document is self-consistent and cites material that was never on the frontier.

    What refuses it is the independently held manifest, which is the only statement in the
    system that was made before ranking and cannot be rebuilt from the artifact.

    Falsifier: drop the manifest from the verification and this pack passes every other rule
    in the contract.
    """
    result, frozen, req = standard_pack()
    outsider = artifact("ev-outside", locator="archive://alpha.md")
    reference = ContextPackEvidenceReference(
        evidence_id=outsider.evidence_id, content_checksum=outsider.content_checksum
    )
    first_citation, *other_citations = result.citations
    assert isinstance(first_citation, ContextPackEvidenceCitation)

    ungrounded = reseal(
        replace(
            result,
            evidence=(outsider,),
            citations=(
                replace(first_citation, evidence_reference=reference),
                *other_citations,
            ),
            reproducibility=replace(
                result.reproducibility, evidence_versions=(reference,)
            ),
        )
    )

    assert ungrounded.pack_id == artifact_digest(ungrounded)
    assert ("evidence", "ev-outside") not in {
        (partition, first)
        for partition, first, _ in frontier_identities(frozen.manifest)
    }
    with pytest.raises(
        ContractSemanticError, match="the authorized candidate set does not contain"
    ):
        validate(ungrounded, req=req, manifest=frozen.manifest)


#: Where each of the nineteen `ContextPackReproducibility` members comes from, written out
#: rather than implied. The point of the table is that no row's source is the field itself:
#: a fixture literal checked against a fixture literal is a tautology dressed as provenance,
#: and it is exactly what a reproducibility record must not be verified by.
#:
#: Four kinds of source, and the distinction is the substance of E3. **Production constant**
#: means this module implements the algorithm and publishes its identity, so the test reads
#: the constant out of `context_pack.py`. **Contract constant/helper** means the frozen
#: contract owns the value or the reduction. **Observed** means the value is read back off
#: the build's own inputs or outputs -- the request, the frozen frontier, the selected
#: items. **Handler-supplied** is the one honest gap, and it has exactly one member.
REPRODUCIBILITY_PROVENANCE: Mapping[str, str] = {
    "pack_format_version": "contract constant CONTEXT_PACK_FORMAT_VERSION",
    "builder_version": "production constant CONTEXT_PACK_BUILDER_VERSION",
    "normalized_request": (
        "observed: the request DTO's own members, this file's independent case-folding of "
        "its query, the contract's view constant and the production normalization constant"
    ),
    "authorization_context": (
        "observed: the build context's authorization values, plus the freeze's manifest put "
        "back through the contract's own compute_authorized_candidate_set_checksum"
    ),
    "evidence_versions": "observed: the identities actually returned in result.evidence",
    "record_versions": "observed: the identities actually returned in result.records",
    "freshness": (
        "observed: the frozen frontier's supplied projection material, and the supplied "
        "canonical instant as its as_of"
    ),
    "retrieval_version": (
        "handler-supplied: nothing in this module retrieves, so this is a producer "
        "statement recorded verbatim. Its real provenance is the later Lane D handler "
        "integration seam, and this file establishes only that it is stored unchanged"
    ),
    "ranking_version": "production constant CONTEXT_PACK_RANKING_VERSION",
    "reranking_version": "production constant CONTEXT_PACK_RERANKER_DISABLED",
    "selection_version": "production constant CONTEXT_PACK_SELECTION_VERSION",
    "tokenizer_id": "production constant CONTEXT_PACK_TOKENIZER_ID",
    "tokenizer_version": "production constant CONTEXT_PACK_TOKENIZER_VERSION",
    "summarizer_version": "contract constant CONTEXT_PACK_SUMMARIZER_DISABLED",
    "model_versions": "observed: empty, because the builder refuses any other value",
    "canonical_resolution_time": "observed: the build context's supplied instant",
    "generated_at": "observed: the same supplied instant, by construction",
    "artifact_canonicalization": (
        "contract constant CONTEXT_PACK_ARTIFACT_CANONICALIZATION"
    ),
    "artifact_checksum": (
        "contract helper: recomputed by compute_context_pack_artifact_digest over this "
        "result's own reduction"
    ),
}


def test_e3_eighteen_of_nineteen_reproducibility_fields_have_a_non_circular_source(
) -> None:
    """Eighteen members checked against something that is not themselves -- and one not.

    The table above is asserted to cover the dataclass exactly, so a member added to the
    contract cannot slip through unattributed, and every row below is a comparison against a
    production constant, a contract constant, a contract helper, or a value read off the
    build's own inputs and outputs.

    **The nineteenth row does not close, and this test does not pretend it does.**
    `retrieval_version` is checked against `HANDLER_SUPPLIED_RETRIEVAL_VERSION`, which is a
    literal invented in this file's own fixture block -- so the assertion establishes that
    the builder stores a supplied label unchanged and *nothing whatever* about where a real
    one would come from. That is asserted rather than only stated: the fixture literal is
    required to appear nowhere in the production source, which is what makes "this value has
    no source in this slice" a fact about the code rather than a caveat in a docstring.

    E3 as the packet states it is closed when the later handler supplies that field from a
    real, non-circular retrieval -- an integration test with a database and a retrieval in
    it. Nothing in a pure two-file slice can substitute for that, and inventing a plausible
    version string here would be the exact fabricated-but-verifying claim the builder's own
    version pinning exists to refuse.
    """
    result, frozen, req = standard_pack()
    repro = result.reproducibility

    validate(result, req=req, manifest=frozen.manifest)

    assert set(REPRODUCIBILITY_PROVENANCE) == {
        field.name for field in dataclasses.fields(ContextPackReproducibility)
    }
    assert len(REPRODUCIBILITY_PROVENANCE) == 19

    # Contract constants and the contract's own helpers.
    assert repro.pack_format_version == CONTEXT_PACK_FORMAT_VERSION
    assert repro.artifact_canonicalization == CONTEXT_PACK_ARTIFACT_CANONICALIZATION
    assert repro.summarizer_version == CONTEXT_PACK_SUMMARIZER_DISABLED
    assert repro.artifact_checksum == artifact_digest(result)

    # Algorithms implemented in `context_pack.py`, under that module's own constants.
    assert repro.builder_version == CONTEXT_PACK_BUILDER_VERSION
    assert repro.ranking_version == CONTEXT_PACK_RANKING_VERSION
    assert repro.reranking_version == CONTEXT_PACK_RERANKER_DISABLED
    assert repro.selection_version == CONTEXT_PACK_SELECTION_VERSION
    assert repro.tokenizer_id == CONTEXT_PACK_TOKENIZER_ID
    assert repro.tokenizer_version == CONTEXT_PACK_TOKENIZER_VERSION
    assert repro.normalized_request.normalization_version == (
        CONTEXT_PACK_NORMALIZATION_VERSION
    )

    # The normalized request: the request's own members, and this file's own folding.
    normalized = repro.normalized_request
    assert normalized.normalized_query == req.query.casefold()
    assert normalized.mode == req.mode
    assert normalized.token_budget == req.token_budget
    assert normalized.domain_scope == req.domain_scope
    assert normalized.record_type == req.record_type
    assert normalized.view == CONTEXT_PACK_NORMALIZED_REQUEST_VIEW

    # The authorization context: the supplied values, and the freeze's own manifest.
    authorization = repro.authorization_context
    assert authorization.workspace_id == WORKSPACE_ID
    assert authorization.authority == AUTHORITY
    assert authorization.scopes == SCOPES
    assert authorization.purpose == PURPOSE
    assert dict(authorization.policy_versions) == POLICY_VERSIONS
    assert authorization.pre_ranking_authorization_enforced is True
    assert authorization.authorized_candidate_set_checksum == (
        compute_authorized_candidate_set_checksum(frozen.manifest)
    )

    # The selected identities, read back off what the pack actually returned.
    assert repro.evidence_versions == tuple(
        ContextPackEvidenceReference(
            evidence_id=item.evidence_id, content_checksum=item.content_checksum
        )
        for item in result.evidence
    )
    assert repro.record_versions == tuple(
        RecordVersionReference(
            record_id=item.provenance.identity.record_id,
            version=item.provenance.identity.version,
        )
        for item in result.records
    )

    # The freshness, from the frozen frontier's supplied material and the supplied instant.
    assert repro.freshness == ProjectionFreshness(
        as_of=RESOLUTION_TIME,
        projection_versions=dict(frozen.frontier.projection_versions),
        projection_watermarks=dict(frozen.frontier.projection_watermarks),
        stale=frozen.frontier.projection_stale,
    )

    # The instants, and the empty model set.
    assert repro.canonical_resolution_time == RESOLUTION_TIME
    assert repro.generated_at == RESOLUTION_TIME
    assert dict(repro.model_versions) == {}

    # The one handler-supplied producer statement, stored verbatim and nothing more -- and
    # the honesty of that "nothing more", asserted: the value is a literal this file
    # invented, it occurs nowhere in the production source, and there is therefore no
    # non-circular source for it anywhere in this slice to check it against.
    assert repro.retrieval_version == HANDLER_SUPPLIED_RETRIEVAL_VERSION
    assert HANDLER_SUPPLIED_RETRIEVAL_VERSION not in production_source()
    assert REPRODUCIBILITY_PROVENANCE["retrieval_version"].startswith("handler-supplied")


def test_supplemental_token_counts_cover_the_exact_emitted_content_and_sum_to_tokens_used(
) -> None:
    """Real accounting over the real string, recounted independently.

    The count is recomputed here from a restated pattern rather than the module's own
    compiled one: importing the production tokenizer would make this test agree with a
    tokenizer that had stopped counting.

    Falsifier: a count taken over a pre-truncation string, over the matched surface rather
    than the emitted content, or summed across anything but the sections would break one
    of the two equalities below.
    """
    result, frozen, req = standard_pack()

    validate(result, req=req, manifest=frozen.manifest)
    for section in result.sections:
        assert section.token_count == len(_TOKEN_RE.findall(section.content))
        assert section.token_count > 0
    assert result.budget.tokens_used == sum(
        section.token_count for section in result.sections
    )
    assert result.budget.tokens_used <= result.budget.token_budget


def test_supplemental_a_candidate_that_does_not_fit_is_skipped_and_accounted_for() -> None:
    """Greedy under the budget, and a skip rather than a stop.

    The evidence candidate ranks first and costs more than the whole budget; the governed
    record ranks second and fits. A builder that ended selection at the first miss would
    return an empty pack and withhold content the caller was entitled to; one that
    truncated the artifact's content would emit a section whose token count and citation
    both describe text nobody received.

    Falsifier: both halves are asserted -- the omission exists *and* the later, smaller
    candidate was still selected.
    """
    frozen = standard_freeze()
    req = request(token_budget=12)
    result = build_context_pack(frozen.frontier, context(req))

    validate(result, req=req, manifest=frozen.manifest)
    assert OMISSION_TOKEN_BUDGET in {omission.code for omission in result.omissions}
    assert f"{CONTEXT_PACK_CANDIDATE_PARTITION_EVIDENCE}/ev-1@" in "".join(
        omission.path or ""
        for omission in result.omissions
        if omission.code == OMISSION_TOKEN_BUDGET
    )
    assert [item.provenance.identity.record_id for item in result.records] == ["rec-1"]
    assert result.evidence == ()
    assert result.budget.tokens_used == sum(s.token_count for s in result.sections)


def test_supplemental_content_too_large_for_one_section_is_omitted_whole() -> None:
    """The other ceiling a candidate can fail, and the same refusal to truncate.

    Nothing upstream bounds a governed record's opaque JSON, and
    `ContextPackSection.content` has a `maxLength` the contract enforces. A builder that
    emitted the record anyway would produce a pack its own contract refuses; one that cut
    the content to fit would present part of a record as the record.

    Falsifier: the oversized record is accounted for by its own code and the pack still
    passes the contract's judge, which a truncating builder could not manage.
    """
    oversized = {"body": QUERY + " " + "x" * CONTEXT_PACK_MAX_SECTION_CONTENT_LENGTH}
    frozen = freeze(
        records=(
            record_member("rec-big", content=oversized),
            record_member("rec-1"),
        )
    )
    req = request()
    result = build_context_pack(frozen.frontier, context(req))

    validate(result, req=req, manifest=frozen.manifest)
    assert {omission.code for omission in result.omissions} == {
        OMISSION_SECTION_TOO_LARGE
    }
    assert f"{CONTEXT_PACK_CANDIDATE_PARTITION_RECORDS}/rec-big@v1" in paths(result)
    assert [item.provenance.identity.record_id for item in result.records] == ["rec-1"]


def wide_freeze(count: int) -> ContextPackFreeze:
    return freeze(
        records=tuple(
            record_member(f"rec-{index:04d}", content={"body": QUERY, "n": index})
            for index in range(count)
        )
    )


def test_supplemental_a_frontier_of_256_is_accepted_and_accounted_for_in_full() -> None:
    """The ceiling is the number that makes exact accounting possible, at the boundary.

    256 is `omissions`'s `maxItems` and `sections`'s, and at most one of each is emitted
    per frozen candidate -- so a frontier of exactly 256 is the widest one a pack can
    account for in full, and it is accepted rather than narrowed.
    """
    frozen = wide_freeze(CONTEXT_PACK_MAX_FRONTIER_CANDIDATES)
    req = request(token_budget=100_000)
    result = build_context_pack(frozen.frontier, context(req))

    validate(result, req=req, manifest=frozen.manifest)
    assert len(result.sections) == CONTEXT_PACK_MAX_FRONTIER_CANDIDATES
    assert len(result.records) == CONTEXT_PACK_MAX_FRONTIER_CANDIDATES
    assert result.omissions == ()


def test_supplemental_a_frontier_of_257_is_refused_rather_than_truncated() -> None:
    """The refusal, and the two things it deliberately is not.

    It is not a truncation -- a pack stating it ranked over material it never saw -- and it
    is not a silent surplus left unaccounted. It is a deterministic builder exception the
    later handler maps to `size_limit_exceeded`, carrying the ceiling and the observed size
    and nothing about any candidate.
    """
    members = tuple(
        record_member(f"rec-{index:04d}")
        for index in range(CONTEXT_PACK_MAX_FRONTIER_CANDIDATES + 1)
    )

    with pytest.raises(ContextPackFrontierTooLarge) as raised:
        freeze(records=members)

    message = str(raised.value)
    assert "257" in message
    assert str(CONTEXT_PACK_MAX_FRONTIER_CANDIDATES) in message
    assert "rec-" not in message


def test_supplemental_the_ceiling_counts_every_partition_the_frontier_holds() -> None:
    """History counts toward the ceiling, because history is accounted for.

    Falsifier: counting only the selectable partitions would admit a frontier of 256
    current records plus any amount of history, and the omissions accounting for that
    history would exceed the contract's `maxItems`.
    """
    with pytest.raises(ContextPackFrontierTooLarge):
        freeze(
            evidence=(evidence_member("ev-1", locator="archive://alpha.md"),),
            records=tuple(
                record_member(f"rec-{index:04d}")
                for index in range(CONTEXT_PACK_MAX_FRONTIER_CANDIDATES - 1)
            ),
            history=(record_member("rec-old", version="v0", currentness="superseded"),),
        )


@pytest.mark.parametrize("budget", [4096, 12, 1])
def test_e4_the_frontier_the_selection_and_the_omissions_balance_by_identity(
    budget: int,
) -> None:
    """Exact accounting, at identity level rather than by count.

    `frontier - selected - accounted` is empty, and so is every other difference: the
    selected set and the accounted set are disjoint, and their union is the whole frontier.
    Counting alone would let one candidate be omitted twice while another vanished.

    Run under three budgets, because the partition of the frontier into selected and
    omitted changes with the budget while the balance must not.
    """
    frozen = standard_freeze()
    req = request(token_budget=budget)
    result = build_context_pack(frozen.frontier, context(req))

    validate(result, req=req, manifest=frozen.manifest)
    frontier_set = frontier_identities(frozen.manifest)
    selected = selected_identities(result)
    accounted = accounted_identities(result)

    assert selected <= frontier_set
    assert accounted <= frontier_set
    assert selected & accounted == set()
    assert frontier_set - selected - accounted == set()
    assert len(result.omissions) == len(accounted)


def test_supplemental_every_selected_item_is_cited_and_every_citation_is_sectioned() -> None:
    """Coverage in both directions, which the contract checks and this states.

    Falsifier: an uncited selection is material the caller cannot attribute; an unsectioned
    citation points at nothing the caller was given. Both are checked by the validator, so
    what this adds is the exact one-to-one shape the builder actually emits.
    """
    result, frozen, req = standard_pack()

    validate(result, req=req, manifest=frozen.manifest)
    citation_ids = {citation.citation_id for citation in result.citations}
    used = {
        citation_id
        for section in result.sections
        for citation_id in section.citation_ids
    }
    assert used == citation_ids
    assert len(result.citations) == len(result.sections)
    assert len(result.citations) == len(result.evidence) + len(result.records)


# --- the validator binding: every supplied version, bound exactly --------------


@pytest.mark.parametrize(
    ("field", "wrong"),
    [
        ("normalized_query", "beta"),
        ("normalization_version", "norm-9"),
        ("builder_version", "builder-9"),
        ("retrieval_version", "retrieval-9"),
        ("ranking_version", "ranking-9"),
        ("reranking_version", "reranking-9"),
        ("selection_version", "selection-9"),
        ("tokenizer_id", "tok-9"),
        ("tokenizer_version", "tok-v9"),
        ("summarizer_version", "summarizer-9"),
        ("model_versions", {"planner": "model-9"}),
    ],
)
def test_every_supplied_version_field_is_bound_exactly_at_validation(
    field: str, wrong: Any
) -> None:
    """All eleven producer assertions, each shown to be bound rather than shape-checked.

    A reproducibility record's versions are the producer's statements about the producer:
    nothing inside the artifact can contradict them, so an unsupplied expectation leaves
    only a shape check. This walks every one of them and requires the mismatch to raise --
    including `model_versions`, where `{}` is a caller expecting a build that used no
    model and is a different statement from supplying no expectation at all.
    """
    result, frozen, req = standard_pack()

    validate(result, req=req, manifest=frozen.manifest)
    with pytest.raises(ContractSemanticError):
        validate(result, req=req, manifest=frozen.manifest, **{field: wrong})


def test_the_authority_workspace_purpose_and_policy_are_bound_too() -> None:
    """The mandatory expectations, each shown to be a real comparison.

    Falsifier: these are not optional bindings -- a validator handed the wrong one of any
    of them could not tell a recorded authority context from an invented one.
    """
    result, frozen, req = standard_pack()
    other_authority = replace(AUTHORITY, roles=("reader",))

    with pytest.raises(ContractSemanticError):
        validate(result, req=req, manifest=frozen.manifest, authority=other_authority)
    with pytest.raises(ContractSemanticError):
        validate(result, req=req, manifest=frozen.manifest, scopes=("evidence.read",))
    with pytest.raises(ContractSemanticError):
        validate(
            result,
            req=req,
            manifest=frozen.manifest,
            policy_versions={"evidence_acl": "policy-9"},
        )
