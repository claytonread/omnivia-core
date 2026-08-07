"""V06-3 Lane A: the filter chain, the frozen frontier and the ranker's isolation.

Packet §8.1 F1, F3 and F4, §20.3's five-item evidence-label ACL suite, §20.12's three
collective proofs of ranker isolation, and one per-filter refusal for each filter in the
chain. `test_evidence_search_vertical.py` carries F2 and the end-to-end path; this file
is the unit-level half, over values rather than over a database, because every property
here is a property of the chain rather than of storage.

**Why the obvious test is worthless, restated because it governs this whole file.** A
test that searches as an unauthorized principal and asserts an empty result passes
identically whether filtering happened before ranking or after -- and passes if ranking
saw every document in the workspace and the last step dropped the ones it should not
have shown. Every positive test below therefore states its falsifier, and the mutations
that make the falsifiers real are exercised as tests rather than argued in prose.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from typing import Any

import pytest
from omnivia_core_runtime.service.main import LOCAL_PRINCIPAL
from omnivia_core_runtime.storage import retrieval as retrieval_module
from omnivia_core_runtime.storage.retrieval import (
    CONFIGURED_LOCAL_OWNER,
    FRONTIER_FILTERS,
    AuthorizedFrontier,
    EvidenceCandidate,
    EvidenceLabelGrant,
    authorized_frontier,
    candidate_set_manifest,
    local_owner_label_grant,
    normalize_query,
    rank_candidates,
)

from omnivia_core.contracts.v1 import (
    CONTEXT_PACK_AUTHORIZED_CANDIDATE_SET_FORMAT,
    CONTEXT_PACK_CANDIDATE_PARTITION_EVIDENCE,
    ContextPackAuthorizedCandidateSetManifest,
    ContextPackAuthorizedEvidenceCandidate,
    EvidenceArtifact,
    RecordTemporalMetadata,
    SourceReference,
    compute_authorized_candidate_set_checksum,
)

WORKSPACE_ID = "ws-retrieval-0001"
OTHER_WORKSPACE_ID = "ws-retrieval-0002"
BASE_US = 1_700_000_000_000_000
RESOLUTION_US = BASE_US + 1_000_000

#: A grant that holds every label, which is what the configured local owner gets.
ALL_LABELS = EvidenceLabelGrant(
    principal_id=LOCAL_PRINCIPAL,
    workspace_id=WORKSPACE_ID,
    all_labels=True,
    labels=frozenset(),
)

#: A grant that holds nothing, which is what every other principal gets. Not an absent
#: check: it is evaluated on every candidate exactly as the all-label grant is.
EMPTY_GRANT = EvidenceLabelGrant(
    principal_id="someone-else",
    workspace_id=WORKSPACE_ID,
    all_labels=False,
    labels=frozenset(),
)


def candidate(
    evidence_id: str,
    *,
    workspace_id: str = WORKSPACE_ID,
    sensitivity: str = "internal",
    permission_labels: tuple[str, ...] = (),
    tombstoned: bool = False,
    recorded_at_us: int = BASE_US,
    search_text: str = "filesystem.archive doc archive://doc.md",
) -> EvidenceCandidate:
    """One candidate, with a real contract DTO behind it."""
    checksum = "sha256:" + evidence_id.encode().hex().ljust(64, "0")[:64]
    return EvidenceCandidate(
        evidence_id=evidence_id,
        workspace_id=workspace_id,
        content_checksum=checksum,
        sensitivity=sensitivity,
        permission_labels=permission_labels,
        tombstoned=tombstoned,
        recorded_at_us=recorded_at_us,
        search_text=search_text,
        artifact=EvidenceArtifact(
            evidence_id=evidence_id,
            workspace_id=workspace_id,
            source=SourceReference(kind="filesystem.archive", source_id=evidence_id),
            temporal=RecordTemporalMetadata(
                ingested_at="2023-11-14T22:13:20.000Z",
                recorded_at="2023-11-14T22:13:20.000Z",
            ),
            content_checksum=checksum,
            media_type="text/markdown",
            metadata={},
            permission_labels=permission_labels,
            sensitivity=sensitivity,
            tombstoned=tombstoned,
            parser_status="parsed",
            ingestion_status="ingested",
            provenance_history=(),
        ),
    )


def frontier(
    *candidates: EvidenceCandidate,
    grant: EvidenceLabelGrant = ALL_LABELS,
    **kwargs: Any,
) -> AuthorizedFrontier:
    return authorized_frontier(
        candidates,
        workspace_id=WORKSPACE_ID,
        grant=grant,
        resolution_time_us=RESOLUTION_US,
        **kwargs,
    )


def ids(candidates: tuple[EvidenceCandidate, ...]) -> tuple[str, ...]:
    return tuple(item.evidence_id for item in candidates)


# --- the chain, filter by filter ----------------------------------------------
#
# One test per filter, each showing the same candidate admitted and excluded across the
# one fact that filter reads. A filter with no exclusion case is a filter nothing proves
# is running.


def test_the_chain_states_the_filters_it_applied_and_the_order() -> None:
    """The frontier names what narrowed it, so "which filters ran" is not inferred."""
    assert FRONTIER_FILTERS == (
        "workspace",
        "evidence_label_acl",
        "sensitivity",
        "tombstone",
        "temporal",
    )
    assert frontier(candidate("evd-1")).filters_applied == FRONTIER_FILTERS


def test_workspace_domain_separation_excludes_another_workspaces_artifact() -> None:
    """Falsifier: drop the workspace comparison and `evd-2` joins the frontier."""
    built = frontier(
        candidate("evd-1"), candidate("evd-2", workspace_id=OTHER_WORKSPACE_ID)
    )

    assert ids(built.candidates) == ("evd-1",)
    assert ids(built.excluded) == ("evd-2",)


def test_the_acl_filter_excludes_a_labelled_artifact_from_an_empty_grant() -> None:
    """Falsifier: return `True` unconditionally from `permits` and `evd-2` appears."""
    built = frontier(
        candidate("evd-1"),
        candidate("evd-2", permission_labels=("restricted",)),
        grant=EMPTY_GRANT,
    )

    assert ids(built.candidates) == ("evd-1",)
    assert ids(built.excluded) == ("evd-2",)


def test_the_sensitivity_filter_excludes_every_other_sensitivity() -> None:
    """Falsifier: ignore the selector and both artifacts reach the frontier."""
    built = frontier(
        candidate("evd-1", sensitivity="internal"),
        candidate("evd-2", sensitivity="confidential"),
        sensitivity="internal",
    )

    assert ids(built.candidates) == ("evd-1",)


def test_the_tombstone_filter_excludes_unless_the_request_asks() -> None:
    """Falsifier: default `include_tombstoned` to true and `evd-2` is never absent."""
    hidden = frontier(candidate("evd-1"), candidate("evd-2", tombstoned=True))
    shown = frontier(
        candidate("evd-1"),
        candidate("evd-2", tombstoned=True),
        include_tombstoned=True,
    )

    assert ids(hidden.candidates) == ("evd-1",)
    assert ids(shown.candidates) == ("evd-1", "evd-2")


def test_the_temporal_filter_makes_a_later_record_absent_not_merely_unranked() -> None:
    """§12.3 test 12. Absent from the frontier, and therefore from the digest.

    Falsifier: move the instant comparison into `rank_candidates` and the two frontiers
    below have the same checksum -- which is the whole difference between a frontier
    filter and an ordering hint.
    """
    inside = candidate("evd-1", recorded_at_us=RESOLUTION_US)
    after = candidate("evd-2", recorded_at_us=RESOLUTION_US + 1)
    built = frontier(inside, after)

    assert ids(built.candidates) == ("evd-1",)
    assert built.checksum != frontier(inside, candidate("evd-2")).checksum


def test_the_filters_compose_and_none_substitutes_for_another() -> None:
    """§12.3 test 10: `include_tombstoned` cannot surface what the ACL filter excluded.

    Falsifier: make the tombstone branch an `elif` on the ACL branch, or evaluate the
    filters as an `or`, and the artifact below is surfaced by asking for tombstoned
    material -- which is the shape of a real privilege escalation, not a style point.
    """
    hidden = candidate("evd-2", permission_labels=("restricted",), tombstoned=True)
    built = frontier(candidate("evd-1"), hidden, grant=EMPTY_GRANT, include_tombstoned=True)

    assert ids(built.candidates) == ("evd-1",)
    assert ids(built.excluded) == ("evd-2",)


# --- §20.3: the evidence-label ACL suite, five items --------------------------


def test_acl_1_the_configured_local_owner_holds_every_label() -> None:
    """Falsifier: hand the local owner the empty grant and the labelled item vanishes."""
    grant = local_owner_label_grant(
        principal_id=CONFIGURED_LOCAL_OWNER,
        workspace_id=WORKSPACE_ID,
        granted_workspace=WORKSPACE_ID,
    )

    assert grant.all_labels is True
    assert grant.permits(("restricted", "legal-hold")) is True
    assert ids(
        frontier(
            candidate("evd-1", permission_labels=("restricted",)), grant=grant
        ).candidates
    ) == ("evd-1",)


def test_acl_2_a_synthetic_restricted_label_is_denied_under_a_partial_grant() -> None:
    """A label the grant does not hold denies, and holding a subset is not holding it.

    Falsifier: test the grant's labels for *intersection* rather than containment and
    `evd-2` is admitted on the strength of the one label it shares.
    """
    partial = EvidenceLabelGrant(
        principal_id="analyst",
        workspace_id=WORKSPACE_ID,
        all_labels=False,
        labels=frozenset({"general"}),
    )

    assert partial.permits(("general",)) is True
    assert partial.permits(("general", "restricted")) is False
    built = frontier(
        candidate("evd-1", permission_labels=("general",)),
        candidate("evd-2", permission_labels=("general", "restricted")),
        grant=partial,
    )

    assert ids(built.candidates) == ("evd-1",)


def test_acl_3_any_other_principal_defaults_to_no_grant_at_all() -> None:
    """Deny by default, on both halves of "the exact configured local owner".

    A principal that is not the configured owner gets nothing, and so does the
    configured owner asking about a workspace this endpoint was not launched to own --
    the decision grants all labels *for its granted workspace*, and anything wider would
    be the "global local bypass" §20.3 forbids by name.

    Falsifier: drop either conjunct in `local_owner_label_grant` and one of the two
    assertions below flips to an all-label grant.
    """
    stranger = local_owner_label_grant(
        principal_id="someone-else",
        workspace_id=WORKSPACE_ID,
        granted_workspace=WORKSPACE_ID,
    )
    wrong_workspace = local_owner_label_grant(
        principal_id=CONFIGURED_LOCAL_OWNER,
        workspace_id=OTHER_WORKSPACE_ID,
        granted_workspace=WORKSPACE_ID,
    )

    assert stranger.all_labels is False
    assert stranger.labels == frozenset()
    assert wrong_workspace.all_labels is False


def test_acl_4_an_empty_grant_denies_every_labelled_artifact() -> None:
    """The empty grant is a real grant that refuses, not an absent check.

    Unlabelled material is still admitted: there is no restriction on it for a grant to
    fail to hold, and treating "no labels" as "deny" would make an empty workspace
    unreadable rather than unauthorized.

    Falsifier: implement the empty grant as a skipped ACL stage and `evd-2` appears.
    """
    built = frontier(
        candidate("evd-1"),
        candidate("evd-2", permission_labels=("restricted",)),
        grant=EMPTY_GRANT,
    )

    assert ids(built.candidates) == ("evd-1",)
    assert EMPTY_GRANT.permits(()) is True
    assert EMPTY_GRANT.permits(("restricted",)) is False


def test_acl_5_changing_the_grant_changes_the_candidate_set_deterministically() -> None:
    """The digest moves with the grant, and only with the grant.

    Same candidates, same request, two grants: the frontier checksums differ, and each
    is stable across repeated construction. That is what "deterministic candidate-set
    changes when grants change" means operationally -- an assertion about result length
    would pass with the set computed at random.

    Falsifier: make the ACL stage run after ranking and both checksums become the wide
    one, because the frontier both were computed from was never narrowed.
    """
    items = (candidate("evd-1"), candidate("evd-2", permission_labels=("restricted",)))
    wide = authorized_frontier(
        items, workspace_id=WORKSPACE_ID, grant=ALL_LABELS, resolution_time_us=RESOLUTION_US
    )
    narrow = authorized_frontier(
        items, workspace_id=WORKSPACE_ID, grant=EMPTY_GRANT, resolution_time_us=RESOLUTION_US
    )

    assert wide.checksum != narrow.checksum
    assert wide.checksum == authorized_frontier(
        items, workspace_id=WORKSPACE_ID, grant=ALL_LABELS, resolution_time_us=RESOLUTION_US
    ).checksum
    assert ids(wide.candidates) == ("evd-1", "evd-2")
    assert ids(narrow.candidates) == ("evd-1",)


def test_acl_6_a_filtered_artifact_reaches_neither_ranking_nor_assembly() -> None:
    """The fifth suite item, and the one that has to be about *reachability*.

    Not "it is absent from the result" -- that is true of anything ranking scored last.
    The excluded artifact is absent from `frontier.candidates`, which is the ranker's
    entire input, so there is no execution in which ranking observes it. The ranker is
    handed the frontier and, as the isolation tests below establish, can reach nothing
    else.

    Falsifier: pass the unfiltered candidate tuple to `rank_candidates` instead of the
    frozen frontier and the restricted artifact is ranked and returned.
    """
    restricted = candidate("evd-2", permission_labels=("restricted",))
    built = frontier(candidate("evd-1"), restricted, grant=EMPTY_GRANT)

    assert "evd-2" not in ids(built.candidates)
    assert "evd-2" not in ids(rank_candidates(built, "doc", limit=10))
    assert all(
        item.evidence_id != "evd-2"
        for item in candidate_set_manifest(WORKSPACE_ID, built.candidates).candidates
    )


# --- §8.1 F1 and F3: the digest is the check, the attestation is not ----------


def test_f1_moving_a_filter_after_the_freeze_changes_the_checksum() -> None:
    """F1: the same result content, a wider frontier, a different digest.

    The mutation is performed here rather than described: `unfiltered` is what the
    frontier would be if the ACL stage ran after ranking instead of before it, and the
    two checksums differ even though the ranked page below is identical.
    """
    items = (candidate("evd-1"), candidate("evd-2", permission_labels=("restricted",)))
    correct = authorized_frontier(
        items, workspace_id=WORKSPACE_ID, grant=EMPTY_GRANT, resolution_time_us=RESOLUTION_US
    )
    unfiltered = authorized_frontier(
        items, workspace_id=WORKSPACE_ID, grant=ALL_LABELS, resolution_time_us=RESOLUTION_US
    )

    # The post-ranking filter a defective build would apply, restoring the same page.
    ranked_then_filtered = tuple(
        item for item in rank_candidates(unfiltered, "doc", limit=10)
        if EMPTY_GRANT.permits(item.permission_labels)
    )

    assert ids(ranked_then_filtered) == ids(rank_candidates(correct, "doc", limit=10))
    assert unfiltered.checksum != correct.checksum


def test_f3_an_attestation_boolean_cannot_stand_in_for_the_digest() -> None:
    """F3: nothing on the frontier is a self-asserted claim that filtering happened.

    The frontier carries no `pre_ranking_authorization_enforced`-shaped field, so there
    is no boolean a build could hardcode while filtering after ranking. Had one existed,
    F1 above would still fire -- which is the point of the row: a test relying on such a
    boolean must be shown to survive F3, and therefore is not evidence for §7.2.
    """
    fields = set(AuthorizedFrontier.__dataclass_fields__)

    assert fields == {
        "workspace_id",
        "candidates",
        "checksum",
        "filters_applied",
        "excluded",
    }
    assert not any(isinstance(getattr(frontier(candidate("evd-1")), name), bool)
                   for name in fields)


def test_the_manifest_is_produced_at_the_freeze_not_read_back_from_a_result() -> None:
    """§8.1's binding clause: an expected manifest derived from the artifact under test
    asserts equality with itself and is not evidence.

    So the expected value here is built independently -- by this test, from what it
    seeded -- and compared with the frontier's own. A fixture written by reading a
    previously produced frontier would pass with the semantics gutted, which is the
    fixture tautology this programme has already been bitten by once.
    """
    one, two = candidate("evd-1"), candidate("evd-2")
    built = frontier(one, two)

    expected = ContextPackAuthorizedCandidateSetManifest(
        format=CONTEXT_PACK_AUTHORIZED_CANDIDATE_SET_FORMAT,
        workspace_id=WORKSPACE_ID,
        candidates=(
            ContextPackAuthorizedEvidenceCandidate(
                partition=CONTEXT_PACK_CANDIDATE_PARTITION_EVIDENCE,
                evidence_id="evd-1",
                content_checksum=one.content_checksum,
            ),
            ContextPackAuthorizedEvidenceCandidate(
                partition=CONTEXT_PACK_CANDIDATE_PARTITION_EVIDENCE,
                evidence_id="evd-2",
                content_checksum=two.content_checksum,
            ),
        ),
    )

    assert built.checksum == compute_authorized_candidate_set_checksum(expected)


def test_the_digest_ignores_everything_ranking_could_change() -> None:
    """Two frontiers over the same identities agree however the artifacts differ.

    The contract's preimage is `(partition, evidence_id, content_checksum)`, so recency,
    sensitivity and ordering are all outside it. Without this the digest would move on
    input a later step legitimately changes, and F2 would produce false positives rather
    than catching real frontier widening.
    """
    early = frontier(candidate("evd-1", recorded_at_us=BASE_US))
    late = frontier(candidate("evd-1", recorded_at_us=BASE_US + 99))

    assert early.checksum == late.checksum


# --- §20.12: ranker isolation, all three proofs, none substituting for another -


def test_isolation_1_the_ranker_takes_no_repository_or_storage_parameter() -> None:
    """Proof one of three. Necessary and, the owner ruled, nowhere near sufficient."""
    signature = inspect.signature(rank_candidates)

    assert list(signature.parameters) == ["frontier", "query", "limit"]
    assert signature.parameters["frontier"].annotation == "AuthorizedFrontier"
    assert signature.parameters["query"].annotation == "str"
    assert signature.parameters["limit"].annotation == "int"


def test_isolation_2_the_rankers_module_imports_no_store_of_any_kind() -> None:
    """Proof two: the import boundary, which is what a signature cannot show.

    A signature says nothing about a module-level `_STORE` or an imported `sqlite3`.
    This reads the module's own import statements -- not its runtime namespace, which a
    lazy import inside a function would evade -- and refuses every root that could
    reach a row.
    """
    source = Path(inspect.getsourcefile(retrieval_module) or "").read_text()
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)

    for forbidden in (
        "sqlite3",
        "omnivia_core_runtime.storage.connection",
        "omnivia_core_runtime.storage.repository",
        "omnivia_core_runtime.storage.migrations",
        "omnivia_core_runtime.ownership.fencing",
    ):
        assert forbidden not in imported

    # And nothing under this package at all beyond the frozen contract, so a successor
    # cannot reach a store through a module this one is allowed to name.
    assert not any(
        name.startswith("omnivia_core_runtime.") for name in imported
    ), sorted(imported)


def test_isolation_3_the_module_holds_no_store_in_a_global_or_a_default() -> None:
    """Proof three's static half: nothing at module scope is a live handle.

    Every module-level binding is a constant, a type or a function. A closure-held store
    would have to be captured by something at this level, and there is nothing here that
    could have captured one.
    """
    for name, value in vars(retrieval_module).items():
        if name.startswith("__"):
            continue
        assert not hasattr(value, "execute"), name
        assert not hasattr(value, "cursor"), name

    # No parameter carries a callable default either -- that is where a store would be
    # bound if it could not be a global. `rank_candidates` has no defaults at all.
    for parameter in inspect.signature(rank_candidates).parameters.values():
        assert parameter.default is inspect.Parameter.empty


@pytest.mark.parametrize(
    "form",
    ["parameter", "module_global", "closure", "callback"],
)
def test_f4_a_ranker_given_a_live_store_in_any_of_its_four_forms_is_killed(
    form: str,
) -> None:
    """F4, demonstrated as the mutation it is, in all four forms §20.12 names.

    Each branch below builds the defective ranker the prohibition describes, has it read
    one extra row, and shows the result carrying an artifact that is **not** a member of
    the frozen frontier. That is the kill: the real `rank_candidates` cannot be written
    this way because its module imports nothing that could supply the store, and this
    test is what makes "cannot" mean something a reviewer can check rather than something
    the packet asserts.
    """
    frozen = frontier(candidate("evd-1"))
    smuggled = candidate("evd-99", permission_labels=("restricted",))

    class Store:
        """Stands in for a repository handle: it can produce an unauthorized row."""

        def extra(self) -> EvidenceCandidate:
            return smuggled

    store = Store()

    if form == "parameter":

        def defective(f: AuthorizedFrontier, q: str, *, store: Store) -> tuple[EvidenceCandidate, ...]:
            return (*f.candidates, store.extra())

        leaked = defective(frozen, "doc", store=store)
    elif form == "module_global":
        module_store = store

        def defective_global(f: AuthorizedFrontier, q: str) -> tuple[EvidenceCandidate, ...]:
            return (*f.candidates, module_store.extra())

        leaked = defective_global(frozen, "doc")
    elif form == "closure":

        def make(bound: Store) -> Any:
            def ranker(f: AuthorizedFrontier, q: str) -> tuple[EvidenceCandidate, ...]:
                return (*f.candidates, bound.extra())

            return ranker

        leaked = make(store)(frozen, "doc")
    else:

        def defective_callback(
            f: AuthorizedFrontier, q: str, *, fetch: Any
        ) -> tuple[EvidenceCandidate, ...]:
            return (*f.candidates, fetch())

        leaked = defective_callback(frozen, "doc", fetch=store.extra)

    # The defect, made visible: an item in the result that the frontier never held.
    assert "evd-99" in ids(leaked)
    assert "evd-99" not in ids(frozen.candidates)

    # And the real ranker, given the same frozen frontier, cannot produce it.
    ranked = rank_candidates(frozen, "doc", limit=100)
    assert "evd-99" not in ids(ranked)
    assert set(ids(ranked)) <= set(ids(frozen.candidates))


# --- ordering: total, deterministic, and not the store's row order ------------


def test_the_order_is_total_and_breaks_ties_on_a_stable_identity() -> None:
    """§8.2's first ordering hazard. Equal instants are common, not exotic.

    Falsifier: sort on `recorded_at_us` alone and the three artifacts below come back in
    whatever order they were passed, which is insertion order dressed up as ranking.
    """
    same = [
        candidate("evd-c", recorded_at_us=BASE_US),
        candidate("evd-a", recorded_at_us=BASE_US),
        candidate("evd-b", recorded_at_us=BASE_US),
    ]
    built = frontier(*same)
    reversed_built = frontier(*reversed(same))

    assert ids(rank_candidates(built, "doc", limit=10)) == ("evd-a", "evd-b", "evd-c")
    assert ids(rank_candidates(reversed_built, "doc", limit=10)) == (
        "evd-a",
        "evd-b",
        "evd-c",
    )


def test_recent_evidence_ranks_before_older_evidence() -> None:
    built = frontier(
        candidate("evd-old", recorded_at_us=BASE_US),
        candidate("evd-new", recorded_at_us=BASE_US + 10),
    )

    assert ids(rank_candidates(built, "doc", limit=10)) == ("evd-new", "evd-old")


def test_the_limit_truncates_the_order_rather_than_the_frontier() -> None:
    """A page is a view of the frontier; the frontier itself is not narrowed by it.

    Falsifier: apply the limit during filtering and the digest depends on page size,
    which would make two honest builds of the same authorized set disagree.
    """
    built = frontier(*(candidate(f"evd-{i}", recorded_at_us=BASE_US + i) for i in range(5)))

    assert len(built.candidates) == 5
    assert ids(rank_candidates(built, "doc", limit=2)) == ("evd-4", "evd-3")
    assert built.checksum == frontier(
        *(candidate(f"evd-{i}", recorded_at_us=BASE_US + i) for i in range(5))
    ).checksum


def test_the_query_selects_from_the_frontier_and_normalizes_both_sides() -> None:
    """Matching is a property of the text, not of the form a caller typed it in."""
    built = frontier(
        candidate("evd-1", search_text="filesystem.archive Ünïcode-DOC"),
        candidate("evd-2", search_text="filesystem.archive other"),
    )

    assert normalize_query("ÜNÏCODE") == normalize_query("ünïcode")
    assert ids(rank_candidates(built, "ÜNÏCODE-doc", limit=10)) == ("evd-1",)
    assert ids(rank_candidates(built, "", limit=10)) == ("evd-1", "evd-2")


def test_the_query_cannot_widen_the_frontier() -> None:
    """Selection narrows. There is no query that returns an excluded artifact.

    Falsifier: have `rank_candidates` fall back to any source other than
    `frontier.candidates` when the query matches nothing -- F4's shape, arrived at by
    accident rather than by design.
    """
    built = frontier(
        candidate("evd-1"),
        candidate("evd-2", permission_labels=("restricted",)),
        grant=EMPTY_GRANT,
    )

    for query in ("", "evd-2", "restricted", "doc", "archive"):
        assert set(ids(rank_candidates(built, query, limit=100))) <= set(
            ids(built.candidates)
        )


# --- the duplicated constant, guarded -----------------------------------------


def test_the_configured_owner_constant_agrees_with_the_services_own_principal() -> None:
    """`retrieval.CONFIGURED_LOCAL_OWNER` and `main.LOCAL_PRINCIPAL` are one fact.

    The ACL stage needs the configured principal from somewhere the session cannot move,
    and importing the process entrypoint into the storage layer would be worse than the
    duplication. This assertion is what stops the duplication rotting: rename either
    constant and the local owner silently stops holding any label, which would be a
    total denial rather than an over-grant -- but a silent one either way.
    """
    assert CONFIGURED_LOCAL_OWNER == LOCAL_PRINCIPAL
