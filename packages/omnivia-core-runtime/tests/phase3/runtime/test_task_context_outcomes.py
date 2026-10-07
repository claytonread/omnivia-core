"""DEV-REQ-159 and DEV-REQ-008: task-context exports and outcome requests, domain rules and migration 0068.

The domain half drives `service/task_context.py` directly on a handoff shaped like the Dev assembler's, so
every refusal is a closed reason and every identity is checked against what the handoff records. The
storage half runs against a real migrated workspace through the guarded write path, so the append-only
triggers, the current-fence guards and the workspace binding are the ones a service process meets. A row
is altered only by dropping its update guard on a separate connection, the way a tamperer with file access
would, so the read-side identity check is what catches it.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import test_application_audit_idempotency_migration as m1
from omnivia_core_runtime.ownership.fencing import fenced_transaction
from omnivia_core_runtime.service.task_context import (
    MAX_BYTE_BUDGET,
    MAX_TOKEN_BUDGET,
    POLICY_DIGEST,
    REFUSED_BUDGET_INSUFFICIENT,
    REFUSED_BUDGET_INVALID,
    REFUSED_HANDOFF_INVALID,
    REFUSED_HANDOFF_MISSING,
    REFUSED_INELIGIBLE,
    REFUSED_NOT_FOUND,
    REFUSED_OBJECTIVE_INVALID,
    REFUSED_OBJECTIVE_UNBOUNDED,
    REFUSED_SIZE_EXCEEDED,
    REFUSED_STALE_FENCE,
    TaskContextRefused,
    build_export,
    build_outcome_request,
    handoff_identity,
    validate_objective,
    verify_handoff,
)
from omnivia_core_runtime.storage.migrations import load_migrations
from omnivia_core_runtime.storage.task_context import (
    StoredExport,
    StoredOutcomeRequest,
    TaskContextInvalid,
    read_export,
    read_outcome_request,
    record_export,
    record_outcome_request,
)

WS = "ws-task-context-0001"
PRINCIPAL = "principal-task-context"
GENERATION = 1
CREATED = 1_800_000_000_000_000
BYTE_BIG = MAX_BYTE_BUDGET
TOKEN_BIG = MAX_TOKEN_BUDGET
REFUSED_EXTERNAL_WRITE = "unguarded|no such function|from outside the runtime|append-only|not authorized"


# -- handoffs and exports -----------------------------------------------------------------


def sealed(body: dict[str, Any]) -> dict[str, Any]:
    """A handoff whose recorded identity is the one its content recomputes to."""
    body["contentIdentity"] = handoff_identity(body)
    return body


def handoff(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "adapter": "task-context-handoff",
        "operation": "task_context.assemble",
        "project": "project-alpha",
        "target": "service/core",
        "revision": "r1",
        "objective": "Ship the export projection",
        "constraints": "Keep the pipeline quiet",
        "plan": "Step one. Step two.",
        "completedChanges": "Added storage.",
        "currentResults": "Tests green.",
        "generationId": "gen-1",
        "assembledAt": "2026-10-04T00:00:00Z",
        "omissions": [],
        "omissionOverflow": 0,
        "tokenBudget": 4000,
        "byteBudget": 16000,
        "tokenEstimator": "utf8-ceil4-v1",
        "contentIdentity": "0" * 64,
        "tokenEstimate": 100,
        "byteEstimate": 400,
        "sourceReferences": [],
        "truncated": False,
    }
    body.update(overrides)
    return sealed(body)


def export_of(
    body: dict[str, Any] | None = None,
    *,
    token_budget: int = TOKEN_BIG,
    byte_budget: int = BYTE_BIG,
    generation: int = GENERATION,
    principal: str = PRINCIPAL,
    workspace: str = WS,
    created: int = CREATED,
) -> StoredExport:
    return build_export(
        workspace_id=workspace,
        principal=principal,
        fencing_generation=generation,
        handoff=handoff() if body is None else body,
        token_budget=token_budget,
        byte_budget=byte_budget,
        created_at_us=created,
    )


def admitted(*, token_budget: int = TOKEN_BIG, byte_budget: int = BYTE_BIG) -> StoredExport | None:
    """The export if its budgets admit it, `None` if they refuse it as oversize, and any other refusal raised."""
    try:
        return export_of(token_budget=token_budget, byte_budget=byte_budget)
    except TaskContextRefused as refusal:
        if refusal.reason == REFUSED_SIZE_EXCEEDED:
            return None
        raise


def refusal_reason(action: Callable[[], object]) -> str:
    with pytest.raises(TaskContextRefused) as caught:
        action()
    return caught.value.reason


# -- Dev-compatible handoff verification --------------------------------------------------


def test_a_dev_shaped_handoff_verifies_to_the_identity_it_records() -> None:
    body = handoff()
    identity = verify_handoff(body)
    assert identity == body["contentIdentity"] == handoff_identity(body)
    assert len(identity) == 64


def test_only_content_moves_the_handoff_identity_and_volatile_fields_do_not() -> None:
    base = handoff()["contentIdentity"]
    assert handoff(assembledAt="2030-01-01T00:00:00Z")["contentIdentity"] == base
    assert handoff(tokenEstimate=1, byteEstimate=1, tokenBudget=1, byteBudget=1)["contentIdentity"] == base
    assert handoff(truncated=True)["contentIdentity"] == base
    assert handoff(plan="A different plan.")["contentIdentity"] != base


@pytest.mark.parametrize("missing", [None, {}, "not a mapping", {"refused": True}])
def test_a_missing_or_refused_handoff_is_handoff_missing(missing: object) -> None:
    assert refusal_reason(lambda: verify_handoff(missing)) == REFUSED_HANDOFF_MISSING


def _without(key: str) -> dict[str, Any]:
    body = handoff()
    del body[key]
    return body


def _with_reference(reference: dict[str, Any]) -> dict[str, Any]:
    return handoff(sourceReferences=[reference])


CONTEXT_PACK = {"kind": "context-pack", "packId": "pack-1", "captureAuthorization": "a" * 64}
SOURCE_MAP = {
    "kind": "task-source-map",
    "generationId": None,
    "path": "src/a.py",
    "relationship": "reads",
    "reason": "in scope",
    "confidence": 0.5,
}
OMISSION = {"kind": "source", "identifier": None, "reason": "budget"}


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({**handoff(), "extra": 1}, id="extra-key"),
        pytest.param(_without("truncated"), id="missing-key"),
        pytest.param(handoff(adapter="some-other-adapter"), id="wrong-adapter"),
        pytest.param(handoff(operation="task_context.other"), id="wrong-operation"),
        pytest.param(handoff(tokenEstimator="bytes"), id="wrong-estimator"),
        pytest.param(handoff(tokenBudget=True), id="boolean-budget"),
        pytest.param(handoff(truncated=1), id="integer-truncated"),
        pytest.param(handoff(omissions=[OMISSION] * 21), id="too-many-omissions"),
        pytest.param(handoff(omissions=[{**OMISSION, "extra": 1}]), id="omission-extra-key"),
        pytest.param(_with_reference({**CONTEXT_PACK, "extra": 1}), id="reference-extra-key"),
        pytest.param(_with_reference({"kind": "unknown"}), id="reference-unknown-kind"),
        pytest.param(_with_reference({**SOURCE_MAP, "path": None}), id="reference-bad-path"),
        pytest.param({**handoff(), "contentIdentity": "not-hex"}, id="identity-not-hex"),
    ],
)
def test_a_handoff_outside_its_closed_shape_is_refused(body: dict[str, Any]) -> None:
    assert refusal_reason(lambda: verify_handoff(body)) == REFUSED_HANDOFF_INVALID


def test_a_handoff_whose_content_changed_without_recomputing_is_refused() -> None:
    body = handoff()
    body["objective"] = "Quietly changed after sealing"
    assert refusal_reason(lambda: verify_handoff(body)) == REFUSED_HANDOFF_INVALID
    assert refusal_reason(lambda: export_of(body)) == REFUSED_HANDOFF_INVALID


def test_a_refused_handoff_produces_no_export() -> None:
    reason = refusal_reason(
        lambda: build_export(
            workspace_id=WS,
            principal=PRINCIPAL,
            fencing_generation=GENERATION,
            handoff={"refused": True},
            token_budget=TOKEN_BIG,
            byte_budget=BYTE_BIG,
            created_at_us=CREATED,
        )
    )
    assert reason == REFUSED_HANDOFF_MISSING


# -- budgets: explicit, validated, never truncated ----------------------------------------


@pytest.mark.parametrize(
    ("token_budget", "byte_budget"),
    [(0, BYTE_BIG), (TOKEN_BIG + 1, BYTE_BIG), (True, BYTE_BIG), (TOKEN_BIG, 0), (TOKEN_BIG, BYTE_BIG + 1), (TOKEN_BIG, 1.5)],
)
def test_an_out_of_bounds_or_non_integer_budget_is_budget_invalid(token_budget: Any, byte_budget: Any) -> None:
    assert refusal_reason(lambda: export_of(token_budget=token_budget, byte_budget=byte_budget)) == REFUSED_BUDGET_INVALID


def test_a_budget_too_small_for_the_export_envelope_is_insufficient() -> None:
    assert refusal_reason(lambda: export_of(byte_budget=10)) == REFUSED_BUDGET_INSUFFICIENT
    assert refusal_reason(lambda: export_of(token_budget=1)) == REFUSED_BUDGET_INSUFFICIENT


def test_the_byte_budget_admits_its_export_exactly_and_refuses_one_byte_less() -> None:
    size = export_of().columns()["byte_estimate"]
    exact = min(
        budget for budget in range(size - 8, size + 9) if admitted(byte_budget=budget) is not None
    )
    fitted = admitted(byte_budget=exact)
    assert fitted is not None
    assert fitted.columns()["byte_estimate"] <= exact
    assert admitted(byte_budget=exact - 1) is None
    assert refusal_reason(lambda: export_of(byte_budget=exact - 1)) == REFUSED_SIZE_EXCEEDED


def test_the_token_budget_admits_its_export_exactly_and_refuses_one_token_less() -> None:
    tokens = export_of().columns()["token_estimate"]
    exact = min(
        budget
        for budget in range(tokens - 4, tokens + 5)
        if admitted(token_budget=budget) is not None
    )
    assert admitted(token_budget=exact) is not None
    assert admitted(token_budget=exact - 1) is None


# -- fixed projection: allowlisted content, redaction and withheld fields -----------------


def test_the_export_carries_only_the_allowlisted_content_redacted_by_the_fixed_patterns() -> None:
    body = handoff(
        objective="Contact owner@example.com about it",
        plan="Use Bearer abc.DEF-123 to call the API",
        completedChanges="password=hunter2 was rotated",
        constraints="SECRET-CONSTRAINT-TEXT must not leave",
        assembledAt="2026-10-04T09:09:09Z",
    )
    export = export_of(body)
    document = export.document

    assert document["content"]["objective"] == "Contact [redacted:email] about it"
    assert document["content"]["plan"] == "Use [redacted:bearer] to call the API"
    assert document["content"]["completedChanges"] == "[redacted:secret_assignment] was rotated"
    assert document["redactions"] == [
        {"pattern": "bearer", "count": 1},
        {"pattern": "email", "count": 1},
        {"pattern": "secret_assignment", "count": 1},
    ]
    assert set(document["content"]) == {
        "project",
        "target",
        "revision",
        "objective",
        "plan",
        "completedChanges",
        "currentResults",
        "sourceReferences",
        "generationId",
    }
    assert "constraints" in document["withheldFields"]
    assert "assembledAt" in document["withheldFields"]
    assert "SECRET-CONSTRAINT-TEXT" not in export.document_json
    assert "2026-10-04T09:09:09Z" not in export.document_json
    assert "owner@example.com" not in export.document_json
    assert "hunter2" not in export.document_json


# -- objective: verbatim, non-empty, bounded in UTF-8 bytes -------------------------------


@pytest.mark.parametrize("objective", ["é" * 4096, "🙂" * 2048, "a"])
def test_an_objective_at_or_under_its_byte_bound_is_kept_verbatim(objective: str) -> None:
    assert validate_objective(objective) == objective


@pytest.mark.parametrize("objective", ["é" * 4097, "€" * 2731])
def test_the_objective_bound_counts_utf8_bytes_not_characters(objective: str) -> None:
    assert len(objective.encode("utf-8")) > 8192
    assert refusal_reason(lambda: validate_objective(objective)) == REFUSED_OBJECTIVE_UNBOUNDED


def test_an_objective_at_the_byte_limit_with_multibyte_characters_is_admitted() -> None:
    assert len(("€" * 2730 + "aa").encode("utf-8")) == 8192
    assert validate_objective("€" * 2730 + "aa")


@pytest.mark.parametrize("objective", ["", "   \n", "\ud800", 42, None])
def test_an_empty_unencodable_or_non_text_objective_is_refused(objective: object) -> None:
    assert refusal_reason(lambda: validate_objective(objective)) == REFUSED_OBJECTIVE_INVALID


# -- content addressing -------------------------------------------------------------------


def test_an_export_is_content_addressed_over_exactly_the_bytes_it_stores() -> None:
    export = export_of()
    columns = export.columns()
    digest = hashlib.sha256(export.document_json.encode("utf-8")).hexdigest()
    assert columns["content_identity"] == digest
    assert columns["export_id"] == f"tcx-{digest}"
    assert columns["byte_estimate"] == len(export.document_json.encode("utf-8"))
    assert columns["token_estimate"] == -(-columns["byte_estimate"] // 4)


def test_identical_inputs_name_the_identical_export_and_key_order_does_not_matter() -> None:
    first = export_of()
    assert export_of().export_id == first.export_id
    assert export_of(dict(reversed(list(handoff().items())))).export_id == first.export_id
    assert export_of(handoff(assembledAt="2031-05-05T05:05:05Z")).export_id == first.export_id


@pytest.mark.parametrize(
    "variant",
    [
        pytest.param({"principal": "someone-else"}, id="principal"),
        pytest.param({"generation": GENERATION + 1}, id="fence"),
        pytest.param({"workspace": "ws-task-context-0002"}, id="workspace"),
        pytest.param({"body": handoff(plan="Different plan.")}, id="content"),
    ],
)
def test_a_different_principal_fence_workspace_or_content_names_a_different_export(variant: dict[str, Any]) -> None:
    assert export_of(**variant).export_id != export_of().export_id


def test_a_handoff_identity_and_policy_are_carried_into_the_export() -> None:
    body = handoff()
    columns = export_of(body).columns()
    assert columns["source_handoff_identity"] == body["contentIdentity"]
    assert columns["policy_digest"] == POLICY_DIGEST
    assert columns["exported_by"] == PRINCIPAL
    assert columns["fencing_generation"] == GENERATION
    assert columns["token_budget"] == TOKEN_BIG
    assert columns["byte_budget"] == BYTE_BIG


# -- outcome request domain: foreign, stale and ineligible exports ------------------------


def _request(export: StoredExport, **overrides: Any) -> StoredOutcomeRequest:
    fields: dict[str, Any] = {
        "workspace_id": WS,
        "principal": PRINCIPAL,
        "objective": "Summarise the decision",
        "export": export,
        "current_generation": GENERATION,
        "created_at_us": CREATED,
    }
    fields.update(overrides)
    return build_outcome_request(**fields)


def test_an_outcome_request_names_its_export_and_its_identity_is_content_addressed() -> None:
    export = export_of()
    request = _request(export)
    assert request.export_id == export.export_id
    assert request.source_handoff_identity == export.columns()["source_handoff_identity"]
    assert request.requested_by == PRINCIPAL
    assert request.status == "received"
    assert request.outcome_request_id == _request(export).outcome_request_id
    assert request.outcome_request_id.startswith("outreq-")
    assert len(request.outcome_request_id) == 71
    assert _request(export, objective="Another objective").outcome_request_id != request.outcome_request_id
    assert _request(export, principal="someone-else").outcome_request_id != request.outcome_request_id


def test_an_export_from_another_workspace_is_not_found_for_an_outcome_request() -> None:
    export = export_of(workspace="ws-task-context-0002")
    assert refusal_reason(lambda: _request(export)) == REFUSED_NOT_FOUND


def test_an_export_recorded_under_an_earlier_fence_is_refused_as_stale() -> None:
    export = export_of(generation=GENERATION)
    assert refusal_reason(lambda: _request(export, current_generation=GENERATION + 1)) == REFUSED_STALE_FENCE


def test_an_export_produced_under_another_policy_is_ineligible() -> None:
    document = export_of().document
    document["policyDigest"] = "f" * 64
    export = StoredExport.of(document, CREATED)
    assert refusal_reason(lambda: _request(export)) == REFUSED_INELIGIBLE


# -- storage: migration 0068 through the guarded write path -------------------------------


@pytest.fixture
def owned(tmp_path: Path) -> Iterator[m1.Owned]:
    path = tmp_path / "workspace.sqlite"
    m1.materialise_phase0_baseline(path)
    m1.bootstrap_and_migrate(path, workspace_id=WS)
    holder = m1.take_ownership(path, workspace_id=WS)
    yield holder
    holder.connection.close()


def _restart(holder: m1.Owned) -> m1.Owned:
    holder.connection.close()
    return m1.take_ownership(holder.path, workspace_id=WS)


def _fenced(holder: m1.Owned, action: Callable[[sqlite3.Connection], object]) -> object:
    with fenced_transaction(
        holder.connection,
        holder.identity,
        workspace_id=WS,
        fencing_generation=holder.generation,
    ) as connection:
        return action(connection)


def _count(holder: m1.Owned, table: str) -> int:
    return int(holder.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _raw(path: Path, *statements: str) -> None:
    """Run statements on a separate connection with no service authority, as a file-level tamperer would."""
    connection = sqlite3.connect(path)
    try:
        for statement in statements:
            connection.execute(statement)
        connection.commit()
    finally:
        connection.close()


def test_an_export_and_its_request_survive_a_restart_with_identical_content(owned: m1.Owned) -> None:
    export = export_of(generation=owned.generation)
    request = _request(export, current_generation=owned.generation)
    _fenced(owned, lambda c: record_export(c, export))
    _fenced(owned, lambda c: record_outcome_request(c, request))

    restarted = _restart(owned)
    assert restarted.generation > owned.generation
    stored = read_export(restarted.connection, workspace_id=WS, export_id=export.export_id)
    assert stored == export
    assert stored is not None and stored.document_json == export.document_json
    assert read_outcome_request(
        restarted.connection, workspace_id=WS, outcome_request_id=request.outcome_request_id
    ) == request
    restarted.connection.close()


def test_replaying_identical_content_returns_the_stored_row_and_keeps_its_first_instant(owned: m1.Owned) -> None:
    first = export_of(generation=owned.generation)
    _fenced(owned, lambda c: record_export(c, first))
    replay = export_of(generation=owned.generation, created=CREATED + 5_000_000)
    returned = _fenced(owned, lambda c: record_export(c, replay))
    assert returned == first
    assert _count(owned, "omnivia_task_context_exports") == 1


@pytest.mark.parametrize(
    "tamper",
    [
        pytest.param("UPDATE omnivia_task_context_exports SET exported_by = 'mallory'", id="column"),
        pytest.param(
            "UPDATE omnivia_task_context_exports SET document_json = replace(document_json, 'Ship', 'Stop')",
            id="document",
        ),
    ],
)
def test_an_export_row_altered_at_rest_reads_as_invalid(owned: m1.Owned, tamper: str) -> None:
    export = export_of(generation=owned.generation)
    _fenced(owned, lambda c: record_export(c, export))
    path = owned.path
    owned.connection.close()
    _raw(path, "DROP TRIGGER omnivia_guard_task_context_exports_update", tamper)

    reader = sqlite3.connect(path)
    try:
        with pytest.raises(TaskContextInvalid):
            read_export(reader, workspace_id=WS, export_id=export.export_id)
    finally:
        reader.close()


def test_an_outcome_request_row_altered_at_rest_reads_as_invalid(owned: m1.Owned) -> None:
    export = export_of(generation=owned.generation)
    request = _request(export, current_generation=owned.generation)
    _fenced(owned, lambda c: record_export(c, export))
    _fenced(owned, lambda c: record_outcome_request(c, request))
    path = owned.path
    owned.connection.close()
    _raw(
        path,
        "DROP TRIGGER omnivia_guard_outcome_requests_update",
        "UPDATE omnivia_outcome_requests SET objective = 'Something else entirely'",
    )

    reader = sqlite3.connect(path)
    try:
        with pytest.raises(TaskContextInvalid):
            read_outcome_request(reader, workspace_id=WS, outcome_request_id=request.outcome_request_id)
    finally:
        reader.close()


def test_writes_outside_the_fence_are_refused_for_both_tables(owned: m1.Owned) -> None:
    export = export_of(generation=owned.generation)
    columns = export.columns()
    names = ", ".join(columns)
    holders = ", ".join(":" + name for name in columns)
    with pytest.raises(sqlite3.DatabaseError, match=REFUSED_EXTERNAL_WRITE):
        owned.connection.execute(
            f"INSERT INTO omnivia_task_context_exports ({names}) VALUES ({holders})", columns
        )
    request = _request(export, current_generation=owned.generation)
    with pytest.raises(sqlite3.DatabaseError, match=REFUSED_EXTERNAL_WRITE):
        owned.connection.execute(
            "INSERT INTO omnivia_outcome_requests (workspace_id, outcome_request_id, requested_by, objective, "
            "export_id, source_handoff_identity, status, fencing_generation, created_at_us) "
            "VALUES (:workspace_id, :outcome_request_id, :requested_by, :objective, :export_id, "
            ":source_handoff_identity, :status, :fencing_generation, :created_at_us)",
            request.columns(),
        )
    assert _count(owned, "omnivia_task_context_exports") == 0
    assert _count(owned, "omnivia_outcome_requests") == 0


def test_exports_and_requests_are_append_only_even_inside_the_fence(owned: m1.Owned) -> None:
    export = export_of(generation=owned.generation)
    request = _request(export, current_generation=owned.generation)
    _fenced(owned, lambda c: record_export(c, export))
    _fenced(owned, lambda c: record_outcome_request(c, request))
    for statement in (
        "UPDATE omnivia_task_context_exports SET exported_by = 'x'",
        "DELETE FROM omnivia_task_context_exports",
        "UPDATE omnivia_outcome_requests SET status = 'received'",
        "DELETE FROM omnivia_outcome_requests",
    ):
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            _fenced(owned, lambda c, s=statement: c.execute(s))
    assert _count(owned, "omnivia_task_context_exports") == 1
    assert _count(owned, "omnivia_outcome_requests") == 1


@pytest.mark.parametrize(
    ("document_key", "document_value", "message"),
    [
        pytest.param("fencingGeneration", GENERATION + 41, "current fencing generation", id="stale-fence"),
        pytest.param("workspaceId", "ws-task-context-0002", "open workspace", id="foreign-workspace"),
    ],
)
def test_an_export_must_bind_the_open_workspace_and_current_fence(
    owned: m1.Owned, document_key: str, document_value: object, message: str
) -> None:
    document = export_of(generation=owned.generation).document
    document[document_key] = document_value
    bad = StoredExport.of(document, CREATED)
    with pytest.raises(sqlite3.IntegrityError, match=message):
        _fenced(owned, lambda c: record_export(c, bad))
    assert _count(owned, "omnivia_task_context_exports") == 0


def test_an_outcome_request_for_a_stale_fence_export_is_refused_by_the_guard_after_a_restart(owned: m1.Owned) -> None:
    export = export_of(generation=owned.generation)
    _fenced(owned, lambda c: record_export(c, export))
    restarted = _restart(owned)

    assert refusal_reason(
        lambda: _request(export, current_generation=restarted.generation)
    ) == REFUSED_STALE_FENCE
    stale = StoredOutcomeRequest(
        workspace_id=WS,
        requested_by=PRINCIPAL,
        objective="Summarise the decision",
        export_id=export.export_id,
        source_handoff_identity=export.columns()["source_handoff_identity"],
        fencing_generation=restarted.generation,
        created_at_us=CREATED,
    )
    with pytest.raises(sqlite3.IntegrityError, match="current fencing generation"):
        _fenced(restarted, lambda c: record_outcome_request(c, stale))
    assert _count(restarted, "omnivia_outcome_requests") == 0
    restarted.connection.close()


@pytest.mark.parametrize(
    ("export_id", "source_handoff", "message"),
    [
        pytest.param("tcx-" + "a" * 64, "b" * 64, "must name an export|FOREIGN KEY", id="missing-export"),
        pytest.param(None, "c" * 64, "must name an export", id="wrong-source-handoff"),
    ],
)
def test_an_outcome_request_must_name_a_recorded_export_with_its_source(
    owned: m1.Owned, export_id: str | None, source_handoff: str, message: str
) -> None:
    export = export_of(generation=owned.generation)
    _fenced(owned, lambda c: record_export(c, export))
    request = _request(export, current_generation=owned.generation)
    bad = StoredOutcomeRequest(
        workspace_id=WS,
        requested_by=PRINCIPAL,
        objective=request.objective,
        export_id=export.export_id if export_id is None else export_id,
        source_handoff_identity=source_handoff,
        fencing_generation=owned.generation,
        created_at_us=CREATED,
    )
    with pytest.raises(sqlite3.IntegrityError, match=message):
        _fenced(owned, lambda c: record_outcome_request(c, bad))
    assert _count(owned, "omnivia_outcome_requests") == 0


def test_a_recorded_outcome_request_replays_to_the_stored_row(owned: m1.Owned) -> None:
    export = export_of(generation=owned.generation)
    request = _request(export, current_generation=owned.generation)
    _fenced(owned, lambda c: record_export(c, export))
    first = _fenced(owned, lambda c: record_outcome_request(c, request))
    again = _fenced(owned, lambda c: record_outcome_request(c, request))
    assert first == request == again
    assert _count(owned, "omnivia_outcome_requests") == 1


# -- migration 0068 authority: version, filename, checksum and allocation metadata ----------

MIGRATION_FILENAME = "0068_task_context_outcomes.sql"
REPO_ROOT = Path(__file__).resolve().parents[5]
ALLOCATIONS = REPO_ROOT / "contracts" / "migrations" / "v1" / "allocations.json"


MIGRATION_FILE = REPO_ROOT / "packages" / "omnivia-core-runtime" / "src" / "omnivia_core_runtime" / "storage" / "migration_files" / MIGRATION_FILENAME


def _migration_file_digest() -> str:
    return hashlib.sha256(MIGRATION_FILE.read_text(encoding="utf-8").encode("utf-8")).hexdigest()


def _allocation(number: int) -> dict[str, Any]:
    entries = json.loads(ALLOCATIONS.read_text(encoding="utf-8"))["allocations"]
    matches = [entry for entry in entries if entry["number"] == number]
    assert len(matches) == 1
    return matches[0]


def test_0068_is_the_unique_successor_to_0067_under_its_filename() -> None:
    found = [migration for migration in load_migrations() if migration.version == 68]
    assert [migration.name for migration in found] == [MIGRATION_FILENAME]
    assert _allocation(68)["predecessor"] == _allocation(67)["number"] == 67


def test_the_0068_allocation_pins_its_filename_predecessor_owner_and_state() -> None:
    assert _allocation(68) == {
        "number": 68,
        "filename": MIGRATION_FILENAME,
        "owner": "Agent Runtime",
        "repository": "omnivia-core",
        "state": "candidate",
        "predecessor": 67,
        "sha256": _migration_file_digest(),
        "introduced_commit": "9807ce91bcfe25db29d09757364105378fd58bda",
        "accepted_commit": None,
    }


def test_the_0068_checksum_is_the_allocated_digest_of_the_migration_text() -> None:
    (migration,) = [migration for migration in load_migrations() if migration.version == 68]
    assert migration.checksum == _migration_file_digest() == _allocation(68)["sha256"]
    assert migration.checksum == "fce1d79b1c73d9ef40d324568e619059a5b6954179a50a4d15d5a29ffa93b3e9"
