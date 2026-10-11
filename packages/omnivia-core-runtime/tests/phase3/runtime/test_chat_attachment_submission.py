"""C7A: staged attachment references through `SubmitMessage`, `EnqueueMessage` and generation.

An accepted attachment is proved atomically at admission (staged row, evidence, blob), kept
pinned in the queue row's tagged combined `references_json` and as a standard `attachment`
Message part, and proved again before every provider request, which projects it as the
contract's opaque `file` part and states it in `authorizedReferences.attachments`.

Reuses the C7A staging harness (a real published blob, evidence artifact and provenance
event under the fence) and the GB-01/GB-05 dispatcher and generation harnesses.

Not here, on purpose: context references (`test_chat_attachments.py` proves they still
fail closed) and any host upload or UI.
"""

# ruff: noqa: F811

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
import test_application_audit_idempotency_migration as m1
import test_chat_offline_queue as gb05
import test_v06_5_s0_mutation_foundation as s0
from omnivia_core_runtime.ownership.identity import FakeClock
from omnivia_core_runtime.service.chat_attachment_authority import (
    ATTACHMENT_PART_TYPE,
    AttachmentRefused,
    PinnedAttachment,
    attachment_part_payload,
    attachment_reference_of_part,
    attachment_unavailable,
    revalidate_attachments,
)
from omnivia_core_runtime.service.chat_generation_executor import ChatGenerationExecutor
from omnivia_core_runtime.storage import chat
from omnivia_core_runtime.storage.chat_attachments import AttachmentEvidence
from omnivia_core_runtime.workspace.blob_publication import blob_path
from test_chat_attachments import (
    CONTENT,
    INTEGRITY,
    _attachment_command,
    _digest,
    _fenced,
    _raw_stage,
    _ref,
    _refused,
    _row,
    _run,
    _seed_file,
    _service,
    _tombstone,
    _written,
)
from test_chat_offline_queue import owned, seeded  # noqa: F401  (pytest fixtures)
from test_chat_submit_message_resolver import (
    ACTOR_ID,
    BASE_US,
    BRANCH_ID,
    CHAT_TABLES,
    CONVERSATION_ID,
    PRINCIPAL,
    WALL,
    WORKSPACE_ID,
    _counts,
    _dispatcher,
    _executor_config,
    _request,
    _stream,
    _submit_command,
)

from omnivia_core.chat_contract.v1 import PROTOCOL_VERSION
from omnivia_core.contracts.v1 import SuccessResponseEnvelope

OTHER = b"another already-uploaded file"
THIRD = b"a third already-uploaded file"


@pytest.fixture
def svc(seeded: m1.Owned) -> Any:
    return _service(seeded)


# --- helpers ----------------------------------------------------------------------------


def _head(service: Any) -> tuple[str, int, int]:
    """The branch head the next `SubmitMessage` must state, and the conversation sequence."""
    branch = chat.read_branch(service.connection, workspace_id=WORKSPACE_ID, branch_id=BRANCH_ID)
    conversation = chat.read_conversation(
        service.connection, workspace_id=WORKSPACE_ID, conversation_id=CONVERSATION_ID
    )
    assert branch is not None and conversation is not None
    assert branch.current_head_message_id is not None
    return (
        branch.current_head_message_id,
        branch.head_version,
        conversation.latest_conversation_sequence,
    )


def _attachment(service: Any, name: str, reference_id: str, command_id: str) -> Any:
    _, _, sequence = _head(service)
    return _dispatcher(service).dispatch(
        _request(
            _attachment_command(reference_id, command_id=command_id),
            command_name=name,
            idempotency_key=f"idem-{command_id}",
            request_id=f"req-{command_id}",
            expected_sequence=sequence,
        )
    )


def _stage(service: Any, reference_id: str, *, command_id: str) -> Any:
    return _attachment(service, "StageAttachment", reference_id, command_id)


def _remove(service: Any, reference_id: str, *, command_id: str) -> Any:
    return _attachment(service, "RemoveStagedAttachment", reference_id, command_id)


def _staged(service: Any, name: str, content: bytes = CONTENT) -> str:
    """Seed one file and stage it for the principal; returns its pinned revision."""
    revision = _seed_file(service, name, content)
    assert isinstance(_stage(service, name, command_id=f"cmd-stage-{name}"), SuccessResponseEnvelope)
    return revision


def _plain(value: Any) -> Any:
    return json.loads(
        json.dumps(value, default=lambda item: dict(item) if isinstance(item, Mapping) else list(item))
    )


def _submit(
    service: Any,
    references: list[Mapping[str, Any]],
    *,
    command_id: str = "cmd-c7a-submit",
    text: str = "look at these",
    head: tuple[str, int, int] | None = None,
    **extra: Any,
) -> Any:
    head_id, head_version, sequence = head or _head(service)
    command = _submit_command(
        command_id=command_id,
        message_id=f"{command_id}.msg",
        actorId=PRINCIPAL,
        expected_head_message_id=head_id,
        expected_head_version=head_version,
        editable_parts=({"type": "text", "payload": {"text": text}},),
        attachment_references=tuple(references),
        **extra,
    )
    return _dispatcher(service).dispatch(
        _request(
            command,
            idempotency_key=f"idem-{command_id}",
            request_id=f"req-{command_id}",
            expected_sequence=sequence,
        )
    )


def _enqueue(
    service: Any,
    references: list[Mapping[str, Any]],
    *,
    command_id: str = "cmd-c7a-enqueue",
    text: str = "queued with files",
) -> Any:
    return _run(
        service,
        "EnqueueMessage",
        gb05._enqueue_command(
            command_id=command_id, text=text, attachmentReferences=list(references)
        ),
    )


def _drain(
    service: Any,
    references: list[Mapping[str, Any]],
    *,
    queue_command_id: str = "cmd-c7a-enqueue",
    command_id: str = "cmd-c7a-drain",
    text: str = "queued with files",
    head: tuple[str, int, int] | None = None,
) -> Any:
    return _submit(
        service,
        references,
        command_id=command_id,
        text=text,
        head=head,
        fromQueuedSubmissionId=f"{queue_command_id}.sub",
    )


def _queue_row(service: Any, command_id: str = "cmd-c7a-enqueue") -> chat.QueuedSubmission:
    row = chat.read_queued_submission(
        service.connection,
        workspace_id=WORKSPACE_ID,
        queued_submission_id=f"{command_id}.sub",
    )
    assert row is not None
    return row


def _parts(service: Any, message_id: str) -> list[chat.MessagePart]:
    return list(
        chat.read_message_parts(
            service.connection, workspace_id=WORKSPACE_ID, message_id=message_id
        )
    )


def _only_replayed(before: Mapping[str, int], after: Mapping[str, int]) -> bool:
    """A replay answers from the stored outcome: it adds one execution row and nothing else."""
    return dict(after) == {**before, s0.EXECUTIONS_TABLE: before[s0.EXECUTIONS_TABLE] + 1}


def _tagged(*pairs: tuple[str, str]) -> tuple[dict[str, str], ...]:
    return tuple(
        {"kind": "attachment", "referenceId": name, "sourceRevision": revision}
        for name, revision in pairs
    )


def _executor(service: Any, provider: Any, *, blobs_root: Any = ...) -> ChatGenerationExecutor:
    return ChatGenerationExecutor(
        connection=service.connection,
        identity=service.identity,
        fencing_generation=service.generation,
        workspace_id=WORKSPACE_ID,
        clock=FakeClock(wall=WALL),
        invoke=provider,
        config=_executor_config(),
        blobs_root=service.layout.blobs_path if blobs_root is ... else blobs_root,
    )


class _Provider:
    """A provider that records what it was asked and answers with one short reply."""

    def __init__(self) -> None:
        self.requests: list[Any] = []

    def __call__(self, request: Any) -> Any:
        self.requests.append(request)
        return _stream(request, ("seen",))


def _job_outcome(service: Any, job_id: str) -> tuple[str, str | None, int]:
    status = chat.read_generation_job_status_projection(
        service.connection, workspace_id=WORKSPACE_ID, generation_job_id=job_id
    )
    assert status is not None
    return status.state, status.sanitized_error_code, status.version


def _assert_no_file_material(service: Any) -> None:
    """No chat row, and no wire document, holds a path, a URL or the file's bytes."""
    needles = [str(service.layout.blobs_path), CONTENT.decode(), OTHER.decode(), "file://"]
    for table in CHAT_TABLES:
        for row in service.connection.execute(f"SELECT * FROM {table}"):
            text = json.dumps([str(cell) for cell in row])
            assert not any(needle in text for needle in needles), table
    manifests = service.connection.execute(
        "SELECT request_manifest_body_json FROM omnivia_chat_request_manifests"
    ).fetchall()
    for (body,) in manifests:
        assert not any(needle in body for needle in needles)
        assert "http" not in body


# --- 1. direct SubmitMessage ----------------------------------------------------------------


def test_direct_submit_persists_pinned_references_in_the_message_and_the_queue_row(
    svc: Any,
) -> None:
    revision_a = _staged(svc, "file-a")
    revision_b = _staged(svc, "file-b", OTHER)
    staged_before = (_row(svc, "file-a"), _row(svc, "file-b"))
    before = _counts(svc)

    # Order is the caller's; an unstated revision takes the pinned one.
    response = _submit(svc, [_ref("file-b"), _ref("file-a", revision_a)])
    assert isinstance(response, SuccessResponseEnvelope), response

    delta = {
        table: count
        for table, count in _written(before, _counts(svc)).items()
        if table in CHAT_TABLES
    }
    assert delta == {
        "omnivia_chat_messages": 1,
        "omnivia_chat_message_parts": 3,
        "omnivia_chat_branch_head_events": 1,
        "omnivia_chat_queued_submissions": 1,
        "omnivia_chat_generation_jobs": 1,
        "omnivia_chat_generation_job_status_projection": 1,
        "omnivia_chat_queue_order_projection": 1,
        "omnivia_chat_transactional_outbox": 1,
    }

    parts = _parts(svc, "cmd-c7a-submit.msg")
    assert [part.part_type for part in parts] == ["text", ATTACHMENT_PART_TYPE, ATTACHMENT_PART_TYPE]
    assert [part.part_index for part in parts] == [0, 1, 2]
    # The opaque reference, plus the media type and byte size the admission check
    # already read to decide the file could be sent at all. Both are what the
    # evidence artifact and its blob row state, never anything the caller supplied.
    assert [dict(part.payload) for part in parts[1:]] == [
        {
            "attachmentReference": {"referenceId": "file-b", "sourceRevision": revision_b},
            "mediaType": "text/plain",
            "sizeBytes": len(OTHER),
        },
        {
            "attachmentReference": {"referenceId": "file-a", "sourceRevision": revision_a},
            "mediaType": "text/plain",
            "sizeBytes": len(CONTENT),
        },
    ]
    assert {part.visibility for part in parts} == {"standard"}
    assert {part.provenance for part in parts} == {"human"}

    # The message hash covers the attachment parts, so it differs from the text-only one.
    plain = _submit(svc, [], command_id="cmd-c7a-plain")
    assert isinstance(plain, SuccessResponseEnvelope), plain
    messages = {
        message.message_id: message
        for message in chat.read_messages_by_conversation_sequence(
            svc.connection, workspace_id=WORKSPACE_ID, conversation_id=CONVERSATION_ID
        )
    }
    assert (
        messages["cmd-c7a-submit.msg"].content_hash != messages["cmd-c7a-plain.msg"].content_hash
    )

    queue = _queue_row(svc, "cmd-c7a-submit")
    assert queue.references == _tagged(("file-b", revision_b), ("file-a", revision_a))
    assert [dict(part) for part in queue.editable_parts] == [
        {"type": "text", "visibility": "standard", "payload": {"text": "look at these"}}
    ]
    # The staged rows are the actor's own authority, not consumed by sending.
    assert (_row(svc, "file-a"), _row(svc, "file-b")) == staged_before
    _assert_no_file_material(svc)

    # Transcript/readback: the queue projection states the same references, in order.
    gb05._give_principal_a_view_state(svc)
    snapshot = gb05._snapshot(svc, request_id="req-c7a-direct")
    queued = {row["queuedSubmissionId"]: row for row in snapshot["queuedSubmissions"]}
    assert queued["cmd-c7a-submit.sub"]["attachmentReferences"] == [
        _ref("file-b", revision_b),
        _ref("file-a", revision_a),
    ]
    assert queued["cmd-c7a-submit.sub"]["contextReferences"] == []
    assert queued["cmd-c7a-plain.sub"]["attachmentReferences"] == []


def test_a_direct_submit_replays_from_its_stored_outcome(svc: Any) -> None:
    revision = _staged(svc, "file-a")
    head = _head(svc)
    first = _submit(svc, [_ref("file-a", revision)], head=head)
    assert isinstance(first, SuccessResponseEnvelope)
    before = _counts(svc)
    replay = _submit(svc, [_ref("file-a", revision)], head=head)
    assert isinstance(replay, SuccessResponseEnvelope)
    assert replay.result == first.result
    assert _only_replayed(before, _counts(svc))


# --- 2. admission refuses atomically ------------------------------------------------------------

_CASES = (
    "never-staged",
    "unknown-reference",
    "removed",
    "other-actor",
    "other-conversation",
    "wrong-revision",
    "tombstoned",
    "integrity-failed",
    "blob-missing",
    "blob-truncated",
    "blob-substituted",
)


def _break(service: Any, case: str) -> list[Mapping[str, Any]]:
    """Make `file-b` unfit in the named way; returns the references to send (b is last)."""
    good = _staged(service, "file-a")
    reference = _ref("file-b")
    digest_b = _digest(OTHER)
    if case == "unknown-reference":
        return [_ref("file-a", good), _ref("file-never-captured")]
    if case in ("never-staged", "other-actor", "other-conversation"):
        revision = _seed_file(service, "file-b", OTHER)
        if case == "other-actor":
            with _fenced(service):
                _raw_stage(service, "file-b", revision=revision, digest=digest_b, actor_id=ACTOR_ID)
        elif case == "other-conversation":
            with _fenced(service):
                service.connection.execute(
                    "INSERT INTO omnivia_chat_conversations (workspace_id, conversation_id, "
                    "state, graph_revision, latest_conversation_sequence, schema_version, "
                    "created_by_actor_id, created_at_us, updated_at_us) "
                    "VALUES (?, 'conv-other', 'active', 1, 0, 1, ?, ?, ?)",
                    (WORKSPACE_ID, PRINCIPAL, BASE_US, BASE_US),
                )
                _raw_stage(
                    service, "file-b", revision=revision, digest=digest_b,
                    conversation_id="conv-other",
                )
        return [_ref("file-a", good), reference]
    revision = _staged(service, "file-b", OTHER)
    if case == "removed":
        assert isinstance(_remove(service, "file-b", command_id="cmd-remove-b"), SuccessResponseEnvelope)
    elif case == "wrong-revision":
        reference = _ref("file-b", "sha256:" + "f" * 64)
    elif case == "tombstoned":
        _tombstone(service, "file-b")
    elif case == "integrity-failed":
        with _fenced(service):
            service.connection.execute(
                f"INSERT INTO {INTEGRITY} (integrity_event_id, workspace_id, content_digest, "
                "integrity_sequence, outcome, observed_digest, observed_length_bytes, "
                "expected_length_bytes, inventory_id, checked_at_us) "
                "VALUES ('int-b', ?, ?, 1, 'missing', NULL, NULL, ?, NULL, ?)",
                (WORKSPACE_ID, digest_b, len(OTHER), BASE_US + 5),
            )
    elif case == "blob-missing":
        blob_path(service.layout.blobs_path, digest_b).unlink()
    elif case == "blob-truncated":
        blob_path(service.layout.blobs_path, digest_b).write_bytes(b"short")
    elif case == "blob-substituted":  # same length: only the admission re-hash sees it
        blob_path(service.layout.blobs_path, digest_b).write_bytes(b"X" * len(OTHER))
    else:  # pragma: no cover
        raise AssertionError(case)
    del revision
    return [_ref("file-a", good), reference]


@pytest.mark.parametrize("surface", ["submit", "enqueue"])
@pytest.mark.parametrize("case", _CASES)
def test_one_bad_reference_refuses_the_whole_request_and_writes_nothing(
    svc: Any, case: str, surface: str
) -> None:
    references = _break(svc, case)
    before = _counts(svc)
    rows_before = (_row(svc, "file-a"), _row(svc, "file-b"))

    send = _submit if surface == "submit" else _enqueue
    _refused(send(svc, references), "conflict")

    # The good reference came first: nothing of it was written either.
    assert _counts(svc) == before, case
    assert (_row(svc, "file-a"), _row(svc, "file-b")) == rows_before


@pytest.mark.parametrize("surface", ["submit", "enqueue"])
def test_duplicate_foreign_actor_and_foreign_workspace_are_invalid_before_any_write(
    svc: Any, surface: str
) -> None:
    revision = _staged(svc, "file-a")
    before = _counts(svc)
    send = _submit if surface == "submit" else _enqueue

    _refused(send(svc, [_ref("file-a", revision), _ref("file-a")]), "invalid_request")

    if surface == "submit":
        foreign_actor = _submit_command(
            command_id="cmd-c7a-foreign-actor",
            actorId=ACTOR_ID,
            attachment_references=(_ref("file-a", revision),),
        )
        foreign_workspace = _submit_command(
            command_id="cmd-c7a-foreign-ws",
            workspace_id="ws-someone-else",
            actorId=PRINCIPAL,
            attachment_references=(_ref("file-a", revision),),
        )
        for command in (foreign_actor, foreign_workspace):
            response = _dispatcher(svc).dispatch(
                _request(command, idempotency_key=f"idem-{command['commandId']}",
                         request_id=f"req-{command['commandId']}")
            )
            assert response.__class__.__name__ == "ErrorResponseEnvelope", response
    else:
        command = gb05._enqueue_command(
            command_id="cmd-c7a-foreign-actor",
            actor_id=ACTOR_ID,
            attachmentReferences=[_ref("file-a", revision)],
        )
        _refused(_run(svc, "EnqueueMessage", command), "invalid_request")
    assert _counts(svc) == before


def test_ancestry_failure_is_unavailable_without_a_database() -> None:
    base = {
        "content_checksum": "sha256:" + "a" * 64,
        "media_type": "text/plain",
        "blob_content_digest": _digest(CONTENT),
        "content_length_bytes": len(CONTENT),
        "tombstoned": False,
        "integrity_failed": False,
    }
    root = Path("/nonexistent-blob-root")
    assert attachment_unavailable(AttachmentEvidence(ancestry_verified=False, **base), root, rehash=False)
    assert attachment_unavailable(AttachmentEvidence(ancestry_verified=True, **base), root, rehash=False)


def test_the_part_payload_is_one_opaque_reference_plus_the_two_display_facts() -> None:
    reference = {"referenceId": "file-a", "sourceRevision": "sha256:" + "a" * 64}
    pinned = PinnedAttachment(
        reference_id=reference["referenceId"],
        source_revision=reference["sourceRevision"],
        media_type="application/pdf",
        size_bytes=2048,
    )
    payload = attachment_part_payload(pinned)
    # The reference, the media type and the byte size -- and NOTHING else. No name:
    # Core stores none. No path, URL, handle or byte, and no room for one.
    assert payload == {
        "attachmentReference": reference,
        "mediaType": "application/pdf",
        "sizeBytes": 2048,
    }
    assert attachment_reference_of_part(payload) == reference
    # `.reference` is the ONLY route to the wire shape, and it is exactly the two
    # contract fields -- the queued row's stored entry and the provider `file` part
    # are both built from it, and both refuse anything wider.
    assert dict(pinned.reference) == reference
    # A part committed before the facts were carried still decodes: they are optional.
    assert attachment_reference_of_part({"attachmentReference": reference}) == reference
    # One fact without the other is still a payload this build can read.
    assert (
        attachment_reference_of_part({"attachmentReference": reference, "sizeBytes": 1})
        == reference
    )
    for bad in (
        {},
        {"attachmentReference": {"referenceId": "file-a"}},
        {"attachmentReference": {**reference, "path": "/tmp/x"}},
        {"attachmentReference": reference, "url": "https://example.test/x"},
        # The display facts are tolerated BY NAME. Anything else beside them is not,
        # so widening the payload stays a deliberate act and never an accident.
        {"attachmentReference": reference, "name": "secrets.pdf"},
        {"attachmentReference": reference, "mediaType": "application/pdf", "path": "/tmp/x"},
        {"attachmentReference": {"referenceId": 1, "sourceRevision": "x"}},
    ):
        assert attachment_reference_of_part(bad) is None


# --- 3. EnqueueMessage, restart and drain ----------------------------------------------------


def test_enqueue_stores_the_pinned_references_and_the_drain_must_restate_them_exactly(
    svc: Any,
) -> None:
    revision_a = _staged(svc, "file-a")
    revision_b = _staged(svc, "file-b", OTHER)
    revision_c = _staged(svc, "file-c", THIRD)

    response = _enqueue(svc, [_ref("file-a", revision_a), _ref("file-b")])
    assert isinstance(response, SuccessResponseEnvelope), response
    queue = _queue_row(svc)
    assert queue.references == _tagged(("file-a", revision_a), ("file-b", revision_b))
    gb05._give_principal_a_view_state(svc)
    projected = gb05._snapshot(svc, request_id="req-c7a-enqueue")["queuedSubmissions"]
    assert [row["attachmentReferences"] for row in projected] == [
        [_ref("file-a", revision_a), _ref("file-b", revision_b)]
    ]

    # An edit changes the authored text and nothing else: the references stay.
    update = _run(
        svc,
        "UpdateQueuedSubmission",
        {
            "protocolVersion": PROTOCOL_VERSION,
            "commandId": "cmd-c7a-update",
            "workspaceId": WORKSPACE_ID,
            "conversationId": CONVERSATION_ID,
            "actorId": PRINCIPAL,
            "queuedSubmissionId": "cmd-c7a-enqueue.sub",
            "editableParts": [{"type": "text", "payload": {"text": "edited text"}}],
            "expectedVersion": 1,
        },
    )
    assert isinstance(update, SuccessResponseEnvelope), update
    assert _queue_row(svc).references == queue.references

    before = _counts(svc)
    attempts = {
        "omitted-all": [],
        "dropped-one": [_ref("file-a", revision_a)],
        "reordered": [_ref("file-b", revision_b), _ref("file-a", revision_a)],
        "substituted": [_ref("file-a", revision_a), _ref("file-c", revision_c)],
        "extra": [_ref("file-a"), _ref("file-b"), _ref("file-c")],
        "re-pinned": [_ref("file-a", revision_a), _ref("file-b", revision_a)],
    }
    for label, references in attempts.items():
        response = _drain(svc, references, command_id=f"cmd-c7a-drain-{label}", text="edited text")
        _refused(response, "conflict")
        assert _counts(svc) == before, label

    # The exact restatement (an unstated revision is the pinned one) is the drain.
    head = _head(svc)
    drained = _drain(svc, [_ref("file-a", revision_a), _ref("file-b")], text="edited text", head=head)
    assert isinstance(drained, SuccessResponseEnvelope), drained
    delta = _written(before, _counts(svc))
    assert delta["omnivia_chat_messages"] == 1 and delta["omnivia_chat_message_parts"] == 3
    assert "omnivia_chat_queued_submissions" not in delta  # the row is reused, not rewritten
    assert [part.part_type for part in _parts(svc, "cmd-c7a-drain.msg")] == [
        "text", ATTACHMENT_PART_TYPE, ATTACHMENT_PART_TYPE
    ]
    assert _queue_row(svc).references == queue.references
    assert _queue_row(svc).state == "queued"

    # Replay: the same drain answers from its stored outcome; a different drain of the
    # same row is the already-drained conflict.
    after = _counts(svc)
    replay = _drain(
        svc, [_ref("file-a", revision_a), _ref("file-b")], text="edited text", head=head
    )
    assert isinstance(replay, SuccessResponseEnvelope) and replay.result == drained.result
    assert _only_replayed(after, _counts(svc))
    _refused(
        _drain(svc, [_ref("file-a", revision_a), _ref("file-b")], command_id="cmd-c7a-drain-2",
               text="edited text"),
        "conflict",
    )


def test_a_drain_with_references_for_a_row_that_has_none_is_the_content_conflict(svc: Any) -> None:
    revision = _staged(svc, "file-a")
    assert isinstance(_enqueue(svc, [], command_id="cmd-c7a-plain-enqueue"), SuccessResponseEnvelope)
    before = _counts(svc)
    response = _drain(
        svc, [_ref("file-a", revision)], queue_command_id="cmd-c7a-plain-enqueue"
    )
    _refused(response, "conflict")
    assert _counts(svc) == before


def test_a_drain_revalidates_what_the_enqueue_accepted(svc: Any) -> None:
    revision = _staged(svc, "file-a")
    assert isinstance(_enqueue(svc, [_ref("file-a", revision)]), SuccessResponseEnvelope)
    assert isinstance(_remove(svc, "file-a", command_id="cmd-remove-a"), SuccessResponseEnvelope)
    before = _counts(svc)
    _refused(_drain(svc, [_ref("file-a", revision)]), "conflict")
    assert _counts(svc) == before
    assert _queue_row(svc).state == "queued"


def test_a_stored_context_entry_in_a_queue_row_can_never_be_drained(svc: Any) -> None:
    with chat.chat_writer(
        svc.connection, svc.identity, workspace_id=WORKSPACE_ID, fencing_generation=svc.generation
    ) as writer:
        writer.append_queued_submission(
            queued_submission_id="cmd-c7a-ctx.sub",
            conversation_id=CONVERSATION_ID,
            actor_id=PRINCIPAL,
            queue_sequence=1,
            branch_id="branch-gb01-main",
            editable_parts=(
                {"type": "text", "visibility": "standard", "payload": {"text": "ctx"}},
            ),
            references=({"kind": "context", "referenceId": "ctx-1"},),
            idempotency_key="cmd-c7a-ctx",
            created_at_us=BASE_US + 1,
            updated_at_us=BASE_US + 1,
        )
        writer.insert_queue_order_projection(
            queued_submission_id="cmd-c7a-ctx.sub",
            conversation_id=CONVERSATION_ID,
            queue_position=1,
            updated_by_actor_id=PRINCIPAL,
            updated_at_us=BASE_US + 1,
        )
    before = _counts(svc)
    for references in ([], [_ref("ctx-1")]):
        response = _drain(svc, references, queue_command_id="cmd-c7a-ctx", text="ctx")
        _refused(response, "conflict")
    assert _counts(svc) == before


def test_enqueue_drain_and_generation_survive_a_restart(tmp_path: Path) -> None:
    path = tmp_path / "c7a-submission-restart.sqlite"
    s0.materialise_phase0_baseline(path)
    m1.bootstrap_and_migrate(path)
    holder = m1.take_ownership(path)
    try:
        gb05._seed_restart_workspace(holder)
        gb05._give_principal_a_view_state(holder)
        service = _service(holder)
        revision_a = _staged(service, "file-a")
        revision_b = _staged(service, "file-b", OTHER)
        assert isinstance(
            _enqueue(service, [_ref("file-b", revision_b), _ref("file-a")]), SuccessResponseEnvelope
        )
    finally:
        holder.connection.close()

    reopened = m1.take_ownership(path)
    try:
        service = _service(reopened)
        wanted = [_ref("file-b", revision_b), _ref("file-a", revision_a)]
        assert _queue_row(service).references == _tagged(
            ("file-b", revision_b), ("file-a", revision_a)
        )
        snapshot = gb05._snapshot(service, request_id="req-c7a-restart-1")
        assert snapshot["queuedSubmissions"][0]["attachmentReferences"] == wanted
        before = _counts(service)
        # Substitution is still refused after a restart; the exact drain is accepted.
        _refused(_drain(service, [_ref("file-a", revision_a)]), "conflict")
        assert _counts(service) == before
        assert isinstance(_drain(service, wanted), SuccessResponseEnvelope)
    finally:
        reopened.connection.close()

    again = m1.take_ownership(path)
    try:
        service = _service(again)
        provider = _Provider()
        job_id = _executor(service, provider).execute_next()
        assert job_id == "cmd-c7a-enqueue.gen"
        assert len(provider.requests) == 1
        assert _job_outcome(service, job_id)[:2] == ("succeeded", None)
        assert _plain(provider.requests[0].messages[-1]["parts"]) == [
            {"kind": "text", "text": "queued with files"},
            {"kind": "file", "attachmentReference": wanted[0]},
            {"kind": "file", "attachmentReference": wanted[1]},
        ]
        before = _counts(service)
        # Restart idempotency: the finished Generation is not invoked or rewritten again.
        assert _executor(service, provider).execute_next() is None
        assert len(provider.requests) == 1 and _counts(service) == before
    finally:
        again.connection.close()


# --- 4. generation: the manifest and the provider projection ------------------------------------


def test_the_request_manifest_and_provider_request_state_the_attachments(svc: Any) -> None:
    revision_a = _staged(svc, "file-a")
    revision_b = _staged(svc, "file-b", OTHER)
    assert isinstance(
        _submit(svc, [_ref("file-b"), _ref("file-a", revision_a)]), SuccessResponseEnvelope
    )
    provider = _Provider()

    job_id = _executor(svc, provider).execute_next()
    assert job_id == "cmd-c7a-submit.gen"
    assert _job_outcome(svc, job_id)[:2] == ("succeeded", None)
    (request,) = provider.requests

    wanted = [_ref("file-b", revision_b), _ref("file-a", revision_a)]
    assert _plain(request.messages[-1]) == {
        "role": "user",
        "parts": [
            {"kind": "text", "text": "look at these"},
            {"kind": "file", "attachmentReference": wanted[0]},
            {"kind": "file", "attachmentReference": wanted[1]},
        ],
    }
    wire = json.dumps(request.to_wire())
    assert str(svc.layout.blobs_path) not in wire and CONTENT.decode() not in wire
    assert "http" not in wire and "path" not in wire.replace("attachmentReference", "")

    attempt = next(
        row[0]
        for row in svc.connection.execute(
            "SELECT generation_attempt_id FROM omnivia_chat_generation_attempts"
        )
    )
    manifest = chat.read_request_manifest(
        svc.connection, workspace_id=WORKSPACE_ID, generation_attempt_id=attempt
    )
    assert manifest is not None
    body = manifest.manifest_body
    assert body["authorizedReferences"] == {"context": [], "attachments": wanted, "evidence": []}
    sent = next(m for m in body["modelVisible"]["messages"] if m["messageId"] == "cmd-c7a-submit.msg")
    assert [part["partType"] for part in sent["parts"]] == ["text", "attachment", "attachment"]
    _assert_no_file_material(svc)


def test_a_message_without_attachments_keeps_the_empty_manifest_list(svc: Any) -> None:
    assert isinstance(_submit(svc, []), SuccessResponseEnvelope)
    provider = _Provider()
    assert _executor(svc, provider).execute_next() == "cmd-c7a-submit.gen"
    attempt = svc.connection.execute(
        "SELECT generation_attempt_id FROM omnivia_chat_generation_attempts"
    ).fetchone()[0]
    manifest = chat.read_request_manifest(
        svc.connection, workspace_id=WORKSPACE_ID, generation_attempt_id=attempt
    )
    assert manifest is not None
    assert manifest.manifest_body["authorizedReferences"]["attachments"] == []
    assert _plain(provider.requests[0].messages[-1]["parts"]) == [{"kind": "text", "text": "look at these"}]


def test_an_earlier_turns_attachment_is_reproved_by_evidence_not_by_the_composer(svc: Any) -> None:
    revision = _staged(svc, "file-a")
    assert isinstance(_submit(svc, [_ref("file-a", revision)]), SuccessResponseEnvelope)
    provider = _Provider()
    assert _executor(svc, provider).execute_next() == "cmd-c7a-submit.gen"

    # The actor clears the composer; the later turn still carries the file it was sent with.
    assert isinstance(_remove(svc, "file-a", command_id="cmd-remove-a"), SuccessResponseEnvelope)
    assert isinstance(_submit(svc, [], command_id="cmd-c7a-second", text="and now?"), SuccessResponseEnvelope)
    assert _executor(svc, provider).execute_next() == "cmd-c7a-second.gen"
    assert _job_outcome(svc, "cmd-c7a-second.gen")[:2] == ("succeeded", None)
    history = _plain(provider.requests[1].messages)
    assert any(
        {"kind": "file", "attachmentReference": _ref("file-a", revision)} in message["parts"]
        for message in history
    )

    # …but the evidence going away does stop that later turn.
    _tombstone(svc, "file-a")
    assert isinstance(_submit(svc, [], command_id="cmd-c7a-third", text="still there?"), SuccessResponseEnvelope)
    calls = len(provider.requests)
    assert _executor(svc, provider).execute_next() == "cmd-c7a-third.gen"
    assert len(provider.requests) == calls
    assert _job_outcome(svc, "cmd-c7a-third.gen")[:2] == ("failed", "invalid-request")


# --- 5. revoked between admission and generation -----------------------------------------------

_REVOCATIONS = ("removed", "tombstoned", "integrity-failed", "blob-missing", "blob-substituted")


def _revoke(service: Any, case: str) -> None:
    digest = _digest(OTHER)
    if case == "removed":
        assert isinstance(_remove(service, "file-b", command_id="cmd-remove-b"), SuccessResponseEnvelope)
    elif case == "tombstoned":
        _tombstone(service, "file-b")
    elif case == "integrity-failed":
        with _fenced(service):
            service.connection.execute(
                f"INSERT INTO {INTEGRITY} (integrity_event_id, workspace_id, content_digest, "
                "integrity_sequence, outcome, observed_digest, observed_length_bytes, "
                "expected_length_bytes, inventory_id, checked_at_us) "
                "VALUES ('int-b', ?, ?, 1, 'missing', NULL, NULL, ?, NULL, ?)",
                (WORKSPACE_ID, digest, len(OTHER), BASE_US + 5),
            )
    elif case == "blob-missing":
        blob_path(service.layout.blobs_path, digest).unlink()
    elif case == "blob-substituted":
        blob_path(service.layout.blobs_path, digest).write_bytes(b"X" * len(OTHER))
    else:  # pragma: no cover
        raise AssertionError(case)


@pytest.mark.parametrize("case", _REVOCATIONS)
def test_a_withdrawn_attachment_ends_the_generation_before_any_provider_call(
    svc: Any, case: str
) -> None:
    revision_a = _staged(svc, "file-a")
    _staged(svc, "file-b", OTHER)
    assert isinstance(_submit(svc, [_ref("file-a", revision_a), _ref("file-b")]), SuccessResponseEnvelope)
    _revoke(svc, case)
    messages_before = m1.count(svc.connection, "omnivia_chat_messages")
    provider = _Provider()

    job_id = _executor(svc, provider).execute_next()

    assert job_id == "cmd-c7a-submit.gen"
    assert provider.requests == []  # no partial provider call
    state, code, _ = _job_outcome(svc, job_id)
    assert (state, code) == ("failed", "invalid-request"), case
    # Honest and final: no manifest or invocation was written for a request never made,
    # no assistant Message exists, and the failure is not offered as a retry.
    assert m1.count(svc.connection, "omnivia_chat_request_manifests") == 0
    assert m1.count(svc.connection, "omnivia_chat_messages") == messages_before
    status = chat.read_generation_job_status_projection(
        svc.connection, workspace_id=WORKSPACE_ID, generation_job_id=job_id
    )
    assert status is not None and status.result_message_id is None
    (outcome,) = chat.read_generation_attempt_outcomes(
        svc.connection, workspace_id=WORKSPACE_ID, generation_job_id=job_id
    )
    assert (outcome.terminal_state, outcome.retryable) == ("failed", False)
    error_events = [
        event
        for event in chat.read_generation_events(
            svc.connection, workspace_id=WORKSPACE_ID, generation_job_id=job_id
        )
        if event.event_type == "chat.generation.failed"
    ]
    assert len(error_events) == 1
    assert "file-b" not in json.dumps(dict(error_events[0].payload))

    # Restart: the terminal Generation is closed, not re-run, and nothing more is written.
    before = _counts(svc)
    assert _executor(svc, provider).execute_next() is None
    assert provider.requests == [] and _counts(svc) == before


def test_without_a_blob_root_the_executor_fails_closed_on_any_attachment(svc: Any) -> None:
    revision = _staged(svc, "file-a")
    assert isinstance(_submit(svc, [_ref("file-a", revision)]), SuccessResponseEnvelope)
    provider = _Provider()
    job_id = _executor(svc, provider, blobs_root=None).execute_next()
    assert provider.requests == []
    assert _job_outcome(svc, job_id or "")[:2] == ("failed", "invalid-request")


def test_a_malformed_stored_attachment_part_never_reaches_the_provider(svc: Any) -> None:
    assert isinstance(_submit(svc, []), SuccessResponseEnvelope)
    # Not reachable through the commands, so this proves the executor's own decode with a
    # part this build did not write: a path where an opaque reference must be.
    with chat.chat_writer(
        svc.connection, svc.identity, workspace_id=WORKSPACE_ID, fencing_generation=svc.generation
    ) as writer:
        writer.append_message_part(
            part_id="cmd-c7a-submit.msg.p1",
            message_id="cmd-c7a-submit.msg",
            conversation_id=CONVERSATION_ID,
            part_index=1,
            part_type=ATTACHMENT_PART_TYPE,
            schema_version=1,
            visibility="standard",
            payload={"path": "/etc/passwd"},
            content_hash="sha256:" + "0" * 64,
            created_at_us=BASE_US,
            provenance="human",
        )
    provider = _Provider()
    job_id = _executor(svc, provider).execute_next()
    assert provider.requests == []
    assert _job_outcome(svc, job_id or "")[1] == "malformed-response"


def test_revalidate_attachments_is_all_or_nothing_and_writes_nothing(svc: Any) -> None:
    revision = _staged(svc, "file-a")
    _seed_file(svc, "file-b", OTHER)  # captured but never staged
    before = _counts(svc)
    root = svc.layout.blobs_path

    def check(references: list[Mapping[str, Any]], **kwargs: Any) -> Any:
        return revalidate_attachments(
            svc.connection, workspace_id=WORKSPACE_ID, conversation_id=CONVERSATION_ID,
            actor_id=PRINCIPAL, references=references, blobs_root=root,
            require_staged=kwargs.get("require_staged", True), rehash=True,
        )

    assert check([_ref("file-a")]) == (
        PinnedAttachment(
            reference_id="file-a",
            source_revision=revision,
            media_type="text/plain",
            size_bytes=len(CONTENT),
        ),
    )
    with pytest.raises(AttachmentRefused) as refused:
        check([_ref("file-a"), _ref("file-b")])
    assert refused.value.reason == "not_staged"
    with pytest.raises(AttachmentRefused) as refused:
        check([_ref("file-a"), _ref("file-a")])
    assert refused.value.reason == "duplicate"
    # A sent Message's own reference must state its revision; the composer is not asked.
    with pytest.raises(AttachmentRefused) as refused:
        check([_ref("file-a")], require_staged=False)
    assert refused.value.reason == "revision"
    assert check([_ref("file-b", _digest(OTHER))], require_staged=False)[0].reference_id == "file-b"
    assert _counts(svc) == before
