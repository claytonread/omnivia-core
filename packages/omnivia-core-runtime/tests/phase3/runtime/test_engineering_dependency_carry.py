"""Dependency sets carried across claim-preserving governance (migration 0051).

`knowledge.propose` and `candidate.approve` mint new exact versions whose content,
claim and evidence are byte copies of the version they transition, and a
consistent sealed dependency set travels with them inside the same fenced, audited
settlement. Everything runs through the production surface. Approval and review
never qualify anything; a replay carries nothing twice; a failed or altered
transition leaves nothing; a damaged source carries nothing; the 0051 guard
refuses every other writer; a carried set is as sealed as an original, never
crosses streams or repositories, survives a restart and yields to revocation;
and fresh and upgraded workspaces reach one canonical schema.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import test_blobs_staged_sources_and_evidence_migration as m2
import test_engineering_source_coverage as esc
from omnivia_core_runtime.ownership.fencing import assert_guards_intact
from omnivia_core_runtime.service.operations import OperationError
from omnivia_core_runtime.storage import (
    engineering_conflicts,
    engineering_preview,
    engineering_source,
)
from omnivia_core_runtime.storage import governance as governance_storage
from omnivia_core_runtime.storage.connection import (
    OpenMode,
    authorised,
    execute_script,
    fingerprint_schema,
    foreign_key_check,
    integrity_check,
    open_database,
    split_sql_statements,
)
from omnivia_core_runtime.storage.migrations import (
    applied_migrations,
    apply_pending_migrations,
    canonical_schema_fingerprint,
    load_migrations,
    phase0_baseline_sql,
    read_workspace_state,
)

from omnivia_core.contracts.v1 import (
    ERROR_CODE_INTERNAL_NON_RECOVERABLE,
    ErrorResponseEnvelope,
    MutationPrecondition,
    SuccessResponseEnvelope,
    to_canonical_json,
)

Workspace = esc.Workspace
WORKSPACE_ID = esc.WORKSPACE_ID
REPOSITORY = esc.REPOSITORY
FILES_A = esc.FILES_A
TARGET_A = {"repository_id": REPOSITORY, "snapshot_id": "esnap-a"}

MIGRATION_VERSION = 51
MIGRATION_NAME = "0051_engineering_dependency_carry.sql"
GUARD = "omnivia_guard_omnivia_engineering_dependency_sets_insert"
OWNER_REFUSAL = "a dependency set belongs to an exact record version"
CARRY_REFUSAL = "a carried dependency set repeats the consistent sealed set"

#: Everything one transition makes durable, including the carried set.
_DURABLE = (
    "omnivia_application_audit_events",
    "omnivia_governed_version_assemblies",
    "omnivia_application_governance_transitions",
    "omnivia_idempotency_claims",
    "omnivia_idempotency_outcomes",
    "omnivia_engineering_dependencies",
    "omnivia_engineering_dependency_sets",
)


@pytest.fixture
def workspace(tmp_path: Path) -> Any:
    opened = Workspace(tmp_path)
    yield opened
    opened.holder.connection.close()


def _payload(record: dict[str, str]) -> dict[str, Any]:
    return {"record_id": record["record_id"], "rationale": {"reason_code": "review"}}


def _transition(
    workspace: Workspace, operation: str, record: dict[str, str], **kwargs: Any
) -> dict[str, str]:
    result = workspace.ok(
        operation,
        _payload(record),
        mutation_precondition=MutationPrecondition(record_version=record["version"]),
        **kwargs,
    )
    identity = result["updated_record"]["provenance"]["identity"]
    return {"record_id": identity["record_id"], "version": identity["version"]}


def _attempt(workspace: Workspace, operation: str, record: dict[str, str]) -> Any:
    """One transition that must fail; what matters is what became durable."""
    try:
        return workspace.call(
            operation,
            _payload(record),
            mutation_precondition=MutationPrecondition(record_version=record["version"]),
        )
    except (RuntimeError, sqlite3.DatabaseError) as error:
        return error


def _durable(workspace: Workspace) -> dict[str, int]:
    connection = workspace.holder.connection
    return {
        table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in _DURABLE
    }


def _set(workspace: Workspace, record: dict[str, str]) -> tuple[Any, ...] | None:
    row = workspace.holder.connection.execute(
        "SELECT repository_id, stream_id, snapshot_id, producer, producer_version, "
        "coverage, dependency_count, audit_ref, recorded_at_us "
        "FROM omnivia_engineering_dependency_sets WHERE record_id = ? AND version = ?",
        (record["record_id"], record["version"]),
    ).fetchone()
    return None if row is None else tuple(row)


def _settlement(workspace: Workspace, version: str) -> tuple[str, int]:
    """The audit and settlement instant of the operation that minted `version`."""
    row = workspace.holder.connection.execute(
        "SELECT audit_ref, recorded_at_us FROM omnivia_governed_version_assemblies "
        "WHERE governed_record_version_id = ?",
        (version,),
    ).fetchone()
    return str(row[0]), int(row[1])


def _watch_carry(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record every database refusal the carry meets, and let it propagate."""
    refusals: list[str] = []
    original = engineering_source.carry_dependency_set

    def watched(*args: Any, **kwargs: Any) -> bool:
        try:
            return original(*args, **kwargs)
        except sqlite3.DatabaseError as error:
            refusals.append(str(error))
            raise

    monkeypatch.setattr(engineering_source, "carry_dependency_set", watched)
    return refusals


def _copy_set(
    connection: sqlite3.Connection,
    *,
    record_id: str,
    source_version: str,
    target_version: str,
    audit_ref: str,
    recorded_at_us: int,
) -> None:
    """Directly write an exact copy of one version's stored set onto another."""
    rows = connection.execute(
        "SELECT selector_type, selector, meaning, producer, expected_digest "
        "FROM omnivia_engineering_dependencies WHERE version = ?",
        (source_version,),
    ).fetchall()
    for index, row in enumerate(rows):
        connection.execute(
            "INSERT INTO omnivia_engineering_dependencies "
            "(workspace_id, dependency_id, record_id, version, selector_type, selector, "
            "meaning, producer, expected_digest, recorded_at_us, audit_ref) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                WORKSPACE_ID,
                f"edep-direct-{index}",
                record_id,
                target_version,
                *row,
                recorded_at_us,
                audit_ref,
            ),
        )
    connection.execute(
        "INSERT INTO omnivia_engineering_dependency_sets "
        "(workspace_id, record_id, version, repository_id, stream_id, snapshot_id, "
        "producer, producer_version, coverage, dependency_count, recorded_at_us, "
        "audit_ref) SELECT workspace_id, ?, ?, repository_id, stream_id, snapshot_id, "
        "producer, producer_version, coverage, dependency_count, ?, ? "
        "FROM omnivia_engineering_dependency_sets WHERE version = ?",
        (record_id, target_version, recorded_at_us, audit_ref, source_version),
    )


# --- approval and review never qualify ---------------------------------------------


def test_approval_and_review_never_qualify_an_unqualified_observation(
    workspace: Workspace,
) -> None:
    """A carried set stays exactly as qualified as its source: approval adds no
    profile, no evidence, no coverage and no attested digest, and a reviewer's
    evidence is recorded without ever establishing `matched`."""
    workspace.record(esc._source(1, "esnap-a", FILES_A))
    cases = {
        "no profile": esc._observation(None, title="No profile provider"),
        "no evidence": esc._observation(esc._manifest(), evidence=False),
        "partial": esc._observation(esc._manifest(coverage="partial")),
        "unattested digest": esc._observation(
            esc._manifest(dependencies=[esc._dependency("src/auth.py", esc.AUTH_V2)])
        ),
        "symbol": esc._observation(
            esc._manifest(
                dependencies=[
                    esc._dependency("src/auth.py", esc.AUTH_V1),
                    esc._dependency("auth.Provider", None, selector_type="symbol"),
                ]
            )
        ),
    }
    accepted = {
        name: esc._accept(workspace, workspace.observe(payload))
        for name, payload in cases.items()
    }
    for name, record in accepted.items():
        assert workspace.status(record, "esnap-a") == "unknown", name
        # A set travels only where one was recorded; approval never writes one.
        assert (_set(workspace, record) is None) == (name == "no profile"), name
    assert workspace.matched("esnap-a", view="accepted") == []

    for name in ("no profile", "no evidence", "partial"):
        review = workspace.ok(
            "engineering.review.record",
            {
                "record_ref": accepted[name],
                "target_snapshot": TARGET_A,
                "review_outcome": "evidence_attached",
                "review_evidence_id": "ev-reviewer-1",
            },
            mutation_precondition=MutationPrecondition(record_version="assessment-0"),
        )
        assert review["applicability"] == "unknown", name
        assert workspace.status(accepted[name], "esnap-a") == "unknown", name
    assert _set(workspace, accepted["no profile"]) is None
    assert workspace.matched("esnap-a", view="accepted") == []
    pack = workspace.ok(
        "engineering.context.build",
        {
            "query": "provider",
            "targets": [TARGET_A],
            "profile": "implement",
            "applicability_mode": "current_safe",
        },
    )["pack"]
    assert pack["sections"] == [] and pack["citations"] == []
    assert pack["omissions"] == [{"field": "sections", "reason": "applicability_unproven"}]


# --- idempotency and atomicity -----------------------------------------------------


def test_a_replayed_transition_carries_nothing_twice(workspace: Workspace) -> None:
    """Each carry writes fresh row identities under its own transition's audit and
    settlement instant; a replay answers from the stored outcome and writes none."""
    workspace.record(esc._source(1, "esnap-a", FILES_A))
    created = workspace.observe(esc._observation(esc._manifest()))
    proposed = _transition(workspace, "knowledge.propose", created, key="carry-p")
    after_propose = _durable(workspace)
    assert _transition(workspace, "knowledge.propose", created, key="carry-p") == proposed
    assert _durable(workspace) == after_propose
    accepted = _transition(workspace, "candidate.approve", proposed, key="carry-a")
    after_approve = _durable(workspace)
    assert _transition(workspace, "candidate.approve", proposed, key="carry-a") == accepted
    assert _durable(workspace) == after_approve
    assert after_approve["omnivia_engineering_dependency_sets"] == 3
    assert after_approve["omnivia_engineering_dependencies"] == 3 * len(esc.DEPENDENCIES)

    connection = workspace.holder.connection
    identities: list[set[str]] = []
    for record, operation in (
        (created, "memory.create"),
        (proposed, "knowledge.propose"),
        (accepted, "candidate.approve"),
    ):
        audit_ref, settled_at_us = _settlement(workspace, record["version"])
        assert connection.execute(
            "SELECT operation FROM omnivia_application_audit_events WHERE audit_ref = ?",
            (audit_ref,),
        ).fetchone() == (operation,)
        stored = _set(workspace, record)
        assert stored is not None and stored[7:] == (audit_ref, settled_at_us)
        rows = connection.execute(
            "SELECT dependency_id, audit_ref, recorded_at_us "
            "FROM omnivia_engineering_dependencies WHERE version = ?",
            (record["version"],),
        ).fetchall()
        assert {(row[1], row[2]) for row in rows} == {(audit_ref, settled_at_us)}
        identities.append({str(row[0]) for row in rows})
    assert len(set().union(*identities)) == 3 * len(esc.DEPENDENCIES)
    assert workspace.status(accepted, "esnap-a") == "matched"


def test_a_failed_transition_leaves_no_carried_set(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The carry commits with its transition or not at all."""
    workspace.record(esc._source(1, "esnap-a", FILES_A))
    created = workspace.observe(esc._observation(esc._manifest()))
    before = _durable(workspace)
    original = engineering_source.carry_dependency_set

    def carry_then_fail(*args: Any, **kwargs: Any) -> bool:
        assert original(*args, **kwargs) is True
        raise OperationError(ERROR_CODE_INTERNAL_NON_RECOVERABLE, "forced rollback")

    with monkeypatch.context() as patched:
        patched.setattr(engineering_source, "carry_dependency_set", carry_then_fail)
        response = _attempt(workspace, "knowledge.propose", created)
    assert isinstance(response, ErrorResponseEnvelope), response
    assert response.error.code == ERROR_CODE_INTERNAL_NON_RECOVERABLE
    assert _durable(workspace) == before
    assert workspace.holder.connection.in_transaction is False

    proposed = _transition(workspace, "knowledge.propose", created)
    assert workspace.status(proposed, "esnap-a") == "matched"


def test_a_carry_that_differs_from_what_its_transition_copied_is_refused(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Inside the real audited transition, a carried set that restates any claim
    differently, or a transition whose copy of content or evidence differs from its
    source, is refused by the 0051 guard and the whole transition rolls back. A
    changed claim never reaches the carry: 0014 refuses the transition first."""
    m2.write(workspace.holder, m2.EVIDENCE, evidence_id="evd-other", source_native_id="doc-other")
    workspace.record(esc._source(1, "esnap-a", FILES_A))
    workspace.record(esc._source(2, "esnap-a2", FILES_A, predecessor="esnap-a"))
    created = workspace.observe(esc._observation(esc._manifest(coverage="partial")))
    before = _durable(workspace)

    def forged_carry(forge: Callable[[list[Any], list[list[Any]]], None]) -> Any:
        def carry(
            connection: sqlite3.Connection,
            settlement: Any,
            *,
            workspace_id: str,
            record_id: str,
            source_version: str,
            target_version: str,
            allocate_identifier: Any,
        ) -> bool:
            claims = list(
                connection.execute(
                    "SELECT repository_id, stream_id, snapshot_id, producer, "
                    "producer_version, coverage FROM omnivia_engineering_dependency_sets "
                    "WHERE version = ?",
                    (source_version,),
                ).fetchone()
            )
            rows = [
                list(row)
                for row in connection.execute(
                    "SELECT selector_type, selector, meaning, producer, expected_digest "
                    "FROM omnivia_engineering_dependencies WHERE version = ? "
                    "ORDER BY selector",
                    (source_version,),
                )
            ]
            forge(claims, rows)
            engineering_source._seal_dependency_set(
                connection,
                settlement,
                workspace_id=workspace_id,
                record_id=record_id,
                version=target_version,
                claims=claims,
                dependencies=rows,
                allocate_identifier=allocate_identifier,
            )
            return True

        return carry

    def changed_source(field: str) -> Any:
        original = governance_storage._source

        def source(*args: Any, **kwargs: Any) -> Any:
            found = original(*args, **kwargs)
            document = json.loads(getattr(found, field))
            (document["content"] if field == "claim_json" else document)["title"] = "Changed"
            text = to_canonical_json(document)
            if field == "content_json":
                return replace(found, content_json=text)
            return replace(
                found,
                claim_json=text,
                claim_digest=esc._sha(text),
                claim_byte_length=len(text.encode("utf-8")),
            )

        return source

    original_links = governance_storage._insert_evidence_links

    def other_evidence(connection: sqlite3.Connection, **kwargs: Any) -> None:
        original_links(connection, **{**kwargs, "evidence_ids": ("evd-other",)})

    def edit(index: int, column: int, value: Any) -> Callable[..., None]:
        def forge(claims: list[Any], rows: list[list[Any]]) -> None:
            (claims if index < 0 else rows[index])[column] = value

        return forge

    extra = ["whole_file", "src/extra.py", "must_match", "omnivia-dev-indexer", esc.AUTH_V1]
    carry = (engineering_source, "carry_dependency_set")
    cases = (
        ("coverage", *carry, forged_carry(edit(-1, 5, "complete"))),
        ("baseline", *carry, forged_carry(edit(-1, 2, "esnap-a2"))),
        ("producer", *carry, forged_carry(edit(-1, 4, "2.0.0"))),
        ("digest", *carry, forged_carry(edit(0, 4, esc.AUTH_V2))),
        # Rows are read by selector, so row 0 is the context-only README.
        ("meaning", *carry, forged_carry(edit(0, 2, "must_match"))),
        ("dropped", *carry, forged_carry(lambda _claims, rows: rows.pop())),
        ("extra", *carry, forged_carry(lambda _claims, rows: rows.append(list(extra)))),
        ("content", governance_storage, "_source", changed_source("content_json")),
        ("evidence", governance_storage, "_insert_evidence_links", other_evidence),
        ("claim", governance_storage, "_source", changed_source("claim_json")),
    )
    for name, module, attribute, replacement in cases:
        with monkeypatch.context() as patched:
            patched.setattr(module, attribute, replacement)
            refusals = _watch_carry(patched)
            outcome = _attempt(workspace, "knowledge.propose", created)
        assert not isinstance(outcome, SuccessResponseEnvelope), name
        expected = [] if name == "claim" else [True]
        assert [CARRY_REFUSAL in text for text in refusals] == expected, (name, refusals)
        assert _durable(workspace) == before, name

    # Unaltered, the same transition carries the set, still only as partial.
    proposed = _transition(workspace, "knowledge.propose", created)
    stored = _set(workspace, proposed)
    assert stored is not None and stored[5] == "partial"
    assert workspace.status(proposed, "esnap-a") == "unknown"


# --- damaged sources and other writers ---------------------------------------------


def test_a_tampered_source_set_is_never_carried(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A source whose stored rows no longer match its seal (damaged from outside
    the runtime, with the delete guard lifted and restored verbatim) carries
    nothing: the transition still settles and every new version stays `unknown`.
    Forcing the copy past the service check is refused by the 0051 guard."""
    workspace.record(esc._source(1, "esnap-a", FILES_A))
    created = workspace.observe(esc._observation(esc._manifest()))
    connection = workspace.holder.connection
    guard = "omnivia_guard_omnivia_engineering_dependencies_delete"
    (restore,) = connection.execute(
        "SELECT sql FROM sqlite_schema WHERE type = 'trigger' AND name = ?", (guard,)
    ).fetchone()
    with authorised(connection, ddl=True):
        connection.execute(f"DROP TRIGGER {guard}")
    try:
        with authorised(connection, mutations=True):
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "DELETE FROM omnivia_engineering_dependencies "
                "WHERE version = ? AND selector = 'README.md'",
                (created["version"],),
            )
            connection.execute("COMMIT")
    finally:
        with authorised(connection, ddl=True):
            connection.execute(restore)
    assert fingerprint_schema(connection).matches(canonical_schema_fingerprint())
    assert workspace.status(created, "esnap-a") == "unknown"

    before = _durable(workspace)

    def forced(
        connection: sqlite3.Connection, settlement: Any, **kwargs: Any
    ) -> bool:
        # The surviving rows restated under the seal's own count.
        _copy_set(
            connection,
            record_id=kwargs["record_id"],
            source_version=kwargs["source_version"],
            target_version=kwargs["target_version"],
            audit_ref=settlement.audit_ref,
            recorded_at_us=settlement.settled_at_us,
        )
        return True

    with monkeypatch.context() as patched:
        patched.setattr(engineering_source, "carry_dependency_set", forced)
        refusals = _watch_carry(patched)
        assert not isinstance(
            _attempt(workspace, "knowledge.propose", created), SuccessResponseEnvelope
        )
    assert len(refusals) == 1 and CARRY_REFUSAL in refusals[0]
    assert _durable(workspace) == before

    proposed = _transition(workspace, "knowledge.propose", created)
    accepted = _transition(workspace, "candidate.approve", proposed)
    for record in (proposed, accepted):
        assert _set(workspace, record) is None
        assert workspace.status(record, "esnap-a") == "unknown"
    after = _durable(workspace)
    assert after["omnivia_engineering_dependency_sets"] == 1
    assert after["omnivia_engineering_dependencies"] == len(esc.DEPENDENCIES) - 1
    assert workspace.matched("esnap-a", view="accepted") == []


def test_only_the_transition_itself_can_carry_a_set(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Direct SQL cannot mint a carry: not after the transition has settled, not
    under `candidate.reject`, `record.supersede` or a borrowed `memory.create`
    audit, and not from an unguarded connection."""
    workspace.record(esc._source(1, "esnap-a", FILES_A))
    connection = workspace.holder.connection

    # A transition that settled without its carry, as one before 0051 did.
    settled_source = _transition(
        workspace, "knowledge.propose", workspace.observe(esc._observation(esc._manifest()))
    )
    with monkeypatch.context() as patched:
        patched.setattr(engineering_source, "carry_dependency_set", lambda *_a, **_k: False)
        uncarried = _transition(workspace, "candidate.approve", settled_source)
    rejected_source = _transition(
        workspace,
        "knowledge.propose",
        workspace.observe(esc._observation(esc._manifest(), title="Rejected provider")),
    )
    rejected = _transition(workspace, "candidate.reject", rejected_source)
    fact = esc._observation(None, title="Fact provider")
    fact = {**fact, "record_type": "memory.fact", "content": {"fact": "Provider A."}}
    superseded = esc._supersede(
        workspace,
        esc._accept(workspace, workspace.observe(fact)),
        {**fact, "content": {"fact": "Provider B."}},
    )
    before = _durable(workspace)

    attempts = (
        (uncarried, settled_source, uncarried["version"], CARRY_REFUSAL),
        (rejected, rejected_source, rejected["version"], OWNER_REFUSAL),
        (superseded, settled_source, superseded["version"], OWNER_REFUSAL),
        # The proposal's own memory.create audit borrowed for the accepted version.
        (uncarried, settled_source, _memory_create_version(workspace, uncarried), OWNER_REFUSAL),
    )
    for target, source, audited_by, message in attempts:
        audit_ref, settled_at_us = _settlement(workspace, audited_by)
        with pytest.raises(sqlite3.DatabaseError, match=message), esc._fenced(workspace):
            _copy_set(
                connection,
                record_id=target["record_id"],
                source_version=source["version"],
                target_version=target["version"],
                audit_ref=audit_ref,
                recorded_at_us=settled_at_us,
            )
    # Outside the fenced transaction the service connection refuses the write.
    audit_ref, settled_at_us = _settlement(workspace, uncarried["version"])
    with pytest.raises(sqlite3.DatabaseError):
        _copy_set(
            connection,
            record_id=uncarried["record_id"],
            source_version=settled_source["version"],
            target_version=uncarried["version"],
            audit_ref=audit_ref,
            recorded_at_us=settled_at_us,
        )
    assert _durable(workspace) == before
    for record in (uncarried, rejected, superseded):
        assert _set(workspace, record) is None
    assert workspace.status(uncarried, "esnap-a") == "unknown"
    assert workspace.status(settled_source, "esnap-a") == "matched"


def _memory_create_version(workspace: Workspace, record: dict[str, str]) -> str:
    row = workspace.holder.connection.execute(
        "SELECT a.governed_record_version_id FROM omnivia_governed_version_assemblies a "
        "JOIN omnivia_application_claim_lineage l "
        "ON l.workspace_id = a.workspace_id AND l.assembly_id = a.assembly_id "
        "WHERE a.governed_record_id = ? AND l.operation = 'memory.create'",
        (record["record_id"],),
    ).fetchone()
    return str(row[0])


def test_a_carried_set_is_sealed_and_immutable(workspace: Workspace) -> None:
    workspace.record(esc._source(1, "esnap-a", FILES_A))
    accepted = esc._accept(workspace, workspace.observe(esc._observation(esc._manifest())))
    connection = workspace.holder.connection
    columns = (
        "workspace_id, dependency_id, record_id, version, selector_type, selector, "
        "meaning, producer, recorded_at_us, audit_ref, expected_digest"
    )
    row = connection.execute(
        f"SELECT {columns} FROM omnivia_engineering_dependencies WHERE version = ? LIMIT 1",
        (accepted["version"],),
    ).fetchone()
    forged = (row[0], "edep-forged", *row[2:5], "src/extra.py", *row[6:])
    with pytest.raises(sqlite3.DatabaseError, match="sealed"), esc._fenced(workspace):
        connection.execute(
            f"INSERT INTO omnivia_engineering_dependencies ({columns}) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            forged,
        )
    for statement in (
        "UPDATE omnivia_engineering_dependency_sets SET coverage = 'partial' WHERE version = ?",
        "DELETE FROM omnivia_engineering_dependency_sets WHERE version = ?",
        "UPDATE omnivia_engineering_dependencies SET expected_digest = NULL WHERE version = ?",
        "DELETE FROM omnivia_engineering_dependencies WHERE version = ?",
    ):
        with pytest.raises(sqlite3.DatabaseError, match="append-only"), esc._fenced(workspace):
            connection.execute(statement, (accepted["version"],))
    assert workspace.status(accepted, "esnap-a") == "matched"
    assert workspace.counts()["omnivia_engineering_dependencies"] == 3 * len(esc.DEPENDENCIES)


# --- reads -------------------------------------------------------------------------


def test_a_carried_set_never_crosses_streams_or_repositories(workspace: Workspace) -> None:
    workspace.record(esc._source(1, "esnap-a", FILES_A))
    workspace.record(esc._source(1, "esnap-w", FILES_A, stream="estream-worktree"))
    workspace.record(
        esc._source(
            1, "esnap-r", FILES_A, stream="estream-other-repo", repository="erepo-other"
        )
    )
    accepted = esc._accept(workspace, workspace.observe(esc._observation(esc._manifest())))
    assert workspace.status(accepted, "esnap-a") == "matched"
    assert workspace.status(accepted, "esnap-w") == "unknown"
    assert workspace.status(accepted, "esnap-r") == "unknown"
    assert workspace.matched("esnap-w", view="accepted") == []
    assert workspace.matched("esnap-a", view="accepted") == [accepted["record_id"]]


def test_a_carried_match_survives_restart_and_yields_to_revocation(
    workspace: Workspace,
) -> None:
    m2.write(workspace.holder, m2.EVIDENCE, evidence_id="evd-open", source_native_id="doc-open")
    workspace.record(esc._source(1, "esnap-a", FILES_A))
    open_source = {**esc.EVIDENCE_SOURCE, "source_id": "doc-open"}
    accepted = esc._accept(
        workspace, workspace.observe(esc._observation(esc._manifest(), source=open_source))
    )
    reader = esc._reader()
    build = {
        "query": "provider",
        "targets": [TARGET_A],
        "profile": "implement",
        "applicability_mode": "current_safe",
    }

    workspace.restart()
    assert workspace.status(accepted, "esnap-a") == "matched"
    assert workspace.matched("esnap-a", view="accepted", session=reader) == [
        accepted["record_id"]
    ]
    pack = workspace.ok("engineering.context.build", build, session=reader)["pack"]
    assert [c["record_ref"] for c in pack["citations"]] == [accepted]
    assert [s["partition"] for s in pack["sections"]] == ["accepted_knowledge"]

    # Revocation: the open evidence gains the restricted label.
    m2.write(
        workspace.holder,
        m2.LABELS,
        label_event_id="lbl-open",
        evidence_id="evd-open",
        label_sequence=1,
    )
    assert workspace.matched("esnap-a", view="accepted", session=reader) == []
    revoked = workspace.ok("engineering.context.build", build, session=reader)["pack"]
    assert revoked["sections"] == [] and revoked["citations"] == []
    # The carried set is untouched; only the reader's grant changed.
    assert workspace.status(accepted, "esnap-a") == "matched"


# --- the migration -----------------------------------------------------------------


def test_0051_replaces_one_guard_and_adds_nothing_else() -> None:
    """0051 drops and recreates exactly 0050's dependency-set INSERT guard under
    its own name, with no comment inside the body, so the real statement-splitting
    executor and SQLite's `executescript` store the same schema."""
    (migration,) = [m for m in load_migrations() if m.version == MIGRATION_VERSION]
    assert migration.name == MIGRATION_NAME
    statements = [" ".join(s.split()) for s in split_sql_statements(migration.sql)]
    assert statements[0] == f"DROP TRIGGER {GUARD}"
    assert statements[1].startswith(f"CREATE TRIGGER IF NOT EXISTS {GUARD} ")
    assert len(statements) == 2

    before = sqlite3.connect(":memory:")
    after = sqlite3.connect(":memory:")
    executor = sqlite3.connect(":memory:", isolation_level=None)
    try:
        for connection in (before, after):
            connection.executescript(phase0_baseline_sql())
        execute_script(executor, phase0_baseline_sql())
        for m in load_migrations():
            if m.version < MIGRATION_VERSION:
                before.executescript(m.sql)
            if m.version <= MIGRATION_VERSION:
                after.executescript(m.sql)
                execute_script(executor, m.sql)

        def objects(connection: sqlite3.Connection) -> set[tuple[str, str, str]]:
            return set(
                connection.execute(
                    "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
                )
            )

        assert {(kind, name) for kind, name, _ in objects(after) ^ objects(before)} == {
            ("trigger", GUARD)
        }
        (body,) = [sql for kind, name, sql in objects(after) if name == GUARD]
        assert "--" not in body
        assert fingerprint_schema(executor).matches(fingerprint_schema(after))
    finally:
        for connection in (before, after, executor):
            connection.close()


def test_0051_fresh_and_upgraded_workspaces_reach_one_canonical_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (migration,) = [m for m in load_migrations() if m.version == MIGRATION_VERSION]

    def verified(connection: sqlite3.Connection) -> None:
        assert applied_migrations(connection)[MIGRATION_VERSION] == migration.checksum
        assert fingerprint_schema(connection).matches(canonical_schema_fingerprint())
        assert_guards_intact(connection)
        assert integrity_check(connection) == []
        assert foreign_key_check(connection) == []

    fresh = tmp_path / "fresh.sqlite"
    m2.materialise_phase0_baseline(fresh)
    m2.bootstrap_and_migrate(fresh)
    connection = open_database(fresh, OpenMode.READ_ONLY)
    try:
        verified(connection)
    finally:
        connection.close()

    # Upgraded: a 0050 workspace already holding a proposal with a sealed set.
    (tmp_path / "upgraded").mkdir()
    with (
        monkeypatch.context() as older_release,
        m2.migration_catalogue_through(MIGRATION_VERSION - 1),
    ):
        # The release that wrote this workspace predates the preview projection
        # (0053), so its writers projected nothing; 0053's backfill covers the version.
        older_release.setattr(engineering_preview, "record_preview", lambda *_a, **_k: None)
        older_release.setattr(
            engineering_conflicts, "enqueue_discovery", lambda *_a, **_k: None
        )
        upgraded = Workspace(tmp_path / "upgraded")
        upgraded.record(esc._source(1, "esnap-a", FILES_A))
        created = upgraded.observe(esc._observation(esc._manifest()))
        assert MIGRATION_VERSION not in applied_migrations(upgraded.holder.connection)
        upgraded.holder.connection.close()
    # Exactly 0051 is applied, whatever successors the catalogue holds.
    with m2.migration_catalogue_through(MIGRATION_VERSION):
        maintenance = open_database(upgraded.holder.path, OpenMode.EXCLUSIVE_MAINTENANCE)
        try:
            state = read_workspace_state(maintenance)
            assert state is not None
            applied = apply_pending_migrations(
                maintenance,
                mode=OpenMode.EXCLUSIVE_MAINTENANCE,
                service_instance_id=m2.SERVICE_INSTANCE,
                fencing_generation=state.fencing_generation,
                workspace_id=WORKSPACE_ID,
            )
            assert [m.version for m in applied] == [MIGRATION_VERSION]
            verified(maintenance)
        finally:
            maintenance.close()

    # A real start migrates the workspace to head before it serves a read, so the
    # successors this test held back (0052's guard, 0053's preview projection) apply
    # to the pre-upgrade proposal before the service restarts on it.
    head = open_database(upgraded.holder.path, OpenMode.EXCLUSIVE_MAINTENANCE)
    try:
        state = read_workspace_state(head)
        assert state is not None
        applied_to_head = apply_pending_migrations(
            head,
            mode=OpenMode.EXCLUSIVE_MAINTENANCE,
            service_instance_id=m2.SERVICE_INSTANCE,
            fencing_generation=state.fencing_generation,
            workspace_id=WORKSPACE_ID,
        )
        assert [m.version for m in applied_to_head] == [
            m.version for m in load_migrations() if m.version > MIGRATION_VERSION
        ]
    finally:
        head.close()

    # The restarted service carries the pre-upgrade set through governance.
    upgraded.restart()
    try:
        assert upgraded.status(created, "esnap-a") == "matched"
        accepted = esc._accept(upgraded, created)
        assert upgraded.status(accepted, "esnap-a") == "matched"
        assert upgraded.matched("esnap-a", view="accepted") == [created["record_id"]]
    finally:
        upgraded.holder.connection.close()
