"""Migration-level qualification for the document-intake persistence substrate."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from omnivia_core_runtime.ownership.identity import FakeProcessEvidence, ProcessEvidence
from omnivia_core_runtime.service import document_intake as service
from omnivia_core_runtime.service import runner as runner_module
from omnivia_core_runtime.service.document_intake import (
    owned_workspace,
    register_profile,
)
from omnivia_core_runtime.service.runner import ServiceRunner
from omnivia_core_runtime.service.versions import SERVER_VERSION
from omnivia_core_runtime.service.workspace_init import initialise_workspace
from omnivia_core_runtime.storage.migrations import applied_migrations, load_migrations

MIGRATION_FILENAME = "0069_document_intake.sql"
PROFILE = {
    "schema": "omnivia.extraction-profile.v1",
    "adapter": "plain-text",
    "adapterVersion": "1",
    "decoderBuild": "plain-text-test-1",
    "processingMode": "text_only",
    "ocr": {"engine": "none", "engineRevision": "none-1", "modelHashes": [], "languages": []},
    "embeddedImageOcr": False,
    "revisionView": "current_text_with_separate_annotations",
    "secondaryContentPolicy": "preserve_role_and_visibility",
    "formulaPolicy": "expression_and_saved_cache_without_calculation",
    "html": {"encoding": "utf-8"},
    "delimited": {"encoding": "utf-8", "delimiter": ",", "quote": '"'},
    "limits": {
        "sourceBytes": 16 * 1024 * 1024,
        "decodedPixels": 100_000_000,
        "containerEntries": 10_000,
        "expandedBytes": 256 * 1024 * 1024,
        "outputTextBytes": 32 * 1024 * 1024,
        "outputStructuredBytes": 128 * 1024 * 1024,
        "memoryBytes": 512 * 1024 * 1024,
        "timeMs": 120_000,
        "temporaryBytes": 512 * 1024 * 1024,
    },
    "readingOrderVersion": "source-order-1",
    "deduplicationVersion": "exact-span-1",
    "parserFeatures": ["exact_text"],
    "unknownFeaturePolicy": "partial",
    "completenessPolicy": "explicit_unit_coverage",
}


@pytest.fixture
def runner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[ServiceRunner]:
    process = FakeProcessEvidence(
        ProcessEvidence(
            pid=4344,
            start_time="document-intake-migration-test",
            boot_id="boot-document-intake-migration",
            os_principal="test-user",
        )
    )
    monkeypatch.setattr(runner_module, "SystemProcessEvidence", lambda: process)
    workspace = tmp_path / "workspace"
    installation = tmp_path / "installation"
    initialise_workspace(
        workspace_root=workspace,
        installation_root=installation,
        core_version=SERVER_VERSION,
    )
    with owned_workspace(
        workspace_root=workspace,
        installation_root=installation,
        core_version=SERVER_VERSION,
    ) as owner:
        yield owner


def test_0069_is_the_unique_successor_to_0068() -> None:
    found = [migration for migration in load_migrations() if migration.version == 69]
    assert [migration.name for migration in found] == [MIGRATION_FILENAME]
    # 0070 (app settings) is the head; 0069 is still the only migration at 69.
    assert max(migration.version for migration in load_migrations()) == 70


def test_0069_applies_all_document_intake_tables_and_guards(runner: ServiceRunner) -> None:
    assert runner.connection is not None
    assert max(applied_migrations(runner.connection)) == 70
    expected_tables = {
        "omnivia_documents",
        "omnivia_document_original_bindings",
        "omnivia_document_captures",
        "omnivia_document_capture_retention",
        "omnivia_document_capture_outbox",
        "omnivia_extraction_profiles",
        "omnivia_extraction_revisions",
        "omnivia_extraction_results",
        "omnivia_extracted_segments",
        "omnivia_document_extraction_projection",
        "omnivia_document_heads",
    }
    tables = {
        str(row[0])
        for row in runner.connection.execute(
            "SELECT name FROM sqlite_schema WHERE type = 'table' AND name LIKE 'omnivia_%'"
        )
    }
    assert expected_tables <= tables
    triggers = {
        str(row[0])
        for row in runner.connection.execute(
            "SELECT name FROM sqlite_schema WHERE type = 'trigger' AND name LIKE 'omnivia_guard_%'"
        )
    }
    for table in expected_tables:
        assert f"omnivia_guard_{table.removeprefix('omnivia_')}_insert" in triggers
        assert f"omnivia_guard_{table.removeprefix('omnivia_')}_delete" in triggers


def test_0069_refuses_unguarded_and_immutable_writes(runner: ServiceRunner) -> None:
    assert runner.connection is not None
    assert runner.workspace_id is not None
    assert runner.generation is not None

    with pytest.raises(sqlite3.DatabaseError, match="not authorized|unguarded"):
        runner.connection.execute(
            "INSERT INTO omnivia_documents "
            "(workspace_id, document_id, fencing_generation, created_at_us) VALUES (?, ?, ?, ?)",
            (runner.workspace_id, "doc-unguarded", runner.generation, 1),
        )

    profile = register_profile(runner, PROFILE)
    with service._fence(runner), pytest.raises(sqlite3.IntegrityError, match="append-only"):
        runner.connection.execute(
            "UPDATE omnivia_extraction_profiles SET profile_json = '{}' "
            "WHERE workspace_id = ? AND profile_digest = ?",
            (runner.workspace_id, profile),
        )
    with service._fence(runner), pytest.raises(sqlite3.IntegrityError, match="append-only"):
        runner.connection.execute(
            "DELETE FROM omnivia_extraction_profiles "
            "WHERE workspace_id = ? AND profile_digest = ?",
            (runner.workspace_id, profile),
        )


def test_0069_identifier_checks_reject_invalid_rows(runner: ServiceRunner) -> None:
    assert runner.connection is not None
    assert runner.workspace_id is not None
    assert runner.generation is not None
    with service._fence(runner), pytest.raises(sqlite3.IntegrityError, match="CHECK constraint"):
        runner.connection.execute(
            "INSERT INTO omnivia_documents "
            "(workspace_id, document_id, fencing_generation, created_at_us) VALUES (?, ?, ?, ?)",
            (runner.workspace_id, "_invalid", runner.generation, 1),
        )


def test_0069_binding_and_outbox_rows_only_move_forward(runner: ServiceRunner) -> None:
    assert runner.connection is not None
    binding = service.connect_original(
        runner, document_id="doc-guarded", owner_ref="owner-a", route_kind="direct_file"
    )
    source = runner.layout.root / "guarded-source.txt"
    source.write_bytes(b"guarded")
    capture = service.capture_document(
        runner,
        source_path=source,
        document_id="doc-guarded",
        expected_base_capture_id=None,
        operation_key="cap-guarded",
        media_type="text/plain",
        intent_kind="new_capture",
        binding=service.BindingClaim("owner-a", binding.binding_generation, "direct_file"),
    )

    with service._fence(runner), pytest.raises(sqlite3.IntegrityError, match="disconnects at its generation"):
        runner.connection.execute(
            "UPDATE omnivia_document_original_bindings "
            "SET binding_generation = binding_generation + 1, connection_state = 'disconnected'"
        )
    with service._fence(runner), pytest.raises(sqlite3.IntegrityError, match="only settles"):
        runner.connection.execute(
            "UPDATE omnivia_document_capture_outbox SET outbox_sequence = outbox_sequence + 1, "
            "outbox_state = 'settled', settled_at_us = 1 WHERE capture_id = ?",
            (capture.capture.capture_id,),
        )
    with service._fence(runner), pytest.raises(sqlite3.IntegrityError, match="never deleted"):
        runner.connection.execute(
            "DELETE FROM omnivia_document_capture_outbox WHERE capture_id = ?",
            (capture.capture.capture_id,),
        )
