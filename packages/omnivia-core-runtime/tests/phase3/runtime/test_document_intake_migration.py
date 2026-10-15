"""Migration-level qualification for the document-intake persistence substrate."""

from __future__ import annotations

import json
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
from omnivia_core_runtime.service.document_processing import (
    portable_profile,
    resolve_settings,
)
from omnivia_core_runtime.service.processing_settings import ProcessingSettings
from omnivia_core_runtime.service.runner import ServiceRunner
from omnivia_core_runtime.service.versions import SERVER_VERSION
from omnivia_core_runtime.service.workspace_init import initialise_workspace
from omnivia_core_runtime.storage import document_intake as store
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
    # 0072 (capture processing resources) is the head; 0069 is still the only
    # migration at 69.
    assert max(migration.version for migration in load_migrations()) == 72


def test_0069_applies_all_document_intake_tables_and_guards(runner: ServiceRunner) -> None:
    assert runner.connection is not None
    assert max(applied_migrations(runner.connection)) == 72
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


# --- 0071: the preference snapshot one capture was admitted under ---------------

#: The resource snapshot a capture that resolves no recognition records. Spelled
#: out rather than built, so a test driving one column's CHECK constraint hands the
#: other a value that is certainly valid.
_RESOURCES_NONE = (
    '{"detail":null,"kind":"none","reason":null,'
    '"schema":"omnivia.capture-ocr-resources.v1",'
    '"selection":[],"unavailableModels":[]}'
)


#: A well-formed profile digest, for the tests that are about one of the other two
#: columns. Spelled out for the same reason `_RESOURCES_NONE` is.
_DIGEST = "sha256:" + "0" * 64

#: A settings document 0071's column accepts, for the tests that are about 0072's
#: two columns and need its one to be certainly valid.
_SETTINGS = '{"embeddedImageOcr":false,"languages":["eng"],"mode":"text_only"}'


def _admitting(
    monkeypatch: pytest.MonkeyPatch,
    settings: str | None,
    resources: str | None,
    digest: str | None = _DIGEST,
) -> None:
    """Admit captures with these three frozen facts, whatever the live state.

    The seam is the one function that reads the preferences, resolves the
    resources and digests the profile they name, so a test can drive any of the
    three columns' own constraints -- or the pre-migration NULL -- through the
    production write that fills them.
    """
    monkeypatch.setattr(
        service,
        "_admitted_processing",
        lambda _runner, **_kwargs: (settings, resources, digest),
    )


def _capture(
    runner: ServiceRunner, *, document_id: str, operation_key: str, content: bytes
) -> service.DocumentCaptureResult:
    """One admitted capture of `content`, with its own binding and source file."""
    assert runner.connection is not None
    binding = service.connect_original(
        runner, document_id=document_id, owner_ref="owner-0071", route_kind="direct_file"
    )
    source = runner.layout.root / f"{operation_key}.txt"
    source.write_bytes(content)
    return service.capture_document(
        runner,
        source_path=source,
        document_id=document_id,
        expected_base_capture_id=None,
        operation_key=operation_key,
        media_type="text/plain",
        intent_kind="new_capture",
        binding=service.BindingClaim(
            "owner-0071", binding.binding_generation, "direct_file"
        ),
    )


def test_0071_adds_the_snapshot_column_to_the_capture_table(runner: ServiceRunner) -> None:
    assert runner.connection is not None
    columns = {
        str(row[1])
        for row in runner.connection.execute(
            "PRAGMA table_info(omnivia_document_captures)"
        )
    }
    assert "processing_settings_json" in columns


def test_0071_freezes_the_admitted_preferences_on_an_append_only_row(
    runner: ServiceRunner,
) -> None:
    """A new capture always carries a snapshot, and the row it sits on never changes.

    The immutability is 0069's, not this migration's: `omnivia_document_captures`
    refuses every UPDATE unconditionally, so a value written at INSERT is frozen by
    construction and the column needs no guard of its own. Asserted here because that
    is the property the whole freeze-at-admission design rests on.
    """
    captured = _capture(
        runner, document_id="doc-0071-frozen", operation_key="cap-0071-frozen",
        content=b"frozen body",
    )
    stored = captured.capture.processing_settings_json
    assert stored is not None
    assert json.loads(stored) == {
        "embeddedImageOcr": False,
        "languages": ["eng"],
        "mode": "text_and_local_ocr",
    }
    # The canonical form, not merely an equal document: the snapshot is compared and
    # digested as text downstream, so its byte form is part of what is frozen.
    assert stored == (
        '{"embeddedImageOcr":false,"languages":["eng"],"mode":"text_and_local_ocr"}'
    )
    reread = store.read_capture(
        runner.connection,
        workspace_id=runner.workspace_id or "",
        capture_id=captured.capture.capture_id,
    )
    assert reread is not None and reread.processing_settings_json == stored

    with service._fence(runner), pytest.raises(sqlite3.IntegrityError, match="append-only"):
        runner.connection.execute(
            "UPDATE omnivia_document_captures SET processing_settings_json = ? "
            "WHERE workspace_id = ? AND capture_id = ?",
            ('{"mode":"text_only"}', runner.workspace_id, captured.capture.capture_id),
        )


def test_0071_keeps_a_capture_that_recorded_no_selection(
    runner: ServiceRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """NULL is the pre-migration capture, and it is stored rather than back-filled.

    Inventing a selection for a capture admitted before this column existed would be a
    claim about what ran, so the column is nullable and absence stays absence.
    """
    _admitting(monkeypatch, None, None, None)
    captured = _capture(
        runner, document_id="doc-0071-null", operation_key="cap-0071-null",
        content=b"legacy body",
    )
    assert captured.capture.processing_settings_json is None
    reread = store.read_capture(
        runner.connection,
        workspace_id=runner.workspace_id or "",
        capture_id=captured.capture.capture_id,
    )
    assert reread is not None and reread.processing_settings_json is None


@pytest.mark.parametrize(
    "frozen",
    [
        pytest.param('"text_only"', id="not-an-object"),
        pytest.param("[]", id="array"),
        pytest.param("{", id="not-json"),
        pytest.param("{}" + " " * 4096, id="over-the-bound"),
        pytest.param('{"mode":"text\u0000only"}', id="embedded-nul"),
    ],
)
def test_0071_refuses_a_snapshot_that_is_not_a_bounded_json_object(
    runner: ServiceRunner, monkeypatch: pytest.MonkeyPatch, frozen: str
) -> None:
    """The column's own CHECK, exercised through the admission path that writes it.

    The snapshot is read back and parsed by a later worker, so a value that is not a
    bounded JSON object must be refused where it is written rather than discovered when
    something tries to act on it.
    """
    _admitting(monkeypatch, frozen, _RESOURCES_NONE)
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint"):
        _capture(
            runner, document_id="doc-0071-bad", operation_key="cap-0071-bad",
            content=b"bad body",
        )
    assert runner.connection is not None
    assert (
        runner.connection.execute(
            "SELECT COUNT(*) FROM omnivia_document_captures"
        ).fetchone()[0]
        == 0
    )


# --- 0072: the exact resources one capture was admitted against -----------------


def test_0072_adds_the_resource_column_to_the_capture_table(
    runner: ServiceRunner,
) -> None:
    assert runner.connection is not None
    columns = {
        str(row[1])
        for row in runner.connection.execute(
            "PRAGMA table_info(omnivia_document_captures)"
        )
    }
    assert "processing_resources_json" in columns


def test_0072_freezes_the_admitted_resources_on_an_append_only_row(
    runner: ServiceRunner,
) -> None:
    """A new capture always carries a resource snapshot, including when it pins none.

    Plain text resolves no recognition, so the recorded decision is that this
    capture was admitted against nothing -- which is deliberately a *value* and not
    a NULL, because NULL already means "admitted before this column existed" and
    the two must stay distinguishable. The row's immutability is 0069's, exactly as
    it is for 0071's column.
    """
    captured = _capture(
        runner,
        document_id="doc-0072-frozen",
        operation_key="cap-0072-frozen",
        content=b"frozen body",
    )
    stored = captured.capture.processing_resources_json
    assert stored is not None
    assert json.loads(stored) == {
        "detail": None,
        "kind": "none",
        "reason": None,
        "schema": "omnivia.capture-ocr-resources.v1",
        "selection": [],
        "unavailableModels": [],
    }
    # The canonical form, not merely an equal document: the snapshot is compared
    # and digested as text downstream, so its byte form is part of what is frozen.
    assert stored == _RESOURCES_NONE
    assert runner.connection is not None
    reread = store.read_capture(
        runner.connection,
        workspace_id=runner.workspace_id or "",
        capture_id=captured.capture.capture_id,
    )
    assert reread is not None and reread.processing_resources_json == stored

    with service._fence(runner), pytest.raises(
        sqlite3.IntegrityError, match="append-only"
    ):
        runner.connection.execute(
            "UPDATE omnivia_document_captures SET processing_resources_json = ? "
            "WHERE workspace_id = ? AND capture_id = ?",
            (_RESOURCES_NONE, runner.workspace_id, captured.capture.capture_id),
        )


def test_0072_keeps_a_capture_that_recorded_no_resources(
    runner: ServiceRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """NULL is the pre-migration capture, stored rather than back-filled.

    Inventing resource identities for a capture admitted before this column
    existed would be a claim about which bytes were read, which is the one claim
    this design must never make up. The column is nullable and absence stays
    absence; what such a capture then executes is the packaged fail-closed
    catalogue's answer, which pins no model.
    """
    _admitting(monkeypatch, None, None, None)
    captured = _capture(
        runner,
        document_id="doc-0072-null",
        operation_key="cap-0072-null",
        content=b"legacy body",
    )
    assert captured.capture.processing_resources_json is None
    # Both columns together: a stored digest would be a digest of a decision this
    # row does not carry, so the pre-migration capture has neither.
    assert captured.capture.processing_profile_digest is None
    assert runner.connection is not None
    reread = store.read_capture(
        runner.connection,
        workspace_id=runner.workspace_id or "",
        capture_id=captured.capture.capture_id,
    )
    assert reread is not None and reread.processing_resources_json is None
    assert reread.processing_profile_digest is None


@pytest.mark.parametrize(
    "frozen",
    [
        pytest.param('"frozen"', id="not-an-object"),
        pytest.param("[]", id="array"),
        pytest.param("{", id="not-json"),
        pytest.param("{}" + " " * 4096, id="over-the-bound"),
        pytest.param('{"kind":"no\u0000ne"}', id="embedded-nul"),
    ],
)
def test_0072_refuses_a_snapshot_that_is_not_a_bounded_json_object(
    runner: ServiceRunner, monkeypatch: pytest.MonkeyPatch, frozen: str
) -> None:
    """The column's own CHECK, exercised through the admission path that writes it.

    The snapshot is read back and strictly decoded by a later worker, and a value
    the column would accept but the decoder would not is a capture that can never
    execute. Refusing it where it is written is what keeps the two in step.
    """
    # A well-formed digest beside it: the two columns are paired, so a malformed
    # snapshot with no digest would be refused by the pairing before this test
    # reached the shape constraint it is about.
    _admitting(monkeypatch, _SETTINGS, frozen)
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint"):
        _capture(
            runner,
            document_id="doc-0072-bad",
            operation_key="cap-0072-bad",
            content=b"bad body",
        )
    assert runner.connection is not None
    assert (
        runner.connection.execute(
            "SELECT COUNT(*) FROM omnivia_document_captures"
        ).fetchone()[0]
        == 0
    )


def test_0072_freezes_the_admitted_profile_digest_beside_them(
    runner: ServiceRunner,
) -> None:
    """Every new capture carries the digest its own admission resolved.

    Plain text under the default preferences recognises nothing, and the digest is
    stored all the same: a text-only profile has an identity too, and it moves
    under a changed adapter or decoder build exactly as a recognising one does.
    The value is the real ExtractionProfileV1 digest, not a token -- it is compared
    with one the worker computes -- and the row it sits on is 0069's append-only
    row.
    """
    captured = _capture(
        runner,
        document_id="doc-0072-digest",
        operation_key="cap-0072-digest",
        content=b"frozen body",
    )
    stored = captured.capture.processing_profile_digest
    assert stored is not None
    assert stored == store.profile_digest(
        portable_profile(
            media_type=captured.capture.media_type,
            profile_ref=captured.capture.processing_profile_ref,
            revision=captured.capture.processing_profile_revision,
            settings=resolve_settings(
                media_type=captured.capture.media_type,
                profile_ref=captured.capture.processing_profile_ref,
                settings=ProcessingSettings.from_value(
                    json.loads(captured.capture.processing_settings_json or "")
                ),
            ),
        )
    )
    assert runner.connection is not None
    reread = store.read_capture(
        runner.connection,
        workspace_id=runner.workspace_id or "",
        capture_id=captured.capture.capture_id,
    )
    assert reread is not None and reread.processing_profile_digest == stored

    with service._fence(runner), pytest.raises(
        sqlite3.IntegrityError, match="append-only"
    ):
        runner.connection.execute(
            "UPDATE omnivia_document_captures SET processing_profile_digest = ? "
            "WHERE workspace_id = ? AND capture_id = ?",
            (stored, runner.workspace_id, captured.capture.capture_id),
        )


@pytest.mark.parametrize(
    "digest",
    [
        pytest.param("sha256:" + "0" * 63, id="too-short"),
        pytest.param("sha256:" + "0" * 65, id="too-long"),
        pytest.param("sha512:" + "0" * 64, id="another-algorithm"),
        pytest.param("sha256:" + "0" * 63 + "A", id="upper-case-hex"),
        pytest.param("sha256:" + "0" * 63 + "g", id="not-hex"),
    ],
)
def test_0072_refuses_a_digest_that_is_not_a_profile_digest(
    runner: ServiceRunner, monkeypatch: pytest.MonkeyPatch, digest: str
) -> None:
    """The same shape every other profile digest 0069 stores, enforced here too.

    The stored value is compared with a digest the worker computes over a
    canonical profile, so a column that accepted some other shape would hold a
    value that could only ever compare unequal -- a capture that can never
    execute, discovered at execution instead of where it was written.
    """
    _admitting(monkeypatch, _SETTINGS, _RESOURCES_NONE, digest)
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint"):
        _capture(
            runner,
            document_id="doc-0072-digest-bad",
            operation_key="cap-0072-digest-bad",
            content=b"bad body",
        )
    assert runner.connection is not None
    assert (
        runner.connection.execute(
            "SELECT COUNT(*) FROM omnivia_document_captures"
        ).fetchone()[0]
        == 0
    )


@pytest.mark.parametrize(
    ("resources", "digest"),
    [
        pytest.param(_RESOURCES_NONE, None, id="resources-without-a-digest"),
        pytest.param(None, _DIGEST, id="digest-without-resources"),
    ],
)
def test_0072_refuses_one_of_the_two_columns_without_the_other(
    runner: ServiceRunner,
    monkeypatch: pytest.MonkeyPatch,
    resources: str | None,
    digest: str | None,
) -> None:
    """Both or neither, enforced where a raw write is checked.

    Each column's own CHECK accepts NULL, so the two separately accept a row that
    froze resources and no digest, or a digest and no resources. The admission path
    writes both from one resolution in one statement and so cannot produce either,
    but a repair script, a hand-written INSERT or a restored backup can -- and the
    first of the two is not merely untidy. Execution reads a NULL digest as
    "admitted before 0072" and skips the immutable profile comparison entirely, so
    a row carrying frozen resources and no digest would execute them under whatever
    profile the current build resolves: the substitution END-02 exists to close,
    reintroduced through a shape the database permitted.
    """
    _admitting(monkeypatch, _SETTINGS, resources, digest)
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint"):
        _capture(
            runner,
            document_id="doc-0072-unpaired",
            operation_key="cap-0072-unpaired",
            content=b"unpaired body",
        )
    assert runner.connection is not None
    assert (
        runner.connection.execute(
            "SELECT COUNT(*) FROM omnivia_document_captures"
        ).fetchone()[0]
        == 0
    )


@pytest.mark.parametrize(
    ("resources", "digest"),
    [
        pytest.param(_RESOURCES_NONE, _DIGEST, id="both-recorded"),
        pytest.param(None, None, id="neither-recorded"),
    ],
)
def test_0072_keeps_a_capture_whose_two_columns_agree(
    runner: ServiceRunner,
    monkeypatch: pytest.MonkeyPatch,
    resources: str | None,
    digest: str | None,
) -> None:
    """The pairing refuses the halves and nothing else.

    The two states END-02 depends on are the post-0072 capture, which records a
    decision and the digest of the profile it resolved to, and the pre-0072 one,
    which records neither. Both still go in through the admission path that writes
    them.
    """
    _admitting(monkeypatch, _SETTINGS, resources, digest)
    captured = _capture(
        runner,
        document_id="doc-0072-paired",
        operation_key="cap-0072-paired",
        content=b"paired body",
    )
    assert captured.capture.processing_resources_json == resources
    assert captured.capture.processing_profile_digest == digest
    assert runner.connection is not None
    reread = store.read_capture(
        runner.connection,
        workspace_id=runner.workspace_id or "",
        capture_id=captured.capture.capture_id,
    )
    assert reread is not None
    assert reread.processing_resources_json == resources
    assert reread.processing_profile_digest == digest
