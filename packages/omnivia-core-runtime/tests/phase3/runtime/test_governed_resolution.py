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
    assert resolve(owned) == ("version-zeta",)
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
