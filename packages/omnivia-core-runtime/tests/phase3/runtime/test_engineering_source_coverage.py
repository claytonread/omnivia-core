"""Trusted source coverage, dependency-qualified applicability and `current_safe`
(SPEC-CORE-ENGMEM-001 P0-04; migration 0050).

The vertical runs through the production application surface. A trusted source
records snapshots under its own `engineering:source` grant, `memory.create`
proposes an evidence-backed observation with a whole-file dependency manifest, and
`current_safe` reads consult source coverage before any ranking. Only exact
whole-file digests attested by a recorded, covered baseline make a record
`matched` at a covered target; everything else stays pending, adverse or unknown,
and source history is never rewritten.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import test_blobs_staged_sources_and_evidence_migration as m2
import test_v06_5_s0_mutation_foundation as s0
from omnivia_core_runtime.ownership.fencing import StaleGeneration, fenced_transaction
from omnivia_core_runtime.ownership.identity import SystemClock
from omnivia_core_runtime.service.application import (
    ENGINEERING_FAMILY_PURPOSES,
    GOVERNANCE_FAMILY_PURPOSES,
    MEMORY_FAMILY_PURPOSES,
    ProductionApplicationSurface,
    build_installation_application_dispatcher,
    engineering_family_session,
    memory_family_session,
)
from omnivia_core_runtime.service.authorization import AuthenticatedSession, Grant
from omnivia_core_runtime.service.dispatch import Dispatcher
from omnivia_core_runtime.service.installed_mcp import (
    AUTHORING_POLICY,
    RESTRICTED_POLICY,
)
from omnivia_core_runtime.service.main import _build_production_application_surface
from omnivia_core_runtime.service.operations import SERVICE_OPERATIONS
from omnivia_core_runtime.storage import engineering_source

from omnivia_core.contracts.v1 import (
    ErrorResponseEnvelope,
    MutationPrecondition,
    SuccessResponseEnvelope,
    get_operation_metadata,
)

_PURPOSES = {
    **MEMORY_FAMILY_PURPOSES,
    **GOVERNANCE_FAMILY_PURPOSES,
    **ENGINEERING_FAMILY_PURPOSES,
}

WORKSPACE_ID = m2.WORKSPACE_ID
PRINCIPAL = "local-user"
REPOSITORY = "erepo-app"
STREAM = "estream-main"

_SOURCE_TABLES = (
    "omnivia_engineering_repositories",
    "omnivia_engineering_snapshots",
    "omnivia_engineering_source_streams",
    "omnivia_engineering_source_events",
    "omnivia_engineering_dependencies",
    "omnivia_engineering_dependency_sets",
    "omnivia_engineering_assessments",
)


def _sha(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


AUTH_V1 = _sha("auth v1")
AUTH_V2 = _sha("auth v2")
UTIL_V1 = _sha("util v1")
README_V1 = _sha("readme v1")
FILES_A = {"src/auth.py": AUTH_V1, "src/util.py": UTIL_V1, "README.md": README_V1}

#: The evidence artifact `m2.seed_chain` captures; `memory.create` resolves it
#: through the existing evidence path and label grant.
EVIDENCE_SOURCE = {
    "kind": "filesystem.archive",
    "source_id": "doc-1",
    "locator": "archive://doc.md",
    "retrieved_at": datetime.fromtimestamp(m2.BASE_US / 1_000_000, tz=UTC).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    ),
}


class _InstallationService:
    """Construction-only shape; its bound production handlers are never invoked."""

    authority = SimpleNamespace(installation_id=s0.INSTALLATION_ID)


def _surface(holder: Any) -> ProductionApplicationSurface:
    probe = Dispatcher.for_service_operations(
        Grant(
            principal=PRINCIPAL,
            workspaces=frozenset({WORKSPACE_ID}),
            operations=frozenset(SERVICE_OPERATIONS),
        ),
        holder,
    )
    started = SimpleNamespace(**vars(holder), workspace_id=WORKSPACE_ID, clock=SystemClock())
    installation = build_installation_application_dispatcher(
        service=_InstallationService(),  # type: ignore[arg-type]
        principal_id=PRINCIPAL,
        fallback=probe,
    )
    return _build_production_application_surface(
        started=started,  # type: ignore[arg-type]
        probe=probe,
        installation=installation,
    )


class Workspace:
    """One owned, fully migrated workspace behind the real production surface."""

    def __init__(self, tmp_path: Path) -> None:
        path = tmp_path / "workspace.sqlite"
        m2.materialise_phase0_baseline(path)
        m2.bootstrap_and_migrate(path)
        self.holder = m2.take_ownership(path)
        m2.seed_chain(self.holder)
        self.surface = _surface(self.holder)
        self._requests = 0

    def restart(self) -> None:
        """Drop the connection and adopt the workspace again, as a restart does."""
        self.holder.connection.close()
        self.holder = m2.take_ownership(self.holder.path)
        self.surface = _surface(self.holder)

    def call(
        self,
        operation: str,
        payload: dict[str, Any],
        *,
        key: str | None = None,
        session: AuthenticatedSession | None = None,
        **metadata: Any,
    ) -> Any:
        self._requests += 1
        request_id = f"req-src-{self._requests}"
        entry = get_operation_metadata(operation)
        overrides: dict[str, Any] = {
            "request_id": request_id,
            "correlation_id": f"cor-{request_id}",
            "trace_id": f"trc-{request_id}",
            "purpose": _PURPOSES[operation],
            "workspace_id": WORKSPACE_ID,
        }
        if entry.idempotency.supports_idempotency_key:
            overrides["idempotency_key"] = key or f"idem-{request_id}"
        overrides.update(metadata)
        envelope = s0.envelope_for(entry, operation_input=payload, **overrides)
        if session is None:
            return self.surface.dispatch(envelope)
        return self.surface.dispatch_for_session(envelope, session)

    def ok(self, operation: str, payload: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        response = self.call(operation, payload, **kwargs)
        assert isinstance(response, SuccessResponseEnvelope), response
        return dict(response.to_wire()["result"])

    def refused(
        self, operation: str, payload: dict[str, Any], **kwargs: Any
    ) -> tuple[str, str, str]:
        response = self.call(operation, payload, **kwargs)
        assert isinstance(response, ErrorResponseEnvelope), response
        return response.error.code, response.error.message, response.error.retry_class

    def counts(self) -> dict[str, int]:
        return {
            table: int(
                self.holder.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            )
            for table in _SOURCE_TABLES
        }

    def stream(self, stream_id: str = STREAM) -> tuple[Any, ...] | None:
        row = self.holder.connection.execute(
            "SELECT repository_id, principal_id, announced_sequence, covered_sequence "
            "FROM omnivia_engineering_source_streams WHERE stream_id = ?",
            (stream_id,),
        ).fetchone()
        return None if row is None else tuple(row)

    def record(self, payload: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        return self.ok("engineering.source.record", payload, **kwargs)

    def observe(self, payload: dict[str, Any], **kwargs: Any) -> dict[str, str]:
        result = self.ok("memory.create", payload, **kwargs)
        identity = result["record"]["provenance"]["identity"]
        return {"record_id": identity["record_id"], "version": identity["version"]}

    def search(
        self,
        snapshot_id: str,
        *,
        view: str = "candidates",
        session: AuthenticatedSession | None = None,
        **extra: Any,
    ) -> Any:
        payload: dict[str, Any] = {
            "query": "provider",
            "view": view,
            "applicability_mode": "current_safe",
            "repository_target": {"repository_id": REPOSITORY, "snapshot_id": snapshot_id},
        }
        payload.update(extra)
        return self.call("engineering.search", payload, session=session)

    def matched(self, snapshot_id: str, **extra: Any) -> list[str]:
        response = self.search(snapshot_id, **extra)
        assert isinstance(response, SuccessResponseEnvelope), response
        result = response.to_wire()["result"]
        assert result["coverage"] == {"projection": "current", "applicability": "current"}
        assert {preview["applicability"] for preview in result["previews"]} <= {"matched"}
        return [preview["record_id"] for preview in result["previews"]]

    def status(self, record: dict[str, str], snapshot_id: str) -> str:
        """The shared evaluator's verdict, or `pending` for an uncovered target.

        Evidence availability is read from the stored version exactly as the read
        path derives it: an `available` disposition with resolved evidence.
        """
        connection = self.holder.connection
        target = engineering_source.covered_snapshot(
            connection, workspace_id=WORKSPACE_ID, snapshot_id=snapshot_id
        )
        if target is None:
            return "pending"
        disposition, links = connection.execute(
            "SELECT v.evidence_disposition, COUNT(l.evidence_id) "
            "FROM omnivia_governed_version_assemblies v "
            "LEFT JOIN omnivia_governed_version_evidence_links l "
            "ON l.workspace_id = v.workspace_id AND l.assembly_id = v.assembly_id "
            "WHERE v.governed_record_version_id = ? GROUP BY v.assembly_id",
            (record["version"],),
        ).fetchone()
        return engineering_source.evaluate_applicability(
            connection,
            workspace_id=WORKSPACE_ID,
            record_id=record["record_id"],
            version=record["version"],
            evidence_available=disposition == "available" and links > 0,
            target=target,
        )


@pytest.fixture
def workspace(tmp_path: Path) -> Any:
    opened = Workspace(tmp_path)
    yield opened
    opened.holder.connection.close()


def _source(
    sequence: int,
    snapshot_id: str,
    files: dict[str, str],
    *,
    predecessor: str | None = None,
    stream: str = STREAM,
    repository: str = REPOSITORY,
    kind: str = "git_commit",
    base_commit: str | None = "c0ffee01",
    capture: str = "complete",
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "repository_id": repository,
        "stream_id": stream,
        "sequence": sequence,
        "snapshot_id": snapshot_id,
        "snapshot_kind": kind,
        "capture_status": capture,
        "manifest": [{"path": path, "digest": digest} for path, digest in files.items()],
    }
    if base_commit is not None:
        payload["base_commit"] = base_commit
    if predecessor is not None:
        payload["predecessor"] = {"sequence": sequence - 1, "snapshot_id": predecessor}
    return payload


def _dependency(
    selector: str,
    digest: str | None,
    meaning: str = "must_match",
    selector_type: str = "whole_file",
) -> dict[str, Any]:
    dependency: dict[str, Any] = {
        "selector_type": selector_type,
        "selector": selector,
        "meaning": meaning,
    }
    if digest is not None:
        dependency["expected_digest"] = digest
    return dependency


DEPENDENCIES = [
    _dependency("src/auth.py", AUTH_V1),
    _dependency("src/util.py", UTIL_V1, "requires_revalidation_on_change"),
    _dependency("README.md", README_V1, "context_only"),
]


def _manifest(
    snapshot_id: str = "esnap-a",
    dependencies: list[dict[str, Any]] | None = None,
    *,
    stream: str = STREAM,
    repository: str = REPOSITORY,
    coverage: str = "complete",
) -> dict[str, Any]:
    return {
        "repository_id": repository,
        "stream_id": stream,
        "snapshot_id": snapshot_id,
        "producer": "omnivia-dev-indexer",
        "producer_version": "1.0.0",
        "coverage": coverage,
        "dependencies": DEPENDENCIES if dependencies is None else dependencies,
    }


def _observation(
    manifest: dict[str, Any] | None,
    *,
    title: str = "Sign-in provider decision",
    evidence: bool = True,
    source: dict[str, Any] = EVIDENCE_SOURCE,
) -> dict[str, Any]:
    content: dict[str, Any] = {
        "schema_version": "1.0",
        "kind": "decision",
        "title": title,
        "summary": "Interactive sign-in uses provider A.",
        "what": "Provider A is the sign-in provider.",
        "assertion_basis": "observed",
    }
    if manifest is not None:
        content["dependency_manifest"] = manifest
    return {
        "record_type": "knowledge.decision",
        "domain_scope": "engineering.codebase",
        "content": content,
        "evidence_disposition": "available" if evidence else "unavailable",
        "sources": [source] if evidence else [],
        "assertion": {
            "actor_id": "agent-1",
            "actor_kind": "agent",
            "actor_role": "contributor",
            "asserted_at": "2026-01-27T00:00:00Z",
            "evidence": [{"source": source}] if evidence else [],
        },
    }


# --- the production vertical ---------------------------------------------------------


def test_the_source_coverage_vertical_through_the_production_surface(
    workspace: Workspace,
) -> None:
    first = workspace.record(_source(1, "esnap-a", FILES_A), key="src-a")
    assert first["disposition"] == "recorded"
    assert first["coverage"] == {
        "state": "current",
        "covered_sequence": 1,
        "announced_sequence": 1,
    }
    record = workspace.observe(_observation(_manifest()))

    # The proposal is not accepted knowledge, in either mode.
    assert workspace.matched("esnap-a", view="accepted") == []
    diagnostic = workspace.ok("engineering.search", {"query": "provider"})
    assert diagnostic["previews"] == []

    # Candidates, current_safe at A: the exact version is proven matched.
    assert workspace.matched("esnap-a") == [record["record_id"]]

    # B arrives with a gap (sequence 2 missing): coverage stays at 1 and every
    # current_safe read of B is refused before ranking.
    files_b = {**FILES_A, "src/auth.py": AUTH_V2}
    gapped = workspace.record(_source(3, "esnap-b", files_b, predecessor="esnap-a1"))
    assert gapped["coverage"] == {
        "state": "pending",
        "covered_sequence": 1,
        "announced_sequence": 3,
    }
    assert workspace.refused(
        "engineering.search",
        {
            "query": "provider",
            "view": "candidates",
            "applicability_mode": "current_safe",
            "repository_target": {"repository_id": REPOSITORY, "snapshot_id": "esnap-b"},
        },
    ) == ("dependency_unavailable", "applicability_pending", "retryable_after_delay")
    build_b = {
        "query": "provider",
        "targets": [{"repository_id": REPOSITORY, "snapshot_id": "esnap-b"}],
        "profile": "investigate",
        "applicability_mode": "current_safe",
    }
    assert workspace.refused("engineering.context.build", build_b)[:2] == (
        "dependency_unavailable",
        "applicability_pending",
    )

    # Restart: the event, the barrier and the stored receipt all survive.
    workspace.restart()
    assert workspace.stream() == (REPOSITORY, PRINCIPAL, 3, 1)
    assert workspace.record(_source(1, "esnap-a", FILES_A), key="src-a") == first
    assert workspace.refused("engineering.search", {
        "query": "provider",
        "view": "candidates",
        "applicability_mode": "current_safe",
        "repository_target": {"snapshot_id": "esnap-b"},
    })[1] == "applicability_pending"

    # The missing predecessor arrives: one bounded drain covers 2 and 3.
    filled = workspace.record(_source(2, "esnap-a1", FILES_A, predecessor="esnap-a"))
    assert filled["coverage"] == {
        "state": "current",
        "covered_sequence": 3,
        "announced_sequence": 3,
    }
    assert workspace.matched("esnap-a1") == [record["record_id"]]
    # B changed a required file: potentially stale, so it cannot enter safe reads.
    assert workspace.status(record, "esnap-b") == "potentially_stale"
    assert workspace.matched("esnap-b") == []
    pack = workspace.ok("engineering.context.build", build_b)["pack"]
    assert pack["sections"] == [] and pack["citations"] == []
    assert pack["omissions"] == [{"field": "sections", "reason": "applicability_unproven"}]
    assert pack["applicability"] == [
        {
            "snapshot": {"repository_id": REPOSITORY, "snapshot_id": "esnap-b"},
            "status": "not_evaluated",
        }
    ]

    # A revert to A's exact digests qualifies again; all history is retained.
    workspace.record(_source(4, "esnap-c", FILES_A, predecessor="esnap-b"))
    assert workspace.matched("esnap-c") == [record["record_id"]]
    assert workspace.status(record, "esnap-b") == "potentially_stale"
    assert [
        row[0]
        for row in workspace.holder.connection.execute(
            "SELECT snapshot_id FROM omnivia_engineering_source_events ORDER BY sequence"
        )
    ] == ["esnap-a", "esnap-a1", "esnap-b", "esnap-c"]

    safe = workspace.ok(
        "engineering.context.build",
        {**build_b, "targets": [{"repository_id": REPOSITORY, "snapshot_id": "esnap-c"}]},
    )["pack"]
    assert [section["partition"] for section in safe["sections"]] == ["candidate_findings"]
    assert safe["citations"][0]["record_ref"] == record
    assert safe["applicability"][0]["status"] == "matched"
    assert safe["normalized_request"]["applicability_mode"] == "current_safe"
    assert safe["reproducibility"]["source_coverage"][0]["sequence"] == 4
    # Reads never wrote an assessment or touched source history.
    assert workspace.counts()["omnivia_engineering_assessments"] == 0


# --- grants ---------------------------------------------------------------------


def test_only_the_distinct_source_grant_records_source_state(workspace: Workspace) -> None:
    before = workspace.counts()
    payload = _source(1, "esnap-a", FILES_A)

    # A contributor holding only the observation grant cannot reach it.
    contributor = memory_family_session(
        principal_id=PRINCIPAL,
        installation_id=s0.INSTALLATION_ID,
        workspace_id=WORKSPACE_ID,
    )
    assert workspace.refused("engineering.source.record", payload, session=contributor)[0] == (
        "authorization_denied"
    )

    # The engineering grant without its source scope, or without its source
    # capability, is refused as well: the contributor role alone is not enough.
    full = engineering_family_session(
        principal_id=PRINCIPAL,
        installation_id=s0.INSTALLATION_ID,
        workspace_id=WORKSPACE_ID,
    )
    assert "engineering:source" in full.scopes
    no_scope = AuthenticatedSession(
        principal_id=full.principal_id,
        roles=full.roles,
        installations=full.installations,
        workspaces=full.workspaces,
        operations=full.operations,
        scopes=full.scopes - {"engineering:source"},
        purposes=full.purposes,
        capabilities=full.capabilities,
    )
    no_capability = AuthenticatedSession(
        principal_id=full.principal_id,
        roles=full.roles,
        installations=full.installations,
        workspaces=full.workspaces,
        operations=full.operations,
        scopes=full.scopes,
        purposes=full.purposes,
        capabilities=tuple(
            ref for ref in full.capabilities if ref.id != "engineering.source"
        ),
    )
    assert workspace.refused("engineering.source.record", payload, session=no_scope)[0] == (
        "authorization_denied"
    )
    assert workspace.refused(
        "engineering.source.record", payload, session=no_capability
    )[0] == "capability_not_granted"
    assert workspace.counts() == before

    # No model-facing MCP profile carries any part of the source grant.
    for policy in (RESTRICTED_POLICY, AUTHORING_POLICY):
        values = {grant.value for grant in policy}
        assert not values & {"engineering.source.record", "engineering:source", "engineering.source"}


def test_another_principal_cannot_write_or_replace_a_stream(workspace: Workspace) -> None:
    workspace.record(_source(1, "esnap-a", FILES_A))
    before = workspace.counts()
    intruder = engineering_family_session(
        principal_id="intruder",
        installation_id=s0.INSTALLATION_ID,
        workspace_id=WORKSPACE_ID,
    )
    for payload in (
        _source(2, "esnap-x", FILES_A, predecessor="esnap-a"),
        _source(1, "esnap-a", FILES_A),
    ):
        code, message, _ = workspace.refused(
            "engineering.source.record", payload, session=intruder
        )
        assert (code, message) == (
            "authorization_denied",
            "the source stream is owned by another principal",
        )
    assert workspace.counts() == before
    assert workspace.stream() == (REPOSITORY, PRINCIPAL, 1, 1)

    # The owner cannot rebind its own stream to another repository either.
    assert workspace.refused(
        "engineering.source.record",
        _source(2, "esnap-y", FILES_A, predecessor="esnap-a", repository="erepo-other"),
    )[0] == "conflict"
    assert workspace.counts() == before


# --- identity, idempotency and ordering ----------------------------------------------


def test_duplicate_delivery_and_conflicting_reuse(workspace: Workspace) -> None:
    first = workspace.record(_source(1, "esnap-a", FILES_A), key="deliver-1")
    after_first = workspace.counts()

    # The original key replays the stored receipt.
    assert workspace.record(_source(1, "esnap-a", FILES_A), key="deliver-1") == first
    # An exact duplicate under a new key records nothing new.
    again = workspace.record(_source(1, "esnap-a", FILES_A), key="deliver-2")
    assert again["disposition"] == "already_recorded"
    assert again["recorded_at"] == first["recorded_at"]
    assert again["audit_reference"] != first["audit_reference"]
    assert workspace.counts() == after_first

    # The same key with a different request is an idempotency conflict.
    assert workspace.refused(
        "engineering.source.record",
        _source(1, "esnap-a", {**FILES_A, "src/auth.py": AUTH_V2}),
        key="deliver-1",
    )[0] == "idempotency_conflict"
    # A different event under a used sequence or snapshot identity conflicts,
    # whichever key carries it, and even from a fresh stream.
    for payload in (
        _source(1, "esnap-a", {**FILES_A, "src/auth.py": AUTH_V2}),
        _source(1, "esnap-other", FILES_A),
        _source(1, "esnap-a", FILES_A, stream="estream-other"),
    ):
        assert workspace.refused("engineering.source.record", payload)[0] == "conflict"
    assert workspace.counts() == after_first


def test_coverage_never_crosses_gaps_or_broken_chains(workspace: Workspace) -> None:
    workspace.record(_source(1, "esnap-1", FILES_A))
    # 3 waits for 2; its stated predecessor is "esnap-2".
    workspace.record(_source(3, "esnap-3", FILES_A, predecessor="esnap-2"))
    assert workspace.stream() == (REPOSITORY, PRINCIPAL, 3, 1)
    before = workspace.counts()
    # A 2 that is not the "esnap-2" that 3 names disagrees with its stored
    # successor, and a 2 naming the wrong predecessor disagrees with 1.
    assert workspace.refused(
        "engineering.source.record", _source(2, "esnap-2x", FILES_A, predecessor="esnap-1")
    )[0] == "conflict"
    assert workspace.refused(
        "engineering.source.record", _source(2, "esnap-2", FILES_A, predecessor="esnap-0")
    )[0] == "conflict"
    assert workspace.counts() == before
    assert workspace.stream() == (REPOSITORY, PRINCIPAL, 3, 1)

    # The right 2 completes the chain in one drain; capture time played no part.
    workspace.record(_source(2, "esnap-2", FILES_A, predecessor="esnap-1"))
    assert workspace.stream() == (REPOSITORY, PRINCIPAL, 3, 3)

    # The documented 64-event pending window bounds how far past its covered
    # chain a stream may announce, which is what keeps every drain bounded.
    window = "estream-window"
    workspace.record(_source(1, "esnap-w-1", FILES_A, stream=window))
    assert workspace.refused(
        "engineering.source.record",
        _source(66, "esnap-w-66", FILES_A, predecessor="esnap-w-65", stream=window),
    )[0] == "size_limit_exceeded"
    workspace.record(_source(65, "esnap-w-65", FILES_A, predecessor="esnap-w-64", stream=window))
    assert workspace.stream(window) == (REPOSITORY, PRINCIPAL, 65, 1)

    # Streams are separate histories: another worktree's stream starts its own.
    other = workspace.record(_source(1, "esnap-w1", FILES_A, stream="estream-worktree"))
    assert other["coverage"]["covered_sequence"] == 1
    assert workspace.stream() == (REPOSITORY, PRINCIPAL, 3, 3)


# --- the evaluator ---------------------------------------------------------------------


def test_changed_deleted_dirty_and_incomplete_targets(workspace: Workspace) -> None:
    workspace.record(_source(1, "esnap-a", FILES_A))
    record = workspace.observe(_observation(_manifest()))
    assert workspace.status(record, "esnap-a") == "matched"

    # A dirty working tree never asserts a base commit.
    assert workspace.refused(
        "engineering.source.record",
        _source(2, "esnap-dirty", FILES_A, predecessor="esnap-a", kind="working_tree"),
    )[0] == "invalid_request"
    workspace.record(
        _source(
            2,
            "esnap-dirty",
            {**FILES_A, "src/auth.py": AUTH_V2},
            predecessor="esnap-a",
            kind="working_tree",
            base_commit=None,
        )
    )
    assert workspace.status(record, "esnap-dirty") == "potentially_stale"
    # A clean commit naming A's base commit proves nothing: digests decide.
    workspace.record(
        _source(3, "esnap-same-base", {**FILES_A, "src/util.py": _sha("util v2")}, predecessor="esnap-dirty")
    )
    assert workspace.status(record, "esnap-same-base") == "potentially_stale"
    # A required file absent under complete capture is invalid.
    workspace.record(
        _source(4, "esnap-deleted", {"src/util.py": UTIL_V1, "README.md": README_V1}, predecessor="esnap-same-base")
    )
    assert workspace.status(record, "esnap-deleted") == "invalid"
    # The same absence under incomplete capture proves nothing.
    workspace.record(
        _source(
            5,
            "esnap-partial",
            {"src/util.py": UTIL_V1},
            predecessor="esnap-deleted",
            capture="incomplete",
        )
    )
    assert workspace.status(record, "esnap-partial") == "unknown"
    # Identical digests under incomplete capture still cannot qualify matched.
    workspace.record(
        _source(6, "esnap-partial-same", FILES_A, predecessor="esnap-partial", capture="incomplete")
    )
    assert workspace.status(record, "esnap-partial-same") == "unknown"
    # context_only README changes never matter.
    workspace.record(
        _source(7, "esnap-readme", {**FILES_A, "README.md": _sha("readme v2")}, predecessor="esnap-partial-same")
    )
    assert workspace.status(record, "esnap-readme") == "matched"
    for snapshot_id in ("esnap-dirty", "esnap-same-base", "esnap-deleted", "esnap-partial"):
        assert workspace.matched(snapshot_id) == []
    assert workspace.matched("esnap-readme") == [record["record_id"]]


def test_unqualified_dependency_sets_are_never_matched(workspace: Workspace) -> None:
    workspace.record(_source(1, "esnap-a", FILES_A))
    workspace.record(_source(1, "esnap-w", FILES_A, stream="estream-worktree"))
    workspace.record(
        _source(1, "esnap-r", FILES_A, stream="estream-other-repo", repository="erepo-other")
    )
    cases = {
        "no profile": _observation(None, title="No profile provider"),
        "no evidence": _observation(_manifest(), evidence=False),
        "partial": _observation(_manifest(coverage="partial")),
        "empty": _observation(_manifest(dependencies=[])),
        "context only": _observation(
            _manifest(dependencies=[_dependency("src/auth.py", AUTH_V1, "context_only")])
        ),
        "symbol": _observation(
            _manifest(
                dependencies=[
                    _dependency("src/auth.py", AUTH_V1),
                    _dependency("auth.Provider.login", None, selector_type="symbol"),
                ]
            )
        ),
        "config key": _observation(
            _manifest(dependencies=[_dependency("auth.provider", None, selector_type="config_key")])
        ),
        "unattested digest": _observation(
            _manifest(dependencies=[_dependency("src/auth.py", AUTH_V2)])
        ),
        "absent at baseline": _observation(
            _manifest(dependencies=[_dependency("src/missing.py", AUTH_V1)])
        ),
    }
    records = {name: workspace.observe(payload) for name, payload in cases.items()}
    for name, record in records.items():
        assert workspace.status(record, "esnap-a") == "unknown", name
    assert workspace.matched("esnap-a") == []

    # A qualified record still cannot inherit matched across streams or repositories.
    qualified = workspace.observe(_observation(_manifest(), title="Qualified provider"))
    assert workspace.status(qualified, "esnap-a") == "matched"
    assert workspace.status(qualified, "esnap-w") == "unknown"
    assert workspace.status(qualified, "esnap-r") == "unknown"
    assert workspace.matched("esnap-w") == []
    assert workspace.matched("esnap-a") == [qualified["record_id"]]


def test_an_approved_version_does_not_inherit_matched(workspace: Workspace) -> None:
    """Governance transitions mint new exact versions and no dependency set comes
    with them, so `current_safe` omits the accepted version rather than inheriting
    the proposal's `matched` (a known limitation, not a qualification)."""
    workspace.record(_source(1, "esnap-a", FILES_A))
    record = workspace.observe(_observation(_manifest()))
    assert workspace.matched("esnap-a") == [record["record_id"]]
    version = record["version"]
    for operation in ("knowledge.propose", "candidate.approve"):
        transitioned = workspace.ok(
            operation,
            {"record_id": record["record_id"], "rationale": {"reason_code": "review"}},
            mutation_precondition=MutationPrecondition(record_version=version),
        )
        version = transitioned["updated_record"]["provenance"]["identity"]["version"]
    accepted = {"record_id": record["record_id"], "version": version}
    assert accepted["version"] != record["version"]
    assert workspace.status(accepted, "esnap-a") == "unknown"
    assert workspace.matched("esnap-a", view="accepted") == []
    assert workspace.ok("engineering.search", {"query": "provider"})["previews"] != []


def test_review_evidence_cannot_establish_a_match(workspace: Workspace) -> None:
    workspace.record(_source(1, "esnap-a", FILES_A))
    record = workspace.observe(
        _observation(_manifest(coverage="partial"), title="Reviewed provider")
    )
    review = workspace.ok(
        "engineering.review.record",
        {
            "record_ref": record,
            "target_snapshot": {"repository_id": REPOSITORY, "snapshot_id": "esnap-a"},
            "review_outcome": "evidence_attached",
            "review_evidence_id": "ev-arbitrary-1",
        },
        mutation_precondition=MutationPrecondition(record_version="assessment-0"),
    )
    assert review["applicability"] == "unknown"
    # The review is recorded; it attests nothing about dependencies.
    assert workspace.status(record, "esnap-a") == "unknown"
    assert workspace.matched("esnap-a") == []


# --- authorization -----------------------------------------------------------------------


def test_current_safe_never_reveals_label_denied_records_to_another_reader(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The seeded evidence carries `group.engineering`; only the configured owner
    holds it. Another engineering reader with every operation, scope and
    capability grant learns nothing about records backed by it, in search or pack
    build, and the denied versions never reach the evaluator or the scorer."""
    from omnivia_core_runtime.service.handlers import engineering as handlers

    m2.write(workspace.holder, m2.EVIDENCE, evidence_id="evd-open", source_native_id="doc-open")
    workspace.record(_source(1, "esnap-a", FILES_A))
    hidden = workspace.observe(_observation(_manifest(), title="XYZZY secret provider"))
    hidden_unproven = workspace.observe(
        _observation(_manifest(coverage="partial"), title="XYZZY partial provider")
    )
    permitted = workspace.observe(
        _observation(
            _manifest(),
            title="Open provider",
            source={**EVIDENCE_SOURCE, "source_id": "doc-open"},
        )
    )
    denied_ids = {hidden["record_id"], hidden_unproven["record_id"]}

    evaluated: list[str] = []
    ranked: list[str] = []
    evaluate = engineering_source.evaluate_applicability
    rank = handlers.rank_governed

    def spy_evaluate(connection: Any, **kwargs: Any) -> str:
        evaluated.append(kwargs["record_id"])
        return evaluate(connection, **kwargs)

    def spy_rank(frontier: Any, *args: Any, **kwargs: Any) -> Any:
        ranked.extend(c.record.provenance.identity.record_id for c in frontier.candidates)
        return rank(frontier, *args, **kwargs)

    monkeypatch.setattr(engineering_source, "evaluate_applicability", spy_evaluate)
    monkeypatch.setattr(handlers, "rank_governed", spy_rank)
    reader = engineering_family_session(
        principal_id="reader",
        installation_id=s0.INSTALLATION_ID,
        workspace_id=WORKSPACE_ID,
    )
    build = {
        "query": "provider",
        "targets": [{"repository_id": REPOSITORY, "snapshot_id": "esnap-a"}],
        "profile": "investigate",
        "applicability_mode": "current_safe",
    }

    searched = workspace.search("esnap-a", session=reader)
    built = workspace.call("engineering.context.build", build, session=reader)
    for response in (searched, built):
        assert isinstance(response, SuccessResponseEnvelope), response
        wire = json.dumps(response.to_wire())
        assert "XYZZY" not in wire
        assert not any(record_id in wire for record_id in denied_ids)
    assert [p["record_id"] for p in searched.to_wire()["result"]["previews"]] == [
        permitted["record_id"]
    ]
    pack = built.to_wire()["result"]["pack"]
    assert [c["record_ref"] for c in pack["citations"]] == [permitted]
    assert len(pack["sections"]) == 1 and pack["omissions"] == []
    assert set(evaluated) == set(ranked) == {permitted["record_id"]}

    # The owner holds the label: it sees the matched record and the omission.
    evaluated.clear()
    assert set(workspace.matched("esnap-a")) == {hidden["record_id"], permitted["record_id"]}
    owner_pack = workspace.ok("engineering.context.build", build)["pack"]
    assert {c["record_ref"]["record_id"] for c in owner_pack["citations"]} == {
        hidden["record_id"],
        permitted["record_id"],
    }
    assert owner_pack["omissions"] == [
        {"field": "sections", "reason": "applicability_unproven"}
    ]
    assert denied_ids <= set(evaluated)


# --- bounds ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/etc/passwd",
        "../outside.py",
        "src/../../x.py",
        "src\\auth.py",
        "src//auth.py",
        "./src/auth.py",
        "src/",
        "",
        "src/a\x00b.py",
        "src/a\nb.py",
        "C:/src/auth.py",
        "c:auth.py",
        "x" * 513,
    ],
)
def test_malformed_manifest_paths_are_refused(workspace: Workspace, path: str) -> None:
    before = workspace.counts()
    assert workspace.refused(
        "engineering.source.record", _source(1, "esnap-a", {path: AUTH_V1})
    )[0] == "invalid_request"
    assert workspace.counts() == before


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p.update(unexpected=True),
        lambda p: p.update(checkout_path="/home/me/app"),
        lambda p: p["manifest"][0].update(mode="100644"),
        lambda p: p["manifest"][0].update(digest=AUTH_V1.upper()),
        lambda p: p["manifest"][0].update(digest="sha256:abc"),
        lambda p: p["manifest"].append(dict(p["manifest"][0])),
        lambda p: p.update(sequence=0),
        lambda p: p.update(predecessor={"sequence": 0, "snapshot_id": "esnap-z"}),
        lambda p: p.update(snapshot_kind="branch"),
        lambda p: p.update(capture_status="pending"),
        lambda p: p.pop("base_commit"),
        lambda p: p.update(manifest_digest=_sha("not the manifest")),
        lambda p: p.update(repository_id="bad repo id"),
    ],
)
def test_malformed_source_records_are_refused(workspace: Workspace, mutate: Any) -> None:
    payload = _source(1, "esnap-a", FILES_A)
    mutate(payload)
    before = workspace.counts()
    assert workspace.refused("engineering.source.record", payload)[0] == "invalid_request"
    assert workspace.counts() == before


def test_manifest_caps_unicode_and_case(workspace: Workspace) -> None:
    too_many = {f"f{index}.py": AUTH_V1 for index in range(257)}
    assert workspace.refused(
        "engineering.source.record", _source(1, "esnap-a", too_many)
    )[0] == "size_limit_exceeded"
    too_large = {f"{'d' * 400}/{index:03d}.py": AUTH_V1 for index in range(200)}
    assert workspace.refused(
        "engineering.source.record", _source(1, "esnap-a", too_large)
    )[0] == "size_limit_exceeded"
    assert workspace.counts()["omnivia_engineering_source_events"] == 0

    # Unicode and case are preserved: composed and decomposed forms, and two
    # spellings differing only in case, are four distinct paths.
    files = {
        "Src/Auth.py": AUTH_V1,
        "src/auth.py": AUTH_V2,
        "src/é.py": UTIL_V1,
        "src/é.py": README_V1,
    }
    recorded = workspace.record(_source(1, "esnap-u", files))
    stated = engineering_source.parse_source_record(_source(1, "esnap-u", files))
    assert recorded["manifest_digest"] == stated.manifest_digest
    target = engineering_source.covered_snapshot(
        workspace.holder.connection, workspace_id=WORKSPACE_ID, snapshot_id="esnap-u"
    )
    assert target is not None and dict(target.manifest) == files
    assert workspace.record(
        {**_source(2, "esnap-u2", files, predecessor="esnap-u"), "manifest_digest": stated.manifest_digest}
    )["coverage"]["covered_sequence"] == 2


@pytest.mark.parametrize(
    ("manifest", "code"),
    [
        ({**_manifest(), "extra": 1}, "invalid_request"),
        ({key: value for key, value in _manifest().items() if key != "coverage"}, "invalid_request"),
        (_manifest(coverage="most"), "invalid_request"),
        (_manifest(dependencies=[_dependency("src/auth.py", None)]), "invalid_request"),
        (_manifest(dependencies=[_dependency("../auth.py", AUTH_V1)]), "invalid_request"),
        (_manifest(dependencies=[_dependency("src/auth.py", AUTH_V1, selector_type="rename")]), "invalid_request"),
        (_manifest(dependencies=[_dependency("src/auth.py", AUTH_V1, "optional")]), "invalid_request"),
        (_manifest(dependencies=[{**_dependency("src/auth.py", AUTH_V1), "weight": 1}]), "invalid_request"),
        (_manifest(dependencies=[_dependency("src/auth.py", AUTH_V1)] * 2), "invalid_request"),
        (
            _manifest(dependencies=[_dependency(f"f{i}.py", AUTH_V1) for i in range(65)]),
            "invalid_request",
        ),
        # Wrong-typed closed-vocabulary values are refused, never a TypeError.
        ({**_manifest(), "coverage": []}, "invalid_request"),
        ({**_manifest(), "coverage": {"complete": True}}, "invalid_request"),
        (_manifest(dependencies=[{**_dependency("src/auth.py", AUTH_V1), "selector_type": []}]), "invalid_request"),
        (_manifest(dependencies=[{**_dependency("src/auth.py", AUTH_V1), "selector_type": {}}]), "invalid_request"),
        (_manifest(dependencies=[{**_dependency("src/auth.py", AUTH_V1), "meaning": []}]), "invalid_request"),
        (_manifest(dependencies=[{**_dependency("src/auth.py", AUTH_V1), "meaning": {"x": 1}}]), "invalid_request"),
        (_manifest(snapshot_id="esnap-never"), "dependency_unavailable"),
        (_manifest(stream="estream-worktree"), "dependency_unavailable"),
        (_manifest(repository="erepo-other"), "dependency_unavailable"),
    ],
)
def test_malformed_or_unanchored_dependency_manifests_are_refused(
    workspace: Workspace, manifest: dict[str, Any], code: str
) -> None:
    workspace.record(_source(1, "esnap-a", FILES_A))
    before = workspace.counts()
    governed = workspace.holder.connection.execute(
        "SELECT COUNT(*) FROM omnivia_governed_version_assemblies"
    ).fetchone()[0]
    assert workspace.refused("memory.create", _observation(manifest))[0] == code
    assert workspace.counts() == before
    assert workspace.holder.connection.execute(
        "SELECT COUNT(*) FROM omnivia_governed_version_assemblies"
    ).fetchone()[0] == governed


@pytest.mark.parametrize("wrong", [[], {}, ["git_commit"], {"complete": 1}])
def test_closed_vocabularies_refuse_wrong_types(wrong: Any) -> None:
    for field in ("snapshot_kind", "capture_status"):
        with pytest.raises(engineering_source.SourceRecordInvalid):
            engineering_source.parse_source_record({**_source(1, "esnap-a", FILES_A), field: wrong})
    with pytest.raises(engineering_source.DependencyManifestInvalid):
        engineering_source.parse_dependency_manifest({**_manifest(), "coverage": wrong})
    for field in ("selector_type", "meaning"):
        dependency = {**_dependency("src/auth.py", AUTH_V1), field: wrong}
        with pytest.raises(engineering_source.DependencyManifestInvalid):
            engineering_source.parse_dependency_manifest(_manifest(dependencies=[dependency]))


def test_applicability_that_disagrees_with_the_manifest_is_refused(
    workspace: Workspace,
) -> None:
    workspace.record(_source(1, "esnap-a", FILES_A))
    payload = _observation(_manifest())
    payload["content"]["applicability"] = {"repository_id": "erepo-other"}
    assert workspace.refused("memory.create", payload)[0] == "invalid_request"


# --- read-mode validation --------------------------------------------------------------


def test_current_safe_is_explicit_strict_and_never_downgraded(workspace: Workspace) -> None:
    workspace.record(_source(1, "esnap-a", FILES_A))
    for payload in (
        {"query": "provider", "applicability_mode": "current-safe"},
        {"query": "provider", "applicability_mode": "current_safe"},
        {
            "query": "provider",
            "view": "working_context",
            "applicability_mode": "current_safe",
            "repository_target": {"snapshot_id": "esnap-a"},
        },
        {
            "query": "provider",
            "view": "history",
            "applicability_mode": "current_safe",
            "repository_target": {"snapshot_id": "esnap-a"},
        },
    ):
        assert workspace.refused("engineering.search", payload)[0] == "invalid_request"
    assert workspace.refused(
        "engineering.context.build",
        {
            "query": "provider",
            "targets": [{"snapshot_id": "esnap-a"}],
            "profile": "investigate",
            "applicability_mode": "safe",
        },
    )[0] == "invalid_request"
    # An unrecorded target, or one stated under another repository, is pending.
    for target in (
        {"snapshot_id": "esnap-never"},
        {"repository_id": "erepo-other", "snapshot_id": "esnap-a"},
    ):
        assert workspace.refused(
            "engineering.search",
            {
                "query": "provider",
                "view": "candidates",
                "applicability_mode": "current_safe",
                "repository_target": target,
            },
        )[:2] == ("dependency_unavailable", "applicability_pending")
    # The default stays diagnostic and unchanged.
    diagnostic = workspace.ok("engineering.search", {"query": "provider", "view": "candidates"})
    assert diagnostic["coverage"] == {"projection": "current", "applicability": "unavailable"}


def test_pending_is_decided_before_the_frontier_is_read(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnivia_core_runtime.service.handlers import engineering as handlers

    workspace.record(_source(1, "esnap-a", FILES_A))
    workspace.record(_source(3, "esnap-c", FILES_A, predecessor="esnap-b"))

    def untouchable(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("the frontier was read before coverage decided")

    monkeypatch.setattr(handlers, "read_governed_record_values", untouchable)
    monkeypatch.setattr(handlers, "read_authorized_memory_snapshot", untouchable)
    monkeypatch.setattr(handlers, "rank_governed", untouchable)
    target = {"repository_id": REPOSITORY, "snapshot_id": "esnap-c"}
    assert workspace.refused(
        "engineering.search",
        {
            "query": "provider",
            "view": "candidates",
            "applicability_mode": "current_safe",
            "repository_target": target,
        },
    )[:2] == ("dependency_unavailable", "applicability_pending")
    assert workspace.refused(
        "engineering.context.build",
        {
            "query": "provider",
            "targets": [{"repository_id": REPOSITORY, "snapshot_id": "esnap-a"}, target],
            "profile": "investigate",
            "applicability_mode": "current_safe",
        },
    )[:2] == ("dependency_unavailable", "applicability_pending")


def test_current_safe_pack_targets_are_non_empty_and_bounded(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No targets never degrades to an unqualified pack, and more than
    CURRENT_SAFE_TARGET_CAP targets is refused; both before any source read."""
    from omnivia_core_runtime.service.handlers import engineering as handlers

    workspace.record(_source(1, "esnap-a", FILES_A))
    record = workspace.observe(_observation(_manifest()))
    target = {"repository_id": REPOSITORY, "snapshot_id": "esnap-a"}
    build = {"query": "provider", "profile": "investigate", "applicability_mode": "current_safe"}

    def untouchable(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("a source or frontier read ran before the target check")

    with monkeypatch.context() as patched:
        patched.setattr(engineering_source, "covered_snapshot", untouchable)
        patched.setattr(engineering_source, "evaluate_applicability", untouchable)
        patched.setattr(handlers, "read_governed_record_values", untouchable)
        patched.setattr(handlers, "read_authorized_memory_snapshot", untouchable)
        patched.setattr(handlers, "rank_governed", untouchable)
        assert workspace.refused(
            "engineering.context.build", {**build, "targets": []}
        )[0] == "invalid_request"
        assert workspace.refused(
            "engineering.context.build",
            {**build, "targets": [target] * (handlers.CURRENT_SAFE_TARGET_CAP + 1)},
        )[0] == "size_limit_exceeded"

    # Exactly the cap still reaches normal coverage and qualification.
    pack = workspace.ok(
        "engineering.context.build",
        {**build, "targets": [target] * handlers.CURRENT_SAFE_TARGET_CAP},
    )["pack"]
    assert [c["record_ref"] for c in pack["citations"]] == [record]
    assert {s["status"] for s in pack["applicability"]} == {"matched"}
    # Diagnostic is unchanged: no targets still builds.
    workspace.ok(
        "engineering.context.build",
        {**build, "targets": [], "applicability_mode": "diagnostic"},
    )


# --- fencing and atomicity ---------------------------------------------------------------


def _attempt(workspace: Workspace, payload: dict[str, Any]) -> Any:
    """One record that must fail; whether the dispatcher renders the failure or
    lets it propagate, what matters is what became durable."""
    try:
        return workspace.call("engineering.source.record", payload)
    except (RuntimeError, StaleGeneration, sqlite3.DatabaseError) as error:
        return error


def test_a_failed_record_leaves_no_partial_head_or_barrier(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = workspace.counts()

    def explode(*_args: Any, **_kwargs: Any) -> int:
        raise RuntimeError("drain failed after the head and event were written")

    monkeypatch.setattr(engineering_source, "_advance_coverage", explode)
    outcome = _attempt(workspace, _source(1, "esnap-a", FILES_A))
    assert not isinstance(outcome, SuccessResponseEnvelope)
    # No repository, stream head, snapshot or event from the failed record.
    assert workspace.counts() == before
    assert workspace.stream() is None
    assert workspace.holder.connection.in_transaction is False


def test_an_obsolete_writer_commits_nothing(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace.record(_source(1, "esnap-a", FILES_A))
    before = workspace.counts()
    original = engineering_source._advance_coverage

    def taken_over(connection: Any, *args: Any) -> int:
        covered = original(connection, *args)
        # A takeover moves the fencing generation under the open transaction.
        connection.execute(
            "UPDATE omnivia_workspace_state SET fencing_generation = fencing_generation + 1 "
            "WHERE singleton = 1"
        )
        return covered

    monkeypatch.setattr(engineering_source, "_advance_coverage", taken_over)
    outcome = _attempt(workspace, _source(2, "esnap-b", FILES_A, predecessor="esnap-a"))
    assert not isinstance(outcome, SuccessResponseEnvelope)
    assert workspace.counts() == before
    assert workspace.stream() == (REPOSITORY, PRINCIPAL, 1, 1)


def test_source_rows_are_append_only_and_guarded(workspace: Workspace) -> None:
    workspace.record(_source(1, "esnap-a", FILES_A))
    for statement in (
        "UPDATE omnivia_engineering_source_events SET manifest_digest = manifest_digest",
        "DELETE FROM omnivia_engineering_source_events",
        "DELETE FROM omnivia_engineering_source_streams",
        "UPDATE omnivia_engineering_source_streams SET covered_sequence = 0",
        "UPDATE omnivia_engineering_source_streams SET principal_id = 'intruder'",
    ):
        with (
            pytest.raises(sqlite3.DatabaseError),
            fenced_transaction(
                workspace.holder.connection,
                workspace.holder.identity,
                workspace_id=WORKSPACE_ID,
                fencing_generation=workspace.holder.generation,
            ),
        ):
            workspace.holder.connection.execute(statement)
    assert workspace.stream() == (REPOSITORY, PRINCIPAL, 1, 1)


def _fenced(workspace: Workspace) -> Any:
    return fenced_transaction(
        workspace.holder.connection,
        workspace.holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=workspace.holder.generation,
    )


def test_coverage_guard_validates_only_the_newly_covered_range(workspace: Workspace) -> None:
    workspace.record(_source(1, "esnap-1", FILES_A))
    workspace.record(_source(2, "esnap-2", FILES_A, predecessor="esnap-1"))
    workspace.record(_source(4, "esnap-4", FILES_A, predecessor="esnap-3"))
    workspace.record(_source(5, "esnap-5", FILES_A, predecessor="esnap-4"))
    assert workspace.stream() == (REPOSITORY, PRINCIPAL, 5, 2)
    # A forged advance over the gap at 3 is refused, however far it reaches.
    for covered in (3, 4, 5):
        with (
            pytest.raises(sqlite3.DatabaseError, match="missing event"),
            _fenced(workspace),
        ):
            workspace.holder.connection.execute(
                "UPDATE omnivia_engineering_source_streams SET covered_sequence = ?",
                (covered,),
            )
    assert workspace.stream() == (REPOSITORY, PRINCIPAL, 5, 2)
    # The real 3 drains 3, 4 and 5 in one multi-event advance the guard admits.
    workspace.record(_source(3, "esnap-3", FILES_A, predecessor="esnap-2"))
    assert workspace.stream() == (REPOSITORY, PRINCIPAL, 5, 5)


def test_a_sealed_dependency_set_never_changes(workspace: Workspace) -> None:
    workspace.record(_source(1, "esnap-a", FILES_A))
    record = workspace.observe(_observation(_manifest()))
    connection = workspace.holder.connection
    columns = (
        "workspace_id, dependency_id, record_id, version, selector_type, selector, "
        "meaning, producer, recorded_at_us, audit_ref, expected_digest"
    )
    extra = connection.execute(
        f"SELECT {columns} FROM omnivia_engineering_dependencies "
        "WHERE record_id = ? AND version = ? LIMIT 1",
        (record["record_id"], record["version"]),
    ).fetchone()
    forged = (extra[0], "edep-forged", *extra[2:5], "src/extra.py", *extra[6:])
    insert = (
        f"INSERT INTO omnivia_engineering_dependencies ({columns}) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
    )
    with pytest.raises(sqlite3.DatabaseError, match="sealed"), _fenced(workspace):
        connection.execute(insert, forged)
    assert workspace.counts()["omnivia_engineering_dependencies"] == len(DEPENDENCIES)
    assert workspace.status(record, "esnap-a") == "matched"

    # Should stored rows ever disagree with the sealed count (DDL to drop the seal
    # is not authorized here), the evaluator fails closed rather than using them.
    target = engineering_source.covered_snapshot(
        connection, workspace_id=WORKSPACE_ID, snapshot_id="esnap-a"
    )
    assert target is not None

    class Inconsistent:
        def __init__(self, change: Any) -> None:
            self.change = change

        def execute(self, sql: str, params: Any = ()) -> Any:
            cursor = connection.execute(sql, params)
            if "FROM omnivia_engineering_dependencies " not in sql:
                return cursor
            rows = self.change(cursor.fetchall())
            return SimpleNamespace(fetchall=lambda: rows)

    for change in (
        lambda rows: [*rows, ("whole_file", "src/extra.py", "must_match", AUTH_V1)],
        lambda rows: rows[:-1],
    ):
        assert engineering_source.evaluate_applicability(
            Inconsistent(change),  # type: ignore[arg-type]
            workspace_id=WORKSPACE_ID,
            record_id=record["record_id"],
            version=record["version"],
            evidence_available=True,
            target=target,
        ) == "unknown"
