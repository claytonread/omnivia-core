"""T-0688 IP-06 acceptance for the Workflow Run definition-binding repository.

The 0035 migration tests hold the schema to what SQL can enforce. These hold
`storage/workflow_runtime_hardening.py` to what SQL cannot.

*A bound Run is one write.* The run row and its complete binding are admitted in one
fenced transaction, visible together inside it, and durable together after it. A
binding that is malformed, incomplete or mismatched against the exact Workflow Version,
definition digest or plan the run names leaves neither row -- no run-only residue -- and
stale authority leaves the workspace exactly as it found it.

*A Legacy Run is labelled, never backfilled.* A run durable before this lane projects
`legacyBinding: true`, names no `bindingRef`, and gains no binding row from being read.
The only historical reference it carries is the exact release pin its own run row proves.

*The record is the bytes.* The stored document is RFC 8785 canonical JSON, addressed by
the SHA-256 of exactly those bytes. Corrupting the bytes, the digest, the length, the
canonical spelling or an indexed column outside this database's own guards fails closed
with a `StorageError` rather than returning something that merely parses.

*A resume decides; it never rebinds.* Equal resolution allows, drift and explicitly
revoked material refuse with their closed diagnostics, and reconciliation is explicit
and attributable. The stored binding is read back byte for byte after every one of them.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest
import test_application_audit_idempotency_migration as m1
import test_rt102_agent_runtime_migration as m18
import test_rt102_agent_runtime_repository as r102
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
    BoundRunAdmission,
    StoredRuntimeDefinitionBinding,
    admit_bound_run,
    evaluate_binding_resume,
    read_runtime_definition_binding,
    read_runtime_definition_binding_projection,
    reconcile_binding_decision,
    workflow_binding_writer,
)

from omnivia_core.contracts.v1.canonical_json import canonicalize
from omnivia_core.contracts.v1.compatibility import ContractSemanticError
from omnivia_core.contracts.v1.semantics_workflow import (
    RUNTIME_BINDING_RECONCILE_OUTCOMES,
    validate_runtime_definition_binding,
    validate_runtime_definition_binding_projection,
)

WORKSPACE_ID = m27.WORKSPACE_ID
OTHER_WORKSPACE_ID = m18.OTHER_WORKSPACE_ID
RUN_ID = m27.RUN_ID
BINDINGS = "omnivia_workflow_runtime_bindings"
RUNS = m27.RUNS

BINDING_ID = "binding-ip06-one"
RUN_BOUND_AT_US = m27.BASE_US + 20
BOUND_AT_US = m27.BASE_US + 100

EVIDENCE = {"evidenceRef": "evidence-ip06-resume"}
ACTOR = {"principalId": "core-operator"}

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def instant(microseconds: int) -> str:
    """One microsecond instant as the `Timestamp` spelling the contract accepts."""
    moment = _EPOCH + timedelta(microseconds=microseconds)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond:06d}Z"


def digest_of(document: str) -> str:
    return f"sha256:{sha256(document.encode('utf-8')).hexdigest()}"


def binding(**overrides: Any) -> dict[str, Any]:
    """A complete `RuntimeDefinitionBinding` pinning m27's run to m27's plan."""
    document: dict[str, Any] = {
        "bindingSchemaVersion": "1.0.0",
        "bindingId": BINDING_ID,
        "workflowId": m27.WORKFLOW_ID,
        "workflowVersion": m27.WORKFLOW_VERSION,
        "releaseRef": {"releaseId": "release-ip06"},
        "definitionDigest": m27.DEFINITION_HASH,
        "executionProfileDigest": "sha256:" + "3" * 64,
        "effectivePolicyDigest": "sha256:" + "4" * 64,
        "componentImplementationDigests": {"component-echo": "sha256:" + "5" * 64},
        "resourceBindingSnapshots": [
            {
                "resourceRequirementId": "resource-store",
                "resourceRef": {"resourceId": "store-primary"},
                "snapshotRef": {"snapshotId": "snapshot-ip06"},
                "snapshotDigest": "sha256:" + "6" * 64,
            }
        ],
        "modelPolicySnapshotRef": {"snapshotId": "model-policy-ip06"},
        "modelPolicySnapshotDigest": "sha256:" + "7" * 64,
        "boundAt": instant(BOUND_AT_US),
        "boundBy": {"principalId": "core-service"},
    }
    document.update(overrides)
    return document


def admission(**overrides: Any) -> BoundRunAdmission:
    values: dict[str, Any] = {
        "run_id": RUN_ID,
        "workflow_id": m27.WORKFLOW_ID,
        "workflow_version": m27.WORKFLOW_VERSION,
        "plan_hash": m27.PLAN_HASH,
        "bound_at_us": RUN_BOUND_AT_US,
        "binding": binding(),
    }
    values.update(overrides)
    return BoundRunAdmission(**values)


@pytest.fixture
def owned(tmp_path: Path) -> Iterator[m1.Owned]:
    path = tmp_path / "workspace.sqlite"
    materialise_phase0_baseline(path)
    m1.bootstrap_and_migrate(path, workspace_id=WORKSPACE_ID)
    holder = m1.take_ownership(path)
    yield holder
    holder.connection.close()


def prepare(holder: m1.Owned) -> None:
    """The sealed plan and the canonical Runtime run a Workflow run is admitted onto."""
    m27.seed_plan(holder)
    m27.seed_runtime_run(holder)


def admit(
    holder: m1.Owned, record: BoundRunAdmission | None = None
) -> StoredRuntimeDefinitionBinding:
    return admit_bound_run(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
        admission=record or admission(),
    )


def counts(connection: sqlite3.Connection) -> tuple[int, int]:
    return tuple(  # type: ignore[return-value]
        int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in (RUNS, BINDINGS)
    )


def stored_row(connection: sqlite3.Connection) -> tuple[Any, ...]:
    row = connection.execute(
        f"SELECT binding_json, binding_digest, binding_byte_length, bound_at_us "
        f"FROM {BINDINGS} WHERE workspace_id = ? AND run_id = ?",
        (WORKSPACE_ID, RUN_ID),
    ).fetchone()
    assert row is not None
    return tuple(row)


def corrupt(
    holder: m1.Owned,
    statement: str,
    *parameters: object,
    table: str = BINDINGS,
    operation: str = "update",
) -> sqlite3.Connection:
    """One file edited outside this database's own guards, which reads exist for.

    `r102.tamper` reopens the file with nothing in the way; the pragma and the dropped
    trigger are what let an offline edit land on an append-only, CHECK-guarded row at
    all, which is the state a verified read has to fail closed on. `table` and
    `operation` name which append-only guard the edit has to get past, because a
    contradiction between a binding and the run or plan it names can be introduced from
    either side of that relation.
    """
    connection = r102.tamper(
        holder,
        "PRAGMA ignore_check_constraints = ON",
        f"DROP TRIGGER omnivia_guard_{table.removeprefix('omnivia_')}_{operation}",
    )
    connection.execute(statement, parameters)
    connection.commit()
    return connection


# --- one atomic admission -----------------------------------------------------------


def test_a_new_bound_run_and_its_binding_are_one_write(owned: m1.Owned) -> None:
    """The run and the whole binding, stored as the canonical bytes they address."""
    prepare(owned)
    stored = admit(owned)

    document = canonicalize(binding())
    assert stored.binding == binding()
    assert stored.content_address == digest_of(document)
    assert stored.content_length_bytes == len(document.encode("utf-8"))
    assert stored_row(owned.connection) == (
        document,
        digest_of(document),
        len(document.encode("utf-8")),
        BOUND_AT_US,
    )
    assert owned.connection.execute(
        f"SELECT workflow_id, workflow_version, plan_hash, bound_at_us FROM {RUNS} "
        "WHERE workspace_id = ? AND run_id = ?",
        (WORKSPACE_ID, RUN_ID),
    ).fetchall() == [
        (m27.WORKFLOW_ID, m27.WORKFLOW_VERSION, m27.PLAN_HASH, RUN_BOUND_AT_US)
    ]

    read = read_runtime_definition_binding(
        owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
    )
    assert read == stored
    validate_runtime_definition_binding(read.binding)  # type: ignore[union-attr]


def test_the_binding_is_visible_inside_the_transaction_that_admitted_it(
    owned: m1.Owned,
) -> None:
    """The composition seam: a caller may read what it just wrote, before commit."""
    prepare(owned)
    with workflow_binding_writer(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
    ) as writer:
        stored = writer.admit_bound_run(admission())
        inside = read_runtime_definition_binding(
            owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
        )
        assert inside == stored
        assert counts(owned.connection) == (1, 1)
    assert counts(owned.connection) == (1, 1)


REFUSED: dict[str, tuple[dict[str, Any], type[Exception]]] = {
    "malformed": ({"boundAt": "not-a-timestamp"}, ContractSemanticError),
    "incomplete": ({"effectivePolicyDigest": None}, ContractSemanticError),
    "unknown field": ({"replacementBinding": {"id": "x"}}, ContractSemanticError),
    "empty components": ({"componentImplementationDigests": {}}, ContractSemanticError),
    "workflow id": ({"workflowId": "workflow-other"}, sqlite3.DatabaseError),
    "workflow version": ({"workflowVersion": "2.0.0"}, sqlite3.DatabaseError),
    "definition digest": (
        {"definitionDigest": "sha256:" + "9" * 64},
        sqlite3.DatabaseError,
    ),
    "predates its run": (
        {"boundAt": instant(m27.BASE_US + 1)},
        sqlite3.DatabaseError,
    ),
}


@pytest.mark.parametrize("case", sorted(REFUSED))
def test_a_binding_that_cannot_be_stored_leaves_neither_row(
    owned: m1.Owned, case: str
) -> None:
    """Malformed, incomplete or mismatched: refused whole, with no run-only residue."""
    overrides, expected = REFUSED[case]
    prepare(owned)
    with pytest.raises(expected):
        admit(owned, admission(binding=binding(**overrides)))
    assert counts(owned.connection) == (0, 0)
    assert (
        read_runtime_definition_binding_projection(
            owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
        )
        is None
    )


def test_an_instant_finer_than_a_microsecond_is_refused_rather_than_truncated(
    owned: m1.Owned,
) -> None:
    """`fromisoformat` would drop the nanosecond digits and store a different instant."""
    prepare(owned)
    nanosecond = instant(BOUND_AT_US).removesuffix("Z") + "789Z"
    with pytest.raises(StorageError, match="finer than a microsecond"):
        admit(owned, admission(binding=binding(boundAt=nanosecond)))
    assert counts(owned.connection) == (0, 0)


def test_stale_authority_refuses_without_mutation(owned: m1.Owned) -> None:
    prepare(owned)
    with pytest.raises(StaleGeneration):
        admit_bound_run(
            owned.connection,
            owned.identity,
            workspace_id=WORKSPACE_ID,
            fencing_generation=owned.generation + 1,
            admission=admission(),
        )
    assert counts(owned.connection) == (0, 0)


def test_a_recorded_binding_can_be_neither_replaced_nor_changed(
    owned: m1.Owned,
) -> None:
    """Append-only, and there is no write here that offers to amend one."""
    prepare(owned)
    stored = admit(owned)
    before = stored_row(owned.connection)

    with pytest.raises(sqlite3.IntegrityError):
        admit(owned, admission(binding=binding(bindingId="binding-ip06-two")))
    for statement in (
        f"UPDATE {BINDINGS} SET bound_at_us = bound_at_us + 1",
        f"DELETE FROM {BINDINGS}",
    ):
        with (
            m27.guarded(owned),
            pytest.raises(sqlite3.DatabaseError, match="append-only"),
        ):
            owned.connection.execute(statement)

    assert stored_row(owned.connection) == before
    assert (
        read_runtime_definition_binding(
            owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
        )
        == stored
    )


# --- projection: current and legacy --------------------------------------------------


def test_a_bound_run_projects_exactly_one_binding_ref(owned: m1.Owned) -> None:
    prepare(owned)
    stored = admit(owned)
    projection = read_runtime_definition_binding_projection(
        owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
    )
    assert projection == {
        "runId": RUN_ID,
        "legacyBinding": False,
        "bindingRef": {
            "bindingId": BINDING_ID,
            "bindingDigest": stored.content_address,
        },
    }
    assert "historicalExactRefs" not in projection
    validate_runtime_definition_binding_projection(projection)


def test_a_legacy_run_is_labelled_and_never_backfilled(owned: m1.Owned) -> None:
    """A run durable before 0035 keeps no binding, and reading it writes none."""
    m27.seed_workflow_run(owned)
    projection = read_runtime_definition_binding_projection(
        owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
    )
    assert projection == {
        "runId": RUN_ID,
        "legacyBinding": True,
        # The one reference the run row itself proves: nothing here is derived from a
        # binding, because there is none to derive it from.
        "historicalExactRefs": [
            {
                "workflowId": m27.WORKFLOW_ID,
                "workflowVersion": m27.WORKFLOW_VERSION,
                "planDigest": m27.PLAN_HASH,
            }
        ],
    }
    assert "bindingRef" not in projection
    validate_runtime_definition_binding_projection(projection)
    assert counts(owned.connection) == (1, 0)
    assert (
        read_runtime_definition_binding(
            owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
        )
        is None
    )
    assert counts(owned.connection) == (1, 0)


def test_no_reader_answers_for_a_workspace_it_was_not_asked_about(
    owned: m1.Owned,
) -> None:
    prepare(owned)
    admit(owned)
    assert (
        read_runtime_definition_binding(
            owned.connection, workspace_id=OTHER_WORKSPACE_ID, run_id=RUN_ID
        )
        is None
    )
    assert (
        read_runtime_definition_binding_projection(
            owned.connection, workspace_id=OTHER_WORKSPACE_ID, run_id=RUN_ID
        )
        is None
    )
    assert (
        read_runtime_definition_binding_projection(
            owned.connection, workspace_id=WORKSPACE_ID, run_id="run-absent"
        )
        is None
    )


# --- corruption fails closed ---------------------------------------------------------


def test_tampered_bytes_are_refused_rather_than_returned(owned: m1.Owned) -> None:
    prepare(owned)
    admit(owned)
    document = str(stored_row(owned.connection)[0])
    # Same length, so only the digest can tell.
    edited = document.replace("core-service", "core-serviceX")[: len(document)]
    assert len(edited) == len(document) and edited != document
    connection = corrupt(owned, f"UPDATE {BINDINGS} SET binding_json = ?", edited)
    try:
        with pytest.raises(StorageError, match="does not match its recorded digest"):
            read_runtime_definition_binding(
                connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
            )
    finally:
        connection.close()


def test_a_tampered_digest_is_refused_rather_than_believed(owned: m1.Owned) -> None:
    prepare(owned)
    admit(owned)
    connection = corrupt(
        owned, f"UPDATE {BINDINGS} SET binding_digest = ?", "sha256:" + "e" * 64
    )
    try:
        with pytest.raises(StorageError, match="does not match its recorded digest"):
            read_runtime_definition_binding(
                connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
            )
    finally:
        connection.close()


def test_a_tampered_byte_length_is_refused_rather_than_ignored(owned: m1.Owned) -> None:
    prepare(owned)
    admit(owned)
    connection = corrupt(
        owned, f"UPDATE {BINDINGS} SET binding_byte_length = binding_byte_length + 1"
    )
    try:
        with pytest.raises(
            StorageError, match="does not match its recorded byte length"
        ):
            read_runtime_definition_binding(
                connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
            )
    finally:
        connection.close()


def test_non_canonical_bytes_are_refused_even_when_they_decode(owned: m1.Owned) -> None:
    """A document whose digest no other JCS implementation reproduces is not this one."""
    prepare(owned)
    admit(owned)
    spaced = canonicalize(binding()).replace(",", ", ")
    connection = corrupt(
        owned,
        f"UPDATE {BINDINGS} SET binding_json = ?, binding_digest = ?, "
        "binding_byte_length = ?",
        spaced,
        digest_of(spaced),
        len(spaced.encode("utf-8")),
    )
    try:
        with pytest.raises(StorageError, match="is not canonical JSON"):
            read_runtime_definition_binding(
                connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
            )
    finally:
        connection.close()


@pytest.mark.parametrize(
    "column,value",
    (
        ("bound_at_us", BOUND_AT_US + 1),
        ("binding_id", "binding-ip06-other"),
        ("binding_schema_version", 2),
    ),
)
def test_a_column_that_contradicts_the_document_fails_closed(
    owned: m1.Owned, column: str, value: object
) -> None:
    """The indexed columns are copies of the document, and a copy that drifted is a lie."""
    prepare(owned)
    admit(owned)
    connection = corrupt(owned, f"UPDATE {BINDINGS} SET {column} = ?", value)
    try:
        with pytest.raises(
            StorageError, match="disagrees with the columns it is indexed by"
        ):
            read_runtime_definition_binding(
                connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
            )
    finally:
        connection.close()


def test_a_stored_document_that_is_not_a_valid_binding_is_refused(
    owned: m1.Owned,
) -> None:
    prepare(owned)
    admit(owned)
    document = canonicalize({"bindingId": BINDING_ID})
    connection = corrupt(
        owned,
        f"UPDATE {BINDINGS} SET binding_json = ?, binding_digest = ?, "
        "binding_byte_length = ?",
        document,
        digest_of(document),
        len(document.encode("utf-8")),
    )
    try:
        with pytest.raises(
            StorageError, match="is not a valid RuntimeDefinitionBinding"
        ):
            read_runtime_definition_binding(
                connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
            )
    finally:
        connection.close()


def test_a_document_outside_the_canonicalizer_domain_is_refused(
    owned: m1.Owned,
) -> None:
    """Valid JSON is not necessarily canonicalizable, and neither is it believed.

    `9007199254740993` parses, and has no canonical form under OmniVia's admission
    profile because it does not round trip through binary64. The digest and the length
    are adjusted to the tampered bytes so the read reaches canonicalization rather than
    stopping at the address.
    """
    prepare(owned)
    admit(owned)
    document = f'{{"bindingId":"{BINDING_ID}","n":9007199254740993}}'
    connection = corrupt(
        owned,
        f"UPDATE {BINDINGS} SET binding_json = ?, binding_digest = ?, "
        "binding_byte_length = ?",
        document,
        digest_of(document),
        len(document.encode("utf-8")),
    )
    try:
        with pytest.raises(StorageError, match="cannot be canonicalized"):
            read_runtime_definition_binding(
                connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
            )
    finally:
        connection.close()


def test_a_binding_that_contradicts_its_run_or_plan_fails_closed(
    owned: m1.Owned,
) -> None:
    """0035's INSERT-trigger join, re-run on the read: the plan's digest moved.

    The join to the run still holds -- runs reach plans by `plan_hash`, not by
    `definition_hash` -- so the binding row is intact, self-consistent and indexed
    correctly, and only the relation it was admitted against disagrees.
    """
    prepare(owned)
    admit(owned)
    connection = corrupt(
        owned,
        f"UPDATE {m27.PLANS} SET definition_hash = ?",
        "sha256:" + "9" * 64,
        table=m27.PLANS,
    )
    try:
        with pytest.raises(StorageError, match="disagrees with the run and plan"):
            read_runtime_definition_binding(
                connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
            )
    finally:
        connection.close()


def test_a_binding_whose_run_relation_is_gone_is_not_read_as_legacy(
    owned: m1.Owned,
) -> None:
    """A binding that exists and cannot be believed is never a Legacy Run."""
    prepare(owned)
    admit(owned)
    connection = corrupt(
        owned,
        f"DELETE FROM {RUNS} WHERE run_id = ?",
        RUN_ID,
        table=RUNS,
        operation="delete",
    )
    try:
        with pytest.raises(StorageError, match="no longer there"):
            read_runtime_definition_binding(
                connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
            )
    finally:
        connection.close()


# --- resume and reconciliation -------------------------------------------------------


DRIFT: dict[str, dict[str, Any]] = {
    "definition digest": {"definitionDigest": "sha256:" + "0" * 64},
    "execution profile digest": {"executionProfileDigest": "sha256:" + "b" * 64},
    "policy digest": {"effectivePolicyDigest": "sha256:" + "c" * 64},
    "component digest": {
        "componentImplementationDigests": {"component-echo": "sha256:" + "d" * 64}
    },
    "release reference": {"releaseRef": {"releaseId": "release-other"}},
    "resource snapshot": {
        "resourceBindingSnapshots": [
            {
                "resourceRequirementId": "resource-store",
                "resourceRef": {"resourceId": "store-primary"},
                "snapshotRef": {"snapshotId": "snapshot-other"},
                "snapshotDigest": "sha256:" + "6" * 64,
            }
        ]
    },
}


def resume(
    stored: StoredRuntimeDefinitionBinding,
    resolved: dict[str, Any] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    return evaluate_binding_resume(
        stored=stored,
        resolved=resolved if resolved is not None else dict(stored.binding),
        evidence=EVIDENCE,
        **kwargs,
    )


def test_an_exact_resolution_allows_the_resume(owned: m1.Owned) -> None:
    prepare(owned)
    stored = admit(owned)
    # A re-resolution recorded at a different instant by a different principal is the
    # same bound material: only the pins are compared.
    resolved = binding(
        boundAt=instant(BOUND_AT_US + 5_000),
        boundBy={"principalId": "core-resumer"},
        bindingId="binding-ip06-resolved",
    )
    assert resume(stored, resolved) == {
        "decision": "allow",
        "runId": RUN_ID,
        "evidence": EVIDENCE,
    }


def test_a_decision_names_the_run_the_binding_was_read_under(owned: m1.Owned) -> None:
    """The `runId` comes from the record, so it cannot be aimed at another Run.

    Valid binding bytes are valid for exactly one Run. There is no caller-supplied run
    identifier left to disagree with the record, and a record carrying a different one
    decides for that Run instead -- which is what makes the tie structural rather than a
    convention the caller is trusted to keep.
    """
    prepare(owned)
    admit(owned)
    stored = read_runtime_definition_binding(
        owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
    )
    assert stored is not None
    assert (stored.workspace_id, stored.run_id) == (WORKSPACE_ID, RUN_ID)
    assert resume(stored)["runId"] == stored.run_id == RUN_ID
    elsewhere = replace(stored, run_id="run-elsewhere")
    assert resume(elsewhere)["runId"] == "run-elsewhere"
    assert (
        resume(elsewhere, binding(**DRIFT["definition digest"]))["runId"]
        == "run-elsewhere"
    )


@pytest.mark.parametrize("case", sorted(DRIFT))
def test_any_drift_refuses_with_the_drift_diagnostic(
    owned: m1.Owned, case: str
) -> None:
    prepare(owned)
    stored = admit(owned)
    assert resume(stored, binding(**DRIFT[case])) == {
        "decision": "refuse",
        "runId": RUN_ID,
        "evidence": EVIDENCE,
        "diagnostic": "RT_BINDING_DRIFT",
    }


def test_a_model_policy_that_is_no_longer_pinned_is_drift(owned: m1.Owned) -> None:
    """The optional pair is compared by presence too, not only by value."""
    prepare(owned)
    stored = admit(owned)
    resolved = binding()
    del resolved["modelPolicySnapshotRef"]
    del resolved["modelPolicySnapshotDigest"]
    assert resume(stored, resolved)["diagnostic"] == "RT_BINDING_DRIFT"


@pytest.mark.parametrize("state", ("revoked", "missing", "incompatible"))
def test_explicitly_unusable_bound_material_refuses_as_revoked(
    owned: m1.Owned, state: str
) -> None:
    """Reported unusable material is a more specific fact than what it resolves to now."""
    prepare(owned)
    stored = admit(owned)
    decision = resume(
        stored,
        binding(**DRIFT["definition digest"]),
        material_states={"component-echo": state},
    )
    assert decision == {
        "decision": "refuse",
        "runId": RUN_ID,
        "evidence": EVIDENCE,
        "diagnostic": "RT_BINDING_REVOKED",
    }
    assert (
        resume(stored, material_states={"component-echo": "available"})["decision"]
        == "allow"
    )


def test_an_unknown_material_state_is_not_evidence_of_availability(
    owned: m1.Owned,
) -> None:
    prepare(owned)
    stored = admit(owned)
    with pytest.raises(StorageError, match="unknown state"):
        resume(stored, material_states={"component-echo": "probably-fine"})


@pytest.mark.parametrize("outcome", RUNTIME_BINDING_RECONCILE_OUTCOMES)
def test_every_reconcile_outcome_is_explicit_and_attributable(outcome: str) -> None:
    assert reconcile_binding_decision(
        run_id=RUN_ID,
        evidence=EVIDENCE,
        deciding_actor=ACTOR,
        reason="operator reviewed the drift",
        outcome=outcome,
    ) == {
        "decision": "reconcile",
        "runId": RUN_ID,
        "evidence": EVIDENCE,
        "decidingActor": ACTOR,
        "reason": "operator reviewed the drift",
        "outcome": outcome,
    }


def test_an_outcome_outside_the_closed_set_is_refused() -> None:
    with pytest.raises(StorageError, match="is not a reconciliation outcome"):
        reconcile_binding_decision(
            run_id=RUN_ID,
            evidence=EVIDENCE,
            deciding_actor=ACTOR,
            reason="operator invented an outcome",
            outcome="rebind",
        )


def test_no_decision_changes_the_stored_binding(owned: m1.Owned) -> None:
    """Every decision this lane can reach, and the row is byte for byte what it was."""
    prepare(owned)
    stored = admit(owned)
    before = stored_row(owned.connection)

    resume(stored)
    resume(stored, binding(**DRIFT["definition digest"]))
    resume(stored, material_states={"component-echo": "revoked"})
    for outcome in RUNTIME_BINDING_RECONCILE_OUTCOMES:
        reconcile_binding_decision(
            run_id=RUN_ID,
            evidence=EVIDENCE,
            deciding_actor=ACTOR,
            reason="operator decided",
            outcome=outcome,
        )

    assert stored_row(owned.connection) == before
    assert (
        read_runtime_definition_binding(
            owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
        )
        == stored
    )
    assert counts(owned.connection) == (1, 1)


# --- durability ----------------------------------------------------------------------


def test_a_verified_backup_restores_a_binding_that_still_reads(
    owned: m1.Owned, tmp_path: Path
) -> None:
    prepare(owned)
    stored = admit(owned)
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
        assert (
            read_runtime_definition_binding(
                connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
            )
            == stored
        )
        assert read_runtime_definition_binding_projection(
            connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
        ) == {
            "runId": RUN_ID,
            "legacyBinding": False,
            "bindingRef": {
                "bindingId": BINDING_ID,
                "bindingDigest": stored.content_address,
            },
        }
    finally:
        connection.close()
