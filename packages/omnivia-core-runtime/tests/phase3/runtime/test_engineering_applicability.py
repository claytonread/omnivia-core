"""Applicability, priority and review rules over the real substrate (PR-E/PR-G).

A priority is one principal's own preference: an audited upsert that never
changes governed state, and `preferred` reorders the already-ranked page as a
stable secondary sort — never as a score. A review records an attestation and a
target-specific assessment: the expected assessment version is a real
precondition, an acknowledged review without evidence cannot clear a stale
target (§15.5), and the assessment is conservative. No dependency validation
exists yet, so nothing mints `matched`: not registration, recency, a review
outcome or a review evidence id. A legacy `matched` row is not replayed by
search.
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest
import test_application_audit_idempotency_migration as m1
import test_v06_5_s0_mutation_foundation as s0
from omnivia_core_runtime.service.application import authorize_application_request
from omnivia_core_runtime.service.handlers.engineering import EngineeringHandlers
from omnivia_core_runtime.service.operations import OperationContext, OperationError
from omnivia_core_runtime.storage import engineering_applicability as app_storage
from omnivia_core_runtime.storage import repository_identity as repo_identity

from omnivia_core.contracts.v1 import (
    ERROR_CODE_MUTATION_PRECONDITION_FAILED,
    get_operation_metadata,
)

WORKSPACE_ID = s0.WORKSPACE_ID

PRIORITY = get_operation_metadata("context.priority.set")
REVIEW = get_operation_metadata("engineering.review.record")
SEARCH = get_operation_metadata("engineering.search")

_CONTENT: dict[str, Any] = {
    "schema_version": "1.0",
    "kind": "decision",
    "title": "Authentication uses provider A on the mainline",
    "summary": "The mainline decision names provider A.",
    "what": "Provider A is the mainline authentication provider.",
    "assertion_basis": "observed",
    "applicability": {"repository_id": "erepo-1"},
}


def _owned(tmp_path: Any) -> Any:
    path = tmp_path / "workspace.sqlite"
    s0.materialise_phase0_baseline(path)
    m1.bootstrap_and_migrate(path)
    return m1.take_ownership(path)


def _handlers(holder: Any, entry: Any, *, at_s: int = 0) -> Any:
    # `at_s` moves the settlement wall clock so that the latest assessment is
    # decided by time, not by a tie between random assessment ids.
    return EngineeringHandlers(
        service=SimpleNamespace(connection=holder.connection, identity=holder.identity),
        session=s0.session_for(entry),
        binding=s0.BINDING,
        clock=s0.clock_at(wall=s0.WALL_BASE + timedelta(seconds=at_s)),
    )


def _context(
    holder: Any,
    entry: Any,
    operation_input: dict[str, Any],
    *,
    stated_version: str | None = None,
    idempotency_key: str | None = None,
) -> OperationContext:
    overrides: dict[str, Any] = {}
    if idempotency_key is not None:
        overrides["idempotency_key"] = idempotency_key
    if stated_version is not None:
        from omnivia_core.contracts.v1 import MutationPrecondition

        overrides["mutation_precondition"] = MutationPrecondition(
            record_version=stated_version
        )
    envelope = s0.envelope_for(entry, operation_input=operation_input, **overrides)
    authorized = authorize_application_request(
        envelope,
        session=s0.session_for(entry),
        binding=s0.BINDING,
        supported_capabilities=s0.SUPPORTED,
    )
    return OperationContext(
        request=envelope,
        principal=authorized.principal_id,
        workspace_id=authorized.workspace_id or WORKSPACE_ID,
        granted_operations=frozenset({entry.name}),
        authorization=authorized,
    )


def _settle_create(holder: Any, *, marker: str, content: dict[str, Any]) -> Any:
    from omnivia_core_runtime.ownership.fencing import read_guard
    from omnivia_core_runtime.service.mutation import (
        execute_mutation,
        issue_mutation_grant,
    )
    from omnivia_core_runtime.storage.memory import create_memory_record
    from omnivia_core_runtime.storage.retrieval import local_owner_label_grant

    from omnivia_core.contracts.v1 import MemoryCreateInput, idempotency_equivalence

    entry = get_operation_metadata("memory.create")
    operation_input = {
        "record_type": "knowledge.decision",
        "domain_scope": "engineering.codebase",
        "content": content,
        "evidence_disposition": "unavailable",
        "sources": [],
        "assertion": {
            "actor_id": "agent-1",
            "actor_kind": "agent",
            "actor_role": "contributor",
            "asserted_at": "2026-01-27T00:00:00Z",
            "evidence": [],
        },
        "marker": marker,
    }
    envelope = s0.envelope_for(
        entry, operation_input=operation_input, idempotency_key=f"idem-create-{marker}"
    )
    authorized = authorize_application_request(
        envelope,
        session=s0.session_for(entry),
        binding=s0.BINDING,
        supported_capabilities=s0.SUPPORTED,
    )
    grant = issue_mutation_grant(
        authorized,
        session=s0.session_for(entry),
        binding=s0.BINDING,
        guard=read_guard(holder.connection),
        equivalence=idempotency_equivalence(
            entry.name,
            envelope.metadata,
            operation_input,
            principal_id=authorized.principal_id,
            workspace_id=authorized.workspace_id,
        ),
        clock=s0.clock_at(),
    )

    def mutate(fenced: Any, settlement: Any) -> Any:
        return create_memory_record(
            fenced,
            settlement,
            workspace_id=WORKSPACE_ID,
            claim=MemoryCreateInput.from_wire(
                {k: v for k, v in operation_input.items() if k != "marker"}
            ),
            label_grant=local_owner_label_grant(
                principal_id=authorized.principal_id,
                workspace_id=WORKSPACE_ID,
                granted_workspace=WORKSPACE_ID,
            ),
        )

    return execute_mutation(
        holder.connection,
        holder.identity,
        grant=grant,
        context=authorized,
        equivalence=idempotency_equivalence(
            entry.name,
            envelope.metadata,
            operation_input,
            principal_id=authorized.principal_id,
            workspace_id=authorized.workspace_id,
        ),
        mutate=mutate,
        validate_result=lambda _result: True,
        clock=s0.clock_at(),
    )


def _register_repo_and_snapshot(holder: Any, *, marker: str, snapshot_id: str) -> None:
    def mutate(fenced: Any, settlement: Any) -> Any:
        if marker.endswith("repo"):
            repo_identity.register_repository(
                fenced,
                settlement,
                workspace_id=WORKSPACE_ID,
                repository_id="erepo-1",
                display_name="app",
                provider_hint=None,
                registered_at_us=settlement.settled_at_us,
            )
            return {"registered": "erepo-1"}
        repo_identity.record_snapshot(
            fenced,
            settlement,
            workspace_id=WORKSPACE_ID,
            snapshot_id=snapshot_id,
            repository_id="erepo-1",
            snapshot_kind="git_commit",
            manifest={"files": 1},
            base_commit="abc123",
            capture_status="complete",
            captured_at_us=settlement.settled_at_us,
        )
        return {"snapshot": snapshot_id}

    _fenced(holder, marker=marker, mutate=mutate)


def _seed_assessment(
    holder: Any, *, marker: str, record: dict[str, Any], snapshot_id: str, status: str
) -> None:
    """Write an assessment row the way pre-fix builds did, through the real
    fenced coordinator. This is how the tests get a legacy `matched` row."""

    def mutate(fenced: Any, settlement: Any) -> Any:
        app_storage.record_assessment(
            fenced,
            settlement,
            workspace_id=WORKSPACE_ID,
            assessment_id=f"eas-{marker}",
            record_id=record["record_id"],
            version=record["version"],
            target_snapshot_id=snapshot_id,
            status=status,
            basis="review",
            assessed_at_us=settlement.settled_at_us,
        )
        return {"seeded": marker}

    _fenced(holder, marker=marker, mutate=mutate)


def _fenced(holder: Any, *, marker: str, mutate: Any) -> None:
    from omnivia_core_runtime.ownership.fencing import read_guard
    from omnivia_core_runtime.service.mutation import (
        execute_mutation,
        issue_mutation_grant,
    )

    from omnivia_core.contracts.v1 import idempotency_equivalence

    entry = get_operation_metadata("memory.create")
    operation_input = {"marker": marker}
    envelope = s0.envelope_for(
        entry, operation_input=operation_input, idempotency_key=f"idem-{marker}"
    )
    authorized = authorize_application_request(
        envelope,
        session=s0.session_for(entry),
        binding=s0.BINDING,
        supported_capabilities=s0.SUPPORTED,
    )
    grant = issue_mutation_grant(
        authorized,
        session=s0.session_for(entry),
        binding=s0.BINDING,
        guard=read_guard(holder.connection),
        equivalence=idempotency_equivalence(
            entry.name,
            envelope.metadata,
            operation_input,
            principal_id=authorized.principal_id,
            workspace_id=authorized.workspace_id,
        ),
        clock=s0.clock_at(),
    )
    execute_mutation(
        holder.connection,
        holder.identity,
        grant=grant,
        context=authorized,
        equivalence=idempotency_equivalence(
            entry.name,
            envelope.metadata,
            operation_input,
            principal_id=authorized.principal_id,
            workspace_id=authorized.workspace_id,
        ),
        mutate=mutate,
        validate_result=lambda _result: True,
        clock=s0.clock_at(),
    )


def _search(holder: Any, **overrides: Any) -> Any:
    operation_input: dict[str, Any] = {"query": "provider A"}
    operation_input.update(overrides)
    handlers = _handlers(holder, SEARCH)
    context = _context(holder, SEARCH, operation_input)
    return handlers.engineering_search(context)


def test_a_priority_is_an_audited_upsert_and_reorders_the_ranked_page(
    tmp_path: Any,
) -> None:
    holder = _owned(tmp_path)
    try:
        first = _settle_create(holder, marker="obs-1", content=dict(_CONTENT))
        second_content = dict(_CONTENT)
        second_content["title"] = "Second provider A decision"
        second = _settle_create(holder, marker="obs-2", content=second_content)
        first_id = first.result["record"]["provenance"]["identity"]["record_id"]
        second_id = second.result["record"]["provenance"]["identity"]["record_id"]

        second_version = second.result["record"]["provenance"]["identity"]["version"]
        handlers = _handlers(holder, PRIORITY)
        preferred = handlers.context_priority_set(
            _context(
                holder,
                PRIORITY,
                {
                    "target": {"record_id": second_id, "version": second_version},
                    "priority": "preferred",
                },
                idempotency_key="idem-priority-preferred",
            )
        )
        assert preferred["priority"] == "preferred"

        page = _search(holder, view="candidates")
        order = [p["record_id"] for p in page["previews"]]
        assert order == [second_id, first_id]

        # Restating as normal is the audited supersession, not a deletion.
        normal = handlers.context_priority_set(
            _context(
                holder,
                PRIORITY,
                {
                    "target": {"record_id": second_id, "version": second_version},
                    "priority": "normal",
                },
                idempotency_key="idem-priority-normal",
            )
        )
        assert normal["priority"] == "normal"
        restored = _search(holder, view="candidates")
        # The natural rank order after the restore is nondeterministic (ids are
        # random); what the restore pins is that `preferred` no longer forces
        # the second record ahead of the first.
        assert sorted(p["record_id"] for p in restored["previews"]) == sorted(
            [first_id, second_id]
        )
    finally:
        holder.connection.close()


def test_a_review_precondition_and_the_conservative_assessment(tmp_path: Any) -> None:
    holder = _owned(tmp_path)
    try:
        _register_repo_and_snapshot(holder, marker="repo", snapshot_id="esnap-a")
        _register_repo_and_snapshot(holder, marker="snap-a", snapshot_id="esnap-a")
        outcome = _settle_create(holder, marker="obs-1", content=dict(_CONTENT))
        identity = outcome.result["record"]["provenance"]["identity"]
        record_id = identity["record_id"]
        record_version = identity["version"]
        handlers = _handlers(holder, REVIEW)

        # Stating an expectation where no assessment exists is a precondition
        # failure: the caller must re-read before the first write.
        with pytest.raises(OperationError) as stale:
            handlers.engineering_review_record(
                _context(
                    holder,
                    REVIEW,
                    {
                        "record_ref": {
                            "record_id": record_id,
                            "version": record_version,
                        },
                        "target_snapshot": {"snapshot_id": "esnap-a"},
                        "review_outcome": "evidence_attached",
                        "review_evidence_id": "ev-1",
                        "expected_assessment_version": "eas-nowhere",
                    },
                    stated_version="assessment-0",
                    idempotency_key="idem-review-stale",
                )
            )
        assert stale.value.code == ERROR_CODE_MUTATION_PRECONDITION_FAILED

        # The honest first write: the count-based expectation states zero prior
        # assessments. The target is the newest registered snapshot, but that
        # is not dependency validation, and neither is the evidence id. The
        # result is `unknown`, not `matched`.
        recorded = handlers.engineering_review_record(
            _context(
                holder,
                REVIEW,
                {
                    "record_ref": {
                        "record_id": record_id,
                        "version": record_version,
                    },
                    "target_snapshot": {"snapshot_id": "esnap-a"},
                    "review_outcome": "evidence_attached",
                    "review_evidence_id": "ev-1",
                },
                stated_version="assessment-0",
                idempotency_key="idem-review-first",
            )
        )
        assert recorded["applicability"] == "unknown"

        # A newer head is registered: the same target is now potentially stale,
        # and an acknowledgement without evidence cannot clear it.
        _register_repo_and_snapshot(holder, marker="snap-b", snapshot_id="esnap-b")
        recorded_again = handlers.engineering_review_record(
            _context(
                holder,
                REVIEW,
                {
                    "record_ref": {
                        "record_id": record_id,
                        "version": record_version,
                    },
                    "target_snapshot": {"snapshot_id": "esnap-a"},
                    "review_outcome": "acknowledged",
                },
                stated_version="assessment-1",
                idempotency_key="idem-review-second",
            )
        )
        assert recorded_again["applicability"] == "potentially_stale"
    finally:
        holder.connection.close()


def test_an_unknown_target_is_unknown_never_matched(tmp_path: Any) -> None:
    holder = _owned(tmp_path)
    try:
        _register_repo_and_snapshot(holder, marker="repo", snapshot_id="esnap-a")
        _register_repo_and_snapshot(holder, marker="snap-a", snapshot_id="esnap-a")
        outcome = _settle_create(holder, marker="obs-1", content=dict(_CONTENT))
        identity = outcome.result["record"]["provenance"]["identity"]
        handlers = _handlers(holder, REVIEW)
        recorded = handlers.engineering_review_record(
            _context(
                holder,
                REVIEW,
                {
                    "record_ref": {
                        "record_id": identity["record_id"],
                        "version": identity["version"],
                    },
                    "target_snapshot": {"snapshot_id": "esnap-nowhere"},
                    "review_outcome": "acknowledged",
                },
                stated_version="assessment-0",
                idempotency_key="idem-review-unknown",
            )
        )
        assert recorded["applicability"] == "unknown"
    finally:
        holder.connection.close()


def _record(holder: Any, *, marker: str = "obs-1", title: str | None = None) -> Any:
    content = dict(_CONTENT)
    if title is not None:
        content["title"] = title
    outcome = _settle_create(holder, marker=marker, content=content)
    identity = outcome.result["record"]["provenance"]["identity"]
    return {"record_id": identity["record_id"], "version": identity["version"]}


def _review(
    holder: Any,
    record: dict[str, Any],
    snapshot_id: str,
    *,
    stated: str,
    key: str,
    at_s: int,
    outcome: str = "acknowledged",
    evidence: str | None = None,
) -> Any:
    operation_input: dict[str, Any] = {
        "record_ref": dict(record),
        "target_snapshot": {"snapshot_id": snapshot_id},
        "review_outcome": outcome,
    }
    if evidence is not None:
        operation_input["review_evidence_id"] = evidence
    return _handlers(holder, REVIEW, at_s=at_s).engineering_review_record(
        _context(
            holder, REVIEW, operation_input, stated_version=stated, idempotency_key=key
        )
    )


def _history(holder: Any, record: dict[str, Any]) -> list[tuple[Any, ...]]:
    return holder.connection.execute(
        "SELECT assessment_id, target_snapshot_id, status, basis, assessed_at_us, "
        "audit_ref FROM omnivia_engineering_assessments "
        "WHERE record_id = ? AND version = ? ORDER BY assessed_at_us, assessment_id",
        (record["record_id"], record["version"]),
    ).fetchall()


def _applicability(
    holder: Any, snapshot_id: str, *, view: str = "candidates"
) -> tuple[dict[str, str], Any]:
    page = _search(
        holder,
        view=view,
        repository_target={"repository_id": "erepo-1", "snapshot_id": snapshot_id},
    )
    return (
        {p["record_id"]: p["applicability"] for p in page["previews"]},
        page["coverage"],
    )


@pytest.mark.parametrize(
    ("outcome", "evidence"),
    [("acknowledged", None), ("evidence_attached", "ev-arbitrary-1")],
)
def test_a_review_of_the_newest_snapshot_never_mints_matched(
    tmp_path: Any, outcome: str, evidence: str | None
) -> None:
    holder = _owned(tmp_path)
    try:
        _register_repo_and_snapshot(holder, marker="repo", snapshot_id="esnap-a")
        _register_repo_and_snapshot(holder, marker="snap-a", snapshot_id="esnap-a")
        record = _record(holder)
        recorded = _review(
            holder,
            record,
            "esnap-a",
            outcome=outcome,
            evidence=evidence,
            stated="assessment-0",
            key="idem-review-newest",
            at_s=1,
        )
        assert recorded["applicability"] == "unknown"
        assert [row[2] for row in _history(holder, record)] == ["unknown"]
        assert _applicability(holder, "esnap-a")[0] == {record["record_id"]: "unknown"}
    finally:
        holder.connection.close()


def test_search_never_replays_a_legacy_matched_assessment(tmp_path: Any) -> None:
    holder = _owned(tmp_path)
    try:
        _register_repo_and_snapshot(holder, marker="repo", snapshot_id="esnap-a")
        _register_repo_and_snapshot(holder, marker="snap-a", snapshot_id="esnap-a")
        record = _record(holder)
        other = _record(holder, marker="obs-2", title="Second provider A decision")
        _seed_assessment(
            holder, marker="legacy", record=record, snapshot_id="esnap-a", status="matched"
        )
        seeded = _history(holder, record)
        assert [row[2] for row in seeded] == ["matched"]

        # Being the newest registered snapshot does not make the legacy row
        # true. A record version with no assessment at this exact target is
        # `not_evaluated`, and coverage stays `unavailable`.
        statuses, coverage = _applicability(holder, "esnap-a")
        assert statuses == {record["record_id"]: "unknown", other["record_id"]: "not_evaluated"}
        assert coverage == {"projection": "current", "applicability": "unavailable"}

        # The head moves before any review: the legacy row is now potentially
        # stale, and the new head has no assessment of its own.
        _register_repo_and_snapshot(holder, marker="snap-b", snapshot_id="esnap-b")
        assert _applicability(holder, "esnap-a")[0][record["record_id"]] == (
            "potentially_stale"
        )
        assert _applicability(holder, "esnap-b")[0] == {
            record["record_id"]: "not_evaluated",
            other["record_id"]: "not_evaluated",
        }

        # Proposed records stay out of the accepted view even when they have
        # assessments, and reads leave the history unchanged.
        assert _applicability(holder, "esnap-a", view="accepted")[0] == {}
        assert _history(holder, record) == seeded
        assert _history(holder, other) == []
    finally:
        holder.connection.close()


@pytest.mark.parametrize("prior", ["invalid", "potentially_stale"])
def test_a_review_cannot_clear_a_stale_or_invalid_assessment(
    tmp_path: Any, prior: str
) -> None:
    holder = _owned(tmp_path)
    try:
        _register_repo_and_snapshot(holder, marker="repo", snapshot_id="esnap-a")
        _register_repo_and_snapshot(holder, marker="snap-a", snapshot_id="esnap-a")
        record = _record(holder)
        _seed_assessment(
            holder, marker="prior", record=record, snapshot_id="esnap-a", status=prior
        )
        seeded = _history(holder, record)

        # Neither an acknowledgement nor an unvalidated evidence id clears it,
        # even on the newest registered snapshot.
        reviews = [("acknowledged", None), ("evidence_attached", "ev-arbitrary-1")]
        for count, (outcome, evidence) in enumerate(reviews, start=1):
            recorded = _review(
                holder,
                record,
                "esnap-a",
                outcome=outcome,
                evidence=evidence,
                stated=f"assessment-{count}",
                key=f"idem-review-{count}",
                at_s=count,
            )
            assert recorded["applicability"] == prior

        history = _history(holder, record)
        assert history[0] == seeded[0]
        assert [row[2] for row in history] == [prior, prior, prior]
        assert _applicability(holder, "esnap-a")[0] == {record["record_id"]: prior}
    finally:
        holder.connection.close()


def test_a_review_replay_is_idempotent_and_history_is_append_only(
    tmp_path: Any,
) -> None:
    holder = _owned(tmp_path)
    try:
        _register_repo_and_snapshot(holder, marker="repo", snapshot_id="esnap-a")
        _register_repo_and_snapshot(holder, marker="snap-a", snapshot_id="esnap-a")
        record = _record(holder)
        review = {
            "outcome": "evidence_attached",
            "evidence": "ev-1",
            "stated": "assessment-0",
            "key": "idem-review-once",
        }
        first = _review(holder, record, "esnap-a", at_s=1, **review)
        once = _history(holder, record)
        assert len(once) == 1

        # Same key and body: the stored answer is replayed and nothing is written.
        replayed = _review(holder, record, "esnap-a", at_s=2, **review)
        assert dict(replayed) == dict(first)
        assert _history(holder, record) == once

        # A later review appends a row. The earlier one is never rewritten.
        _register_repo_and_snapshot(holder, marker="snap-b", snapshot_id="esnap-b")
        second = _review(
            holder,
            record,
            "esnap-a",
            stated="assessment-1",
            key="idem-review-twice",
            at_s=3,
        )
        assert second["applicability"] == "potentially_stale"
        history = _history(holder, record)
        assert history[0] == once[0]
        assert [row[2] for row in history] == ["unknown", "potentially_stale"]
        attestations = holder.connection.execute(
            "SELECT COUNT(*) FROM omnivia_engineering_review_attestations "
            "WHERE record_id = ?",
            (record["record_id"],),
        ).fetchone()[0]
        assert attestations == 2
    finally:
        holder.connection.close()
