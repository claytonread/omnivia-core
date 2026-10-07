"""Evidence-only review finding quarantine (DEV-REQ-176; migration 0065).

Proves that a stale or unvalidatable finding is kept as evidence and never becomes authority:
stale and missing generation, workspace and run are retained with their reason, exact and
same-bytes submissions deduplicate, different bytes or binding facts stay distinct, a stale writer
and a rolled-back fence write nothing, cross-workspace and malformed input are refused, and every
authority table is byte-for-byte unchanged before and after. Rows are read back from a fresh process.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import test_blobs_staged_sources_and_evidence_migration as m2
from omnivia_core_runtime.ownership.fencing import StaleGeneration, fenced_transaction
from omnivia_core_runtime.service.review_finding_quarantine import (
    quarantine_review_finding,
)
from omnivia_core_runtime.storage import review_finding_quarantine as quarantine
from omnivia_core_runtime.storage.inventory import DatabaseInventory, capture_inventory
from omnivia_core_runtime.storage.migrations import (
    load_migrations,
    materialise_phase0_baseline,
)

from omnivia_core.contracts.v1 import ERROR_CODE_IDEMPOTENCY_CONFLICT

WORKSPACE_ID = m2.WORKSPACE_ID
OTHER_WORKSPACE_ID = "ws-quarantine-other-0001"
BASE_US = m2.BASE_US
TABLE = "omnivia_review_finding_quarantines"
MIGRATION_NAME = "0065_review_finding_quarantine.sql"
#: Ledger tables change on every migrated open; every other table is an authority or substrate row.
LEDGER = {"omnivia_schema_migrations", "omnivia_migration_attempts"}
DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64

def _bootstrap(path: Path) -> m2.Owned:
    materialise_phase0_baseline(path)
    m2.bootstrap_and_migrate(path)
    return m2.take_ownership(path)


@pytest.fixture
def owned(tmp_path: Path) -> Iterator[m2.Owned]:
    holder = _bootstrap(tmp_path / "workspace.sqlite")
    yield holder
    holder.connection.close()


def _envelope(holder: m2.Owned, **overrides: Any) -> quarantine.ReviewFindingEnvelope:
    """A stale-generation finding observed one generation behind the current authority."""
    values: dict[str, Any] = {
        "workspace_id": WORKSPACE_ID,
        "run_id": "run-quarantine-1",
        "candidate_id": "candidate-1",
        "evidence_id": "evidence-1",
        "content_digest": DIGEST_A,
        "reason": "stale_generation",
        "observed_generation": holder.generation - 1,
        "observed_binding": "binding-1",
        "attributed_to": "reviewer-1",
        "recorded_at_us": BASE_US,
    }
    values.update(overrides)
    return quarantine.ReviewFindingEnvelope(**values)


def _quarantine(
    holder: m2.Owned,
    envelope: quarantine.ReviewFindingEnvelope,
    *,
    key: str = "key-1",
    fencing_generation: int | None = None,
) -> quarantine.QuarantinedFinding:
    return quarantine_review_finding(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation if fencing_generation is None else fencing_generation,
        idempotency_key=key,
        envelope=envelope,
    )


def _count(connection: Any) -> int:
    return m2.count(connection, TABLE)


def _authority(connection: Any) -> DatabaseInventory:
    return capture_inventory(connection)


def _assert_authority_unchanged(before: DatabaseInventory, after: DatabaseInventory) -> None:
    """Every table outside the new evidence table and the ledger holds the same rows."""
    assert set(after.table_names) == set(before.table_names)
    for name in before.table_names:
        if name in LEDGER or name == TABLE:
            continue
        assert after.table(name) == before.table(name), name


def test_the_migration_allocates_one_evidence_table_and_refuses_any_other_writer(
    owned: m2.Owned,
) -> None:
    assert MIGRATION_NAME in {m.name for m in load_migrations()}
    assert TABLE in m2.object_names(owned.connection, "table")
    # A connection with no fence cannot write: the authorizer refuses the statement.
    with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
        owned.connection.execute(
            "INSERT INTO omnivia_review_finding_quarantines DEFAULT VALUES"
        )
    assert _count(owned.connection) == 0


def test_a_stale_generation_finding_is_retained_as_evidence_and_authority_is_unchanged(
    owned: m2.Owned,
) -> None:
    before = _authority(owned.connection)
    record = _quarantine(owned, _envelope(owned))

    assert record.envelope.reason == "stale_generation"
    assert record.quarantined_under_generation == owned.generation
    assert record.envelope.observed_generation == owned.generation - 1
    assert _count(owned.connection) == 1
    assert quarantine.read_finding(
        owned.connection, workspace_id=WORKSPACE_ID, finding_digest=record.finding_digest
    ) == record
    _assert_authority_unchanged(before, _authority(owned.connection))


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param(
            {"reason": "missing_generation", "observed_generation": None},
            id="missing-generation",
        ),
        pytest.param(
            {"reason": "missing_workspace", "observed_binding": None, "observed_generation": None},
            id="missing-workspace",
        ),
        pytest.param({"reason": "missing_run", "run_id": None}, id="missing-run"),
    ],
)
def test_a_missing_generation_workspace_or_run_is_retained_and_never_validated(
    owned: m2.Owned, overrides: dict[str, Any]
) -> None:
    before = _authority(owned.connection)
    record = _quarantine(owned, _envelope(owned, **overrides))

    assert record.envelope.reason == overrides["reason"]
    stored = quarantine.read_findings(owned.connection, workspace_id=WORKSPACE_ID)
    assert [item.envelope.reason for item in stored] == [overrides["reason"]]
    _assert_authority_unchanged(before, _authority(owned.connection))


def test_an_exact_resubmission_returns_the_canonical_record(owned: m2.Owned) -> None:
    first = _quarantine(owned, _envelope(owned))
    replay = _quarantine(owned, _envelope(owned))

    assert replay == first
    assert _count(owned.connection) == 1


def test_the_same_bytes_under_another_key_are_refused_and_bind_that_key_to_nothing(
    owned: m2.Owned,
) -> None:
    first = _quarantine(owned, _envelope(owned), key="key-1")
    before = _authority(owned.connection)

    with pytest.raises(quarantine.ReviewFindingConflict):
        _quarantine(owned, _envelope(owned), key="key-2")

    assert _count(owned.connection) == 1
    assert quarantine.read_findings(owned.connection, workspace_id=WORKSPACE_ID) == (first,)
    _assert_authority_unchanged(before, _authority(owned.connection))


def test_three_calls_bind_each_observed_key_to_exactly_one_digest(owned: m2.Owned) -> None:
    """The regression: (e1,k1), (e1,k2), (e2,k2). The second call used to return e1 with k2 unbound,
    so the third bound k2 to e2 as well. Now k2 is first observed for e2 alone."""
    first = _quarantine(owned, _envelope(owned), key="k1")
    other = _envelope(owned, content_digest=DIGEST_B)
    with pytest.raises(quarantine.ReviewFindingConflict):
        _quarantine(owned, _envelope(owned), key="k2")
    second = _quarantine(owned, other, key="k2")

    bindings = {
        (record.idempotency_key, record.finding_digest)
        for record in quarantine.read_findings(owned.connection, workspace_id=WORKSPACE_ID)
    }
    assert bindings == {("k1", first.finding_digest), ("k2", second.finding_digest)}
    assert _count(owned.connection) == 2
    assert _quarantine(owned, other, key="k2") == second


def test_different_bytes_or_binding_facts_produce_distinct_records(owned: m2.Owned) -> None:
    base = _envelope(owned)
    variants = [
        base,
        replace(base, content_digest=DIGEST_B),
        replace(base, observed_binding="binding-2"),
        replace(base, reason="missing_run", run_id=None),
    ]
    records = [
        _quarantine(owned, variant, key=f"key-{index}") for index, variant in enumerate(variants)
    ]

    assert len({record.finding_digest for record in records}) == len(variants)
    assert _count(owned.connection) == len(variants)
    assert {r.envelope.content_digest for r in records} == {DIGEST_A, DIGEST_B}


def test_a_reused_idempotency_key_with_different_bytes_conflicts_and_writes_nothing(
    owned: m2.Owned,
) -> None:
    _quarantine(owned, _envelope(owned), key="key-1")
    before = _authority(owned.connection)

    with pytest.raises(quarantine.ReviewFindingConflict) as raised:
        _quarantine(owned, _envelope(owned, content_digest=DIGEST_B), key="key-1")

    assert raised.value.error_code == ERROR_CODE_IDEMPOTENCY_CONFLICT
    assert _count(owned.connection) == 1
    _assert_authority_unchanged(before, _authority(owned.connection))


@pytest.mark.parametrize("offset", [-1, 1], ids=["behind-authority", "ahead-of-authority"])
def test_a_stale_writer_is_refused_before_anything_is_written(
    owned: m2.Owned, offset: int
) -> None:
    before = _authority(owned.connection)

    with pytest.raises(StaleGeneration):
        _quarantine(owned, _envelope(owned), fencing_generation=owned.generation + offset)

    assert _count(owned.connection) == 0
    _assert_authority_unchanged(before, _authority(owned.connection))


def test_a_refusal_after_the_write_rolls_the_whole_fence_back(owned: m2.Owned) -> None:
    before = _authority(owned.connection)

    with (
        pytest.raises(RuntimeError, match="later step refused"),
        fenced_transaction(
            owned.connection,
            owned.identity,
            workspace_id=WORKSPACE_ID,
            fencing_generation=owned.generation,
        ) as fenced,
    ):
        quarantine.record_finding(
            fenced,
            workspace_id=WORKSPACE_ID,
            idempotency_key="key-rolled-back",
            envelope=_envelope(owned),
        )
        raise RuntimeError("later step refused")

    assert _count(owned.connection) == 0
    _assert_authority_unchanged(before, _authority(owned.connection))


def test_a_finding_for_another_workspace_is_refused_and_writes_nothing(owned: m2.Owned) -> None:
    before = _authority(owned.connection)

    with pytest.raises(quarantine.ReviewFindingInvalid, match="open workspace"):
        _quarantine(owned, _envelope(owned, workspace_id=OTHER_WORKSPACE_ID))

    assert _count(owned.connection) == 0
    _assert_authority_unchanged(before, _authority(owned.connection))


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"reason": "validated"}, id="reason-outside-vocabulary"),
        pytest.param({"observed_generation": None}, id="stale-without-generation"),
        pytest.param({"observed_binding": None}, id="stale-without-binding"),
        pytest.param({"run_id": None}, id="stale-without-run"),
        pytest.param({"reason": "missing_generation"}, id="missing-generation-with-observed"),
        pytest.param({"reason": "missing_run"}, id="missing-run-with-run"),
        pytest.param({"observed_generation": True}, id="boolean-generation"),
        pytest.param({"observed_generation": -1}, id="negative-generation"),
        pytest.param({"recorded_at_us": 0}, id="zero-instant"),
        pytest.param({"content_digest": "sha256:" + "A" * 64}, id="uppercase-digest"),
        pytest.param({"content_digest": "md5:" + "a" * 32}, id="wrong-digest-algorithm"),
        pytest.param({"candidate_id": "bad id!"}, id="candidate-outside-grammar"),
        pytest.param({"attributed_to": ""}, id="empty-attribution"),
        pytest.param({"observed_binding": ""}, id="empty-binding"),
        pytest.param({"recorded_at_us": True}, id="boolean-instant"),
        pytest.param({"observed_generation": 1.0}, id="float-generation"),
        pytest.param({"reason": ["stale_generation"]}, id="unhashable-reason"),
        pytest.param(
            {"reason": "missing_workspace", "observed_generation": None},
            id="missing-workspace-with-binding",
        ),
        pytest.param(
            {"reason": "missing_generation", "observed_generation": None, "run_id": None},
            id="missing-generation-without-run",
        ),
    ],
)
def test_malformed_input_is_refused_before_storage(
    owned: m2.Owned, overrides: dict[str, Any]
) -> None:
    before = _authority(owned.connection)

    with pytest.raises(quarantine.ReviewFindingInvalid):
        _quarantine(owned, _envelope(owned, **overrides))

    assert _count(owned.connection) == 0
    _assert_authority_unchanged(before, _authority(owned.connection))


@pytest.mark.parametrize("ahead", [0, 1], ids=["equal-to-authority", "ahead-of-authority"])
def test_a_stale_generation_must_be_strictly_behind_the_authority(
    owned: m2.Owned, ahead: int
) -> None:
    before = _authority(owned.connection)
    bad = _envelope(owned, observed_generation=owned.generation + ahead)

    with pytest.raises(quarantine.ReviewFindingInvalid, match="reason shape"):
        _quarantine(owned, bad)

    assert _count(owned.connection) == 0
    _assert_authority_unchanged(before, _authority(owned.connection))


def test_a_malformed_idempotency_key_is_refused_before_storage(owned: m2.Owned) -> None:
    with pytest.raises(quarantine.ReviewFindingInvalid):
        _quarantine(owned, _envelope(owned), key="")
    assert _count(owned.connection) == 0


def test_a_stored_row_that_does_not_verify_its_own_digest_is_refused_on_read(
    owned: m2.Owned,
) -> None:
    with fenced_transaction(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
    ) as fenced:
        fenced.execute(
            "INSERT INTO omnivia_review_finding_quarantines (workspace_id, finding_digest, "
            "idempotency_key, run_id, candidate_id, evidence_id, content_digest, reason, "
            "observed_generation, observed_binding, quarantined_under_generation, attributed_to, "
            "recorded_at_us) VALUES (?, ?, 'key-tampered', 'run-1', 'candidate-1', 'evidence-1', "
            "?, 'stale_generation', ?, 'binding-1', ?, 'reviewer-1', ?)",
            (
                WORKSPACE_ID,
                "sha256:" + "0" * 64,
                DIGEST_A,
                owned.generation - 1,
                owned.generation,
                BASE_US,
            ),
        )

    with pytest.raises(quarantine.ReviewFindingInvalid, match="does not verify its digest"):
        quarantine.read_findings(owned.connection, workspace_id=WORKSPACE_ID)


def test_the_schema_refuses_a_row_that_claims_validation_and_any_update_or_delete(
    owned: m2.Owned,
) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"), fenced_transaction(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
    ) as fenced:
        fenced.execute(
            "INSERT INTO omnivia_review_finding_quarantines (workspace_id, finding_digest, "
            "idempotency_key, run_id, candidate_id, evidence_id, content_digest, reason, "
            "observed_generation, observed_binding, quarantined_under_generation, attributed_to, "
            "recorded_at_us) VALUES (?, ?, 'key-validated', 'run-1', 'candidate-1', 'evidence-1', "
            "?, 'validated', NULL, NULL, ?, 'reviewer-1', ?)",
            (WORKSPACE_ID, DIGEST_B, DIGEST_A, owned.generation, BASE_US),
        )
    assert _count(owned.connection) == 0

    _quarantine(owned, _envelope(owned))
    with pytest.raises(sqlite3.DatabaseError, match="append-only"), fenced_transaction(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
    ) as fenced:
        fenced.execute("UPDATE omnivia_review_finding_quarantines SET reason = 'missing_run'")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"), fenced_transaction(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
    ) as fenced:
        fenced.execute("DELETE FROM omnivia_review_finding_quarantines")
    assert _count(owned.connection) == 1


_RAW_INSERT = (
    "INSERT INTO omnivia_review_finding_quarantines (workspace_id, finding_digest, "
    "idempotency_key, run_id, candidate_id, evidence_id, content_digest, reason, "
    "observed_generation, observed_binding, quarantined_under_generation, attributed_to, "
    "recorded_at_us) VALUES (?, ?, ?, 'run-1', 'candidate-1', 'evidence-1', ?, ?, ?, "
    "'binding-1', ?, 'reviewer-1', ?)"
)


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"run_id": "run-quarantine-2"}, id="run"),
        pytest.param({"candidate_id": "candidate-2"}, id="candidate"),
        pytest.param({"evidence_id": "evidence-2"}, id="evidence"),
        pytest.param({"attributed_to": "reviewer-2"}, id="attribution"),
        pytest.param({"recorded_at_us": BASE_US + 1}, id="instant"),
    ],
)
def test_every_envelope_field_is_part_of_the_identity(
    owned: m2.Owned, overrides: dict[str, Any]
) -> None:
    base = _quarantine(owned, _envelope(owned), key="key-base")
    variant = _quarantine(owned, _envelope(owned, **overrides), key="key-variant")

    assert variant.finding_digest != base.finding_digest
    assert _count(owned.connection) == 2


@pytest.mark.parametrize(
    ("reason", "observed_generation"),
    [
        pytest.param("validated", 0, id="reason-outside-vocabulary"),
        # Integer affinity would coerce "0" to 0, so a non-numeric text survives the insert.
        pytest.param("stale_generation", "not-a-number", id="text-generation"),
    ],
)
def test_a_stored_row_outside_its_closed_shape_is_refused_on_read(
    owned: m2.Owned, reason: str, observed_generation: object
) -> None:
    # The CHECK constraints are the storage-level guard; lifting them here proves the reader
    # refuses a row it did not write rather than trusting the schema alone.
    with fenced_transaction(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
    ) as fenced:
        fenced.execute("PRAGMA ignore_check_constraints = ON")
        try:
            fenced.execute(
                _RAW_INSERT,
                (
                    WORKSPACE_ID,
                    "sha256:" + "0" * 64,
                    "key-malformed",
                    DIGEST_A,
                    reason,
                    observed_generation,
                    owned.generation,
                    BASE_US,
                ),
            )
        finally:
            fenced.execute("PRAGMA ignore_check_constraints = OFF")

    with pytest.raises(quarantine.ReviewFindingInvalid, match="closed shape"):
        quarantine.read_findings(owned.connection, workspace_id=WORKSPACE_ID)


def test_the_schema_binds_each_row_to_the_open_workspace_and_current_generation(
    owned: m2.Owned,
) -> None:
    with (
        pytest.raises(sqlite3.DatabaseError, match="must bind the open workspace"),
        fenced_transaction(
            owned.connection,
            owned.identity,
            workspace_id=WORKSPACE_ID,
            fencing_generation=owned.generation,
        ) as fenced,
    ):
        fenced.execute(
            _RAW_INSERT,
            (
                OTHER_WORKSPACE_ID,
                DIGEST_B,
                "key-other-workspace",
                DIGEST_A,
                "stale_generation",
                owned.generation - 1,
                owned.generation,
                BASE_US,
            ),
        )
    with (
        pytest.raises(sqlite3.DatabaseError, match="current fencing generation"),
        fenced_transaction(
            owned.connection,
            owned.identity,
            workspace_id=WORKSPACE_ID,
            fencing_generation=owned.generation,
        ) as fenced,
    ):
        fenced.execute(
            _RAW_INSERT,
            (
                WORKSPACE_ID,
                DIGEST_B,
                "key-ahead-generation",
                DIGEST_A,
                "stale_generation",
                owned.generation - 1,
                owned.generation + 1,
                BASE_US,
            ),
        )

    assert _count(owned.connection) == 0


_FRESH_READER = """
import json, sys
from pathlib import Path
from omnivia_core_runtime.storage.connection import OpenMode, open_database
from omnivia_core_runtime.storage.review_finding_quarantine import read_findings
connection = open_database(Path(sys.argv[1]), OpenMode.READ_ONLY)
rows = read_findings(connection, workspace_id=sys.argv[2])
print(json.dumps([[r.finding_digest, r.idempotency_key, r.envelope.reason, r.envelope.observed_generation,
                   r.quarantined_under_generation] for r in rows]))
"""


def test_quarantined_records_are_durable_and_readable_from_a_fresh_process(
    owned: m2.Owned,
) -> None:
    written = [
        _quarantine(owned, _envelope(owned), key="key-stale"),
        _quarantine(
            owned,
            _envelope(owned, reason="missing_generation", observed_generation=None),
            key="key-missing",
        ),
    ]
    path = owned.path
    owned.connection.close()

    completed = subprocess.run(
        [sys.executable, "-c", _FRESH_READER, str(path), WORKSPACE_ID],
        capture_output=True,
        text=True,
        check=True,
    )

    fresh = json.loads(completed.stdout)
    expected = sorted(written, key=lambda r: (r.envelope.recorded_at_us, r.finding_digest))
    assert fresh == [
        [r.finding_digest, r.idempotency_key, r.envelope.reason,
         r.envelope.observed_generation, r.quarantined_under_generation]
        for r in expected
    ]


_BINDINGS_READER = """
import json, sys
from pathlib import Path
from omnivia_core_runtime.storage.connection import OpenMode, open_database
from omnivia_core_runtime.storage.review_finding_quarantine import read_findings
connection = open_database(Path(sys.argv[1]), OpenMode.READ_ONLY)
print(json.dumps(sorted([r.idempotency_key, r.finding_digest] for r in read_findings(connection, workspace_id=sys.argv[2]))))
"""


def test_the_key_and_evidence_bindings_hold_across_conflicts_and_a_fresh_process(
    owned: m2.Owned,
) -> None:
    """A/k1; A/k2 refused; B/k2; A/k2 refused again. Each key ends bound to one digest, in a fresh process."""
    a = _quarantine(owned, _envelope(owned), key="k1")
    with pytest.raises(quarantine.ReviewFindingConflict):
        _quarantine(owned, _envelope(owned), key="k2")
    b = _quarantine(owned, _envelope(owned, content_digest=DIGEST_B), key="k2")
    with pytest.raises(quarantine.ReviewFindingConflict):
        _quarantine(owned, _envelope(owned), key="k2")
    assert _quarantine(owned, _envelope(owned), key="k1") == a
    path = owned.path
    owned.connection.close()

    completed = subprocess.run(
        [sys.executable, "-c", _BINDINGS_READER, str(path), WORKSPACE_ID],
        capture_output=True,
        text=True,
        check=True,
    )

    assert json.loads(completed.stdout) == sorted(
        [["k1", a.finding_digest], ["k2", b.finding_digest]]
    )
