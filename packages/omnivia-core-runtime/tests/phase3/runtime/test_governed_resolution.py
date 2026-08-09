"""V06-3 Lane C acceptance for the read-only sealed governed-version resolver.

Every row this reads was written through 0009's accepted fenced path -- the M3 migration
suite's own seeding helpers, reused rather than restated, so a fixture here can never
assert a shape the accepted writer would refuse. Nothing below inserts a governed row by
any other route.
"""

from __future__ import annotations

import dataclasses
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
import test_blobs_staged_sources_and_evidence_migration as m2
import test_governed_truth_and_relations_migration as m3
from omnivia_core_runtime.ownership.fencing import fenced_transaction
from omnivia_core_runtime.storage import governed
from omnivia_core_runtime.storage.connection import (
    install_authorizer,
    mark_authorizer_installed,
)
from omnivia_core_runtime.storage.governed import (
    GovernedSupersession,
    GovernedVersion,
    read_governed_records,
    read_governed_supersessions,
    resolve_governed_versions,
)

from omnivia_core.contracts.v1 import (
    ContractSemanticError,
    GovernedRecord,
    MemorySearchInput,
    MemorySearchResult,
    PageMetadata,
    RecordTemporalMetadata,
    SupersessionReference,
    validate_governed_record,
    validate_record_temporal_metadata,
)

#: `omnivia_core.contracts.v1` re-exports `validate_knowledge_search_result` but not its
#: `memory.search` twin, so the candidates-view semantic validator is reached through the
#: module that declares it in `__all__`. Closing that export gap is a contracts change and
#: this lane does not own one.
from omnivia_core.contracts.v1.semantics_knowledge import validate_memory_search_result

#: The M3 suite's own workspace fixture: a Phase 0 baseline migrated through 0009, owned
#: under a live lease, with the M2 evidence chain and eight audit events already seeded.
owned = m3.owned

WORKSPACE_ID = m3.WORKSPACE_ID
BASE_US = m3.BASE_US

#: Late enough that every supersession the seeds record has already happened.
LATE_US = BASE_US + 1_000


@pytest.fixture
def concurrent(tmp_path: Path) -> Iterator[tuple[m2.Owned, m2.Owned]]:
    """The `owned` workspace, reachable by a resolver *and* a second, concurrent writer.

    `m3.owned` cannot be used for this and no fixture built on `take_ownership` can:
    `OpenMode.SERVICE_OWNED` sets `locking_mode = EXCLUSIVE`, which is the point of it --
    one Core Service holds one workspace file for its whole ownership lifetime -- and that
    lock is never released while the connection is open, so a second connection of any mode
    against the same file fails to open at all. Both connections here are therefore
    `EPHEMERAL`, which is writable and non-exclusive.

    What the EPHEMERAL open does not do for itself is install the authorizer, so both
    connections are given the same deny-by-default one `open_database` installs on a
    `SERVICE_OWNED` open, and given it before either has written a row. Every seed and
    every write below therefore runs the normal installed authorizer path, reaching a row
    only through the widening `fenced_transaction` performs -- the same fence every other
    test in this file writes under.

    WAL is asserted rather than assumed. It is what lets the writer commit while the
    resolver is still holding an open read snapshot, which is the whole interleaving lane
    C 19 forces; under a rollback journal the writer would block on the reader instead.

    Everything else is `m3.owned`, statement for statement: the same Phase 0 baseline, the
    same migration catalogue, the same lease, the same guard row and the same M2 evidence
    chain and eight audit events. The lease and guard live in the database rather than on a
    connection, so the writer proves its authority against the same rows the resolver's
    connection installed, and writes through `fenced_transaction` exactly as every other
    seed in this file does.
    """
    path = tmp_path / "workspace.sqlite"
    m2.materialise_phase0_baseline(path)
    with m2.migration_catalogue_through(m3.MIGRATION_VERSION):
        m2.bootstrap_and_migrate(path)
    identity = m2.make_identity()
    resolver = m2.open_database(path, m2.OpenMode.EPHEMERAL)
    writer = m2.open_database(path, m2.OpenMode.EPHEMERAL)
    for connection in (resolver, writer):
        journal = connection.execute("PRAGMA journal_mode").fetchone()[0]
        assert str(journal).lower() == "wal"
        install_authorizer(connection, allow_mutations=False, allow_ddl=False)
        mark_authorizer_installed(connection)
    lease = m2.acquire_lease(
        resolver,
        identity,
        clock=m2.FakeClock(),
        workspace_id=WORKSPACE_ID,
        holds_storage_lock=True,
        lock_mechanism="flock",
    )
    m2.open_guard(
        resolver,
        identity,
        clock=m2.FakeClock(),
        workspace_id=WORKSPACE_ID,
        fencing_generation=lease.fencing_generation,
    )
    holder = m2.Owned(
        connection=resolver,
        identity=identity,
        generation=lease.fencing_generation,
        path=path,
    )
    m2.seed_chain(holder)
    with fenced_transaction(
        resolver,
        identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
    ):
        for number in range(1, 9):
            m3.insert(
                resolver,
                "omnivia_application_audit_events",
                m3.audit_row(f"audit-{number}"),
            )
    yield holder, dataclasses.replace(holder, connection=writer)
    writer.close()
    resolver.close()


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


def test_lane_c_17_a_supersession_fact_appears_only_once_it_took_effect(
    owned: m2.Owned,
) -> None:
    """The fact reader dates a supersession by when it took effect, not by either row.

    Same seed as lane C 14: the edge is at BASE+20 and the sealed target assembly it names
    at BASE+30. At BASE+25 the replacement had not been written, so there is no fact to
    read at all -- and when the fact does appear it is stamped BASE+30, the later of the
    two, carrying the reason its own provenance event recorded.
    """
    seal_correction_recorded_early(
        owned, edge_recorded_at_us=BASE_US + 20, target_recorded_at_us=BASE_US + 30
    )
    assert (
        read_governed_supersessions(
            owned.connection,
            workspace_id=WORKSPACE_ID,
            resolution_instant_us=BASE_US + 25,
        )
        == ()
    )
    assert read_governed_supersessions(
        owned.connection, workspace_id=WORKSPACE_ID, resolution_instant_us=BASE_US + 30
    ) == (
        GovernedSupersession(
            workspace_id=WORKSPACE_ID,
            governed_record_id="record-1",
            source_version_id="version-accepted",
            target_version_id="version-late-target",
            assembly_id="assembly-late-target",
            effective_at_us=BASE_US + 30,
            reason_code="governance.reviewed",
        ),
    )


def test_lane_c_18_the_supersession_facts_are_deterministic(owned: m2.Owned) -> None:
    """Two corrections of one record read back in the reader's stated order, not row order.

    The chain is seeded so those two orders disagree. `version-a-corrected` sorts *before*
    `version-accepted` -- `-` precedes `c` -- and it is the source of the edge written
    second, so the tuple the reader must return is the exact reverse of the order the two
    edges were inserted in.

    Falsifier: drop the `ORDER BY` from the fact reader and this asserts against whatever
    order the query plan happened to walk the edge table in; the current primary-key scan
    yields insertion order -- the reverse of what is asserted here.
    """
    m3.seed_corrected_version(
        owned, assembly_id="assembly-a-corrected", version_id="version-a-corrected"
    )
    m3.seed_corrected_version(
        owned,
        source_version_id="version-a-corrected",
        target_recorded_at_us=BASE_US + 30,
        assembly_id="assembly-z-corrected",
        version_id="version-z-corrected",
        audit_ref="audit-4",
        digest="sha256:" + "5" * 64,
        bootstrap=False,
    )
    facts = read_governed_supersessions(
        owned.connection, workspace_id=WORKSPACE_ID, resolution_instant_us=LATE_US
    )
    assert facts == (
        GovernedSupersession(
            workspace_id=WORKSPACE_ID,
            governed_record_id="record-1",
            source_version_id="version-a-corrected",
            target_version_id="version-z-corrected",
            assembly_id="assembly-z-corrected",
            effective_at_us=BASE_US + 30,
            reason_code="governance.reviewed",
        ),
        GovernedSupersession(
            workspace_id=WORKSPACE_ID,
            governed_record_id="record-1",
            source_version_id="version-accepted",
            target_version_id="version-a-corrected",
            assembly_id="assembly-a-corrected",
            effective_at_us=BASE_US + 20,
            reason_code="governance.reviewed",
        ),
    )
    assert facts == read_governed_supersessions(
        owned.connection, workspace_id=WORKSPACE_ID, resolution_instant_us=LATE_US
    )


def test_lane_c_19_a_correction_sealed_mid_resolution_cannot_split_the_snapshot(
    concurrent: tuple[m2.Owned, m2.Owned], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A resolution reads one database state, even while an accepted writer seals into it.

    The frontier is folded from two queries, and between them is exactly where a workspace
    is at its most dangerous: another connection seals a correction, so the second query
    sees the edge retiring `version-accepted` while the first has already handed back a row
    set that does not contain the replacement. The record then resolves to *nothing* -- a
    version the workspace was citing a microsecond earlier, gone, with no instant at which
    that answer was ever true.

    The interleaving is forced rather than raced: the writer runs from inside a hook on the
    fact reader itself, so the correction is committed after the version SELECT and before
    the edge SELECT on every run, on every machine, with no sleep anywhere. The writer is
    the second EPHEMERAL connection onto this workspace, carrying the same installed
    authorizer and writing under this workspace's own lease and guard through
    `fenced_transaction`, so the correction is a real, accepted, sealed, committed row --
    not a shape only this test can produce.

    Falsifier: read the two queries in separate autocommit statements, as this module did
    before, and the assertion below gets `()`.
    """
    resolver, writer = concurrent
    m3.seed_accepted_version(resolver)
    read_facts = governed._read_supersession_facts
    sealed: list[str] = []

    def seal_then_read(
        fenced: sqlite3.Connection, *, workspace_id: str, resolution_instant_us: int
    ) -> tuple[GovernedSupersession, ...]:
        if not sealed:
            sealed.append("version-corrected")
            m3.seed_corrected_version(writer, bootstrap=False)
        return read_facts(
            fenced,
            workspace_id=workspace_id,
            resolution_instant_us=resolution_instant_us,
        )

    monkeypatch.setattr(governed, "_read_supersession_facts", seal_then_read)
    assert resolve(resolver) == ("version-accepted",)
    assert sealed == ["version-corrected"]
    assert resolver.connection.in_transaction is False
    monkeypatch.undo()
    # The correction really did commit: the next resolution, which is the first one whose
    # snapshot opens after it, sees it whole.
    assert resolve(resolver) == ("version-corrected",)


def test_lane_c_20_a_caller_owned_transaction_is_left_exactly_as_it_was_found(
    owned: m2.Owned,
) -> None:
    """The resolver ends only a transaction it opened itself.

    A caller resolving inside its own transaction is reading the frontier as one fact among
    several it means to see together; committing or rolling that transaction back here would
    end the caller's snapshot underneath it and silently make the reads around this one
    incomparable. Lane C 13 asserts the other half -- that a transaction this function *did*
    open is closed before it returns.
    """
    seed_chain(owned)
    owned.connection.execute("BEGIN")
    try:
        assert resolve(owned) == ("version-corrected-next",)
        assert owned.connection.in_transaction is True
    finally:
        owned.connection.execute("ROLLBACK")


def snapshot(
    holder: m2.Owned, *, view: str | None = None, instant_us: int = LATE_US
) -> governed._GovernedHydrationSnapshot:
    """The hydration snapshot, which the four tests below are about."""
    return governed._read_governed_hydration_snapshot(
        holder.connection,
        workspace_id=WORKSPACE_ID,
        resolution_instant_us=instant_us,
        view=view,
    )


def test_lane_c_21_a_snapshot_carries_its_own_versions_provenance_and_sources(
    owned: m2.Owned,
) -> None:
    """One selected version, its ordered events, and the exact source rows they cite.

    `assembly-late-target` is the workspace's current canonical version at LATE_US and it
    carries two provenance events, so the ordering is observable rather than incidental. The
    workspace also holds `assembly-1` (a sealed candidate) and `assembly-accepted` (now
    history), each with provenance and evidence links of its own -- none of which is this
    version's account of itself.

    The link fields are asserted whole, against the M2 chain `seed_chain` seeded: a join that
    reached the artifact but not the span, or the span of some other record, would still
    return a plausible-looking citation with the pointer and offsets quietly absent or wrong.

    Falsifier: drop the assembly filter and the events tuple gains `assembly-1`'s and
    `assembly-accepted`'s one event each, so the version's provenance reads as the
    workspace's four acts rather than the two that happened to it.
    """
    seal_correction_recorded_early(
        owned, edge_recorded_at_us=BASE_US + 20, target_recorded_at_us=BASE_US + 30
    )
    held = snapshot(owned)
    assert tuple(v.governed_record_version_id for v in held.versions) == (
        "version-late-target",
    )
    assert tuple((e.provenance_event_id, e.action) for e in held.events) == (
        ("event-assembly-late-target-corrected", "governance.corrected"),
        ("event-assembly-late-target-superseded", "record.superseded"),
    )
    assert {e.assembly_id for e in held.events} == {"assembly-late-target"}
    assert all(e.evidence_disposition == "available" for e in held.events)
    assert held.events[0] == governed._GovernedProvenanceEvent(
        workspace_id=WORKSPACE_ID,
        assembly_id="assembly-late-target",
        governed_record_version_id="version-late-target",
        provenance_event_id="event-assembly-late-target-corrected",
        provenance_sequence=1,
        action="governance.corrected",
        actor_id="reviewer-1",
        actor_kind="human",
        policy_id=None,
        occurred_at_us=BASE_US + 1,
        reason_code="governance.reviewed",
        reason_comment=None,
        evidence_disposition="available",
    )
    assert held.links == tuple(
        governed._GovernedEvidenceLink(
            workspace_id=WORKSPACE_ID,
            assembly_id="assembly-late-target",
            governed_record_version_id="version-late-target",
            provenance_event_id=event_id,
            provenance_sequence=sequence,
            link_ordinal=1,
            evidence_id="evd-0001",
            source_kind="filesystem.archive",
            source_native_id="doc-1",
            source_locator="archive://doc.md",
            source_retrieved_at_us=m2.BASE_US,
            ingested_at_us=m2.BASE_US + 4,
            event_at_us=m2.BASE_US - 10,
            observed_at_us=m2.BASE_US - 5,
            normalized_record_id="nrc-0001",
            normalized_span_id="nsp-0001",
            span_pointer="/body/0",
            span_start_offset=0,
            span_end_offset=10,
        )
        for event_id, sequence in (
            ("event-assembly-late-target-corrected", 1),
            ("event-assembly-late-target-superseded", 2),
        )
    )
    assert held.supersessions == read_governed_supersessions(
        owned.connection, workspace_id=WORKSPACE_ID, resolution_instant_us=LATE_US
    )


def test_lane_c_22_each_view_carries_only_the_support_of_what_it_selected(
    owned: m2.Owned,
) -> None:
    """Support rows follow the view's selection, so no view describes another's versions.

    The same workspace answers three different questions here, and the wrong support attached
    to the right versions is the more dangerous half of getting it wrong: a `candidates`
    snapshot carrying the governed layer's provenance would show an unreviewed proposal
    annotated with the acceptance of something else entirely.

    Events and links follow the selected *assemblies*; the supersession edges cannot, because
    an edge names two versions and a view need not hold both of them. So they are scoped by
    version id on either end, and the three views want three different parts of the same two
    edges: `current_canonical` keeps only the incoming edge that made `version-corrected-next`
    current, `history` keeps both -- the one that retired `version-accepted` and the one out
    of `version-corrected` that retired it in turn -- and `candidates` names neither end of
    either and carries no edges at all.

    Falsifier: return the workspace's events unfiltered and every assertion below sees all
    four assemblies, whichever view was asked for; return the facts unfiltered and all three
    views carry both edges, so `candidates` explains a correction chain none of its versions
    is part of.
    """
    seed_chain(owned)
    accepted_to_corrected = GovernedSupersession(
        workspace_id=WORKSPACE_ID,
        governed_record_id="record-1",
        source_version_id="version-accepted",
        target_version_id="version-corrected",
        assembly_id="assembly-corrected",
        effective_at_us=BASE_US + 20,
        reason_code="governance.reviewed",
    )
    corrected_to_next = GovernedSupersession(
        workspace_id=WORKSPACE_ID,
        governed_record_id="record-1",
        source_version_id="version-corrected",
        target_version_id="version-corrected-next",
        assembly_id="assembly-corrected-next",
        effective_at_us=BASE_US + 30,
        reason_code="governance.reviewed",
    )
    for view, assemblies, facts in (
        ("candidates", {"assembly-1"}, ()),
        (
            "history",
            {"assembly-accepted", "assembly-corrected"},
            (accepted_to_corrected, corrected_to_next),
        ),
        (None, {"assembly-corrected-next"}, (corrected_to_next,)),
    ):
        held = snapshot(owned, view=view)
        assert {v.assembly_id for v in held.versions} == assemblies
        assert {e.assembly_id for e in held.events} == assemblies
        assert {link.assembly_id for link in held.links} == assemblies
        assert held.supersessions == facts


def test_lane_c_23_the_snapshot_ends_only_a_transaction_it_opened(
    owned: m2.Owned,
) -> None:
    """A caller's transaction outlives the snapshot; one opened here does not.

    Both halves are the same rule as lane C 13 and 20, restated one level out, because this
    reader opens the transaction the resolver then declines to end -- so an ownership bug
    here would be invisible to those two.

    Falsifier: commit unconditionally and the caller's transaction is gone at the assertion
    below, taking the snapshot the caller was reading around this one with it.
    """
    seed_chain(owned)
    assert snapshot(owned).versions
    assert owned.connection.in_transaction is False

    owned.connection.execute("BEGIN")
    try:
        assert snapshot(owned).versions
        assert owned.connection.in_transaction is True
    finally:
        owned.connection.execute("ROLLBACK")


def test_lane_c_24_a_correction_sealed_before_the_support_reads_cannot_split_it(
    concurrent: tuple[m2.Owned, m2.Owned], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The support facts are read from the state the versions were resolved from.

    Lane C 19 forces the interleave between the resolver's two queries; this one forces it at
    the next boundary out, where the versions have been chosen and nothing describing them has
    been read yet. An accepted writer seals `version-corrected` there, and a support read
    outside the resolution's transaction would then see the edge retiring `version-accepted`.
    That edge names a version this frontier does contain, so it survives the version scoping
    and comes back attached to a frontier still saying `version-accepted` is current: the
    frontier and its own account of itself disagreeing about which correction had happened.
    The correction's own events and links do not survive -- `assembly-corrected` was never
    selected -- which is why the edge is the thing that has to be asserted here.

    The writer runs from inside a hook on `_read_hydration_support`, so the commit lands after
    version resolution and before the first support query on every run, with no sleep. It is
    the second EPHEMERAL connection, carrying the same installed authorizer and writing under
    this workspace's lease and guard through `fenced_transaction`.

    Falsifier: read the support rows outside the snapshot's transaction -- in autocommit, or
    after the commit -- and `supersessions` comes back holding the edge that retires the
    version `versions` still contains.
    """
    resolver, writer = concurrent
    m3.seed_accepted_version(resolver)
    read_support = governed._read_hydration_support
    sealed: list[str] = []

    def seal_then_read(
        fenced: sqlite3.Connection, *, workspace_id: str, resolution_instant_us: int
    ) -> tuple[
        tuple[GovernedSupersession, ...],
        tuple[governed._GovernedProvenanceEvent, ...],
        tuple[governed._GovernedEvidenceLink, ...],
    ]:
        if not sealed:
            sealed.append("version-corrected")
            m3.seed_corrected_version(writer, bootstrap=False)
        return read_support(
            fenced,
            workspace_id=workspace_id,
            resolution_instant_us=resolution_instant_us,
        )

    monkeypatch.setattr(governed, "_read_hydration_support", seal_then_read)
    held = snapshot(resolver)
    assert sealed == ["version-corrected"]
    assert tuple(v.governed_record_version_id for v in held.versions) == (
        "version-accepted",
    )
    assert held.supersessions == ()
    assert {e.assembly_id for e in held.events} == {"assembly-accepted"}
    assert {link.assembly_id for link in held.links} == {"assembly-accepted"}
    assert resolver.connection.in_transaction is False

    monkeypatch.undo()
    # The correction really did commit: the next snapshot, the first whose transaction opens
    # after it, sees the new version and the edge retiring the old one together.
    later = snapshot(resolver)
    assert tuple(v.governed_record_version_id for v in later.versions) == (
        "version-corrected",
    )
    assert {e.assembly_id for e in later.events} == {"assembly-corrected"}
    assert tuple(f.target_version_id for f in later.supersessions) == (
        "version-corrected",
    )


#: 2023-11-14T22:13:20Z, plus 123_456 microseconds -- a sub-millisecond remainder, so every
#: rendering below fails if the last three digits are rounded rather than truncated.
RECORDED_US = 1_700_000_000_123_456


def temporal_version(
    *,
    recorded_at_us: int = RECORDED_US,
    valid_from_us: int = RECORDED_US - 60_000_000,
    valid_to_us: int | None = None,
    sealed_at_us: int = RECORDED_US + 90_000_000,
) -> GovernedVersion:
    """A `GovernedVersion` carrying the four instants these tests distinguish between.

    Every other field is a placeholder, which is itself part of the assertion: the helper
    under test is pure and reads nothing but the instants, so nothing here needs a database
    and no seeded row can make one of these tests pass for the wrong reason. The three
    timestamps are deliberately distinct, so a mapping that reached for `sealed_at_us` or
    `valid_from_us` where Amendment 008 says `recorded_at_us` renders a visibly different
    string.
    """
    return GovernedVersion(
        workspace_id=WORKSPACE_ID,
        assembly_id="assembly-t",
        seal_id="seal-t",
        governed_record_id="record-t",
        governed_record_version_id="version-t",
        record_type="note",
        domain_scope="scope-t",
        layer=governed.LAYER_GOVERNED,
        governance_disposition="accepted",
        authority_level="canonical",
        decision_source_kind=None,
        decision_source_id=None,
        content_schema_version="1.0.0",
        content_json="{}",
        content_digest="digest-t",
        evidence_disposition="cited",
        valid_from_us=valid_from_us,
        valid_to_us=valid_to_us,
        recorded_at_us=recorded_at_us,
        append_ordinal=0,
        correlation_kind="audit",
        correlation_id="audit-t",
        audit_ref=None,
        sealed_at_us=sealed_at_us,
    )


@pytest.mark.parametrize(
    ("recorded_at_us", "expected"),
    [
        (1_700_000_000_000_000, "2023-11-14T22:13:20.000Z"),
        (1_700_000_000_000_999, "2023-11-14T22:13:20.000Z"),
        (1_700_000_000_001_000, "2023-11-14T22:13:20.001Z"),
        (RECORDED_US, "2023-11-14T22:13:20.123Z"),
        (1_700_000_000_999_999, "2023-11-14T22:13:20.999Z"),
    ],
)
def test_lane_c_25_the_instants_render_as_truncated_millisecond_utc(
    recorded_at_us: int, expected: str
) -> None:
    """Microseconds render as the canonical millisecond `Timestamp`, truncated not rounded.

    This is the one shape the contract's pattern accepts and the one every other timestamp
    this service emits already has, so a governed record's instants have to be
    indistinguishable from an evidence candidate's. The sub-millisecond cases are the whole
    test: `999` microseconds is *still* `.000Z`, and `999_999` is `.999Z` rather than the
    next second.

    Falsifier: round instead of truncating and the second and fifth rows move a millisecond;
    emit microsecond precision and every row grows three digits the pattern refuses.
    """
    assert governed._temporal_metadata(
        temporal_version(recorded_at_us=recorded_at_us)
    ).recorded_at == expected


def test_lane_c_26_ingestion_and_record_time_are_both_the_versions_recorded_instant() -> None:
    """`ingested_at` and `recorded_at` both render the version's `recorded_at_us`.

    `recorded_at` is the authoritative persisted assembly time. `ingested_at` is required by
    the contract, but 0009 stores no distinct governed-record ingestion instant, so
    Amendment 008 approves reusing `recorded_at_us` for it as an explicit V06-3 read-model
    compatibility projection -- not an assertion that ingestion never happened or that it
    coincided with persistence. The two other clocks reachable from here, the seal's and the
    validity window's, answer different questions, and the fixture keeps all three distinct
    so a mapping that took the wrong one cannot render the right string by accident.

    The three optional instants are asserted absent because the exact facts they require are
    not inputs here: `event_at` and `observed_at` are facts about the world behind the record
    and `superseded_at` is a fact about an edge. Substituting an evidence artifact's or a
    provenance event's clock -- for those or for `ingested_at` -- is forbidden; it would
    publish an account no row asserts.

    Falsifier: map `ingested_at` to `sealed_at_us` (or to `valid_from_us`) and the equality
    below fails against a timestamp that still looks perfectly well-formed.
    """
    temporal = governed._temporal_metadata(temporal_version())

    assert temporal.ingested_at == temporal.recorded_at == "2023-11-14T22:13:20.123Z"
    assert temporal.ingested_at != "2023-11-14T22:14:50.123Z"  # sealed_at_us
    assert temporal.ingested_at != "2023-11-14T22:12:20.123Z"  # valid_from_us
    assert temporal.event_at is None
    assert temporal.observed_at is None
    assert temporal.superseded_at is None


def test_lane_c_27_the_validity_window_maps_exactly_and_an_open_end_stays_absent() -> None:
    """`[valid_from_us, valid_to_us)` becomes `valid_from`/`valid_until`, verbatim.

    An absent `valid_to_us` is 0009's open-ended assertion -- this version is valid until
    something replaces it -- and it stays *absent* on the wire rather than becoming a
    far-future instant. A synthesised upper bound would be a claim the database never made,
    and one a reader could not tell from a real one.

    Falsifier: substitute any sentinel for the open end and the `is None` below fails; swap
    the two bounds and the rendered strings trade places.
    """
    open_ended = governed._temporal_metadata(temporal_version())
    assert open_ended.valid_from == "2023-11-14T22:12:20.123Z"
    assert open_ended.valid_until is None

    bounded = governed._temporal_metadata(
        temporal_version(valid_to_us=RECORDED_US + 60_000_000)
    )
    assert bounded.valid_from == "2023-11-14T22:12:20.123Z"
    assert bounded.valid_until == "2023-11-14T22:14:20.123Z"


def test_lane_c_28_the_metadata_validates_and_round_trips_through_the_wire() -> None:
    """The value the helper returns is one the contract accepts and can decode back.

    Two separate claims. It passes `validate_record_temporal_metadata` -- which parses every
    present instant and checks that `ingested_at` is not after `recorded_at` and
    `valid_from` not after `valid_until` -- and it survives `to_wire`/`from_wire` unchanged,
    which is what a peer on the other side of the boundary actually reads. The absent fields
    are asserted missing from the wire mapping rather than present-and-null, because that is
    the difference between "not stated" and "stated as nothing".

    Falsifier: emit a non-canonical timestamp and validation raises; emit the absent optionals
    as nulls and `from_wire` refuses the payload outright.
    """
    temporal = governed._temporal_metadata(
        temporal_version(valid_to_us=RECORDED_US + 60_000_000)
    )

    validate_record_temporal_metadata(temporal)
    wire = temporal.to_wire()
    assert set(wire) == {"ingested_at", "recorded_at", "valid_from", "valid_until"}
    assert RecordTemporalMetadata.from_wire(wire) == temporal


def test_lane_c_29_the_helper_is_pure_and_deterministic() -> None:
    """Same version in, equal value out, every time, and nothing changed on the way through.

    The helper reads no clock and no connection, so the only thing its answer can depend on
    is the version handed to it -- which is what makes a governed record's published
    temporal metadata reproducible for a version sealed years ago. The input is compared
    field by field before and after, and the output is a frozen value, so neither end of the
    call is somewhere a later assembly step could write.

    Falsifier: fill any field from `datetime.now` and the two calls stop being equal.
    """
    version = temporal_version(valid_to_us=RECORDED_US + 60_000_000)
    before = dataclasses.asdict(version)

    first = governed._temporal_metadata(version)
    second = governed._temporal_metadata(version)

    assert first == second
    assert dataclasses.asdict(version) == before
    with pytest.raises(dataclasses.FrozenInstanceError):
        first.recorded_at = "2023-11-14T22:13:20.999Z"  # type: ignore[misc]


#: Four instants, each in a different second, so every clock this hydration could reach for
#: renders a visibly different string: the version's own `recorded_at_us`, the artifact and
#: provenance clocks it must never reach for, and the instant a supersession took effect.
HYDRATED_US = 1_700_000_000_123_456
ARTIFACT_US = HYDRATED_US - 300_000_000
EVENT_US = HYDRATED_US - 200_000_000
EFFECTIVE_US = HYDRATED_US + 500_000_000


def hydratable(**overrides: object) -> GovernedVersion:
    """One sealed, accepted, canonical governed version whose every field is hydratable.

    Distinct from `temporal_version`, which is deliberately built from placeholders because
    `_temporal_metadata` reads nothing but the instants. A whole `GovernedRecord` is
    validated against the contract, so every field here has to be the shape 0009 actually
    stores -- a lowercase dotted `record_type`/`domain_scope`, a governed layer carrying a
    disposition and a `decision_source_id`, and canonical JSON content.
    """
    base = GovernedVersion(
        workspace_id=WORKSPACE_ID,
        assembly_id="assembly-h",
        seal_id="seal-h",
        governed_record_id="record-h",
        governed_record_version_id="version-h",
        record_type="knowledge.claim",
        domain_scope="product.core",
        layer=governed.LAYER_GOVERNED,
        governance_disposition="accepted",
        authority_level="canonical",
        decision_source_kind="human_reviewer",
        decision_source_id="reviewer-h",
        content_schema_version="1.0",
        content_json='{"statement":"A durable claim"}',
        content_digest=m3.DIGEST_B,
        evidence_disposition="available",
        valid_from_us=HYDRATED_US - 60_000_000,
        valid_to_us=None,
        recorded_at_us=HYDRATED_US,
        append_ordinal=1,
        correlation_kind="m1_audit",
        correlation_id="audit-h",
        audit_ref="audit-h",
        sealed_at_us=HYDRATED_US + 90_000_000,
    )
    return dataclasses.replace(base, **overrides)


def hydratable_event(
    event_id: str = "event-h", **overrides: object
) -> governed._GovernedProvenanceEvent:
    base = governed._GovernedProvenanceEvent(
        workspace_id=WORKSPACE_ID,
        assembly_id="assembly-h",
        governed_record_version_id="version-h",
        provenance_event_id=event_id,
        provenance_sequence=1,
        action="governance.accepted",
        actor_id="reviewer-h",
        actor_kind="human",
        policy_id=None,
        occurred_at_us=EVENT_US,
        reason_code="governance.reviewed",
        reason_comment=None,
        evidence_disposition="available",
    )
    return dataclasses.replace(base, provenance_event_id=event_id, **overrides)


def hydratable_link(
    evidence_id: str = "evd-h", **overrides: object
) -> governed._GovernedEvidenceLink:
    base = governed._GovernedEvidenceLink(
        workspace_id=WORKSPACE_ID,
        assembly_id="assembly-h",
        governed_record_version_id="version-h",
        provenance_event_id="event-h",
        provenance_sequence=1,
        link_ordinal=1,
        evidence_id=evidence_id,
        source_kind="filesystem.archive",
        source_native_id=f"doc-{evidence_id}",
        source_locator="archive://doc.md",
        source_retrieved_at_us=ARTIFACT_US,
        ingested_at_us=ARTIFACT_US,
        event_at_us=ARTIFACT_US,
        observed_at_us=ARTIFACT_US,
        normalized_record_id="nrc-h",
        normalized_span_id="nsp-h",
        span_pointer="/body/0",
        span_start_offset=0,
        span_end_offset=10,
    )
    return dataclasses.replace(base, evidence_id=evidence_id, **overrides)


def hydration(
    versions: tuple[GovernedVersion, ...],
    *,
    supersessions: tuple[GovernedSupersession, ...] = (),
    events: tuple[governed._GovernedProvenanceEvent, ...] = (),
    links: tuple[governed._GovernedEvidenceLink, ...] = (),
) -> governed._GovernedHydrationSnapshot:
    """A frozen hydration snapshot built by hand, with no database anywhere near it.

    The pure mapping's whole contract is that this is all it needs, so the tests that state
    exactly which stored fact each wire field comes from are built here rather than seeded:
    a hand-built snapshot can hold four visibly different clocks and a malformed content
    column, neither of which the accepted writer would let a fixture produce.
    """
    return governed._GovernedHydrationSnapshot(
        versions=versions, supersessions=supersessions, events=events, links=links
    )


def hydrated(
    holder: m2.Owned, *, view: str | None = None, instant_us: int = LATE_US
) -> tuple[GovernedRecord, ...]:
    return read_governed_records(
        holder.connection,
        workspace_id=WORKSPACE_ID,
        resolution_instant_us=instant_us,
        view=view,
    )


def test_lane_c_30_a_selected_version_hydrates_to_its_exact_wire_document(
    owned: m2.Owned,
) -> None:
    """One sealed version, whole, as the document a peer on the other side actually reads.

    Asserted as one `to_wire()` mapping rather than field by field, because the failure this
    guards against is a field quietly *missing* or quietly *extra*, which per-field
    assertions cannot see. `assertion` and `extraction` are absent for Amendment 008's fourth
    rule -- both require an instant 0009 stores nowhere -- and absent here means the key is
    not in the mapping at all, which is the difference between "not stated" and "stated as
    nothing".

    The two required instants are keyed to the selected assembly's own `recorded_at_us`
    rather than written out, since which clock they come from is lane C 26's assertion, not
    this one's; `valid_from` is written out, because 0009's default open-ended window starts
    at the exact microsecond `-1` and a mapping that clamped it to the epoch would still
    render something plausible.

    The record's two provenance events both cite `evd-0001`, so `sources` collapsing to one
    entry is the deduplication the contract requires -- two declarations of one `(kind,
    source_id)` are refused outright -- while `history` keeps both citations, because an
    append-only account of two acts is not a duplicate of anything.

    Falsifier: emit `sources` per link and `validate_governed_record` refuses the record for
    duplicate sources; emit `evidence` as `()` where the event cited nothing and the wire
    grows a key stating an empty list where the rows state nothing at all.
    """
    seal_correction_recorded_early(
        owned, edge_recorded_at_us=BASE_US + 20, target_recorded_at_us=BASE_US + 30
    )
    (version,) = resolve_governed_versions(
        owned.connection, workspace_id=WORKSPACE_ID, resolution_instant_us=LATE_US
    )
    (record,) = hydrated(owned)

    recorded_at = governed._timestamp(version.recorded_at_us)
    citation = {
        "source": {
            "kind": "filesystem.archive",
            "source_id": "doc-1",
            "locator": "archive://doc.md",
            "retrieved_at": governed._timestamp(m2.BASE_US),
        },
        "span": {"pointer": "/body/0", "start_offset": 0, "end_offset": 10},
    }
    assert record.to_wire() == {
        "workspace_id": WORKSPACE_ID,
        "record_type": "knowledge.claim",
        "domain_scope": "product.core",
        "authority_level": "canonical",
        "reviewer": "reviewer-1",
        "content": {"statement": "A durable claim"},
        "provenance": {
            "identity": {
                "record_id": "record-1",
                "version": "version-late-target",
                "layer": "l2",
                "governance_state": "accepted",
                "currentness": "current",
                "supersedes": {
                    "record_id": "record-1",
                    "version": "version-accepted",
                    "reason": "governance.reviewed",
                },
            },
            "temporal": {
                "ingested_at": recorded_at,
                "recorded_at": recorded_at,
                "valid_from": "1969-12-31T23:59:59.999Z",
            },
            "history": [
                {
                    "actor_id": "reviewer-1",
                    "actor_kind": "human",
                    "action": action,
                    "occurred_at": governed._timestamp(BASE_US + sequence),
                    "reason_code": "governance.reviewed",
                    "evidence": [citation],
                }
                for action, sequence in (
                    ("governance.corrected", 1),
                    ("record.superseded", 2),
                )
            ],
            "evidence_disposition": "available",
            "sources": [citation["source"]],
        },
    }
    validate_governed_record(record)
    assert GovernedRecord.from_wire(record.to_wire()) == record


def test_lane_c_31_all_three_views_hydrate_and_validate_their_own_selection(
    owned: m2.Owned,
) -> None:
    """Each view's versions become contract-valid records carrying that view's semantics.

    One workspace, three questions, and the answers differ in exactly the places the stored
    facts differ: the candidate layer is `l1`, in the `candidate` governance state, and
    carries no reviewer, because 0009 forbids the candidate half of the table a disposition
    or a `decision_source_id` at all; the governed layer is `l2`, `accepted`, `canonical`
    and names the exact reviewer it was accepted by. Currentness is not asked of the view --
    it is read off the sealed edges -- so `history` is `superseded` and the other two are
    `current` without the mapping ever being told which question it is answering.

    The candidate record is put through the *search projection's* own semantic validator
    rather than only the generic record one, because that is the check the state has to
    survive: `validate_memory_search_result` admits exactly `l1`/`candidate`/`current` under
    the candidates view, and `candidates` is authorized alone so the view string cannot
    widen its own trust. The generic `validate_governed_record` accepts `proposed` here
    quite happily, which is exactly why it cannot be the only assertion.

    Falsifier: map an absent `governance_disposition` to `proposed` -- `memory.create`'s
    state, not this view's -- and `validate_memory_search_result` refuses the record as not
    an exact l1/candidate/current one, while every generic-record assertion still passes.
    Map it to `accepted` and the candidate is refused for a layer/governance contradiction;
    take `currentness` from the view name and `candidates` still passes while a superseded
    candidate would silently read as live.
    """
    seed_chain(owned)
    for view, expected in (
        ("candidates", ("version-1", "l1", "candidate", "proposed", None, "current")),
        (
            None,
            ("version-corrected-next", "l2", "accepted", "canonical", "reviewer-1", "current"),
        ),
    ):
        (record,) = hydrated(owned, view=view)
        identity = record.provenance.identity
        assert (
            identity.version,
            identity.layer,
            identity.governance_state,
            record.authority_level,
            record.reviewer,
            identity.currentness,
        ) == expected
        validate_governed_record(record)

    (candidate,) = hydrated(owned, view="candidates")
    validate_memory_search_result(
        MemorySearchResult(records=(candidate,), page=PageMetadata()),
        MemorySearchInput(query="candidate frontier", view="candidates"),
        WORKSPACE_ID,
        governed._timestamp(LATE_US),
        {"candidates"},
    )

    history = hydrated(owned, view="history")
    assert tuple(r.provenance.identity.version for r in history) == (
        "version-accepted",
        "version-corrected",
    )
    for record in history:
        assert record.provenance.identity.currentness == "superseded"
        assert record.provenance.identity.layer == "l2"
        validate_governed_record(record)


def test_lane_c_32_one_assembly_maps_identically_however_it_was_selected(
    owned: m2.Owned,
) -> None:
    """The same version, current at one instant and history at another, keeps its temporal.

    Amendment 008 says supersession selection changes which assembly is selected and nothing
    about the temporal mapping of that assembly. `version-accepted` is the current canonical
    answer at BASE+25 and history at BASE+30, so every instant that is a fact about the
    version itself must be byte-identical across the two; the only field that may differ is
    `superseded_at`, which is a fact about an edge rather than about this version.

    Falsifier: derive `ingested_at` or `recorded_at` from anything the view changes -- the
    resolution instant, the supersession's effective instant, the replacing assembly's own
    clock -- and the two renderings stop agreeing.
    """
    seal_correction_recorded_early(
        owned, edge_recorded_at_us=BASE_US + 20, target_recorded_at_us=BASE_US + 30
    )
    (live,) = hydrated(owned, instant_us=BASE_US + 25)
    (retired,) = hydrated(owned, view="history", instant_us=BASE_US + 30)

    assert live.provenance.identity.version == retired.provenance.identity.version
    assert dataclasses.replace(
        retired.provenance.temporal, superseded_at=None
    ) == live.provenance.temporal
    assert live.provenance.temporal.superseded_at is None
    assert retired.provenance.temporal.superseded_at is not None


def test_lane_c_33_no_evidence_or_event_clock_can_move_the_records_instants() -> None:
    """The record's instants come from its assembly, whatever the rows around it say.

    Four clocks are in reach here and three of them are somebody else's: the artifact's
    `ingested_at_us`, `event_at_us` and `observed_at_us`, and the provenance event's
    `occurred_at_us`. Amendment 008 forbids all of them for the governed record's own
    temporal metadata -- a citation added later must not move `ingested_at` -- and the
    fixture puts each in a different second so a mapping that took one renders a visibly
    different string.

    `event_at` and `observed_at` stay absent for the same reason: the artifact has them, the
    governed record does not, and copying them across would publish an account of the world
    behind *this* claim that no row about this claim asserts. `assertion`/`extraction` are
    absent because their required `asserted_at`/`extracted_at` have no durable column.

    Falsifier: move any of the three artifact clocks or the event clock and re-run -- every
    assertion below is unchanged only if none of them is an input.
    """
    version = hydratable()
    (record,) = governed._hydrate_governed_records(
        hydration(
            (version,),
            events=(hydratable_event(),),
            links=(hydratable_link(),),
        )
    )
    # The same version, described by rows whose every clock has moved a year. Only the
    # support rows differ between the two snapshots, so any difference in the temporal
    # metadata below is a support row's clock leaking into it.
    (shifted,) = governed._hydrate_governed_records(
        hydration(
            (version,),
            events=(hydratable_event(occurred_at_us=EVENT_US - 31_536_000_000_000),),
            links=(
                hydratable_link(
                    source_retrieved_at_us=ARTIFACT_US - 31_536_000_000_000,
                    ingested_at_us=ARTIFACT_US - 31_536_000_000_000,
                    event_at_us=ARTIFACT_US - 31_536_000_000_000,
                    observed_at_us=ARTIFACT_US - 31_536_000_000_000,
                ),
            ),
        )
    )

    recorded_at = governed._timestamp(HYDRATED_US)
    assert record.provenance.temporal == RecordTemporalMetadata(
        ingested_at=recorded_at,
        recorded_at=recorded_at,
        valid_from=governed._timestamp(HYDRATED_US - 60_000_000),
    )
    assert record.provenance.temporal == shifted.provenance.temporal
    assert record.provenance.assertion is None
    assert record.provenance.extraction is None
    assert "assertion" not in record.provenance.to_wire()
    assert "extraction" not in record.provenance.to_wire()
    # The artifact's own clocks did reach the snapshot; they simply are not this record's.
    assert record.provenance.sources[0].retrieved_at == governed._timestamp(ARTIFACT_US)
    assert record.provenance.history[0].occurred_at == governed._timestamp(EVENT_US)


def test_lane_c_34_one_edge_is_read_reciprocally_from_both_of_its_ends() -> None:
    """A single supersession makes the source `superseded_by` the target and vice versa.

    One edge, two versions, and each must name the *other* -- pointing a version at itself,
    or pointing both fields at the same end, is the failure that makes a correction chain
    unwalkable in one direction while looking perfectly well-formed in the other. The reason
    the edge recorded is preserved on both halves, and `superseded_at` is the edge's
    effective instant exactly, not the replacing version's `recorded_at_us`, which the
    fixture deliberately puts in a different second.

    A current version must carry no `superseded_at` at all: the contract refuses the
    combination outright, so a mapping that stamped one on the live version would be caught
    by validation rather than by this assertion -- which is why the *absence* is asserted
    here and the refusal is left to `validate_governed_record`.

    Falsifier: swap the two directions and the retired version claims to supersede its own
    replacement; take `superseded_at` from the target's `recorded_at_us` and it renders half
    a second early.
    """
    retired = hydratable(
        assembly_id="assembly-old",
        governed_record_version_id="version-old",
        recorded_at_us=HYDRATED_US,
    )
    current = hydratable(
        assembly_id="assembly-new",
        governed_record_version_id="version-new",
        recorded_at_us=HYDRATED_US + 400_000_000,
    )
    edge = GovernedSupersession(
        workspace_id=WORKSPACE_ID,
        governed_record_id="record-h",
        source_version_id="version-old",
        target_version_id="version-new",
        assembly_id="assembly-new",
        effective_at_us=EFFECTIVE_US,
        reason_code="governance.reviewed",
    )
    old, new = governed._hydrate_governed_records(
        hydration(
            (retired, current),
            supersessions=(edge,),
            events=tuple(
                hydratable_event(
                    f"event-{side}",
                    assembly_id=f"assembly-{side}",
                    governed_record_version_id=f"version-{side}",
                )
                for side in ("old", "new")
            ),
            links=tuple(
                hydratable_link(
                    f"evd-{side}",
                    assembly_id=f"assembly-{side}",
                    governed_record_version_id=f"version-{side}",
                    provenance_event_id=f"event-{side}",
                )
                for side in ("old", "new")
            ),
        )
    )

    assert old.provenance.identity.currentness == "superseded"
    assert old.provenance.identity.superseded_by == SupersessionReference(
        record_id="record-h", version="version-new", reason="governance.reviewed"
    )
    assert old.provenance.identity.supersedes is None
    assert old.provenance.temporal.superseded_at == governed._timestamp(EFFECTIVE_US)
    assert old.provenance.temporal.superseded_at != governed._timestamp(
        current.recorded_at_us
    )

    assert new.provenance.identity.currentness == "current"
    assert new.provenance.identity.supersedes == SupersessionReference(
        record_id="record-h", version="version-old", reason="governance.reviewed"
    )
    assert new.provenance.identity.superseded_by is None
    assert new.provenance.temporal.superseded_at is None


def test_lane_c_35_support_rows_are_grouped_by_the_version_they_name() -> None:
    """Two records in one snapshot each get their own history and sources, in order.

    Both versions sit in one assembly and their support rows are interleaved on purpose, so
    a mapping that walked the tuples positionally, or that grouped on `assembly_id` alone,
    would hand a record the other's account of itself. `version-a` carries two acts citing
    one artifact twice and `version-shared` one act citing a different artifact, so the
    per-version ordering and the source deduplication are both observable at once.

    Falsifier: group on `assembly_id` alone and `version-a`'s history gains
    `version-shared`'s event and `version-shared`'s source; walk the events in any order but
    the snapshot's and the two acts below trade places.
    """
    first = hydratable(governed_record_version_id="version-a")
    second = hydratable(
        governed_record_id="record-b", governed_record_version_id="version-shared"
    )
    snapshot = hydration(
        (first, second),
        events=(
            hydratable_event("event-a1", governed_record_version_id="version-a"),
            hydratable_event(
                "event-b1",
                governed_record_version_id="version-shared",
                action="governance.corrected",
            ),
            hydratable_event(
                "event-a2",
                governed_record_version_id="version-a",
                provenance_sequence=2,
                action="record.superseded",
            ),
        ),
        links=(
            hydratable_link(
                "evd-1",
                governed_record_version_id="version-a",
                provenance_event_id="event-a1",
            ),
            hydratable_link(
                "evd-2",
                governed_record_version_id="version-shared",
                provenance_event_id="event-b1",
            ),
            hydratable_link(
                "evd-1",
                governed_record_version_id="version-a",
                provenance_event_id="event-a2",
            ),
        ),
    )
    records = governed._hydrate_governed_records(snapshot)

    (one, two) = records
    assert tuple(entry.action for entry in one.provenance.history) == (
        "governance.accepted",
        "record.superseded",
    )
    assert tuple(entry.action for entry in two.provenance.history) == (
        "governance.corrected",
    )
    assert tuple(s.source_id for s in one.provenance.sources) == ("doc-evd-1",)
    assert tuple(s.source_id for s in two.provenance.sources) == ("doc-evd-2",)
    assert governed._hydrate_governed_records(snapshot) == records


def test_lane_c_36_the_mapping_runs_entirely_off_the_frozen_snapshot(
    owned: m2.Owned,
) -> None:
    """Hydration succeeds after the connection it was read through is closed.

    The strongest available statement of "no post-freeze SQL and no lazy lookup": the
    snapshot is taken, the connection is closed, and the mapping still produces the whole
    record set. Any statement, cursor or deferred read left behind would raise
    `ProgrammingError` on the closed connection instead.

    Falsifier: have the mapping consult the database for one further fact -- a reviewer, a
    source, a reason -- and this raises rather than returning records.
    """
    seed_chain(owned)
    held = snapshot(owned, view="history")
    owned.connection.close()

    records = governed._hydrate_governed_records(held)
    assert tuple(r.provenance.identity.version for r in records) == (
        "version-accepted",
        "version-corrected",
    )
    with pytest.raises(sqlite3.ProgrammingError):
        owned.connection.execute("SELECT 1")


@pytest.mark.parametrize(
    "content_json", ("not json at all", '["a list is not a record"]', "null")
)
def test_lane_c_37_unreadable_content_is_refused_not_replaced(content_json: str) -> None:
    """A content column that will not decode to an object stops the record being published.

    0009 bounds `content_json` by bytes and forbids NULs but never calls SQLite's `json(...)`,
    so "this is a JSON object" is an invariant the write path asserts and the read path
    cannot assume. `repository.py` degrades an unreadable *artifact metadata* blob to `{}`
    because that field is opaque decoration; the same degradation here would publish a
    governed claim as though it stated nothing, which is why this refuses instead.

    Falsifier: fall back to `{}` and all three rows below return a well-formed, citable,
    empty governed record.
    """
    with pytest.raises(ValueError, match="content_json"):
        governed._hydrate_governed_records(
            hydration((hydratable(content_json=content_json),))
        )


@pytest.mark.parametrize(
    ("overrides", "cited", "error"),
    (
        ({"decision_source_id": None}, True, ContractSemanticError),
        ({"governance_disposition": "rejected"}, True, ContractSemanticError),
        ({"layer": "context_model"}, True, ValueError),
        ({"governance_disposition": None}, True, ValueError),
        ({}, False, ContractSemanticError),
    ),
)
def test_lane_c_38_an_incoherent_stored_state_refuses_rather_than_repairs(
    overrides: dict[str, object], cited: bool, error: type[Exception]
) -> None:
    """Five stored shapes that cannot be published, each refused rather than patched up.

    A canonical version with no `decision_source_id` has no reviewer, and the contract says
    canonical authority asserts a decision somebody made; supplying the assembly's asserting
    actor, or the accepting event's, would be this module inventing the reviewer. A
    `rejected` disposition carrying `canonical` authority asserts two opposite decisions
    about one version. A `context_model` layer has no authoritative wire layer -- 0009
    refuses it a seal, so it cannot legitimately be here at all. A governed version with no
    disposition is missing the one column its half of the table requires. And `available`
    evidence with no cited source claims evidence the snapshot does not contain.

    Every one of these is a row the accepted writer's own CHECKs forbid, which is exactly why
    they are built here rather than seeded: the point is that this module does not depend on
    those CHECKs still holding, and does not quietly repair a row that slipped past them.

    Falsifier: default the reviewer to the assembly's asserting actor, or coerce an unknown
    layer to `l2`, and the corresponding row publishes a record no stored fact supports.
    """
    with pytest.raises(error):
        governed._hydrate_governed_records(
            hydration(
                (hydratable(**overrides),),
                events=(hydratable_event(),),
                links=(hydratable_link(),) if cited else (),
            )
        )


def policy_governed(**overrides: object) -> GovernedVersion:
    """A governed version whose decision source is a policy evaluator, not a person.

    0009's assembly trigger requires exactly this pairing: a `policy_evaluator` assembly's
    provenance events carry `policy_id = decision_source_id` and no actor columns at all.
    """
    return hydratable(
        **{
            "decision_source_kind": "policy_evaluator",
            "decision_source_id": "policy-h",
            **overrides,
        }
    )


def policy_event(**overrides: object) -> governed._GovernedProvenanceEvent:
    """The event shape 0009 permits on a policy-evaluated assembly: policy, no actor."""
    return hydratable_event(
        **{"actor_id": None, "actor_kind": None, "policy_id": "policy-h", **overrides}
    )


def test_lane_c_39_a_policy_event_publishes_the_versions_own_decision_source() -> None:
    """A policy act's actor is its exact `policy_id`, under the version's exact stored kind.

    Both halves are read, never synthesised. `actor_id` is the event's own `policy_id`, and
    `actor_kind` is the *version's* `decision_source_kind` -- the only column in the
    assembly that records what kind of decision source governed it. 0009 ties the two
    together (`e.policy_id IS NOT a.decision_source_id` aborts the seal), so publishing the
    stored kind is reading that tie rather than assuming it.

    Falsifier: hard-code `policy_evaluator` as a module constant and this test still passes
    while lane C 40's mismatched rows publish an actor kind no row asserts -- which is the
    whole reason the kind is taken from the version and the tie is checked.
    """
    version = policy_governed()
    (record,) = governed._hydrate_governed_records(
        hydration(
            (version,), events=(policy_event(),), links=(hydratable_link(),)
        )
    )

    (entry,) = record.provenance.history
    assert entry.actor_id == "policy-h" == version.decision_source_id
    assert entry.actor_kind == "policy_evaluator" == version.decision_source_kind
    assert record.reviewer == "policy-h"
    validate_governed_record(record)


@pytest.mark.parametrize(
    ("version_overrides", "event_overrides"),
    (
        # The version says a person decided this, the event says a policy did.
        ({"decision_source_kind": "human_reviewer"}, {}),
        # The version records no decision source kind at all to take one from.
        ({"decision_source_kind": None}, {}),
        # Right kind, different policy: this event is not this version's decision.
        ({}, {"policy_id": "policy-elsewhere"}),
        # Actor-shaped but internally incomplete, in both directions.
        ({}, {"actor_id": "reviewer-h", "actor_kind": None, "policy_id": None}),
        ({}, {"actor_id": None, "actor_kind": "human", "policy_id": None}),
        # Neither shape at all.
        ({}, {"actor_id": None, "actor_kind": None, "policy_id": None}),
    ),
)
def test_lane_c_40_an_actor_that_is_not_an_exact_stored_fact_is_refused(
    version_overrides: dict[str, object], event_overrides: dict[str, object]
) -> None:
    """Six rows from which no exact actor identity follows, each refused rather than filled.

    A `ProvenanceEntry` requires both an `actor_id` and an `actor_kind`, and every way of
    producing one here that is not a stored fact is a lie about who acted: taking the kind
    from a constant when the version says `human_reviewer`, taking it from a version that
    records no decision source, accepting a `policy_id` that is not the one the version was
    governed by, or completing half an actor identity from the other half. 0009's own
    trigger forbids every one of these, which is why they are built here rather than seeded
    -- this module must not depend on that trigger still holding.

    Falsifier: publish a synthesized `policy_evaluator` for any event lacking an `actor_id`
    and the first three rows below produce a well-formed, citable record attributing a
    governed decision to a policy source the assembly never named.
    """
    with pytest.raises(ValueError, match="provenance event"):
        governed._hydrate_governed_records(
            hydration(
                (policy_governed(**version_overrides),),
                events=(policy_event(**event_overrides),),
                links=(hydratable_link(),),
            )
        )


def test_lane_c_41_an_identical_repeated_source_declaration_deduplicates() -> None:
    """Two events citing one artifact identically state one source, and keep both citations.

    The contract refuses a `sources` list declaring one `(kind, source_id)` twice, and two
    acts resting on the same artifact are an ordinary append-only history -- so the repeat
    collapses to a single entry while `history` keeps both citations, which are two facts
    rather than a duplicate of one.

    Falsifier: emit `sources` per link and `validate_governed_record` refuses the record for
    duplicate sources; drop a citation from `history` and an act loses the evidence it named.
    """
    (record,) = governed._hydrate_governed_records(
        hydration(
            (hydratable(),),
            events=(
                hydratable_event("event-1"),
                hydratable_event("event-2", provenance_sequence=2, action="record.superseded"),
            ),
            links=(
                hydratable_link("evd-1", provenance_event_id="event-1"),
                hydratable_link("evd-1", provenance_event_id="event-2"),
            ),
        )
    )

    assert tuple(
        (source.kind, source.source_id) for source in record.provenance.sources
    ) == (("filesystem.archive", "doc-evd-1"),)
    assert tuple(len(entry.evidence or ()) for entry in record.provenance.history) == (1, 1)
    validate_governed_record(record)


@pytest.mark.parametrize(
    "conflict",
    (
        {"source_locator": "archive://elsewhere.md"},
        # A whole millisecond, since the rendered `Timestamp` truncates below one.
        {"source_retrieved_at_us": ARTIFACT_US + 1_000},
    ),
)
def test_lane_c_42_a_conflicting_duplicate_source_is_refused_not_collapsed(
    conflict: dict[str, object],
) -> None:
    """One `(kind, source_id)` declared twice with different facts stops the record.

    Deduplication is only sound when the two declarations say the same thing. Two rows
    disagreeing on where the source was read from, or on when it was retrieved, are two
    accounts of one citation, and keeping whichever the link reader happened to order first
    would publish one of them as the record's only answer -- indistinguishable, downstream,
    from an uncontested one.

    Falsifier: keep the first declaration and both rows below return a record whose single
    `sources` entry silently drops a conflicting stored fact.
    """
    with pytest.raises(ValueError, match="different facts"):
        governed._hydrate_governed_records(
            hydration(
                (hydratable(),),
                events=(hydratable_event(),),
                links=(
                    hydratable_link("evd-1"),
                    hydratable_link("evd-1", **conflict),
                ),
            )
        )
