# Core `chat.query`: one closed read operation for the hosted list queries

Date: 2026-10-22. Status: implemented in this branch.

## Decision

Core adds exactly one Application Contract operation, `chat.query`, for the three
Chat Contract v1 list reads the hosted UI needs and `chat.snapshot` does not carry.
It is workspace-scoped, a read (`side_effect: none`), `chat:read` / `chat.read`,
authorised under the `chat_observation` purpose like `chat.snapshot`, `chat.events`
and `chat.draft`. The generic CLI dispatch exposes it as `omnivia chat query`.

## The allow-list

`query_name` is a closed enumeration. Nothing else is servable at any version.

| `query_name` | Query document | Result document |
| --- | --- | --- |
| `BranchListQuery` | `queries.schema.json#/$defs/BranchListQuery` | `BranchListResult` |
| `DescendantsQuery` | `queries.schema.json#/$defs/DescendantsQuery` | `DescendantsResult` |
| `ConversationListQuery` | `queries.schema.json#/$defs/ConversationListQuery` | `ConversationListResult` |

`query` and `query_result` are carried opaquely at the Application envelope, as
`chat.command` carries its command: restating the Chat shapes in the Application
Contract would be a second, drifting copy. The resolver validates both documents
against the exact Chat Contract definition the name selects, through the Chat codec
(`codec.validate_chat_query` / `validate_chat_query_result`, backed by the generated
`VALIDATION_SCHEMAS`), so a name that does not match its document is refused before
any storage read. The query shapes in `contracts/chat/v1` are unchanged.

## Why not rows, tables or an arbitrary query name

A generic query operation, or one that returned database rows, would make the
renderer the author of what Core reads. Core stays the sole authority for
persistence, identity, access control, pagination and search: no query carries a SQL
fragment, an offset, a column or a table name, and every result is a Chat Contract
document, not a row. The renderer holds no database access.

## Authority boundary

- Workspace, actor and request id in the query must equal the trusted request
  facts (workspace the request was authorised for, the authenticated principal, the
  envelope's own `request_id`). A document may restate them, never contradict them.
  A mismatch is `invalid_request` and the refusal names the field, never the value.
- There is no per-conversation ACL in the model. Visibility is the rule
  `chat.snapshot` already applies (workspace plus authenticated actor), narrowed by
  durable lifecycle facts: a tombstoned conversation, branch or message, and an
  `internal` message or part, are not accessible.
- An absent, tombstoned or foreign conversation, and a `fromMessageId` that is
  absent, in another conversation, or whose ancestry to the root includes an
  inaccessible message, all refuse as one constant `not_found`. No partial path is
  assembled and the refusal is not an oracle for which case it was.

## Semantics

- **BranchListQuery**: branch rows of the conversation by `(created_at_us,
  branch_id)`; `includeArchived` adds archived branches; tombstoned branches are
  never listed. Reads branch rows and the head event instant only, so listing never
  loads a transcript.
- **DescendantsQuery**: the strict subtree under `fromMessageId` (the divergence
  point), by `conversation_sequence`, with parts. The walk descends only through
  accessible messages, so nothing reachable solely through a hidden one is returned.
  The query carries no device, so "inactive" is not actor-relative and the subtree
  is not filtered by any actor's selected path.
- **ConversationListQuery**: conversations in one state by `(updated_at_us DESC,
  conversation_id DESC)`. An absent `state` is the active list; archived is asked for
  by name; a tombstoned list is empty. `searchText` is a literal, case-insensitive
  (ASCII) substring of the title or any accessible `text` part, and names the newest
  accessible matching message and its branch. Hidden messages and parts never match.
  `activeBranchId` is the actor's own selected branch, else the conversation default.
  Titles are shown to the summary's 512 character bound.

## Bounded cursors

`pageSize` is the Chat Contract's own 1 to 200; one extra row is read to state
`hasMore`. A cursor is the existing signed token codec (`service/pagination.py`)
over a versioned payload: a keyset position (never an offset) and a digest of the
query name, trusted workspace and actor, and every filter, search and sort fact the
page was read under. A cursor that is malformed, tampered, from another process, of
another version, or replayed under a different query, filter, search, actor or
workspace is refused as `invalid_request`.

## Storage

No migration. The reads use `omnivia_idx_chat_message_branches_conversation`,
`omnivia_idx_chat_conversations_list` and the message and part primary keys. Search
is a substring scan with no FTS index, and a descendants page re-walks its subtree;
both are bounded per page but linear in a conversation's size, and are marked in
code as the places a projection or FTS index would be added if workspaces grow.
