"""`chat.query`: the closed Chat Contract v1 list reads, over a real migrated workspace.

Three queries -- `BranchListQuery`, `DescendantsQuery`, `ConversationListQuery` -- served
by `service/chat_query.py` over `storage/chat.py`'s keyset reads. These tests run the
resolver against real SQLite rows rather than substituting storage, because what is
being proved is the SQL itself: ordering under ties, bounded pages, the accessible-
message rule and the absence of a transcript read from a branch list.
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
import test_chat_repository as repo
import test_v06_5_s0_mutation_foundation as s0
from omnivia_core_runtime.ownership.identity import FakeClock
from omnivia_core_runtime.service.application import (
    CHAT_FAMILY_PURPOSES,
    CHAT_OBSERVATION_PURPOSE,
    build_chat_application_dispatcher,
    build_chat_registry,
    chat_family_session,
)
from omnivia_core_runtime.service.authorization import Grant, ServiceBinding
from omnivia_core_runtime.service.chat_query import resolve_chat_query
from omnivia_core_runtime.service.dispatch import Dispatcher
from omnivia_core_runtime.service.handlers.chat import (
    CHAT_FAMILY_OPERATIONS,
    CHAT_QUERY_OPERATION,
    ChatHandlers,
)
from omnivia_core_runtime.service.operations import (
    SERVICE_OPERATIONS,
    OperationContext,
    OperationError,
)
from omnivia_core_runtime.service.pagination import PROCESS_CONTINUATION_TOKENS
from omnivia_core_runtime.storage.memory import random_identifier
from omnivia_core_runtime.storage.migrations import materialise_phase0_baseline

from omnivia_core.chat_contract.v1.codec import (
    CHAT_QUERY_NAMES,
    validate_chat_query,
    validate_chat_query_result,
)
from omnivia_core.contracts.v1 import get_operation_metadata
from omnivia_core.contracts.v1.generated import ChatQueryInput

OPERATION = "chat.query"
ENTRY = get_operation_metadata(OPERATION)
WORKSPACE_ID = m1.WORKSPACE_ID
PRINCIPAL = s0.PRINCIPAL
INSTALLATION_ID = "inst-chat-query"
REQUEST_ID = "req-chat-query-1"
BASE_US = 2_700_000_000_000_000

BRANCH_LIST = "BranchListQuery"
DESCENDANTS = "DescendantsQuery"
CONVERSATION_LIST = "ConversationListQuery"

C_ALPHA = "conv-q-alpha"
C_BETA = "conv-q-beta"
C_ARCHIVED = "conv-q-archived"
C_TOMBSTONED = "conv-q-tombstoned"
C_TIE_A = "conv-q-tie-a"
C_TIE_B = "conv-q-tie-b"
C_TIE_C = "conv-q-tie-c"


def _ts(microseconds: int) -> str:
    """The expected RFC 3339 instant, written independently of the implementation."""
    moment = datetime.fromtimestamp(microseconds / 1_000_000, tz=UTC)
    if moment.microsecond // 1000 == 0:
        return moment.strftime("%Y-%m-%dT%H:%M:%SZ")
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


# --- seeding ---------------------------------------------------------------------------


def _seed_conversation(
    holder: m1.Owned,
    conversation_id: str,
    *,
    title: str | None,
    updated_us: int,
    messages: tuple[tuple[Any, ...], ...] = (),
    branches: tuple[tuple[Any, ...], ...] = (),
    state: str = "active",
) -> None:
    """One conversation. `messages` rows are `(id, parent, role (unused), text, branch, visibility,
    tombstoned)`; `branches` rows are `(id, origin, initial_message_id, created_us)`."""
    with repo.writer(holder) as w:
        w.append_conversation(
            conversation_id=conversation_id,
            state=state,
            graph_revision=1,
            latest_conversation_sequence=0,
            schema_version=1,
            created_by_actor_id=PRINCIPAL,
            created_at_us=BASE_US,
            updated_at_us=updated_us if state != "active" else BASE_US,
            title=title,
            title_source="user" if title else None,
            archived_at_us=updated_us if state == "archived" else None,
            tombstoned_at_us=updated_us if state == "tombstoned" else None,
        )
        if state != "active":
            return
        w.update_conversation(
            conversation_id=conversation_id,
            expected_graph_revision=1,
            graph_revision=2,
            latest_conversation_sequence=len(messages),
            state="active",
            updated_at_us=updated_us,
            title=title,
            title_source="user" if title else None,
        )
        sequences: dict[str, int] = {}
        for sequence, row in enumerate(messages, 1):
            message_id, parent, _role, text, branch, visibility, tombstoned = row
            sequences[message_id] = sequence
            at = BASE_US + sequence * 10
            w.append_message(
                message_id=message_id,
                conversation_id=conversation_id,
                parent_message_id=parent,
                # Every seeded message is a human `user` one: an `assistant` message must
                # name a generation job, and a list read has no use for that machinery.
                role="user",
                author_type="human",
                author_id=PRINCIPAL,
                conversation_sequence=sequence,
                schema_version=1,
                content_hash=repo.digest("a"),
                completion_status="complete",
                visibility=visibility,
                created_on_branch_id=branch,
                created_at_us=at,
                committed_at_us=at,
                tombstoned_at_us=at + 1 if tombstoned else None,
            )
            w.append_message_part(
                part_id=f"{message_id}-p0",
                message_id=message_id,
                conversation_id=conversation_id,
                part_index=0,
                part_type="text",
                schema_version=1,
                visibility="standard",
                payload={"text": text},
                content_hash=repo.digest("b"),
                created_at_us=at,
                provenance="human",
            )
        for branch_id, origin, initial, created_us in branches:
            w.append_branch(
                branch_id=branch_id,
                conversation_id=conversation_id,
                origin_kind=origin,
                initial_head_message_id=initial,
                created_by_actor_id=PRINCIPAL,
                created_at_us=created_us,
                created_conversation_sequence=sequences[initial],
                schema_version=1,
            )
            w.append_branch_head_event(
                event_id=f"head-{branch_id}",
                conversation_id=conversation_id,
                branch_id=branch_id,
                head_version=1,
                previous_head_message_id=None,
                new_head_message_id=initial,
                cause="branch_created",
                command_id=f"command-{branch_id}",
                graph_revision=2,
                conversation_sequence=sequences[initial],
                actor_id=PRINCIPAL,
                occurred_at_us=created_us,
                schema_version=1,
            )


#: Alpha: one root with two amendment forks (main / amend), a sibling reply pair, an
#: internal child and a tombstoned child, and five branches -- three of them tied on
#: `created_at_us` so the tiebreaker is observable.
ALPHA_MESSAGES = (
    ("m-root", None, "user", "alpha budget review", "br-main", "standard", False),
    ("m-reply", "m-root", "assistant", "main reply", "br-main", "standard", False),
    ("m-amend", "m-root", "user", "alpha amended question", "br-amend", "standard", False),
    ("m-amend-reply", "m-amend", "assistant", "amended reply", "br-amend", "standard", False),
    ("m-amend-two", "m-amend", "assistant", "second reply", "br-amend", "standard", False),
    ("m-internal", "m-root", "assistant", "hiddenneedle", "br-main", "internal", False),
    ("m-hidden-child", "m-internal", "assistant", "childofinternal", "br-main", "standard", False),
    ("m-tomb", "m-root", "assistant", "tombneedle", "br-main", "standard", True),
    ("m-tomb-child", "m-tomb", "assistant", "childoftomb", "br-main", "standard", False),
)
ALPHA_BRANCHES = (
    ("br-main", "original", "m-root", BASE_US + 100),
    ("br-amend", "message_amendment", "m-amend", BASE_US + 200),
    ("br-tie-b", "explicit_fork", "m-reply", BASE_US + 300),
    ("br-tie-a", "explicit_fork", "m-reply", BASE_US + 300),
    ("br-tie-c", "explicit_fork", "m-reply", BASE_US + 300),
)


def _seed_all(holder: m1.Owned) -> None:
    _seed_conversation(
        holder, C_ALPHA, title="Alpha plan", updated_us=BASE_US + 5_000,
        messages=ALPHA_MESSAGES, branches=ALPHA_BRANCHES,
    )
    _seed_conversation(
        holder, C_BETA, title="Beta notes", updated_us=BASE_US + 4_000,
        messages=(
            ("b-root", None, "user", "gamma only", "br-b", "standard", False),
            ("b-secret", "b-root", "assistant", "secretalpha", "br-b", "internal", False),
        ),
        branches=(("br-b", "original", "b-root", BASE_US + 100),),
    )
    _seed_conversation(
        holder, C_ARCHIVED, title="Alpha archive", updated_us=BASE_US + 3_000,
        state="archived",
    )
    _seed_conversation(
        holder, C_TOMBSTONED, title="Alpha tombstoned", updated_us=BASE_US + 6_000,
        state="tombstoned",
    )
    for conversation_id in (C_TIE_A, C_TIE_B, C_TIE_C):
        _seed_conversation(
            holder, conversation_id, title=f"Tie {conversation_id[-1].upper()}", updated_us=BASE_US + 2_000
        )


@pytest.fixture
def owned(tmp_path: Path) -> Iterator[m1.Owned]:
    path = tmp_path / "workspace.sqlite"
    materialise_phase0_baseline(path)
    m1.bootstrap_and_migrate(path, workspace_id=WORKSPACE_ID)
    holder = m1.take_ownership(path)
    _seed_all(holder)
    yield holder
    holder.connection.close()


#: Branch lifecycle has no repository writer, so archive and tombstone are applied the
#: way `test_chat_repository.tamper` corrupts a row: from outside the runtime's guards.
_BRANCH_LIFECYCLE = (
    "DROP TRIGGER omnivia_guard_chat_message_branches_update",
    (
        "UPDATE omnivia_chat_message_branches SET state = 'archived', "
        f"archived_at_us = {BASE_US + 900} WHERE branch_id = 'br-tie-a'"
    ),
    (
        "UPDATE omnivia_chat_message_branches SET state = 'tombstoned', "
        f"tombstoned_at_us = {BASE_US + 900} WHERE branch_id = 'br-tie-c'"
    ),
)


@pytest.fixture
def db(owned: m1.Owned) -> Iterator[sqlite3.Connection]:
    connection = repo.tamper(owned, *_BRANCH_LIFECYCLE)
    yield connection
    connection.close()


# --- calling ---------------------------------------------------------------------------


def _context(
    name: str,
    query: Mapping[str, Any],
    *,
    principal: str = PRINCIPAL,
    workspace_id: str = WORKSPACE_ID,
    request_id: str = REQUEST_ID,
) -> OperationContext:
    return OperationContext(
        request=s0.envelope_for(
            ENTRY,
            operation_input={"query_name": name, "query": dict(query)},
            request_id=request_id,
            workspace_id=workspace_id,
        ),
        principal=principal,
        workspace_id=workspace_id,
        granted_operations=frozenset({OPERATION}),
    )


def _query(name: str, **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "requestId": REQUEST_ID,
        "workspaceId": WORKSPACE_ID,
        "actorId": PRINCIPAL,
        "pageSize": 50,
    }
    if name in (BRANCH_LIST, DESCENDANTS):
        base["conversationId"] = C_ALPHA
    if name == DESCENDANTS:
        base["fromMessageId"] = "m-root"
    base.update(overrides)
    return {key: value for key, value in base.items() if value is not None}


def _run(
    connection: sqlite3.Connection,
    name: str,
    query: Mapping[str, Any] | None = None,
    **context: Any,
) -> Mapping[str, Any]:
    document = _query(name) if query is None else query
    return resolve_chat_query(
        connection,
        ChatQueryInput(query_name=name, query=dict(document)),
        _context(name, document, **context),
    )


def _result(
    connection: sqlite3.Connection, name: str, query: Mapping[str, Any] | None = None, **context: Any
) -> dict[str, Any]:
    wire = _run(connection, name, query, **context)
    assert wire["query_name"] == name
    result = dict(wire["query_result"])
    validate_chat_query_result(name, result)
    return result


def _refusal(
    connection: sqlite3.Connection, name: str, query: Mapping[str, Any] | None = None, **context: Any
) -> OperationError:
    with pytest.raises(OperationError) as raised:
        _run(connection, name, query, **context)
    return raised.value


def _pages(
    connection: sqlite3.Connection, name: str, key: str, **overrides: Any
) -> list[list[Any]]:
    pages: list[list[Any]] = []
    cursor: str | None = None
    while True:
        result = _result(connection, name, _query(name, cursor=cursor, **overrides))
        pages.append(result[key])
        cursor = result.get("nextCursor")
        assert (cursor is not None) == result["hasMore"]
        if cursor is None:
            return pages


# --- the envelope and the closed catalogue ---------------------------------------------


_APPLICATION_CHAT_SCHEMA = (
    Path(__file__).resolve().parents[5]
    / "contracts" / "application" / "v1" / "schemas" / "chat.schema.json"
)


def test_the_envelope_enumeration_is_exactly_the_codecs_three_closed_names() -> None:
    definitions = json.loads(_APPLICATION_CHAT_SCHEMA.read_text(encoding="utf-8"))["$defs"]
    for envelope, field in (
        ("ChatQueryInput", "query_name"),
        ("ChatQueryResult", "query_name"),
    ):
        assert set(definitions[envelope]["properties"][field]["enum"]) == CHAT_QUERY_NAMES
    assert CHAT_QUERY_NAMES == {BRANCH_LIST, DESCENDANTS, CONVERSATION_LIST}
    assert definitions["ChatQueryInput"]["unevaluatedProperties"] is False
    assert definitions["ChatQueryInput"]["required"] == ["query_name", "query"]


@pytest.mark.parametrize(
    "refused", ["ComposerDraftQuery", "ConversationSnapshotQuery", "BranchPathQuery", "", "branchlistquery"]
)
def test_a_name_outside_the_closed_enumeration_is_refused_before_any_read(
    db: sqlite3.Connection, refused: str
) -> None:
    statements: list[str] = []
    db.set_trace_callback(statements.append)
    error = _refusal(db, refused, _query(BRANCH_LIST))
    db.set_trace_callback(None)
    assert error.code == "invalid_request"
    assert statements == []


_VALID = {
    BRANCH_LIST: {"requestId": "r", "workspaceId": "w", "actorId": "a", "conversationId": "c", "pageSize": 1},
    DESCENDANTS: {
        "requestId": "r", "workspaceId": "w", "actorId": "a", "conversationId": "c",
        "fromMessageId": "m", "pageSize": 200,
    },
    CONVERSATION_LIST: {
        "requestId": "r", "workspaceId": "w", "actorId": "a", "pageSize": 1,
        "state": "archived", "searchText": "x",
    },
}


def test_each_query_is_validated_against_its_exact_chat_contract_definition() -> None:
    for name, valid in _VALID.items():
        validate_chat_query(name, valid)
        for other, document in _VALID.items():
            if other != name:
                # Another query's document is not this query's: the definitions are closed.
                with pytest.raises(ValueError):
                    validate_chat_query(name, document)
    with pytest.raises(ValueError):
        validate_chat_query("SomeOtherQuery", _VALID[BRANCH_LIST])
    with pytest.raises(ValueError):
        validate_chat_query_result("SomeOtherQuery", {})


@pytest.mark.parametrize(
    ("name", "other"),
    [
        (BRANCH_LIST, DESCENDANTS),
        (BRANCH_LIST, CONVERSATION_LIST),
        (DESCENDANTS, BRANCH_LIST),
        (DESCENDANTS, CONVERSATION_LIST),
        (CONVERSATION_LIST, BRANCH_LIST),
        (CONVERSATION_LIST, DESCENDANTS),
    ],
)
def test_a_name_that_does_not_match_its_document_is_refused_before_any_read(
    db: sqlite3.Connection, name: str, other: str
) -> None:
    statements: list[str] = []
    db.set_trace_callback(statements.append)
    error = _refusal(db, name, _query(other))
    db.set_trace_callback(None)
    assert error.code == "invalid_request"
    assert statements == []


def test_the_operation_is_a_chat_observation_read_in_the_chat_family() -> None:
    assert ENTRY.scope.side_effect == "none"
    assert ENTRY.scope.required_scopes == ("chat:read",)
    assert ENTRY.scope.scope_kind == "workspace"
    assert ENTRY.pagination.paginated is False
    assert ENTRY.idempotency.required is False
    assert CHAT_QUERY_OPERATION == OPERATION
    assert OPERATION in CHAT_FAMILY_OPERATIONS
    assert CHAT_FAMILY_PURPOSES[OPERATION] == CHAT_OBSERVATION_PURPOSE
    session = chat_family_session(
        principal_id=PRINCIPAL, installation_id=INSTALLATION_ID, workspace_id=WORKSPACE_ID
    )
    assert OPERATION in session.operations
    assert "chat:read" in session.scopes
    assert CHAT_OBSERVATION_PURPOSE in session.purposes


def test_the_chat_registry_routes_the_operation_to_the_bound_query_handler() -> None:
    class _Service:
        connection = None
        identity = None

    session = chat_family_session(
        principal_id=PRINCIPAL, installation_id=INSTALLATION_ID, workspace_id=WORKSPACE_ID
    )
    handlers = ChatHandlers(
        service=_Service(),
        session=session,
        binding=ServiceBinding(installation_id=INSTALLATION_ID, workspace_id=WORKSPACE_ID),
        clock=FakeClock(wall=BASE_US // 1_000_000),
        allocate_identifier=random_identifier,
    )
    handler = build_chat_registry(handlers).get(OPERATION)
    assert handler is not None
    assert getattr(handler, "__self__", None) is handlers
    assert getattr(handler, "__func__", None) is ChatHandlers.chat_query


# --- trusted identity -------------------------------------------------------------------


@pytest.mark.parametrize("name", [BRANCH_LIST, DESCENDANTS, CONVERSATION_LIST])
def test_actor_workspace_and_request_mismatches_are_refused(
    db: sqlite3.Connection, name: str
) -> None:
    for field, value in (
        ("actorId", "actor-someone-else"),
        ("workspaceId", "ws-someone-else"),
        ("requestId", "req-not-this-one"),
    ):
        error = _refusal(db, name, _query(name, **{field: value}))
        assert error.code == "invalid_request"
        assert field in error.message
        assert value not in error.message


@pytest.mark.parametrize("name", [BRANCH_LIST, DESCENDANTS])
def test_an_unknown_tombstoned_or_unrelated_conversation_is_one_constant_not_found(
    db: sqlite3.Connection, name: str
) -> None:
    refusals = {
        _refusal(db, name, _query(name, conversationId=conversation_id)).message
        for conversation_id in ("conv-q-unknown", C_TOMBSTONED)
    }
    assert len(refusals) == 1
    error = _refusal(db, name, _query(name, conversationId="conv-q-unknown"))
    assert error.code == "not_found"


# --- BranchListQuery --------------------------------------------------------------------


def _branch(branch_id: str, origin: str, head: str, created_us: int, state: str = "open") -> dict[str, Any]:
    return {
        "branchId": branch_id,
        "originKind": origin,
        "state": state,
        "currentHeadMessageId": head,
        "headVersion": 1,
        "lastAdvancedAt": _ts(created_us),
    }


def test_a_branch_list_is_the_exact_wire_result_and_excludes_archived_and_tombstoned(
    db: sqlite3.Connection,
) -> None:
    wire = _run(db, BRANCH_LIST)
    assert wire == {
        "query_name": BRANCH_LIST,
        "query_result": {
            "branches": [
                _branch("br-main", "original", "m-root", BASE_US + 100),
                _branch("br-amend", "message_amendment", "m-amend", BASE_US + 200),
                _branch("br-tie-b", "explicit_fork", "m-reply", BASE_US + 300),
            ],
            "hasMore": False,
        },
    }


def test_include_archived_adds_the_archived_branch_but_never_a_tombstoned_one(
    db: sqlite3.Connection,
) -> None:
    result = _result(db, BRANCH_LIST, _query(BRANCH_LIST, includeArchived=True))
    assert [(row["branchId"], row["state"]) for row in result["branches"]] == [
        ("br-main", "open"),
        ("br-amend", "open"),
        ("br-tie-a", "archived"),
        ("br-tie-b", "open"),
    ]


def test_branch_pages_are_deterministic_under_ties_and_cover_every_row_once(
    db: sqlite3.Connection,
) -> None:
    pages = _pages(db, BRANCH_LIST, "branches", pageSize=1, includeArchived=True)
    assert [[row["branchId"] for row in page] for page in pages] == [
        ["br-main"], ["br-amend"], ["br-tie-a"], ["br-tie-b"],
    ]
    two = _pages(db, BRANCH_LIST, "branches", pageSize=2, includeArchived=True)
    assert [len(page) for page in two] == [2, 2]
    assert [row["branchId"] for page in two for row in page] == [
        "br-main", "br-amend", "br-tie-a", "br-tie-b",
    ]


def test_an_exactly_full_last_page_states_no_more(db: sqlite3.Connection) -> None:
    result = _result(db, BRANCH_LIST, _query(BRANCH_LIST, pageSize=3))
    assert len(result["branches"]) == 3
    assert result["hasMore"] is False
    assert "nextCursor" not in result


def test_a_conversation_with_no_branches_answers_an_empty_page(db: sqlite3.Connection) -> None:
    result = _result(db, BRANCH_LIST, _query(BRANCH_LIST, conversationId=C_TIE_A))
    assert result == {"branches": [], "hasMore": False}


def test_a_branch_list_never_reads_a_message_or_a_part(db: sqlite3.Connection) -> None:
    statements: list[str] = []
    db.set_trace_callback(statements.append)
    _result(db, BRANCH_LIST, _query(BRANCH_LIST, includeArchived=True))
    db.set_trace_callback(None)
    reads = " ".join(statements)
    assert "omnivia_chat_message_branches" in reads
    assert "omnivia_chat_messages" not in reads
    assert "omnivia_chat_message_parts" not in reads


def test_the_branch_state_vocabulary_is_the_contracts_own(db: sqlite3.Connection) -> None:
    result = _result(db, BRANCH_LIST, _query(BRANCH_LIST, includeArchived=True))
    assert {row["state"] for row in result["branches"]} <= {"open", "archived"}
    assert {row["originKind"] for row in result["branches"]} == {
        "original", "message_amendment", "explicit_fork",
    }


# --- DescendantsQuery -------------------------------------------------------------------


def _ids(result: Mapping[str, Any]) -> list[str]:
    return [message["messageId"] for message in result["messages"]]


def test_descendants_are_the_accessible_subtree_in_sequence_order_with_their_parts(
    db: sqlite3.Connection,
) -> None:
    result = _result(db, DESCENDANTS)
    # The tombstoned and internal children, and everything reachable only through
    # them, are not here; the strict subtree excludes the from-message itself.
    assert _ids(result) == ["m-reply", "m-amend", "m-amend-reply", "m-amend-two"]
    assert result["hasMore"] is False
    by_id = {message["messageId"]: message for message in result["messages"]}
    assert [part["payload"] for part in by_id["m-amend"]["parts"]] == [
        {"text": "alpha amended question"}
    ]
    assert by_id["m-amend"]["parentMessageId"] == "m-root"
    assert by_id["m-amend"]["conversationId"] == C_ALPHA


def test_descendants_page_lazily_at_a_divergence_point_without_cross_path_leakage(
    db: sqlite3.Connection,
) -> None:
    pages = _pages(db, DESCENDANTS, "messages", fromMessageId="m-amend", pageSize=1)
    assert [[message["messageId"] for message in page] for page in pages] == [
        ["m-amend-reply"], ["m-amend-two"],
    ]
    # The sibling path under m-reply is never part of m-amend's subtree.
    assert all(
        message["messageId"] not in {"m-reply", "m-root", "m-amend"}
        for page in pages
        for message in page
    )


def test_descendants_of_a_leaf_are_an_empty_page(db: sqlite3.Connection) -> None:
    result = _result(db, DESCENDANTS, _query(DESCENDANTS, fromMessageId="m-amend-two"))
    assert result == {"messages": [], "hasMore": False}


@pytest.mark.parametrize(
    "from_message",
    [
        "m-unknown",  # absent
        "m-internal",  # inaccessible itself
        "m-hidden-child",  # accessible, but its ancestor is internal
        "m-tomb",  # tombstoned
        "m-tomb-child",  # accessible, but its ancestor is tombstoned
        "b-root",  # another conversation's message
    ],
)
def test_an_inaccessible_or_unrelated_ancestor_fails_closed_with_one_constant_refusal(
    db: sqlite3.Connection, from_message: str
) -> None:
    error = _refusal(db, DESCENDANTS, _query(DESCENDANTS, fromMessageId=from_message))
    reference = _refusal(db, DESCENDANTS, _query(DESCENDANTS, fromMessageId="m-unknown"))
    assert error.code == "not_found"
    assert error.message == reference.message
    assert from_message not in error.message


def test_a_message_hidden_by_visibility_never_reaches_the_descendant_wire(
    db: sqlite3.Connection,
) -> None:
    wire = json.dumps(_run(db, DESCENDANTS)["query_result"])
    for hidden in ("hiddenneedle", "tombneedle", "childofinternal", "childoftomb"):
        assert hidden not in wire


# --- ConversationListQuery --------------------------------------------------------------


def _conversation(conversation_id: str, title: str, state: str = "active", **extra: Any) -> dict[str, Any]:
    return {"conversationId": conversation_id, "title": title, "state": state, "graphRevision": 2, **extra}


def test_a_conversation_list_is_the_exact_wire_result_active_only_by_default(
    db: sqlite3.Connection,
) -> None:
    wire = _run(db, CONVERSATION_LIST)
    assert wire == {
        "query_name": CONVERSATION_LIST,
        "query_result": {
            "conversations": [
                _conversation(C_ALPHA, "Alpha plan"),
                _conversation(C_BETA, "Beta notes"),
                _conversation(C_TIE_C, "Tie C"),
                _conversation(C_TIE_B, "Tie B"),
                _conversation(C_TIE_A, "Tie A"),
            ],
            "hasMore": False,
        },
    }


def test_the_archived_filter_lists_only_archived_and_a_tombstoned_list_is_empty(
    db: sqlite3.Connection,
) -> None:
    archived = _result(db, CONVERSATION_LIST, _query(CONVERSATION_LIST, state="archived"))
    assert archived["conversations"] == [
        {"conversationId": C_ARCHIVED, "title": "Alpha archive", "state": "archived", "graphRevision": 1}
    ]
    tombstoned = _result(db, CONVERSATION_LIST, _query(CONVERSATION_LIST, state="tombstoned"))
    assert tombstoned == {"conversations": [], "hasMore": False}


def test_conversation_pages_are_deterministic_under_ties(db: sqlite3.Connection) -> None:
    pages = _pages(db, CONVERSATION_LIST, "conversations", pageSize=2)
    assert [[row["conversationId"] for row in page] for page in pages] == [
        [C_ALPHA, C_BETA], [C_TIE_C, C_TIE_B], [C_TIE_A],
    ]
    again = _pages(db, CONVERSATION_LIST, "conversations", pageSize=2)
    assert again == pages


def test_a_title_is_bounded_to_the_summarys_own_maximum(owned: m1.Owned) -> None:
    long_title = "t" * 600
    _seed_conversation(owned, "conv-q-long", title=long_title, updated_us=BASE_US + 9_000)
    result = _result(owned.connection, CONVERSATION_LIST)
    assert result["conversations"][0]["conversationId"] == "conv-q-long"
    assert result["conversations"][0]["title"] == "t" * 512


def test_search_matches_accessible_message_text_and_names_the_exact_match(
    db: sqlite3.Connection,
) -> None:
    result = _result(db, CONVERSATION_LIST, _query(CONVERSATION_LIST, searchText="ALPHA"))
    # Alpha matches by title and message text; the newest accessible text match is the
    # amendment. Beta's only "alpha" is internal, so it is not a result at all.
    assert result["conversations"] == [
        _conversation(
            C_ALPHA, "Alpha plan",
            matchedMessageId="m-amend", matchedBranchId="br-amend",
        )
    ]
    assert result["hasMore"] is False


def test_a_title_only_match_names_no_message(db: sqlite3.Connection) -> None:
    result = _result(db, CONVERSATION_LIST, _query(CONVERSATION_LIST, searchText="beta"))
    assert result["conversations"] == [_conversation(C_BETA, "Beta notes")]


def test_search_is_a_literal_substring_never_a_pattern(db: sqlite3.Connection) -> None:
    for pattern in ("%", "_", "a%a", "alpha_", "\\"):
        result = _result(db, CONVERSATION_LIST, _query(CONVERSATION_LIST, searchText=pattern))
        assert result == {"conversations": [], "hasMore": False}, pattern


@pytest.mark.parametrize("needle", ["hiddenneedle", "tombneedle", "secretalpha"])
def test_search_never_matches_or_leaks_hidden_content(db: sqlite3.Connection, needle: str) -> None:
    result = _result(db, CONVERSATION_LIST, _query(CONVERSATION_LIST, searchText=needle))
    assert result == {"conversations": [], "hasMore": False}


def test_search_honours_the_archived_filter_and_never_returns_a_tombstoned_conversation(
    db: sqlite3.Connection,
) -> None:
    archived = _result(
        db, CONVERSATION_LIST, _query(CONVERSATION_LIST, searchText="alpha", state="archived")
    )
    assert [row["conversationId"] for row in archived["conversations"]] == [C_ARCHIVED]
    everything = json.dumps(_result(db, CONVERSATION_LIST, _query(CONVERSATION_LIST, searchText="alpha")))
    assert C_TOMBSTONED not in everything


def test_a_blank_search_is_no_search(db: sqlite3.Connection) -> None:
    blank = _result(db, CONVERSATION_LIST, _query(CONVERSATION_LIST, searchText="   "))
    assert blank == _result(db, CONVERSATION_LIST)


def test_the_active_branch_is_the_conversations_default_where_it_has_one(
    owned: m1.Owned,
) -> None:
    connection = repo.tamper(
        owned,
        "DROP TRIGGER omnivia_guard_chat_conversations_update",
        f"UPDATE omnivia_chat_conversations SET default_branch_id = 'br-main' "
        f"WHERE conversation_id = '{C_ALPHA}'",
    )
    try:
        rows = _result(connection, CONVERSATION_LIST)["conversations"]
    finally:
        connection.close()
    assert rows[0]["conversationId"] == C_ALPHA
    assert rows[0]["activeBranchId"] == "br-main"
    assert all("activeBranchId" not in row for row in rows[1:])


# --- bounds -----------------------------------------------------------------------------


@pytest.mark.parametrize("name", [BRANCH_LIST, DESCENDANTS, CONVERSATION_LIST])
def test_the_page_size_ceiling_is_the_contracts_own(db: sqlite3.Connection, name: str) -> None:
    assert _result(db, name, _query(name, pageSize=200))["hasMore"] is False
    for refused in (0, 201, -1, "50", True):
        assert _refusal(db, name, _query(name, pageSize=refused)).code == "invalid_request"


def test_one_extra_row_is_read_to_state_has_more_and_no_more(db: sqlite3.Connection) -> None:
    statements: list[str] = []
    db.set_trace_callback(statements.append)
    _result(db, CONVERSATION_LIST, _query(CONVERSATION_LIST, pageSize=2))
    db.set_trace_callback(None)
    assert any("LIMIT 3" in statement for statement in statements)


# --- cursors ----------------------------------------------------------------------------


def _first_cursor(connection: sqlite3.Connection, name: str, **overrides: Any) -> str:
    key = {BRANCH_LIST: "branches", DESCENDANTS: "messages", CONVERSATION_LIST: "conversations"}[name]
    result = _result(connection, name, _query(name, pageSize=1, **overrides))
    assert result["hasMore"] is True and len(result[key]) == 1
    return str(result["nextCursor"])


@pytest.mark.parametrize("name", [BRANCH_LIST, DESCENDANTS, CONVERSATION_LIST])
def test_a_cursor_continues_the_same_query_and_a_tampered_one_is_refused(
    db: sqlite3.Connection, name: str
) -> None:
    cursor = _first_cursor(db, name)
    assert _result(db, name, _query(name, pageSize=1, cursor=cursor))
    document, signature = cursor.split(".")
    flipped = ("A" if signature[0] != "A" else "B") + signature[1:]
    for refused in (
        f"{document}.{flipped}",
        f"{flipped}.{signature}",
        document,
        "not-a-cursor",
        cursor + ".",
        cursor[::-1],
    ):
        error = _refusal(db, name, _query(name, pageSize=1, cursor=refused))
        assert error.code == "invalid_request"
        assert refused not in error.message


@pytest.mark.parametrize("name", [BRANCH_LIST, DESCENDANTS, CONVERSATION_LIST])
def test_a_signed_cursor_of_another_version_or_shape_is_refused_as_stale(
    db: sqlite3.Connection, name: str
) -> None:
    good = PROCESS_CONTINUATION_TOKENS.decode(_first_cursor(db, name))
    for forged in (
        {**good, "v": 0},
        {**good, "v": 2},
        {**good, "a": []},
        {**good, "a": ["x", "y"]},
        {**good, "a": [True, "y"]},
        {**good, "extra": 1},
        {"a": good["a"], "v": 1},
    ):
        cursor = PROCESS_CONTINUATION_TOKENS.encode(forged)
        assert _refusal(db, name, _query(name, pageSize=1, cursor=cursor)).code == "invalid_request"


def test_a_cursor_is_bound_to_its_query_filter_and_search(db: sqlite3.Connection) -> None:
    branch_cursor = _first_cursor(db, BRANCH_LIST, includeArchived=True)
    descendants_cursor = _first_cursor(db, DESCENDANTS)
    conversation_cursor = _first_cursor(db, CONVERSATION_LIST)
    searched_cursor = _first_cursor(db, CONVERSATION_LIST, searchText="tie")

    cross_query = (
        (BRANCH_LIST, {}, descendants_cursor),
        (BRANCH_LIST, {}, conversation_cursor),
        (DESCENDANTS, {}, branch_cursor),
        (CONVERSATION_LIST, {}, branch_cursor),
        (CONVERSATION_LIST, {}, descendants_cursor),
    )
    cross_filter = (
        (BRANCH_LIST, {"includeArchived": False}, branch_cursor),
        (BRANCH_LIST, {"includeArchived": True, "conversationId": C_BETA}, branch_cursor),
        (DESCENDANTS, {"fromMessageId": "m-amend"}, descendants_cursor),
        (DESCENDANTS, {"conversationId": C_BETA, "fromMessageId": "b-root"}, descendants_cursor),
        (CONVERSATION_LIST, {"state": "archived"}, conversation_cursor),
        (CONVERSATION_LIST, {"searchText": "tie"}, conversation_cursor),
        (CONVERSATION_LIST, {"searchText": "TIE"}, searched_cursor),
        (CONVERSATION_LIST, {}, searched_cursor),
    )
    for name, overrides, cursor in (*cross_query, *cross_filter):
        error = _refusal(db, name, _query(name, pageSize=1, cursor=cursor, **overrides))
        assert error.code == "invalid_request", (name, overrides)


def test_a_cursor_is_bound_to_the_trusted_actor_and_workspace(db: sqlite3.Connection) -> None:
    cursor = _first_cursor(db, BRANCH_LIST)
    other = "actor-someone-else"
    error = _refusal(
        db,
        BRANCH_LIST,
        _query(BRANCH_LIST, pageSize=1, cursor=cursor, actorId=other),
        principal=other,
    )
    assert error.code == "invalid_request"


def test_a_cursor_is_not_an_offset(db: sqlite3.Connection) -> None:
    for offset in ("1", "2", "0", "3"):
        error = _refusal(db, BRANCH_LIST, _query(BRANCH_LIST, pageSize=1, cursor=offset))
        assert error.code == "invalid_request"


# --- the dispatcher: the wire a CLI or service caller sees --------------------------------


def _fallback() -> Dispatcher:
    return Dispatcher.for_service_operations(
        Grant(
            principal=PRINCIPAL,
            workspaces=frozenset({WORKSPACE_ID}),
            operations=frozenset(SERVICE_OPERATIONS),
        )
    )


def _dispatch(holder: m1.Owned, name: str, query: Mapping[str, Any]) -> Any:
    request_id = f"req-dispatch-{name}"
    dispatcher = build_chat_application_dispatcher(
        service=holder,
        principal_id=PRINCIPAL,
        installation_id=INSTALLATION_ID,
        workspace_id=WORKSPACE_ID,
        fallback=_fallback(),
        clock=FakeClock(wall=BASE_US // 1_000_000),
        execute_generation=lambda **_fields: None,
    )
    return dispatcher.dispatch(
        s0.envelope_for(
            ENTRY,
            operation_input={"query_name": name, "query": {**query, "requestId": request_id}},
            request_id=request_id,
            correlation_id=f"cor-{request_id}",
            trace_id=f"trc-{request_id}",
            purpose=CHAT_FAMILY_PURPOSES[OPERATION],
            workspace_id=WORKSPACE_ID,
        )
    )


def test_the_dispatcher_answers_each_query_with_its_exact_wire_result(owned: m1.Owned) -> None:
    branches = _dispatch(owned, BRANCH_LIST, _query(BRANCH_LIST, pageSize=2))
    assert branches.result["query_name"] == BRANCH_LIST
    assert branches.result["query_result"]["branches"] == [
        _branch("br-main", "original", "m-root", BASE_US + 100),
        _branch("br-amend", "message_amendment", "m-amend", BASE_US + 200),
    ]
    assert branches.result["query_result"]["hasMore"] is True

    descendants = _dispatch(owned, DESCENDANTS, _query(DESCENDANTS, fromMessageId="m-amend"))
    assert descendants.result["query_name"] == DESCENDANTS
    assert _ids(descendants.result["query_result"]) == ["m-amend-reply", "m-amend-two"]

    conversations = _dispatch(owned, CONVERSATION_LIST, _query(CONVERSATION_LIST, pageSize=2))
    assert conversations.result["query_name"] == CONVERSATION_LIST
    assert conversations.result["query_result"]["conversations"] == [
        _conversation(C_ALPHA, "Alpha plan"),
        _conversation(C_BETA, "Beta notes"),
    ]


def test_the_dispatcher_refuses_a_foreign_actor_without_reading(owned: m1.Owned) -> None:
    response = _dispatch(owned, BRANCH_LIST, _query(BRANCH_LIST, actorId="actor-someone-else"))
    assert response.error.code == "invalid_request"
