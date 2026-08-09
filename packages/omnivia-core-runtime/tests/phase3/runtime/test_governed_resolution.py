"""V06-3 Lane C acceptance for the read-only sealed governed-version resolver.

Every row this reads was written through 0009's accepted fenced path -- the M3 migration
suite's own seeding helpers, reused rather than restated, so a fixture here can never
assert a shape the accepted writer would refuse. Nothing below inserts a governed row by
any other route.
"""

from __future__ import annotations

import dataclasses

import pytest
import test_blobs_staged_sources_and_evidence_migration as m2
import test_governed_truth_and_relations_migration as m3
from omnivia_core_runtime.ownership.fencing import fenced_transaction
from omnivia_core_runtime.storage.governed import (
    GovernedVersion,
    resolve_governed_versions,
)

#: The M3 suite's own workspace fixture: a Phase 0 baseline migrated through 0009, owned
#: under a live lease, with the M2 evidence chain and eight audit events already seeded.
owned = m3.owned

WORKSPACE_ID = m3.WORKSPACE_ID
BASE_US = m3.BASE_US

#: Late enough that every supersession the seeds record has already happened.
LATE_US = BASE_US + 1_000


def resolve(
    holder: m2.Owned, *, view: str | None = None, instant_us: int = LATE_US
) -> tuple[str, ...]:
    """The resolved version ids, which is what every assertion below is actually about."""
    return tuple(
        version.governed_record_version_id
        for version in resolve_governed_versions(
            holder.connection,
            workspace_id=WORKSPACE_ID,
            resolution_instant_us=instant_us,
            view=view,
        )
    )


def seal_accepted(
    holder: m2.Owned,
    *,
    assembly_id: str,
    version_id: str,
    audit_ref: str,
    record_id: str = "record-1",
    candidate_assembly: str = "assembly-1",
    candidate_version: str = "version-1",
    candidate_audit: str = "audit-1",
    valid_from_us: int = -1,
    valid_to_us: int | None = None,
    recorded_at_us: int | None = None,
    ordinal: int = 1,
    digest: str = m3.DIGEST_B,
) -> None:
    """One sealed, accepted, canonical governed version with an exact validity window.

    `m3.seed_accepted_version` is the same act with 0009's default open-ended window and no
    control over `recorded_at_us`; this states both, because the half-open interval and the
    same-transaction tie-break are two of the properties under test and neither is
    observable at the defaults. Every insert still goes through `m3`'s own row builders
    inside a fenced transaction, so the accepted writer's rules apply unchanged.
    """
    if not holder.connection.execute(
        f"SELECT 1 FROM {m3.SEALS} WHERE workspace_id=? AND governed_record_version_id=?",
        (WORKSPACE_ID, candidate_version),
    ).fetchone():
        m3.seed_human_candidate(
            holder,
            record_id=record_id,
            assembly_id=candidate_assembly,
            version_id=candidate_version,
            audit_ref=candidate_audit,
        )
    event_id = f"event-{assembly_id}"
    row = m3.assembly_row(
        assembly_id,
        version_id,
        record_id,
        layer="governed",
        origin=None,
        disposition="accepted",
        authority="canonical",
        decision_kind="human_reviewer",
        decision_id="reviewer-1",
        audit_ref=audit_ref,
        correlation_id=audit_ref,
        ordinal=ordinal,
        digest=digest,
    )
    row["valid_from_us"] = valid_from_us
    row["valid_to_us"] = valid_to_us
    if recorded_at_us is not None:
        row["recorded_at_us"] = recorded_at_us
    with fenced_transaction(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
    ):
        m3.insert(holder.connection, m3.ASSEMBLIES, row)
        m3.insert(
            holder.connection,
            m3.EVENTS,
            m3.event_row(
                event_id,
                assembly_id,
                version_id,
                "governance.accepted",
                audit_ref=audit_ref,
                correlation_id=audit_ref,
                actor_id="reviewer-1",
                actor_kind="human",
                actor_role="reviewer",
                predecessor_record_id=record_id,
                predecessor_version_id=candidate_version,
            ),
        )
        m3.insert(holder.connection, m3.LINKS, m3.link_row(event_id, assembly_id))
        m3.insert(
            holder.connection,
            m3.SEALS,
            m3.seal_row(
                assembly_id,
                version_id,
                seal_id=f"seal-{assembly_id}",
                correlation_id=audit_ref,
            ),
        )


def seed_chain(holder: m2.Owned) -> None:
    """`record-1` corrected twice: accepted, then superseded at BASE+20, then at BASE+30."""
    m3.seed_corrected_version(holder)
    m3.seed_corrected_version(
        holder,
        source_version_id="version-corrected",
        target_recorded_at_us=BASE_US + 30,
        assembly_id="assembly-corrected-next",
        version_id="version-corrected-next",
        audit_ref="audit-4",
        digest="sha256:" + "5" * 64,
        bootstrap=False,
    )


def seal_correction_recorded_early(
    holder: m2.Owned,
    *,
    edge_recorded_at_us: int,
    target_recorded_at_us: int,
    source_version_id: str = "version-accepted",
    assembly_id: str = "assembly-late-target",
    version_id: str = "version-late-target",
    audit_ref: str = "audit-3",
) -> None:
    """A correction whose edge was recorded before the target assembly it names.

    0009's seal trigger requires the edge to sit in its target's own assembly and requires
    the *source* to be no later than that assembly, but it states nothing at all about the
    edge's `recorded_at_us` against the assembly's -- so this is a shape the accepted
    writer permits, which is why it is seeded here rather than asserted impossible.
    `m3.seed_corrected_version` cannot express it: it stamps both from one argument.
    """
    m3.seed_accepted_version(holder)
    correction_event = f"event-{assembly_id}-corrected"
    supersession_event = f"event-{assembly_id}-superseded"
    with fenced_transaction(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
    ):
        row = m3.assembly_row(
            assembly_id,
            version_id,
            "record-1",
            layer="governed",
            origin=None,
            disposition="accepted",
            authority="canonical",
            decision_kind="human_reviewer",
            decision_id="reviewer-1",
            audit_ref=audit_ref,
            correlation_id=audit_ref,
            ordinal=1,
            digest="sha256:" + "7" * 64,
        )
        row["recorded_at_us"] = target_recorded_at_us
        m3.insert(holder.connection, m3.ASSEMBLIES, row)
        for event_id, action, sequence in (
            (correction_event, "governance.corrected", 1),
            (supersession_event, "record.superseded", 2),
        ):
            m3.insert(
                holder.connection,
                m3.EVENTS,
                m3.event_row(
                    event_id,
                    assembly_id,
                    version_id,
                    action,
                    sequence=sequence,
                    audit_ref=audit_ref,
                    correlation_id=audit_ref,
                    actor_id="reviewer-1",
                    actor_kind="human",
                    actor_role="reviewer",
                    predecessor_record_id="record-1",
                    predecessor_version_id=source_version_id,
                ),
            )
            m3.insert(holder.connection, m3.LINKS, m3.link_row(event_id, assembly_id))
        m3.insert(
            holder.connection,
            m3.SUPERSESSIONS,
            {
                "workspace_id": WORKSPACE_ID,
                "supersession_id": f"supersession-{version_id}",
                "assembly_id": assembly_id,
                "governed_record_id": "record-1",
                "source_version_id": source_version_id,
                "target_version_id": version_id,
                "provenance_event_id": supersession_event,
                "correlation_kind": "m1_audit",
                "correlation_id": audit_ref,
                "recorded_at_us": edge_recorded_at_us,
            },
        )
        m3.insert(
            holder.connection,
            m3.SEALS,
            m3.seal_row(
                assembly_id,
                version_id,
                seal_id=f"seal-{version_id}",
                correlation_id=audit_ref,
            ),
        )


def test_lane_c_01_empty_workspace_resolves_empty_under_every_view(
    owned: m2.Owned,
) -> None:
    for view in (None, "current_canonical", "candidates", "history"):
        assert resolve(owned, view=view) == ()


def test_lane_c_02_absent_view_is_the_current_canonical_view(owned: m2.Owned) -> None:
    m3.seed_accepted_version(owned)
    assert resolve(owned, view=None) == ("version-accepted",)
    assert resolve(owned, view="current_canonical") == ("version-accepted",)


def test_lane_c_03_an_unrecognised_view_is_refused_not_defaulted(
    owned: m2.Owned,
) -> None:
    m3.seed_accepted_version(owned)
    with pytest.raises(ValueError, match="not a recognized GovernedRecordView"):
        resolve(owned, view="Current_Canonical")


def test_lane_c_04_candidates_and_history_are_reachable_only_by_name(
    owned: m2.Owned,
) -> None:
    m3.seed_accepted_version(owned)
    assert resolve(owned, view="candidates") == ("version-1",)
    assert resolve(owned, view="history") == ()
    assert "version-1" not in resolve(owned)


def test_lane_c_05_supersession_chain_folds_to_the_unreplaced_version(
    owned: m2.Owned,
) -> None:
    seed_chain(owned)
    assert resolve(owned) == ("version-corrected-next",)


@pytest.mark.parametrize(
    ("instant_us", "current", "history"),
    (
        (BASE_US + 10, "version-accepted", ()),
        (BASE_US + 20, "version-corrected", ("version-accepted",)),
        (BASE_US + 25, "version-corrected", ("version-accepted",)),
        (
            BASE_US + 30,
            "version-corrected-next",
            ("version-accepted", "version-corrected"),
        ),
    ),
)
def test_lane_c_06_each_view_answers_for_the_instant_it_resolved_at(
    owned: m2.Owned, instant_us: int, current: str, history: tuple[str, ...]
) -> None:
    seed_chain(owned)
    assert resolve(owned, instant_us=instant_us) == (current,)
    assert set(resolve(owned, view="history", instant_us=instant_us)) == set(history)


@pytest.mark.parametrize(
    ("instant_us", "resolved"),
    (
        (BASE_US + 999, ()),
        (BASE_US + 1_000, ("version-windowed",)),
        (BASE_US + 1_999, ("version-windowed",)),
        (BASE_US + 2_000, ()),
    ),
)
def test_lane_c_07_validity_is_the_half_open_interval(
    owned: m2.Owned, instant_us: int, resolved: tuple[str, ...]
) -> None:
    seal_accepted(
        owned,
        assembly_id="assembly-windowed",
        version_id="version-windowed",
        audit_ref="audit-2",
        valid_from_us=BASE_US + 1_000,
        valid_to_us=BASE_US + 2_000,
    )
    assert resolve(owned, instant_us=instant_us) == resolved


def test_lane_c_08_unsealed_assemblies_and_edges_are_invisible_to_every_view(
    owned: m2.Owned,
) -> None:
    """An unsealed correction leaves the version it would have replaced untouched.

    Both halves matter and they fail in opposite directions: an unsealed *assembly* that
    entered a view would publish a version that never earned authority, and an unsealed
    supersession *edge* that counted would retire `version-accepted` -- still the
    workspace's live answer -- in favour of nothing.
    """
    m3.seed_corrected_version(owned, seal=False)
    m3.seed_human_candidate(
        owned,
        record_id="record-2",
        assembly_id="assembly-2",
        version_id="version-2",
        audit_ref="audit-5",
        seal=False,
    )
    assert resolve(owned, view="candidates") == ("version-1",)
    assert resolve(owned) == ("version-accepted",)
    assert resolve(owned, view="history") == ()


def test_lane_c_09_a_foreign_workspace_sees_nothing(owned: m2.Owned) -> None:
    seed_chain(owned)
    for view in (None, "candidates", "history"):
        assert (
            resolve_governed_versions(
                owned.connection,
                workspace_id="workspace-elsewhere",
                resolution_instant_us=LATE_US,
                view=view,
            )
            == ()
        )


def test_lane_c_10_same_transaction_versions_break_their_tie_deterministically(
    owned: m2.Owned,
) -> None:
    """Equal `recorded_at_us` is decided by stream identity, not by row order.

    The two assemblies are named against their streams on purpose -- `assembly-zeta`
    carries `audit-2` and `assembly-alpha` carries `audit-3` -- so a key that fell through
    to `assembly_id` would pick `version-zeta` and a key that stops at the stream picks
    `version-alpha`. Either way the answer is a stated one, which is the property: it does
    not come from whichever row SQLite happened to hand back last.
    """
    for assembly_id, version_id, audit_ref, digest in (
        ("assembly-zeta", "version-zeta", "audit-2", m3.DIGEST_B),
        ("assembly-alpha", "version-alpha", "audit-3", "sha256:" + "6" * 64),
    ):
        seal_accepted(
            owned,
            assembly_id=assembly_id,
            version_id=version_id,
            audit_ref=audit_ref,
            recorded_at_us=BASE_US + 10,
            digest=digest,
        )
    assert resolve(owned) == ("version-alpha",)
    assert resolve(owned) == resolve(owned)


def test_lane_c_11_only_accepted_canonical_versions_reach_the_canonical_views(
    owned: m2.Owned,
) -> None:
    m3.seed_governed_outcome(owned, "rejected")
    assert resolve(owned) == ()
    assert resolve(owned, view="history") == ()
    assert resolve(owned, view="candidates") == ("version-1",)


def test_lane_c_12_the_returned_frontier_is_frozen(owned: m2.Owned) -> None:
    seed_chain(owned)
    frontier = resolve_governed_versions(
        owned.connection, workspace_id=WORKSPACE_ID, resolution_instant_us=LATE_US
    )
    assert isinstance(frontier, tuple)
    assert all(isinstance(version, GovernedVersion) for version in frontier)
    with pytest.raises(dataclasses.FrozenInstanceError):
        frontier[0].authority_level = "proposed"  # type: ignore[misc]


def test_lane_c_13_resolution_writes_nothing(owned: m2.Owned) -> None:
    seed_chain(owned)
    before = owned.connection.execute(f"SELECT COUNT(*) FROM {m3.VIEW}").fetchone()[0]
    for view in (None, "candidates", "history"):
        resolve(owned, view=view)
    assert (
        owned.connection.execute(f"SELECT COUNT(*) FROM {m3.VIEW}").fetchone()[0]
        == before
    )
    assert owned.connection.in_transaction is False


@pytest.mark.parametrize(
    ("instant_us", "current", "history"),
    (
        (BASE_US + 20, ("version-accepted",), ()),
        (BASE_US + 25, ("version-accepted",), ()),
        (BASE_US + 30, ("version-late-target",), ("version-accepted",)),
        (LATE_US, ("version-late-target",), ("version-accepted",)),
    ),
)
def test_lane_c_14_an_edge_recorded_before_its_target_retires_nothing_early(
    owned: m2.Owned,
    instant_us: int,
    current: tuple[str, ...],
    history: tuple[str, ...],
) -> None:
    """A supersession takes effect only once *both* halves of it are visible.

    The edge is recorded at BASE+20 and the sealed target assembly it names at BASE+30.
    Folding the edge alone would retire `version-accepted` at BASE+20 while its replacement
    was still unwritten, so BASE+25 would resolve to no current version at all and put a
    version the workspace was still citing into `history` -- an answer the workspace never
    held. The effective replacement instant is the later of the two.
    """
    seal_correction_recorded_early(
        owned, edge_recorded_at_us=BASE_US + 20, target_recorded_at_us=BASE_US + 30
    )
    assert resolve(owned, instant_us=instant_us) == current
    assert resolve(owned, view="history", instant_us=instant_us) == history


@pytest.mark.parametrize(
    ("earlier_stream_ordinal", "later_stream_ordinal"), ((9_000, 1), (1, 9_000))
)
def test_lane_c_15_ordinals_from_different_streams_are_never_compared(
    owned: m2.Owned, earlier_stream_ordinal: int, later_stream_ordinal: int
) -> None:
    """A stream's own counter cannot outrank the other stream it was never counting.

    0009 scopes `append_ordinal` to the correlation parent -- *"ordinals from different
    parents are never compared, because they were never counting the same thing"* -- so
    two frontier versions tied on authority and `recorded_at_us` must be separated by
    which stream they came from, not by how far each stream's local counter had got.

    `audit-2` and `audit-3` are two such streams, and the ordinals are deliberately
    lopsided in both directions: the lexically earlier stream is run up to 9000 in one
    parametrisation and left at 1 in the other. The winner is the same version both times,
    because nothing about the answer may depend on those two numbers' magnitudes.

    Falsifier: order `_precedence` by `append_ordinal` before `(correlation_kind,
    correlation_id)` and the two parametrisations disagree -- `(9_000, 1)` resolves to
    `version-earlier-stream` and `(1, 9_000)` to `version-later-stream`, so swapping two
    unrelated counters silently swaps which version the workspace cites.
    """
    for assembly_id, version_id, audit_ref, ordinal, digest in (
        (
            "assembly-earlier-stream",
            "version-earlier-stream",
            "audit-2",
            earlier_stream_ordinal,
            m3.DIGEST_B,
        ),
        (
            "assembly-later-stream",
            "version-later-stream",
            "audit-3",
            later_stream_ordinal,
            "sha256:" + "6" * 64,
        ),
    ):
        seal_accepted(
            owned,
            assembly_id=assembly_id,
            version_id=version_id,
            audit_ref=audit_ref,
            recorded_at_us=BASE_US + 10,
            ordinal=ordinal,
            digest=digest,
        )
    assert resolve(owned) == ("version-later-stream",)


def test_lane_c_16_the_append_ordinal_still_orders_one_stream(owned: m2.Owned) -> None:
    """Inside one correlation stream the ordinal is exactly what separates two versions.

    Both versions are sealed under `audit-2`, so `_precedence` reaches the ordinal with
    everything before it equal -- and 0009's own unique index on `(workspace,
    correlation_kind, correlation_id, append_ordinal)` makes that comparison total.
    `assembly-alpha-stream` carries the higher ordinal on purpose: a key that ignored the
    ordinal and fell through to `assembly_id` would pick `version-zeta-stream` instead.

    Falsifier: drop `append_ordinal` from `_precedence` and this resolves to
    `version-zeta-stream`, retiring the later version of the stream in favour of an
    earlier one on nothing but an alphabetical accident.
    """
    for assembly_id, version_id, ordinal, digest in (
        ("assembly-alpha-stream", "version-alpha-stream", 7, m3.DIGEST_B),
        ("assembly-zeta-stream", "version-zeta-stream", 1, "sha256:" + "6" * 64),
    ):
        seal_accepted(
            owned,
            assembly_id=assembly_id,
            version_id=version_id,
            audit_ref="audit-2",
            recorded_at_us=BASE_US + 10,
            ordinal=ordinal,
            digest=digest,
        )
    assert resolve(owned) == ("version-alpha-stream",)
