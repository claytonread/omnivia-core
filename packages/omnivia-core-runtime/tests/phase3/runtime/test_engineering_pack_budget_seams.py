"""Metadata-only governed payload planning (SPEC-CORE-ENGMEM-001 follow-up).

`plan_authorized_governed_payload` lets a caller already holding an
ACL-authorized, bounded selected assembly set -- and each selected assembly's
own authorized support closure -- learn exact UTF-8 byte lengths for its
content, transition rationale and claim support *before* any payload body is
read. These tests prove it reads byte metadata only, represents shared
support so a caller can sum an incremental cost without double-counting it,
and fails closed on a transition whose endpoint escapes its record's
authorized closure.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import test_engineering_source_coverage as sc
from omnivia_core_runtime.storage.governed import (
    GovernedPayloadComponents,
    plan_authorized_governed_payload,
)
from omnivia_core_runtime.storage.payload_budget import PayloadReadBudget

#: Every transition in these tests settles well before this instant.
FAR_FUTURE_US = 9_999_999_999_999_999

#: Non-ASCII on purpose: proves the plan counts encoded UTF-8 bytes, not
#: `len()` characters, for content whose multibyte sequences differ from its
#: character count.
MULTILINGUAL_TITLE = "契約の決定 🔥 Décision café naïve"


@pytest.fixture
def workspace(tmp_path: Path) -> Any:
    opened = sc.Workspace(tmp_path)
    yield opened
    opened.holder.connection.close()


def _propose(workspace: sc.Workspace, record: dict[str, str]) -> dict[str, str]:
    from omnivia_core.contracts.v1 import MutationPrecondition

    result = workspace.ok(
        "knowledge.propose",
        {"record_id": record["record_id"], "rationale": {"reason_code": "review"}},
        mutation_precondition=MutationPrecondition(record_version=record["version"]),
    )
    identity = result["updated_record"]["provenance"]["identity"]
    return {"record_id": identity["record_id"], "version": identity["version"]}


def _assemblies(
    workspace: sc.Workspace, record_id: str
) -> list[tuple[str, str, str]]:
    """`(assembly_id, governed_record_version_id, content_json)`, in append order."""
    rows = workspace.holder.connection.execute(
        "SELECT assembly_id, governed_record_version_id, content_json "
        "FROM omnivia_governed_version_assemblies "
        "WHERE workspace_id = ? AND governed_record_id = ? ORDER BY append_ordinal",
        (sc.WORKSPACE_ID, record_id),
    ).fetchall()
    return [(str(row[0]), str(row[1]), str(row[2])) for row in rows]


def _claim_byte_length(workspace: sc.Workspace, assembly_id: str) -> int:
    row = workspace.holder.connection.execute(
        "SELECT claim_byte_length FROM omnivia_application_claim_lineage "
        "WHERE workspace_id = ? AND assembly_id = ?",
        (sc.WORKSPACE_ID, assembly_id),
    ).fetchone()
    assert row is not None
    return int(row[0])


def _transitions(
    workspace: sc.Workspace, record_id: str
) -> list[tuple[str, str, str, int]]:
    """`(transition_id, source_assembly_id, target_assembly_id, rationale_byte_length)`."""
    rows = workspace.holder.connection.execute(
        "SELECT transition_id, source_assembly_id, target_assembly_id, "
        "rationale_byte_length FROM omnivia_application_governance_transitions "
        "WHERE workspace_id = ? AND governed_record_id = ? ORDER BY settled_at_us",
        (sc.WORKSPACE_ID, record_id),
    ).fetchall()
    return [(str(row[0]), str(row[1]), str(row[2]), int(row[3])) for row in rows]


def _plan(
    workspace: sc.Workspace,
    authorized_support: dict[str, tuple[str, ...]],
) -> GovernedPayloadComponents:
    connection = workspace.holder.connection
    connection.execute("BEGIN")
    try:
        return plan_authorized_governed_payload(
            connection,
            workspace_id=sc.WORKSPACE_ID,
            resolution_instant_us=FAR_FUTURE_US,
            authorized_support=authorized_support,
            payload_budget=PayloadReadBudget(limit=10_000_000),
        )
    finally:
        connection.execute("COMMIT")


def _traced_plan(
    workspace: sc.Workspace,
    authorized_support: dict[str, tuple[str, ...]],
) -> tuple[GovernedPayloadComponents, list[str]]:
    statements: list[str] = []
    workspace.holder.connection.set_trace_callback(statements.append)
    try:
        components = _plan(workspace, authorized_support)
    finally:
        workspace.holder.connection.set_trace_callback(None)
    return components, statements


def _never_selects_body(statements: list[str], column: str) -> bool:
    """Whether `column` appears in `statements` only wrapped by the byte-length
    projection (`octet_length(...)` or `length(CAST(... AS BLOB))`), never as a
    bare selected value SQLite would return to the caller."""
    for statement in statements:
        index = 0
        while True:
            index = statement.find(column, index)
            if index == -1:
                break
            before = statement[:index].rstrip()
            if not before.endswith(("octet_length(", "CAST(")):
                return False
            index += len(column)
    return True


def test_plan_reads_exact_multilingual_byte_lengths_without_selecting_bodies(
    workspace: sc.Workspace,
) -> None:
    created = workspace.observe(sc._observation(None, title=MULTILINGUAL_TITLE))
    proposed = _propose(workspace, created)
    assemblies = _assemblies(workspace, created["record_id"])
    assert [version for _asm, version, _content in assemblies] == [
        created["version"],
        proposed["version"],
    ]
    created_asm, proposed_asm = (assembly_id for assembly_id, _v, _c in assemblies)
    authorized_support = {created_asm: (), proposed_asm: ()}

    components, statements = _traced_plan(workspace, authorized_support)

    expected_content = {
        assembly_id: len(content_json.encode("utf-8"))
        for assembly_id, _version, content_json in assemblies
    }
    # The multibyte title makes this a non-trivial proof: the stored JSON's
    # byte length exceeds its character count.
    assert expected_content[created_asm] > len(assemblies[0][2])
    assert dict(components.content_byte_lengths) == expected_content

    (transition,) = _transitions(workspace, created["record_id"])
    transition_id, source_asm, target_asm, rationale_bytes = transition
    assert (source_asm, target_asm) == (created_asm, proposed_asm)
    assert dict(components.transition_rationale_byte_lengths) == {
        transition_id: rationale_bytes
    }
    assert dict(components.claim_byte_lengths) == {
        created_asm: _claim_byte_length(workspace, created_asm),
        proposed_asm: _claim_byte_length(workspace, proposed_asm),
    }

    assert _never_selects_body(statements, "content_json")
    assert not any("rationale_json" in statement for statement in statements)
    assert not any("claim_json" in statement for statement in statements)


def test_shared_support_is_identity_keyed_not_double_counted(
    workspace: sc.Workspace,
) -> None:
    created = workspace.observe(sc._observation(None, title="Shared support chain"))
    accepted = sc._accept(workspace, created)
    assemblies = _assemblies(workspace, created["record_id"])
    assert len(assemblies) == 3
    created_asm, candidate_asm, accepted_asm = (
        assembly_id for assembly_id, _v, _c in assemblies
    )
    assert accepted["version"] == assemblies[2][1]

    # Both selected versions' closures name the same distant ancestor
    # (`created_asm`) as authorized support -- the shape a caller reaches for
    # when two selected versions of one record share part of their chain.
    authorized_support = {
        candidate_asm: (created_asm,),
        accepted_asm: (created_asm, candidate_asm),
    }
    components = _plan(workspace, authorized_support)

    distinct_claim_assemblies = {created_asm, candidate_asm, accepted_asm}
    assert set(components.claim_byte_lengths) == distinct_claim_assemblies
    assert len(components.claim_byte_lengths) == 3

    # Summing each selected key's own closure separately double-counts
    # `created_asm`: it is named by both. The identity-keyed result does not.
    naive_per_record_count = sum(
        len({assembly_id, *support})
        for assembly_id, support in authorized_support.items()
    )
    assert naive_per_record_count == 5
    assert len(components.claim_byte_lengths) < naive_per_record_count

    expected_total = sum(
        _claim_byte_length(workspace, assembly_id)
        for assembly_id in distinct_claim_assemblies
    )
    assert sum(components.claim_byte_lengths.values()) == expected_total

    transitions = _transitions(workspace, created["record_id"])
    assert len(transitions) == 2
    assert set(components.transition_rationale_byte_lengths) == {
        transition_id for transition_id, *_rest in transitions
    }


def test_transition_endpoint_outside_the_closure_fails_closed(
    workspace: sc.Workspace,
) -> None:
    created = workspace.observe(sc._observation(None, title="Unauthorized chain"))
    sc._accept(workspace, created)
    assemblies = _assemblies(workspace, created["record_id"])
    _created_asm, _candidate_asm, accepted_asm = (
        assembly_id for assembly_id, _v, _c in assemblies
    )

    # Only the accepted assembly is authorized; its own transition's source
    # (the candidate assembly) is not in the closure at all.
    authorized_support = {accepted_asm: ()}
    with pytest.raises(ValueError, match="not ACL-authorized"):
        _plan(workspace, authorized_support)


def test_support_from_another_record_cannot_enter_the_group(
    workspace: sc.Workspace,
) -> None:
    first = workspace.observe(sc._observation(None, title="First record"))
    second = workspace.observe(sc._observation(None, title="Second record"))
    (first_assembly, _first_version, _first_content), = _assemblies(
        workspace, first["record_id"]
    )
    (second_assembly, _second_version, _second_content), = _assemblies(
        workspace, second["record_id"]
    )

    with pytest.raises(ValueError, match="crossed an authorized record closure"):
        _plan(workspace, {first_assembly: (second_assembly,)})


def test_planning_requires_the_callers_active_read_transaction(
    workspace: sc.Workspace,
) -> None:
    created = workspace.observe(sc._observation(None, title="No transaction"))
    (assembly_id, _version, _content), = _assemblies(workspace, created["record_id"])
    assert workspace.holder.connection.in_transaction is False
    with pytest.raises(ValueError, match="active read snapshot"):
        plan_authorized_governed_payload(
            workspace.holder.connection,
            workspace_id=sc.WORKSPACE_ID,
            resolution_instant_us=FAR_FUTURE_US,
            authorized_support={assembly_id: ()},
            payload_budget=PayloadReadBudget(limit=10_000_000),
        )
