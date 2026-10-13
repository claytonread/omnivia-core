"""Document intake: capture atomicity, idempotency, stale bases, separate heads, profiles, retained-copy
reprocessing, completion order, removal and retention fencing, and failure that keeps the previous head.

Bindings are opaque owner/device routes; a capture records the binding lineage it was taken under, its intent kind
and a stable disposition reason, and each new capture writes exactly one pending processing obligation.

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
    BindingClaim,
    DocumentCaptureSuppressed,
    DocumentIntakeRefused,
    ExtractedSegment,
    ExtractionOutcome,
    admit_extraction,
    capture_document,
    complete_extraction,
    connect_original,
    current_capture,
    disconnect_original,
    expire_capture,
    owned_workspace,
    pending_capture_obligations,
    read_extraction_projection,
    read_retained_source,
    register_profile,
    reprocess_captured_copy,
    set_document_source_state,
    settle_capture_obligation,
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
def paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
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
    return workspace, installation


@pytest.fixture
def runner(paths: tuple[Path, Path]) -> Iterator[ServiceRunner]:
    with owned_workspace(
        workspace_root=paths[0], installation_root=paths[1], core_version=SERVER_VERSION
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
            "omnivia_document_capture_outbox",
            "omnivia_document_original_bindings",
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
        intent: str | None = None,
        binding: BindingClaim | None = None,
    ) -> service.DocumentCaptureResult:
        return capture_document(
            self.runner,
            source_path=self.path(name or f"{key}.txt", content),
            document_id=document_id,
            expected_base_capture_id=base,
            operation_key=key,
            media_type=media_type,
            intent_kind=intent or ("new_capture" if base is None else "refresh"),
            binding=binding,
        )


@pytest.fixture
def sources(runner: ServiceRunner, tmp_path: Path) -> Sources:
    return Sources(runner, tmp_path / "originals")


def succeeded(
    label: str = "out", segments: tuple[ExtractedSegment, ...] = ()
) -> ExtractionOutcome:
    return ExtractionOutcome(
        state="succeeded",
        output_digest=digest_of(label.encode()),
        coverage={"pages": 1},
        limitations={},
        worker_build="test-1",
        segments=segments,
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
    assert snapshot(runner) == (1, 1, 1, 1, 0, 0, 1, 0)
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
            intent_kind="refresh",
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
    assert snapshot(runner) == (0, 0, 0, 0, 0, 0, 0, 0)


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
    nothing = sources.capture("cap-4", b"unseen", None, intent="refresh")

    assert late.became_head is False and nothing.became_head is False
    assert late.capture.disposition == nothing.capture.disposition == "historical_only"
    assert late.capture.disposition_reason == nothing.capture.disposition_reason == "stale_base"
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


# -- capture: original bindings and admission intent ----------------------------------------


def claim(binding: store.StoredBinding) -> BindingClaim:
    return BindingClaim(
        owner_ref=binding.owner_ref,
        generation=binding.binding_generation,
        route_kind=binding.route_kind,
        root_ref=binding.root_ref,
        member_ref=binding.member_ref,
    )


def test_a_disconnect_before_first_capture_is_an_owner_scoped_tombstone(
    runner: ServiceRunner, sources: Sources
) -> None:
    tombstone = disconnect_original(
        runner, document_id=DOC, owner_ref="owner-a", route_kind="direct_file"
    )

    captured = sources.capture("cap-1", b"late bytes", None, binding=claim(tombstone))

    assert (tombstone.binding_generation, tombstone.connection_state) == (1, "disconnected")
    assert (captured.capture.association_kind, captured.capture.disposition_reason) == (
        "historical_binding",
        "disconnected_generation",
    )
    assert captured.became_head is False
    assert head(runner).current_capture_id is None
    assert [row.capture_id for row in pending_capture_obligations(runner, limit=10)] == [
        captured.capture.capture_id
    ]


def test_disconnecting_one_owner_does_not_disconnect_another_owner(
    runner: ServiceRunner, sources: Sources
) -> None:
    owner_a = connect_original(
        runner, document_id=DOC, owner_ref="owner-a", route_kind="direct_file"
    )
    owner_b = connect_original(
        runner, document_id=DOC, owner_ref="owner-b", route_kind="direct_file"
    )
    disconnect_original(runner, document_id=DOC, owner_ref="owner-a", route_kind="direct_file")

    active = sources.capture("cap-b", b"owner b", None, binding=claim(owner_b))
    historical = sources.capture(
        "cap-a", b"owner a", active.capture.capture_id, binding=claim(owner_a)
    )

    assert (active.capture.association_kind, active.capture.disposition_reason) == (
        "active_binding",
        "eligible",
    )
    assert (historical.capture.association_kind, historical.capture.disposition_reason) == (
        "historical_binding",
        "disconnected_generation",
    )
    assert head(runner).current_capture_id == active.capture.capture_id


def test_relink_advances_generation_and_keeps_old_generation_historical(
    runner: ServiceRunner, sources: Sources
) -> None:
    first_generation = connect_original(
        runner,
        document_id=DOC,
        owner_ref="owner-a",
        route_kind="root_member",
        root_ref="root-a",
        member_ref="member-a",
    )
    disconnect_original(
        runner,
        document_id=DOC,
        owner_ref="owner-a",
        route_kind="root_member",
        root_ref="root-a",
        member_ref="member-a",
    )
    relinked = connect_original(
        runner,
        document_id=DOC,
        owner_ref="owner-a",
        route_kind="root_member",
        root_ref="root-a",
        member_ref="member-a",
    )

    old = sources.capture("cap-old", b"old", None, binding=claim(first_generation))
    current = sources.capture(
        "cap-current", b"current", None, intent="refresh", binding=claim(relinked)
    )

    assert relinked.binding_generation == 2
    assert (old.capture.association_kind, old.capture.disposition_reason) == (
        "historical_binding",
        "superseded_generation",
    )
    assert current.capture.association_kind == "active_binding"
    assert current.became_head is True


def test_unknown_cross_document_and_wrong_route_binding_claims_write_nothing(
    runner: ServiceRunner, sources: Sources
) -> None:
    bound = connect_original(
        runner, document_id="doc-other", owner_ref="owner-a", route_kind="direct_file"
    )
    before = snapshot(runner)

    assert "unknown" in refusal(
        lambda: sources.capture(
            "cap-unknown",
            b"x",
            None,
            binding=BindingClaim("owner-unknown", 1, "direct_file"),
        )
    )
    assert "another document" in refusal(
        lambda: sources.capture("cap-cross", b"x", None, binding=claim(bound))
    )
    assert "different route" in refusal(
        lambda: sources.capture(
            "cap-route",
            b"x",
            None,
            document_id="doc-other",
            binding=BindingClaim("owner-a", 1, "root_member", "root-a", "member-a"),
        )
    )
    assert snapshot(runner) == before


def test_snapshot_capture_has_no_binding_lineage_and_new_capture_is_a_creation_precondition(
    runner: ServiceRunner, sources: Sources
) -> None:
    first = sources.capture("cap-1", b"one", None)
    conflict = sources.capture("cap-2", b"two", None)

    assert first.capture.association_kind == "snapshot_only"
    assert (
        first.capture.binding_owner_ref,
        first.capture.binding_generation,
        first.capture.binding_route_kind,
    ) == (None, None, None)
    assert (conflict.capture.disposition, conflict.capture.disposition_reason) == (
        "historical_only",
        "creation_conflict",
    )
    assert head(runner).current_capture_id == first.capture.capture_id


def test_a_refresh_with_an_explicit_null_base_can_fill_an_empty_bound_document(
    runner: ServiceRunner, sources: Sources
) -> None:
    binding = connect_original(
        runner, document_id=DOC, owner_ref="owner-a", route_kind="direct_file"
    )

    captured = sources.capture(
        "cap-1", b"first", None, intent="refresh", binding=claim(binding)
    )

    assert captured.became_head is True
    assert captured.capture.disposition_reason == "eligible"


def test_capture_replay_includes_binding_lineage_and_has_one_outbox_obligation(
    runner: ServiceRunner, sources: Sources
) -> None:
    binding = connect_original(
        runner, document_id=DOC, owner_ref="owner-a", route_kind="direct_file"
    )
    first = sources.capture("cap-1", b"one", None, binding=claim(binding))
    replay = sources.capture("cap-1", b"one", None, binding=claim(binding))

    assert replay.status == "replayed" and replay.capture == first.capture
    assert count(runner, "omnivia_document_capture_outbox") == 1
    assert "different capture request" in refusal(
        lambda: sources.capture(
            "cap-1",
            b"one",
            None,
            binding=BindingClaim("owner-a", binding.binding_generation + 1, "direct_file"),
        )
    )


# -- capture: durable processing obligation and retention-aware reads ----------------------


def test_capture_obligations_are_ordered_bounded_and_settle_idempotently(
    runner: ServiceRunner, sources: Sources
) -> None:
    first = sources.capture("cap-1", b"one", None)
    second = sources.capture("cap-2", b"two", first.capture.capture_id)

    assert [row.capture_id for row in pending_capture_obligations(runner, limit=1)] == [
        first.capture.capture_id
    ]
    settled = settle_capture_obligation(runner, capture_id=first.capture.capture_id)
    replay = settle_capture_obligation(runner, capture_id=first.capture.capture_id)

    assert settled.outbox_state == "settled" and settled.settled_at_us is not None
    assert replay == settled
    assert [row.capture_id for row in pending_capture_obligations(runner, limit=10)] == [
        second.capture.capture_id
    ]
    assert "bounded" in refusal(lambda: pending_capture_obligations(runner, limit=0))
    assert "no such capture obligation" in refusal(
        lambda: settle_capture_obligation(runner, capture_id="dcap-" + "0" * 64)
    )


def test_current_capture_never_falls_back_after_the_head_expires(
    runner: ServiceRunner, sources: Sources
) -> None:
    first = sources.capture("cap-1", b"one", None)
    second = sources.capture("cap-2", b"two", first.capture.capture_id)

    assert current_capture(runner, document_id=DOC) == second.capture
    expire_capture(runner, capture_id=second.capture.capture_id)

    assert current_capture(runner, document_id=DOC) is None
    assert head(runner).current_capture_id == second.capture.capture_id


@pytest.mark.parametrize("intent", ["automatic_capture", "folder_capture"])
def test_automatic_intents_cannot_reintroduce_identical_expired_bytes(
    runner: ServiceRunner, sources: Sources, intent: str
) -> None:
    first = sources.capture("cap-1", b"one", None)
    expire_capture(runner, capture_id=first.capture.capture_id)
    before = snapshot(runner)

    with pytest.raises(DocumentCaptureSuppressed, match="expired or deleted"):
        sources.capture("cap-auto", b"one", first.capture.capture_id, intent=intent)

    assert snapshot(runner) == before


def test_manual_capture_can_reintroduce_identical_expired_bytes_as_history(
    runner: ServiceRunner, sources: Sources
) -> None:
    first = sources.capture("cap-1", b"one", None)
    expire_capture(runner, capture_id=first.capture.capture_id)

    manual = sources.capture("cap-2", b"one", first.capture.capture_id, intent="refresh")

    assert manual.capture.source_digest == first.capture.source_digest
    assert manual.capture.disposition == "head_eligible"


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


# -- located extraction output and current retrieval projection -----------------------------


def segment(
    capture_id: str,
    text: str,
    *,
    role: str = "body",
    visibility: str = "visible",
    revision_state: str = "current",
) -> ExtractedSegment:
    return ExtractedSegment(
        semantic_role=role,
        source_locator={"captureId": capture_id, "block": text},
        text=text,
        visibility=visibility,
        revision_state=revision_state,
    )


def test_a_promoted_result_publishes_located_segments_with_protected_roles_filtered(
    runner: ServiceRunner, sources: Sources
) -> None:
    captured = sources.capture("cap-1", b"one", None)
    revision = admit(runner, captured.capture.capture_id, "ext-1", None)
    segments = (
        segment(captured.capture.capture_id, "Needle body"),
        segment(captured.capture.capture_id, "Reviewer note", role="comment", visibility="annotation"),
        segment(captured.capture.capture_id, "Hidden run", role="hidden_content", visibility="hidden"),
    )

    result = complete_extraction(
        runner,
        extraction_revision_id=revision.extraction_revision_id,
        outcome=succeeded(segments=segments),
    )

    assert result.publication_disposition == "promoted"
    assert len(result.segments_digest) == 71
    assert [
        row.text
        for row in store.read_segments(
            runner.connection,
            workspace_id=runner.workspace_id,
            extraction_revision_id=revision.extraction_revision_id,
        )
    ] == ["Needle body", "Reviewer note", "Hidden run"]
    assert [row.text for row in read_extraction_projection(runner, document_id=DOC)] == [
        "Needle body"
    ]
    all_roles = frozenset({"body", "comment", "hidden_content"})
    assert [
        row.semantic_role
        for row in read_extraction_projection(runner, document_id=DOC, include_roles=all_roles)
    ] == ["body", "comment", "hidden_content"]
    assert [
        row.text for row in read_extraction_projection(runner, document_id=DOC, query="needle")
    ] == ["Needle body"]


def test_a_new_promoted_revision_atomically_replaces_the_document_projection(
    runner: ServiceRunner, sources: Sources
) -> None:
    captured = sources.capture("cap-1", b"one", None)
    first = admit(runner, captured.capture.capture_id, "ext-1", None)
    complete_extraction(
        runner,
        extraction_revision_id=first.extraction_revision_id,
        outcome=succeeded("first", (segment(captured.capture.capture_id, "old"),)),
    )
    second = admit(runner, captured.capture.capture_id, "ext-2", first.extraction_revision_id)

    complete_extraction(
        runner,
        extraction_revision_id=second.extraction_revision_id,
        outcome=succeeded("second", (segment(captured.capture.capture_id, "new"),)),
    )

    projected = read_extraction_projection(runner, document_id=DOC)
    assert [(row.extraction_revision_id, row.text) for row in projected] == [
        (second.extraction_revision_id, "new")
    ]
    assert count(runner, "omnivia_document_extraction_projection") == 1
    assert count(runner, "omnivia_extracted_segments") == 2


def test_a_historical_result_keeps_its_segments_without_replacing_current_retrieval(
    runner: ServiceRunner, sources: Sources
) -> None:
    captured = sources.capture("cap-1", b"one", None)
    first = admit(runner, captured.capture.capture_id, "ext-1", None)
    complete_extraction(
        runner,
        extraction_revision_id=first.extraction_revision_id,
        outcome=succeeded("first", (segment(captured.capture.capture_id, "current"),)),
    )
    historical = admit(runner, captured.capture.capture_id, "ext-2", None)

    result = complete_extraction(
        runner,
        extraction_revision_id=historical.extraction_revision_id,
        outcome=succeeded("late", (segment(captured.capture.capture_id, "historical"),)),
    )

    assert result.publication_disposition == "historical_only"
    assert [row.text for row in read_extraction_projection(runner, document_id=DOC)] == ["current"]
    assert [
        row.text
        for row in store.read_segments(
            runner.connection,
            workspace_id=runner.workspace_id,
            extraction_revision_id=historical.extraction_revision_id,
        )
    ] == ["historical"]


def test_an_invalid_segment_locator_rolls_back_result_segments_projection_and_head(
    runner: ServiceRunner, sources: Sources
) -> None:
    captured = sources.capture("cap-1", b"one", None)
    revision = admit(runner, captured.capture.capture_id, "ext-1", None)

    assert "must name its capture" in refusal(
        lambda: complete_extraction(
            runner,
            extraction_revision_id=revision.extraction_revision_id,
            outcome=succeeded(segments=(segment("dcap-" + "0" * 64, "wrong capture"),)),
        )
    )

    assert count(runner, "omnivia_extraction_results") == 0
    assert count(runner, "omnivia_extracted_segments") == 0
    assert count(runner, "omnivia_document_extraction_projection") == 0
    assert head(runner).current_extraction_id is None


def test_a_terminal_replay_requires_the_exact_same_segment_set(
    runner: ServiceRunner, sources: Sources
) -> None:
    captured = sources.capture("cap-1", b"one", None)
    revision = admit(runner, captured.capture.capture_id, "ext-1", None)
    complete_extraction(
        runner,
        extraction_revision_id=revision.extraction_revision_id,
        outcome=succeeded(segments=(segment(captured.capture.capture_id, "same"),)),
    )

    assert "different terminal result" in refusal(
        lambda: complete_extraction(
            runner,
            extraction_revision_id=revision.extraction_revision_id,
            outcome=succeeded(segments=(segment(captured.capture.capture_id, "changed"),)),
        )
    )
    assert [row.text for row in read_extraction_projection(runner, document_id=DOC)] == ["same"]


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
        intent_kind="new_capture",
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


def test_a_settled_reprocess_replay_survives_later_capture_expiry(
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
    expire_capture(runner, capture_id=first.capture.capture_id)
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


def test_a_removed_source_admits_captures_as_history_refuses_extractions_and_keeps_its_heads(
    runner: ServiceRunner, sources: Sources
) -> None:
    first = sources.capture("cap-1", b"one", None)
    set_document_source_state(runner, document_id=DOC, removed=True)

    late = sources.capture("cap-2", b"two", first.capture.capture_id)
    assert "source is removed" in refusal(
        lambda: admit_extraction(
            runner,
            capture_id=first.capture.capture_id,
            profile_digest=register_profile(runner, PROFILE),
            expected_base_extraction_id=None,
            operation_key="ext-1",
        )
    )

    assert late.became_head is False
    assert (late.capture.disposition, late.capture.disposition_reason) == ("historical_only", "removed_source")
    assert late.capture.source_removal_generation == 2
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
