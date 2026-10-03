"""Honest legacy Engineering Memory import (SPEC-CORE-ENGMEM-001 AC-032).

Every import here runs through the packaged `omnivia-core-service` entry point
(`main`) in process, so what is proven is the maintenance path an operator runs:
normal `ServiceRunner` ownership, one fenced transaction per note, and a redacted
receipt. A legacy note with no source revision arrives as a sealed, proposed
candidate whose evidence availability is the legacy one and whose applicability
is unknown, with no commit, reviewer, policy, audit or acceptance behind it.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import test_v06_5_s0_mutation_foundation as s0
from omnivia_core_cli.surface import APPLICATION_COMMANDS
from omnivia_core_mcp.manifest import PROFILES, exposure_manifest
from omnivia_core_runtime.ownership.fencing import fenced_transaction
from omnivia_core_runtime.service import legacy_import, source_capture
from omnivia_core_runtime.service.application import authorize_application_request
from omnivia_core_runtime.service.authorization import ServiceBinding
from omnivia_core_runtime.service.handlers.engineering import EngineeringHandlers
from omnivia_core_runtime.service.main import build_parser, main
from omnivia_core_runtime.service.operations import (
    APPLICATION_OPERATIONS,
    OperationContext,
    OperationError,
)
from omnivia_core_runtime.service.runner import ServiceRunner, ServiceSettings
from omnivia_core_runtime.service.source_capture import capture_local_source
from omnivia_core_runtime.service.versions import SERVER_VERSION
from omnivia_core_runtime.service.workspace_init import initialise_workspace
from omnivia_core_runtime.storage import engineering_preview
from omnivia_core_runtime.storage.connection import StorageError
from omnivia_core_runtime.storage.engineering_source import (
    CoveredSnapshot,
    evaluate_applicability,
)
from omnivia_core_runtime.storage.governed import read_governed_records

from omnivia_core.contracts.v1 import OPERATION_CATALOGUE, get_operation_metadata
from omnivia_core.contracts.v1.canonical_json import canonicalize

#: Text that must never reach a refusal: it stands for caller content and identity.
MARKER = "legacy-marker-7431"

#: Every table an import writes, in the order a note's rows are appended.
WRITTEN = (
    "omnivia_governed_records",
    "omnivia_governed_version_assemblies",
    "omnivia_governed_provenance_events",
    "omnivia_governed_legacy_lineage",
    "omnivia_governed_version_evidence_links",
    "omnivia_governed_version_seals",
    "omnivia_engineering_preview_projection",
)

#: Facts an import must never create: audit, review, policy, extraction, relation,
#: supersession, application lineage, repository, snapshot, commit, continuity,
#: dependency or applicability.
NEVER_WRITTEN = (
    "omnivia_application_audit_events",
    "omnivia_application_claim_lineage",
    "omnivia_application_governance_transitions",
    "omnivia_governed_extraction_lineage",
    "omnivia_governed_relation_endpoints",
    "omnivia_record_supersessions",
    "omnivia_engineering_repositories",
    "omnivia_engineering_checkouts",
    "omnivia_engineering_snapshots",
    "omnivia_engineering_sessions",
    "omnivia_engineering_checkpoints",
    "omnivia_engineering_dependency_sets",
    "omnivia_engineering_dependencies",
    "omnivia_engineering_assessments",
    "omnivia_engineering_review_attestations",
    "omnivia_engineering_source_events",
)


class _Crash(BaseException):
    """A process dying mid-note: not an error anything may catch and translate."""


def _digest(value: object) -> str:
    return f"sha256:{hashlib.sha256(canonicalize(value).encode('utf-8')).hexdigest()}"


def _legacy_note(body: str = "Credential retries mask the stale session bug") -> Any:
    # Prose naming a repository, a commit and a remote: none of it may become
    # trusted applicability.
    return {
        "title": "Stale session restoration",
        "body": body,
        "applicability": {"repository_id": "erepo-legacy", "snapshot_id": "snap-9"},
        "commit": "0123456789abcdef0123456789abcdef01234567",
        "remote": "git@example.invalid:team/app.git",
        "author": MARKER,
    }


def _entry(
    legacy_id: str = "note-17",
    legacy_version: str = "3",
    *,
    note: Any = None,
    evidence: dict[str, Any] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    value = _legacy_note() if note is None else note
    entry: dict[str, Any] = {
        "legacy_id": legacy_id,
        "legacy_version": legacy_version,
        "record_type": "knowledge.finding",
        "title": "Credential retries mask stale session restoration",
        "summary": "Legacy finding about stale session restoration.",
        "note": value,
        "note_digest": _digest(value),
        "created_at": "2024-03-01T12:00:00Z",
        "updated_at": "2024-03-02T08:30:00.250123456+01:00",
        "evidence": evidence or {"disposition": "unavailable"},
    }
    entry.update(extra)
    return entry


class _Env:
    """One initialised workspace and the service entry point that imports into it."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.workspace = tmp_path / "workspace"
        self.installation = tmp_path / "installation-state"
        result = initialise_workspace(
            workspace_root=self.workspace,
            installation_root=self.installation,
            core_version=SERVER_VERSION,
        )
        assert result.workspace_id is not None
        self.workspace_id = result.workspace_id

    def document(self, *entries: dict[str, Any], **overrides: Any) -> dict[str, Any]:
        document: dict[str, Any] = {
            "format": "omnivia.engineering-legacy-import.v1",
            "workspace_id": self.workspace_id,
            "domain_scope": "engineering.codebase",
            "notes": list(entries) or [_entry()],
        }
        document.update(overrides)
        return document

    def write(self, document: object, name: str = f"{MARKER}.json") -> Path:
        path = self.tmp / name
        if isinstance(document, bytes):
            path.write_bytes(document)
        else:
            path.write_text(json.dumps(document), encoding="utf-8")
        return path

    def run(
        self, capsys: pytest.CaptureFixture[str], path: Path
    ) -> tuple[int, dict[str, Any], str]:
        code = main(
            [
                "--workspace",
                str(self.workspace),
                "--installation-state",
                str(self.installation),
                "--core-version",
                SERVER_VERSION,
                "--import-legacy",
                str(path),
            ]
        )
        captured = capsys.readouterr()
        lines = captured.out.splitlines()
        assert len(lines) == 1, captured.out
        return code, json.loads(lines[0]), captured.err

    def refused(
        self, capsys: pytest.CaptureFixture[str], path: Path, reason: str
    ) -> None:
        """A fixed, payload-free refusal: no path, content, identity or error text."""
        code, receipt, err = self.run(capsys, path)
        assert code == 1
        assert receipt == {
            "already_imported": 0,
            "document_digest": None,
            "format": "omnivia.engineering-legacy-import-result.v1",
            "imported": 0,
            "attempted_migration_run_id": None,
            "reason": reason,
            "records": [],
            "status": "refused",
            "workspace_id": None,
        }
        assert err == reason + "\n"
        for leaked in (MARKER, str(path), path.name, "note-17", "Traceback"):
            assert leaked not in json.dumps(receipt) + err

    def partial(
        self,
        capsys: pytest.CaptureFixture[str],
        path: Path,
        reason: str,
        *,
        imported: int,
        already_imported: int,
    ) -> None:
        """A partial-success receipt: honest counts, no per-note identity at all."""
        code, receipt, err = self.run(capsys, path)
        assert code == 1
        assert receipt == {
            "already_imported": already_imported,
            "document_digest": None,
            "format": "omnivia.engineering-legacy-import-result.v1",
            "imported": imported,
            "attempted_migration_run_id": None,
            "reason": reason,
            "records": [],
            "status": "partially_imported",
            "workspace_id": None,
        }
        assert err == reason + "\n"
        for leaked in (MARKER, str(path), path.name, "note-17", "note-18", "Traceback"):
            assert leaked not in json.dumps(receipt) + err

    def rows(
        self, table: str, where: str = "", columns: str = "*"
    ) -> list[tuple[Any, ...]]:
        connection = sqlite3.connect(self.workspace / "workspace.sqlite")
        try:
            return connection.execute(
                f"SELECT {columns} FROM {table} {where}"
            ).fetchall()
        finally:
            connection.close()

    def counts(
        self, tables: tuple[str, ...] = WRITTEN + NEVER_WRITTEN
    ) -> dict[str, int]:
        return {table: len(self.rows(table)) for table in tables}

    def runner(self) -> ServiceRunner:
        runner = ServiceRunner(
            ServiceSettings(
                workspace_root=self.workspace,
                installation_root=self.installation,
                core_version=SERVER_VERSION,
                endpoint=None,
            )
        )
        assert runner.start().ready
        return runner


def _row(env: _Env, table: str, where: str) -> dict[str, Any]:
    connection = sqlite3.connect(env.workspace / "workspace.sqlite")
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(f"SELECT * FROM {table} WHERE {where}").fetchall()
    finally:
        connection.close()
    assert len(rows) == 1
    return dict(rows[0])


def _search(
    runner: ServiceRunner, workspace_id: str, view: str, **extra: object
) -> dict[str, Any]:
    """`engineering.search` through the real handler and the real authorization seam."""
    entry = get_operation_metadata("engineering.search")
    binding = ServiceBinding(
        installation_id=s0.INSTALLATION_ID, workspace_id=workspace_id
    )
    envelope = s0.envelope_for(
        entry,
        operation_input={"query": "stale session", "view": view, **extra},
        workspace_id=workspace_id,
    )
    authorized = authorize_application_request(
        envelope,
        session=s0.session_for(entry, workspaces=frozenset({workspace_id})),
        binding=binding,
        supported_capabilities=s0.SUPPORTED,
    )
    handlers = EngineeringHandlers(
        service=SimpleNamespace(connection=runner.connection, identity=runner.identity),
        binding=binding,
    )
    result = handlers.engineering_search(
        OperationContext(
            request=envelope,
            principal=authorized.principal_id,
            workspace_id=workspace_id,
            granted_operations=frozenset({entry.name}),
            authorization=authorized,
        )
    )
    return dict(result)


def test_a_note_without_source_revisions_is_imported_as_an_honest_candidate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    env = _Env(tmp_path)
    before = env.counts(NEVER_WRITTEN)
    entry = _entry()
    document = env.document(entry)
    code, receipt, err = env.run(capsys, env.write(document))

    assert code == 0, receipt
    assert err == ""
    document_hex = hashlib.sha256(canonicalize(document).encode("utf-8")).hexdigest()
    run_id = f"mig-{document_hex}"
    assert receipt["status"] == "imported"
    assert receipt["format"] == "omnivia.engineering-legacy-import-result.v1"
    assert receipt["workspace_id"] == env.workspace_id
    assert receipt["attempted_migration_run_id"] == run_id
    assert receipt["document_digest"] == f"sha256:{document_hex}"
    assert (receipt["imported"], receipt["already_imported"]) == (1, 0)
    [mapping] = receipt["records"]
    assert (mapping["legacy_id"], mapping["legacy_version"]) == ("note-17", "3")
    assert mapping["status"] == "imported"
    assert (mapping["migration_run_id"], mapping["append_ordinal"]) == (run_id, 1)
    # No caller content and no path: the note's author and the document location.
    assert MARKER not in json.dumps(receipt)
    assert str(tmp_path) not in json.dumps(receipt)

    record_id, version_id = mapping["record_id"], mapping["version"]
    assert (
        _row(env, "omnivia_governed_records", f"governed_record_id = '{record_id}'")[
            "record_type"
        ]
        == "knowledge.finding"
    )
    assembly = _row(
        env,
        "omnivia_governed_version_assemblies",
        f"governed_record_version_id = '{version_id}'",
    )
    assert {
        key: assembly[key]
        for key in (
            "governed_record_id",
            "record_type",
            "domain_scope",
            "layer",
            "authority_level",
            "governance_disposition",
            "candidate_origin",
            "extraction_kind",
            "decision_source_kind",
            "decision_source_id",
            "authority_policy_id",
            "authority_policy_version",
            "policy_decision_ref",
            "evidence_disposition",
            "reason_code",
            "correlation_kind",
            "correlation_id",
            "audit_ref",
            "assertion_actor_id",
            "assertion_actor_kind",
            "assertion_actor_role",
            "append_ordinal",
            "valid_to_us",
        )
    } == {
        "governed_record_id": record_id,
        "record_type": "knowledge.finding",
        "domain_scope": "engineering.codebase",
        "layer": "candidate",
        "authority_level": "proposed",
        "governance_disposition": None,
        "candidate_origin": "migrated_legacy_claim",
        "extraction_kind": None,
        "decision_source_kind": None,
        "decision_source_id": None,
        "authority_policy_id": None,
        "authority_policy_version": None,
        "policy_decision_ref": None,
        "evidence_disposition": "unavailable",
        "reason_code": "evidence.unavailable",
        "correlation_kind": "migration_run",
        "correlation_id": run_id,
        "audit_ref": None,
        "assertion_actor_id": "core-service",
        "assertion_actor_kind": "service",
        "assertion_actor_role": "legacy_import",
        "append_ordinal": 1,
        "valid_to_us": None,
    }

    # The stored content is the exact canonical envelope, and the original note sits
    # in it whole, with its own digest and the legacy times verbatim.
    content_json = assembly["content_json"]
    assert canonicalize(json.loads(content_json)) == content_json
    assert assembly["content_digest"] == (
        f"sha256:{hashlib.sha256(content_json.encode('utf-8')).hexdigest()}"
    )
    assert json.loads(content_json) == {
        "kind": "legacy_note",
        "title": entry["title"],
        "summary": entry["summary"],
        "legacy": {
            "id": "note-17",
            "version": "3",
            "note": entry["note"],
            "note_digest": entry["note_digest"],
            "created_at": "2024-03-01T12:00:00Z",
            "updated_at": "2024-03-02T08:30:00.250123456+01:00",
        },
    }
    assert "applicability" not in json.loads(content_json)

    event = _row(
        env,
        "omnivia_governed_provenance_events",
        f"assembly_id = '{assembly['assembly_id']}'",
    )
    assert (
        event["action"],
        event["actor_id"],
        event["actor_kind"],
        event["actor_role"],
        event["policy_id"],
        event["policy_version"],
        event["audit_ref"],
        event["predecessor_record_id"],
        event["predecessor_version_id"],
        event["reason_code"],
        event["evidence_disposition"],
        event["correlation_id"],
    ) == (
        "candidate.legacy_migrated",
        "core-service",
        "service",
        "legacy_import",
        None,
        None,
        None,
        None,
        None,
        "evidence.unavailable",
        "unavailable",
        run_id,
    )
    lineage = _row(
        env,
        "omnivia_governed_legacy_lineage",
        f"assembly_id = '{assembly['assembly_id']}'",
    )
    assert (
        lineage["provenance_event_id"],
        lineage["legacy_source_id"],
        lineage["legacy_source_version"],
        lineage["legacy_source_digest"],
        lineage["migration_run_id"],
        lineage["evidence_disposition"],
    ) == (
        event["provenance_event_id"],
        "note-17",
        "3",
        entry["note_digest"],
        run_id,
        "unavailable",
    )
    assert (
        _row(
            env,
            "omnivia_governed_version_seals",
            f"assembly_id = '{assembly['assembly_id']}'",
        )["correlation_kind"]
        == "migration_run"
    )

    # Applicability is unknown: nothing the note said reached the projection.
    preview = _row(
        env,
        "omnivia_engineering_preview_projection",
        f"assembly_id = '{assembly['assembly_id']}'",
    )
    assert (
        preview["observation_kind"],
        preview["repository_id"],
        preview["snapshot_id"],
        preview["assertion_basis"],
        preview["topic_key"],
    ) == ("legacy_note", None, None, None, None)

    assert env.counts(WRITTEN) == dict.fromkeys(WRITTEN, 1) | {
        "omnivia_governed_version_evidence_links": 0
    }
    assert env.counts(NEVER_WRITTEN) == before


def test_an_exact_replay_is_already_imported_and_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    env = _Env(tmp_path)
    path = env.write(env.document(_entry()))
    _, first, _ = env.run(capsys, path)
    written = env.counts()

    code, again, err = env.run(capsys, path)
    assert code == 0 and err == ""
    assert again["status"] == "already_imported"
    assert (again["imported"], again["already_imported"]) == (0, 1)
    assert again["records"] == [first["records"][0] | {"status": "already_imported"}]
    assert again["attempted_migration_run_id"] == first["attempted_migration_run_id"]
    assert env.counts() == written

    # Another document attempts another run, while the first note keeps the immutable
    # run and ordinal written by the document that originally imported it.
    other = env.write(env.document(_entry(), _entry("note-18")), name="second.json")
    code, mixed, _ = env.run(capsys, other)
    assert code == 0
    assert mixed["attempted_migration_run_id"] != first["attempted_migration_run_id"]
    assert [record["status"] for record in mixed["records"]] == [
        "already_imported",
        "imported",
    ]
    assert mixed["records"][0] == again["records"][0]
    assert (
        mixed["records"][1]["migration_run_id"],
        mixed["records"][1]["append_ordinal"],
    ) == (mixed["attempted_migration_run_id"], 2)
    assert env.counts(("omnivia_governed_version_assemblies",)) == {
        "omnivia_governed_version_assemblies": 2
    }

    # Ordering an overlap after a new note does not rewrite its durable position or
    # claim that the reused note belongs to this third document's run.
    reversed_overlap = env.write(
        env.document(_entry("note-19"), _entry()), name="reversed-overlap.json"
    )
    code, reversed_receipt, _ = env.run(capsys, reversed_overlap)
    assert code == 0
    assert [record["status"] for record in reversed_receipt["records"]] == [
        "imported",
        "already_imported",
    ]
    new_record, original_record = reversed_receipt["records"]
    assert (
        new_record["migration_run_id"],
        new_record["append_ordinal"],
    ) == (reversed_receipt["attempted_migration_run_id"], 1)
    assert original_record == again["records"][0]
    assert env.counts(("omnivia_governed_version_assemblies",)) == {
        "omnivia_governed_version_assemblies": 3
    }


@pytest.mark.parametrize(
    "changed",
    [
        {"note": _legacy_note("A different body under the same legacy version")},
        {"title": "A different title under the same legacy version"},
        {"updated_at": "2025-01-01T00:00:00Z"},
        {"record_type": "knowledge.risk"},
        {"evidence": {"disposition": "redacted"}},
    ],
    ids=["note", "title", "legacy-time", "record-type", "evidence"],
)
def test_a_changed_mapping_under_an_imported_identity_is_refused_without_writes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], changed: dict[str, Any]
) -> None:
    env = _Env(tmp_path)
    env.run(capsys, env.write(env.document(_entry())))
    written = env.counts()

    tampered = _entry(**changed)
    if "note" in changed:
        tampered["note_digest"] = _digest(changed["note"])
    # The new note after it is refused too: the whole document is checked first.
    path = env.write(env.document(tampered, _entry("note-18")), name="tampered.json")
    env.refused(
        capsys,
        path,
        "a legacy note identity is already imported with different content",
    )
    assert env.counts() == written


def test_plan_queries_name_only_the_requested_legacy_identities(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`_plan`'s lineage and evidence-link predicates are bounded by the document,
    not by the workspace: an unrelated legacy identity's id, version and assembly
    id never appear in either query. This proves the predicate's extent only --
    it does not claim SQLite plans either query with an index-backed scan; that
    still needs a future migration on
    ``(workspace_id, legacy_source_id, legacy_source_version)``.
    """
    env = _Env(tmp_path)
    noise_source = tmp_path / "noise-evidence.txt"
    noise_source.write_text("noise evidence\n", encoding="utf-8")
    noise_evidence = capture_local_source(
        workspace_root=env.workspace,
        installation_root=env.installation,
        source_path=noise_source,
        source_id="noise-evidence-1",
        media_type="text/plain",
        core_version=SERVER_VERSION,
    )
    assert noise_evidence.evidence_id is not None
    keep_source = tmp_path / "keep-evidence.txt"
    keep_source.write_text("keep evidence\n", encoding="utf-8")
    keep_evidence = capture_local_source(
        workspace_root=env.workspace,
        installation_root=env.installation,
        source_path=keep_source,
        source_id="keep-evidence-1",
        media_type="text/plain",
        core_version=SERVER_VERSION,
    )
    assert keep_evidence.evidence_id is not None

    keep_1_evidence = {
        "disposition": "available",
        "evidence_ids": [keep_evidence.evidence_id],
    }
    document = env.document(
        _entry(
            "noise-1",
            evidence={
                "disposition": "available",
                "evidence_ids": [noise_evidence.evidence_id],
            },
        ),
        _entry("keep-1", evidence=keep_1_evidence),
        _entry("keep-2"),
    )
    code, _, _ = env.run(capsys, env.write(document))
    assert code == 0

    requested = (_entry("keep-1", evidence=keep_1_evidence), _entry("keep-2"))
    notes = tuple(
        legacy_import._note(ordinal, entry)
        for ordinal, entry in enumerate(requested, 1)
    )
    traced: list[str] = []
    runner = env.runner()
    try:
        assert runner.connection is not None
        runner.connection.set_trace_callback(traced.append)
        try:
            legacy_import._plan(runner.connection, env.workspace_id, notes)
        finally:
            runner.connection.set_trace_callback(None)
    finally:
        runner.stop()

    [lineage_sql] = [
        sql
        for sql in traced
        if "FROM omnivia_governed_legacy_lineage l" in sql
        and "JOIN omnivia_authoritative_governed_versions a" in sql
    ]
    assert "'keep-1'" in lineage_sql and "'keep-2'" in lineage_sql
    assert "'noise-1'" not in lineage_sql

    # The evidence-link predicate names assembly ids, not legacy ids or evidence
    # ids: prove it is bounded to exactly the assemblies the (already bounded)
    # lineage query matched, and never reaches the unrelated one.
    assembly_by_legacy_id = {
        str(legacy_id): str(assembly_id)
        for legacy_id, assembly_id in env.rows(
            "omnivia_governed_legacy_lineage", columns="legacy_source_id, assembly_id"
        )
    }
    [evidence_sql] = [
        sql for sql in traced if "FROM omnivia_governed_version_evidence_links k" in sql
    ]
    assert assembly_by_legacy_id["keep-1"] in evidence_sql
    assert assembly_by_legacy_id["keep-2"] in evidence_sql
    assert assembly_by_legacy_id["noise-1"] not in evidence_sql


def test_storage_refuses_duplicate_legacy_identity(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The authoritative store enforces the identity used for idempotency."""
    env = _Env(tmp_path)
    entry = _entry()
    code, _, _ = env.run(capsys, env.write(env.document(entry)))
    assert code == 0
    note = legacy_import._note(1, entry)

    runner = env.runner()
    try:
        assert runner.connection is not None
        assert runner.identity is not None
        assert runner.generation is not None
        with pytest.raises(
            sqlite3.IntegrityError,
            match="omnivia_governed_legacy_lineage.workspace_id",
        ), fenced_transaction(
            runner.connection,
            runner.identity,
            workspace_id=env.workspace_id,
            fencing_generation=runner.generation,
        ) as fenced:
            legacy_import._write(
                fenced, env.workspace_id, "mig-duplicate-lineage", note
            )
    finally:
        runner.stop()


def _raw_document(env: _Env, text: str) -> bytes:
    return text.replace("WORKSPACE", env.workspace_id).encode("utf-8")


@pytest.mark.parametrize(
    ("case", "reason"),
    [
        ("not-json", "import document is not strict JSON"),
        ("duplicate-member", "import document is not strict JSON"),
        ("nan", "import document is not strict JSON"),
        ("inexact-integer", "import document is not strict JSON"),
        ("unknown-top-level-member", "import document does not match the v1 format"),
        ("unknown-note-member", "import document does not match the v1 format"),
        ("reviewer-smuggled", "import document does not match the v1 format"),
        ("commit-smuggled", "import document does not match the v1 format"),
        ("wrong-type", "import document does not match the v1 format"),
        ("wrong-format", "import document does not match the v1 format"),
        ("unknown-record-type", "import document does not match the v1 format"),
        ("empty-notes", "import document does not match the v1 format"),
        ("malformed-digest", "import document does not match the v1 format"),
        ("bad-time", "import document does not match the v1 format"),
        ("untruncated-title", "import document does not match the v1 format"),
        ("ids-without-availability", "import document does not match the v1 format"),
        ("availability-without-ids", "import document does not match the v1 format"),
        ("duplicate-evidence", "import document does not match the v1 format"),
        ("too-many-notes", "import document does not match the v1 format"),
        ("wrong-digest", "a note digest does not match its canonical content"),
        ("repeated-identity", "a legacy note identity is repeated in the document"),
        ("oversized-content", "a note exceeds the engineering content bound"),
        (
            "oversized-file",
            "import document is not one stable regular file within bounds",
        ),
    ],
)
def test_a_malformed_document_fails_closed_before_ownership(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    reason: str,
) -> None:
    env = _Env(tmp_path)
    entry = _entry(legacy_id=f"note-{MARKER}")
    document = env.document(entry)
    raw: bytes | None = None
    if case == "not-json":
        raw = f'{{"format": "{MARKER}"'.encode()
    elif case == "duplicate-member":
        raw = _raw_document(
            env,
            '{"format": "omnivia.engineering-legacy-import.v1", "workspace_id": '
            f'"WORKSPACE", "workspace_id": "{MARKER}", "domain_scope": '
            '"engineering.codebase", "notes": []}',
        )
    elif case == "nan":
        raw = json.dumps(document).replace('"3"', "NaN").encode()
    elif case == "inexact-integer":
        entry["note"] = {"count": 9007199254740993, "author": MARKER}
    elif case == "unknown-top-level-member":
        document["reviewer"] = MARKER
    elif case == "unknown-note-member":
        entry[MARKER] = True
    elif case == "reviewer-smuggled":
        entry["reviewer"] = MARKER
    elif case == "commit-smuggled":
        entry["commit"] = "0123456789abcdef0123456789abcdef01234567"
    elif case == "wrong-type":
        entry["legacy_version"] = 3
    elif case == "wrong-format":
        document["format"] = "omnivia.engineering-legacy-import.v2"
    elif case == "unknown-record-type":
        entry["record_type"] = "knowledge.claim"
    elif case == "empty-notes":
        document["notes"] = []
    elif case == "malformed-digest":
        entry["note_digest"] = f"sha256:{MARKER}"
    elif case == "bad-time":
        entry["created_at"] = "2024-02-30T00:00:00Z"
    elif case == "untruncated-title":
        entry["title"] = "t" * 201
    elif case == "ids-without-availability":
        entry["evidence"] = {"disposition": "unavailable", "evidence_ids": [MARKER]}
    elif case == "availability-without-ids":
        entry["evidence"] = {"disposition": "available"}
    elif case == "duplicate-evidence":
        entry["evidence"] = {
            "disposition": "available",
            "evidence_ids": [MARKER, MARKER],
        }
    elif case == "too-many-notes":
        document["notes"] = [
            _entry(f"note-{ordinal}") for ordinal in range(legacy_import.MAX_NOTES + 1)
        ]
    elif case == "wrong-digest":
        entry["note_digest"] = _digest({"a different": "note"})
    elif case == "repeated-identity":
        document["notes"] = [entry, dict(entry)]
    elif case == "oversized-content":
        entry["note"] = {"body": MARKER * 9000}
        entry["note_digest"] = _digest(entry["note"])
    elif case == "oversized-file":
        monkeypatch.setattr(source_capture, "MAX_SOURCE_BYTES", 64)
    if case == "inexact-integer":
        entry["note_digest"] = f"sha256:{'0' * 64}"
    path = env.write(raw if raw is not None else document)

    env.refused(capsys, path, reason)
    assert env.counts(WRITTEN) == dict.fromkeys(WRITTEN, 0)
    # Refused before ownership: no lease was ever taken.
    assert env.rows("omnivia_workspace_lease") == []


def test_the_largest_receipt_is_bounded() -> None:
    """Every maximally wide row still fits the maintenance output contract."""
    records = tuple(
        legacy_import.LegacyImportRecord(
            legacy_id=f"{ordinal:03d}" + "i" * 125,
            legacy_version="l" * 128,
            record_id="r" * 128,
            version="v" * 128,
            migration_run_id="m" * 128,
            append_ordinal=ordinal,
            status="already_imported",
        )
        for ordinal in range(1, legacy_import.MAX_NOTES + 1)
    )
    receipt = legacy_import.LegacyImportResult(
        status="already_imported",
        workspace_id="w" * 128,
        attempted_migration_run_id="m" * 128,
        document_digest=f"sha256:{'0' * 64}",
        records=records,
        reason="legacy notes are imported as proposed candidates",
        already_imported_count=len(records),
    )
    rendered = json.dumps(receipt.to_dict(), sort_keys=True).encode("utf-8") + b"\n"
    assert len(rendered) <= legacy_import.MAX_RESULT_BYTES


def test_symlinked_missing_nonregular_and_changing_documents_are_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing, non-regular, changing or symlinked *leaf* is refused.

    `path` itself is supplied by a trusted local operator running this
    maintenance command, so a symlink in one of `path`'s ancestor directories is
    outside this reader's threat boundary and is not exercised here; only the
    leaf is checked.
    """
    env = _Env(tmp_path)
    reason = "import document is not one stable regular file within bounds"
    path = env.write(env.document(_entry()))

    env.refused(capsys, tmp_path / f"missing-{MARKER}.json", reason)
    directory = tmp_path / f"dir-{MARKER}"
    directory.mkdir()
    env.refused(capsys, directory, reason)

    real_read = os.read
    done: list[bool] = []

    def changing(fd: int, n: int) -> bytes:
        data = real_read(fd, n)
        if not done:
            done.append(True)
            with path.open("ab") as handle:
                handle.write(b" ")
        return data

    monkeypatch.setattr(source_capture.os, "read", changing)
    env.refused(capsys, path, reason)
    monkeypatch.undo()
    assert env.counts(WRITTEN) == dict.fromkeys(WRITTEN, 0)

    link = tmp_path / f"link-{MARKER}.json"
    try:
        link.symlink_to(path)
    except OSError:
        pytest.skip("cannot create symlinks")
    env.refused(capsys, link, reason)
    assert env.counts(WRITTEN) == dict.fromkeys(WRITTEN, 0)


def test_a_foreign_workspace_or_domain_is_refused_before_any_write(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    env = _Env(tmp_path)
    env.refused(
        capsys,
        env.write(env.document(workspace_id=f"ws-{MARKER}")),
        "import document is bound to another workspace",
    )
    for scope in ("engineering.other", "personal.notes", f"x-{MARKER}", None):
        env.refused(
            capsys,
            env.write(env.document(domain_scope=scope), name=f"{scope}.json"),
            "import document is not bound to the engineering domain",
        )
    assert env.counts(WRITTEN) == dict.fromkeys(WRITTEN, 0)


def test_the_import_needs_normal_workspace_ownership(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    env = _Env(tmp_path)
    path = env.write(env.document(_entry()))
    owner = env.runner()
    try:
        env.refused(capsys, path, "workspace ownership was refused")
    finally:
        owner.stop()
    assert env.counts(WRITTEN) == dict.fromkeys(WRITTEN, 0)
    code, receipt, _ = env.run(capsys, path)
    assert (code, receipt["imported"]) == (0, 1)


def test_imported_candidates_are_visible_only_as_candidates(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    env = _Env(tmp_path)
    _, receipt, _ = env.run(capsys, env.write(env.document(_entry())))
    record_id, version_id = (
        receipt["records"][0]["record_id"],
        receipt["records"][0]["version"],
    )

    runner = env.runner()
    try:
        assert runner.connection is not None
        now_us = 2**62
        [candidate] = read_governed_records(
            runner.connection,
            workspace_id=env.workspace_id,
            resolution_instant_us=now_us,
            view="candidates",
        )
        identity = candidate.provenance.identity
        assert (identity.record_id, identity.version) == (record_id, version_id)
        assert (identity.layer, identity.governance_state) == ("l1", "candidate")
        assert candidate.authority_level == "proposed"
        assert candidate.reviewer is None
        assert candidate.provenance.evidence_disposition == "unavailable"
        assert candidate.provenance.sources == ()
        assert candidate.provenance.assertion is None
        [history] = candidate.provenance.history
        assert (history.action, history.actor_id, history.actor_kind) == (
            "candidate.legacy_migrated",
            "core-service",
            "service",
        )
        assert history.evidence is None
        for view in (None, "current_canonical", "history"):
            assert (
                read_governed_records(
                    runner.connection,
                    workspace_id=env.workspace_id,
                    resolution_instant_us=now_us,
                    view=view,
                )
                == ()
            )

        found = _search(runner, env.workspace_id, "candidates")["previews"]
        [preview] = [item for item in found if item["record_id"] == record_id]
        assert preview["governance_state"] == "candidate"
        assert preview["applicability"] == "not_evaluated"
        assert preview["observation_kind"] == "legacy_note"
        assert _search(runner, env.workspace_id, "accepted")["previews"] == []

        # A current-safe query cannot turn unproved legacy prose into applicable
        # knowledge. It refuses because no trusted dependency coverage exists.
        with pytest.raises(OperationError) as pending:
            _search(
                runner,
                env.workspace_id,
                "candidates",
                applicability_mode="current_safe",
                repository_target={"snapshot_id": "snap-9"},
            )
        assert (pending.value.code, pending.value.message) == (
            "dependency_unavailable",
            "applicability_pending",
        )

        # No dependency set exists, so applicability is unknown at any target.
        assert (
            evaluate_applicability(
                runner.connection,
                workspace_id=env.workspace_id,
                record_id=record_id,
                version=version_id,
                evidence_available=False,
                target=CoveredSnapshot(
                    repository_id="erepo-legacy",
                    stream_id="stream-1",
                    sequence=1,
                    snapshot_id="snap-9",
                    capture_status="complete",
                    manifest_digest=f"sha256:{'0' * 64}",
                    manifest={},
                ),
            )
            == "unknown"
        )
    finally:
        runner.stop()


def test_a_crash_after_a_committed_note_resumes_without_partial_records(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _Env(tmp_path)
    path = env.write(env.document(_entry(), _entry("note-18"), _entry("note-19")))
    real_preview = engineering_preview.record_preview
    calls: list[str] = []

    def crash_on_second(connection: sqlite3.Connection, **kwargs: str) -> None:
        calls.append(kwargs["assembly_id"])
        real_preview(connection, **kwargs)
        if len(calls) == 2:
            raise _Crash

    monkeypatch.setattr(engineering_preview, "record_preview", crash_on_second)
    with pytest.raises(_Crash):
        main(
            [
                "--workspace",
                str(env.workspace),
                "--installation-state",
                str(env.installation),
                "--core-version",
                SERVER_VERSION,
                "--import-legacy",
                str(path),
            ]
        )
    capsys.readouterr()
    monkeypatch.undo()
    # The first note is whole; the second, interrupted after its last row, left none.
    assert env.counts(WRITTEN) == dict.fromkeys(WRITTEN, 1) | {
        "omnivia_governed_version_evidence_links": 0
    }
    assert env.rows("omnivia_governed_version_assemblies", columns="assembly_id") == [
        (calls[0],)
    ]

    code, receipt, _ = env.run(capsys, path)
    assert code == 0
    assert [record["status"] for record in receipt["records"]] == [
        "already_imported",
        "imported",
        "imported",
    ]
    assert env.counts(WRITTEN) == dict.fromkeys(WRITTEN, 3) | {
        "omnivia_governed_version_evidence_links": 0
    }
    # One run, resumed: each note keeps its document position as its run ordinal.
    assert env.rows(
        "omnivia_governed_version_assemblies",
        "ORDER BY append_ordinal",
        columns="correlation_id, append_ordinal",
    ) == [(receipt["attempted_migration_run_id"], ordinal) for ordinal in (1, 2, 3)]
    assert [
        (record["migration_run_id"], record["append_ordinal"])
        for record in receipt["records"]
    ] == [(receipt["attempted_migration_run_id"], ordinal) for ordinal in (1, 2, 3)]


def test_a_fault_after_a_durable_note_is_partial_not_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A note fails, but an earlier note in the same attempt is already durable:

    the receipt is honest about that (`partially_imported`, exact counts) instead
    of pretending nothing happened, and it still names no note's identity.
    """
    env = _Env(tmp_path)
    document = env.document(_entry(), _entry("note-18"))
    path = env.write(document)
    run_id = f"mig-{hashlib.sha256(canonicalize(document).encode('utf-8')).hexdigest()}"
    partial_reason = (
        "a note could not be committed; earlier notes are durable and exact replay "
        "resumes the document"
    )
    real_identifier = legacy_import.random_identifier

    def colliding_seal(prefix: str) -> str:
        # The second note's seal collides with the first's, so 0009's seal trigger
        # refuses it after that note's record, version, event and lineage exist.
        return "seal-collision" if prefix == "seal" else real_identifier(prefix)

    monkeypatch.setattr(legacy_import, "random_identifier", colliding_seal)
    # note-17 committed whole before note-18's seal collided: partial, not refused.
    env.partial(capsys, path, partial_reason, imported=1, already_imported=0)
    monkeypatch.undo()
    assert env.counts(WRITTEN) == dict.fromkeys(WRITTEN, 1) | {
        "omnivia_governed_version_evidence_links": 0
    }
    assert env.rows("omnivia_governed_legacy_lineage", columns="legacy_source_id") == [
        ("note-17",)
    ]

    def failing_preview(connection: sqlite3.Connection, **kwargs: str) -> None:
        raise StorageError(MARKER)

    monkeypatch.setattr(engineering_preview, "record_preview", failing_preview)
    # note-17 is now already_imported (durable from the attempt above) before
    # note-18's fresh write fails: still partial, this time with no new writes.
    env.partial(capsys, path, partial_reason, imported=0, already_imported=1)
    monkeypatch.undo()
    assert env.counts(WRITTEN)["omnivia_governed_version_assemblies"] == 1

    code, receipt, _ = env.run(capsys, path)
    assert code == 0
    assert [record["status"] for record in receipt["records"]] == [
        "already_imported",
        "imported",
    ]
    # The recovered receipt reports the authoritative durable run and ordinal
    # sealed for note-17 during the earlier partial attempt, not a fresh one.
    assert receipt["attempted_migration_run_id"] == run_id
    assert (
        receipt["records"][0]["migration_run_id"],
        receipt["records"][0]["append_ordinal"],
    ) == (run_id, 1)


def test_a_fault_on_the_first_note_with_nothing_durable_yet_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A note fails with no prior durable or replayed note in the attempt: the
    whole document is refused, fully payload-free, exactly as before.
    """
    env = _Env(tmp_path)
    path = env.write(env.document(_entry()))

    def failing_preview(connection: sqlite3.Connection, **kwargs: str) -> None:
        raise StorageError(MARKER)

    monkeypatch.setattr(engineering_preview, "record_preview", failing_preview)
    env.refused(
        capsys,
        path,
        "a note could not be committed; the failed note was rolled back and "
        "exact replay resumes the document",
    )
    monkeypatch.undo()
    assert env.counts(WRITTEN) == dict.fromkeys(WRITTEN, 0)

    code, receipt, _ = env.run(capsys, path)
    assert (code, receipt["records"][0]["status"]) == (0, "imported")


def test_available_evidence_is_linked_only_when_the_workspace_holds_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    env = _Env(tmp_path)
    source = tmp_path / "legacy-attachment.txt"
    source.write_text("the original legacy attachment\n", encoding="utf-8")
    captured = capture_local_source(
        workspace_root=env.workspace,
        installation_root=env.installation,
        source_path=source,
        source_id="legacy-attachment-17",
        media_type="text/plain",
        core_version=SERVER_VERSION,
    )
    assert captured.evidence_id is not None
    second_source = tmp_path / "legacy-attachment-two.txt"
    second_source.write_text("the second original attachment\n", encoding="utf-8")
    second = capture_local_source(
        workspace_root=env.workspace,
        installation_root=env.installation,
        source_path=second_source,
        source_id="legacy-attachment-18",
        media_type="text/plain",
        core_version=SERVER_VERSION,
    )
    assert second.evidence_id is not None

    missing = env.document(
        _entry(evidence={"disposition": "available", "evidence_ids": [f"evd-{MARKER}"]})
    )
    env.refused(
        capsys,
        env.write(missing, name="missing.json"),
        "a note names evidence this workspace does not hold",
    )
    assert env.counts(WRITTEN) == dict.fromkeys(WRITTEN, 0)

    document = env.document(
        _entry(
            evidence={
                "disposition": "available",
                "evidence_ids": [second.evidence_id, captured.evidence_id],
            }
        ),
        _entry("note-18", evidence={"disposition": "redacted"}),
    )
    code, receipt, _ = env.run(capsys, env.write(document, name="evidence.json"))
    assert code == 0
    available, redacted = (record["version"] for record in receipt["records"])
    linked = _row(
        env,
        "omnivia_governed_version_assemblies",
        f"governed_record_version_id = '{available}'",
    )
    assert (linked["evidence_disposition"], linked["reason_code"]) == (
        "available",
        None,
    )
    expected_evidence = sorted((captured.evidence_id, second.evidence_id))
    assert env.rows(
        "omnivia_governed_version_evidence_links",
        "ORDER BY link_ordinal",
        columns="assembly_id, evidence_id, normalized_record_id, normalized_span_id",
    ) == [
        (linked["assembly_id"], evidence_id, None, None)
        for evidence_id in expected_evidence
    ]
    hidden = _row(
        env,
        "omnivia_governed_version_assemblies",
        f"governed_record_version_id = '{redacted}'",
    )
    assert (hidden["evidence_disposition"], hidden["reason_code"]) == (
        "redacted",
        "evidence.redacted",
    )

    # Evidence is set-valued. Another document can list the same identifiers in
    # the opposite order without inventing a new mapping or lineage run.
    reordered = env.document(
        _entry(
            evidence={
                "disposition": "available",
                "evidence_ids": [captured.evidence_id, second.evidence_id],
            }
        ),
        _entry("note-18", evidence={"disposition": "redacted"}),
    )
    code, reordered_receipt, _ = env.run(
        capsys, env.write(reordered, name="evidence-reordered.json")
    )
    assert code == 0
    assert reordered_receipt["status"] == "already_imported"
    assert reordered_receipt["records"] == [
        record | {"status": "already_imported"} for record in receipt["records"]
    ]
    assert (
        reordered_receipt["attempted_migration_run_id"]
        != receipt["attempted_migration_run_id"]
    )

    runner = env.runner()
    try:
        assert runner.connection is not None
        cited = {
            record.provenance.identity.version: record
            for record in read_governed_records(
                runner.connection,
                workspace_id=env.workspace_id,
                resolution_instant_us=2**62,
                view="candidates",
            )
        }
        assert {
            (source.kind, source.source_id)
            for source in cited[available].provenance.sources
        } == {
            ("document", "legacy-attachment-17"),
            ("document", "legacy-attachment-18"),
        }
        assert cited[redacted].provenance.sources == ()

        # Tombstoned evidence is no longer evidence this workspace can cite.
        assert runner.identity is not None and runner.generation is not None
        with fenced_transaction(
            runner.connection,
            runner.identity,
            workspace_id=env.workspace_id,
            fencing_generation=runner.generation,
        ) as fenced:
            fenced.execute(
                "INSERT INTO omnivia_evidence_provenance_events "
                "(provenance_event_id, evidence_id, workspace_id, provenance_sequence, "
                "actor_id, actor_kind, action, occurred_at_us, reason_code, "
                "reason_comment, parser_status, ingestion_status, "
                "tombstoned_observation, source_kind, source_native_id, audit_ref) "
                "VALUES ('prv-tombstone-1', ?, ?, 2, 'core-service', 'service', "
                "'tombstoned', 1, NULL, NULL, NULL, NULL, 1, 'document', "
                "'legacy-attachment-17', NULL)",
                (captured.evidence_id, env.workspace_id),
            )
    finally:
        runner.stop()
    tombstoned = env.document(
        _entry(
            "note-19",
            evidence={
                "disposition": "available",
                "evidence_ids": [captured.evidence_id],
            },
        )
    )
    env.refused(
        capsys,
        env.write(tombstoned, name="tombstoned.json"),
        "a note names evidence this workspace does not hold",
    )
    # What was imported while the evidence was held replays unchanged.
    code, replay, _ = env.run(capsys, env.write(document, name="evidence.json"))
    assert (code, replay["status"], replay["records"]) == (
        0,
        "already_imported",
        [record | {"status": "already_imported"} for record in receipt["records"]],
    )


def test_the_import_is_a_service_maintenance_mode_and_never_an_operation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    surfaces = {
        "catalogue": {entry.name for entry in OPERATION_CATALOGUE},
        "application": set(APPLICATION_OPERATIONS),
        "mcp": {
            exposed.operation
            for profile in PROFILES
            for exposed in exposure_manifest(profile)
        },
        "cli": {command.operation for command in APPLICATION_COMMANDS},
    }
    for surface, operations in surfaces.items():
        assert not [name for name in operations if "legacy" in name], surface
    assert "--import-legacy" in build_parser().format_help()

    env = _Env(tmp_path)
    path = env.write(env.document(_entry()))
    code = main(
        [
            "--workspace",
            str(env.workspace),
            "--installation-state",
            str(env.installation),
            "--capture-source",
            str(path),
            "--source-id",
            "source-1",
            "--import-legacy",
            str(path),
        ]
    )
    captured = capsys.readouterr()
    assert (code, captured.out) == (2, "")
    assert env.counts(WRITTEN) == dict.fromkeys(WRITTEN, 0)
