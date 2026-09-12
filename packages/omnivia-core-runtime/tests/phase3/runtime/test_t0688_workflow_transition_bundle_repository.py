"""T-0688 IP-07 acceptance for the Workflow transition-bundle repository.

The 0035 migration tests hold the schema to what SQL can enforce, and the IP-06 tests
hold the binding repository to what SQL cannot. These hold the transition writer to the
three things SQL cannot express at all.

*The genesis link is derived, not stored.* One declared preimage, one digest, and a
different value for every Run. The vectors below are literal: a change to the separator,
to the canonical spelling or to the digest convention has to break them.

*A transition is one write, fenced by the revision it expected, and closed by stage.*
`R0` and any unrecognised stage record nothing at all. `R1` and `R2` record the bundle
and the single journal event it carries as one pair, and an injected failure on either
insert leaves neither half. A repeat of the same bundle replays; a different body under
the same identifier, a stale or future revision, a wrong link, a missing predecessor and
a caller naming another Run's binding all refuse with no mutation, each under a
diagnostic a caller reads instead of a sentence.

*The record is the bytes, on both sides of the pair.* Bundle, event and event payload are
each stored as the canonical document their digest addresses, and every read recomputes
all of it -- the bytes, the addresses, the public contracts, the indexed columns, the
binding and bundle relations, and the whole integrity chain from genesis -- so a file
edited outside this database fails closed rather than returning something that parses.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import test_application_audit_idempotency_migration as m1
import test_rt102_agent_runtime_migration as m18
import test_t0688_workflow_runtime_hardening_repository as ip06
import test_workflow_runs_migration as m27
from omnivia_core_runtime.ownership.fencing import StaleGeneration
from omnivia_core_runtime.storage.backup import (
    InstallationLayout,
    create_verified_backup,
    new_attempt_id,
    restore_backup,
)
from omnivia_core_runtime.storage.connection import (
    OpenMode,
    StorageError,
    open_database,
)
from omnivia_core_runtime.storage.migrations import materialise_phase0_baseline
from omnivia_core_runtime.storage.workflow_runtime_hardening import (
    StoredRuntimeDefinitionBinding,
    TransitionBundleRefused,
    apply_transition_bundle,
    journal_genesis_link,
    read_runtime_journal_events,
    read_transition_bundle,
    transaction_local_transition_writer,
    workflow_transition_writer,
)

from omnivia_core.contracts.v1.canonical_json import canonicalize
from omnivia_core.contracts.v1.semantics_workflow import (
    compute_transition_bundle_payload_digest,
    validate_runtime_journal_event,
    validate_transition_bundle,
)

WORKSPACE_ID = ip06.WORKSPACE_ID
OTHER_WORKSPACE_ID = m18.OTHER_WORKSPACE_ID
RUN_ID = ip06.RUN_ID
BINDING_ID = ip06.BINDING_ID
BUNDLES = "omnivia_workflow_transition_bundles"
JOURNAL = "omnivia_workflow_runtime_journal_events"

#: Later than the binding's own instant, which 0035 requires of every bundle.
RECORDED_BASE_US = ip06.BOUND_AT_US + 1_000

EVENT_KIND = "runtime.aggregate.advanced"

#: The declared sequence-zero preimage, written out rather than derived, and the digest
#: of exactly those UTF-8 bytes. Two Runs, so "different per Run" is a measured fact and
#: not a claim about an implementation that could hash a constant for every Run alike.
ALPHA_RUN = "run-ip07-alpha"
BETA_RUN = "run-ip07-beta"
ALPHA_PREIMAGE = (
    '{"domainSeparator":"omnivia.workflow-runtime.journal.genesis.v1",'
    '"runId":"run-ip07-alpha"}'
)
BETA_PREIMAGE = (
    '{"domainSeparator":"omnivia.workflow-runtime.journal.genesis.v1",'
    '"runId":"run-ip07-beta"}'
)
ALPHA_GENESIS = (
    "sha256:e04a4c2294d614fec95afda8062daa6f568dd7131eb01f14dd81c5a8ec04c6d9"
)
BETA_GENESIS = "sha256:6acdc86b0171cf43f54a98731ad8b77126853f86266e82907f3b3139e474be10"

#: A well-formed digest that is not any link this Run may name.
WRONG_LINK = "sha256:" + "1" * 64


def payload(sequence: int = 0, **overrides: Any) -> dict[str, Any]:
    """The mapping whose canonical digest one journal event's `payloadDigest` names."""
    document: dict[str, Any] = {
        "runId": RUN_ID,
        "sequence": sequence,
        "outcome": "advanced",
    }
    document.update(overrides)
    return document


def journal_event(
    sequence: int = 0, link: str | None = None, **overrides: Any
) -> dict[str, Any]:
    document: dict[str, Any] = {
        "eventId": f"event-ip07-{sequence}",
        "runId": RUN_ID,
        "sequence": sequence,
        "previousIntegrityLink": link or journal_genesis_link(RUN_ID),
        "eventKind": EVENT_KIND,
        "recordedAt": ip06.instant(RECORDED_BASE_US + sequence),
        "payloadDigest": ip06.digest_of(canonicalize(payload(sequence))),
    }
    document.update(overrides)
    return document


def bundle(
    sequence: int = 0,
    link: str | None = None,
    event: dict[str, Any] | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    """A complete `RuntimeTransitionBundle` advancing ip06's bound run by one revision."""
    document: dict[str, Any] = {
        "bundleSchemaVersion": "1.0.0",
        "bundleId": f"bundle-ip07-{sequence}",
        "runId": RUN_ID,
        "expectedAggregateRevision": sequence,
        "event": event if event is not None else journal_event(sequence, link),
        "producedAggregateRevision": sequence + 1,
    }
    document.update(overrides)
    document["payloadDigest"] = compute_transition_bundle_payload_digest(document)
    return document


def link_after(sequence: int = 0) -> str:
    """The link the event at `sequence + 1` must name: its predecessor's whole document."""
    return ip06.digest_of(canonicalize(journal_event(sequence)))


@pytest.fixture
def owned(tmp_path: Path) -> Iterator[m1.Owned]:
    path = tmp_path / "workspace.sqlite"
    materialise_phase0_baseline(path)
    m1.bootstrap_and_migrate(path, workspace_id=WORKSPACE_ID)
    holder = m1.take_ownership(path)
    yield holder
    holder.connection.close()


@pytest.fixture
def bound(owned: m1.Owned) -> StoredRuntimeDefinitionBinding:
    """ip06's seeded plan and run, with the verified binding a bundle is applied against."""
    ip06.prepare(owned)
    return ip06.admit(owned)


def apply(
    holder: m1.Owned,
    binding: StoredRuntimeDefinitionBinding,
    document: dict[str, Any] | None = None,
    payload_map: dict[str, Any] | None = None,
    *,
    stage: str = "R2",
    generation: int | None = None,
) -> Any:
    return apply_transition_bundle(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation if generation is None else generation,
        binding=binding,
        bundle=document if document is not None else bundle(),
        event_payload=payload_map if payload_map is not None else payload(),
        rollout_stage=stage,
    )


def counts(connection: sqlite3.Connection) -> tuple[int, int]:
    return tuple(  # type: ignore[return-value]
        int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in (BUNDLES, JOURNAL)
    )


class FailingConnection:
    """The owner's connection, with one INSERT it refuses to issue.

    The pair is written by two statements, and which of them is second is an
    implementation choice 0035's deferred foreign keys deliberately leave open. Failing
    either one proves the same property from both sides: no half pair survives.
    """

    def __init__(self, connection: sqlite3.Connection, table: str) -> None:
        self._connection = connection
        self._table = table

    def execute(self, statement: str, parameters: Any = ()) -> Any:
        if statement.startswith(f"INSERT INTO {self._table} "):
            raise sqlite3.OperationalError(f"injected failure writing {self._table}")
        return self._connection.execute(statement, parameters)


# --- the genesis link ----------------------------------------------------------------


def test_the_genesis_link_is_the_declared_preimage_and_nothing_else() -> None:
    """Fixed vectors: the separator, the canonical bytes and the digest convention."""
    assert (
        canonicalize(
            {
                "domainSeparator": "omnivia.workflow-runtime.journal.genesis.v1",
                "runId": ALPHA_RUN,
            }
        )
        == ALPHA_PREIMAGE
    )
    assert ip06.digest_of(ALPHA_PREIMAGE) == ALPHA_GENESIS
    assert ip06.digest_of(BETA_PREIMAGE) == BETA_GENESIS
    assert journal_genesis_link(ALPHA_RUN) == ALPHA_GENESIS
    assert journal_genesis_link(BETA_RUN) == BETA_GENESIS


def test_every_run_has_its_own_genesis_link() -> None:
    """A link shared between Runs would let one Run's first event open another's chain."""
    links = {journal_genesis_link(run) for run in (ALPHA_RUN, BETA_RUN, RUN_ID)}
    assert len(links) == 3
    assert all(len(link) == 71 and link.startswith("sha256:") for link in links)


# --- the rollout gate ----------------------------------------------------------------


@pytest.mark.parametrize("stage", ["R0", "R3", "r2", "", "R2 "])
def test_a_stage_that_enables_nothing_records_nothing(
    owned: m1.Owned, bound: StoredRuntimeDefinitionBinding, stage: str
) -> None:
    """Fail-safe and closed: the disabled stage and every unrecognised one alike."""
    with pytest.raises(TransitionBundleRefused) as refusal:
        apply(owned, bound, stage=stage)
    assert refusal.value.diagnostic == "RT_BUNDLE_WRITER_DISABLED"
    assert counts(owned.connection) == (0, 0)


@pytest.mark.parametrize("stage", ["R1", "R2"])
def test_both_recording_stages_record_the_pair(
    owned: m1.Owned, bound: StoredRuntimeDefinitionBinding, stage: str
) -> None:
    """`R1` is the dual-write candidate and `R2` is authoritative; both write here."""
    outcome = apply(owned, bound, stage=stage)
    assert outcome.disposition == "applied"
    assert counts(owned.connection) == (1, 1)


# --- one atomic transition -----------------------------------------------------------


def test_a_bundle_and_its_event_are_one_write(
    owned: m1.Owned, bound: StoredRuntimeDefinitionBinding
) -> None:
    """Both halves durable, both readable, and both exactly the bytes they address."""
    document = bundle()
    outcome = apply(owned, bound, document)
    body = canonicalize(document)
    assert outcome.disposition == "applied"
    assert outcome.bundle_id == "bundle-ip07-0"
    assert outcome.produced_revision == 1
    assert outcome.payload_digest == document["payloadDigest"]
    assert outcome.content_address == ip06.digest_of(body)

    stored = read_transition_bundle(
        owned.connection,
        workspace_id=WORKSPACE_ID,
        run_id=RUN_ID,
        bundle_id="bundle-ip07-0",
    )
    assert stored is not None
    assert stored.bundle == document
    assert stored.content_address == ip06.digest_of(body)
    assert stored.content_length_bytes == len(body.encode("utf-8"))
    assert (stored.workspace_id, stored.run_id) == (WORKSPACE_ID, RUN_ID)
    assert (stored.binding_id, stored.produced_revision) == (BINDING_ID, 1)
    validate_transition_bundle(stored.bundle)

    events = read_runtime_journal_events(
        owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
    )
    assert len(events) == 1
    assert events[0].event == journal_event()
    assert events[0].payload == payload()
    assert events[0].bundle_id == "bundle-ip07-0"
    assert events[0].event["previousIntegrityLink"] == journal_genesis_link(RUN_ID)
    validate_runtime_journal_event(events[0].event, run_id=RUN_ID)


def test_the_pair_is_visible_inside_the_transaction_that_recorded_it(
    owned: m1.Owned, bound: StoredRuntimeDefinitionBinding
) -> None:
    """The composition seam: a caller may read what it just wrote, before commit."""
    with workflow_transition_writer(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
    ) as writer:
        outcome = writer.apply_transition_bundle(
            binding=bound,
            bundle=bundle(),
            event_payload=payload(),
            rollout_stage="R2",
        )
        assert outcome.disposition == "applied"
        assert counts(owned.connection) == (1, 1)
        assert (
            read_transition_bundle(
                owned.connection,
                workspace_id=WORKSPACE_ID,
                run_id=RUN_ID,
                bundle_id="bundle-ip07-0",
            )
            is not None
        )
    assert counts(owned.connection) == (1, 1)


@pytest.mark.parametrize("table", [BUNDLES, JOURNAL], ids=["bundle", "event"])
def test_an_injected_insert_failure_leaves_no_half_pair(
    owned: m1.Owned, bound: StoredRuntimeDefinitionBinding, table: str
) -> None:
    with pytest.raises(sqlite3.OperationalError), m27.guarded(owned):
        transaction_local_transition_writer(
            FailingConnection(owned.connection, table),  # type: ignore[arg-type]
            workspace_id=WORKSPACE_ID,
        ).apply_transition_bundle(
            binding=bound,
            bundle=bundle(),
            event_payload=payload(),
            rollout_stage="R2",
        )
    assert counts(owned.connection) == (0, 0)


def test_a_second_transition_extends_the_chain_from_its_predecessor(
    owned: m1.Owned, bound: StoredRuntimeDefinitionBinding
) -> None:
    apply(owned, bound)
    outcome = apply(owned, bound, bundle(1, link=link_after(0)), payload(1))
    assert (outcome.disposition, outcome.produced_revision) == ("applied", 2)

    events = read_runtime_journal_events(
        owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
    )
    assert [event.event["sequence"] for event in events] == [0, 1]
    assert events[0].event["previousIntegrityLink"] == journal_genesis_link(RUN_ID)
    assert events[1].event["previousIntegrityLink"] == events[0].content_address
    assert counts(owned.connection) == (2, 2)


# --- idempotency and revision ordering ------------------------------------------------


def test_the_same_bundle_replays_without_a_second_event(
    owned: m1.Owned, bound: StoredRuntimeDefinitionBinding
) -> None:
    """Same identifier, same public payload digest: the recorded revision, no new write."""
    first = apply(owned, bound)
    again = apply(owned, bound)
    assert again.disposition == "replayed"
    assert again.produced_revision == first.produced_revision == 1
    assert (again.payload_digest, again.content_address) == (
        first.payload_digest,
        first.content_address,
    )
    assert counts(owned.connection) == (1, 1)


@pytest.mark.parametrize(
    "other",
    [
        bundle(0, event=journal_event(0, eventId="event-ip07-restated")),
        bundle(1, link=link_after(0), bundleId="bundle-ip07-0"),
    ],
    ids=["a different event", "a different expected revision"],
)
def test_a_replay_is_decided_on_the_stored_bundle_not_an_indexed_digest(
    owned: m1.Owned, bound: StoredRuntimeDefinitionBinding, other: dict[str, Any]
) -> None:
    """The write side believes the bytes, exactly as the read side does.

    `payload_digest` is an indexed column, and a file edited outside this database can
    hold whatever it likes there. Deciding a replay on it lets a genuinely different
    transition -- a different nested event, a different expected revision -- arrive under
    a recorded identifier and be answered with the recorded revision and no write at all,
    which is a transition silently lost rather than a replay.
    """
    first = apply(owned, bound)
    assert other["bundleId"] == "bundle-ip07-0"

    connection = ip06.corrupt(
        owned,
        f"UPDATE {BUNDLES} SET payload_digest = ?",
        other["payloadDigest"],
        table=BUNDLES,
        operation="update",
    )
    connection.close()
    holder = m1.take_ownership(owned.path)
    try:
        with pytest.raises(TransitionBundleRefused) as refusal:
            apply(holder, bound, other, payload(int(str(other["event"]["sequence"]))))
        assert refusal.value.diagnostic == "RT_BUNDLE_INTEGRITY_CONFLICT"
        assert counts(holder.connection) == (1, 1)
        assert (
            holder.connection.execute(
                f"SELECT bundle_json FROM {BUNDLES}"
            ).fetchone()[0]
            == canonicalize(bundle())
        )
        assert first.disposition == "applied"
    finally:
        holder.connection.close()


def test_the_same_identifier_over_a_different_body_is_a_conflict(
    owned: m1.Owned, bound: StoredRuntimeDefinitionBinding
) -> None:
    apply(owned, bound)
    other = bundle(evidenceRefs=[{"evidenceRef": "evidence-ip07-second"}])
    assert other["bundleId"] == "bundle-ip07-0"
    assert other["payloadDigest"] != bundle()["payloadDigest"]

    with pytest.raises(TransitionBundleRefused) as refusal:
        apply(owned, bound, other)
    assert refusal.value.diagnostic == "RT_BUNDLE_INTEGRITY_CONFLICT"
    assert counts(owned.connection) == (1, 1)
    stored = read_transition_bundle(
        owned.connection,
        workspace_id=WORKSPACE_ID,
        run_id=RUN_ID,
        bundle_id="bundle-ip07-0",
    )
    assert stored is not None and stored.bundle == bundle()


@pytest.mark.parametrize("expected", [0, 2, 7], ids=["stale", "future", "far future"])
def test_a_revision_other_than_the_produced_head_is_a_conflict(
    owned: m1.Owned, bound: StoredRuntimeDefinitionBinding, expected: int
) -> None:
    """The first bundle a run produces expects zero; every later one expects the head."""
    apply(owned, bound)
    # A new identifier, so this is the revision check answering and not the replay one.
    stale = bundle(
        expected,
        event=journal_event(expected, link_after(0)),
        bundleId=f"bundle-ip07-retry-{expected}",
    )
    with pytest.raises(TransitionBundleRefused) as refusal:
        apply(owned, bound, stale, payload(expected))
    assert refusal.value.diagnostic == "RT_BUNDLE_REVISION_CONFLICT"
    assert counts(owned.connection) == (1, 1)

    assert (
        apply(owned, bound, bundle(1, link=link_after(0)), payload(1)).produced_revision
        == 2
    )


def test_the_first_bundle_a_run_produces_expects_a_revision_of_zero(
    owned: m1.Owned, bound: StoredRuntimeDefinitionBinding
) -> None:
    with pytest.raises(TransitionBundleRefused) as refusal:
        apply(owned, bound, bundle(1, link=link_after(0)), payload(1))
    assert refusal.value.diagnostic == "RT_BUNDLE_REVISION_CONFLICT"
    assert counts(owned.connection) == (0, 0)


# --- identity, payload and the integrity chain ----------------------------------------


def test_a_bundle_naming_another_run_is_refused(
    owned: m1.Owned, bound: StoredRuntimeDefinitionBinding
) -> None:
    """The binding is the identity; a `runId` beside it cannot redirect the write."""
    elsewhere = bundle(runId=ALPHA_RUN, event=journal_event(runId=ALPHA_RUN))
    with pytest.raises(StorageError, match="names a run other than"):
        apply(owned, bound, elsewhere)
    assert counts(owned.connection) == (0, 0)


def test_a_binding_read_for_another_workspace_cannot_be_applied_here(
    owned: m1.Owned, bound: StoredRuntimeDefinitionBinding
) -> None:
    elsewhere = replace(bound, workspace_id=OTHER_WORKSPACE_ID)
    with pytest.raises(StorageError, match="another workspace's binding"):
        apply(owned, elsewhere)
    assert counts(owned.connection) == (0, 0)


def test_a_payload_the_event_does_not_name_is_never_stored_under_its_digest(
    owned: m1.Owned, bound: StoredRuntimeDefinitionBinding
) -> None:
    with pytest.raises(StorageError, match="is not the digest of the payload supplied"):
        apply(owned, bound, bundle(), payload(outcome="reverted"))
    assert counts(owned.connection) == (0, 0)


def test_an_event_sequence_that_is_not_the_expected_revision_is_refused(
    owned: m1.Owned, bound: StoredRuntimeDefinitionBinding
) -> None:
    document = bundle(event=journal_event(3, journal_genesis_link(RUN_ID)))
    with pytest.raises(StorageError, match="is not the revision its bundle expected"):
        apply(owned, bound, document, payload(3))
    assert counts(owned.connection) == (0, 0)


@pytest.mark.parametrize(
    "link",
    [WRONG_LINK, ALPHA_GENESIS, "sha256:" + "0" * 64],
    ids=["arbitrary", "another run's genesis", "zeroes"],
)
def test_a_first_event_that_is_not_anchored_to_this_run_refuses(
    owned: m1.Owned, bound: StoredRuntimeDefinitionBinding, link: str
) -> None:
    with pytest.raises(TransitionBundleRefused) as refusal:
        apply(owned, bound, bundle(link=link))
    assert refusal.value.diagnostic == "RT_JOURNAL_INTEGRITY_FAILURE"
    assert counts(owned.connection) == (0, 0)


def test_a_later_event_that_does_not_name_its_predecessor_refuses(
    owned: m1.Owned, bound: StoredRuntimeDefinitionBinding
) -> None:
    """A link to genesis at sequence one would fork the chain rather than extend it."""
    apply(owned, bound)
    for link in (journal_genesis_link(RUN_ID), WRONG_LINK):
        with pytest.raises(TransitionBundleRefused) as refusal:
            apply(owned, bound, bundle(1, link=link), payload(1))
        assert refusal.value.diagnostic == "RT_JOURNAL_INTEGRITY_FAILURE"
    assert counts(owned.connection) == (1, 1)


def test_a_link_with_no_predecessor_to_name_refuses_before_any_insert(
    owned: m1.Owned, bound: StoredRuntimeDefinitionBinding
) -> None:
    """A produced head whose journal event is not there links to nothing, and says so.

    The bundle row alone is a state 0035 permits only mid-transaction, which is exactly
    where a writer that trusted the head instead of the chain would append onto it.
    """
    document = bundle()
    body = canonicalize(document)
    orphan = {
        "workspace_id": WORKSPACE_ID,
        "run_id": RUN_ID,
        "bundle_id": document["bundleId"],
        "binding_id": BINDING_ID,
        "expected_revision": 0,
        "produced_revision": 1,
        "payload_digest": document["payloadDigest"],
        "bundle_json": body,
        "bundle_digest": ip06.digest_of(body),
        "bundle_byte_length": len(body.encode("utf-8")),
        "recorded_at_us": RECORDED_BASE_US,
    }
    with pytest.raises(TransitionBundleRefused) as refusal, m27.guarded(owned):
        m27.insert(owned, BUNDLES, orphan)
        transaction_local_transition_writer(
            owned.connection, workspace_id=WORKSPACE_ID
        ).apply_transition_bundle(
            binding=bound,
            bundle=bundle(1, link=link_after(0)),
            event_payload=payload(1),
            rollout_stage="R2",
        )
    assert refusal.value.diagnostic == "RT_JOURNAL_INTEGRITY_FAILURE"
    assert counts(owned.connection) == (0, 0)


def test_stale_authority_refuses_without_mutation(
    owned: m1.Owned, bound: StoredRuntimeDefinitionBinding
) -> None:
    with pytest.raises(StaleGeneration):
        apply(owned, bound, generation=owned.generation + 1)
    assert counts(owned.connection) == (0, 0)


# --- reads: scope, absence and corruption --------------------------------------------


def test_no_reader_answers_for_a_workspace_it_was_not_asked_about(
    owned: m1.Owned, bound: StoredRuntimeDefinitionBinding
) -> None:
    apply(owned, bound)
    assert (
        read_transition_bundle(
            owned.connection,
            workspace_id=OTHER_WORKSPACE_ID,
            run_id=RUN_ID,
            bundle_id="bundle-ip07-0",
        )
        is None
    )
    assert (
        read_runtime_journal_events(
            owned.connection, workspace_id=OTHER_WORKSPACE_ID, run_id=RUN_ID
        )
        == ()
    )
    assert (
        read_transition_bundle(
            owned.connection,
            workspace_id=WORKSPACE_ID,
            run_id=RUN_ID,
            bundle_id="bundle-ip07-absent",
        )
        is None
    )
    assert (
        read_runtime_journal_events(
            owned.connection, workspace_id=WORKSPACE_ID, run_id="run-absent"
        )
        == ()
    )


def read_bundle(connection: sqlite3.Connection) -> Any:
    return read_transition_bundle(
        connection,
        workspace_id=WORKSPACE_ID,
        run_id=RUN_ID,
        bundle_id="bundle-ip07-0",
    )


def bundle_edits() -> dict[str, tuple[str, tuple[Any, ...], str]]:
    document = canonicalize(bundle())
    # Same length, so only the digest can tell.
    edited = document.replace("advanced", "advanceD")
    spaced = document.replace(",", ", ")
    foreign = canonicalize({"bundleId": "bundle-ip07-0"})
    return {
        "bytes": (
            f"UPDATE {BUNDLES} SET bundle_json = ?",
            (edited,),
            "does not match its recorded digest",
        ),
        "digest": (
            f"UPDATE {BUNDLES} SET bundle_digest = ?",
            ("sha256:" + "e" * 64,),
            "does not match its recorded digest",
        ),
        "byte length": (
            f"UPDATE {BUNDLES} SET bundle_byte_length = bundle_byte_length + 1",
            (),
            "does not match its recorded byte length",
        ),
        "canonical form": (
            (
                f"UPDATE {BUNDLES} SET bundle_json = ?, bundle_digest = ?, "
                "bundle_byte_length = ?"
            ),
            (spaced, ip06.digest_of(spaced), len(spaced.encode("utf-8"))),
            "is not canonical JSON",
        ),
        "contract": (
            (
                f"UPDATE {BUNDLES} SET bundle_json = ?, bundle_digest = ?, "
                "bundle_byte_length = ?"
            ),
            (foreign, ip06.digest_of(foreign), len(foreign.encode("utf-8"))),
            "is not a valid RuntimeTransitionBundle",
        ),
        "indexed revision": (
            f"UPDATE {BUNDLES} SET produced_revision = 9",
            (),
            "disagrees with the columns it is indexed by",
        ),
        "indexed payload digest": (
            f"UPDATE {BUNDLES} SET payload_digest = ?",
            ("sha256:" + "a" * 64,),
            "disagrees with the columns it is indexed by",
        ),
        "indexed instant": (
            f"UPDATE {BUNDLES} SET recorded_at_us = recorded_at_us + 1",
            (),
            "disagrees with the columns it is indexed by",
        ),
        "binding relation": (
            f"UPDATE {BUNDLES} SET binding_id = ?",
            ("binding-ip07-other",),
            "names a binding its run does not hold",
        ),
    }


@pytest.mark.parametrize("case", sorted(bundle_edits()))
def test_a_tampered_bundle_fails_closed(
    owned: m1.Owned, bound: StoredRuntimeDefinitionBinding, case: str
) -> None:
    apply(owned, bound)
    statement, parameters, expected = bundle_edits()[case]
    connection = ip06.corrupt(owned, statement, *parameters, table=BUNDLES)
    try:
        with pytest.raises(StorageError, match=expected):
            read_bundle(connection)
    finally:
        connection.close()


def journal_edits() -> dict[str, tuple[str, tuple[Any, ...], str]]:
    document = canonicalize(journal_event())
    body = canonicalize(payload())
    forged = canonicalize(journal_event(0, WRONG_LINK))
    return {
        "event bytes": (
            f"UPDATE {JOURNAL} SET event_json = ?",
            (document.replace("advanced", "advanceD"),),
            "a stored journal event does not match its recorded digest",
        ),
        "payload bytes": (
            f"UPDATE {JOURNAL} SET event_payload_json = ?",
            (body.replace("advanced", "advanceD"),),
            "a stored journal event payload does not match its recorded digest",
        ),
        "payload digest": (
            f"UPDATE {JOURNAL} SET event_payload_digest = ?, payload_digest = ?",
            ("sha256:" + "b" * 64, "sha256:" + "b" * 64),
            "a stored journal event payload does not match its recorded digest",
        ),
        "indexed sequence": (
            f"UPDATE {JOURNAL} SET sequence = 4",
            (),
            "disagrees with the columns it is indexed by",
        ),
        "indexed link": (
            f"UPDATE {JOURNAL} SET previous_link_digest = ?",
            (WRONG_LINK,),
            "disagrees with the columns it is indexed by",
        ),
        "bundle relation": (
            f"UPDATE {JOURNAL} SET bundle_id = ?",
            ("bundle-ip07-other",),
            "not paired with the bundle whose revision it produced",
        ),
        "whole link": (
            (
                f"UPDATE {JOURNAL} SET event_json = ?, event_digest = ?, "
                "event_byte_length = ?, previous_link_digest = ?"
            ),
            (forged, ip06.digest_of(forged), len(forged.encode("utf-8")), WRONG_LINK),
            "does not continue its run's integrity chain",
        ),
    }


@pytest.mark.parametrize("case", sorted(journal_edits()))
def test_a_tampered_journal_event_fails_closed(
    owned: m1.Owned, bound: StoredRuntimeDefinitionBinding, case: str
) -> None:
    apply(owned, bound)
    statement, parameters, expected = journal_edits()[case]
    connection = ip06.corrupt(owned, statement, *parameters, table=JOURNAL)
    try:
        with pytest.raises(StorageError, match=expected):
            read_runtime_journal_events(
                connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
            )
    finally:
        connection.close()


def test_a_bundle_whose_event_was_removed_is_not_read_as_a_bundle(
    owned: m1.Owned, bound: StoredRuntimeDefinitionBinding
) -> None:
    """Half a pair is not a record: a bundle without its event fails closed, not None."""
    apply(owned, bound)
    connection = ip06.corrupt(
        owned, f"DELETE FROM {JOURNAL}", table=JOURNAL, operation="delete"
    )
    try:
        with pytest.raises(StorageError, match="not paired with the journal event"):
            read_bundle(connection)
        assert (
            read_runtime_journal_events(
                connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
            )
            == ()
        )
    finally:
        connection.close()


def test_a_journal_with_a_hole_in_it_is_refused_rather_than_renumbered(
    owned: m1.Owned, bound: StoredRuntimeDefinitionBinding
) -> None:
    apply(owned, bound)
    apply(owned, bound, bundle(1, link=link_after(0)), payload(1))
    connection = ip06.corrupt(
        owned,
        f"DELETE FROM {JOURNAL} WHERE sequence = 0",
        table=JOURNAL,
        operation="delete",
    )
    try:
        with pytest.raises(StorageError, match="does not continue its run's integrity"):
            read_runtime_journal_events(
                connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
            )
    finally:
        connection.close()


# --- durability ----------------------------------------------------------------------


def test_a_verified_backup_restores_a_pair_that_still_reads(
    owned: m1.Owned, bound: StoredRuntimeDefinitionBinding, tmp_path: Path
) -> None:
    apply(owned, bound)
    apply(owned, bound, bundle(1, link=link_after(0)), payload(1))
    stored = read_bundle(owned.connection)
    events = read_runtime_journal_events(
        owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
    )
    owned.connection.close()

    installation = InstallationLayout(root=tmp_path / "installation-state")
    backup = create_verified_backup(
        owned.path,
        installation,
        workspace_id=WORKSPACE_ID,
        attempt_id=new_attempt_id(),
    )
    assert backup.verified

    restored = tmp_path / "restored.sqlite"
    restore_backup(backup.path, restored)
    connection = open_database(restored, OpenMode.EPHEMERAL)
    try:
        assert read_bundle(connection) == stored
        assert (
            read_runtime_journal_events(
                connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
            )
            == events
        )
    finally:
        connection.close()
