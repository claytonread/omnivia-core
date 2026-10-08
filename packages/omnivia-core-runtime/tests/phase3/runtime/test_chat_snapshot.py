"""W5 GB: the production `chat.snapshot` resolver, `service/chat_snapshot.py`.

A pure read with one authoritative storage call, so these tests substitute that call
rather than a database: what is under test is the query decode, the single bound read,
and the projection -- not SQL that `test_chat_repository.py` already covers.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import test_application_audit_idempotency_migration as m1
import test_chat_direction_migration as dm
import test_chat_foundation_migration as foundation
import test_chat_repository as repo
import test_v06_5_s0_mutation_foundation as s0
from jsonschema import Draft202012Validator
from omnivia_core_runtime.ownership.identity import SystemClock
from omnivia_core_runtime.service import chat_snapshot
from omnivia_core_runtime.service.application import (
    CHAT_FAMILY_PURPOSES,
    CHAT_OBSERVATION_PURPOSE,
    build_chat_registry,
    chat_family_session,
)
from omnivia_core_runtime.service.authorization import ServiceBinding
from omnivia_core_runtime.service.handlers import chat as chat_handler_module
from omnivia_core_runtime.service.handlers.chat import (
    CHAT_FAMILY_OPERATIONS,
    CHAT_SNAPSHOT_OPERATION,
    ChatHandlers,
)
from omnivia_core_runtime.service.operations import OperationContext, OperationError
from omnivia_core_runtime.storage.chat import (
    Branch,
    Conversation,
    ConversationSnapshotInputs,
    Direction,
    GenerationJob,
    GenerationJobStatusProjection,
    Message,
    MessagePart,
    ViewState,
)
from omnivia_core_runtime.storage.connection import StorageError
from omnivia_core_runtime.storage.memory import random_identifier
from omnivia_core_runtime.storage.migrations import materialise_phase0_baseline
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from omnivia_core.chat_contract.v1.codec import (
    direction_content_hash,
    validate_direction,
)
from omnivia_core.contracts.v1 import get_operation_metadata
from omnivia_core.contracts.v1.generated import ChatSnapshotInput

OPERATION = "chat.snapshot"
ENTRY = get_operation_metadata(OPERATION)

#: The canonical Chat Contract v1 schema bundle, loaded the same way
#: `tests/chat_contract/test_fixtures.py` and `test_codec.py` do: `jsonschema`/
#: `referencing` are development-only test dependencies, never imported by
#: production code.
_SCHEMAS_DIR = (
    Path(__file__).resolve().parents[5] / "contracts" / "chat" / "v1" / "schemas"
)
_SCHEMA_REGISTRY = Registry().with_resources(
    (schema["$id"], Resource.from_contents(schema, default_specification=DRAFT202012))
    for schema in (
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(_SCHEMAS_DIR.glob("*.schema.json"))
    )
)
_QUERIES_SCHEMA_ID = "https://contracts.omnivia.dev/chat/v1/queries.schema.json"
_BRANCH_SCHEMA_ID = "https://contracts.omnivia.dev/chat/v1/branch.schema.json"


def _schema_errors(ref: str, document: Any) -> list[str]:
    validator = Draft202012Validator({"$ref": ref}, registry=_SCHEMA_REGISTRY)
    return [error.message for error in validator.iter_errors(document)]

WORKSPACE_ID = s0.WORKSPACE_ID
PRINCIPAL = s0.PRINCIPAL
CONVERSATION_ID = "conv-snapshot-01"
BRANCH_ID = "branch-snapshot-main"
DEVICE_ID = "device-snapshot-01"
REQUEST_ID = "req-snapshot-1"
ROOT_MESSAGE_ID = "msg-snapshot-root"
HEAD_MESSAGE_ID = "msg-snapshot-head"
BASE_US = 2_600_000_000_000_000


def _digest(character: str) -> str:
    return "sha256:" + character * 64


def _query(**overrides: Any) -> dict[str, Any]:
    query: dict[str, Any] = {
        "requestId": REQUEST_ID,
        "workspaceId": WORKSPACE_ID,
        "conversationId": CONVERSATION_ID,
        "actorId": PRINCIPAL,
        "deviceId": DEVICE_ID,
    }
    query.update(overrides)
    return query


def _context() -> OperationContext:
    return OperationContext(
        request=s0.envelope_for(
            ENTRY,
            operation_input={
                "conversation_id": CONVERSATION_ID,
                "snapshot_query": _query(),
            },
            request_id=REQUEST_ID,
            workspace_id=WORKSPACE_ID,
        ),
        principal=PRINCIPAL,
        workspace_id=WORKSPACE_ID,
        granted_operations=frozenset({OPERATION}),
    )


def _request(query: Mapping[str, Any] | None = None) -> ChatSnapshotInput:
    return ChatSnapshotInput(
        conversation_id=CONVERSATION_ID,
        snapshot_query=dict(_query() if query is None else query),
    )


# --- the rows one complete snapshot is composed from ---------------------------------


def _conversation(**overrides: Any) -> Conversation:
    fields: dict[str, Any] = {
        "workspace_id": WORKSPACE_ID,
        "conversation_id": CONVERSATION_ID,
        "title": "a titled conversation",
        "title_source": "user",
        "state": "active",
        "default_branch_id": BRANCH_ID,
        "graph_revision": 7,
        "latest_conversation_sequence": 2,
        "schema_version": 1,
        "created_by_actor_id": PRINCIPAL,
        "created_at_us": BASE_US,
        "updated_at_us": BASE_US + 10,
        "archived_at_us": BASE_US + 20,
        "tombstoned_at_us": BASE_US + 30,
    }
    fields.update(overrides)
    return Conversation(**fields)


def _message(message_id: str, sequence: int, **overrides: Any) -> Message:
    fields: dict[str, Any] = {
        "workspace_id": WORKSPACE_ID,
        "conversation_id": CONVERSATION_ID,
        "message_id": message_id,
        "parent_message_id": None,
        "role": "user",
        "author_type": "human",
        "author_id": PRINCIPAL,
        "conversation_sequence": sequence,
        "schema_version": 1,
        "content_hash": _digest("a"),
        "completion_status": "complete",
        "visibility": "standard",
        "created_on_branch_id": BRANCH_ID,
        "generation_job_id": None,
        "created_at_us": BASE_US + 1_000_000,
        "committed_at_us": BASE_US + 1_500_000,
        "tombstoned_at_us": None,
    }
    fields.update(overrides)
    return Message(**fields)


def _part(message_id: str, index: int, **overrides: Any) -> MessagePart:
    fields: dict[str, Any] = {
        "workspace_id": WORKSPACE_ID,
        "conversation_id": CONVERSATION_ID,
        "message_id": message_id,
        "part_id": f"{message_id}-part-{index}",
        "part_index": index,
        "part_type": "text",
        "schema_version": 1,
        "visibility": "standard",
        "payload": {"text": f"part {index}"},
        "provenance": "human",
        "content_hash": _digest("b"),
        "created_at_us": BASE_US + 1_000_000,
    }
    fields.update(overrides)
    return MessagePart(**fields)


def _branch(**overrides: Any) -> Branch:
    fields: dict[str, Any] = {
        "workspace_id": WORKSPACE_ID,
        "conversation_id": CONVERSATION_ID,
        "branch_id": BRANCH_ID,
        "origin_kind": "original",
        "created_from_branch_id": None,
        "fork_parent_message_id": None,
        "fork_source_message_id": None,
        "initial_head_message_id": ROOT_MESSAGE_ID,
        "current_head_message_id": HEAD_MESSAGE_ID,
        "created_by_actor_id": PRINCIPAL,
        "created_at_us": BASE_US,
        "created_conversation_sequence": 1,
        "head_version": 2,
        "schema_version": 1,
        # 0029's own branch vocabulary, which has no `active`: an open branch is
        # `open`. A row can never hold anything else, so neither can this fixture.
        "state": "open",
        "archived_at_us": None,
        "tombstoned_at_us": None,
    }
    fields.update(overrides)
    return Branch(**fields)


def _view_state(**overrides: Any) -> ViewState:
    fields: dict[str, Any] = {
        "workspace_id": WORKSPACE_ID,
        "conversation_id": CONVERSATION_ID,
        "actor_id": PRINCIPAL,
        "device_id": DEVICE_ID,
        "active_branch_id": BRANCH_ID,
        "focused_message_id": HEAD_MESSAGE_ID,
        "last_seen_graph_revision": 7,
        "schema_version": 1,
        "version": 3,
        "updated_at_us": BASE_US + 2_000_000,
    }
    fields.update(overrides)
    return ViewState(**fields)


def _inputs(**overrides: Any) -> ConversationSnapshotInputs:
    root = _message(ROOT_MESSAGE_ID, 1)
    head = _message(
        HEAD_MESSAGE_ID,
        2,
        parent_message_id=ROOT_MESSAGE_ID,
        role="assistant",
        author_type="provider",
        author_id=None,
        created_on_branch_id=None,
        content_hash=_digest("c"),
        tombstoned_at_us=BASE_US + 3_000_000,
        created_at_us=BASE_US + 2_000_000,
        committed_at_us=BASE_US + 2_250_000,
    )
    fields: dict[str, Any] = {
        "conversation": _conversation(),
        "branch": _branch(),
        "view_state": _view_state(),
        "path": (root, head),
        "parts_by_message_id": {
            ROOT_MESSAGE_ID: (_part(ROOT_MESSAGE_ID, 0), _part(ROOT_MESSAGE_ID, 1)),
            HEAD_MESSAGE_ID: (_part(HEAD_MESSAGE_ID, 0, provenance=None),),
        },
        "generation_job_ids": ("gen-snapshot-1", "gen-snapshot-2"),
        "generation_job_ids_truncated": False,
        "queued_submissions": (),
        "queued_submissions_truncated": False,
    }
    fields.update(overrides)
    return ConversationSnapshotInputs(**fields)


class _Reader:
    """The one authoritative read, recorded rather than performed."""

    def __init__(self, result: ConversationSnapshotInputs | None) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []

    def __call__(self, connection: Any, **kwargs: Any) -> Any:
        self.calls.append({"connection": connection, **kwargs})
        return self.result


JOB_1 = "gen-snapshot-1"
JOB_2 = "gen-snapshot-2"
ATTEMPT_1 = "attempt-snapshot-1"
ATTEMPT_2 = "attempt-snapshot-2"
OTHER_ACTOR = "actor-snapshot-other"
JOB_OVERFLOW = (
    "the branch's generation jobs exceed the bounded snapshot maximum and cannot be "
    "answered completely"
)
DIRECTION_OVERFLOW = (
    "the actor's directions exceed the bounded snapshot maximum and cannot be "
    "answered completely"
)
INTERNAL = "the conversation snapshot could not be projected from consistent durable state"


def _job(job_id: str, *, created_us: int = BASE_US + 10_000_000, **overrides: Any) -> GenerationJob:
    fields: dict[str, Any] = {
        "workspace_id": WORKSPACE_ID,
        "conversation_id": CONVERSATION_ID,
        "branch_id": BRANCH_ID,
        "trigger_message_id": ROOT_MESSAGE_ID,
        "generation_job_id": job_id,
        "state": "queued",
        "graph_revision_observed": 7,
        "idempotency_key": f"key-{job_id}",
        "current_attempt_id": None,
        "result_message_id": None,
        "lease_owner": None,
        "lease_epoch": 0,
        "lease_expires_at_us": None,
        "heartbeat_at_us": None,
        "last_event_sequence": 0,
        "sanitized_error_code": None,
        "sanitized_error_detail": None,
        "schema_version": 1,
        "created_at_us": created_us,
        "updated_at_us": created_us,
        "started_at_us": None,
        "finished_at_us": None,
    }
    fields.update(overrides)
    return GenerationJob(**fields)


def _status(
    job_id: str, *, state: str = "queued", version: int = 1, **overrides: Any
) -> GenerationJobStatusProjection:
    fields: dict[str, Any] = {
        "workspace_id": WORKSPACE_ID,
        "conversation_id": CONVERSATION_ID,
        "generation_job_id": job_id,
        "state": state,
        "current_attempt_id": None,
        "result_message_id": None,
        "sanitized_error_code": None,
        "sanitized_error_detail": None,
        "version": version,
        "schema_version": 1,
        "updated_at_us": BASE_US + 11_500_000,
        "finished_at_us": None,
    }
    fields.update(overrides)
    return GenerationJobStatusProjection(**fields)


def _text_parts(text: str) -> list[dict[str, Any]]:
    return [{"payload": {"text": text}, "type": "text"}]


def _direction_row(
    direction_id: str,
    *,
    job_id: str = JOB_1,
    sequence: int = 1,
    state: str = "pending",
    actor_id: str = PRINCIPAL,
    text: str = "Focus on evidence.",
    **overrides: Any,
) -> Direction:
    parts = _text_parts(text)
    fields: dict[str, Any] = {
        "workspace_id": WORKSPACE_ID,
        "conversation_id": CONVERSATION_ID,
        "branch_id": BRANCH_ID,
        "generation_job_id": job_id,
        "target_attempt_id": ATTEMPT_1,
        "direction_id": direction_id,
        "direction_sequence": sequence,
        "actor_id": actor_id,
        "state": state,
        "version": 1 if state == "pending" else 2,
        "content": {"editableParts": parts},
        "content_digest": "sha256:" + direction_content_hash(parts),
        "requested_boundary": "earliest_safe",
        "effective_boundary": None,
        "supersedes_direction_id": None,
        "superseded_by_direction_id": None,
        "applied_attempt_id": None,
        "applied_provider_invocation_id": None,
        "applied_request_manifest_id": None,
        "applied_iteration": None,
        "terminal_reason": None,
        "schema_version": 1,
        "created_at_us": BASE_US + 30_000_000,
        "updated_at_us": BASE_US + 30_000_000 if state == "pending" else BASE_US + 40_000_000,
        "terminal_at_us": None if state == "pending" else BASE_US + 40_000_000,
    }
    fields.update(overrides)
    return Direction(**fields)


class _Store:
    """The branch's job rows and the actor's Direction rows, served in memory.

    Models the real readers' scoping (workspace, conversation, branch, actor) so that
    what a test sees excluded is excluded by the resolver's own call, and records every
    job it was asked to read so a test can prove a job was never touched.
    """

    def __init__(self, jobs: Mapping[str, tuple[GenerationJob | None, Any]] | None = None) -> None:
        self.jobs: dict[str, tuple[GenerationJob | None, Any]] = dict(
            {
                JOB_1: (
                    _job(
                        JOB_1,
                        state="succeeded",
                        current_attempt_id=ATTEMPT_1,
                        result_message_id=HEAD_MESSAGE_ID,
                    ),
                    _status(
                        JOB_1,
                        state="succeeded",
                        version=4,
                        current_attempt_id=ATTEMPT_1,
                        result_message_id=HEAD_MESSAGE_ID,
                        finished_at_us=BASE_US + 11_500_000,
                    ),
                ),
                JOB_2: (
                    _job(JOB_2, state="running", created_us=BASE_US + 20_000_000),
                    _status(JOB_2, state="running", version=2, current_attempt_id=ATTEMPT_2),
                ),
            }
            if jobs is None
            else jobs
        )
        self.listed: list[str] | None = None
        self.directions: list[Direction] = []
        self.unscoped_directions = False
        self.direction_error: Exception | None = None
        self.job_calls: list[dict[str, Any]] = []
        self.id_calls: list[dict[str, Any]] = []
        self.direction_calls: list[dict[str, Any]] = []
        self.reads: list[str] = []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def ids(connection: Any, **kwargs: Any) -> tuple[str, ...]:
            self.id_calls.append(kwargs)
            if self.listed is not None:  # a listing a test states verbatim
                return tuple(self.listed[: kwargs["limit"]])
            # Otherwise the real reader's own scoping: only jobs the branch owns.
            owned = [
                job_id
                for job_id, (job, _) in self.jobs.items()
                if job is None or job.branch_id == kwargs["branch_id"]
            ]
            return tuple(owned[: kwargs["limit"]])

        def job(connection: Any, *, workspace_id: str, generation_job_id: str) -> Any:
            self.reads.append(generation_job_id)
            return self.jobs.get(generation_job_id, (None, None))[0]

        def status(connection: Any, *, workspace_id: str, generation_job_id: str) -> Any:
            return self.jobs.get(generation_job_id, (None, None))[1]

        def directions(connection: Any, **kwargs: Any) -> tuple[Direction, ...]:
            self.direction_calls.append(kwargs)
            if self.direction_error is not None:
                raise self.direction_error
            if self.unscoped_directions:
                return tuple(self.directions)
            return tuple(
                row
                for row in self.directions
                if (row.workspace_id, row.conversation_id, row.branch_id, row.actor_id)
                == (
                    kwargs["workspace_id"],
                    kwargs["conversation_id"],
                    kwargs["branch_id"],
                    kwargs["actor_id"],
                )
            )

        monkeypatch.setattr(chat_snapshot, "read_branch_generation_job_ids", ids)
        monkeypatch.setattr(chat_snapshot, "read_generation_job", job)
        monkeypatch.setattr(chat_snapshot, "read_generation_job_status_projection", status)
        monkeypatch.setattr(chat_snapshot, "read_actor_directions_for_branch", directions)


def _resolve(
    monkeypatch: pytest.MonkeyPatch,
    reader: _Reader,
    request: ChatSnapshotInput | None = None,
    store: _Store | None = None,
) -> Mapping[str, Any]:
    monkeypatch.setattr(chat_snapshot, "read_conversation_snapshot_inputs", reader)
    (_Store() if store is None else store).install(monkeypatch)
    connection = sqlite3.connect(":memory:")
    try:
        return chat_snapshot.resolve_chat_snapshot(
            connection,
            _request() if request is None else request,
            _context(),
        )
    finally:
        connection.close()


# --- 1: the complete projection -------------------------------------------------------


def test_a_complete_snapshot_projects_the_whole_governed_document(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reader = _Reader(_inputs())

    wire = _resolve(monkeypatch, reader)

    # The read happened exactly once, bound to the envelope's own facts.
    assert len(reader.calls) == 1
    assert isinstance(reader.calls[0].pop("connection"), sqlite3.Connection)
    assert reader.calls == [
        {
            "workspace_id": WORKSPACE_ID,
            "conversation_id": CONVERSATION_ID,
            "actor_id": PRINCIPAL,
            "device_id": DEVICE_ID,
        }
    ]
    assert set(wire) == {"conversation_id", "snapshot"}
    assert wire["conversation_id"] == CONVERSATION_ID

    snapshot = wire["snapshot"]
    assert set(snapshot) == {
        "conversation",
        "path",
        "branch",
        "viewState",
        "generationJobs",
        "directions",
    }
    assert snapshot["conversation"] == {
        "workspaceId": WORKSPACE_ID,
        "conversationId": CONVERSATION_ID,
        "createdBy": PRINCIPAL,
        "createdAt": "2052-05-22T14:13:20Z",
        "state": "active",
        "graphRevision": 7,
        "latestConversationSequence": 2,
        "schemaVersion": 1,
        "title": "a titled conversation",
        "defaultBranchId": BRANCH_ID,
        "archivedAt": "2052-05-22T14:13:20Z",
        "tombstonedAt": "2052-05-22T14:13:20Z",
    }

    path = snapshot["path"]
    assert path["branchId"] == BRANCH_ID
    assert path["headMessageId"] == HEAD_MESSAGE_ID
    assert path["graphRevision"] == 7
    assert path["viewStateVersion"] == 3
    assert path["generationJobIds"] == ["gen-snapshot-1", "gen-snapshot-2"]
    # Sibling position is a graph fact this read does not load, so it is absent.
    assert "divergenceMetadataByMessageId" not in path

    root, head = path["messages"]
    assert [message["messageId"] for message in (root, head)] == [
        ROOT_MESSAGE_ID,
        HEAD_MESSAGE_ID,
    ]
    # Optional members are absent, never null.
    assert "parentMessageId" not in root
    assert head["parentMessageId"] == ROOT_MESSAGE_ID
    assert "authorId" not in head
    assert "createdOnBranchId" not in head
    assert "tombstonedAt" not in root
    assert head["tombstonedAt"] == "2052-05-22T14:13:23Z"
    # The stored `sha256:` spelling is not the contract's.
    assert root["contentHash"] == "a" * 64
    assert head["contentHash"] == "c" * 64
    assert head["createdAt"] == "2052-05-22T14:13:22Z"
    assert head["committedAt"] == "2052-05-22T14:13:22.250Z"

    assert [part["index"] for part in root["parts"]] == [0, 1]
    assert root["parts"][0] == {
        "workspaceId": WORKSPACE_ID,
        "conversationId": CONVERSATION_ID,
        "messageId": ROOT_MESSAGE_ID,
        "partId": f"{ROOT_MESSAGE_ID}-part-0",
        "index": 0,
        "type": "text",
        "schemaVersion": 1,
        "visibility": "standard",
        "payload": {"text": "part 0"},
        "contentHash": "b" * 64,
        "createdAt": "2052-05-22T14:13:21Z",
        "provenance": "human",
    }
    assert "provenance" not in head["parts"][0]

    # The durable branch record the path was taken from, carrying the `headVersion`
    # the projection has nowhere to state. Optional members absent, never null.
    assert snapshot["branch"] == {
        "workspaceId": WORKSPACE_ID,
        "conversationId": CONVERSATION_ID,
        "branchId": BRANCH_ID,
        "originKind": "original",
        "initialHeadMessageId": ROOT_MESSAGE_ID,
        "currentHeadMessageId": HEAD_MESSAGE_ID,
        "createdBy": PRINCIPAL,
        "createdAt": "2052-05-22T14:13:20Z",
        "createdConversationSequence": 1,
        "headVersion": 2,
        "schemaVersion": 1,
        "state": "open",
    }
    # One branch identity, not two: the record and the path agree by construction.
    assert snapshot["branch"]["branchId"] == path["branchId"]
    assert snapshot["branch"]["currentHeadMessageId"] == path["headMessageId"]

    assert snapshot["viewState"] == {
        "workspaceId": WORKSPACE_ID,
        "conversationId": CONVERSATION_ID,
        "actorId": PRINCIPAL,
        "activeBranchId": BRANCH_ID,
        "lastSeenGraphRevision": 7,
        "schemaVersion": 1,
        "updatedAt": "2052-05-22T14:13:22Z",
        "version": 3,
        "deviceId": DEVICE_ID,
        "focusedMessageId": HEAD_MESSAGE_ID,
    }


def test_a_complete_snapshot_conforms_to_the_frozen_contract_schemas(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The query this resolver decoded, and the wire snapshot it emitted, are
    both governed documents: validated here against the exact canonical
    `$defs` -- not the resolver's own opinion of its output."""
    reader = _Reader(_inputs())
    query = _query()

    wire = _resolve(monkeypatch, reader, _request(query))

    assert (
        _schema_errors(f"{_QUERIES_SCHEMA_ID}#/$defs/ConversationSnapshotQuery", query) == []
    )
    assert (
        _schema_errors(
            f"{_QUERIES_SCHEMA_ID}#/$defs/ConversationSnapshotResult", wire["snapshot"]
        )
        == []
    )
    # And the branch member against its own frozen record, not only through the
    # result that carries it.
    assert (
        _schema_errors(
            f"{_BRANCH_SCHEMA_ID}#/$defs/MessageBranch", wire["snapshot"]["branch"]
        )
        == []
    )


def test_a_forked_and_archived_branch_carries_its_optional_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every optional `MessageBranch` member is emitted where the row holds one, and
    the result still conforms."""
    reader = _Reader(
        _inputs(
            branch=_branch(
                origin_kind="message_amendment",
                created_from_branch_id="branch-snapshot-origin",
                fork_parent_message_id=ROOT_MESSAGE_ID,
                fork_source_message_id=HEAD_MESSAGE_ID,
                state="archived",
                archived_at_us=BASE_US + 4_000_000,
                tombstoned_at_us=BASE_US + 5_000_000,
            )
        )
    )

    branch = _resolve(monkeypatch, reader)["snapshot"]["branch"]

    assert branch["originKind"] == "message_amendment"
    assert branch["createdFromBranchId"] == "branch-snapshot-origin"
    assert branch["forkParentMessageId"] == ROOT_MESSAGE_ID
    assert branch["forkSourceMessageId"] == HEAD_MESSAGE_ID
    assert branch["state"] == "archived"
    assert branch["archivedAt"] == "2052-05-22T14:13:24Z"
    assert branch["tombstonedAt"] == "2052-05-22T14:13:25Z"
    assert _schema_errors(f"{_BRANCH_SCHEMA_ID}#/$defs/MessageBranch", branch) == []


def test_a_branch_job_set_past_the_bound_refuses_the_whole_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`generationJobIds` is complete or it is nothing: a controller proving a
    candidate job against a shortened list would read "absent" for a job that
    exists, so the read that could not be answered completely is refused entirely.
    """
    reader = _Reader(_inputs(generation_job_ids_truncated=True))

    with pytest.raises(OperationError) as raised:
        _resolve(monkeypatch, reader)

    assert raised.value.code == "size_limit_exceeded"
    assert raised.value.message == (
        "the branch's generation jobs exceed the bounded snapshot maximum and "
        "cannot be answered completely"
    )


def test_a_conversation_with_no_branch_yet_is_still_the_governed_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cold-start conversation exists but has no branch, no path and no view state
    (REF-042 §9.1). `ConversationSnapshotResult` requires all three, so this read
    stays the one governed refusal rather than inventing a branchless spelling the
    Chat Contract does not publish; the host's authoritative empty state is the
    `chat.command` result that created the conversation.
    """
    reader = _Reader(
        _inputs(
            conversation=_conversation(
                default_branch_id=None,
                graph_revision=1,
                latest_conversation_sequence=0,
                archived_at_us=None,
                tombstoned_at_us=None,
            ),
            branch=None,
            view_state=None,
            path=(),
            parts_by_message_id={},
            generation_job_ids=(),
        )
    )

    with pytest.raises(OperationError) as raised:
        _resolve(monkeypatch, reader)

    assert raised.value.code == "not_found"
    assert raised.value.message == (
        "no complete conversation snapshot is available for this request"
    )
    # Still one read: a refusal never costs a second trip to the authority.
    assert len(reader.calls) == 1


def test_an_actor_level_view_state_carries_no_device_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The stored empty device scope has no contract spelling: it is simply absent."""
    reader = _Reader(_inputs(view_state=_view_state(device_id="", focused_message_id=None)))
    query = _query()
    del query["deviceId"]

    wire = _resolve(monkeypatch, reader, _request(query))

    assert reader.calls[0]["device_id"] == ""
    assert "deviceId" not in wire["snapshot"]["viewState"]
    assert "focusedMessageId" not in wire["snapshot"]["viewState"]


# --- 2: strict refusals, before any read ---------------------------------------------

_SECRET = "secret-value-the-service-must-not-echo"


@pytest.mark.parametrize(
    ("query", "field"),
    (
        ({**_query(), "unexpectedField": _SECRET}, "unexpectedField"),
        ({key: value for key, value in _query().items() if key != "requestId"}, "requestId"),
        (
            {key: value for key, value in _query().items() if key != "workspaceId"},
            "workspaceId",
        ),
        (
            {key: value for key, value in _query().items() if key != "conversationId"},
            "conversationId",
        ),
        ({key: value for key, value in _query().items() if key != "actorId"}, "actorId"),
        ({**_query(), "requestId": 7}, "requestId"),
        ({**_query(), "workspaceId": None}, "workspaceId"),
        ({**_query(), "conversationId": ["not", "a", "string"]}, "conversationId"),
        ({**_query(), "actorId": ""}, "actorId"),
        ({**_query(), "deviceId": {"device": _SECRET}}, "deviceId"),
        ({**_query(), "actorId": "not an identifier"}, "actorId"),
        ({**_query(), "workspaceId": "ws-" + _SECRET}, "workspaceId"),
        ({**_query(), "conversationId": "conv-" + _SECRET}, "conversationId"),
        ({**_query(), "actorId": "actor-" + _SECRET}, "actorId"),
        ({**_query(), "requestId": "req-" + _SECRET}, "requestId"),
    ),
)
def test_a_query_this_contract_does_not_admit_refuses_without_reading(
    monkeypatch: pytest.MonkeyPatch, query: Mapping[str, Any], field: str
) -> None:
    reader = _Reader(_inputs())

    with pytest.raises(OperationError) as raised:
        _resolve(monkeypatch, reader, _request(query))

    assert raised.value.code == "invalid_request"
    assert repr(field) in raised.value.message
    assert _SECRET not in raised.value.message
    assert reader.calls == []


# --- 3: one bounded not_found -----------------------------------------------------------

_MISSING = (
    None,
    _inputs(branch=None),
    _inputs(view_state=None),
    _inputs(path=()),
    # A path whose last message is not the branch head is not this branch's path.
    _inputs(branch=_branch(current_head_message_id="msg-snapshot-elsewhere")),
)


@pytest.mark.parametrize("inputs", _MISSING, ids=range(len(_MISSING)))
def test_every_incomplete_snapshot_refuses_identically(
    monkeypatch: pytest.MonkeyPatch, inputs: ConversationSnapshotInputs | None
) -> None:
    reader = _Reader(inputs)

    with pytest.raises(OperationError) as raised:
        _resolve(monkeypatch, reader)

    assert raised.value.code == "not_found"
    # One constant diagnostic, so the refusal is never an oracle for which fact is missing.
    assert raised.value.message == (
        "no complete conversation snapshot is available for this request"
    )
    assert len(reader.calls) == 1


# --- 3b: C6 Generation Jobs and the actor's Directions ---------------------------------

_GENERATION_SCHEMA_ID = "https://contracts.omnivia.dev/chat/v1/generation.schema.json"
_RESULT_REF = f"{_QUERIES_SCHEMA_ID}#/$defs/ConversationSnapshotResult"


def _conforms(snapshot: Mapping[str, Any]) -> None:
    assert _schema_errors(_RESULT_REF, snapshot) == []
    for job in snapshot["generationJobs"]:
        assert _schema_errors(f"{_GENERATION_SCHEMA_ID}#/$defs/GenerationJob", job) == []
    for direction in snapshot["directions"]:
        validate_direction(direction)
        assert _schema_errors(f"{_GENERATION_SCHEMA_ID}#/$defs/Direction", direction) == []


def _snapshot(
    monkeypatch: pytest.MonkeyPatch,
    store: _Store | None = None,
    inputs: ConversationSnapshotInputs | None = None,
) -> Mapping[str, Any]:
    return _resolve(monkeypatch, _Reader(_inputs() if inputs is None else inputs), store=store)[
        "snapshot"
    ]


def _refusal(
    monkeypatch: pytest.MonkeyPatch, store: _Store, code: str, message: str
) -> OperationError:
    with pytest.raises(OperationError) as raised:
        _resolve(monkeypatch, _Reader(_inputs()), store=store)
    assert raised.value.code == code
    assert raised.value.message == message
    return raised.value


def test_an_empty_snapshot_still_states_both_c6_collections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = _snapshot(monkeypatch, _Store({}), _inputs(generation_job_ids=()))

    assert snapshot["generationJobs"] == []
    assert snapshot["directions"] == []
    assert snapshot["path"]["generationJobIds"] == []
    _conforms(snapshot)
    # The collections are required, so a document without them is not a snapshot.
    for missing in ("generationJobs", "directions"):
        assert _schema_errors(
            _RESULT_REF, {key: value for key, value in snapshot.items() if key != missing}
        )


_JOB_STATES = (
    ("queued", "queued", {}, {}),
    ("running", "running", {"current_attempt_id": ATTEMPT_1}, {}),
    (
        "succeeded",
        "succeeded",
        {"current_attempt_id": ATTEMPT_1, "result_message_id": HEAD_MESSAGE_ID},
        {"finished_at_us": BASE_US + 11_500_000},
    ),
    (
        "failed",
        "failed",
        {"current_attempt_id": ATTEMPT_1, "sanitized_error_code": "provider_failed"},
        {"finished_at_us": BASE_US + 11_500_000},
    ),
    (
        "cancelled",
        "cancelled",
        {"current_attempt_id": ATTEMPT_1, "sanitized_error_code": "cancelled"},
        {"finished_at_us": BASE_US + 11_500_000},
    ),
    # 0030's non-terminal `retryable`: the foundation row already failed, and the
    # public vocabulary has no internal-only state, so a caller sees a failed job.
    (
        "retryable",
        "failed",
        {"current_attempt_id": ATTEMPT_1, "sanitized_error_code": "provider_unavailable"},
        {},
    ),
)


@pytest.mark.parametrize(
    ("stored", "public", "status_fields", "finished"),
    _JOB_STATES,
    ids=[case[0] for case in _JOB_STATES],
)
def test_every_stored_job_state_projects_exact_fields_and_the_real_version(
    monkeypatch: pytest.MonkeyPatch,
    stored: str,
    public: str,
    status_fields: dict[str, Any],
    finished: dict[str, Any],
) -> None:
    foundation_state = "failed" if stored == "retryable" else stored
    store = _Store(
        {
            JOB_1: (
                _job(
                    JOB_1,
                    state=foundation_state,
                    current_attempt_id=status_fields.get("current_attempt_id"),
                    result_message_id=status_fields.get("result_message_id"),
                ),
                _status(JOB_1, state=stored, version=7, **status_fields, **finished),
            )
        }
    )

    snapshot = _snapshot(monkeypatch, store, _inputs(generation_job_ids=(JOB_1,)))

    expected: dict[str, Any] = {
        "workspaceId": WORKSPACE_ID,
        "conversationId": CONVERSATION_ID,
        "branchId": BRANCH_ID,
        "triggerMessageId": ROOT_MESSAGE_ID,
        "jobId": JOB_1,
        "state": public,
        "graphRevisionObserved": 7,
        # The projection's own token: never inferred, never the foundation row's.
        "version": 7,
        "schemaVersion": 1,
        "createdAt": "2052-05-22T14:13:30Z",
        "updatedAt": "2052-05-22T14:13:31.500Z",
    }
    if "current_attempt_id" in status_fields:
        expected["currentAttemptId"] = ATTEMPT_1
    if stored == "succeeded":
        expected["resultMessageId"] = HEAD_MESSAGE_ID
    assert snapshot["generationJobs"] == [expected]
    assert snapshot["path"]["generationJobIds"] == [JOB_1]
    _conforms(snapshot)


def test_jobs_are_ordered_by_created_at_then_job_id_and_the_path_names_the_same_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Listed out of order on purpose: the order is the resolver's, not the reader's.
    # `gen-a`, `gen-b` and `gen-z` share a millisecond, so the id decides among them.
    created = {
        "gen-z": BASE_US + 2_000_500,
        "gen-b": BASE_US + 2_000_000,
        "gen-c": BASE_US + 1_000_000,
        "gen-a": BASE_US + 2_000_999,
    }
    store = _Store(
        {
            job_id: (_job(job_id, created_us=at), _status(job_id))
            for job_id, at in created.items()
        }
    )

    snapshot = _snapshot(monkeypatch, store, _inputs(generation_job_ids=tuple(created)))

    ids = [job["jobId"] for job in snapshot["generationJobs"]]
    assert ids == ["gen-c", "gen-a", "gen-b", "gen-z"]
    assert snapshot["path"]["generationJobIds"] == ids
    assert set(ids) == set(snapshot["path"]["generationJobIds"])
    _conforms(snapshot)


def test_the_selected_branch_excludes_a_job_an_ancestor_message_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ancestor message carried over from the branch this one forked from names a
    job the selected branch does not own. The aggregate reader unions it in; the
    snapshot states only what the selected branch owns, and never reads the other."""
    ancestor = "gen-source-branch"
    base = _inputs()
    inputs = _inputs(
        path=(
            _message(ROOT_MESSAGE_ID, 1, generation_job_id=ancestor),
            base.path[1],
        ),
        generation_job_ids=(ancestor, JOB_1, JOB_2),
    )
    store = _Store()
    store.jobs[ancestor] = (
        _job(ancestor, branch_id="branch-snapshot-source"),
        _status(ancestor),
    )

    snapshot = _snapshot(monkeypatch, store, inputs)

    assert [job["jobId"] for job in snapshot["generationJobs"]] == [JOB_1, JOB_2]
    assert snapshot["path"]["generationJobIds"] == [JOB_1, JOB_2]
    assert ancestor not in store.reads
    assert store.id_calls[0]["branch_id"] == BRANCH_ID
    _conforms(snapshot)


_FOUNDATION_DEFECTS = {
    "no foundation row": lambda: (None, _status(JOB_1)),
    "no status projection": lambda: (_job(JOB_1), None),
    "foundation on another branch": lambda: (_job(JOB_1, branch_id="branch-else"), _status(JOB_1)),
    "foundation in another conversation": lambda: (
        _job(JOB_1, conversation_id="conv-else"),
        _status(JOB_1),
    ),
    "foundation for another job": lambda: (_job("gen-else"), _status(JOB_1)),
    "status in another conversation": lambda: (
        _job(JOB_1),
        _status(JOB_1, conversation_id="conv-else"),
    ),
    "status in another workspace": lambda: (
        _job(JOB_1),
        _status(JOB_1, workspace_id="ws-else"),
    ),
    "status for another job": lambda: (_job(JOB_1), _status("gen-else")),
    "an invalid status state": lambda: (_job(JOB_1), _status(JOB_1, state="paused")),
    "succeeded status over a running foundation row": lambda: (
        _job(JOB_1, state="running"),
        _status(JOB_1, state="succeeded", result_message_id=HEAD_MESSAGE_ID),
    ),
    "succeeded status without a result message": lambda: (
        _job(JOB_1, state="failed"),
        _status(JOB_1, state="succeeded"),
    ),
    "succeeded status over a cancelled foundation row": lambda: (
        _job(JOB_1, state="cancelled"),
        _status(JOB_1, state="succeeded", result_message_id=HEAD_MESSAGE_ID),
    ),
    "succeeded status over a failed foundation row holding a result": lambda: (
        _job(JOB_1, state="failed", result_message_id=HEAD_MESSAGE_ID),
        _status(JOB_1, state="succeeded", result_message_id=HEAD_MESSAGE_ID),
    ),
    "succeeded status with another result message": lambda: (
        _job(
            JOB_1,
            state="succeeded",
            current_attempt_id=ATTEMPT_1,
            result_message_id="msg-else",
        ),
        _status(
            JOB_1,
            state="succeeded",
            current_attempt_id=ATTEMPT_1,
            result_message_id=HEAD_MESSAGE_ID,
        ),
    ),
    "succeeded status re-naming the attempt of a failed foundation row": lambda: (
        _job(JOB_1, state="failed", current_attempt_id=ATTEMPT_1),
        _status(
            JOB_1,
            state="succeeded",
            current_attempt_id=ATTEMPT_1,
            result_message_id=HEAD_MESSAGE_ID,
        ),
    ),
    "succeeded status naming another attempt over a succeeded foundation row": lambda: (
        _job(
            JOB_1,
            state="succeeded",
            current_attempt_id=ATTEMPT_1,
            result_message_id=HEAD_MESSAGE_ID,
        ),
        _status(
            JOB_1,
            state="succeeded",
            current_attempt_id=ATTEMPT_2,
            result_message_id=HEAD_MESSAGE_ID,
        ),
    ),
}


@pytest.mark.parametrize("defect", sorted(_FOUNDATION_DEFECTS))
def test_missing_or_inconsistent_job_authority_fails_closed_with_one_constant(
    monkeypatch: pytest.MonkeyPatch, defect: str
) -> None:
    store = _Store({JOB_1: _FOUNDATION_DEFECTS[defect]()})
    store.listed = [JOB_1]

    error = _refusal(monkeypatch, store, "internal_non_recoverable", INTERNAL)

    assert JOB_1 not in error.message and "else" not in error.message
    assert error.__cause__ is None


def test_a_branch_with_more_than_the_bounded_jobs_fails_completely(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ids = [f"gen-bulk-{index:03d}" for index in range(201)]
    store = _Store(
        {
            job_id: (_job(job_id, created_us=BASE_US + index * 1000), _status(job_id))
            for index, job_id in enumerate(ids)
        }
    )

    _refusal(monkeypatch, store, "size_limit_exceeded", JOB_OVERFLOW)

    # Read one past the bound to know; never truncated, and no job row was projected.
    assert [call["limit"] for call in store.id_calls] == [201]
    assert store.reads == []


def test_exactly_the_bounded_number_of_jobs_is_a_complete_conforming_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ids = [f"gen-bulk-{index:03d}" for index in range(200)]
    store = _Store(
        {
            job_id: (_job(job_id, created_us=BASE_US + index * 1000), _status(job_id))
            for index, job_id in enumerate(ids)
        }
    )

    snapshot = _snapshot(monkeypatch, store, _inputs(generation_job_ids=tuple(ids)))

    assert [job["jobId"] for job in snapshot["generationJobs"]] == ids
    assert snapshot["path"]["generationJobIds"] == ids
    _conforms(snapshot)


def _shape_store() -> _Store:
    """Five Directions across the two jobs, in every state, plus ones that must not appear."""
    store = _Store()
    store.directions = [
        # Deliberately not in order.
        _direction_row("dir-applied", job_id=JOB_2, sequence=1, state="applied",
                       effective_boundary="next_iteration",
                       applied_attempt_id=ATTEMPT_2,
                       applied_provider_invocation_id="invocation-2",
                       applied_request_manifest_id="manifest-2",
                       applied_iteration=3, text="applied é"),
        _direction_row("dir-rejected", job_id=JOB_1, sequence=3, state="rejected",
                       terminal_reason={"code": "boundary_unavailable", "safeMessage": "no boundary"}),
        _direction_row("dir-pending", job_id=JOB_2, sequence=2, text="pending"),
        _direction_row("dir-superseded", job_id=JOB_1, sequence=1, state="superseded",
                       superseded_by_direction_id="dir-cancelled",
                       terminal_reason={"code": "superseded"}),
        _direction_row("dir-cancelled", job_id=JOB_1, sequence=2, state="cancelled",
                       supersedes_direction_id="dir-superseded",
                       terminal_reason={"code": "job_terminal"}),
        # Someone else's, another branch's, and another conversation's.
        _direction_row("dir-other-actor", job_id=JOB_1, sequence=4, actor_id=OTHER_ACTOR),
        _direction_row("dir-other-branch", job_id=JOB_1, sequence=5, branch_id="branch-else"),
        _direction_row("dir-other-conversation", job_id=JOB_1, sequence=6, conversation_id="conv-else"),
    ]
    return store


def test_the_actor_sees_only_their_own_directions_in_every_state_in_contract_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _shape_store()

    snapshot = _snapshot(monkeypatch, store)

    directions = snapshot["directions"]
    # jobId, then jobSequence, then directionId.
    assert [(d["jobId"], d["jobSequence"], d["directionId"]) for d in directions] == [
        (JOB_1, 1, "dir-superseded"),
        (JOB_1, 2, "dir-cancelled"),
        (JOB_1, 3, "dir-rejected"),
        (JOB_2, 1, "dir-applied"),
        (JOB_2, 2, "dir-pending"),
    ]
    assert {d["actorId"] for d in directions} == {PRINCIPAL}
    assert store.direction_calls == [
        {
            "workspace_id": WORKSPACE_ID,
            "conversation_id": CONVERSATION_ID,
            "branch_id": BRANCH_ID,
            "actor_id": PRINCIPAL,
        }
    ]
    _conforms(snapshot)


def test_each_direction_state_has_exactly_its_own_shape_and_the_bare_content_hash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    by_id = {d["directionId"]: d for d in _snapshot(monkeypatch, _shape_store())["directions"]}
    common = {
        "workspaceId": WORKSPACE_ID,
        "conversationId": CONVERSATION_ID,
        "branchId": BRANCH_ID,
        "actorId": PRINCIPAL,
        "targetAttemptId": ATTEMPT_1,
        "requestedBoundary": "earliest_safe",
        "schemaVersion": 1,
        "createdAt": "2052-05-22T14:13:50Z",
    }

    def content(text: str) -> dict[str, Any]:
        return {
            "editableParts": _text_parts(text),
            "contentHash": direction_content_hash(_text_parts(text)),
        }

    # Pending: no application, supersession, effective boundary or terminal reason --
    # absent, not null.
    assert by_id["dir-pending"] == {
        **common, **content("pending"),
        "directionId": "dir-pending", "jobId": JOB_2, "jobSequence": 2,
        "state": "pending", "version": 1, "updatedAt": "2052-05-22T14:13:50Z",
    }
    assert by_id["dir-applied"] == {
        **common, **content("applied é"),
        "directionId": "dir-applied", "jobId": JOB_2, "jobSequence": 1,
        "state": "applied", "version": 2, "updatedAt": "2052-05-22T14:14:00Z",
        "effectiveBoundary": "next_iteration",
        "application": {
            "attemptId": ATTEMPT_2,
            "providerInvocationId": "invocation-2",
            "requestManifestId": "manifest-2",
            "iteration": 3,
            "appliedAt": "2052-05-22T14:14:00Z",
        },
    }
    assert by_id["dir-superseded"] == {
        **common, **content("Focus on evidence."),
        "directionId": "dir-superseded", "jobId": JOB_1, "jobSequence": 1,
        "state": "superseded", "version": 2, "updatedAt": "2052-05-22T14:14:00Z",
        "terminalReason": {"code": "superseded", "occurredAt": "2052-05-22T14:14:00Z"},
        "supersession": {
            "supersededByDirectionId": "dir-cancelled",
            "supersededAt": "2052-05-22T14:14:00Z",
        },
    }
    assert by_id["dir-rejected"] == {
        **common, **content("Focus on evidence."),
        "directionId": "dir-rejected", "jobId": JOB_1, "jobSequence": 3,
        "state": "rejected", "version": 2, "updatedAt": "2052-05-22T14:14:00Z",
        "terminalReason": {
            "code": "boundary_unavailable",
            "occurredAt": "2052-05-22T14:14:00Z",
            "safeMessage": "no boundary",
        },
    }
    assert by_id["dir-cancelled"] == {
        **common, **content("Focus on evidence."),
        "directionId": "dir-cancelled", "jobId": JOB_1, "jobSequence": 2,
        "state": "cancelled", "version": 2, "updatedAt": "2052-05-22T14:14:00Z",
        "supersedesDirectionId": "dir-superseded",
        "terminalReason": {"code": "job_terminal", "occurredAt": "2052-05-22T14:14:00Z"},
    }
    # The stored `sha256:` prefix is a storage spelling; the public hash is bare hex.
    for direction in by_id.values():
        assert len(direction["contentHash"]) == 64
        assert not direction["contentHash"].startswith("sha256:")


def test_a_direction_count_past_the_bound_fails_the_whole_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _Store()
    store.directions = [
        _direction_row(f"dir-bulk-{index:03d}", sequence=index + 1) for index in range(201)
    ]

    _refusal(monkeypatch, store, "size_limit_exceeded", DIRECTION_OVERFLOW)


_DIRECTION_DEFECTS = {
    "orphan job": lambda: _direction_row("dir-x", job_id="gen-not-listed"),
    "job on another branch": lambda: _direction_row("dir-x", job_id="gen-source-branch"),
    "scope mismatch: workspace": lambda: _direction_row("dir-x", workspace_id="ws-else"),
    "scope mismatch: conversation": lambda: _direction_row("dir-x", conversation_id="conv-else"),
    "scope mismatch: branch": lambda: _direction_row("dir-x", branch_id="branch-else"),
    "scope mismatch: actor": lambda: _direction_row("dir-x", actor_id=OTHER_ACTOR),
    "content hash of other content": lambda: _direction_row(
        "dir-x", content_digest="sha256:" + "0" * 64
    ),
    "content digest without the storage prefix": lambda: _direction_row(
        "dir-x", content_digest=direction_content_hash(_text_parts("Focus on evidence."))
    ),
    "content that is not editable parts": lambda: _direction_row(
        "dir-x", content={"editableParts": "secret-text"}
    ),
    "content with a non-text part": lambda: _direction_row(
        "dir-x", content={"editableParts": [{"type": "image", "payload": {"text": "secret"}}]}
    ),
    "unknown state": lambda: _direction_row("dir-x", state="paused"),
    "terminal reason with an unknown code": lambda: _direction_row(
        "dir-x", state="rejected", terminal_reason={"code": "direction.closed"}
    ),
    "terminal reason of another state": lambda: _direction_row(
        "dir-x", state="rejected", terminal_reason={"code": "job_terminal"}
    ),
    "terminal reason with an extra key": lambda: _direction_row(
        "dir-x", state="rejected", terminal_reason={"code": "policy_refused", "detail": "secret"}
    ),
    "terminal state without a reason": lambda: _direction_row("dir-x", state="cancelled"),
    "applied without evidence": lambda: _direction_row(
        "dir-x", state="applied", effective_boundary="next_iteration"
    ),
    "applied by its own target attempt": lambda: _direction_row(
        "dir-x", state="applied", effective_boundary="next_iteration",
        applied_attempt_id=ATTEMPT_1, applied_provider_invocation_id="invocation-2",
        applied_request_manifest_id="manifest-2", applied_iteration=1,
    ),
    "superseded without its successor": lambda: _direction_row(
        "dir-x", state="superseded", terminal_reason={"code": "superseded"}
    ),
}


@pytest.mark.parametrize("defect", sorted(_DIRECTION_DEFECTS))
def test_an_orphan_mismatched_or_tampered_direction_fails_closed_with_one_constant(
    monkeypatch: pytest.MonkeyPatch, defect: str
) -> None:
    store = _Store()
    store.unscoped_directions = True  # a reader (or its caller) that let a stray row through
    store.directions = [_direction_row("dir-ok"), _DIRECTION_DEFECTS[defect]()]
    store.jobs["gen-source-branch"] = (_job("gen-source-branch", branch_id="branch-else"), _status("gen-source-branch"))

    error = _refusal(monkeypatch, store, "internal_non_recoverable", INTERNAL)

    for leaked in ("secret", "dir-x", "gen-not-listed", "0" * 64, "else", "closed"):
        assert leaked not in error.message
    assert error.__cause__ is None


def test_a_storage_defect_in_the_direction_reader_is_the_same_constant_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _Store()
    store.direction_error = StorageError(
        "a stored chat direction content digest does not match its content: "
        + "sha256:" + "f" * 64
        + " SELECT secret FROM omnivia_chat_directions"
    )

    error = _refusal(monkeypatch, store, "internal_non_recoverable", INTERNAL)

    assert error.__cause__ is None
    for leaked in ("f" * 64, "SELECT", "omnivia_chat_directions", "digest"):
        assert leaked not in error.message


# --- 3c: the same projection over a real, migrated database ---------------------------


@pytest.fixture
def owned(tmp_path: Path) -> Iterator[m1.Owned]:
    path = tmp_path / "workspace.sqlite"
    materialise_phase0_baseline(path)
    m1.bootstrap_and_migrate(path, workspace_id=WORKSPACE_ID)
    holder = m1.take_ownership(path)
    yield holder
    holder.connection.close()


#: The migration fixtures seed their own draft with a non-array `references_json`, which
#: the aggregate reader (rightly) refuses. The actor reads this conversation from a
#: second device instead: its own view state, and no draft.
DB_DEVICE_ID = "device-snapshot-db"


def _db_view(holder: m1.Owned) -> None:
    with repo.writer(holder) as w:
        w.insert_view_state(
            conversation_id=foundation.CONVERSATION_ID,
            actor_id=foundation.ACTOR_ID,
            device_id=DB_DEVICE_ID,
            active_branch_id=foundation.BRANCH_ID,
            last_seen_graph_revision=2,
            schema_version=1,
            updated_at_us=repo.BASE_US + 80,
        )


def _db_snapshot(connection: sqlite3.Connection) -> Mapping[str, Any]:
    """`chat.snapshot` for the migration fixtures' conversation, as its own author."""
    conversation_id = foundation.CONVERSATION_ID
    query = {
        "requestId": REQUEST_ID,
        "workspaceId": WORKSPACE_ID,
        "conversationId": conversation_id,
        "actorId": foundation.ACTOR_ID,
        "deviceId": DB_DEVICE_ID,
    }
    context = OperationContext(
        request=s0.envelope_for(
            ENTRY,
            operation_input={"conversation_id": conversation_id, "snapshot_query": query},
            request_id=REQUEST_ID,
            workspace_id=WORKSPACE_ID,
        ),
        principal=foundation.ACTOR_ID,
        workspace_id=WORKSPACE_ID,
        granted_operations=frozenset({OPERATION}),
    )
    request = ChatSnapshotInput(conversation_id=conversation_id, snapshot_query=query)
    return chat_snapshot.resolve_chat_snapshot(connection, request, context)["snapshot"]


def _instant(microseconds: int) -> str:
    moment = datetime.fromtimestamp(microseconds / 1_000_000, tz=UTC)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_a_real_database_projects_the_branchs_jobs_and_only_the_actors_directions(
    owned: m1.Owned,
) -> None:
    dm.seed_job(owned)
    _db_view(owned)
    with repo.writer(owned) as w:
        repo.append_direction(w, 1)
        w.update_direction(
            direction_id="direction-1", expected_version=1, state="superseded",
            terminal_at_us=repo.BASE_US + 400, terminal_reason={"code": "superseded"},
            superseded_by_direction_id="direction-2",
        )
        repo.append_direction(w, 2, supersedes_direction_id="direction-1")
        repo.append_direction(
            w, 1, direction_id="direction-other", generation_job_id=dm.OTHER_JOB_ID,
            target_attempt_id=dm.OTHER_ATTEMPT_1, actor_id=repo.OTHER_ACTOR_ID,
        )

    snapshot = _db_snapshot(owned.connection)

    jobs = snapshot["generationJobs"]
    assert [(job["jobId"], job["state"], job["version"]) for job in jobs] == [
        (dm.JOB_ID, "running", 1),
        (dm.OTHER_JOB_ID, "running", 1),
    ]
    assert snapshot["path"]["generationJobIds"] == [dm.JOB_ID, dm.OTHER_JOB_ID]
    assert [(d["directionId"], d["state"], d["version"]) for d in snapshot["directions"]] == [
        ("direction-1", "superseded", 2),
        ("direction-2", "pending", 1),
    ]
    assert snapshot["directions"][0]["supersession"] == {
        "supersededByDirectionId": "direction-2",
        "supersededAt": _instant(repo.BASE_US + 400),
    }
    assert "supersession" not in snapshot["directions"][1]
    assert snapshot["directions"][1]["supersedesDirectionId"] == "direction-1"
    _conforms(snapshot)
    assert owned.connection.in_transaction is False


def test_a_real_retry_success_projects_over_a_foundation_row_frozen_failed(
    owned: m1.Owned,
) -> None:
    """0029 never reopens a terminal job: after `RetryGeneration` the foundation row
    stays `failed` from attempt 1 while the 0030 projection runs attempt 2 to success.
    The projection is the live authority, so the snapshot states the succeeded job."""
    dm.seed_job(owned)
    _db_view(owned)
    with foundation.guarded(owned):
        foundation.insert(
            owned,
            foundation.MESSAGES,
            foundation.message_row(
                foundation.ASSISTANT_MESSAGE_ID,
                4,
                role="assistant",
                parent_message_id=foundation.TRIGGER_MESSAGE_ID,
                generation_job_id=dm.JOB_ID,
            ),
        )
        owned.connection.execute(
            f"UPDATE {foundation.JOBS} SET state = 'failed', "
            "sanitized_error_code = 'provider_failed', last_event_sequence = 3, "
            "updated_at_us = ?, finished_at_us = ? "
            "WHERE workspace_id = ? AND generation_job_id = ?",
            (repo.BASE_US + 230, repo.BASE_US + 230, WORKSPACE_ID, dm.JOB_ID),
        )
    dm.move_status(owned, "retryable", dm.ATTEMPT_1, version=2, sanitized_error_code="rate_limited")
    dm.move_status(owned, "running", dm.ATTEMPT_2, version=3, sanitized_error_code=None)
    dm.move_status(
        owned,
        "succeeded",
        dm.ATTEMPT_2,
        version=4,
        sanitized_error_code=None,
        result_message_id=foundation.ASSISTANT_MESSAGE_ID,
        finished_at_us=repo.BASE_US + 260,
        updated_at_us=repo.BASE_US + 260,
    )
    frozen = owned.connection.execute(
        f"SELECT state, result_message_id FROM {foundation.JOBS} "
        "WHERE workspace_id = ? AND generation_job_id = ?",
        (WORKSPACE_ID, dm.JOB_ID),
    ).fetchone()
    assert tuple(frozen) == ("failed", None)

    snapshot = _db_snapshot(owned.connection)

    job = next(job for job in snapshot["generationJobs"] if job["jobId"] == dm.JOB_ID)
    assert (job["state"], job["version"], job["currentAttemptId"]) == (
        "succeeded",
        4,
        dm.ATTEMPT_2,
    )
    assert job["resultMessageId"] == foundation.ASSISTANT_MESSAGE_ID
    assert job["updatedAt"] == _instant(repo.BASE_US + 260)
    _conforms(snapshot)
    assert owned.connection.in_transaction is False


def test_a_real_applied_direction_carries_its_complete_application_evidence(
    owned: m1.Owned,
) -> None:
    repo.seed_direction(owned)
    _db_view(owned)
    repo.apply_direction(owned)

    (direction,) = _db_snapshot(owned.connection)["directions"]

    assert direction["state"] == "applied"
    assert direction["effectiveBoundary"] == "next_iteration"
    assert direction["application"] == {
        "attemptId": dm.ATTEMPT_2,
        "providerInvocationId": dm.INVOCATIONS[dm.ATTEMPT_2],
        "requestManifestId": dm.MANIFEST_IDS[dm.ATTEMPT_2],
        "iteration": 1,
        "appliedAt": _instant(repo.BASE_US + 400),
    }
    assert direction["targetAttemptId"] == dm.ATTEMPT_1
    assert "terminalReason" not in direction and "supersession" not in direction
    assert direction["contentHash"] == dm.CONTENT_DIGEST.removeprefix("sha256:")
    assert direction["editableParts"] == repo.direction_content()["editableParts"]
    validate_direction(direction)


def test_real_rejected_and_cancelled_directions_carry_their_stored_reason_and_instant(
    owned: m1.Owned,
) -> None:
    repo.seed_direction(owned)
    _db_view(owned)
    with repo.writer(owned) as w:
        w.update_direction(
            direction_id="direction-1", expected_version=1, state="rejected",
            terminal_at_us=repo.BASE_US + 400,
            terminal_reason={"code": "policy_refused", "safeMessage": "Refused by policy."},
        )
        repo.append_direction(w, 2)
        w.update_direction(
            direction_id="direction-2", expected_version=1, state="cancelled",
            terminal_at_us=repo.BASE_US + 500, terminal_reason={"code": "generation_stopped"},
        )

    rejected, cancelled = _db_snapshot(owned.connection)["directions"]

    assert rejected["terminalReason"] == {
        "code": "policy_refused",
        "occurredAt": _instant(repo.BASE_US + 400),
        "safeMessage": "Refused by policy.",
    }
    assert cancelled["terminalReason"] == {
        "code": "generation_stopped",
        "occurredAt": _instant(repo.BASE_US + 500),
    }
    for direction in (rejected, cancelled):
        assert not {"application", "supersession", "effectiveBoundary"} & set(direction)


_TAMPERS = (
    f"content_digest = '{dm.OTHER_DIGEST}'",
    "content_json = '{\"editableParts\": []}'",
    "content_json = 'not-json-at-all'",
    "content_json = '[]'",
)


@pytest.mark.parametrize("update", _TAMPERS)
def test_tampered_direction_storage_is_the_constant_refusal_and_rolls_back(
    owned: m1.Owned, update: str
) -> None:
    repo.seed_direction(owned)
    _db_view(owned)
    connection = repo.tamper(
        owned,
        repo.TAMPER_TRIGGER,
        f"UPDATE omnivia_chat_directions SET {update} {repo.TAMPER_WHERE}",
    )
    try:
        with pytest.raises(OperationError) as raised:
            _db_snapshot(connection)

        assert raised.value.code == "internal_non_recoverable"
        assert raised.value.message == INTERNAL
        assert raised.value.__cause__ is None
        for leaked in (dm.OTHER_DIGEST.removeprefix("sha256:"), "not-json", "direction-1", "x"):
            assert leaked not in raised.value.message
        assert connection.in_transaction is False
    finally:
        connection.close()


def test_a_noncanonical_stored_terminal_reason_is_the_constant_refusal(
    owned: m1.Owned,
) -> None:
    repo.seed_direction(owned)
    _db_view(owned)
    with repo.writer(owned) as w:
        w.update_direction(
            direction_id="direction-1", expected_version=1, state="rejected",
            terminal_at_us=repo.BASE_US + 400, terminal_reason={"code": "policy_refused"},
        )
    connection = repo.tamper(
        owned,
        repo.TAMPER_TRIGGER,
        "UPDATE omnivia_chat_directions SET terminal_reason_json = '{\"code\": \"policy_refused\"}' "
        + repo.TAMPER_WHERE,
    )
    try:
        with pytest.raises(OperationError) as raised:
            _db_snapshot(connection)

        assert (raised.value.code, raised.value.message) == ("internal_non_recoverable", INTERNAL)
        assert "policy_refused" not in raised.value.message
        assert connection.in_transaction is False
    finally:
        connection.close()


def test_the_read_transaction_is_opened_and_closed_only_when_the_resolver_owns_it(
    owned: m1.Owned,
) -> None:
    repo.seed_direction(owned)
    _db_view(owned)
    control = ("BEGIN", "COMMIT", "ROLLBACK")

    def run(connection: sqlite3.Connection) -> tuple[list[str], list[str]]:
        statements: list[str] = []
        connection.set_trace_callback(statements.append)
        try:
            _db_snapshot(connection)
        finally:
            connection.set_trace_callback(None)
        return (
            [statement for statement in statements if statement in control],
            statements,
        )

    # Standalone: one explicit read transaction, committed once -- and every job and
    # Direction read happens inside it, not after the aggregate reader's own has ended.
    controls, statements = run(owned.connection)
    assert controls == ["BEGIN", "COMMIT"]
    begin, commit = statements.index("BEGIN"), statements.index("COMMIT")
    late = [
        index
        for index, statement in enumerate(statements)
        if "omnivia_chat_generation_job_status_projection" in statement
        or "omnivia_chat_directions" in statement
    ]
    assert late and all(begin < index < commit for index in late)
    assert owned.connection.in_transaction is False

    # The caller's own: preserved, never committed or rolled back here.
    owned.connection.execute("BEGIN")
    try:
        assert run(owned.connection)[0] == []
        assert owned.connection.in_transaction is True
    finally:
        owned.connection.execute("ROLLBACK")


def test_a_failed_snapshot_ends_only_a_transaction_the_resolver_opened(
    owned: m1.Owned, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo.seed_direction(owned)
    _db_view(owned)

    def broken(*args: Any, **kwargs: Any) -> Any:
        raise StorageError("forced direction read failure")

    monkeypatch.setattr(chat_snapshot, "read_actor_directions_for_branch", broken)

    with pytest.raises(OperationError):
        _db_snapshot(owned.connection)
    assert owned.connection.in_transaction is False

    owned.connection.execute("BEGIN")
    try:
        with pytest.raises(OperationError):
            _db_snapshot(owned.connection)
        assert owned.connection.in_transaction is True
    finally:
        owned.connection.execute("ROLLBACK")


def _concurrent_status_bump(path: Path, job_id: str) -> None:
    """Another connection advances one job's status projection under the reader.

    The runtime's own connection holds the workspace exclusively, so the reader here is
    a plain connection to the same WAL database (as `tamper` uses), and the writer is
    another. The guard trigger is dropped because only the owning service may write.
    """
    other = sqlite3.connect(str(path), timeout=0, isolation_level=None)
    try:
        other.execute("DROP TRIGGER omnivia_guard_chat_generation_job_status_projection_update")
        other.execute(
            "UPDATE omnivia_chat_generation_job_status_projection SET version = 99 "
            "WHERE workspace_id = ? AND generation_job_id = ?",
            (WORKSPACE_ID, job_id),
        )
    finally:
        other.close()


def test_a_concurrent_write_between_job_reads_cannot_produce_a_mixed_snapshot(
    owned: m1.Owned, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Between the first job's status read and the second's, another connection
    commits a new version of the second job. The snapshot was taken at one moment, so
    it states both jobs as they were; only a later snapshot sees the write.
    """
    dm.seed_job(owned)
    _db_view(owned)
    owned.connection.close()
    reader = sqlite3.connect(str(owned.path))
    real = chat_snapshot.read_generation_job_status_projection
    writes: list[str] = []

    def racing(connection: sqlite3.Connection, **kwargs: Any) -> Any:
        row = real(connection, **kwargs)
        if not writes:
            _concurrent_status_bump(owned.path, dm.OTHER_JOB_ID)
            writes.append(kwargs["generation_job_id"])
        return row

    try:
        monkeypatch.setattr(chat_snapshot, "read_generation_job_status_projection", racing)
        snapshot = _db_snapshot(reader)

        assert writes == [dm.JOB_ID]  # the write landed after job one and before job two
        assert [job["version"] for job in snapshot["generationJobs"]] == [1, 1]
        _conforms(snapshot)
        assert reader.in_transaction is False

        # The write was real: the next snapshot, a new moment, sees it.
        monkeypatch.setattr(chat_snapshot, "read_generation_job_status_projection", real)
        later = _db_snapshot(reader)
        assert [job["version"] for job in later["generationJobs"]] == [1, 99]
    finally:
        reader.close()


# --- 4: the handler, wired into the production Chat family ---------------------------

INSTALLATION_ID = "inst-chat-snapshot"


class _FakeService:
    """Just what `ChatHandlers._authority()` reads: a connection and an identity."""

    def __init__(self, connection: Any = None, identity: Any = None) -> None:
        self.connection = connection
        self.identity = identity


def _handlers(service: Any) -> ChatHandlers:
    session = chat_family_session(
        principal_id=PRINCIPAL,
        installation_id=INSTALLATION_ID,
        workspace_id=WORKSPACE_ID,
    )
    binding = ServiceBinding(installation_id=INSTALLATION_ID, workspace_id=WORKSPACE_ID)
    return ChatHandlers(
        service=service,
        session=session,
        binding=binding,
        clock=SystemClock(),
        allocate_identifier=random_identifier,
    )


def test_the_handler_passes_the_decoded_request_context_and_connection_to_the_resolver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[Any, ChatSnapshotInput, OperationContext]] = []
    sentinel_wire: Mapping[str, Any] = {"conversation_id": CONVERSATION_ID, "snapshot": {}}

    def _stub(
        connection: Any, request: ChatSnapshotInput, context: OperationContext
    ) -> Mapping[str, Any]:
        calls.append((connection, request, context))
        return sentinel_wire

    monkeypatch.setattr(chat_handler_module, "resolve_chat_snapshot", _stub)
    monkeypatch.setattr(chat_handler_module, "read_guard", lambda connection: object())

    connection = object()
    context = _context()

    wire = _handlers(_FakeService(connection, object())).chat_snapshot(context)

    assert wire is sentinel_wire
    assert len(calls) == 1
    called_connection, called_request, called_context = calls[0]
    assert called_connection is connection
    assert called_context is context
    assert isinstance(called_request, ChatSnapshotInput)
    assert called_request.conversation_id == CONVERSATION_ID
    assert called_request.snapshot_query == _query()


def test_a_malformed_request_is_the_constant_chat_family_diagnostic_and_never_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    def _stub(*args: Any, **kwargs: Any) -> Mapping[str, Any]:
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(chat_handler_module, "resolve_chat_snapshot", _stub)
    monkeypatch.setattr(chat_handler_module, "read_guard", lambda connection: object())

    context = OperationContext(
        request=s0.envelope_for(
            ENTRY,
            # Missing the required `snapshot_query` field: not a valid `ChatSnapshotInput`.
            operation_input={"conversation_id": CONVERSATION_ID},
            request_id=REQUEST_ID,
            workspace_id=WORKSPACE_ID,
        ),
        principal=PRINCIPAL,
        workspace_id=WORKSPACE_ID,
        granted_operations=frozenset({OPERATION}),
    )

    with pytest.raises(OperationError) as raised:
        _handlers(_FakeService(object(), object())).chat_snapshot(context)

    assert raised.value.code == "invalid_request"
    assert raised.value.message == chat_handler_module._MESSAGE_INVALID
    assert called is False


def test_no_authoritative_storage_refuses_before_the_resolver_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    def _stub(*args: Any, **kwargs: Any) -> Mapping[str, Any]:
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(chat_handler_module, "resolve_chat_snapshot", _stub)

    with pytest.raises(OperationError) as raised:
        _handlers(_FakeService()).chat_snapshot(_context())

    assert raised.value.code == "internal_non_recoverable"
    assert called is False


def test_the_resolvers_own_operation_error_reaches_the_caller_unweakened(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = OperationError("not_found", "no complete conversation snapshot is available")

    def _stub(*args: Any, **kwargs: Any) -> Mapping[str, Any]:
        raise original

    monkeypatch.setattr(chat_handler_module, "resolve_chat_snapshot", _stub)
    monkeypatch.setattr(chat_handler_module, "read_guard", lambda connection: object())

    with pytest.raises(OperationError) as raised:
        _handlers(_FakeService(object(), object())).chat_snapshot(_context())

    assert raised.value is original
    assert raised.value.code == "not_found"
    assert raised.value.message == original.message


def test_chat_snapshot_is_registered_as_a_chat_family_observation_operation() -> None:
    assert CHAT_SNAPSHOT_OPERATION in CHAT_FAMILY_OPERATIONS
    assert CHAT_FAMILY_PURPOSES[CHAT_SNAPSHOT_OPERATION] == CHAT_OBSERVATION_PURPOSE


def test_chat_family_session_grants_chat_snapshot_under_the_observation_purpose() -> None:
    session = chat_family_session(
        principal_id=PRINCIPAL,
        installation_id=INSTALLATION_ID,
        workspace_id=WORKSPACE_ID,
    )

    assert CHAT_SNAPSHOT_OPERATION in session.operations
    assert "chat:read" in session.scopes
    assert CHAT_OBSERVATION_PURPOSE in session.purposes


def test_build_chat_registry_routes_chat_snapshot_to_the_bound_handler() -> None:
    handlers = _handlers(_FakeService())

    registry = build_chat_registry(handlers)

    handler = registry.get(CHAT_SNAPSHOT_OPERATION)
    assert handler is not None
    assert getattr(handler, "__self__", None) is handlers
    assert getattr(handler, "__func__", None) is ChatHandlers.chat_snapshot
