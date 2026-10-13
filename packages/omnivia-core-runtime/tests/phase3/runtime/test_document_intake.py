"""Document intake: capture atomicity, idempotency, stale bases, separate heads, profiles, retained-copy
reprocessing, completion order, removal and retention fencing, and failure that keeps the previous head.

Every test drives `service/document_intake.py` against a real workspace owned by a `ServiceRunner`, so the
fence, the guard triggers and the blob store are the ones the service meets. A row is altered only in-process,
by widening the owned connection for one block with `authorised`, the way a tamperer with file access would.
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from omnivia_core_runtime.ownership.identity import FakeProcessEvidence, ProcessEvidence
from omnivia_core_runtime.service import document_intake as service
from omnivia_core_runtime.service import runner as runner_module
from omnivia_core_runtime.service.document_intake import (
    DocumentIntakeRefused,
    ExtractionOutcome,
    admit_extraction,
    capture_document,
    complete_extraction,
    expire_capture,
    owned_workspace,
    read_retained_source,
    register_profile,
    reprocess_captured_copy,
    set_document_source_state,
)
from omnivia_core_runtime.service.runner import ServiceRunner
from omnivia_core_runtime.service.versions import SERVER_VERSION
from omnivia_core_runtime.service.workspace_init import initialise_workspace
from omnivia_core_runtime.storage import document_intake as store
from omnivia_core_runtime.storage.connection import authorised
from omnivia_core_runtime.workspace.blob_publication import blob_path

DOC = "doc-0001"
PROFILE: dict[str, Any] = {
    "schema": "omnivia.extraction-profile.v1",
    "processingMode": "text_only",
    "adapter": "plain-text",
    "adapterVersion": "1",
}
MEDIA = "text/plain"


def digest_of(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


@pytest.fixture
def runner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[ServiceRunner]:
    process = FakeProcessEvidence(
        ProcessEvidence(
            pid=4343,
            start_time="document-intake-test",
            boot_id="boot-document-intake",
            os_principal="test-user",
        )
    )
    monkeypatch.setattr(runner_module, "SystemProcessEvidence", lambda: process)
    workspace = tmp_path / "workspace"
    installation = tmp_path / "installation"
    initialise_workspace(
        workspace_root=workspace, installation_root=installation, core_version=SERVER_VERSION
    )
    with owned_workspace(
        workspace_root=workspace, installation_root=installation, core_version=SERVER_VERSION
    ) as owner:
        yield owner


def count(runner: ServiceRunner, table: str) -> int:
    assert runner.connection is not None
    return int(runner.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def head(runner: ServiceRunner, document_id: str = DOC) -> store.DocumentHead:
    assert runner.connection is not None and runner.workspace_id is not None
    found = store.read_head(runner.connection, workspace_id=runner.workspace_id, document_id=document_id)
    assert found is not None
    return found


def snapshot(runner: ServiceRunner) -> tuple[int, ...]:
    return tuple(
        count(runner, table)
        for table in (
            "omnivia_documents",
            "omnivia_document_captures",
            "omnivia_document_capture_retention",
            "omnivia_evidence_artifacts",
            "omnivia_extraction_revisions",
            "omnivia_extraction_results",
        )
    )


class Sources:
    """Writes local files and captures them, so a test names content and a base and nothing else."""

    def __init__(self, runner: ServiceRunner, root: Path) -> None:
        self.runner = runner
        self.root = root
        root.mkdir(exist_ok=True)

    def path(self, name: str, content: bytes) -> Path:
        path = self.root / name
        path.write_bytes(content)
        return path

    def capture(
        self,
        key: str,
        content: bytes,
        base: str | None,
        *,
        document_id: str = DOC,
        name: str | None = None,
        media_type: str = MEDIA,
    ) -> service.DocumentCaptureResult:
        return capture_document(
            self.runner,
            source_path=self.path(name or f"{key}.txt", content),
            document_id=document_id,
            expected_base_capture_id=base,
            operation_key=key,
            media_type=media_type,
        )


@pytest.fixture
def sources(runner: ServiceRunner, tmp_path: Path) -> Sources:
    return Sources(runner, tmp_path / "originals")


def succeeded(label: str = "out") -> ExtractionOutcome:
    return ExtractionOutcome(
        state="succeeded",
        output_digest=digest_of(label.encode()),
        coverage={"pages": 1},
        limitations={},
        worker_build="test-1",
    )


def admit(
    runner: ServiceRunner, capture_id: str, key: str, base: str | None, digest: str | None = None
) -> store.StoredRevision:
    revision, replayed = admit_extraction(
        runner,
        capture_id=capture_id,
        profile_digest=digest or register_profile(runner, PROFILE),
        expected_base_extraction_id=base,
        operation_key=key,
    )
    assert replayed is False
    return revision


def refusal(action: Callable[[], object]) -> str:
    with pytest.raises(DocumentIntakeRefused) as caught:
        action()
    return str(caught.value)


@contextmanager
def tampering(runner: ServiceRunner) -> Iterator[sqlite3.Connection]:
    assert runner.connection is not None
    with authorised(runner.connection, mutations=True, ddl=True) as connection:
        yield connection


# -- capture: atomicity ---------------------------------------------------------------------


def test_a_capture_writes_the_document_evidence_retention_and_head_together(
    runner: ServiceRunner, sources: Sources
) -> None:
    result = sources.capture("cap-1", b"first body", None)

    assert result.status == "captured" and result.became_head is True
    capture = result.capture
    assert capture.capture_sequence == 1
    assert capture.disposition == "head_eligible"
    assert capture.source_digest == digest_of(b"first body")
    assert capture.source_length_bytes == len(b"first body")
    assert snapshot(runner) == (1, 1, 1, 1, 0, 0)
    state = head(runner)
    assert (state.current_capture_id, state.capture_head_sequence) == (capture.capture_id, 1)
    assert (state.current_extraction_id, state.extraction_head_ordinal) == (None, 0)
    assert read_retained_source(runner, capture_id=capture.capture_id) == b"first body"


def test_a_failure_after_the_evidence_is_written_leaves_nothing_behind(
    runner: ServiceRunner, sources: Sources, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = sources.capture("cap-1", b"one", None)
    before = snapshot(runner)

    def boom(*_args: object, **_kwargs: object) -> None:
        raise sqlite3.IntegrityError("simulated failure after the evidence insert")

    monkeypatch.setattr(store, "insert_capture", boom)
    with pytest.raises(sqlite3.IntegrityError):
        sources.capture("cap-2", b"two", first.capture.capture_id)
    with pytest.raises(sqlite3.IntegrityError):
        sources.capture("cap-new", b"three", None, document_id="doc-new")

    assert snapshot(runner) == before
    assert head(runner).current_capture_id == first.capture.capture_id
    assert count(runner, "omnivia_document_heads") == 1


def test_a_failed_head_advance_rolls_back_the_capture_it_followed(
    runner: ServiceRunner, sources: Sources, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = sources.capture("cap-1", b"one", None)
    before = snapshot(runner)

    def boom(*_args: object, **_kwargs: object) -> None:
        raise sqlite3.IntegrityError("simulated head failure")

    monkeypatch.setattr(store, "advance_capture_head", boom)
    with pytest.raises(sqlite3.IntegrityError):
        sources.capture("cap-2", b"two", first.capture.capture_id)

    assert snapshot(runner) == before
    assert head(runner).current_capture_id == first.capture.capture_id


def test_a_refresh_that_cannot_read_its_source_keeps_the_previous_head(
    runner: ServiceRunner, sources: Sources, tmp_path: Path
) -> None:
    first = sources.capture("cap-1", b"one", None)
    before = snapshot(runner)

    with pytest.raises(DocumentIntakeRefused):
        capture_document(
            runner,
            source_path=tmp_path / "originals" / "missing.txt",
            document_id=DOC,
            expected_base_capture_id=first.capture.capture_id,
            operation_key="cap-2",
            media_type=MEDIA,
        )

    assert snapshot(runner) == before
    assert head(runner).current_capture_id == first.capture.capture_id


@pytest.mark.parametrize(
    ("document_id", "operation_key", "media_type"),
    [
        pytest.param("bad id", "cap-x", MEDIA, id="document-id"),
        pytest.param(DOC, "bad key", MEDIA, id="operation-key"),
        pytest.param(DOC, "cap-x", "not a media type", id="media-type"),
    ],
)
def test_an_out_of_domain_identifier_or_media_type_is_refused_before_anything_is_written(
    runner: ServiceRunner, sources: Sources, document_id: str, operation_key: str, media_type: str
) -> None:
    with pytest.raises(DocumentIntakeRefused):
        sources.capture(
            operation_key, b"x", None, document_id=document_id, name="x.txt", media_type=media_type
        )
    assert snapshot(runner) == (0, 0, 0, 0, 0, 0)


def test_an_expected_base_from_another_document_is_refused_and_creates_no_document(
    runner: ServiceRunner, sources: Sources
) -> None:
    other = sources.capture("cap-a", b"a", None, document_id="doc-a")
    before = snapshot(runner)

    assert "not a capture of this document" in refusal(
        lambda: sources.capture("cap-b", b"b", other.capture.capture_id, document_id="doc-b")
    )
    assert "not a capture of this document" in refusal(
        lambda: sources.capture("cap-c", b"c", "dcap-" + "0" * 64, document_id="doc-b")
    )
    assert snapshot(runner) == before


# -- capture: idempotency -------------------------------------------------------------------


def test_replaying_a_capture_returns_the_stored_record_and_writes_nothing(
    runner: ServiceRunner, sources: Sources
) -> None:
    first = sources.capture("cap-1", b"one", None)
    before = snapshot(runner)

    replay = sources.capture("cap-1", b"one", None, name="same-bytes-again.txt")

    assert replay.status == "replayed"
    assert replay.capture == first.capture
    assert replay.became_head is True
    assert snapshot(runner) == before


def test_a_replay_after_a_later_capture_reports_that_it_is_no_longer_the_head(
    runner: ServiceRunner, sources: Sources
) -> None:
    first = sources.capture("cap-1", b"one", None)
    second = sources.capture("cap-2", b"two", first.capture.capture_id)
    before = snapshot(runner)

    replay = sources.capture("cap-1", b"one", None)

    assert replay.status == "replayed" and replay.became_head is False
    assert head(runner).current_capture_id == second.capture.capture_id
    assert snapshot(runner) == before


@pytest.mark.parametrize(
    "change",
    [
        pytest.param({"content": b"different"}, id="content"),
        pytest.param({"document_id": "doc-other"}, id="document"),
        pytest.param({"media_type": "text/markdown"}, id="media-type"),
        pytest.param({"base": "dcap-" + "1" * 64}, id="base"),
    ],
)
def test_the_same_operation_key_with_a_different_request_is_a_conflict(
    runner: ServiceRunner, sources: Sources, change: dict[str, Any]
) -> None:
    first = sources.capture("cap-1", b"one", None)
    before = snapshot(runner)

    assert "different capture request" in refusal(
        lambda: sources.capture(
            "cap-1",
            change.get("content", b"one"),
            change.get("base"),
            document_id=change.get("document_id", DOC),
            media_type=change.get("media_type", MEDIA),
        )
    )

    assert snapshot(runner) == before
    assert head(runner).current_capture_id == first.capture.capture_id


def test_two_keys_with_identical_bytes_make_two_captures_with_their_own_evidence(
    runner: ServiceRunner, sources: Sources
) -> None:
    first = sources.capture("cap-1", b"same", None)
    second = sources.capture("cap-2", b"same", first.capture.capture_id)

    assert first.capture.capture_id != second.capture.capture_id
    assert first.capture.evidence_id != second.capture.evidence_id
    assert first.capture.source_digest == second.capture.source_digest
    assert count(runner, "omnivia_blob_objects") == 1
    assert count(runner, "omnivia_evidence_artifacts") == 2


def test_a_capture_key_and_an_extraction_key_are_separate_namespaces(
    runner: ServiceRunner, sources: Sources
) -> None:
    first = sources.capture("shared-key", b"one", None)
    revision = admit(runner, first.capture.capture_id, "shared-key", None)

    assert revision.operation_key == first.capture.operation_key == "shared-key"
    assert revision.extraction_revision_id != first.capture.capture_id


# -- capture: stale bases -------------------------------------------------------------------


def test_a_capture_on_a_stale_base_is_kept_as_history_and_never_becomes_the_head(
    runner: ServiceRunner, sources: Sources
) -> None:
    first = sources.capture("cap-1", b"one", None)
    second = sources.capture("cap-2", b"two", first.capture.capture_id)
    late = sources.capture("cap-3", b"late", first.capture.capture_id)
    nothing = sources.capture("cap-4", b"unseen", None)

    assert late.became_head is False and nothing.became_head is False
    assert late.capture.disposition == nothing.capture.disposition == "historical_only"
    assert [late.capture.capture_sequence, nothing.capture.capture_sequence] == [3, 4]
    state = head(runner)
    assert (state.current_capture_id, state.capture_head_sequence) == (second.capture.capture_id, 2)
    assert read_retained_source(runner, capture_id=late.capture.capture_id) == b"late"

    fresh = sources.capture("cap-5", b"fresh", second.capture.capture_id)
    assert fresh.became_head is True and fresh.capture.capture_sequence == 5
    assert head(runner).current_capture_id == fresh.capture.capture_id


def test_a_historical_capture_can_be_the_base_of_another_historical_capture(
    runner: ServiceRunner, sources: Sources
) -> None:
    first = sources.capture("cap-1", b"one", None)
    sources.capture("cap-2", b"two", first.capture.capture_id)
    late = sources.capture("cap-3", b"late", first.capture.capture_id)
    chained = sources.capture("cap-4", b"chained", late.capture.capture_id)

    assert chained.became_head is False
    assert chained.capture.expected_base_capture_id == late.capture.capture_id


# -- independent heads ----------------------------------------------------------------------


def test_an_extraction_moves_the_extraction_head_and_never_the_capture_head(
    runner: ServiceRunner, sources: Sources
) -> None:
    first = sources.capture("cap-1", b"one", None)
    revision = admit(runner, first.capture.capture_id, "ext-1", None)
    before = head(runner)

    result = complete_extraction(
        runner, extraction_revision_id=revision.extraction_revision_id, outcome=succeeded()
    )

    after = head(runner)
    assert result.publication_disposition == "promoted"
    assert (after.current_extraction_id, after.extraction_head_ordinal) == (
        revision.extraction_revision_id,
        1,
    )
    assert (after.current_capture_id, after.capture_head_sequence) == (
        before.current_capture_id,
        before.capture_head_sequence,
    )


def test_a_new_capture_never_moves_or_clears_the_extraction_head(
    runner: ServiceRunner, sources: Sources
) -> None:
    first = sources.capture("cap-1", b"one", None)
    revision = admit(runner, first.capture.capture_id, "ext-1", None)
    complete_extraction(runner, extraction_revision_id=revision.extraction_revision_id, outcome=succeeded())

    second = sources.capture("cap-2", b"two", first.capture.capture_id)

    state = head(runner)
    assert state.current_capture_id == second.capture.capture_id
    assert (state.current_extraction_id, state.extraction_head_ordinal) == (
        revision.extraction_revision_id,
        1,
    )


def test_an_extraction_over_a_historical_capture_cannot_move_the_capture_head(
    runner: ServiceRunner, sources: Sources
) -> None:
    first = sources.capture("cap-1", b"one", None)
    second = sources.capture("cap-2", b"two", first.capture.capture_id)
    late = sources.capture("cap-3", b"late", first.capture.capture_id)
    revision = admit(runner, late.capture.capture_id, "ext-late", None)

    complete_extraction(runner, extraction_revision_id=revision.extraction_revision_id, outcome=succeeded())

    state = head(runner)
    assert state.current_capture_id == second.capture.capture_id
    assert state.current_extraction_id == revision.extraction_revision_id


def test_the_two_heads_cannot_move_in_one_write(runner: ServiceRunner, sources: Sources) -> None:
    first = sources.capture("cap-1", b"one", None)
    second = sources.capture("cap-2", b"two", first.capture.capture_id)
    revision = admit(runner, second.capture.capture_id, "ext-1", None)
    complete_extraction(runner, extraction_revision_id=revision.extraction_revision_id, outcome=succeeded())
    third = sources.capture("cap-3", b"three", second.capture.capture_id)
    other = admit(runner, third.capture.capture_id, "ext-2", revision.extraction_revision_id)
    complete_extraction(runner, extraction_revision_id=other.extraction_revision_id, outcome=succeeded("b"))
    assert runner.connection is not None

    with (
        pytest.raises(sqlite3.IntegrityError, match="exactly one of the capture head"),
        service._fence(runner),
    ):
        runner.connection.execute(
            "UPDATE omnivia_document_heads SET current_capture_id = ?, capture_head_sequence = 2, "
            "current_extraction_id = ?, extraction_head_ordinal = 1",
            (second.capture.capture_id, revision.extraction_revision_id),
        )

    state = head(runner)
    assert state.current_capture_id == third.capture.capture_id
    assert state.current_extraction_id == other.extraction_revision_id


# -- content-addressed profiles -------------------------------------------------------------


def test_a_profile_is_identified_by_the_digest_of_its_canonical_json(runner: ServiceRunner) -> None:
    reordered = dict(reversed(list(PROFILE.items())))
    digest = register_profile(runner, PROFILE)

    assert register_profile(runner, reordered) == digest == store.profile_digest(PROFILE)
    assert count(runner, "omnivia_extraction_profiles") == 1
    changed = register_profile(runner, {**PROFILE, "adapterVersion": "2"})
    assert changed != digest
    assert count(runner, "omnivia_extraction_profiles") == 2
    assert runner.connection is not None and runner.workspace_id is not None
    assert store.read_profile(runner.connection, workspace_id=runner.workspace_id, digest=digest) == PROFILE


@pytest.mark.parametrize(
    "profile",
    [
        pytest.param({**PROFILE, "schema": "other"}, id="schema"),
        pytest.param({**PROFILE, "processingMode": "cloud_ocr"}, id="mode"),
        pytest.param({**PROFILE, "adapter": ""}, id="adapter"),
        pytest.param({k: v for k, v in PROFILE.items() if k != "adapterVersion"}, id="version"),
        pytest.param({**PROFILE, "blob": "x" * 70_000}, id="oversized"),
        pytest.param({**PROFILE, "bad": float("nan")}, id="not-canonical"),
    ],
)
def test_a_profile_outside_its_closed_shape_is_refused_and_stores_nothing(
    runner: ServiceRunner, profile: dict[str, Any]
) -> None:
    with pytest.raises(DocumentIntakeRefused):
        register_profile(runner, profile)
    assert count(runner, "omnivia_extraction_profiles") == 0


def test_a_profile_row_altered_at_rest_reads_as_invalid(runner: ServiceRunner) -> None:
    digest = register_profile(runner, PROFILE)
    assert runner.connection is not None and runner.workspace_id is not None
    with tampering(runner) as connection:
        connection.execute("DROP TRIGGER omnivia_guard_extraction_profiles_update")
        connection.execute("UPDATE omnivia_extraction_profiles SET profile_json = ?", ('{"a":1}',))

    with pytest.raises(store.DocumentIntakeInvalid):
        store.read_profile(runner.connection, workspace_id=runner.workspace_id, digest=digest)


def test_an_extraction_names_a_registered_profile_by_digest_only(
    runner: ServiceRunner, sources: Sources
) -> None:
    first = sources.capture("cap-1", b"one", None)

    assert "not registered" in refusal(
        lambda: admit_extraction(
            runner,
            capture_id=first.capture.capture_id,
            profile_digest="sha256:" + "a" * 64,
            expected_base_extraction_id=None,
            operation_key="ext-1",
        )
    )
    assert count(runner, "omnivia_extraction_revisions") == 0


def test_two_profiles_over_one_capture_are_two_revisions_with_their_own_frozen_profile(
    runner: ServiceRunner, sources: Sources
) -> None:
    first = sources.capture("cap-1", b"one", None)
    plain = admit(runner, first.capture.capture_id, "ext-1", None)
    other = admit(
        runner,
        first.capture.capture_id,
        "ext-2",
        None,
        register_profile(runner, {**PROFILE, "adapterVersion": "2"}),
    )

    assert plain.profile_digest != other.profile_digest
    assert plain.input_digest == other.input_digest == first.capture.source_digest
    assert (plain.extraction_sequence, other.extraction_sequence) == (1, 2)
    assert (plain.document_ordinal, other.document_ordinal) == (1, 2)


# -- extraction admission replay ------------------------------------------------------------


def test_replaying_an_extraction_key_returns_the_revision_and_a_changed_request_conflicts(
    runner: ServiceRunner, sources: Sources
) -> None:
    first = sources.capture("cap-1", b"one", None)
    digest = register_profile(runner, PROFILE)
    revision = admit(runner, first.capture.capture_id, "ext-1", None, digest)
    before = snapshot(runner)

    replayed, was_replay = admit_extraction(
        runner,
        capture_id=first.capture.capture_id,
        profile_digest=digest,
        expected_base_extraction_id=None,
        operation_key="ext-1",
    )

    assert was_replay is True and replayed == revision
    assert snapshot(runner) == before
    other_digest = register_profile(runner, {**PROFILE, "adapterVersion": "2"})
    assert "different extraction request" in refusal(
        lambda: admit_extraction(
            runner,
            capture_id=first.capture.capture_id,
            profile_digest=other_digest,
            expected_base_extraction_id=None,
            operation_key="ext-1",
        )
    )


# -- retained-copy reprocessing -------------------------------------------------------------


def test_reprocessing_reads_only_the_retained_blob_and_creates_no_capture(
    runner: ServiceRunner,
    sources: Sources,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = sources.path("cap-1.txt", b"retained bytes")
    first = capture_document(
        runner,
        source_path=path,
        document_id=DOC,
        expected_base_capture_id=None,
        operation_key="cap-1",
        media_type=MEDIA,
    )
    path.unlink()
    monkeypatch.setattr(service, "_read_source", lambda *_a, **_k: pytest.fail("the original was reread"))
    seen: list[bytes] = []

    def extractor(content: bytes, profile: Any) -> ExtractionOutcome:
        seen.append(content)
        return succeeded()

    before = (count(runner, "omnivia_document_captures"), count(runner, "omnivia_evidence_artifacts"))
    result = reprocess_captured_copy(
        runner,
        capture_id=first.capture.capture_id,
        profile=PROFILE,
        expected_base_extraction_id=None,
        operation_key="ext-1",
        extractor=extractor,
    )

    assert seen == [b"retained bytes"]
    assert result.publication_disposition == "promoted"
    assert (count(runner, "omnivia_document_captures"), count(runner, "omnivia_evidence_artifacts")) == before
    assert head(runner).current_capture_id == first.capture.capture_id


def test_reprocessing_with_a_new_profile_adds_a_revision_over_the_same_capture(
    runner: ServiceRunner, sources: Sources
) -> None:
    first = sources.capture("cap-1", b"one", None)
    one = reprocess_captured_copy(
        runner,
        capture_id=first.capture.capture_id,
        profile=PROFILE,
        expected_base_extraction_id=None,
        operation_key="ext-1",
        extractor=lambda *_: succeeded("v1"),
    )
    two = reprocess_captured_copy(
        runner,
        capture_id=first.capture.capture_id,
        profile={**PROFILE, "adapterVersion": "2"},
        expected_base_extraction_id=one.extraction_revision_id,
        operation_key="ext-2",
        extractor=lambda *_: succeeded("v2"),
    )

    assert two.publication_disposition == "promoted"
    assert head(runner).current_extraction_id == two.extraction_revision_id
    assert count(runner, "omnivia_document_captures") == 1


def test_a_replayed_reprocess_returns_the_stored_result_without_running_the_extractor(
    runner: ServiceRunner, sources: Sources
) -> None:
    first = sources.capture("cap-1", b"one", None)
    calls: list[int] = []

    def extractor(content: bytes, profile: Any) -> ExtractionOutcome:
        calls.append(1)
        return succeeded()

    arguments: dict[str, Any] = {
        "capture_id": first.capture.capture_id,
        "profile": PROFILE,
        "expected_base_extraction_id": None,
        "operation_key": "ext-1",
        "extractor": extractor,
    }
    stored = reprocess_captured_copy(runner, **arguments)
    again = reprocess_captured_copy(runner, **arguments)

    assert again == stored and calls == [1]
    assert count(runner, "omnivia_extraction_revisions") == 1


def test_a_corrupt_missing_or_expired_retained_copy_is_refused_before_anything_is_admitted(
    runner: ServiceRunner, sources: Sources
) -> None:
    assert runner.layout is not None
    corrupt = sources.capture("cap-1", b"corrupt me", None)
    blob = blob_path(runner.layout.blobs_path, corrupt.capture.source_digest)
    blob.chmod(0o600)
    blob.write_bytes(b"corrupt you")
    missing = sources.capture("cap-2", b"remove me", corrupt.capture.capture_id)
    blob_path(runner.layout.blobs_path, missing.capture.source_digest).unlink()
    expired = sources.capture("cap-3", b"expire me", missing.capture.capture_id)
    expire_capture(runner, capture_id=expired.capture.capture_id)
    before = snapshot(runner)

    for capture in (corrupt, missing, expired):
        assert refusal(
            lambda c=capture: reprocess_captured_copy(  # type: ignore[misc]
                runner,
                capture_id=c.capture.capture_id,
                profile=PROFILE,
                expected_base_extraction_id=None,
                operation_key="ext-" + c.capture.operation_key,
                extractor=lambda *_: pytest.fail("the extractor ran"),
            )
        )
    assert count(runner, "omnivia_extraction_revisions") == 0
    assert snapshot(runner)[:4] == before[:4]


def test_a_deleted_capture_is_not_retained_and_cannot_be_read_or_admitted(
    runner: ServiceRunner, sources: Sources
) -> None:
    first = sources.capture("cap-1", b"one", None)
    assert expire_capture(runner, capture_id=first.capture.capture_id, deleted=True) == 2

    assert "no longer retained" in refusal(lambda: read_retained_source(runner, capture_id=first.capture.capture_id))
    assert "no longer retained" in refusal(
        lambda: admit_extraction(
            runner,
            capture_id=first.capture.capture_id,
            profile_digest=register_profile(runner, PROFILE),
            expected_base_extraction_id=None,
            operation_key="ext-1",
        )
    )


# -- reversed completion --------------------------------------------------------------------


def test_a_revision_that_completes_after_a_newer_one_is_superseded_and_keeps_the_newer_head(
    runner: ServiceRunner, sources: Sources
) -> None:
    first = sources.capture("cap-1", b"one", None)
    older = admit(runner, first.capture.capture_id, "ext-1", None)
    newer = admit(runner, first.capture.capture_id, "ext-2", None)
    assert (older.head_eligible, newer.head_eligible) == (1, 1)
    assert (older.document_ordinal, newer.document_ordinal) == (1, 2)

    promoted = complete_extraction(
        runner, extraction_revision_id=newer.extraction_revision_id, outcome=succeeded("newer")
    )
    late = complete_extraction(
        runner, extraction_revision_id=older.extraction_revision_id, outcome=succeeded("older")
    )

    assert promoted.publication_disposition == "promoted"
    assert late.publication_disposition == "superseded"
    state = head(runner)
    assert (state.current_extraction_id, state.extraction_head_ordinal) == (newer.extraction_revision_id, 2)


def test_in_order_completion_promotes_each_revision_in_turn(
    runner: ServiceRunner, sources: Sources
) -> None:
    first = sources.capture("cap-1", b"one", None)
    older = admit(runner, first.capture.capture_id, "ext-1", None)
    newer = admit(runner, first.capture.capture_id, "ext-2", None)

    one = complete_extraction(runner, extraction_revision_id=older.extraction_revision_id, outcome=succeeded())
    two = complete_extraction(runner, extraction_revision_id=newer.extraction_revision_id, outcome=succeeded())

    assert (one.publication_disposition, two.publication_disposition) == ("promoted", "promoted")
    assert head(runner).current_extraction_id == newer.extraction_revision_id


def test_a_revision_admitted_on_a_stale_extraction_base_is_historical_only(
    runner: ServiceRunner, sources: Sources
) -> None:
    first = sources.capture("cap-1", b"one", None)
    base = admit(runner, first.capture.capture_id, "ext-1", None)
    complete_extraction(runner, extraction_revision_id=base.extraction_revision_id, outcome=succeeded())
    stale = admit(runner, first.capture.capture_id, "ext-2", None)
    assert stale.head_eligible == 0

    result = complete_extraction(
        runner, extraction_revision_id=stale.extraction_revision_id, outcome=succeeded("stale")
    )

    assert result.publication_disposition == "historical_only"
    assert head(runner).current_extraction_id == base.extraction_revision_id


def test_a_revision_names_the_digest_and_frozen_generations_of_its_capture(
    runner: ServiceRunner, sources: Sources
) -> None:
    first = sources.capture("cap-1", b"one", None)
    revision = admit(runner, first.capture.capture_id, "ext-1", None)

    assert revision.input_digest == first.capture.source_digest
    assert (revision.source_removal_generation, revision.capture_retention_generation) == (1, 1)
    assert revision.document_id == DOC


# -- removal and retention fencing ----------------------------------------------------------


def test_a_removal_between_admission_and_completion_fences_the_result_and_keeps_the_head(
    runner: ServiceRunner, sources: Sources
) -> None:
    first = sources.capture("cap-1", b"one", None)
    kept = admit(runner, first.capture.capture_id, "ext-1", None)
    complete_extraction(runner, extraction_revision_id=kept.extraction_revision_id, outcome=succeeded())
    pending = admit(runner, first.capture.capture_id, "ext-2", kept.extraction_revision_id)

    assert set_document_source_state(runner, document_id=DOC, removed=True) == 2
    result = complete_extraction(
        runner, extraction_revision_id=pending.extraction_revision_id, outcome=succeeded("late")
    )

    assert result.publication_disposition == "fenced_removal"
    assert head(runner).current_extraction_id == kept.extraction_revision_id


def test_a_restored_source_does_not_unfence_a_revision_admitted_before_the_removal(
    runner: ServiceRunner, sources: Sources
) -> None:
    first = sources.capture("cap-1", b"one", None)
    pending = admit(runner, first.capture.capture_id, "ext-1", None)

    set_document_source_state(runner, document_id=DOC, removed=True)
    assert set_document_source_state(runner, document_id=DOC, removed=False) == 3
    result = complete_extraction(
        runner, extraction_revision_id=pending.extraction_revision_id, outcome=succeeded()
    )

    assert result.publication_disposition == "fenced_removal"
    assert head(runner).current_extraction_id is None


def test_a_removed_source_refuses_new_captures_and_extractions_and_keeps_its_heads(
    runner: ServiceRunner, sources: Sources
) -> None:
    first = sources.capture("cap-1", b"one", None)
    set_document_source_state(runner, document_id=DOC, removed=True)
    before = snapshot(runner)

    assert "source is removed" in refusal(lambda: sources.capture("cap-2", b"two", first.capture.capture_id))
    assert "source is removed" in refusal(
        lambda: admit_extraction(
            runner,
            capture_id=first.capture.capture_id,
            profile_digest=register_profile(runner, PROFILE),
            expected_base_extraction_id=None,
            operation_key="ext-1",
        )
    )

    assert snapshot(runner) == before
    state = head(runner)
    assert state.source_state == "removed" and state.current_capture_id == first.capture.capture_id


def test_restoring_a_source_reopens_it_for_captures_at_the_new_generation(
    runner: ServiceRunner, sources: Sources
) -> None:
    first = sources.capture("cap-1", b"one", None)
    set_document_source_state(runner, document_id=DOC, removed=True)
    set_document_source_state(runner, document_id=DOC, removed=False)

    second = sources.capture("cap-2", b"two", first.capture.capture_id)

    assert second.became_head is True and second.capture.source_removal_generation == 3


def test_a_source_state_change_must_name_the_other_state_of_a_known_document(
    runner: ServiceRunner, sources: Sources
) -> None:
    sources.capture("cap-1", b"one", None)

    assert "other state" in refusal(lambda: set_document_source_state(runner, document_id=DOC, removed=False))
    assert "no such document" in refusal(
        lambda: set_document_source_state(runner, document_id="doc-unknown", removed=True)
    )
    assert head(runner).source_removal_generation == 1


def test_expiring_a_capture_between_admission_and_completion_fences_the_result(
    runner: ServiceRunner, sources: Sources
) -> None:
    first = sources.capture("cap-1", b"one", None)
    pending = admit(runner, first.capture.capture_id, "ext-1", None)

    assert expire_capture(runner, capture_id=first.capture.capture_id) == 2
    result = complete_extraction(
        runner, extraction_revision_id=pending.extraction_revision_id, outcome=succeeded()
    )

    assert result.publication_disposition == "fenced_retention"
    state = head(runner)
    assert state.current_extraction_id is None
    assert state.current_capture_id == first.capture.capture_id


def test_retention_only_moves_forward_and_a_removed_capture_is_never_made_head(
    runner: ServiceRunner, sources: Sources
) -> None:
    first = sources.capture("cap-1", b"one", None)
    expire_capture(runner, capture_id=first.capture.capture_id)
    assert expire_capture(runner, capture_id=first.capture.capture_id, deleted=True) == 3
    assert runner.connection is not None

    with (
        pytest.raises(sqlite3.IntegrityError, match="only move forward"),
        service._fence(runner),
    ):
        runner.connection.execute(
            "UPDATE omnivia_document_capture_retention SET retention_state = 'retained', "
            "capture_retention_generation = 4"
        )
    with (
        pytest.raises(sqlite3.IntegrityError, match="only move forward"),
        service._fence(runner),
    ):
        runner.connection.execute(
            "UPDATE omnivia_document_capture_retention SET retention_state = 'expired', "
            "capture_retention_generation = 4"
        )
    assert "only moves to expired or deleted" in refusal(
        lambda: store_set_retention(runner, first.capture.capture_id, "retained")
    )


def store_set_retention(runner: ServiceRunner, capture_id: str, state: str) -> None:
    assert runner.connection is not None and runner.workspace_id is not None and runner.generation is not None
    with service._fence(runner):
        try:
            store.set_capture_retention(
                runner.connection,
                workspace_id=runner.workspace_id,
                capture_id=capture_id,
                state=state,
                fencing_generation=runner.generation,
                now_us=1_800_000_000_000_000,
            )
        except store.DocumentIntakeInvalid as error:
            raise DocumentIntakeRefused(str(error)) from error


# -- failure keeps the previous head --------------------------------------------------------


@pytest.mark.parametrize("state", ["unsupported", "failed"])
def test_an_unsupported_or_failed_result_is_recorded_and_keeps_the_previous_extraction_head(
    runner: ServiceRunner, sources: Sources, state: str
) -> None:
    first = sources.capture("cap-1", b"one", None)
    good = admit(runner, first.capture.capture_id, "ext-1", None)
    complete_extraction(runner, extraction_revision_id=good.extraction_revision_id, outcome=succeeded())
    bad = admit(runner, first.capture.capture_id, "ext-2", good.extraction_revision_id)

    result = complete_extraction(
        runner,
        extraction_revision_id=bad.extraction_revision_id,
        outcome=ExtractionOutcome(state=state, limitations={"reason": "no adapter"}),
    )

    assert result.publication_disposition == "not_published" and result.output_digest is None
    assert head(runner).current_extraction_id == good.extraction_revision_id
    assert count(runner, "omnivia_extraction_results") == 2


def test_an_extractor_that_raises_is_recorded_as_failed_and_keeps_the_previous_head(
    runner: ServiceRunner, sources: Sources
) -> None:
    first = sources.capture("cap-1", b"one", None)
    good = reprocess_captured_copy(
        runner,
        capture_id=first.capture.capture_id,
        profile=PROFILE,
        expected_base_extraction_id=None,
        operation_key="ext-1",
        extractor=lambda *_: succeeded(),
    )

    def explode(content: bytes, profile: Any) -> ExtractionOutcome:
        raise RuntimeError("/private/path/leak.pdf could not be parsed")

    failed = reprocess_captured_copy(
        runner,
        capture_id=first.capture.capture_id,
        profile={**PROFILE, "adapterVersion": "2"},
        expected_base_extraction_id=good.extraction_revision_id,
        operation_key="ext-2",
        extractor=explode,
    )

    assert failed.result_state == "failed" and failed.publication_disposition == "not_published"
    assert "leak" not in failed.limitations_json
    assert head(runner).current_extraction_id == good.extraction_revision_id


def test_a_failed_head_advance_rolls_back_the_result_it_followed(
    runner: ServiceRunner, sources: Sources, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = sources.capture("cap-1", b"one", None)
    revision = admit(runner, first.capture.capture_id, "ext-1", None)

    def boom(*_args: object, **_kwargs: object) -> None:
        raise sqlite3.IntegrityError("simulated head failure")

    monkeypatch.setattr(store, "advance_extraction_head", boom)
    with pytest.raises(sqlite3.IntegrityError):
        complete_extraction(runner, extraction_revision_id=revision.extraction_revision_id, outcome=succeeded())

    assert count(runner, "omnivia_extraction_results") == 0
    assert head(runner).current_extraction_id is None
    monkeypatch.undo()
    retry = complete_extraction(
        runner, extraction_revision_id=revision.extraction_revision_id, outcome=succeeded()
    )
    assert retry.publication_disposition == "promoted"


def test_completing_twice_returns_the_stored_result_and_a_different_outcome_is_refused(
    runner: ServiceRunner, sources: Sources
) -> None:
    first = sources.capture("cap-1", b"one", None)
    revision = admit(runner, first.capture.capture_id, "ext-1", None)
    stored = complete_extraction(
        runner, extraction_revision_id=revision.extraction_revision_id, outcome=succeeded()
    )

    assert complete_extraction(
        runner, extraction_revision_id=revision.extraction_revision_id, outcome=succeeded()
    ) == stored
    assert "different terminal result" in refusal(
        lambda: complete_extraction(
            runner, extraction_revision_id=revision.extraction_revision_id, outcome=succeeded("other")
        )
    )
    assert count(runner, "omnivia_extraction_results") == 1


@pytest.mark.parametrize(
    "outcome",
    [
        pytest.param(ExtractionOutcome(state="mystery"), id="unknown-state"),
        pytest.param(ExtractionOutcome(state="succeeded"), id="published-without-output"),
        pytest.param(
            ExtractionOutcome(state="failed", output_digest=digest_of(b"x")), id="failed-with-output"
        ),
        pytest.param(
            ExtractionOutcome(state="succeeded", output_digest="not-a-digest"), id="bad-digest"
        ),
        pytest.param(
            ExtractionOutcome(state="succeeded", output_digest=digest_of(b"x"), coverage={"x": float("nan")}),
            id="non-canonical-coverage",
        ),
    ],
)
def test_an_outcome_outside_its_closed_shape_is_refused_and_keeps_the_previous_head(
    runner: ServiceRunner, sources: Sources, outcome: ExtractionOutcome
) -> None:
    first = sources.capture("cap-1", b"one", None)
    revision = admit(runner, first.capture.capture_id, "ext-1", None)

    with pytest.raises(DocumentIntakeRefused):
        complete_extraction(runner, extraction_revision_id=revision.extraction_revision_id, outcome=outcome)

    assert count(runner, "omnivia_extraction_results") == 0
    assert head(runner).current_extraction_id is None
