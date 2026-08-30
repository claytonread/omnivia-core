"""W5 GB: the production `chat.snapshot` resolver, `service/chat_snapshot.py`.

A pure read with one authoritative storage call, so these tests substitute that call
rather than a database: what is under test is the query decode, the single bound read,
and the projection -- not SQL that `test_chat_repository.py` already covers.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
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
    Message,
    MessagePart,
    ViewState,
)
from omnivia_core_runtime.storage.memory import random_identifier
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

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


def _resolve(
    monkeypatch: pytest.MonkeyPatch,
    reader: _Reader,
    request: ChatSnapshotInput | None = None,
) -> Mapping[str, Any]:
    monkeypatch.setattr(chat_snapshot, "read_conversation_snapshot_inputs", reader)
    return chat_snapshot.resolve_chat_snapshot(
        None,  # type: ignore[arg-type]
        _request() if request is None else request,
        _context(),
    )


# --- 1: the complete projection -------------------------------------------------------


def test_a_complete_snapshot_projects_the_whole_governed_document(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reader = _Reader(_inputs())

    wire = _resolve(monkeypatch, reader)

    # The read happened exactly once, bound to the envelope's own facts.
    assert reader.calls == [
        {
            "connection": None,
            "workspace_id": WORKSPACE_ID,
            "conversation_id": CONVERSATION_ID,
            "actor_id": PRINCIPAL,
            "device_id": DEVICE_ID,
        }
    ]
    assert set(wire) == {"conversation_id", "snapshot"}
    assert wire["conversation_id"] == CONVERSATION_ID

    snapshot = wire["snapshot"]
    assert set(snapshot) == {"conversation", "path", "branch", "viewState"}
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
