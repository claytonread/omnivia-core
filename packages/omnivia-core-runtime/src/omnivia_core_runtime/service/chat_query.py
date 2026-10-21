"""The production `chat.query` resolver: three closed Chat Contract v1 list reads.

`chat.query` serves exactly `BranchListQuery`, `DescendantsQuery` and
`ConversationListQuery`, the lists the hosted UI needs and `chat.snapshot` does not
carry. It is not a generic query surface: the Application envelope names the query in
a closed enumeration and carries its Chat Contract document opaquely, so everything
that decides a request lives here and is checked in this order, before any storage
read: the document against the *exact* Chat Contract definition the name selects
(`omnivia_core.chat_contract.v1.codec.validate_chat_query`), then the trusted facts
the envelope already decided -- workspace, authenticated principal and request id --
which a document may restate but never contradict. A name/document mismatch fails the
first check closed, because the three definitions are closed objects with different
required fields.

**Authority and visibility.** Core is the only reader. There is no per-conversation
ACL in the model, so the visibility rule is the one `chat.snapshot` enforces --
workspace scoping plus the authenticated actor -- narrowed by the durable lifecycle
facts: a tombstoned conversation, branch or message, and an `internal` message or
part, are not accessible. A conversation that is absent, tombstoned or in another
workspace, a `fromMessageId` that is absent, in another conversation, or whose
ancestry to the root includes an inaccessible message, all refuse identically
(`not_found`, one constant sentence), so the refusal is never an oracle and a partial
path is never assembled.

**Cursors.** Server-issued, signed (`service.pagination`), versioned, and bound to a
digest of the query name, trusted workspace and actor, and every filter and sort fact
the page was read under. The payload carries a keyset position, never an offset. A
token that is malformed, tampered, from another process, of another version, or
replayed under a different query, filter or search refuses as `invalid_request`.

**Bounded.** `pageSize` is the Chat Contract's own 1..200; one extra row is read to
state `hasMore`. Every read is parameterised and runs in one read transaction.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from typing import Any, Final

from omnivia_core.chat_contract.v1 import ChatContractDecodeError
from omnivia_core.chat_contract.v1.codec import (
    validate_chat_query,
    validate_chat_query_result,
)
from omnivia_core.contracts.v1 import (
    ERROR_CODE_INTERNAL_NON_RECOVERABLE,
    ERROR_CODE_INVALID_REQUEST,
    ERROR_CODE_NOT_FOUND,
)
from omnivia_core.contracts.v1.generated import ChatQueryInput, ChatQueryResult
from omnivia_core_runtime.service.chat_snapshot import message_document, timestamp
from omnivia_core_runtime.service.operations import OperationContext, OperationError
from omnivia_core_runtime.service.pagination import (
    PROCESS_CONTINUATION_TOKENS,
    ContinuationTokenCodec,
    token_digest,
)
from omnivia_core_runtime.storage.chat import (
    BranchListEntry,
    ConversationListEntry,
    read_accessible_descendants,
    read_branch_page,
    read_conversation,
    read_conversation_page,
    read_message_ancestry_accessible,
)
from omnivia_core_runtime.storage.connection import StorageError

__all__ = ["resolve_chat_query"]

BRANCH_LIST_QUERY: Final = "BranchListQuery"
DESCENDANTS_QUERY: Final = "DescendantsQuery"
CONVERSATION_LIST_QUERY: Final = "ConversationListQuery"

_TOKEN_VERSION: Final = 1
_TOKEN_KEYS: Final = frozenset({"a", "c", "v"})
#: `queries.schema.json#/$defs/ConversationSummary.title` is `maxLength: 512`; the
#: column admits 2048 bytes, so the summary shows a bounded prefix, never a refusal.
_TITLE_MAX_CHARS: Final = 512

_MESSAGE_NOT_FOUND: Final = "no accessible chat data is available for this request"
_MESSAGE_CURSOR: Final = (
    "the chat query cursor is not valid for this query"
)
_MESSAGE_INTERNAL: Final = (
    "the chat query could not be answered from consistent durable state"
)


def _invalid(field: str, rule: str) -> OperationError:
    return OperationError(
        ERROR_CODE_INVALID_REQUEST, f"the chat query field {field!r} {rule}"
    )


def _not_found() -> OperationError:
    return OperationError(ERROR_CODE_NOT_FOUND, _MESSAGE_NOT_FOUND)


def _internal() -> OperationError:
    return OperationError(ERROR_CODE_INTERNAL_NON_RECOVERABLE, _MESSAGE_INTERNAL)


def _optional(document: dict[str, Any], field: str, value: Any) -> None:
    if value is not None:
        document[field] = value


def _decode(
    name: str, query: Mapping[str, Any], context: OperationContext
) -> Mapping[str, Any]:
    """The query document, held to its exact definition and to the trusted envelope."""
    refusal: OperationError | None = None
    try:
        validate_chat_query(name, query)
    except ChatContractDecodeError as error:
        refusal = _invalid(error.path or "query", error.rule)
    if refusal is not None:
        raise refusal
    if query["workspaceId"] != context.workspace_id:
        raise _invalid("workspaceId", "is not the workspace this request was authorized for")
    if query["actorId"] != context.principal:
        raise _invalid("actorId", "is not the principal this request was authenticated as")
    if query["requestId"] != context.request.metadata.request_id:
        raise _invalid("requestId", "is not the identifier of this request")
    return query


def _binding(name: str, context: OperationContext, **facts: Any) -> str:
    """The digest a cursor is bound to: query, trusted identity, filter and sort."""
    return token_digest(
        {
            "query": name,
            "workspace": context.workspace_id,
            "actor": context.principal,
            **facts,
        }
    )


def _decode_cursor(
    tokens: ContinuationTokenCodec, cursor: str | None, binding: str, shape: tuple[type, ...]
) -> tuple[Any, ...] | None:
    """The keyset position a cursor names, or a refusal; `None` for a first page."""
    if cursor is None:
        return None
    payload: Mapping[str, Any] | None = None
    try:
        payload = tokens.decode(cursor)
    except ValueError:
        pass
    if (
        payload is None
        or set(payload) != _TOKEN_KEYS
        or payload["v"] != _TOKEN_VERSION
        or payload["c"] != binding
    ):
        raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_CURSOR)
    position = payload["a"]
    if (
        not isinstance(position, list)
        or len(position) != len(shape)
        or any(type(item) is not kind for item, kind in zip(position, shape, strict=True))
    ):
        raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_CURSOR)
    return tuple(position)


def _encode_cursor(
    tokens: ContinuationTokenCodec, binding: str, position: list[Any]
) -> str:
    return tokens.encode({"a": position, "c": binding, "v": _TOKEN_VERSION})


def _require_conversation(
    connection: sqlite3.Connection, context: OperationContext, conversation_id: str
) -> None:
    conversation = read_conversation(
        connection, workspace_id=context.workspace_id, conversation_id=conversation_id
    )
    if conversation is None or conversation.state == "tombstoned":
        raise _not_found()


def _branch_summary(entry: BranchListEntry) -> Mapping[str, Any]:
    branch = entry.branch
    document: dict[str, Any] = {
        "branchId": branch.branch_id,
        "originKind": branch.origin_kind,
        "state": branch.state,
        "currentHeadMessageId": branch.current_head_message_id,
        "headVersion": branch.head_version,
    }
    if entry.last_advanced_at_us is not None:
        document["lastAdvancedAt"] = timestamp(entry.last_advanced_at_us)
    return document


def _branch_list(
    connection: sqlite3.Connection,
    query: Mapping[str, Any],
    context: OperationContext,
    tokens: ContinuationTokenCodec,
) -> dict[str, Any]:
    conversation_id = query["conversationId"]
    include_archived = query.get("includeArchived", False)
    limit = query["pageSize"]
    binding = _binding(
        BRANCH_LIST_QUERY,
        context,
        conversation=conversation_id,
        includeArchived=include_archived,
        sort="created_at_us,branch_id",
    )
    after = _decode_cursor(tokens, query.get("cursor"), binding, (int, str))
    _require_conversation(connection, context, conversation_id)
    rows = read_branch_page(
        connection,
        workspace_id=context.workspace_id,
        conversation_id=conversation_id,
        include_archived=include_archived,
        after=after,
        limit=limit + 1,
    )
    page = rows[:limit]
    result: dict[str, Any] = {
        "branches": [_branch_summary(entry) for entry in page],
        "hasMore": len(rows) > limit,
    }
    if result["hasMore"]:
        last = page[-1].branch
        result["nextCursor"] = _encode_cursor(
            tokens, binding, [last.created_at_us, last.branch_id]
        )
    return result


def _descendants(
    connection: sqlite3.Connection,
    query: Mapping[str, Any],
    context: OperationContext,
    tokens: ContinuationTokenCodec,
) -> dict[str, Any]:
    conversation_id = query["conversationId"]
    from_message_id = query["fromMessageId"]
    limit = query["pageSize"]
    binding = _binding(
        DESCENDANTS_QUERY,
        context,
        conversation=conversation_id,
        fromMessage=from_message_id,
        sort="conversation_sequence",
    )
    after = _decode_cursor(tokens, query.get("cursor"), binding, (int,))
    _require_conversation(connection, context, conversation_id)
    # The ancestor and every message above it must be accessible before a single
    # descendant is read: a path is whole or it is nothing.
    if (
        read_message_ancestry_accessible(
            connection,
            workspace_id=context.workspace_id,
            conversation_id=conversation_id,
            message_id=from_message_id,
        )
        is not True
    ):
        raise _not_found()
    rows = read_accessible_descendants(
        connection,
        workspace_id=context.workspace_id,
        conversation_id=conversation_id,
        from_message_id=from_message_id,
        after_sequence=0 if after is None else after[0],
        limit=limit + 1,
    )
    page = rows[:limit]
    result: dict[str, Any] = {
        "messages": [message_document(message, parts) for message, parts in page],
        "hasMore": len(rows) > limit,
    }
    if result["hasMore"]:
        result["nextCursor"] = _encode_cursor(
            tokens, binding, [page[-1][0].conversation_sequence]
        )
    return result


def _conversation_summary(entry: ConversationListEntry) -> Mapping[str, Any]:
    conversation = entry.conversation
    document: dict[str, Any] = {
        "conversationId": conversation.conversation_id,
        "state": conversation.state,
        "graphRevision": conversation.graph_revision,
    }
    if conversation.title is not None:
        document["title"] = conversation.title[:_TITLE_MAX_CHARS]
    _optional(document, "activeBranchId", entry.active_branch_id)
    _optional(document, "matchedMessageId", entry.matched_message_id)
    _optional(document, "matchedBranchId", entry.matched_branch_id)
    return document


def _conversation_list(
    connection: sqlite3.Connection,
    query: Mapping[str, Any],
    context: OperationContext,
    tokens: ContinuationTokenCodec,
) -> dict[str, Any]:
    # Absent `state` is the active list; archived conversations are asked for by name.
    state = query.get("state", "active")
    limit = query["pageSize"]
    search = (query.get("searchText") or "").strip() or None
    binding = _binding(
        CONVERSATION_LIST_QUERY,
        context,
        state=state,
        search=search,
        sort="updated_at_us desc,conversation_id desc",
    )
    after = _decode_cursor(tokens, query.get("cursor"), binding, (int, str))
    # A tombstoned conversation is never accessible, so that list is empty by definition.
    rows = (
        ()
        if state == "tombstoned"
        else read_conversation_page(
            connection,
            workspace_id=context.workspace_id,
            actor_id=context.principal,
            state=state,
            search_text=search,
            after=after,
            limit=limit + 1,
        )
    )
    page = rows[:limit]
    result: dict[str, Any] = {
        "conversations": [_conversation_summary(entry) for entry in page],
        "hasMore": len(rows) > limit,
    }
    if result["hasMore"]:
        last = page[-1].conversation
        result["nextCursor"] = _encode_cursor(
            tokens, binding, [last.updated_at_us, last.conversation_id]
        )
    return result


_READS: Final = {
    BRANCH_LIST_QUERY: _branch_list,
    DESCENDANTS_QUERY: _descendants,
    CONVERSATION_LIST_QUERY: _conversation_list,
}


def resolve_chat_query(
    connection: sqlite3.Connection,
    request: ChatQueryInput,
    context: OperationContext,
    *,
    tokens: ContinuationTokenCodec = PROCESS_CONTINUATION_TOKENS,
) -> Mapping[str, Any]:
    """One `chat.query` request, as the `ChatQueryResult` wire mapping.

    Raises `invalid_request` for a document the named query does not admit, one that
    disagrees with the envelope, or a cursor that is not this query's; `not_found`
    for anything inaccessible; and `internal_non_recoverable` where durable state
    cannot be projected as the Chat Contract's own result document.
    """
    name = request.query_name
    read = _READS.get(name)
    if read is None:  # the envelope's enumeration is closed; this is belt and braces
        raise _invalid("query_name", "is not a Chat Contract v1 list query")
    query = _decode(name, request.query, context)
    owns_transaction = not connection.in_transaction
    if owns_transaction:
        connection.execute("BEGIN")
    storage_failed = False
    try:
        result = read(connection, query, context, tokens)
    except BaseException as error:
        if owns_transaction:
            connection.execute("ROLLBACK")
        if not isinstance(error, StorageError):
            raise
        storage_failed = True
    if storage_failed:
        raise _internal()
    if owns_transaction:
        connection.execute("COMMIT")
    result_invalid = False
    try:
        validate_chat_query_result(name, result)
    except ChatContractDecodeError:
        result_invalid = True
    if result_invalid:
        raise _internal()
    return ChatQueryResult(query_name=name, query_result=result).to_wire()
